"""Champion Investigation's "How to play" layer and the item recipes behind
its component direction.

- Recipes: the committed item snapshot keeps each item's own
  CommunityDragon `composition`; only an exact two-component recipe counts.
- Component direction is derived from the recipes of supported completed
  items, never presented as observed components.
- The concise picks reuse the existing evidence rows (no new statistic):
  supported items/pairs/builds in the existing ranking, teammates by
  frequency, trait directions from the corrected trait evidence (Riot
  `num_units`), and the existing 3-star split.

Riot-shaped boards with real Set 18 ids; SQLite and (with
TFTLAB_TEST_DATABASE_URL) Postgres.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from _helpers import make_match, make_unit
from tftlab import how_to_play as htp
from tftlab.cdragon import ItemMeta, SetMetadata, item_stats_snapshot
from tftlab.champion_investigation import champion_investigation
from tftlab.game_art import item_ref
from tftlab.items import ITEM_STATS_PATH, is_component, item_recipe
from tftlab.roster import load_roster
from tftlab.storage import Database

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")
WINDOW = "14.6"

GUINSOO, IE, DCAP, JG = "DA_GuinsoosRageblade", "DA_InfinityEdge", "DA_RabadonsDeathcap", "DA_JeweledGauntlet"
BT, CLAW, TG = "DA_Bloodthirster", "DA_DragonsClaw", "DA_ThiefsGloves"
BOW, ROD, SWORD, GLOVE = (
    "DA_Component_RecurveBow", "DA_Component_NeedlesslyLargeRod", "DA_Component_BFSword", "DA_Component_SparringGloves")

ROSTER = load_roster()
KOGMAW, = ROSTER.champion_ids("Kog'Maw")
DRAVEN, = ROSTER.champion_ids("Draven")
KARMA, = ROSTER.champion_ids("Karma")
CAUSTIC, = ROSTER.trait_ids("Caustic")
ADAPTOR, = ROSTER.trait_ids("Adaptor")
INVOKER, = ROSTER.trait_ids("Invoker")
BOUNTY, = ROSTER.trait_ids("Bounty Seeker")
KOG = {"character_id": KOGMAW, "name": "Kog'Maw", "slug": "kogmaw", "cost": 3, "art_url": None}


# ---------------------------------------------------------------- recipes in the item snapshot


def test_item_snapshot_keeps_each_items_own_recipe_and_never_borrows_one() -> None:
    def item(item_id, name, composition=()):
        return ItemMeta(item_id=item_id, name=name, icon_url=None, composition=tuple(composition))

    meta = SetMetadata(patch="latest", set_number=99, champions={}, traits={}, items={
        "TFT_Item_RecurveBow": item("TFT_Item_RecurveBow", "Recurve Bow"),
        "TFT_Item_NeedlesslyLargeRod": item("TFT_Item_NeedlesslyLargeRod", "Needlessly Large Rod"),
        "TFT_Item_Guinsoo": item("TFT_Item_Guinsoo", "Guinsoo", ["TFT_Item_RecurveBow", "TFT_Item_NeedlesslyLargeRod"]),
        "DA_Guinsoo": item("DA_Guinsoo", "Guinsoo", ["DA_Component_Bow", "DA_Component_Rod"]),
        # Same display name as a craftable TFT item, but no recipe of its own.
        "DA_Item_Guinsoo": item("DA_Item_Guinsoo", "Guinsoo"),
    })
    items = item_stats_snapshot(meta)["items"]
    assert items["TFT_Item_Guinsoo"]["composition"] == ["TFT_Item_RecurveBow", "TFT_Item_NeedlesslyLargeRod"]
    assert items["DA_Guinsoo"]["composition"] == ["DA_Component_Bow", "DA_Component_Rod"]  # ids exactly as served
    assert items["DA_Item_Guinsoo"]["alias_of"] == ["TFT_Item_Guinsoo"]
    assert items["DA_Item_Guinsoo"]["composition"] == []  # an alias never lends its recipe
    assert items["TFT_Item_RecurveBow"]["composition"] == []


def test_committed_snapshot_has_a_verified_recipe_for_every_current_set_craft() -> None:
    """A missing or malformed recipe fails here, offline, instead of quietly
    shrinking the component direction."""
    items = json.loads(ITEM_STATS_PATH.read_text())["items"]
    assert all("composition" in entry for entry in items.values())
    crafts = [
        i for i in items
        if i.startswith("DA_") and not i.startswith(("DA_Component_", "DA_18_Emblem", "DA_Item_"))
    ]
    assert len(crafts) >= 30  # Set 18 has 39; the exact recipes are pinned by the live tests
    for item_id in crafts:
        recipe = item_recipe(item_id)
        assert recipe is not None, item_id
        assert all(c.startswith("DA_Component_") and is_component(c) for c in recipe), (item_id, recipe)
    assert all(items[i]["composition"] == [] for i in items if i.startswith("DA_Component_"))
    assert item_recipe(GUINSOO) == (BOW, ROD)
    assert item_recipe(DCAP) == (ROD, ROD)


@pytest.mark.parametrize("item_id", [
    "TFT_Item_CursedBlade",  # a legacy entry whose "recipe" lists itself
    "DA_18_EmblemCoven",  # an emblem with no recipe
    BOW,  # a component
    "DA_Item_Artifact_TalismanOfAscension",  # an artifact
    "DA_GuinsoosRageblade" + "Radiant",  # not in the snapshot at all
    "DA_NotAnItem",
])
def test_only_an_exact_two_component_recipe_is_verified(item_id: str) -> None:
    assert item_recipe(item_id) is None


# ---------------------------------------------------------------- pure derivations


def row(item_ids: list[str], games: int, *, without: int = 20, top4_with: float = 0.6,
        top4_without: float | None = 0.5, adjusted: float | None = 0.02) -> dict:
    """A Champion Investigation item row (same fields as `_item_row`)."""
    return {
        "items": [item_ref(i) for i in item_ids], "games": games, "share_of_carry_games": games / (games + without),
        "top4_with": top4_with, "avg_placement_with": 4.0, "win_with": 0.1, "games_without": without,
        "top4_without": top4_without if without else None, "avg_placement_without": 4.5 if without else None,
        "adjusted_top4_difference": adjusted if without else None, "limited_sample": min(games, without) < 10,
        "evidence": "observed",
    }


def test_board_floor_is_a_sample_rule_and_a_frequent_item_with_a_negative_result_is_still_common() -> None:
    assert htp.meets_board_floor(row([GUINSOO], 10))
    assert not htp.meets_board_floor(row([GUINSOO], 9))  # too few boards
    # On 152 boards with a negative with-vs-without result: still a common item
    # (frequency), and nothing labels it strong or recommended.
    core = row([DCAP], 152, without=28, top4_with=0.51, top4_without=0.54, adjusted=-0.02)
    better_ranked = row([IE], 83, adjusted=0.09)
    picked = htp.common_items([better_ranked, core])
    assert [r["items"][0]["id"] for r in picked] == [DCAP, IE]  # most boards first, not association order
    assert picked[0]["adjusted_top4_difference"] < 0


def test_component_direction_comes_only_from_verified_recipes_and_says_so() -> None:
    rows = [
        row([GUINSOO], 40),  # Bow + Rod
        row(["DA_18_EmblemCoven"], 25),  # common emblem without a recipe: listed, not counted
        row([DCAP], 30),  # Rod + Rod: Rod counted once for this recipe, twice in copies
        row([IE], 12),  # Sword + Gloves
    ]
    selected = htp.common_items(rows)
    assert [r["items"][0]["id"] for r in selected] == [GUINSOO, DCAP, "DA_18_EmblemCoven", IE]  # by boards
    direction = htp.component_direction(selected)
    assert direction["derived_from_recipes"] is True and direction["observed_components"] is False
    assert "not what players actually held" in direction["basis"]
    assert direction["items_without_recipe"] == ["DA_18_EmblemCoven"] and direction["recipes_considered"] == 3
    ids = [c["component"]["id"] for c in direction["components"]]
    assert ids[0] == ROD  # in 2 of the 3 recipes
    rod = direction["components"][0]
    assert (rod["recipes"], rod["copies"], rod["games"]) == (2, 3, 70)
    assert [i["id"] for i in rod["items"]] == [GUINSOO, DCAP]
    # Ties on recipe count (1 each) go to more contributing boards: Bow (40) before Sword/Gloves (12).
    assert ids[1] == BOW and set(ids) <= {ROD, BOW, SWORD, GLOVE} and len(ids) == htp.DIRECTION_COMPONENTS
    assert all(c["component"]["kind"] == "component" for c in direction["components"])


def test_special_items_and_thiefs_gloves_never_reach_the_direction() -> None:
    rows = [
        row([TG], 50),  # never a carry item, and its recipe must not count
        row(["DA_Item_Artifact_TalismanOfAscension"], 50),
        row([GUINSOO + "Radiant"], 50),
        row(["DA_NotAnItem"], 50),
        row([JG], 20),
    ]
    selected = htp.common_items(rows)
    assert [r["items"][0]["id"] for r in selected] == [JG]
    assert {c["component"]["id"] for c in htp.component_direction(selected)["components"]} == {GLOVE, ROD}


def test_concise_items_are_the_six_most_common_by_boards_then_name() -> None:
    crafts = [GUINSOO, IE, DCAP, JG, BT, CLAW, "DA_Deathblade"]
    rows = [row([i], 10 + n, adjusted=0.1 - n / 100) for n, i in enumerate(crafts)]  # ranking order = list order
    picked = htp.common_items(rows)
    assert [r["items"][0]["id"] for r in picked] == list(reversed(crafts))[:6]  # most boards first; 10-board GUINSOO dropped
    tied = htp.common_items([row([JG], 20), row([BT], 20), row([IE], 20)])
    assert [r["items"][0]["name"] for r in tied] == ["Bloodthirster", "Infinity Edge", "Jeweled Gauntlet"]


def test_no_common_item_is_an_insufficient_recipe_state_not_a_guess() -> None:
    direction = htp.component_direction(htp.common_items([row([GUINSOO], 4)]))
    assert direction["status"] == "insufficient" and direction["components"] == []


@pytest.mark.parametrize("hit,miss,status", [
    (40, 60, "compared"), (6, 60, "limited"), (0, 50, "no_hits"), (50, 0, "all_hits"),
])
def test_star_signal_reports_the_existing_split_with_its_sample_state(hit: int, miss: int, status: str) -> None:
    carry = {"games": hit + miss, "three_star": {
        "hit_games": hit, "miss_games": miss, "hit_rate": hit / (hit + miss), "hit_top4_rate": 0.62,
        "miss_top4_rate": 0.43}}
    signal = htp.star_signal(carry)
    assert signal["status"] == status and signal["min_boards"] == 10
    assert (signal["hit_top4_rate"] is None) == (hit == 0) and (signal["miss_top4_rate"] is None) == (miss == 0)


def test_concise_teammates_are_ordered_by_frequency_not_by_score() -> None:
    def mate(cid, name, games, score):
        return {"character_id": cid, "name": name, "games": games, "adjusted_top4_difference": score}

    partners = [mate("a", "Zed", 30, 0.2), mate("b", "Ahri", 80, -0.1), mate("c", "Bard", 30, 0.0),
                mate("d", "Lux", 5, 0.5), mate("e", "Nami", 50, 0.0)]
    assert [m["name"] for m in htp.teammates(partners)] == ["Ahri", "Nami", "Bard", "Zed"]


def test_trait_direction_uses_riot_num_units_and_needs_boards() -> None:
    def count(n, games):
        return {"num_units": n, "games": games, "share_of_carry_games": games / 100}

    traits = [
        {"trait_id": "T1", "name": "One", "art_url": None, "games": 90, "share_of_carry_games": 0.9,
         "counts": [count(2, 30), count(4, 50), count(6, 10)]},
        {"trait_id": "T2", "name": "Two", "art_url": None, "games": 40, "share_of_carry_games": 0.4,
         "counts": [count(2, 20), count(3, 20)], "above_baseline_only": True},
        {"trait_id": "T3", "name": "Three", "art_url": None, "games": 9, "share_of_carry_games": 0.09,
         "counts": [count(2, 9)]},
    ]
    picks = htp.trait_directions(traits)
    assert [(t["trait_id"], t["num_units"]) for t in picks] == [("T1", 4), ("T2", 2)]  # tie -> lower count
    assert picks[1]["above_baseline_only"] and all(t["trait_id"] != "T3" for t in picks)


# ---------------------------------------------------------------- the payload, on Riot-shaped boards


def _board(match_id: str, carry: str, items: list[str], placement: int, traits: list[tuple[str, int, int]],
           partners: tuple[str, ...] = (), tier: int = 2) -> dict:
    units = [make_unit(carry, tier=tier, items=items)] + [make_unit(p, tier=2) for p in partners]
    return make_match(match_id, units=units, placement=placement, traits=[
        {"name": t, "num_units": n, "style": 1, "tier_current": tier_current, "tier_total": 4}
        for t, n, tier_current in traits])


def _seed(db: Database) -> None:
    for i in range(16):  # Kog'Maw carries: Guinsoo's lines, Draven on 12, Karma on 6
        items = [GUINSOO, IE, DCAP] if i % 2 else [GUINSOO, JG, DCAP]
        partners = tuple(p for p, on in ((DRAVEN, i < 12), (KARMA, i < 6)) if on)
        traits = [(CAUSTIC, 1, 1), (ADAPTOR, 3, 1), (INVOKER, 2, 1)] + ([(BOUNTY, 1, 1)] if i < 12 else [])
        db.ingest_match(_board(f"KOG_{i}", KOGMAW, items, 1 + i % 8, traits, partners, tier=3 if i < 9 else 2))
    for i in range(6):  # Thief's Gloves holders with offensive rolls: never carries
        db.ingest_match(_board(f"KOG_TG_{i}", KOGMAW, [TG, BT, CLAW], 1, [(CAUSTIC, 1, 1)]))
    db.ingest_match(_board("DRV_0", DRAVEN, [IE, BT], 3, [(BOUNTY, 1, 1)]))  # a 1-board carry


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run against Postgres"))]


@pytest.fixture(params=BACKENDS)
def db(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        database = Database(tmp_path / "how-to-play.sqlite3")
    else:
        database = Database(POSTGRES_TEST_URL)
        for table in ("discovery_prepared_candidates", "discovery_prepared_runs", "traits", "units",
                      "participants", "matches"):
            database.execute(f"DELETE FROM {table}")
        database.commit()
    _seed(database)
    yield database
    database.close()


def test_payload_adds_how_to_play_and_individual_items_without_breaking_old_fields(db: Database) -> None:
    body = champion_investigation(db, KOG, WINDOW)
    for key in ("carry", "summary", "partners", "traits", "intrinsic_traits", "window_average"):
        assert key in body
    for key in ("most_common_build", "most_common_normal_build", "builds", "pairs", "individual"):
        assert key in body["items"]
    individual = {r["items"][0]["id"] for r in body["items"]["individual"]}
    assert {GUINSOO, DCAP, IE, JG} <= individual
    h = body["how_to_play"]
    assert set(h) >= {"component_direction", "items", "pairs", "builds", "teammates", "trait_directions",
                      "star_signal", "summary"}
    assert h["evidence"] == "observed" and h["support_min_boards"] == 10


def test_thiefs_gloves_rolls_never_reach_items_or_the_direction(db: Database) -> None:
    body = champion_investigation(db, KOG, WINDOW)
    h = body["how_to_play"]
    shown = {i["id"] for r in (*body["items"]["individual"], *body["items"]["pairs"], *body["items"]["builds"],
                               *h["items"], *h["pairs"], *h["builds"]) for i in r["items"]}
    assert not shown & {TG, BT, CLAW}
    contributing = {i["id"] for c in h["component_direction"]["components"] for i in c["items"]}
    assert contributing <= {GUINSOO, DCAP, IE, JG} and TG not in contributing


def test_how_to_play_on_real_boards(db: Database) -> None:
    h = champion_investigation(db, KOG, WINDOW)["how_to_play"]
    assert {r["items"][0]["id"] for r in h["items"]} == {GUINSOO, DCAP}  # IE and JG are on 8 boards each
    direction = h["component_direction"]
    assert direction["status"] == "available"
    assert direction["components"][0]["component"]["id"] == ROD  # in both Guinsoo's and Deathcap recipes
    assert [m["character_id"] for m in h["teammates"]] == [DRAVEN, KARMA]  # 12 boards, then 6
    assert h["star_signal"]["status"] == "limited" and h["star_signal"]["hit_games"] == 9
    assert "observed associations" in h["summary"] and "because" not in h["summary"]


def test_trait_directions_skip_the_singleton_baseline_and_read_num_units(db: Database) -> None:
    h = champion_investigation(db, KOG, WINDOW)["how_to_play"]
    picks = {t["trait_id"]: t for t in h["trait_directions"]}
    assert CAUSTIC not in picks  # Kog'Maw's guaranteed 1-unit Caustic
    assert picks[ADAPTOR]["num_units"] == 3  # Riot num_units, not tier_current (1)
    assert picks[INVOKER]["num_units"] == 2


def test_a_teammates_singleton_trait_at_one_unit_is_detail_evidence_not_a_trait_direction(db: Database) -> None:
    """Draven's Bounty Seeker at 1 unit on 12 Kog'Maw boards only says Draven
    was there, which the teammates list already shows."""
    body = champion_investigation(db, KOG, WINDOW)
    h = body["how_to_play"]
    assert [t["trait_id"] for t in h["trait_directions"]] == [ADAPTOR, INVOKER]
    assert DRAVEN in {m["character_id"] for m in h["teammates"]}
    bounty = next(t for t in body["traits"] if t["trait_id"] == BOUNTY)  # TRAITS tab keeps it
    assert [c["num_units"] for c in bounty["counts"]] == [1] and bounty["games"] == 12
    assert "Bounty Seeker" not in h["summary"] and "Adaptor at 3 units" in h["summary"]


def test_a_teammates_singleton_trait_selected_at_two_units_stays_eligible(db: Database) -> None:
    for i in range(14):  # Bounty Seeker reported at 2 units (e.g. an emblem) on 14 boards: now its most common count
        db.ingest_match(_board(f"KOG_BS2_{i}", KOGMAW, [GUINSOO, DCAP], 3,
                               [(ADAPTOR, 3, 1), (BOUNTY, 2, 1)], (DRAVEN,)))
    db.commit()
    picks = {t["trait_id"]: t for t in champion_investigation(db, KOG, WINDOW)["how_to_play"]["trait_directions"]}
    assert picks[BOUNTY]["num_units"] == 2 and picks[BOUNTY]["count_games"] == 14


def test_provider_count_comes_from_the_roster_not_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 1-unit filter applies to a trait with exactly one natural shop
    provider in the committed roster. Give the same trait a second provider
    and the same 1-unit observation becomes an ordinary direction."""
    def count(n, games):
        return {"num_units": n, "games": games, "share_of_carry_games": games / 100}

    def trait(trait_id, counts):
        return {"trait_id": trait_id, "name": trait_id, "art_url": None, "games": sum(c["games"] for c in counts),
                "share_of_carry_games": 0.5, "counts": counts}

    rows = [trait(BOUNTY, [count(1, 60), count(2, 9)]), trait(ADAPTOR, [count(1, 40), count(3, 20)])]
    assert htp.natural_provider_count(BOUNTY) == 1 and htp.natural_provider_count(ADAPTOR) > 1
    # One-provider trait selected at 1 unit: skipped. Multi-provider trait at 1 unit: unchanged.
    assert [(t["trait_id"], t["num_units"]) for t in htp.trait_directions(rows)] == [(ADAPTOR, 1)]

    karma = dict(ROSTER.champions[KARMA], traits=[*ROSTER.champions[KARMA]["traits"], BOUNTY])
    two_providers = dataclasses.replace(ROSTER, champions={**ROSTER.champions, KARMA: karma})
    monkeypatch.setattr(htp, "load_roster", lambda: two_providers)
    assert htp.natural_provider_count(BOUNTY) == 2
    assert [(t["trait_id"], t["num_units"]) for t in htp.trait_directions(rows)] == [(BOUNTY, 1), (ADAPTOR, 1)]


def test_a_singleton_trait_above_its_baseline_can_be_a_direction(db: Database) -> None:
    for i in range(13):  # more boards than Bounty Seeker's 12, so it makes the top three
        db.ingest_match(_board(f"KOG_EXT_{i}", KOGMAW, [GUINSOO, IE], 2,
                               [(CAUSTIC, 2, 1), (ADAPTOR, 3, 1), (INVOKER, 2, 1)]))
    db.commit()
    picks = {t["trait_id"]: t for t in champion_investigation(db, KOG, WINDOW)["how_to_play"]["trait_directions"]}
    assert CAUSTIC in picks and picks[CAUSTIC]["num_units"] == 2 and picks[CAUSTIC]["above_baseline_only"]


def test_low_sample_champion_gets_honest_empty_states(db: Database) -> None:
    draven = {"character_id": DRAVEN, "name": "Draven", "slug": "draven", "cost": 5, "art_url": None}
    h = champion_investigation(db, draven, WINDOW)["how_to_play"]
    assert h["items"] == h["pairs"] == h["builds"] == h["trait_directions"] == []
    assert h["component_direction"]["status"] == "insufficient"
    assert h["star_signal"]["status"] == "no_hits"


def test_no_carry_boards_means_no_how_to_play(db: Database) -> None:
    karma = {"character_id": KARMA, "name": "Karma", "slug": "karma", "cost": 1, "art_url": None}
    body = champion_investigation(db, karma, WINDOW)
    assert body["carry"] is None and body["how_to_play"] is None and body["items"]["individual"] == []


# ---------------------------------------------------------------- the page


def test_page_has_accessible_tabs_and_no_intrinsic_block() -> None:
    js = (Path(__file__).parents[1] / "src" / "tftlab" / "web" / "static" / "champion.js").read_text()
    for needle in ('role="tablist"', 'role="tab"', 'role="tabpanel"', "aria-selected", "aria-controls",
                   "ArrowRight", "ArrowLeft", "Home", "End"):
        assert needle in js, needle
    for tab in ("How to play", "Items", "Teammates", "Traits", "Evidence"):
        assert f"'{tab}'" in js, tab
    assert "Comps" not in js and "intrinsic" not in js.lower()
    assert "derived from recipes" in js.lower() or "recipe direction" in js.lower()


def test_concise_labels_describe_frequency_never_strength() -> None:
    js = (Path(__file__).parents[1] / "src" / "tftlab" / "web" / "static" / "champion.js").read_text()
    play = js[js.index("function howToPlaySection("):js.index("// ------------------------------------------------------------------ tabs")]
    for word in ("support", "strong", "best", "recommend", "optimal"):
        assert word not in play.lower().replace("support_min_boards", ""), word
    for label in ("Common items", "Common observed pairs", "Common full builds", "most used first"):
        assert label in play, label
    assert "Supported items" not in js and "Strong observed pairs" not in js
    summary = htp.evidence_summary("X", 50, [row([GUINSOO], 40)], [], [])
    assert "most commonly used items" in summary and not any(w in summary.lower() for w in ("best", "strong", "support"))


def test_concise_pairs_and_builds_are_ordered_by_boards_then_name() -> None:
    carry = {"games": 100, "three_star": {"hit_games": 0, "miss_games": 100, "hit_rate": 0.0,
                                          "hit_top4_rate": None, "miss_top4_rate": 0.5}}
    pairs = [row([GUINSOO, IE], 12, adjusted=0.09), row([GUINSOO, DCAP], 60, adjusted=-0.03),
             row([IE, DCAP], 30), row([GUINSOO, JG], 30, adjusted=0.0), row([JG, DCAP], 9)]
    builds = [row([GUINSOO, IE, DCAP], 15, adjusted=0.1), row([GUINSOO, JG, DCAP], 25, adjusted=-0.05)]
    h = htp.how_to_play("X", carry, item_rows=[], pair_rows=pairs, build_rows=builds, partner_rows=[], trait_rows=[])
    assert [(r["games"], r["items"][1]["name"]) for r in h["pairs"]] == [
        (60, "Rabadon's Deathcap"), (30, "Jeweled Gauntlet"), (30, "Rabadon's Deathcap")]  # 9-board pair below floor
    assert [r["games"] for r in h["builds"]] == [25, 15]


def test_recipe_direction_reads_exactly_the_common_items_shown(db: Database) -> None:
    h = champion_investigation(db, KOG, WINDOW)["how_to_play"]
    assert h["component_direction"]["items_considered"] == [r["items"][0]["id"] for r in h["items"]]


def test_items_tab_keeps_the_analytics_order_and_with_vs_without_values(db: Database) -> None:
    from tftlab.analytics import item_package_stats

    body = champion_investigation(db, KOG, WINDOW)
    ranked = [a.key for a in item_package_stats(db, KOGMAW, WINDOW)["items"]][:len(body["items"]["individual"])]
    assert [r["items"][0]["id"] for r in body["items"]["individual"]] == ranked
    for r in body["items"]["individual"]:
        assert {"top4_with", "top4_without", "adjusted_top4_difference", "games_without"} <= set(r)


def test_normal_items_fall_back_to_an_exact_display_name_icon_but_special_items_never_borrow() -> None:
    sword = item_ref(SWORD)  # no snapshot alias; one cached item is named exactly "B.F. Sword"
    assert sword["kind"] == "component" and sword["art_url"] and "TFT_Item_BFSword" in sword["art_url"]
    assert item_ref("DA_Item_Artifact_TalismanOfAscension")["art_url"] is None
    assert item_ref(GUINSOO + "Radiant")["art_url"] is None
