from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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
    create_experiment,
    get_experiment,
    list_experiments,
    update_experiment,
)
from .ingest import ingest_ladder
from .normalize import CostLookup
from .riot import RiotApiError, RiotClient, classify_riot_error
from .storage import Database
from .validate import validate_live_data

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
    players: int = typer.Option(25, min=1, help="High-Elo seed players"),
    matches_per_player: int = typer.Option(10, min=1, max=100),
    include_master: bool = typer.Option(False, help="Include Master + GM seeds"),
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
    """Pull recent high-Elo matches from Riot into the configured database.

    Challenger-only by default; pass --include-master to widen the seed
    pool. A safe first real ingest: `tftlab ingest-riot --players 10
    --matches-per-player 5`.
    """
    _load_dotenv()
    settings = Settings.from_env()
    if not settings.riot_api_key:
        raise typer.BadParameter("Set RIOT_API_KEY in .env or the environment")
    leagues = ("challenger", "grandmaster", "master") if include_master else ("challenger",)

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
    with Database(target) as db, RiotClient(
        settings.riot_api_key, platform=settings.platform, region=settings.region
    ) as client:
        result = ingest_ladder(
            client,
            db,
            player_limit=players,
            matches_per_player=matches_per_player,
            leagues=leagues,
            cost_lookup=cost_lookup,
        )
        windows = available_balance_windows(db)
        total_participants = db.query_one("SELECT COUNT(*) FROM participants")[0]

    console.print("\n[bold]Ingest report[/bold]")
    console.print(f"  Seed players: {result.seed_players}")
    console.print(f"  Unique match IDs discovered: {result.match_ids_seen}")
    console.print(f"  Matches fetched: {result.matches_fetched}")
    console.print(f"  Matches inserted: {result.matches_inserted}")
    console.print(f"  Matches skipped as duplicates: {result.duplicates_skipped}")
    console.print(f"  Failed requests: {result.failed_requests}")
    console.print(f"  Non-ranked-queue matches skipped: {result.non_target_matches_skipped}")
    console.print(f"  Balance windows found: {', '.join(w for w, _, _ in windows) or 'none'}")
    console.print(f"  Total participants now stored: {total_participants}")
    if degraded:
        console.print("[yellow]  Note: this ingest used degraded (rarity+1) costs.[/yellow]")


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
            "unauthorized": "401 Unauthorized -- RIOT_API_KEY is invalid or expired.",
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


_LOW_SAMPLE_COMMITMENT_GAMES = 30


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
