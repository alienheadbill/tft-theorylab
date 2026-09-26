from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .analytics import available_balance_windows, carry_commitment_stats, default_balance_window, discover_candidates
from .cdragon import CommunityDragonClient, SetMetadata
from .config import Settings
from .demo import generate_demo_matches
from .experiments import (
    Experiment,
    ExperimentError,
    ExperimentNotFound,
    FIELD_NOTE_KINDS,
    add_field_note,
    create_experiment,
    get_experiment,
    list_experiments,
    update_experiment,
)
from .ingest import DEFAULT_MAX_LADDER_PAGES, MAX_LADDER_PAGES, default_run_id, ingest_ladder
from .sampling import SAMPLING_MODES
from .unreal_patch import NoCurrentTrustedWindow
from .unreal_patch import current_trusted_window as current_trusted_window_for
from .normalize import CostLookup
from .riot import RiotApiError, RiotClient, classify_riot_error
from .storage import Database
from .validate import validate_live_data
from .scout import LOW_SAMPLE_COMMITMENT_GAMES, evidence_summary, record_riot_evidence, scout
from .sources import RESEARCH_LABELS, SOURCES, scout_checklist

app = typer.Typer(help="TFT Theory Lab: discover low-usage, data-backed carry lines.")
console = Console()


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


class CommunityDragonUnavailable(RuntimeError):
    """Static metadata was required but CommunityDragon couldn't be reached
    and degraded (rarity+1) costs weren't explicitly allowed."""


def _fetch_current_metadata() -> SetMetadata:
    with CommunityDragonClient() as cdragon:
        return cdragon.get_set_metadata("latest")


def _resolve_cost_lookup(
    *,
    use_static_costs: bool,
    allow_degraded_costs: bool,
    fetch_metadata: Callable[[], SetMetadata] = _fetch_current_metadata,
) -> tuple[CostLookup | None, bool, int | None]:
    """Decide how ingested units get their shop cost.

    Returns `(cost_lookup, degraded, set_number)`. When CommunityDragon is
    unreachable and `allow_degraded_costs` is False (the default), this
    raises `CommunityDragonUnavailable` instead of quietly falling back to
    `rarity + 1` -- production ingestion should fail loudly rather than
    silently trusting an heuristic as authoritative cost data. Passing
    `allow_degraded_costs=True` allows the fallback, but the caller must
    still surface `degraded=True` prominently (see `ingest_riot` below).
    """
    if not use_static_costs:
        return None, False, None
    try:
        metadata = fetch_metadata()
    except Exception as exc:
        if not allow_degraded_costs:
            raise CommunityDragonUnavailable(
                f"CommunityDragon metadata unavailable ({exc}). Refusing to silently use rarity+1 "
                "for production cost data. Pass --allow-degraded-costs to proceed anyway."
            ) from exc
        return None, True, None
    return metadata.cost_for_champion, False, metadata.set_number


def _resolve_db_target(db: str | None) -> str:
    """Pick a database target from an explicit `--db` value, else
    `DATABASE_URL`, else the local SQLite path from settings.

    `--db` is deliberately typed as a plain string, not typer's `Path`:
    `pathlib.Path("postgresql://user:pass@host/db")` collapses the `//`
    after the scheme (`Path` normalizes repeated slashes), silently turning
    a valid Postgres URL into a broken one that `Database` would
    misidentify as a SQLite file path.
    """
    if db:
        return db
    _load_dotenv()
    settings = Settings.from_env()
    return settings.database_url or str(settings.db_path)


def _print_stats(stats) -> None:
    table = Table(title="Hidden Reroll Leaderboard")
    table.add_column("Carry")
    table.add_column("Cost", justify="right")
    table.add_column("Commit", justify="right")
    table.add_column("Usage", justify="right")
    table.add_column("Avg", justify="right")
    table.add_column("Top4", justify="right")
    table.add_column("3★ Hit", justify="right")
    table.add_column("Hit T4", justify="right")
    table.add_column("Miss T4", justify="right")
    table.add_column("Score", justify="right")
    for s in stats:
        table.add_row(
            s.name,
            str(s.cost),
            str(s.commitment_games),
            f"{s.usage_rate:.2%}",
            f"{s.avg_placement:.2f}",
            f"{s.top4_rate:.1%}",
            f"{s.hit_3star_rate:.1%}",
            "—" if s.hit_top4_rate is None else f"{s.hit_top4_rate:.1%}",
            "—" if s.miss_top4_rate is None else f"{s.miss_top4_rate:.1%}",
            f"{s.opportunity_score:.1f}",
        )
    console.print(table)


@app.command()
def init(db: Path = typer.Option(Path("data/tftlab.sqlite3"), "--db")) -> None:
    """Create the local database."""
    with Database(db):
        pass
    console.print(f"Initialized {db}")


@app.command()
def demo(
    db: Path = typer.Option(Path("data/demo.sqlite3"), "--db"),
    matches: int = typer.Option(120, min=1),
) -> None:
    """Run the full pipeline with deterministic fake data."""
    if db.exists():
        db.unlink()
    with Database(db) as database:
        inserted = database.ingest_many(generate_demo_matches(matches))
        stats = carry_commitment_stats(database, min_samples=10)
    console.print(f"Inserted {inserted} demo matches / {inserted * 8} participants")
    _print_stats(stats)


@app.command("ingest-riot")
def ingest_riot(
    players: int | None = typer.Option(
        None, min=1, help="Legacy weighted sampling: total seed players (default 25). Not with --<cohort>-seeds."
    ),
    matches_per_player: int = typer.Option(10, min=1, max=100),
    sampling: str | None = typer.Option(
        None,
        help=(
            "Legacy weighted sampling: 'challenger' (default, Challenger only) or 'high_elo' "
            "(Challenger / Grandmaster / Master, 4:3:3). Not with --<cohort>-seeds."
        ),
    ),
    include_master: bool = typer.Option(False, help="Same as --sampling high_elo"),
    challenger_seeds: int | None = typer.Option(None, min=0, help="Seeds from the Challenger cohort"),
    grandmaster_seeds: int | None = typer.Option(None, min=0, help="Seeds from the Grandmaster cohort"),
    master_seeds: int | None = typer.Option(None, min=0, help="Seeds from the Master cohort"),
    diamond_seeds: int | None = typer.Option(None, min=0, help="Seeds from the Diamond cohort (Diamond I-IV)"),
    platinum_seeds: int | None = typer.Option(None, min=0, help="Seeds from the Platinum cohort (Platinum I-IV)"),
    max_ladder_pages: int = typer.Option(
        DEFAULT_MAX_LADDER_PAGES,
        min=1,
        max=MAX_LADDER_PAGES,
        help="Pages read per Diamond/Platinum division (stops early at an empty page)",
    ),
    current_trusted_window: bool = typer.Option(
        False,
        "--current-trusted-window",
        help=(
            "Only request match history inside the current trusted patch window (the latest "
            "verified+sourced tftlab.unreal_patch window that has started and not ended). "
            "Fails if there is none."
        ),
    ),
    start_time: str | None = typer.Option(
        None,
        "--start-time",
        help="Only request match history from this UTC time on, e.g. 2026-09-24T07:00:00Z",
    ),
    use_static_costs: bool = typer.Option(
        True, help="Resolve champion shop costs from CommunityDragon instead of rarity+1"
    ),
    allow_degraded_costs: bool = typer.Option(
        False,
        help=(
            "If CommunityDragon is unreachable, proceed anyway using rarity+1 (and mark the "
            "ingest as degraded) instead of aborting"
        ),
    ),
) -> None:
    """Pull recent ranked matches from Riot into the configured database.

    Explicit cohorts: `--challenger-seeds 20 --grandmaster-seeds 20
    --master-seeds 20 --diamond-seeds 20 --platinum-seeds 20` (each may be
    0; a cohort left out is 0). Each cohort is selected, counted and
    reported separately -- there is no combined cohort. Without any
    `--<cohort>-seeds`, the legacy weighted modes apply (Challenger-only by
    default; `--sampling high_elo` is Challenger / Grandmaster / Master
    4:3:3). Either way seeds rotate: never-sampled players first, then the
    least recently sampled, spread across each tier.

    The population is recent NA standard Ranked TFT matches discovered
    through those ladder players -- not all TFT games. A seed cohort says how
    a lobby was discovered, not every player's rank. Analytics look at every
    champion, item and trait in those matches, all cohorts combined.
    """
    _load_dotenv()
    settings = Settings.from_env()
    if not settings.riot_api_key:
        raise typer.BadParameter("Set RIOT_API_KEY in .env or the environment")
    cohort_options = {
        "challenger": challenger_seeds,
        "grandmaster": grandmaster_seeds,
        "master": master_seeds,
        "diamond": diamond_seeds,
        "platinum": platinum_seeds,
    }
    seed_allocation = None
    if any(v is not None for v in cohort_options.values()):
        if players is not None or sampling is not None or include_master:
            raise typer.BadParameter(
                "use either --<cohort>-seeds or the legacy --players/--sampling/--include-master, not both"
            )
        seed_allocation = {c: v or 0 for c, v in cohort_options.items()}
        if sum(seed_allocation.values()) < 1:
            raise typer.BadParameter("request at least one seed across the cohorts")
        sampling_mode = "cohorts"
    else:
        sampling_mode = "high_elo" if include_master else (sampling or "challenger")
        if sampling_mode not in SAMPLING_MODES:
            raise typer.BadParameter(
                f"--sampling must be one of: {', '.join(SAMPLING_MODES)}", param_hint="--sampling"
            )
    # Resolved before any network or database access.
    history_start, history_end, window_label = _history_bounds(current_trusted_window, start_time)

    try:
        cost_lookup, degraded, set_number = _resolve_cost_lookup(
            use_static_costs=use_static_costs,
            allow_degraded_costs=allow_degraded_costs,
        )
    except CommunityDragonUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    if degraded:
        console.print(
            "[bold yellow]DEGRADED INGEST: CommunityDragon was unreachable; shop costs will use "
            "the rarity+1 heuristic instead of authoritative data.[/bold yellow]"
        )
    elif set_number is not None:
        console.print(f"Using CommunityDragon costs for set {set_number}")

    target = settings.database_url or settings.db_path
    run_id = default_run_id(int(datetime.now(timezone.utc).timestamp() * 1000))
    with Database(target) as db, RiotClient(
        settings.riot_api_key, platform=settings.platform, region=settings.region
    ) as client:
        try:
            result = ingest_ladder(
                client,
                db,
                player_limit=players or 25,
                matches_per_player=matches_per_player,
                sampling_mode=sampling_mode,
                seed_allocation=seed_allocation,
                max_ladder_pages=max_ladder_pages,
                cost_lookup=cost_lookup,
                history_start_time=history_start,
                history_end_time=history_end,
                run_id=run_id,
            )
        except Exception as exc:
            if isinstance(exc, RiotApiError) and classify_riot_error(str(exc)) == "unauthorized":
                console.print(f"[red]{EXPIRED_KEY_MESSAGE}[/red]")
            # Not swallowed: the traceback follows. Matches stored before the
            # failure stay stored; the seed ledger/provenance for this run was
            # never finalized, so the next run's rotation ignores it.
            console.print(
                f"[red]Ingest run {run_id} failed ({type(exc).__name__}); the run remains incomplete "
                "and seed rotation will ignore it on the next run.[/red]"
            )
            raise
        windows = available_balance_windows(db)
        total_participants = db.query_one("SELECT COUNT(*) FROM participants")[0]

    def _rate(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    def _ratio(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    console.print("\n[bold]Ingest report[/bold]")
    console.print(f"  Sampling mode: {result.sampling_mode}")
    console.print(f"  Requested seed players: {result.requested_seeds}")
    console.print(f"  Run id: {result.run_id}")
    console.print(f"  Ingest run status: {result.run_status}")
    console.print(f"  Seed players: {result.seed_players}")
    console.print(
        "  Seed cohorts (sampling provenance: how lobbies were discovered, not every player's rank):"
    )
    for tier, report in result.cohort_reports.items():
        console.print(f"    {tier}: {report.selected} selected (requested {report.requested})")
        if report.pagination_complete is None:
            console.print(f"      available ladder entries: {report.fetched_candidates}")
        else:
            console.print(f"      fetched candidate pool: {report.fetched_candidates}")
            if report.pagination_complete:
                console.print("      pagination: complete (every division reached an empty page)")
            else:
                console.print(
                    f"      pagination: capped at {report.max_ladder_pages} pages/division; "
                    "additional players may exist"
                )
        console.print(
            f"      never sampled before: {report.never_sampled_selected}, "
            f"previously sampled: {report.previously_sampled_selected}"
        )
        console.print(
            f"      seeds with no matches in the requested history: {report.seeds_with_empty_history}, "
            f"failed history requests: {report.failed_history_requests}"
        )
        console.print(
            f"      match-ID references: {report.match_id_references}, unique match IDs: {report.unique_match_ids}, "
            f"ladder requests: {report.ladder_requests}"
        )
    console.print(f"  Requested histories per seed: {result.histories_per_seed}")
    console.print(f"  Trusted window: {window_label or 'none (ordinary recent history)'}")
    console.print(f"  History lower bound (startTime): {_format_epoch_s(result.history_start_time)}")
    console.print(f"  History upper bound (endTime): {_format_epoch_s(result.history_end_time)}")
    console.print(f"  Seeds with no matches in the requested history: {result.seeds_with_empty_history}")
    console.print(f"  Failed history requests: {result.failed_history_requests}")
    console.print(f"  Match-ID references (before dedupe): {result.match_id_references}")
    console.print(f"  Unique match IDs discovered: {result.match_ids_seen}")
    console.print(f"  Unique match IDs found by more than one cohort: {result.cross_cohort_match_ids}")
    console.print(f"  Matches skipped as duplicates: {result.duplicates_skipped}")
    console.print(f"  Matches fetched: {result.matches_fetched}")
    console.print(f"  Matches inserted: {result.matches_inserted}")
    console.print(f"  Failed requests: {result.failed_requests}")
    console.print(f"  Non-ranked-queue matches skipped: {result.non_target_matches_skipped}")
    never = sum(r.never_sampled_selected for r in result.cohort_reports.values())
    previously = sum(r.previously_sampled_selected for r in result.cohort_reports.values())
    seeds = result.seed_players or None
    console.print(
        "  Collection value (is another run worth it?): "
        f"never-sampled seeds {_rate(never / seeds if seeds else None)}, "
        f"previously-sampled seeds {_rate(previously / seeds if seeds else None)}, "
        f"zero-history seeds {_rate(result.seeds_with_empty_history / seeds if seeds else None)}"
    )
    console.print(f"  In-run overlap rate: {_rate(result.in_run_overlap_rate)}")
    console.print(f"  Already-stored duplicate rate: {_rate(result.known_duplicate_rate)}")
    console.print(f"  Inserted matches per seed: {_ratio(result.inserted_per_seed)}")
    console.print(f"  New-match yield (inserted / references): {_rate(result.new_match_yield)}")
    console.print(f"  Earliest inserted match: {_format_epoch_ms(result.earliest_inserted_game_datetime)}")
    console.print(f"  Latest inserted match: {_format_epoch_ms(result.latest_inserted_game_datetime)}")
    if history_start is not None:
        console.print(
            "  Note: startTime narrows what Riot returns; each match's patch and balance window "
            "still come from normal classification -- see validate-live-data."
        )
    console.print(
        f"  Database deadlock retries: {result.deadlock_retries} "
        f"(matches needing a retry: {result.matches_with_deadlock_retry}, "
        f"recovered: {result.deadlocks_recovered})"
    )
    console.print(f"  Seed ledger rows finalized: {result.seed_ledger_rows}")
    console.print(
        f"  Provenance rows finalized: {result.discovery_rows} "
        f"(for {result.matches_with_provenance} stored matches, each stored once)"
    )
    console.print(f"  Balance windows found: {', '.join(w for w, _, _ in windows) or 'none'}")
    console.print(f"  Total participants now stored: {total_participants}")
    if degraded:
        console.print("[yellow]  Note: this ingest used degraded (rarity+1) costs.[/yellow]")
    if result.seed_players and result.failed_history_requests == result.seed_players:
        console.print("[red]Every seed's match-history request failed; nothing could be sampled.[/red]")
        raise typer.Exit(code=1)


#: Development keys deactivate every 24 hours (Riot Developer Portal). Never
#: includes the key itself.
EXPIRED_KEY_MESSAGE = (
    "401 Unauthorized -- RIOT_API_KEY is invalid or expired. Reset the development key in the "
    "Riot Developer Portal and replace the GitHub Actions RIOT_API_KEY secret."
)


@app.command("verify-riot")
def verify_riot() -> None:
    """Verify RIOT_API_KEY with one minimal authenticated request.

    Does not ingest anything. Never prints the key itself.
    """
    _load_dotenv()
    settings = Settings.from_env()
    if not settings.riot_api_key:
        console.print("[red]RIOT_API_KEY is not set.[/red]")
        raise typer.Exit(code=1)

    try:
        with RiotClient(
            settings.riot_api_key, platform=settings.platform, region=settings.region
        ) as client:
            payload = client.challenger()
    except RiotApiError as exc:
        category = classify_riot_error(str(exc))
        messages = {
            "unauthorized": EXPIRED_KEY_MESSAGE,
            "forbidden": "403 Forbidden -- RIOT_API_KEY lacks permission for this endpoint/region.",
            "rate_limited": "429 Rate limited -- back off and retry later; Riot enforces per-key rate limits.",
            "network_error": "Network error contacting Riot API -- check connectivity and try again.",
        }
        console.print(f"[red]Riot API verification failed: {messages.get(category, str(exc))}[/red]")
        raise typer.Exit(code=1)

    entries = len(payload.get("entries", []))
    console.print(
        f"[green]RIOT_API_KEY is valid.[/green] platform={settings.platform} challenger entries={entries}"
    )


def _format_epoch_s(seconds: int | None) -> str:
    if seconds is None:
        return "none"
    return f"{seconds} ({datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()})"


def _history_bounds(current_trusted_window: bool, start_time: str | None) -> tuple[int | None, int | None, str | None]:
    """(startTime, endTime, label) for Riot match-history requests, in
    epoch seconds (Riot's unit for these parameters)."""
    if current_trusted_window and start_time:
        raise typer.BadParameter("use either --current-trusted-window or --start-time, not both")
    if current_trusted_window:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        try:
            window = current_trusted_window_for(now_ms)
        except NoCurrentTrustedWindow as exc:
            console.print(f"[red]No current trusted window: {exc}[/red]")
            raise typer.Exit(code=1)
        return window.starts_at // 1000, window.ends_at // 1000, f"{window.client_patch} (verified: {window.source})"
    if start_time:
        try:
            parsed = datetime.fromisoformat(start_time)
        except ValueError:
            raise typer.BadParameter("must be an ISO-8601 time such as 2026-09-24T07:00:00Z", param_hint="--start-time")
        if parsed.tzinfo is None:
            raise typer.BadParameter("include a timezone, e.g. a trailing Z for UTC", param_hint="--start-time")
        return int(parsed.timestamp()), None, None
    return None, None, None


def _format_epoch_ms(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return f"{ms} ({datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()})"


def _print_distribution(label: str, distribution: dict) -> None:
    """Diagnostic-only value -> count table, capped so a store with many
    distinct raw game_version strings doesn't flood the terminal."""
    console.print(f"  {label}:")
    top = sorted(distribution.items(), key=lambda kv: kv[1], reverse=True)[:10]
    for value, count in top:
        console.print(f"    {value!r}: {count}")
    if len(distribution) > len(top):
        console.print(f"    ... and {len(distribution) - len(top)} more distinct value(s)")


@app.command("validate-live-data")
def validate_live_data_command(
    db: str = typer.Option(
        None, "--db", help="SQLite path or postgres:// URL; defaults to DATABASE_URL or TFT_DB_PATH"
    ),
    balance_window: str = typer.Option(
        None, help="Balance window to validate; defaults to the chronologically latest one in the store"
    ),
    check_metadata: bool = typer.Option(
        True, help="Cross-check champion/item/trait IDs against a live CommunityDragon fetch"
    ),
) -> None:
    """Data-integrity checks for ingested live data. Non-zero exit on severe issues."""
    metadata = None
    if check_metadata:
        try:
            metadata = _fetch_current_metadata()
        except Exception as exc:
            console.print(
                f"[yellow]CommunityDragon metadata unavailable ({exc}); skipping unknown-ID checks.[/yellow]"
            )

    with Database(_resolve_db_target(db)) as database:
        report = validate_live_data(database, balance_window=balance_window, metadata=metadata)

    console.print(f"Balance window: {report.balance_window}")
    console.print(f"Total matches: {report.total_matches}")
    console.print(f"  Ranked TFT (target queue): {report.target_queue_matches}")
    console.print(f"  Non-target queue (Normal/Hyper Roll/Double Up/other): {report.non_target_queue_matches}")
    console.print(f"Total participants: {report.total_participants}")
    console.print(f"Unit shop-cost present: {report.unit_cost_present_pct:.1%}")
    if report.metadata_champion_coverage_pct is None:
        console.print("CommunityDragon champion coverage: skipped (no CommunityDragon metadata)")
    else:
        console.print(f"CommunityDragon champion coverage: {report.metadata_champion_coverage_pct:.1%}")

    def _report_unknown(label: str, values: list[str] | None) -> None:
        if values is None:
            console.print(f"{label}: skipped (no CommunityDragon metadata)")
            return
        preview = f" ({', '.join(values[:10])}{', ...' if len(values) > 10 else ''})" if values else ""
        console.print(f"{label}: {len(values)}{preview}")

    _report_unknown("Unknown champion IDs", report.unknown_champion_ids)
    _report_unknown("Unknown item IDs", report.unknown_item_ids)
    _report_unknown("Unknown trait IDs", report.unknown_trait_ids)

    console.print(f"Matches missing balance_window: {report.matches_missing_balance_window}")
    console.print(f"  Expected Unreal rollout-gap unresolved: {report.unresolved_unreal_matches}")
    console.print(f"  Unexpected missing balance_window: {report.unexpected_missing_balance_window}")
    console.print(f"Malformed placements: {report.malformed_placements}")
    console.print(f"Duplicate match IDs: {report.duplicate_match_ids}")
    console.print(f"Participants without units: {report.participants_without_units}")
    console.print(f"  Source-empty participants: {report.source_empty_participants}")
    console.print(f"  Unexpected participants without units: {report.unexpected_participants_without_units}")
    if report.source_empty_participants:
        console.print(
            f"[bold yellow]  {report.source_empty_participants} participant(s) were sent by Riot with an empty "
            "or missing `units` list. They are kept as stored (placement intact) and excluded only from "
            "unit-observable denominators; a warning, not corruption.[/bold yellow]"
        )

    if report.unresolved_unreal_matches:
        console.print(
            f"[bold yellow]  {report.unresolved_unreal_matches} match(es) have a masked Unreal-era "
            "game_version with no usable (verified + sourced) window in "
            "tftlab.unreal_patch.UNREAL_PATCH_REGISTRY (e.g. the rollout-gap period between two "
            "patches). This is intentional and safe, not structural corruption: their balance_window "
            "is left unset on purpose (never a fake shared bucket), so they're excluded from every "
            "patch-scoped analytics query, and they do NOT by themselves fail validation. Any "
            "'Unexpected missing balance_window' count above zero still does.[/bold yellow]"
        )
        console.print(
            f"  Earliest: {_format_epoch_ms(report.unresolved_unreal_earliest_game_datetime)}"
        )
        console.print(
            f"  Latest: {_format_epoch_ms(report.unresolved_unreal_latest_game_datetime)}"
        )

    console.print(
        f"Unreal patch registry: {report.unreal_registry_usable_windows}/{report.unreal_registry_total_windows} "
        "window(s) usable (verified + sourced)"
    )
    if report.unreal_registry_total_windows > report.unreal_registry_usable_windows:
        console.print(
            "[bold yellow]  One or more registered windows are unverified or missing a source -- they are "
            "NOT being used to classify any match. Confirm the timestamp against Riot's patch notes and set "
            "verified=True with a source, or the window has no effect.[/bold yellow]"
        )

    console.print("\n[bold]Diagnostics[/bold] (store-wide, not scoped to the balance window above)")
    console.print(f"  Earliest game_datetime: {_format_epoch_ms(report.earliest_game_datetime)}")
    console.print(f"  Latest game_datetime: {_format_epoch_ms(report.latest_game_datetime)}")
    _print_distribution("Raw game_version distribution", report.game_version_distribution)
    _print_distribution("Resolved client patch distribution", report.client_patch_distribution)
    _print_distribution("Balance-window distribution", report.balance_window_distribution)

    if report.is_severe:
        console.print("\n[bold red]SEVERE integrity issues detected.[/bold red]")
        raise typer.Exit(code=1)
    console.print("\n[green]No severe integrity issues detected.[/green]")


@app.command("patch-diagnostics")
def patch_diagnostics_command(
    db: str = typer.Option(
        None, "--db", help="SQLite path or postgres:// URL; defaults to DATABASE_URL or TFT_DB_PATH"
    ),
) -> None:
    """Store-wide, read-only game_version/patch/balance_window diagnostics.

    Makes no Riot API or CommunityDragon calls, runs no discovery/analytics
    queries, and deletes nothing -- it only reads the `matches` table as
    already ingested. Purpose-built to read off the real game_datetime
    range for masked Unreal-era matches (see `tftlab.unreal_patch`) before
    filling in `UNREAL_PATCH_REGISTRY`, without running another Riot
    ingest just to see it.

    Connecting to the database still runs the normal, safe, idempotent
    Unreal-patch backfill migration (`Database._backfill_unreal_patches`),
    same as every other command -- a row still using the pre-fix masked
    fallback value may move to the explicit unresolved state (never
    deleted, never combined into a fake bucket); that's expected and is
    exactly the self-healing behavior the migration is designed to do.
    """
    with Database(_resolve_db_target(db)) as database:
        report = validate_live_data(database, metadata=None)

    total_matches = sum(report.game_version_distribution.values())
    console.print(f"Total matches (store-wide): {total_matches}")
    console.print(f"Earliest game_datetime: {_format_epoch_ms(report.earliest_game_datetime)}")
    console.print(f"Latest game_datetime: {_format_epoch_ms(report.latest_game_datetime)}")
    _print_distribution("Raw game_version distribution", report.game_version_distribution)
    _print_distribution("Current patch distribution", report.client_patch_distribution)
    _print_distribution("Balance-window distribution", report.balance_window_distribution)

    console.print(f"Masked-Unreal (unresolved) matches: {report.unresolved_unreal_matches}")
    console.print(
        f"  Earliest: {_format_epoch_ms(report.unresolved_unreal_earliest_game_datetime)}"
    )
    console.print(
        f"  Latest: {_format_epoch_ms(report.unresolved_unreal_latest_game_datetime)}"
    )
    console.print(
        f"Unreal patch registry: {report.unreal_registry_usable_windows}/{report.unreal_registry_total_windows} "
        "window(s) usable (verified + sourced)"
    )


_LOW_SAMPLE_COMMITMENT_GAMES = LOW_SAMPLE_COMMITMENT_GAMES


def _is_low_sample(candidate) -> bool:
    """Below this many commitment games, treat a candidate's evidence as
    too thin to present without a caveat -- matches the general spirit of
    the confidence/shrinkage thresholds used throughout the analytics
    layer, applied here as a simple, visible label rather than a score
    adjustment."""
    return candidate.commitment_games < _LOW_SAMPLE_COMMITMENT_GAMES


@app.command("discovery-smoke")
def discovery_smoke(
    db: str = typer.Option(
        None, "--db", help="SQLite path or postgres:// URL; defaults to DATABASE_URL or TFT_DB_PATH"
    ),
    max_cost: int = typer.Option(3, min=1, max=5),
    min_samples: int = typer.Option(1, min=1),
    limit: int = typer.Option(10, min=1, max=50),
) -> None:
    """Smoke-test the discovery engine against the latest balance window."""
    with Database(_resolve_db_target(db)) as database:
        window = default_balance_window(database)
        candidates = discover_candidates(
            database, min_cost=1, max_cost=max_cost, min_samples=min_samples, top_n=1
        )[:limit]

    if not candidates:
        console.print(f"[yellow]No qualifying candidates in balance window {window!r}.[/yellow]")
        return

    console.print(f"Discovery smoke test -- balance window: {window}\n")
    for c in candidates:
        label = " [bold red]LOW SAMPLE[/bold red]" if _is_low_sample(c) else ""
        console.print(f"[bold]{c.name}[/bold] ({c.character_id}) -- cost {c.cost}{label}")
        console.print(
            f"  commitment games: {c.commitment_games}   appearance rate: {c.appearance_rate:.1%}   "
            f"commitment rate: {c.commitment_rate:.1%}   conversion: {c.carry_conversion_rate:.1%}"
        )
        console.print(
            f"  avg placement: {c.avg_placement:.2f}   top4: {c.top4_rate:.1%}   "
            f"win: {c.win_rate:.1%}   3-star hit: {c.hit_3star_rate:.1%}"
        )
        console.print(f"  opportunity score: {c.opportunity_score:.2f}")
        if c.best_partners:
            p = c.best_partners[0]
            console.print(f"  top partner: {p.label} (association {p.association_score:.4f}, {p.games} games)")
        else:
            console.print("  top partner: none")
        if c.best_item_packages:
            i = c.best_item_packages[0]
            console.print(
                f"  top item package: {i.label} (association {i.association_score:.4f}, {i.games} games)"
            )
        else:
            console.print("  top item package: none")
        if c.best_trait_breakpoints:
            t = c.best_trait_breakpoints[0]
            console.print(
                f"  top trait breakpoint: {t.label} (association {t.association_score:.4f}, {t.games} games)"
            )
        else:
            console.print("  top trait breakpoint: none")
        console.print("")


@app.command()
def leaderboard(
    db: Path = typer.Option(Path("data/tftlab.sqlite3"), "--db"),
    min_samples: int = typer.Option(20, min=1),
    max_cost: int = typer.Option(3, min=1, max=5),
    balance_window: str = typer.Option(
        None, help="Balance window to analyze; defaults to the chronologically latest one in the store"
    ),
) -> None:
    """Calculate item-commitment carry statistics."""
    with Database(db) as database:
        stats = carry_commitment_stats(
            database, balance_window=balance_window, min_samples=min_samples, max_cost=max_cost
        )
    _print_stats(stats)


# ---------------------------------------------------------------------------
# Theorycraft notebook ("experiments"). Writes happen only here, through the
# owner's CLI against the configured database -- the website is read-only.

_DB_OPTION_HELP = "SQLite path or postgres:// URL; defaults to DATABASE_URL, then TFT_DB_PATH"


def _describe_target(database: Database) -> str:
    # Never echo a connection string: it may carry credentials.
    return "postgres database" if database.dialect == "postgres" else f"sqlite file {database.path}"


def _load_json_file(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"couldn't read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path} must contain a JSON object")
    return data


def _experiment_fields_from_flags(
    *,
    title: str | None,
    slug: str | None,
    carry: str | None,
    carry_id: str | None,
    status: str | None,
    lifecycle: str | None,
    summary: str | None,
    notes: str | None,
    core: list[str] | None,
    optional: list[str] | None,
    trait: list[str] | None,
    carry_item: list[str] | None,
    tank_item: list[str] | None,
    secondary_unit: str | None,
    secondary_item: list[str] | None,
    target_level: int | None,
    reroll_level: int | None,
    roll_timing: str | None,
    positioning: str | None,
    augments: str | None,
) -> dict:
    """Only flags that were actually given end up in the result, so the same
    helper serves both add (fills in) and update (partial change)."""
    fields = {
        "title": title, "slug": slug, "carry_name": carry, "carry_character_id": carry_id,
        "evidence_status": status, "lifecycle": lifecycle, "summary": summary, "author_notes": notes,
    }
    data = {k: v for k, v in fields.items() if v is not None}
    comp_flags = {
        "core_units": core, "optional_units": optional, "target_traits": trait,
        "carry_items": carry_item, "tank_items": tank_item, "target_level": target_level,
        "reroll_level": reroll_level, "roll_timing": roll_timing,
        "positioning_notes": positioning, "augment_notes": augments,
    }
    comp = {k: v for k, v in comp_flags.items() if v not in (None, [])}
    if secondary_unit is not None or secondary_item:
        comp["secondary_carry"] = {"unit": secondary_unit, "items": secondary_item or []}
    if comp:
        data["comp"] = comp
    return data


def _merge_entry(base: dict, overrides: dict) -> dict:
    merged = {**base, **{k: v for k, v in overrides.items() if k != "comp"}}
    if "comp" in overrides:
        merged["comp"] = {**(base.get("comp") or {}), **overrides["comp"]}
    return merged


def _print_experiment(e: Experiment) -> None:
    console.print(f"[bold]{e.title}[/bold]  [magenta][{e.evidence_status}][/magenta]  ({e.lifecycle})")
    console.print(f"  slug: {e.slug}   id: {e.experiment_id}{'   (example entry)' if e.origin == 'demo' else ''}")
    if e.carry_name or e.carry_character_id:
        console.print(f"  carry: {e.carry_name or ''} {f'({e.carry_character_id})' if e.carry_character_id else ''}".rstrip())
    if e.summary:
        console.print(f'  thesis: "{e.summary}"')
    comp = e.comp

    def units(key: str) -> str:
        parts = []
        for u in comp[key]:
            label = u["name"] + (f" {u['star']}★" if u.get("star") else "")
            parts.append(label + (f" ({u['note']})" if u.get("note") else ""))
        return ", ".join(parts)

    lines = [
        ("core", units("core_units")),
        ("optional", units("optional_units")),
        ("traits", ", ".join(
            (f"{t['breakpoint']} " if t.get("breakpoint") else "") + t["name"] for t in comp["target_traits"]
        )),
        ("carry items", ", ".join(comp["carry_items"])),
        ("tank items", ", ".join(comp["tank_items"])),
        ("secondary", "" if not comp["secondary_carry"] else " ".join(filter(None, [
            comp["secondary_carry"].get("unit"), ", ".join(comp["secondary_carry"].get("items") or [])
        ]))),
        ("target level", comp["target_level"] or ""),
        ("reroll level", comp["reroll_level"] or ""),
        ("roll timing", comp["roll_timing"] or ""),
        ("positioning", comp["positioning_notes"] or ""),
        ("augments", comp["augment_notes"] or ""),
        ("notes", e.author_notes or ""),
        ("tags", " ".join(f"#{t}" for t in e.tags)),
    ]
    for label, value in lines:
        if value:
            console.print(f"  {label}: {value}")
    console.print(f"  created {e.created_at}   updated {e.updated_at}")
    if e.field_notes:
        console.print("  [bold]field notes[/bold]")
        for n in e.field_notes:
            _print_note(n)
    _print_checklist(scout_checklist(e.field_notes))


def _print_note(n: dict) -> None:
    source = f" · {n['source_name']}" if n["source_name"] else ""
    extras = " ".join(filter(None, [
        f"[{n['evidence_status']}]" if n["evidence_status"] else "",
        f"({n['research_label_text']})" if n["research_label_text"] else "",
    ]))
    console.print(f"    {n['noted_at'][:10]}  {n['kind_label'].upper()}{source}  {extras}".rstrip())
    console.print(f"      {n['body']}", markup=False)
    if n["source_url"]:
        console.print(f"      {n['source_url']}", markup=False)


def _print_checklist(items: list[dict]) -> None:
    console.print("  [bold]research checklist[/bold] (ticked only when a field note came from that source)")
    for item in items:
        mark = "x" if item["checked"] else " "
        when = f"  last {item['last_noted_at'][:10]}" if item["last_noted_at"] else ""
        console.print(f"    \\[{mark}] {item['label']}{when}")


@app.command("experiment-add")
def experiment_add(
    title: str = typer.Option(None, "--title", help="Required unless given in --from-json"),
    from_json: Path = typer.Option(None, "--from-json", help="JSON file with any entry fields; flags override it"),
    slug: str = typer.Option(None, "--slug", help="URL name; generated from the title if omitted"),
    carry: str = typer.Option(None, "--carry", help="Carry's display name, e.g. \"Kha'Zix\""),
    carry_id: str = typer.Option(None, "--carry-id", help="Carry's character_id, e.g. DA_18_KhaZix"),
    status: str = typer.Option(None, "--status", help="THEORYCRAFTED (default) or VARIANT"),
    lifecycle: str = typer.Option(None, "--lifecycle", help="idea (default), testing, watching, archived"),
    summary: str = typer.Option(None, "--summary", help="One-line thesis"),
    notes: str = typer.Option(None, "--notes", help="Personal notes"),
    core: list[str] = typer.Option(None, "--core", help="Core unit (repeatable)"),
    optional: list[str] = typer.Option(None, "--optional", help="Optional/flex unit (repeatable)"),
    trait: list[str] = typer.Option(None, "--trait", help="Trait target like '6 Ravager' (repeatable)"),
    carry_item: list[str] = typer.Option(None, "--carry-item", help="Carry item (repeatable)"),
    tank_item: list[str] = typer.Option(None, "--tank-item", help="Tank item (repeatable)"),
    secondary_unit: str = typer.Option(None, "--secondary-unit", help="Secondary carry"),
    secondary_item: list[str] = typer.Option(None, "--secondary-item", help="Secondary carry item (repeatable)"),
    target_level: int = typer.Option(None, "--target-level", min=1, max=11),
    reroll_level: int = typer.Option(None, "--reroll-level", min=1, max=11),
    roll_timing: str = typer.Option(None, "--roll-timing"),
    positioning: str = typer.Option(None, "--positioning"),
    augments: str = typer.Option(None, "--augments"),
    tag: list[str] = typer.Option(None, "--tag", help="Tag (repeatable)"),
    db: str = typer.Option(None, "--db", help=_DB_OPTION_HELP),
) -> None:
    """Add a theorycraft idea to the notebook. Only a title is required."""
    flags = _experiment_fields_from_flags(
        title=title, slug=slug, carry=carry, carry_id=carry_id, status=status, lifecycle=lifecycle,
        summary=summary, notes=notes, core=core, optional=optional, trait=trait, carry_item=carry_item,
        tank_item=tank_item, secondary_unit=secondary_unit, secondary_item=secondary_item,
        target_level=target_level, reroll_level=reroll_level, roll_timing=roll_timing,
        positioning=positioning, augments=augments,
    )
    if tag:
        flags["tags"] = tag
    data = _merge_entry(_load_json_file(from_json), flags)
    with Database(_resolve_db_target(db)) as database:
        try:
            created = create_experiment(database, data)
        except ExperimentError as exc:
            console.print(f"[red]Not saved:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print(f"Saved to the {_describe_target(database)}.")
    _print_experiment(created)


@app.command("experiment-update")
def experiment_update(
    key: str = typer.Argument(..., help="Experiment slug or id"),
    from_json: Path = typer.Option(None, "--from-json", help="JSON with fields to change; flags override it"),
    title: str = typer.Option(None, "--title"),
    slug: str = typer.Option(None, "--slug"),
    carry: str = typer.Option(None, "--carry"),
    carry_id: str = typer.Option(None, "--carry-id"),
    status: str = typer.Option(None, "--status", help="THEORYCRAFTED or VARIANT"),
    lifecycle: str = typer.Option(None, "--lifecycle"),
    summary: str = typer.Option(None, "--summary"),
    notes: str = typer.Option(None, "--notes"),
    core: list[str] = typer.Option(None, "--core", help="Replaces the core unit list (repeatable)"),
    optional: list[str] = typer.Option(None, "--optional", help="Replaces the optional unit list"),
    trait: list[str] = typer.Option(None, "--trait", help="Replaces the trait targets"),
    carry_item: list[str] = typer.Option(None, "--carry-item", help="Replaces the carry items"),
    tank_item: list[str] = typer.Option(None, "--tank-item", help="Replaces the tank items"),
    secondary_unit: str = typer.Option(None, "--secondary-unit"),
    secondary_item: list[str] = typer.Option(None, "--secondary-item"),
    target_level: int = typer.Option(None, "--target-level", min=1, max=11),
    reroll_level: int = typer.Option(None, "--reroll-level", min=1, max=11),
    roll_timing: str = typer.Option(None, "--roll-timing"),
    positioning: str = typer.Option(None, "--positioning"),
    augments: str = typer.Option(None, "--augments"),
    add_tag: list[str] = typer.Option(None, "--add-tag"),
    remove_tag: list[str] = typer.Option(None, "--remove-tag"),
    db: str = typer.Option(None, "--db", help=_DB_OPTION_HELP),
) -> None:
    """Change an existing idea. Only the fields you pass are touched."""
    flags = _experiment_fields_from_flags(
        title=title, slug=slug, carry=carry, carry_id=carry_id, status=status, lifecycle=lifecycle,
        summary=summary, notes=notes, core=core, optional=optional, trait=trait, carry_item=carry_item,
        tank_item=tank_item, secondary_unit=secondary_unit, secondary_item=secondary_item,
        target_level=target_level, reroll_level=reroll_level, roll_timing=roll_timing,
        positioning=positioning, augments=augments,
    )
    changes = _merge_entry(_load_json_file(from_json), flags)
    with Database(_resolve_db_target(db)) as database:
        try:
            updated = update_experiment(database, key, changes, add_tags=add_tag, remove_tags=remove_tag)
        except ExperimentNotFound:
            console.print(f"[red]No experiment {key!r}.[/red]")
            raise typer.Exit(code=1)
        except ExperimentError as exc:
            console.print(f"[red]Not saved:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print(f"Updated in the {_describe_target(database)}.")
    _print_experiment(updated)


@app.command("experiment-list")
def experiment_list(
    status: str = typer.Option(None, "--status", help="THEORYCRAFTED, VARIANT or OBSERVED"),
    lifecycle: str = typer.Option(None, "--lifecycle"),
    carry: str = typer.Option(None, "--carry", help="Carry name or character_id"),
    tag: str = typer.Option(None, "--tag"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table"),
    db: str = typer.Option(None, "--db", help=_DB_OPTION_HELP),
) -> None:
    """List notebook entries, most recently updated first."""
    with Database(_resolve_db_target(db)) as database:
        entries = list_experiments(database, evidence_status=status, lifecycle=lifecycle, carry=carry, tag=tag)
    if as_json:
        typer.echo(json.dumps([e.to_input() for e in entries], indent=2, ensure_ascii=False))
        return
    if not entries:
        console.print("[yellow]No experiments match.[/yellow] Add one with `tftlab experiment-add --title ...`.")
        return
    table = Table(title="Theorycraft notebook")
    for column in ("Slug", "Title", "Carry", "Evidence", "Lifecycle", "Updated"):
        table.add_column(column)
    for e in entries:
        table.add_row(e.slug, e.title, e.carry_name or "—", e.evidence_status, e.lifecycle, e.updated_at)
    console.print(table)


@app.command("experiment-show")
def experiment_show(
    key: str = typer.Argument(..., help="Experiment slug or id"),
    as_json: bool = typer.Option(
        False, "--json", help="Print editable JSON (feed it back with experiment-update --from-json)"
    ),
    db: str = typer.Option(None, "--db", help=_DB_OPTION_HELP),
) -> None:
    """Show one notebook entry."""
    with Database(_resolve_db_target(db)) as database:
        try:
            entry = get_experiment(database, key)
        except ExperimentNotFound:
            console.print(f"[red]No experiment {key!r}.[/red]")
            raise typer.Exit(code=1)
    if as_json:
        typer.echo(json.dumps(entry.to_input(), indent=2, ensure_ascii=False))
        return
    _print_experiment(entry)


@app.command("experiment-note")
def experiment_note(
    key: str = typer.Argument(..., help="Experiment slug or id"),
    kind: str = typer.Option(None, "--kind", help=", ".join(k for k in FIELD_NOTE_KINDS if k not in ("riot_evidence", "status_change"))),
    body: str = typer.Option(None, "--body", help="What you found, in a sentence or two"),
    source: str = typer.Option(None, "--source", help="e.g. \"TFT Academy\", MetaTFT, tactics.tools, \"Little Buddy Bot\", Reddit"),
    url: str = typer.Option(None, "--url", help="http(s) link to what you looked at"),
    status: str = typer.Option(None, "--status", help="Evidence stamp for this note: THEORYCRAFTED or VARIANT"),
    label: str = typer.Option(None, "--label", help="Research label: " + ", ".join(RESEARCH_LABELS)),
    noted_at: str = typer.Option(None, "--noted-at", help="Date checked, e.g. 2026-09-24 (default: now)"),
    from_json: Path = typer.Option(None, "--from-json", help="JSON with any of these fields plus optional 'data'"),
    db: str = typer.Option(None, "--db", help=_DB_OPTION_HELP),
) -> None:
    """Record research about an idea: a scout report, mechanic note, sighting or personal note.

    Only record a source you actually checked. The note is dated and shown in
    the experiment's Field Notes; it never changes the evidence status.
    """
    data = _load_json_file(from_json)
    allowed = {"kind", "body", "source", "url", "status", "label", "noted_at", "data"}
    unknown = set(data) - allowed
    if unknown:
        raise typer.BadParameter(f"unknown field(s) in --from-json: {', '.join(sorted(unknown))}")
    flags = {"kind": kind, "body": body, "source": source, "url": url, "status": status,
             "label": label, "noted_at": noted_at}
    data.update({k: v for k, v in flags.items() if v is not None})
    with Database(_resolve_db_target(db)) as database:
        try:
            note = add_field_note(
                database, key, kind=data.get("kind") or "", body=data.get("body"),
                source=data.get("source"), source_url=data.get("url"), evidence_status=data.get("status"),
                research_label=data.get("label"), noted_at=data.get("noted_at"), data=data.get("data"),
            )
        except ExperimentNotFound:
            console.print(f"[red]No experiment {key!r}.[/red]")
            raise typer.Exit(code=1)
        except ExperimentError as exc:
            console.print(f"[red]Not saved:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print(f"Field note added in the {_describe_target(database)}.")
    _print_note(note)


def _readable(label: str) -> str:
    """Item/trait ids as people write them: TFT_Item_BlueBuff+TFT_Item_Deathcap -> Blue Buff + Deathcap."""
    parts = [re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"^(?:TFT\d*_Item_|TFT\d*_|DA_\d*_?)", "", p)) for p in label.split("+")]
    return " + ".join(parts)


@app.command("experiment-scout")
def experiment_scout(
    key: str = typer.Argument(..., help="Experiment slug or id"),
    balance_window: str = typer.Option(None, "--balance-window", help="Defaults to the latest window in the store"),
    save: bool = typer.Option(False, "--save", help="Append the Riot evidence as a dated field note"),
    as_json: bool = typer.Option(False, "--json", help="Print the full scout report as JSON"),
    db: str = typer.Option(None, "--db", help=_DB_OPTION_HELP),
) -> None:
    """Fingerprint an idea, check it against OUR Riot data, and list the research still to do.

    Makes no web requests: external sources are listed as still needed until a
    field note from them is recorded with `tftlab experiment-note`.
    """
    with Database(_resolve_db_target(db)) as database:
        try:
            entry = get_experiment(database, key)
        except ExperimentNotFound:
            console.print(f"[red]No experiment {key!r}.[/red]")
            raise typer.Exit(code=1)
        report = scout(database, entry, balance_window=balance_window)
        saved = record_riot_evidence(database, entry, report["riot_evidence"]) if save else None
        target = _describe_target(database)

    if as_json:
        typer.echo(json.dumps({**report, "saved_note": saved}, indent=2, ensure_ascii=False, default=str))
        return

    fp, ev = report["fingerprint"], report["riot_evidence"]
    console.print(f"[bold]SCOUT: {entry.title}[/bold]\n")
    console.print("[bold]Fingerprint[/bold]")
    if not fp["specified"]:
        console.print("  (nothing structured yet: add a carry, core units or trait targets to scout this idea)")
    rows = [
        ("Carry", fp["primary_carry"]["name"] if fp["primary_carry"] else ""),
        ("Secondary carry", fp["secondary_carry"]["name"] if fp["secondary_carry"] else ""),
        ("Core", ", ".join(u["name"] for u in fp["core_units"])),
        ("Optional", ", ".join(u["name"] for u in fp["optional_units"])),
        ("Trait target", ", ".join(f"{t['breakpoint']} {t['name']}" if t["breakpoint"] else t["name"] for t in fp["target_traits"])),
        ("Carry items", ", ".join(fp["carry_item_names"])),
        ("Target level", fp["target_level"] or ""),
        ("Reroll level", fp["reroll_level"] or ""),
        ("Roll timing", fp["roll_timing"] or ""),
    ]
    for label_, value in rows:
        if value:
            console.print(f"  {label_}: {value}", markup=False)
    if fp["signature"]:
        console.print(f"  signature: {fp['signature']}", markup=False)

    console.print(f"\n[bold]Our data[/bold] (balance window {ev['balance_window'] or '—'})")
    if ev["status"] != "ok":
        console.print(f"  {ev['message']}")
    else:
        pct = lambda v: "—" if v is None else f"{v:.1%}"  # noqa: E731
        console.print(f"  Committed games: {ev['commitment_games']}")
        console.print(f"  Appearance rate: {pct(ev['appearance_rate'])}   Commitment rate: {pct(ev['commitment_rate'])}")
        console.print(f"  Avg placement: {ev['avg_placement']:.2f}   Top 4: {pct(ev['top4_rate'])}   "
                      f"Win: {pct(ev['win_rate'])}   3\u2605 hit: {pct(ev['hit_3star_rate'])}")
        console.print(f"  Opportunity Score (ours): {ev['opportunity_score']:.1f}")
        for title, rows_ in (("Partners", ev["best_partners"]), ("Item packages", ev["best_item_packages"]),
                             ("Trait breakpoints", ev["best_trait_breakpoints"])):
            if rows_:
                console.print(f"  Strongest {title.lower()}: " + "; ".join(f"{_readable(r['label'])} ({r['games']} g)" for r in rows_),
                              markup=False)
        for u in ev["core_units"]:
            console.print(f"  With {u['name']}: {u['games']} of {ev['commitment_games']} committed games", markup=False)
        if ev["core_together"]:
            console.print(f"  All core units together: {ev['core_together']['games']} games", markup=False)
        for t in ev["trait_targets"]:
            label_ = f"{t['breakpoint']} {t['name']}" if t["breakpoint"] else t["name"]
            note = "" if t["known_trait"] else " (not a trait in the current set roster)"
            console.print(f"  With {label_} active: {t['games']} of {ev['commitment_games']} committed games{note}", markup=False)
        if ev["low_sample"]:
            console.print(f"  [bold red]LOW SAMPLE[/bold red]: fewer than {LOW_SAMPLE_COMMITMENT_GAMES} committed games; don't trust these numbers yet.")

    console.print("\n[bold]External checks still needed[/bold] (not checked by this command)")
    if report["external_checks_needed"]:
        for label_ in report["external_checks_needed"]:
            console.print(f"  - {label_}")
    else:
        console.print("  none: every source on the checklist has a field note")
    if saved:
        console.print(f"\nSaved a riot_evidence field note in the {target}.")
    else:
        console.print("\nNothing saved. Re-run with --save to append this as a field note.")


@app.command("scout-sources")
def scout_sources() -> None:
    """Print the scout source vocabulary and research labels."""
    for s_ in SOURCES:
        console.print(f"[bold]{s_.label}[/bold] ({s_.key}){' · external' if s_.external else ''}: {s_.role}")
    console.print("")
    for key_, (text, meaning) in RESEARCH_LABELS.items():
        console.print(f"[bold]{text.upper()}[/bold] ({key_}): {meaning}")


@app.command("refresh-game-art")
def refresh_game_art_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch metadata only and report what would be cached."),
) -> None:
    """Cache the current set's champion, item and trait art from CommunityDragon.

    Writes deterministic PNGs under src/tftlab/web/static/game/ and the
    manifest at src/tftlab/data/game_art_manifest.json. The source is fixed
    (CommunityDragon's latest TFT bundle); no URL can be passed in. Exits 1
    if any download fails integrity checks or a kind ends up empty.
    """
    if not dry_run:
        try:
            import PIL  # noqa: F401
        except ImportError:
            console.print('[red]Pillow is required: pip install -e ".[art]"[/red]')
            raise typer.Exit(code=1)
    from .game_art_refresh import GameArtError, refresh_game_art

    try:
        with CommunityDragonClient() as cdragon:
            report = refresh_game_art(cdragon, dry_run=dry_run)
    except (GameArtError, httpx.HTTPError, ValueError) as exc:
        console.print(f"[red]Game art refresh failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"Set {report.set_number}{' (dry run)' if dry_run else ''}")
    for key_, value in report.shape.items():
        console.print(f"  {key_}: {value}")
    for kind in ("champions", "items", "traits"):
        console.print(f"{kind} selected: {', '.join(report.planned[kind])}", soft_wrap=True)
        console.print(
            f"{kind}: {report.cached[kind]} cached"
            f" ({report.fetched[kind]} downloaded, {report.unchanged[kind]} unchanged),"
            f" {len(report.missing[kind])} without an icon"
        )
        for asset_id in report.missing[kind]:
            console.print(f"  missing icon: {kind}/{asset_id}")
    for line in report.pruned:
        console.print(f"removed stale file: {line}")
    if report.bytes_written:
        console.print(f"bytes written: {report.bytes_written}")
    for failure in report.failures:
        console.print(f"[red]failure:[/red] {failure}")
    if not report.ok:
        console.print("[red]Integrity check failed; previous assets and manifest left untouched.[/red]")
        raise typer.Exit(code=1)


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, min=1, max=65535),
    reload: bool = typer.Option(False, help="Reload server on code changes"),
) -> None:
    """Launch the TFT Theory Lab website."""
    import uvicorn

    uvicorn.run("tftlab.webapp:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
