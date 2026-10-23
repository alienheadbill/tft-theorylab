"""How a champion's carry boards split (`distributions` on Champion
Investigation): final player level, the carry's star level and its completed
items -- board counts in value order, never ranked by results, with an honest
state when a data source holds no player levels."""

from __future__ import annotations

import re
from pathlib import Path

from test_observed_boards import KOG, WINDOW, _make_old_style, _snapshot, _top_carry, db  # noqa: F401 (fixture)
from tftlab import champion_investigation as ci
from tftlab.analytics.boards import ObservedBoard, carry_board_distributions
from tftlab.champion_investigation import champion_investigation
from tftlab.storage import Database

WEB = Path(__file__).parent.parent / "src" / "tftlab" / "web"


def _dist(db: Database) -> dict:
    return champion_investigation(db, KOG, WINDOW)["distributions"]


def _rows(block: dict) -> list[tuple[int, int]]:
    return [(r["value"], r["boards"]) for r in block["rows"]]


def test_every_carry_board_counts_once_in_each_distribution(db: Database) -> None:
    dist = _dist(db)
    assert dist["evidence_type"] == "observed" and dist["carry_boards"] == 86
    level = dist["final_level"]
    assert level["available"] is True and _rows(level) == [(7, 10), (8, 44), (9, 22)]
    assert level["boards"] == 76 and level["missing"] == 10  # level 0 = not reported: counted as missing, never a level
    # Two committed Kog'Maw copies on one board (one 3-star): one board, at the highest star level.
    assert _rows(dist["carry_stars"]) == [(2, 72), (3, 14)] and dist["carry_stars"]["missing"] == 0
    assert _rows(dist["carry_items"]) == [(2, 86)]
    for block in (dist["final_level"], dist["carry_stars"], dist["carry_items"]):
        assert abs(sum(r["share"] for r in block["rows"]) - 1.0) < 1e-9
        assert sum(r["boards"] for r in block["rows"]) == block["boards"]


def test_rows_follow_the_value_scale_never_results(db: Database) -> None:
    """Level 8 boards are the most common here and placed worst; level 9's
    placed best. Neither moves: rows are in level order."""
    rows = _dist(db)["final_level"]["rows"]
    assert [r["value"] for r in rows] == sorted(r["value"] for r in rows)
    assert all(set(r) == {"value", "boards", "share", "limited_sample"} for r in rows)  # no placement numbers


def test_limited_sample_marks_rows_under_the_floor() -> None:
    boards = [ObservedBoard(f"m{i}", 0, 1, 8 if i < 12 else 9, 2, (), carry_items=3) for i in range(15)]
    dist = ci.distribution_section(boards, levels_ok=True)
    assert [(r["value"], r["limited_sample"]) for r in dist["final_level"]["rows"]] == [(8, False), (9, True)]
    assert dist["min_boards"] == ci.DISTRIBUTION_MIN_BOARDS == 10


def test_unusable_values_are_missing_never_guessed() -> None:
    boards = [ObservedBoard("a", 0, 1, None, 0, (), carry_items=None),
              ObservedBoard("b", 0, 1, 11, 5, (), carry_items=-1),
              ObservedBoard("c", 0, 1, 8, 3, (), carry_items=3)]
    dist = carry_board_distributions(boards)
    for key in ("final_level", "carry_stars", "carry_items"):
        assert dist[key].counts == ((8 if key == "final_level" else 3, 1),) and dist[key].missing == 2


def test_a_snapshot_without_player_levels_says_so_and_keeps_the_rest(tmp_path: Path) -> None:
    path = _snapshot(tmp_path)
    _make_old_style(path)
    with Database.open_existing(path) as db:
        dist = champion_investigation(db, _top_carry(db), "18.3")["distributions"]
    assert dist["final_level"] == {"available": False, "reason": ci.LEVEL_NOT_PRESERVED}
    assert dist["carry_stars"]["available"] is True and dist["carry_stars"]["boards"] == dist["carry_boards"] > 0


def test_no_carry_boards_means_no_distributions(db: Database) -> None:
    other = dict(KOG, character_id="DA_18_Ahri", name="Ahri", slug="ahri")
    assert champion_investigation(db, other, WINDOW)["distributions"] is None


def test_the_page_never_implies_levelling_timing_or_advice() -> None:
    js = (WEB / "static" / "champion.js").read_text(encoding="utf-8")
    section = js[js.index("const DIST_LABELS"):js.index("function howToPlaySection")]
    lowered = section.lower()
    for banned in ("fast 8", "fast 9", "reroll", "slow roll", "level to", "should level", "best level", "optimal",
                   "recommended", "you should"):
        assert banned not in lowered, banned
    assert "match data has no timeline" in section and "not ranked by results" in section
    assert re.search(r"distributionCard\(inv\)", js) and "Final player level on these boards" in js
