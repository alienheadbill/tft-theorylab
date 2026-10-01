"""Source semantics behind item and trait evidence:

- Thief's Gloves: Riot lists the gloves plus the two items the game rolled
  for the round. Only the gloves are equipped; the rolls are never chosen
  items, and a Thief's Gloves holder is never carry evidence.
- Intrinsic traits: a trait only the carry itself provides comes with
  picking that champion, so it is champion context, not trait-shell
  evidence. Derived from the roster's static trait membership.

Riot-shaped boards with real Set 18 ids; SQLite and (with
TFTLAB_TEST_DATABASE_URL) Postgres.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _helpers import make_match, make_unit
from tftlab import prepared_discovery
from tftlab.analytics import carry_commitment_stats, item_package_stats, trait_count_associations, trait_profile
from tftlab.analytics.discovery import compute_item_flexibility
from tftlab.analytics.item_packages import _completed_items
from tftlab.carry import carry_commitment_sql, is_carry_observation
from tftlab.champion_investigation import champion_investigation
from tftlab.itemization import THIEFS_GLOVES, thiefs_gloves_item_ids, unit_itemization
from tftlab.roster import Roster, load_roster
from tftlab.storage import Database

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
WINDOW = "14.6"

TG, TG_RADIANT = "DA_ThiefsGloves", "DA_ThiefsGlovesRadiant"
BT, CLAW, JG, KRAKEN = "DA_Bloodthirster", "DA_DragonsClaw", "DA_JeweledGauntlet", "DA_KrakensFury"
GUINSOO, IE, DCAP, WARMOG = "DA_GuinsoosRageblade", "DA_InfinityEdge", "DA_RabadonsDeathcap", "DA_WarmogsArmor"

ROSTER = load_roster()
KOGMAW, = ROSTER.champion_ids("Kog'Maw")
ALUNE, = ROSTER.champion_ids("Alune")
DRAVEN, = ROSTER.champion_ids("Draven")
CAUSTIC, = ROSTER.trait_ids("Caustic")
ATTUNED, = ROSTER.trait_ids("Attuned")
BOUNTY, = ROSTER.trait_ids("Bounty Seeker")
ADAPTOR, = ROSTER.trait_ids("Adaptor")
INVOKER, = ROSTER.trait_ids("Invoker")
SPELLWEAVER, = ROSTER.trait_ids("Spellweaver")


# ---------------------------------------------------------------- Thief's Gloves: the abstraction


def test_thiefs_gloves_rolls_are_generated_not_equipped() -> None:
    tg = unit_itemization([TG, BT, CLAW])
    assert tg.special == THIEFS_GLOVES
    assert tg.equipped == (TG,) and tg.completed_equipped == (TG,)
    assert tg.generated == (BT, CLAW)

    radiant = unit_itemization([TG_RADIANT, "DA_BloodthirsterRadiant", "DA_GuinsoosRagebladeRadiant"])
    assert radiant.special == THIEFS_GLOVES and radiant.equipped == (TG_RADIANT,)
    assert radiant.generated == ("DA_BloodthirsterRadiant", "DA_GuinsoosRagebladeRadiant")

    normal = unit_itemization([GUINSOO, IE, "DA_Component_BFSword"])
    assert normal.special is None and normal.generated == ()
    assert normal.equipped == (GUINSOO, IE, "DA_Component_BFSword") and normal.completed_equipped == (GUINSOO, IE)


def test_thiefs_gloves_ids_come_from_item_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = thiefs_gloves_item_ids()
    assert {TG, TG_RADIANT, "TFT_Item_ThiefsGloves", "TFT_Item_ThiefsGlovesRadiant",
            "TFT_Item_ThiefsGloves_AcademyCopy1", "TFT_Item_ThiefsGloves_AcademyCopy2"} <= ids
    assert "TFT_Item_SpellThiefsEdge" not in ids and BT not in ids

    # Not a hardcoded list: a snapshot that bridges another id to Riot's item is followed.
    import tftlab.itemization as itemization

    snapshot = tmp_path / "item_stats.json"
    snapshot.write_text(json.dumps({"items": {"DA_NextSetGloves": {"alias_of": ["TFT_Item_ThiefsGloves"]}}}))
    monkeypatch.setattr(itemization, "ITEM_STATS_PATH", snapshot)
    itemization.thiefs_gloves_item_ids.cache_clear()
    try:
        assert itemization.thiefs_gloves_item_ids() == {
            "DA_NextSetGloves", "TFT_Item_ThiefsGloves", "DA_NextSetGlovesRadiant", "TFT_Item_ThiefsGlovesRadiant"}
    finally:
        itemization.thiefs_gloves_item_ids.cache_clear()


def test_rolled_items_never_become_fixed_items_pairs_or_packages() -> None:
    """Even on a path that keeps a Thief's Gloves unit (e.g. a future Lucky
    Gloves exception), the rolls contribute no single item, pair or package."""
    from tftlab.analytics.item_packages import _package_stats

    rows = [(1, _completed_items(json.dumps([TG, BT, CLAW]))), (3, _completed_items(json.dumps([TG_RADIANT, JG, KRAKEN])))]
    assert rows == [(1, (TG,)), (3, (TG_RADIANT,))]
    stats = _package_stats(rows, min_pair_games=1, min_package_games=1, item_names=None)
    assert {a.key for a in stats["items"]} == {TG, TG_RADIANT}
    assert stats["pairs"] == [] and stats["packages"] == []


def test_ordinary_builds_are_unchanged() -> None:
    assert _completed_items(json.dumps([IE, GUINSOO])) == (GUINSOO, IE)
    assert _completed_items(json.dumps([IE, GUINSOO, "DA_Component_BFSword", DCAP])) == (GUINSOO, IE, DCAP)
    assert is_carry_observation([GUINSOO, IE]) and is_carry_observation([BT, CLAW])


@pytest.mark.parametrize("items", [
    [TG, BT, CLAW], [TG, JG, KRAKEN], [TG, GUINSOO, IE],
    [TG_RADIANT, "DA_BloodthirsterRadiant", "DA_GuinsoosRagebladeRadiant"],
    ["TFT_Item_ThiefsGloves_AcademyCopy1", "TFT_Item_InfinityEdge", "TFT_Item_GuinsoosRageblade"],
])
def test_a_thiefs_gloves_unit_is_never_a_carry_however_offensive_its_rolls(items) -> None:
    # The same rolls equipped normally would qualify; on Thief's Gloves they were not chosen.
    assert is_carry_observation(items[1:])
    assert not is_carry_observation(items)
    assert not is_carry_observation(items, commitment_items=1)


# ---------------------------------------------------------------- database: carry population, items, traits


def _board(match_id: str, carry: str, items: list[str], placement: int, traits: list[tuple[str, int]],
           partners: tuple[str, ...] = (), augments: list[str] | None = None) -> dict:
    units = [make_unit(carry, tier=2, items=items)] + [make_unit(p, tier=2) for p in partners]
    payload = make_match(match_id, units=units, placement=placement, traits=[
        {"name": t, "num_units": n, "style": 1, "tier_current": 1, "tier_total": 3} for t, n in traits])
    if augments:
        payload["info"]["participants"][0]["augments"] = augments
    return payload


KOG_TRAITS = [(CAUSTIC, 1), (ADAPTOR, 3), (INVOKER, 2)]


def _seed(db: Database) -> None:
    for i in range(6):  # Kog'Maw carried with chosen items
        db.ingest_match(_board(f"KOG_{i}", KOGMAW, [GUINSOO, IE, DCAP] if i % 2 else [GUINSOO, IE], 1 + i % 8,
                               KOG_TRAITS + ([(BOUNTY, 1)] if i < 3 else []), partners=(DRAVEN,) if i < 3 else ()))
    for i in range(5):  # Kog'Maw holding Thief's Gloves with offensive rolls, some with Lucky Gloves taken
        rolls = [[BT, CLAW], [JG, KRAKEN], [GUINSOO, IE]][i % 3]
        db.ingest_match(_board(f"KOG_TG_{i}", KOGMAW, [TG if i % 2 else TG_RADIANT, *rolls], 1, KOG_TRAITS,
                               augments=["DA_LuckyGloves"] if i == 0 else (["DA_LuckyGlovesPlus"] if i == 1 else None)))
    for i in range(4):  # Alune carried
        db.ingest_match(_board(f"ALU_{i}", ALUNE, [DCAP, JG], 2 + i, [(ATTUNED, 1), (SPELLWEAVER, 2)]))


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run against Postgres"))]


@pytest.fixture(params=BACKENDS)
def db(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        database = Database(tmp_path / "semantics.sqlite3")
    else:
        database = Database(POSTGRES_TEST_URL)
        for table in ("discovery_prepared_candidates", "discovery_prepared_runs", "traits", "units",
                      "participants", "matches"):
            database.execute(f"DELETE FROM {table}")
        database.commit()
    _seed(database)
    yield database
    database.close()


def test_thiefs_gloves_boards_leave_the_carry_population_in_sql_and_python(db: Database) -> None:
    kog = next(s for s in carry_commitment_stats(db, balance_window=WINDOW, min_cost=1, max_cost=5, min_samples=1)
               if s.character_id == KOGMAW)
    assert kog.appearances == 11 and kog.commitment_games == 6  # the 5 Thief's Gloves boards are not carry boards
    sql, params = carry_commitment_sql("u")
    rows = db.query_all(f"SELECT u.items_json FROM units u WHERE u.character_id = ? AND {sql}", (KOGMAW, *params))
    sql_items = sorted(json.loads(r[0]) for r in rows)
    python_items = sorted(json.loads(r[0]) for r in db.query_all(
        "SELECT items_json FROM units WHERE character_id = ?", (KOGMAW,)) if is_carry_observation(json.loads(r[0])))
    assert sql_items == python_items and all(TG not in i and TG_RADIANT not in i for i in sql_items)


def test_lucky_gloves_is_not_yet_an_exception(db: Database) -> None:
    """Lucky Gloves' Match-V1 augment ids are unverified, so taking it does
    not change anything: those Thief's Gloves boards are still not carry boards."""
    lucky = db.query_all("SELECT augments_json FROM participants WHERE match_id IN ('KOG_TG_0', 'KOG_TG_1')")
    assert sorted(json.loads(r[0]) for r in lucky) == [["DA_LuckyGloves"], ["DA_LuckyGlovesPlus"]]
    stats = item_package_stats(db, KOGMAW, WINDOW, min_pair_games=1, min_package_games=1)
    assert sum(a.games for a in stats["items"] if a.key == GUINSOO) == 6


def test_rolled_items_never_reach_item_evidence_or_flexibility(db: Database, tmp_path: Path) -> None:
    stats = item_package_stats(db, KOGMAW, WINDOW, min_pair_games=1, min_package_games=1)
    keys = {a.key for k in ("items", "pairs", "packages") for a in stats[k]}
    for rolled in (BT, CLAW, JG, KRAKEN, TG, TG_RADIANT, f"{BT}+{CLAW}", f"{JG}+{KRAKEN}"):
        assert not any(rolled == k or rolled in k.split("+") for k in keys), rolled
    assert {a.key for a in stats["items"]} == {GUINSOO, IE, DCAP}
    assert next(a for a in stats["pairs"] if a.key == f"{GUINSOO}+{IE}").games == 6
    assert [a.key for a in stats["packages"]] == ["+".join(sorted([GUINSOO, IE, DCAP]))]

    # Item flexibility is exactly what the same chosen-item boards give without any Thief's Gloves board.
    with Database(tmp_path / "no_tg.sqlite3") as clean:
        for i in range(6):
            clean.ingest_match(_board(f"KOG_{i}", KOGMAW, [GUINSOO, IE, DCAP] if i % 2 else [GUINSOO, IE],
                                      1 + i % 8, KOG_TRAITS))
        baseline = item_package_stats(clean, KOGMAW, WINDOW)["items"]
    assert compute_item_flexibility(item_package_stats(db, KOGMAW, WINDOW)["items"]) == compute_item_flexibility(baseline)


# ---------------------------------------------------------------- intrinsic traits


SYNTHETIC_CHAMPIONS = {
    "C_A": {"name": "A", "cost": 1, "traits": ["T_Solo", "T_Pair"]},
    "C_B": {"name": "B", "cost": 4, "traits": ["T_Pair", "T_Rare"]},
    "C_C": {"name": "C", "cost": 5, "traits": ["T_Rare"]},
    "C_D": {"name": "D", "cost": 2, "traits": ["T_Emblemable", "T_Pair"]},
    "X_Summon": {"name": "Summon", "cost": 0, "traits": ["T_Solo"]},  # not a shop champion
}
SYNTHETIC_TRAITS = {"T_Solo": "Solo", "T_Pair": "Pair", "T_Rare": "Rare", "T_Emblemable": "Emblemable"}


def test_intrinsic_traits_come_from_static_membership_not_names() -> None:
    roster = Roster(set_number=99, traits=SYNTHETIC_TRAITS, champions=SYNTHETIC_CHAMPIONS,
                    trait_items={"T_Emblemable": ["DA_99_EmblemEmblemable"], "T_Pair": ["DA_99_EmblemPair"]},
                    unresolved_emblems=[])
    assert roster.intrinsic_traits("C_A") == ("T_Solo",)
    assert roster.intrinsic_traits("C_B") == () and roster.intrinsic_traits("C_C") == ()
    assert roster.trait_champions("T_Rare") == ("C_B", "C_C")


def test_a_one_champion_trait_an_item_can_add_is_not_intrinsic() -> None:
    """Emblem / trait-item guard: T_Emblemable has a single shop champion (C_D)
    but an item can add it to other units, so it stays buildable."""
    guarded = Roster(set_number=99, traits=SYNTHETIC_TRAITS, champions=SYNTHETIC_CHAMPIONS,
                     trait_items={"T_Emblemable": ["DA_99_EmblemEmblemable"]}, unresolved_emblems=[])
    assert guarded.trait_champions("T_Emblemable") == ("C_D",)
    assert guarded.intrinsic_traits("C_D") == ()
    assert guarded.intrinsic_traits("C_A") == ("T_Solo",)
    # Without verified trait-item data nothing is intrinsic (conservative).
    unverified = Roster(set_number=99, traits=SYNTHETIC_TRAITS, champions=SYNTHETIC_CHAMPIONS)
    assert all(unverified.intrinsic_traits(c) == () for c in SYNTHETIC_CHAMPIONS)
    # Nor when the snapshot never checked for unresolved emblems.
    unchecked = Roster(set_number=99, traits=SYNTHETIC_TRAITS, champions=SYNTHETIC_CHAMPIONS,
                       trait_items={"T_Emblemable": ["DA_99_EmblemEmblemable"]})
    assert not unchecked.trait_item_guard_verified
    assert all(unchecked.intrinsic_traits(c) == () for c in SYNTHETIC_CHAMPIONS)


def _synthetic_meta(**items):
    from tftlab.cdragon import ChampionMeta, ItemMeta, SetMetadata, TraitMeta

    return SetMetadata(
        patch="latest", set_number=99,
        champions={cid: ChampionMeta(character_id=cid, name=c["name"], cost=c["cost"], icon_url=None,
                                     traits=tuple(SYNTHETIC_TRAITS[t] for t in c["traits"]), role=None)
                   for cid, c in SYNTHETIC_CHAMPIONS.items()},
        traits={tid: TraitMeta(trait_id=tid, name=name, icon_url=None) for tid, name in SYNTHETIC_TRAITS.items()},
        items={item_id: ItemMeta(item_id=item_id, name=name, icon_url=None, composition=(),
                                 associated_traits=tuple(associated))
               for item_id, (name, *associated) in items.items()},
    )


def _snapshot_roster(meta) -> Roster:
    from tftlab.cdragon import roster_snapshot

    snapshot = roster_snapshot(meta)
    return Roster(set_number=99, champions=snapshot["champions"], traits=snapshot["traits"],
                  trait_items=snapshot["trait_items"], unresolved_emblems=snapshot["unresolved_emblems"])


GUARD_ITEMS = {
    "DA_99_EmblemEmblemable": ("Emblemable Emblem",),  # emblem: no associatedTraits, linked by name
    "DA_99_PairAugment": ("Pair Power", "T_Pair"),  # by associatedTraits apiName
    "DA_99_RareAugment": ("Rare Power", "Rare"),  # by associatedTraits display name
    "TFT5_Item_BygoneEmblemItem": ("Bygone Emblem", "Set5_Bygone"),  # another set's emblem: ignored
    "DA_99_Sword": ("Sword",),
}


def test_trait_item_guard_reads_associated_traits_and_emblem_names() -> None:
    from tftlab.cdragon import trait_item_ids, unresolved_emblem_ids

    meta = _synthetic_meta(**GUARD_ITEMS)
    assert trait_item_ids(meta) == {
        "T_Emblemable": ("DA_99_EmblemEmblemable",), "T_Pair": ("DA_99_PairAugment",),
        "T_Rare": ("DA_99_RareAugment",)}
    assert unresolved_emblem_ids(meta) == ()
    roster = _snapshot_roster(meta)
    assert roster.trait_item_guard_verified
    assert roster.intrinsic_traits("C_A") == ("T_Solo",) and roster.intrinsic_traits("C_D") == ()


@pytest.mark.parametrize("emblem", [
    # a current-set emblem whose name matches no trait display name
    {"DA_99_EmblemSoloist": ("Soloist Emblem",)},
    # "emblem" only in the apiName, a name that is not "<trait> Emblem"
    {"DA_99_EmblemMystery": ("Mystery Crest",)},
    # "emblem" only in the display name, any case, in the shared namespace
    {"TFT_Item_SoloSigil": ("Solo EMBLEM of Fate",)},
    # current set's own TFT<n>_ namespace
    {"TFT99_Item_UnknownEmblemItem": ("Unknown Emblem",)},
])
def test_an_unresolved_emblem_makes_intrinsic_classification_fail_closed(emblem) -> None:
    """An emblem the guard cannot link to a trait might add a one-champion
    trait (here T_Solo), so nothing may be classified intrinsic until it is
    resolved. The unresolved id is recorded in the snapshot."""
    from tftlab.cdragon import roster_snapshot, unresolved_emblem_ids

    meta = _synthetic_meta(**GUARD_ITEMS, **emblem)
    assert unresolved_emblem_ids(meta) == tuple(emblem)
    assert roster_snapshot(meta)["unresolved_emblems"] == list(emblem)
    roster = _snapshot_roster(meta)
    assert not roster.trait_item_guard_verified
    assert all(roster.intrinsic_traits(c) == () for c in SYNTHETIC_CHAMPIONS)
    # Linking the same emblem to its trait restores classification.
    (item_id, (name, *_)), = emblem.items()
    resolved = _synthetic_meta(**GUARD_ITEMS, **{item_id: (name, "T_Solo")})
    assert unresolved_emblem_ids(resolved) == ()
    assert _snapshot_roster(resolved).intrinsic_traits("C_A") == ()  # T_Solo is now emblem-able


def test_every_committed_set_emblem_is_linked_to_a_trait() -> None:
    """Offline cross-check of the committed snapshots: the roster recorded no
    unresolved emblem, and every emblem id Match-V1 boards can store
    (`item_stats.json`, the board-id namespace) is listed in `trait_items`."""
    from tftlab.cdragon import ItemMeta, SetMetadata, TraitMeta, is_set_emblem
    from tftlab.items import ITEM_STATS_PATH

    assert ROSTER.unresolved_emblems == [] and ROSTER.trait_item_guard_verified
    stats = json.loads(ITEM_STATS_PATH.read_text())["items"]
    meta = SetMetadata(patch="committed", set_number=ROSTER.set_number, champions={},
                       items={k: ItemMeta(item_id=k, name=v["name"], icon_url=None) for k, v in stats.items()},
                       traits={t: TraitMeta(trait_id=t, name=n, icon_url=None) for t, n in ROSTER.traits.items()})
    emblems = {k for k, v in meta.items.items() if is_set_emblem(meta, k, v)}
    linked = {i for items in ROSTER.trait_items.values() for i in items}
    assert len(emblems) >= 20
    assert emblems <= linked, sorted(emblems - linked)


def test_current_set_intrinsic_traits_have_no_trait_item_path() -> None:
    """Static guard on the committed snapshot: Set 18's intrinsic traits have
    no emblem or trait item, while emblem-able traits (e.g. Ravager) are
    recorded and therefore never intrinsic."""
    assert ROSTER.trait_items is not None
    intrinsic = {t for c in ROSTER.champions for t in ROSTER.intrinsic_traits(c)}
    assert len(intrinsic) == 9
    assert not intrinsic & set(ROSTER.trait_items)
    ravager, = ROSTER.trait_ids("Ravager")
    assert "DA_18_EmblemSlayer" in ROSTER.trait_items[ravager]
    for trait, items in ROSTER.trait_items.items():
        assert trait in ROSTER.traits and items


def test_current_set_examples_kogmaw_caustic_and_alune_attuned() -> None:
    assert ROSTER.intrinsic_traits(KOGMAW) == (CAUSTIC,)
    assert ROSTER.intrinsic_traits(ALUNE) == (ATTUNED,)
    assert {ADAPTOR, INVOKER} <= set(ROSTER.champion_traits(KOGMAW))  # multi-unit traits stay buildable
    for champion in ROSTER.champions:
        for trait in ROSTER.intrinsic_traits(champion):
            assert ROSTER.trait_champions(trait) == (champion,)  # only one-champion traits, never "uncommon" ones
    multi = {t for t in ROSTER.traits if len(ROSTER.trait_champions(t)) >= 2}
    assert multi and not multi & {t for c in ROSTER.champions for t in ROSTER.intrinsic_traits(c)}


def test_carry_intrinsic_trait_is_context_not_ranked_shell_evidence(db: Database) -> None:
    champion = {"character_id": KOGMAW, "name": "Kog'Maw", "slug": "kogmaw", "cost": 4, "art_url": None}
    body = champion_investigation(db, champion, WINDOW)
    ranked = [t["trait_id"] for t in body["traits"]]
    assert CAUSTIC not in ranked
    assert {ADAPTOR, INVOKER} <= set(ranked)  # Kog'Maw's own multi-unit traits are kept
    assert BOUNTY in ranked  # another champion's one-champion trait means Draven was added: shell evidence
    (caustic,) = body["intrinsic_traits"]
    assert caustic["trait_id"] == CAUSTIC and caustic["name"] == "Caustic"
    assert caustic["art_url"].startswith("/static/game/traits/") and "Kog'Maw" in caustic["reason"]
    assert "Caustic" not in json.dumps(body["summary"])  # never summarised as a trait to build around
    reason = caustic["reason"].lower()
    for claim in ("top 4", "win", "better", "stronger", "cause", "because of", "placement"):
        assert claim not in reason  # identity, not a performance claim

    profile = trait_profile(db, KOGMAW, WINDOW)
    assert profile.intrinsic == (CAUSTIC,) and CAUSTIC not in profile.counts
    assert all(a.key != CAUSTIC for a in profile.active)


def test_discovery_trait_evidence_uses_the_same_rule(db: Database) -> None:
    alune = {a.key.split(":")[0] for a in trait_count_associations(db, ALUNE, WINDOW, min_games=1)}
    assert ATTUNED not in alune and SPELLWEAVER in alune
    kog = {a.key.split(":")[0] for a in trait_count_associations(db, KOGMAW, WINDOW, min_games=1)}
    assert CAUSTIC not in kog and {ADAPTOR, INVOKER, BOUNTY} <= kog
    # Stored trait rows are untouched.
    stored = db.query_one("SELECT COUNT(*) FROM traits WHERE trait_name = ?", (CAUSTIC,))[0]
    assert stored == 11


def test_intrinsic_traits_stay_out_of_the_champion_page() -> None:
    """Classification stays in the API; the player-facing page does not show
    an intrinsic-trait block (product decision on PR #51)."""
    web = Path(__file__).parents[1] / "src" / "tftlab" / "web"
    for name in ("static/champion.js", "static/app.css", "champion.html"):
        assert "intrinsic" not in (web / name).read_text().lower(), name


# ---------------------------------------------------------------- prepared Discovery


def test_semantic_sources_are_in_the_prepared_analytics_digest(tmp_path: Path) -> None:
    names = {p.name for p in prepared_discovery.ANALYTICS_SOURCES}
    assert {"itemization.py", "roster.py", "set_roster.json", "item_stats.json", "carry.py", "traits.py",
            "item_packages.py"} <= names
    copies = []
    for source in prepared_discovery.ANALYTICS_SOURCES:
        copy = tmp_path / source.name
        copy.write_bytes(source.read_bytes())
        copies.append(copy)
    before = prepared_discovery.analytics_version(copies)
    roster_copy = tmp_path / "set_roster.json"
    data = json.loads(roster_copy.read_text())
    data["champions"][KOGMAW]["traits"].remove(CAUSTIC)
    roster_copy.write_text(json.dumps(data))
    assert prepared_discovery.analytics_version(copies) != before  # trait membership change -> new version


def test_old_prepared_runs_go_stale_and_re_preparation_uses_the_corrected_semantics(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tftlab.prepared_discovery import lookup_prepared, prepare_window, read_prepared_candidate

    monkeypatch.setattr(prepared_discovery, "ANALYTICS_VERSION", "v1-before-this-change")
    prepare_window(db, WINDOW)
    assert lookup_prepared(db, WINDOW).status == "current"
    monkeypatch.undo()  # deploy the corrected code: the old run no longer matches
    assert lookup_prepared(db, WINDOW).status == "stale"

    result = prepare_window(db, WINDOW)
    lookup = lookup_prepared(db, WINDOW)
    assert result.status == "published" and lookup.status == "current"
    kog = read_prepared_candidate(db, lookup.run, KOGMAW, top_n=20)
    assert kog["commitment_games"] == 6
    assert all(a["key"].split(":")[0] != CAUSTIC for a in kog["best_trait_breakpoints"])
    for a in kog["best_item_packages"]:
        assert not set(a["key"].split("+")) & {BT, CLAW, JG, KRAKEN, TG, TG_RADIANT}
