from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .analytics import carry_commitment_stats
from .config import Settings
from .demo import generate_demo_matches
from .ingest import ingest_ladder
from .riot import RiotClient
from .storage import Database

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
        stats = carry_commitment_stats(database.conn, min_samples=10)
    console.print(f"Inserted {inserted} demo matches / {inserted * 8} participants")
    _print_stats(stats)


@app.command("ingest-riot")
def ingest_riot(
    players: int = typer.Option(25, min=1, help="High-Elo seed players"),
    matches_per_player: int = typer.Option(10, min=1, max=100),
    include_master: bool = typer.Option(False, help="Include Master + GM seeds"),
) -> None:
    """Pull recent high-Elo matches from Riot into SQLite."""
    _load_dotenv()
    settings = Settings.from_env()
    if not settings.riot_api_key:
        raise typer.BadParameter("Set RIOT_API_KEY in .env or the environment")
    leagues = ("challenger", "grandmaster", "master") if include_master else ("challenger",)
    with Database(settings.db_path) as db, RiotClient(
        settings.riot_api_key, platform=settings.platform, region=settings.region
    ) as client:
        result = ingest_ladder(
            client,
            db,
            player_limit=players,
            matches_per_player=matches_per_player,
            leagues=leagues,
        )
    console.print(
        f"Seeds: {result.seed_players} | unique match IDs: {result.match_ids_seen} | "
        f"inserted: {result.matches_inserted}"
    )


@app.command()
def leaderboard(
    db: Path = typer.Option(Path("data/tftlab.sqlite3"), "--db"),
    min_samples: int = typer.Option(20, min=1),
    max_cost: int = typer.Option(3, min=1, max=5),
) -> None:
    """Calculate item-commitment carry statistics."""
    with Database(db) as database:
        stats = carry_commitment_stats(
            database.conn, min_samples=min_samples, max_cost=max_cost
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
