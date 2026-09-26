"""Item-only carry eligibility (tftlab.carry) and its analytics integration.

Uses the committed Set 18 item-stat snapshot (src/tftlab/data/item_stats.json)
-- the same metadata production analytics reads -- plus hand-built metadata
for the unknown/hashed cases. No production data, no network.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tftlab import carry
from tftlab.analytics import carry_commitment_stats, discover_candidates, item_package_stats
from tftlab.carry import classify_item, defensive_item_ids, is_carry_observation, load_item_stats
from tftlab.storage import Database
from tftlab.webapp import carry_item_sets

from _helpers import make_match, make_unit

IE, LW = "TFT_Item_InfinityEdge", "TFT_Item_LastWhisper"
RABADON, GUINSOO = "TFT_Item_RabadonsDeathcap", "TFT_Item_GuinsoosRageblade"
WARMOG, GARGOYLE = "TFT_Item_WarmogsArmor", "TFT_Item_GargoyleStoneplate"
CLAW, VISAGE = "TFT_Item_DragonsClaw", "TFT_Item_Redemption"  # Redemption's display name is Spirit Visage
TITAN, STERAK = "TFT_Item_TitansResolve", "TFT_Item_SteraksGage"
RAVAGER_EMBLEM = "DA_18_EmblemSlayer"  # "Ravager Emblem": no readable stats in CommunityDragon
ELISE = "DA_18_Elise"

# The namespace Set 18 Match-V1 boards actually store (production 18.3 data).
D_GARGOYLE, D_CLAW, D_WARMOG, D_VISAGE = "DA_GargoyleStoneplate", "DA_DragonsClaw", "DA_WarmogsArmor", "DA_SpiritVisage"
D_GUINSOO, D_TITAN, D_STERAK, D_LW = "DA_GuinsoosRageblade", "DA_TitansResolve", "DA_SteraksGage", "DA_LastWhisper"


# ---------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    "items, expected",
    [
        ([IE, LW], True),  # 1 damage itemized
        ([RABADON, GUINSOO], True),  # 2 AP itemized
        ([WARMOG, GARGOYLE], False),  # 3 defensive package
        ([WARMOG, GARGOYLE, CLAW], False),  # 4 defensive package
        ([GARGOYLE, VISAGE], False),  # defensive package
        ([WARMOG, WARMOG, GARGOYLE], False),  # duplicates are still all-defensive
        ([GARGOYLE, GUINSOO], True),  # 6 mixed package
        ([TITAN, STERAK], True),  # 7 bruiser: offensive signal alongside defensive stats
        ([RAVAGER_EMBLEM, GUINSOO], True),
        ([RAVAGER_EMBLEM, GUINSOO, TITAN], True),
        ([GARGOYLE, GUINSOO, TITAN], True),
        ([RAVAGER_EMBLEM, WARMOG], True),  # emblem has no readable stats => unknown => include
        ([WARMOG, "TFT_Item_ChainVest", "TFT_Item_NegatronCloak"], False),  # components never count
        ([WARMOG], False),  # below the >=2 completed-item threshold
    ],
)
def test_real_set18_metadata(items, expected) -> None:
    assert is_carry_observation(items) is expected


def test_classification_of_named_items_from_the_real_snapshot() -> None:
    stats = load_item_stats()
    for item in (IE, LW, RABADON, GUINSOO, TITAN, STERAK):
        assert classify_item(stats[item]) == "offensive", item
    for item in (WARMOG, GARGOYLE, CLAW, VISAGE):
        assert classify_item(stats[item]) == "defensive", item
    assert classify_item(stats[RAVAGER_EMBLEM]) == "unknown"  # present, but no readable stats
    # Offensive signal from a variant stat name, not a display name.
    assert classify_item(stats["TFT_Item_Spite"]) == "offensive"  # ADIncrease / APIncrease + Health
    # Ally-buff stats are not the holder's offense.
    assert classify_item(stats["TFT_Item_Zephyr"]) == "defensive"  # AllyBonusAS + Health


def test_unknown_item_never_causes_exclusion() -> None:
    assert is_carry_observation([WARMOG, "TFT_Item_SomethingNewThisPatch"]) is True


def test_hashed_or_ambiguous_metadata_does_not_cause_exclusion() -> None:
    stats = {
        WARMOG: {"stat_effects": ["Health"], "tags": ["Health"]},
        "Hashed_Only": {"stat_effects": [], "tags": []},  # only {hash} names upstream
        "Passive_Only": {"stat_effects": ["ICD", "Duration"], "tags": []},  # no stat evidence
        "Hashed_Plus_Armor": {"stat_effects": ["Armor"], "tags": []},  # hashes dropped, Armor remains
    }
    assert is_carry_observation([WARMOG, "Hashed_Only"], item_stats=stats) is True
    assert is_carry_observation([WARMOG, "Passive_Only"], item_stats=stats) is True
    # Enough readable evidence remains to prove the whole package defensive.
    assert is_carry_observation([WARMOG, "Hashed_Plus_Armor"], item_stats=stats) is False


def test_no_champion_allowlist_role_table_or_tank_list() -> None:
    source = "\n".join(
        line for line in Path(carry.__file__).read_text().splitlines() if not line.lstrip().startswith("#")
    )
    code = re.sub(r'"""(?:.|\n)*?"""', "", source)  # docstrings may mention examples
    assert not re.search(r"DA_\d+_|TFT\d+_[A-Z]|character_id|\brole\b|tank", code, re.IGNORECASE)
    assert re.search(r"def is_carry_observation\(\s*item_ids", source)  # items only, no champion argument


def test_metadata_is_a_committed_file_not_a_network_call() -> None:
    source = Path(carry.__file__).read_text()
    assert "httpx" not in source and "CommunityDragonClient" not in source
    assert carry.ITEM_STATS_PATH.name == "item_stats.json" and carry.ITEM_STATS_PATH.exists()
    assert len(defensive_item_ids()) > 0


# ---------------------------------------------------------------- analytics


def _board(match_id: str, character_id: str, items: list[str], *, placement: int = 3, tier: int = 2) -> dict:
    return make_match(
        match_id, placement=placement,
        units=[make_unit(character_id, rarity=1, tier=tier, items=items), make_unit("TFT99_Filler", rarity=0)],
    )


def _stats_by_id(db: Database) -> dict:
    return {s.character_id: s for s in carry_commitment_stats(db, min_samples=1, max_cost=5)}


def _elise_db(path: Path) -> Database:
    """Elise with real Match-V1 (DA_*) item ids: 3 defensive boards, 3
    offensive boards (two with the Ravager emblem), 1 unitemized board."""
    db = Database(path)
    for i, items in enumerate(([D_WARMOG, D_GARGOYLE], [D_WARMOG, D_GARGOYLE, D_CLAW], [D_GARGOYLE, D_VISAGE])):
        db.ingest_match(_board(f"DEF{i}", ELISE, items, placement=6, tier=3 if i == 0 else 2))
    for i, items in enumerate(([RAVAGER_EMBLEM, D_GUINSOO], [RAVAGER_EMBLEM, D_GUINSOO, D_TITAN], [D_GARGOYLE, D_GUINSOO, D_TITAN])):
        db.ingest_match(_board(f"OFF{i}", ELISE, items, placement=2))
    db.ingest_match(_board("BARE", ELISE, [], placement=5))
    return db


def test_elise_defensive_boards_excluded_offensive_boards_included(tmp_path: Path) -> None:
    with _elise_db(tmp_path / "elise.sqlite3") as db:
        elise = _stats_by_id(db)[ELISE]
    assert elise.appearances == 7  # every board she was on
    assert elise.commitment_games == 3  # only the offensive builds
    assert elise.avg_placement == pytest.approx(2.0)  # defensive 6th places no longer count as carry games
    assert elise.hit_3star_rate == 0.0  # the 3-star defensive board is not a carry hit
    assert elise.carry_conversion_rate == pytest.approx(3 / 7)


def test_appearance_unchanged_and_commitment_drops_only_for_defensive_boards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _elise_db(tmp_path / "before_after.sqlite3") as db:
        after = _stats_by_id(db)[ELISE]
        monkeypatch.setattr(carry, "load_item_stats", lambda path=None: {})  # the old >=2-item rule
        before = _stats_by_id(db)[ELISE]
    assert (before.appearances, before.appearance_rate) == (after.appearances, after.appearance_rate)
    assert before.commitment_games - after.commitment_games == 3  # exactly the three defensive boards
    assert (before.commitment_games, after.commitment_games) == (6, 3)


def test_three_star_defensive_board_is_still_not_a_carry(tmp_path: Path) -> None:
    with Database(tmp_path / "3star.sqlite3") as db:
        for i in range(3):
            db.ingest_match(_board(f"T{i}", "TFT99_Wall", [WARMOG, GARGOYLE, CLAW], tier=3, placement=1))
        assert "TFT99_Wall" not in _stats_by_id(db)


def test_two_star_offensive_misses_still_count(tmp_path: Path) -> None:
    with Database(tmp_path / "miss.sqlite3") as db:
        db.ingest_match(_board("M1", "TFT99_Reroll", [IE, LW], tier=2, placement=5))
        db.ingest_match(_board("M2", "TFT99_Reroll", [IE, LW], tier=3, placement=1))
        stat = _stats_by_id(db)["TFT99_Reroll"]
    assert (stat.commitment_games, stat.miss_games, stat.hit_games) == (2, 1, 1)


def test_downstream_evidence_uses_only_qualifying_boards(tmp_path: Path) -> None:
    with _elise_db(tmp_path / "downstream.sqlite3") as db:
        window = db.query_one("SELECT balance_window FROM matches LIMIT 1")[0]
        package_sets = {row[0] for row in carry_item_sets(db, ELISE, window)}
        packages = item_package_stats(db, ELISE, window, min_pair_games=1, min_package_games=1)
    assert all(D_WARMOG not in s for s in package_sets) and len(package_sets) == 3
    individual = {a.key for a in packages["items"]}
    assert D_WARMOG not in individual and D_GUINSOO in individual


def test_current_sample_regression_tank_spam_removed_offmeta_carry_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A champion with many defensive >=2-item boards no longer shows up as a
    carry; a normally defensive champion built offensively stays discoverable."""
    with Database(tmp_path / "regression.sqlite3") as db:
        for i in range(12):  # the frontliner everyone slams tank items on
            db.ingest_match(_board(f"WALL{i}", "TFT99_Wall", [D_WARMOG, D_GARGOYLE] + ([D_CLAW] if i % 2 else []), placement=4))
        for i in range(6):  # the same kind of unit, built as a carry
            db.ingest_match(_board(f"FLIP{i}", "TFT99_OffMeta", [D_GARGOYLE, D_GUINSOO, D_TITAN], placement=2))
        for i in range(4):
            db.ingest_match(_board(f"FLIPDEF{i}", "TFT99_OffMeta", [D_WARMOG, D_GARGOYLE], placement=6))
        after = {c.character_id for c in discover_candidates(db, max_cost=5, min_samples=3)}
        monkeypatch.setattr(carry, "load_item_stats", lambda path=None: {})
        before = {c.character_id for c in discover_candidates(db, max_cost=5, min_samples=3)}
    assert before == {"TFT99_Wall", "TFT99_OffMeta"}
    assert after == {"TFT99_OffMeta"}


def test_discovery_still_evaluates_every_champion_with_qualifying_boards(tmp_path: Path) -> None:
    carries = {f"TFT99_C{i}": [IE, LW] if i % 2 else [RABADON, GUINSOO] for i in range(6)}
    with Database(tmp_path / "every.sqlite3") as db:
        n = 0
        for cid, items in carries.items():
            for placement in (1, 4, 7):
                n += 1
                db.ingest_match(_board(f"E{n}", cid, items, placement=placement))
        candidates = {c.character_id for c in discover_candidates(db, max_cost=5, min_samples=1, top_n=50)}
    assert candidates == set(carries)


# ---------------------------------------------------------------- SQL == Python, on SQLite and Postgres

PACKAGES = [
    [IE, LW], [WARMOG, GARGOYLE], [WARMOG, WARMOG, GARGOYLE], [WARMOG, GARGOYLE, CLAW], [GARGOYLE, GUINSOO],
    [RAVAGER_EMBLEM, WARMOG], [WARMOG, "TFT_Item_ChainVest"], [WARMOG, "TFT_Item_New"], [TITAN, STERAK], [],
    # Match-V1 DA_ namespace
    [D_GARGOYLE, D_CLAW], [D_WARMOG, D_WARMOG, D_GARGOYLE], [D_GUINSOO, D_TITAN], [D_GARGOYLE, D_GUINSOO],
    [D_WARMOG, "DA_Component_ChainVest", "DA_Component_NegatronCloak"], [D_WARMOG, "DA_NotInTheSnapshot"],
]


def _sql_matches_python(db: Database) -> None:
    for i, items in enumerate(PACKAGES):
        db.ingest_match(_board(f"SQL{i}", f"TFT99_P{i}", items))
    sql, params = carry.carry_commitment_sql("u")
    eligible = {r[0] for r in db.query_all(f"SELECT u.character_id FROM units u WHERE {sql}", params)}
    expected = {f"TFT99_P{i}" for i, items in enumerate(PACKAGES) if is_carry_observation(items)}
    assert eligible == expected
    assert expected == {
        "TFT99_P0", "TFT99_P4", "TFT99_P5", "TFT99_P7", "TFT99_P8",
        "TFT99_P12", "TFT99_P13", "TFT99_P14", "TFT99_P15",
    }


def test_sql_rule_matches_python_rule_on_sqlite(tmp_path: Path) -> None:
    with Database(tmp_path / "sql.sqlite3") as db:
        _sql_matches_python(db)


POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_sql_rule_matches_python_rule_and_elise_on_postgres() -> None:
    db = Database(POSTGRES_TEST_URL)
    try:
        for table in ("match_discoveries", "seed_samples", "ingest_runs", "traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        _sql_matches_python(db)
        for table in ("traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
    finally:
        db.close()
    elise_db = _elise_db_pg()
    try:
        elise = _stats_by_id(elise_db)[ELISE]
    finally:
        elise_db.close()
    assert (elise.appearances, elise.commitment_games) == (7, 3)


def _elise_db_pg() -> Database:
    db = Database(POSTGRES_TEST_URL)
    for i, items in enumerate(([D_WARMOG, D_GARGOYLE], [D_WARMOG, D_GARGOYLE, D_CLAW], [D_GARGOYLE, D_VISAGE])):
        db.ingest_match(_board(f"PGDEF{i}", ELISE, items, placement=6))
    for i, items in enumerate(([RAVAGER_EMBLEM, D_GUINSOO], [RAVAGER_EMBLEM, D_GUINSOO, D_TITAN], [D_GARGOYLE, D_GUINSOO, D_TITAN])):
        db.ingest_match(_board(f"PGOFF{i}", ELISE, items, placement=2))
    db.ingest_match(_board("PGBARE", ELISE, [], placement=5))
    return db


# ---------------------------------------------------------------- the Match-V1 DA_ namespace


@pytest.mark.parametrize(
    "items, expected",
    [
        ([D_GARGOYLE, D_CLAW], False),  # real defensive pair
        ([D_WARMOG, D_GARGOYLE, D_CLAW], False),
        ([D_WARMOG, D_WARMOG, D_GARGOYLE], False),
        ([D_GUINSOO, D_TITAN], True),  # real offensive pair
        ([D_GARGOYLE, D_GUINSOO], True),  # mixed
        ([D_TITAN, D_STERAK], True),  # bruiser
        ([D_GARGOYLE, D_LW], True),  # Last Whisper: offensive via its verified alias
        ([D_WARMOG, "DA_NotInTheSnapshot"], True),  # unknown DA_ id: never excluded
        ([D_WARMOG, "DA_BlueBuff"], True),  # ambiguous alias => unknown => include
        ([RAVAGER_EMBLEM, D_WARMOG], True),  # emblem unknown => include
        ([RAVAGER_EMBLEM, D_GARGOYLE, D_CLAW], True),
        ([D_WARMOG, "DA_Component_ChainVest"], True),  # a component never counts as defensive evidence
    ],
)
def test_real_match_v1_da_namespace(items, expected) -> None:
    assert is_carry_observation(items) is expected


def test_da_ids_classify_like_their_tft_item_counterparts() -> None:
    stats = load_item_stats()
    pairs = {
        D_GARGOYLE: "TFT_Item_GargoyleStoneplate", D_CLAW: "TFT_Item_DragonsClaw", D_VISAGE: "TFT_Item_Redemption",
        D_GUINSOO: "TFT_Item_GuinsoosRageblade", D_TITAN: "TFT_Item_TitansResolve", D_STERAK: "TFT_Item_SteraksGage",
        D_LW: "TFT_Item_LastWhisper",
        "DA_SunfireCape": "TFT_Item_RedBuff",  # legacy api name: TFT_Item_RedBuff *is* Sunfire Cape
        "DA_RedBuff": "TFT_Item_RapidFireCannon",  # ... and "Red Buff" is TFT_Item_RapidFireCannon
    }
    for da_id, tft_id in pairs.items():
        assert classify_item(stats[da_id]) == classify_item(stats[tft_id]), da_id
        assert tft_id in stats[da_id]["alias_of"], da_id
    # Every verified alias agrees with its counterparts' classification.
    for item_id, meta in stats.items():
        for alias in meta.get("alias_of") or []:
            if classify_item({"stat_effects": meta["own_stat_effects"], "tags": meta["own_tags"]}) == "unknown":
                assert classify_item(meta) == classify_item(stats[alias]), (item_id, alias)


def test_emblems_stay_unknown_unless_the_feed_gives_offensive_stats() -> None:
    stats = load_item_stats()
    emblems = {k: v for k, v in stats.items() if k.startswith("DA_18_Emblem")}
    assert RAVAGER_EMBLEM in emblems and stats[RAVAGER_EMBLEM]["name"] == "Ravager Emblem"
    assert all(classify_item(v) == "unknown" for v in emblems.values())
    assert not any(k in defensive_item_ids() for k in emblems)


def test_snapshot_covers_the_match_v1_da_namespace() -> None:
    """Regression guard: the committed Set 18 snapshot must never again hold
    zero Match-V1 (DA_) items."""
    stats = load_item_stats()
    da = [k for k in stats if k.startswith("DA_")]
    assert len(da) >= 60
    for item_id in (D_GARGOYLE, D_CLAW, D_WARMOG, D_GUINSOO, D_TITAN, D_STERAK, D_LW, "DA_Morellonomicon",
                    "DA_VoidStaff", "DA_Deathblade", "DA_SpearOfShojin", "DA_EdgeOfNight", RAVAGER_EMBLEM):
        assert item_id in stats, item_id
    defensive = set(defensive_item_ids())
    assert {D_GARGOYLE, D_CLAW, D_WARMOG} <= defensive
    assert not any(k.startswith(("DA_Component_", "TFT_Item_ChainVest")) for k in defensive)
    # Only equipment: components, emblems, DA_Item_*, and craftables (no set-suffixed augment ids).
    for k in da:
        assert k.startswith(("DA_Component_", "DA_18_Emblem", "DA_Item_")) or not k.startswith("DA_18_"), k
        assert k.startswith("DA_18_Emblem") or not k.endswith("18"), k
    for augment in ("DA_Hugify18", "DA_18_YordleSpirit", "DA_18_FOURcing", "DA_Barrier18"):
        assert augment not in stats


def test_da_namespace_analytics_appearance_unchanged_commitment_only_offensive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(tmp_path / "da.sqlite3") as db:
        for i in range(4):
            db.ingest_match(_board(f"DAD{i}", "TFT99_Front", [D_GARGOYLE, D_CLAW], placement=5))
        for i in range(3):
            db.ingest_match(_board(f"DAO{i}", "TFT99_Front", [D_GUINSOO, D_TITAN], placement=2))
        after = _stats_by_id(db)["TFT99_Front"]
        monkeypatch.setattr(carry, "load_item_stats", lambda path=None: {})
        before = _stats_by_id(db)["TFT99_Front"]
    assert after.appearances == before.appearances == 7
    assert after.appearance_rate == before.appearance_rate
    assert (before.commitment_games, after.commitment_games) == (7, 3)  # the 4 defensive DA_ boards dropped
    assert after.avg_placement == pytest.approx(2.0)
