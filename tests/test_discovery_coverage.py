"""Discovery searches the whole sampled game, not a hand-picked list.

Audit tests: every eligible carry in the balance window is a candidate;
champions nobody saved or named still surface; saved experiments never
influence selection; the API reaches 5-costs; and no champion allowlist
exists in the analytics or sampling code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tftlab import analytics
from tftlab.analytics import carry_commitment_stats, default_balance_window, discover_candidates
from tftlab.experiments import DEMO_EXPERIMENTS, create_experiment, seed_demo_experiments
from tftlab.storage import Database

from _helpers import make_match, make_unit

COMMITTED = ["TFT_Item_InfinityEdge", "TFT_Item_LastWhisper"]  # two completed items
# Carries nobody wrote down anywhere: not demo experiments, not the roster.
UNNAMED = {"TFT99_Nobody": 0, "TFT99_Quiet": 1, "TFT99_Middle": 2, "TFT99_Expensive": 3, "TFT99_Legend": 4}


def _seed(db: Database) -> None:
    n = 0
    for character_id, rarity in UNNAMED.items():
        for placement in (1, 3, 5):
            n += 1
            db.ingest_match(
                make_match(
                    f"COV_{n}",
                    placement=placement,
                    units=[
                        make_unit(character_id, rarity=rarity, items=COMMITTED),
                        make_unit("TFT99_Filler", rarity=0, items=[]),
                    ],
                )
            )


def test_every_eligible_carry_in_the_window_is_a_candidate(tmp_path: Path) -> None:
    with Database(tmp_path / "cov.sqlite3") as db:
        _seed(db)
        window = default_balance_window(db)
        eligible = {s.character_id for s in carry_commitment_stats(db, balance_window=window, min_cost=1, max_cost=5, min_samples=1)}
        candidates = {c.character_id for c in discover_candidates(db, balance_window=window, max_cost=5, min_samples=1)}
    assert eligible == set(UNNAMED)  # the filler is never committed, so never a carry
    assert candidates == eligible


def test_unnamed_champions_surface_and_named_ones_are_not_required(tmp_path: Path) -> None:
    named = {e.get("carry_character_id") for e in DEMO_EXPERIMENTS}
    with Database(tmp_path / "unnamed.sqlite3") as db:
        _seed(db)
        candidates = {c.character_id for c in discover_candidates(db, max_cost=5, min_samples=1)}
    assert candidates and not (candidates & named)


def test_saved_experiments_do_not_change_candidates(tmp_path: Path) -> None:
    with Database(tmp_path / "exp.sqlite3") as db:
        _seed(db)
        before = [(c.character_id, c.opportunity_score) for c in discover_candidates(db, max_cost=5, min_samples=1)]
        seed_demo_experiments(db)
        create_experiment(db, {"title": "Nobody reroll", "carry_character_id": "TFT99_Nobody", "carry_name": "Nobody"})
        after = [(c.character_id, c.opportunity_score) for c in discover_candidates(db, max_cost=5, min_samples=1)]
    assert after == before


@pytest.fixture
def live_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    path = tmp_path / "live.sqlite3"
    with Database(path) as db:
        _seed(db)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(path))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


def test_api_reaches_five_cost_carries(live_client: TestClient) -> None:
    full = live_client.get("/api/discovery", params={"max_cost": 5, "min_samples": 1})
    assert full.status_code == 200 and full.json()["demo"] is False
    by_id = {c["character_id"]: c["cost"] for c in full.json()["candidates"]}
    assert by_id == {cid: rarity + 1 for cid, rarity in UNNAMED.items()}

    default = live_client.get("/api/discovery", params={"min_samples": 1}).json()
    assert {c["cost"] for c in default["candidates"]} <= {1, 2, 3}  # reroll-first default, not a limit
    assert live_client.get("/api/discovery", params={"max_cost": 6}).status_code == 422


def _code_lines(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_no_experiment_or_watchlist_table_feeds_discovery() -> None:
    table_ref = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+(experiment\w*|watchlist\w*)", re.IGNORECASE)
    analytics_dir = Path(analytics.__file__).parent
    for path in sorted(analytics_dir.glob("*.py")):
        code = _code_lines(path)
        assert not table_ref.search(code), path.name
        assert "experiments import" not in code and "watchlist" not in code.lower(), path.name


def test_no_hardcoded_champion_allowlist() -> None:
    """No champion ids or names appear in the analytics, sampling or ingest
    code: selection is purely statistical over what was observed."""
    literal = re.compile(r"DA_\d|DA_[A-Z][A-Za-z]+\d{2}|TFT\d+_[A-Z]|KhaZix|Kha'Zix|Cassiopeia|Fiddlesticks|Caitlyn|Warwick")
    root = Path(analytics.__file__).parent.parent
    files = [*sorted((root / "analytics").glob("*.py")), root / "sampling.py", root / "ingest.py"]
    for path in files:
        assert not literal.search(_code_lines(path)), path.name
