"""Riot item-intent carry eligibility (tftlab.carry) and its analytics integration.

Uses the committed Set 18 item-intent snapshot (src/tftlab/data/item_intent.json,
built from Riot's TFTCharacterRoleData recommended-item lists) -- the same
metadata production analytics reads -- plus hand-built intents for edge
cases. No production data, no network.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tftlab import carry
from tftlab.analytics import carry_commitment_stats, discover_candidates, item_package_stats
from tftlab.carry import (
    CARRY_EVIDENCE,
    DAMAGE,
    KNOWN_UNLISTED,
    MIXED,
    TANK,
    UNKNOWN,
    intent_from_recommendations,
    is_carry_observation,
    item_intent,
    load_item_intent,
    no_carry_evidence_item_ids,
)
from tftlab.items import ITEM_INTENT_PATH, ITEM_STATS_PATH, is_component
from tftlab.storage import Database
from tftlab.webapp import carry_item_sets

from _helpers import make_match, make_unit

# The namespace Set 18 Match-V1 boards actually store.
VISAGE, STEADFAST, WARMOG, GARGOYLE = "DA_SpiritVisage", "DA_SteadfastHeart", "DA_WarmogsArmor", "DA_GargoyleStoneplate"
TITAN, STERAK, GUINSOO, CROWNGUARD = "DA_TitansResolve", "DA_SteraksGage", "DA_GuinsoosRageblade", "DA_Crownguard"
ADAPTIVE, IONIC, CLAW, LW = "DA_AdaptiveHelm", "DA_IonicSpark", "DA_DragonsClaw", "DA_LastWhisper"
RAVAGER_EMBLEM = "DA_18_EmblemSlayer"  # "Ravager Emblem": no Riot counterpart in the recommendation namespace
ELISE = "DA_18_Elise"

# The same items under their TFT_Item_* ids (the ids Riot's role lists name).
T_WARMOG, T_GARGOYLE, T_VISAGE = "TFT_Item_WarmogsArmor", "TFT_Item_GargoyleStoneplate", "TFT_Item_Redemption"
T_GUINSOO, T_TITAN, T_STERAK = "TFT_Item_GuinsoosRageblade", "TFT_Item_TitansResolve", "TFT_Item_SteraksGage"
T_STEADFAST, T_CROWNGUARD = "TFT_Item_NightHarvester", "TFT_Item_Crownguard"


def _snapshot() -> dict:
    return json.loads(ITEM_INTENT_PATH.read_text())


# ---------------------------------------------------------------- item intent


@pytest.mark.parametrize(
    "resolved, tank, non_tank, expected",
    [
        (True, [], ["ADCarry"], DAMAGE),
        (True, ["APTank"], [], TANK),
        (True, ["HTank"], ["ADFighter"], MIXED),
        (True, [], [], KNOWN_UNLISTED),
        (False, [], [], UNKNOWN),
    ],
)
def test_intent_states(resolved, tank, non_tank, expected) -> None:
    assert intent_from_recommendations(resolved=resolved, tank_roles=tank, non_tank_roles=non_tank) == expected


def test_named_set18_items_classify_from_riot_recommendations() -> None:
    expected = {
        VISAGE: TANK, WARMOG: TANK, GARGOYLE: TANK, CLAW: TANK,
        STEADFAST: KNOWN_UNLISTED, CROWNGUARD: KNOWN_UNLISTED,  # no role recommends them: not "tank"
        TITAN: MIXED, IONIC: MIXED,
        STERAK: DAMAGE, GUINSOO: DAMAGE, ADAPTIVE: DAMAGE, LW: DAMAGE,
        RAVAGER_EMBLEM: UNKNOWN,
    }
    assert {i: item_intent(i) for i in expected} == expected


def test_every_intent_state_occurs_in_the_real_snapshot() -> None:
    assert {m["intent"] for m in load_item_intent().values()} == {DAMAGE, TANK, MIXED, KNOWN_UNLISTED, UNKNOWN}


def test_classification_keeps_its_riot_evidence() -> None:
    """Why is Warmog's TANK? Riot recommends it for exactly these Tank roles
    and no non-Tank role. Why is Titan's MIXED? A Tank role and non-Tank
    roles both list it."""
    items = load_item_intent()
    warmog = items[WARMOG]
    assert warmog["riot_items"] == ["TFT_Item_CorruptedWarmogsArmor", T_WARMOG]
    assert warmog["recommended_by_tank_roles"] == ["ADTank", "APTank", "HTank"] and warmog["recommended_by_non_tank_roles"] == []
    titan = items[TITAN]
    assert titan["recommended_by_tank_roles"] == ["HTank"]
    assert {"ADFighter", "APFighter", "HFighter"} <= set(titan["recommended_by_non_tank_roles"])
    assert items[STEADFAST]["riot_items"] == [T_STEADFAST]  # known to Riot, recommended by nobody
    assert items[STEADFAST]["recommended_by_tank_roles"] == items[STEADFAST]["recommended_by_non_tank_roles"] == []
    assert items[RAVAGER_EMBLEM]["riot_items"] == []  # unresolved: absence of evidence is not evidence


def test_snapshot_evidence_is_consistent_with_riot_roles() -> None:
    """Every stored intent follows from its stored evidence, and every cited
    role really is that family and really recommends one of the item's
    Riot items -- so the file explains itself after Riot changes a list."""
    snap = _snapshot()
    roles = snap["roles"]
    for item_id, meta in snap["items"].items():
        assert meta["intent"] == intent_from_recommendations(
            resolved=bool(meta["riot_items"]), tank_roles=meta["recommended_by_tank_roles"],
            non_tank_roles=meta["recommended_by_non_tank_roles"],
        ), item_id
        for family, key in (("tank", "recommended_by_tank_roles"), ("non_tank", "recommended_by_non_tank_roles")):
            for role in meta[key]:
                assert roles[role]["family"] == family, (item_id, role)
                assert set(roles[role]["recommended_items"]) & set(meta["riot_items"]), (item_id, role)
    # Only roles in Riot's current vocabulary give evidence; each is classified.
    current = {n: r for n, r in roles.items() if r["family"]}
    assert {r["ui_name"].split()[-1] for r in current.values()} == {"Tank", "Assassin", "Caster", "Fighter", "Marksman", "Specialist"}
    assert {n for n, r in current.items() if r["family"] == "tank"} == {"ADTank", "APTank", "HTank"}
    assert all(r["ui_name_key"] is None for r in roles.values() if not r["family"])  # legacy objects: no evidence


def test_every_recommended_item_resolves_to_a_snapshot_item() -> None:
    snap = _snapshot()
    stats = json.loads(ITEM_STATS_PATH.read_text())["items"]
    recommended = {i for r in snap["roles"].values() if r["family"] for i in r["recommended_items"]}
    assert recommended and recommended <= set(stats)
    # ... and each is reachable from the Match-V1 DA_ namespace.
    da_resolved = {r for i, m in snap["items"].items() if i.startswith("DA_") for r in m["riot_items"]}
    assert recommended <= da_resolved


def test_da_ids_classify_like_their_tft_item_counterparts() -> None:
    pairs = {
        WARMOG: T_WARMOG, GARGOYLE: T_GARGOYLE, VISAGE: T_VISAGE, GUINSOO: T_GUINSOO, TITAN: T_TITAN,
        STERAK: T_STERAK, STEADFAST: T_STEADFAST, CROWNGUARD: T_CROWNGUARD,
        "DA_SunfireCape": "TFT_Item_RedBuff",  # legacy api name: TFT_Item_RedBuff *is* Sunfire Cape
        "DA_RedBuff": "TFT_Item_RapidFireCannon",  # ... and "Red Buff" is TFT_Item_RapidFireCannon
    }
    for da_id, tft_id in pairs.items():
        assert item_intent(da_id) == item_intent(tft_id), da_id
    items = load_item_intent()
    for item_id, meta in items.items():  # a DA_ item's evidence is exactly its Riot items' evidence
        if item_id.startswith("DA_") and meta["riot_items"]:
            for key in ("recommended_by_tank_roles", "recommended_by_non_tank_roles"):
                assert meta[key] == sorted({r for t in meta["riot_items"] for r in items[t][key]}), item_id


def test_components_and_unknown_ids() -> None:
    items = load_item_intent()
    assert not any(is_component(i) for i in items)
    assert not any(is_component(i) for i in no_carry_evidence_item_ids())
    assert item_intent("DA_SomethingNewNextPatch") == UNKNOWN
    assert all(i.startswith("DA_18_Emblem") for i, m in items.items() if i.startswith("DA_") and m["intent"] == UNKNOWN)


# ---------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    "items, expected",
    [
        ([VISAGE, STEADFAST], False),  # TANK + KNOWN_UNLISTED: the Leona regression
        ([WARMOG, GARGOYLE], False),  # TANK + TANK
        ([WARMOG, GARGOYLE, CLAW], False),
        ([WARMOG, WARMOG, GARGOYLE], False),  # duplicates do not change it
        ([STEADFAST, CROWNGUARD], False),  # KNOWN_UNLISTED + KNOWN_UNLISTED
        ([CROWNGUARD, WARMOG], False),  # not enough Riot evidence of carry intent
        ([TITAN, STERAK], True),  # MIXED + DAMAGE
        ([TITAN, WARMOG], True),  # MIXED alone is carry evidence
        ([RAVAGER_EMBLEM, GUINSOO], True),  # UNKNOWN + DAMAGE
        ([RAVAGER_EMBLEM, WARMOG], True),  # UNKNOWN + TANK: conservative
        ([GARGOYLE, GUINSOO], True),
        ([STEADFAST, GUINSOO], True),
        ([WARMOG, "DA_NotInTheSnapshot"], True),
        ([WARMOG, "DA_Component_ChainVest"], False),  # 1 completed item
        ([GUINSOO, "DA_Component_RecurveBow"], False),  # components never count
        ([WARMOG, GARGOYLE, "DA_Component_ChainVest"], False),
        ([GUINSOO], False),
        # TFT_Item_* ids: the same answers
        ([T_VISAGE, T_STEADFAST], False), ([T_WARMOG, T_GARGOYLE], False), ([T_CROWNGUARD, T_WARMOG], False),
        ([T_TITAN, T_STERAK], True), ([RAVAGER_EMBLEM, T_GUINSOO], True),
    ],
)
def test_rule(items, expected) -> None:
    assert is_carry_observation(items) is expected


def test_no_champion_role_table_or_raw_stat_rule() -> None:
    source = "\n".join(
        line for line in Path(carry.__file__).read_text().splitlines() if not line.lstrip().startswith("#")
    )
    code = re.sub(r'"""(?:.|\n)*?"""', "", source)  # docstrings may mention examples
    assert not re.search(r"DA_\d+_|TFT\d+_[A-Z]|character_id|CritChance|stat_effects|Leona|Steadfast", code)
    assert re.search(r"def is_carry_observation\(\s*item_ids", source)  # items only, no champion argument


def test_metadata_is_a_committed_file_not_a_network_call() -> None:
    source = Path(carry.__file__).read_text()
    assert "httpx" not in source and "CommunityDragonClient" not in source
    assert carry.ITEM_INTENT_PATH.name == "item_intent.json" and carry.ITEM_INTENT_PATH.exists()
    assert set(no_carry_evidence_item_ids()) >= {VISAGE, STEADFAST, WARMOG, GARGOYLE, CROWNGUARD}
    assert not set(no_carry_evidence_item_ids()) & {TITAN, STERAK, GUINSOO, RAVAGER_EMBLEM}


# ---------------------------------------------------------------- analytics


def _board(match_id: str, character_id: str, items: list[str], *, placement: int = 3, tier: int = 2) -> dict:
    return make_match(
        match_id, placement=placement,
        units=[make_unit(character_id, rarity=1, tier=tier, items=items), make_unit("TFT99_Filler", rarity=0)],
    )


def _stats_by_id(db: Database) -> dict:
    return {s.character_id: s for s in carry_commitment_stats(db, min_samples=1, max_cost=5)}


TANK_BOARDS = ([WARMOG, GARGOYLE], [WARMOG, GARGOYLE, CLAW], [VISAGE, STEADFAST])
CARRY_BOARDS = ([RAVAGER_EMBLEM, GUINSOO], [RAVAGER_EMBLEM, GUINSOO, TITAN], [GARGOYLE, GUINSOO, TITAN])


def _elise_boards(db: Database, prefix: str = "") -> Database:
    """Elise with real Match-V1 item ids: 3 boards without carry evidence,
    3 carry boards (two with the Ravager emblem), 1 unitemized board."""
    for i, items in enumerate(TANK_BOARDS):
        db.ingest_match(_board(f"{prefix}DEF{i}", ELISE, items, placement=6, tier=3 if i == 0 else 2))
    for i, items in enumerate(CARRY_BOARDS):
        db.ingest_match(_board(f"{prefix}OFF{i}", ELISE, items, placement=2))
    db.ingest_match(_board(f"{prefix}BARE", ELISE, [], placement=5))
    return db


def test_elise_tank_boards_excluded_carry_boards_included(tmp_path: Path) -> None:
    with _elise_boards(Database(tmp_path / "elise.sqlite3")) as db:
        elise = _stats_by_id(db)[ELISE]
    assert elise.appearances == 7  # every board she was on
    assert elise.commitment_games == 3  # only the boards with carry evidence
    assert elise.avg_placement == pytest.approx(2.0)
    assert elise.hit_3star_rate == 0.0  # the 3-star tank board is not a carry hit
    assert elise.carry_conversion_rate == pytest.approx(3 / 7)


def test_appearance_unchanged_and_commitment_drops_only_where_intent_changes_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _elise_boards(Database(tmp_path / "before_after.sqlite3")) as db:
        after = _stats_by_id(db)[ELISE]
        monkeypatch.setattr(carry, "load_item_intent", lambda path=None: {})  # every item unknown: the bare >=2 rule
        before = _stats_by_id(db)[ELISE]
    assert (before.appearances, before.appearance_rate) == (after.appearances, after.appearance_rate)
    assert (before.commitment_games, after.commitment_games) == (6, 3)  # exactly the three tank boards


def test_two_star_misses_still_count(tmp_path: Path) -> None:
    with Database(tmp_path / "miss.sqlite3") as db:
        db.ingest_match(_board("M1", "TFT99_Reroll", [GUINSOO, LW], tier=2, placement=5))
        db.ingest_match(_board("M2", "TFT99_Reroll", [GUINSOO, LW], tier=3, placement=1))
        db.ingest_match(_board("M3", "TFT99_Reroll", [WARMOG, GARGOYLE], tier=3, placement=1))  # 3-star tank: not a hit
        stat = _stats_by_id(db)["TFT99_Reroll"]
    assert (stat.appearances, stat.commitment_games, stat.miss_games, stat.hit_games) == (3, 2, 1, 1)


def test_duplicate_copies_count_once_and_commit_if_any_copy_qualifies(tmp_path: Path) -> None:
    def two_copies(match_id: str, first: list[str], second: list[str]) -> dict:
        return make_match(match_id, placement=3, units=[
            make_unit(ELISE, rarity=1, tier=2, items=first), make_unit(ELISE, rarity=1, tier=2, items=second),
        ])

    with Database(tmp_path / "dupes.sqlite3") as db:
        db.ingest_match(two_copies("D1", [VISAGE, STEADFAST], [RAVAGER_EMBLEM, GUINSOO]))  # one copy qualifies
        db.ingest_match(two_copies("D2", [VISAGE, STEADFAST], [WARMOG, GARGOYLE]))  # neither does
        elise = _stats_by_id(db)[ELISE]
    assert (elise.appearances, elise.commitment_games) == (2, 1)


def test_downstream_evidence_uses_only_qualifying_boards(tmp_path: Path) -> None:
    with _elise_boards(Database(tmp_path / "downstream.sqlite3")) as db:
        window = db.query_one("SELECT balance_window FROM matches LIMIT 1")[0]
        package_sets = {row[0] for row in carry_item_sets(db, ELISE, window)}
        packages = item_package_stats(db, ELISE, window, min_pair_games=1, min_package_games=1)
    assert len(package_sets) == 3 and all(WARMOG not in s and STEADFAST not in s for s in package_sets)
    individual = {a.key for a in packages["items"]}
    assert not individual & {WARMOG, VISAGE, STEADFAST, "DA_Component_ChainVest"} and GUINSOO in individual


def test_discovery_regression_leona_like_board_excluded_offmeta_conversion_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production Discovery run that motivated this: a frontliner on
    Spirit Visage + Steadfast Heart ranked #1. PR #21 read Steadfast Heart's
    raw CritChance stat as offensive, so the board looked like a carry. No
    Riot role recommends Steadfast Heart and only Tank roles recommend Spirit
    Visage, so the package has no carry evidence. A tank-shaped unit built
    as a carry (emblem + Guinsoo's) stays discoverable."""
    stats = json.loads(ITEM_STATS_PATH.read_text())["items"]
    assert "CritChance" in stats[STEADFAST]["stat_effects"]  # what PR #21 treated as offensive
    with Database(tmp_path / "regression.sqlite3") as db:
        for i in range(12):
            db.ingest_match(_board(f"LEONA{i}", "TFT99_Leona", [VISAGE, STEADFAST], placement=3))
        for i in range(6):
            db.ingest_match(_board(f"FLIP{i}", "TFT99_OffMeta", [RAVAGER_EMBLEM, GUINSOO], placement=2))
        for i in range(4):
            db.ingest_match(_board(f"FLIPDEF{i}", "TFT99_OffMeta", [WARMOG, GARGOYLE], placement=6))
        after = {c.character_id for c in discover_candidates(db, max_cost=5, min_samples=3)}
        leona = _stats_by_id(db)["TFT99_Leona"] if "TFT99_Leona" in _stats_by_id(db) else None
        # PR #21's outcome for these boards (Steadfast Heart counted as offensive) == every board eligible.
        monkeypatch.setattr(carry, "load_item_intent", lambda path=None: {})
        before = {c.character_id for c in discover_candidates(db, max_cost=5, min_samples=3)}
    assert before == {"TFT99_Leona", "TFT99_OffMeta"}
    assert after == {"TFT99_OffMeta"}
    assert leona is None  # no commitment games at all, so not a carry candidate


def test_discovery_still_evaluates_every_champion_with_qualifying_boards(tmp_path: Path) -> None:
    carries = {f"TFT99_C{i}": [GUINSOO, LW] if i % 2 else [ADAPTIVE, "DA_RabadonsDeathcap"] for i in range(6)}
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
    [VISAGE, STEADFAST], [WARMOG, GARGOYLE], [WARMOG, WARMOG, GARGOYLE], [CROWNGUARD, WARMOG], [TITAN, STERAK],
    [RAVAGER_EMBLEM, GUINSOO], [RAVAGER_EMBLEM, WARMOG], [WARMOG, "DA_NotInTheSnapshot"], [GARGOYLE, GUINSOO],
    [WARMOG, "DA_Component_ChainVest", "DA_Component_NegatronCloak"], [], [T_WARMOG, T_GARGOYLE], [T_TITAN, T_STERAK],
    [STEADFAST, CROWNGUARD, VISAGE], [IONIC, WARMOG],
]


def _sql_matches_python(db: Database) -> None:
    for i, items in enumerate(PACKAGES):
        db.ingest_match(_board(f"SQL{i}", f"TFT99_P{i}", items))
    sql, params = carry.carry_commitment_sql("u")
    eligible = {r[0] for r in db.query_all(f"SELECT u.character_id FROM units u WHERE {sql}", params)}
    expected = {f"TFT99_P{i}" for i, items in enumerate(PACKAGES) if is_carry_observation(items)}
    assert eligible == expected
    assert expected == {"TFT99_P4", "TFT99_P5", "TFT99_P6", "TFT99_P7", "TFT99_P8", "TFT99_P12", "TFT99_P14"}


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
        _elise_boards(db, prefix="PG")
        elise = _stats_by_id(db)[ELISE]
        assert (elise.appearances, elise.commitment_games) == (7, 3)
    finally:
        db.close()


def test_every_no_evidence_id_is_counted_in_sql() -> None:
    sql, params = carry.carry_commitment_sql("u")
    ids = no_carry_evidence_item_ids()
    assert params[0] == 2
    assert [p for p in params[1:] if p.startswith('"') and not p.endswith('"')] == ['"DA_', '"TFT_']  # one guard each
    counted = [p for p in params[1:] if p.startswith('"') and p.endswith('"')]
    assert sorted(set(counted)) == sorted(json.dumps(i) for i in ids) and len(counted) == 2 * len(ids)
    assert all(load_item_intent()[i]["intent"] not in CARRY_EVIDENCE for i in ids)
    assert max(len(m) for m in re.findall(r"(?:REPLACE\()+", sql)) <= len("REPLACE(") * 17  # bounded nesting


def test_no_evidence_count_is_exact_for_duplicates_and_prefix_lookalikes(tmp_path: Path) -> None:
    intents = {"DA_Wall": {"intent": TANK}, "DA_WallPlus": {"intent": DAMAGE}, "TFT_Item_Wall": {"intent": TANK}}
    cases = {
        "A": ["DA_Wall", "DA_Wall", "DA_Wall"],  # 3 no-evidence
        "B": ["DA_Wall", "DA_WallPlus"],  # quoted match: DA_Wall is not counted inside DA_WallPlus
        "C": ["TFT_Item_Wall", "DA_Wall"],
        "D": ["DA_Wall", "TFT_Item_Wall", "DA_New"],
    }
    with Database(tmp_path / "exact.sqlite3") as db:
        for cid, items in cases.items():
            db.ingest_match(_board(f"X{cid}", cid, items))
        sql, params = carry.carry_commitment_sql("u", item_intents=intents)
        eligible = {r[0] for r in db.query_all(f"SELECT u.character_id FROM units u WHERE {sql}", params)}
    assert eligible == {cid for cid, items in cases.items() if is_carry_observation(items, item_intents=intents)} == {"B", "D"}
