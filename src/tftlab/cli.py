from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console
from rich.table import Table

from .analytics import available_balance_windows, carry_commitment_stats, default_balance_window, discover_candidates
from .cdragon import CommunityDragonClient, SetMetadata
from .config import Settings
from .demo import generate_demo_matches
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
    console.print(f"Malformed placements: {report.malformed_placements}")
    console.print(f"Duplicate match IDs: {report.duplicate_match_ids}")
    console.print(f"Participants without units: {report.participants_without_units}")

    if report.is_severe:
        console.print("\n[bold red]SEVERE integrity issues detected.[/bold red]")
        raise typer.Exit(code=1)
    console.print("\n[green]No severe integrity issues detected.[/green]")


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
