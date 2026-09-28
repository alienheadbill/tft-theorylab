"""Board-archetype research harness (tftlab.archetype_research, `tftlab archetype-report`).

Synthetic fixtures only: these tests check the harness's mechanics (read-only
access, determinism, similarity, flex tolerance, shell separation, canonical
names, workflow safety). They say nothing about whether real TFT boards form
good archetypes -- that is what the harness exists to find out.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tftlab import archetype_research as ar
from tftlab.cli import app
from tftlab.research_report import NotReadOnly
from tftlab.roster import load_roster
from tftlab.storage import Database

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "read-only-archetype-report.yml"
WINDOW = "14.6"  # the balance window of `make_payload`'s game_version

# Real Set 18 roster ids (so canonical names resolve); the comps are fictional.
C1 = ["DA_18_Akali_AD", "DA_18_Camille", "DA_18_Kobuko", "DA_18_Leona", "DA_18_Ornn", "DA_18_Rakan", "DA_18_RekSai", "DA_18_Varus"]
C2 = ["DA_18_Alistar", "DA_18_Caitlyn", "DA_18_Elise", "DA_18_Kayle", "DA_18_LeBlanc", "DA_18_Sejuani", "DA_18_Shen", "DA_18_Teemo"]
C3 = ["DA_18_Azir", "DA_18_Cassiopeia", "DA_18_Diana", "DA_18_Hecarim", "DA_18_KhaZix", "DA_18_Rammus", "DA_18_Rengar"]
C4 = ["DA_18_Ahri", "DA_18_Aphelios", "DA_18_Ezreal", "DA_18_Lillia", "DA_18_Malphite", "DA_18_Morgana", "DA_18_Sett", "DA_18_Sivir"]
CARRY_ITEMS = ["DA_GuinsoosRageblade", "DA_InfinityEdge", "DA_LastWhisper"]
TANK_ITEMS = ["DA_WarmogsArmor", "DA_GargoyleStoneplate"]
COST = {cid: c["cost"] for cid, c in load_roster().champions.items()}

# Fictional templates. SHELL is shared by X-carry (flex choices) and Y-carry.
X, Y = "DA_18_Rakan", "DA_18_Veigar"
SHELL = ["DA_18_Leona", "DA_18_Kayle", "DA_18_Caitlyn", "DA_18_Azir", "DA_18_Ahri"]
FLEX = ["DA_18_Diana", "DA_18_Lillia", "DA_18_Sett"]
OTHER_SHELL = ["DA_18_Sejuani", "DA_18_Teemo", "DA_18_Hecarim", "DA_18_Rengar", "DA_18_Morgana"]


def unit(cid: str, *, tier: int = 2, items: list[str] | None = None) -> dict:
    return {"character_id": cid, "name": "", "rarity": COST.get(cid, 1) - 1, "tier": tier, "itemNames": items or []}


TRAIT_IDS = sorted(load_roster().traits)


def board_traits(board: list[dict]) -> list[dict]:
    """Fictional but board-dependent traits: each champion belongs to one
    trait (by roster order); tier = breakpoints 2/4/6 reached."""
    counts: dict[str, int] = {}
    for cid in sorted({u["character_id"] for u in board}):
        trait = TRAIT_IDS[sorted(COST).index(cid) % len(TRAIT_IDS)] if cid in COST else TRAIT_IDS[0]
        counts[trait] = counts.get(trait, 0) + 1
    out = []
    for trait, n in sorted(counts.items()):
        tier = sum(n >= bp for bp in (2, 4, 6))
        out.append({"name": trait, "num_units": n, "style": tier, "tier_current": tier, "tier_total": 3})
    return out


def make_payload(match_id: str, boards: list[list[dict]], *, queue_id: int = 1100, puuid_prefix: str = "PUUID-SECRET",
                 placements: list[int] | None = None) -> dict:
    placements = placements or list(range(1, len(boards) + 1))
    return {
        "metadata": {"match_id": match_id, "participants": [f"{puuid_prefix}-{i}" for i in range(len(boards))]},
        "info": {
            "game_version": "Version 14.6.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/14.6>",
            "tft_game_type": "standard", "queue_id": queue_id, "tft_set_number": 18, "tft_set_core_name": "TFTSet18",
            "game_datetime": 1_790_000_000_000,
            "participants": [
                {"placement": placements[i], "level": 8, "augments": [], "units": b, "puuid": f"{puuid_prefix}-{i}",
                 "riotIdGameName": "SecretName", "riotIdTagline": "NA1",
                 "traits": board_traits(b)}
                for i, b in enumerate(boards)
            ],
        },
    }


def x_board(rng: random.Random) -> list[dict]:
    flex = rng.sample(FLEX, 2)
    return ([unit(X, tier=3, items=CARRY_ITEMS), unit(SHELL[0], items=TANK_ITEMS)]
            + [unit(c) for c in SHELL[1:]] + [unit(c, tier=1) for c in flex])


def y_board(rng: random.Random) -> list[dict]:
    return ([unit(Y, tier=2, items=["DA_JeweledGauntlet", "DA_RabadonsDeathcap", "DA_BlueBuff"]),
             unit(SHELL[0], items=TANK_ITEMS)] + [unit(c) for c in SHELL[1:]] + [unit(rng.choice(FLEX), tier=1)])


def x_other_shell(rng: random.Random) -> list[dict]:
    return [unit(X, tier=3, items=CARRY_ITEMS), unit(OTHER_SHELL[0], items=TANK_ITEMS)] + [unit(c) for c in OTHER_SHELL[1:]] + [
        unit(rng.choice(["DA_18_Malphite", "DA_18_Sivir"]), tier=1)]


def noise(rng: random.Random) -> list[dict]:
    return [unit(c) for c in rng.sample(C1 + C2 + C3 + C4, 7)]


def build_payloads(seed: int = 3, matches: int = 12, placement_seed: int | None = None) -> list[dict]:
    """`placement_seed` permutes each match's placements while keeping every
    board's structure (units, stars, items, traits) and position identical."""
    rng = random.Random(seed)
    kinds = [x_board] * 4 + [y_board] * 2 + [x_other_shell] * 1 + [noise]
    payloads = []
    for m in range(matches):
        boards = [kind(rng) for kind in kinds]
        rng.shuffle(boards)
        placements = list(range(1, len(boards) + 1))
        if placement_seed is not None:
            random.Random(placement_seed * 1000 + m).shuffle(placements)
        payloads.append(make_payload(f"M{m:03d}", boards, placements=placements))
    return payloads


def populate(db: Database, payloads: list[dict]) -> None:
    """The Ranked boards plus two population edge cases:
    - EMPTYR (Ranked): one participant Riot sent with no units (source-empty);
    - NORMAL1 (queue 1090, not Ranked): a 6-unit board that would be
      grouping-eligible if queue scoping were wrong, a source-empty
      participant, and an "unexpected" participant (the payload lists units
      but the stored rows are gone) -- none may reach the Ranked counts."""
    for p in payloads:
        db.ingest_match(p)
    db.ingest_match(make_payload("EMPTYR", [[]]))
    db.ingest_match(make_payload("NORMAL1", [[unit(c) for c in C1[:6]], [], [unit(c) for c in C2[:5]]], queue_id=1090))
    db.execute("DELETE FROM units WHERE match_id = 'NORMAL1' AND participant_index = 2")
    db.commit()


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store.sqlite3"
    with Database(path) as db:
        populate(db, build_payloads())
    return path


def run(store: Path, config: ar.ArchetypeConfig | None = None):
    with Database.open_existing(store) as db:
        return ar.build_report(db, balance_window=WINDOW, config=config)


def groups_of(report: dict, strategy: str) -> list[dict]:
    return report["strategies"][strategy]["groups"]


def group_with(report: dict, strategy: str, members: set[str]) -> list[int]:
    return [g["group"] for g in groups_of(report, strategy) if members <= set(g["core_candidates"])]


# ---------------------------------------------------------------- normalization / similarity


def test_normalized_board_is_deterministic_and_excludes_non_shop_entities() -> None:
    champions = load_roster().champions
    units = [
        ar.Unit("DA_18_RekSai", 2, 1, ("DA_SteraksGage",), ("DA_SteraksGage",), True),
        ar.Unit("TFT_TrainingDummy", 1, 1, (), (), False),  # roster lists it at cost 1: must still be excluded
        ar.Unit("TFT_ArmoryKeyOrnn", 1, 8, (), (), False),
        ar.Unit("TFT99_NewChampion", 2, 3, (), (), False),  # unknown to the roster, shop cost: kept, raw id
        ar.Unit("TFT99_NoCost", 1, None, (), (), False),
        ar.Unit("DA_18_RekSai", 1, 1, (), (), False),  # duplicate copy
    ]
    a = ar.normalize_board(0, ("M", 0), 1, 8, units, {"DA_18_Slayer": 1}, champions)
    b = ar.normalize_board(0, ("M", 0), 1, 8, list(units), {"DA_18_Slayer": 1}, champions)
    assert a == b
    assert a.identity == frozenset({"DA_18_RekSai", "TFT99_NewChampion"})
    assert a.excluded == ("TFT99_NoCost", "TFT_ArmoryKeyOrnn", "TFT_TrainingDummy")
    assert a.trait_keys == frozenset({"DA_18_Slayer:1"})


def test_ruzicka() -> None:
    assert ar.ruzicka({"a": 1, "b": 1}, {"a": 1, "b": 1}) == 1.0
    assert ar.ruzicka({"a": 1, "b": 1}, {"c": 1}) == 0.0
    assert ar.ruzicka({"a": 1, "b": 1, "c": 1}, {"a": 1, "b": 1, "d": 1}) == pytest.approx(2 / 4)
    assert ar.ruzicka({"a": 1, "b": 1}, {"a": 0.5, "b": 1}) == pytest.approx(1.5 / 2)
    assert ar.ruzicka({}, {}) == 0.0


def test_structural_baseline_uses_champion_set_only() -> None:
    champions = load_roster().champions
    config = ar.ArchetypeConfig()
    itemized = [ar.Unit(c, 3, None, tuple(CARRY_ITEMS), tuple(CARRY_ITEMS), True) for c in C1[:5]]
    bare = [ar.Unit(c, 1, None, (), (), False) for c in C1[:5]]
    b1 = ar.normalize_board(0, ("M", 0), 1, 8, itemized, {"DA_18_Slayer": 1}, champions)
    b2 = ar.normalize_board(1, ("M", 1), 8, 8, bare, {}, champions)
    base, aware = ar.STRATEGIES[0], ar.STRATEGIES[2]
    v = lambda b, s: ar.board_vectors(b, s, config, champions)  # noqa: E731
    assert ar.similarity(v(b1, base), v(b2, base), base) == 1.0  # items, stars, traits, placement ignored
    assert ar.similarity(v(b1, aware), v(b2, aware), aware) < 1.0


def test_strategy_definitions_are_explicit() -> None:
    names = [s.name for s in ar.STRATEGIES]
    assert names == ["A_structural_baseline", "B_flex_tolerant", "C_structure_aware"]
    a, b, c = ar.STRATEGIES
    assert (a.weighted, a.trait_share, a.merge) == (False, 0.0, None)
    assert (b.weighted, b.trait_share, b.merge) == (False, 0.0, "structural")
    assert (c.weighted, c.trait_share, c.merge) == (True, 0.25, "structure_aware")


# ---------------------------------------------------------------- grouping behaviour


def test_flex_slots_do_not_split_a_shell(store: Path) -> None:
    """Every X-shell board (whatever its two flex units) lands in one group;
    in B that group also holds the Y-carry boards on the same shell (see the
    next tests), in C it does not."""
    report, _, _ = run(store)
    for strategy in ("B_flex_tolerant", "C_structure_aware"):
        homes = [g for g in groups_of(report, strategy) if X in g["units"] and set(SHELL) <= set(g["core_candidates"])]
        assert len(homes) == 1, strategy
        g = homes[0]
        assert round(g["units"][X]["presence"] * g["boards"]) == 12 * 4, strategy
        assert set(FLEX) <= set(g["other_common_units"]) | set(g["core_candidates"]), strategy


def test_same_carry_different_shell_stays_separate(store: Path) -> None:
    report, _, _ = run(store)
    for strategy in ("A_structural_baseline", "B_flex_tolerant", "C_structure_aware"):
        shell = group_with(report, strategy, set(SHELL))
        other = group_with(report, strategy, set(OTHER_SHELL))
        assert shell and other and not set(shell) & set(other), strategy
        with_x = {g["group"] for g in groups_of(report, strategy) if X in g["units"]}
        assert with_x & set(shell) and with_x & set(other), strategy  # X appears in both, in different groups


def test_same_shell_different_itemized_unit_is_where_b_and_c_differ(store: Path) -> None:
    """Documented difference: B (champion presence only) may merge a shell
    whose itemized unit is swapped; C keeps a swapped itemized unit apart."""
    report, _, _ = run(store)
    c_x, c_y = group_with(report, "C_structure_aware", {X, *SHELL}), group_with(report, "C_structure_aware", {Y, *SHELL})
    assert len(c_x) == 1 and len(c_y) == 1 and c_x != c_y
    b_shell = group_with(report, "B_flex_tolerant", set(SHELL))
    assert len(b_shell) == 1  # B: one core swap (X <-> Y) on an identical shell merges
    merged = next(g for g in groups_of(report, "B_flex_tolerant") if g["group"] == b_shell[0])
    assert merged["units"][X]["presence"] > 0 and merged["units"][Y]["presence"] > 0


def test_noise_boards_stay_ungrouped_or_tiny(store: Path) -> None:
    report, _, _ = run(store)
    for strategy in report["strategies"].values():
        assert strategy["boards_considered"] == 12 * 8
        assert strategy["boards_in_multi_board_groups"] + strategy["boards_ungrouped"] == 12 * 8


def test_prune_audit_agrees_with_brute_force(store: Path) -> None:
    report, _, _ = run(store, ar.ArchetypeConfig(prune_audit_boards=10_000))
    for strategy in report["strategies"].values():
        assert strategy["prune_audit"]["boards_audited"] == 12 * 8
        assert strategy["prune_audit"]["disagreements"] == 0


# ---------------------------------------------------------------- variant merge: result checks (validation run #2)

IDS = sorted(ar.identity_champions())
B_STRATEGY, C_STRATEGY = ar.STRATEGIES[1], ar.STRATEGIES[2]
# Test-only configs that switch the pairwise preconditions off so each merged-result rule can be exercised alone
# (with the defaults the core-retention branch is implied by the pairwise overlap requirement, see ArchetypeConfig).
PAIRWISE_OFF = {"merge_min_shared": 2, "merge_shared_fraction": 0.0, "max_swaps": 10}


def variants(*specs: tuple[list[int], int], itemized: set[int] = frozenset()) -> tuple[list[ar.Board], dict[int, int]]:
    """Variant v = specs[v]: (unit indices into IDS, number of identical boards). Indices in `itemized` hold 2 items."""
    champions = load_roster().champions
    boards, variant = [], {}
    for v, (idx, n) in enumerate(specs):
        for _ in range(n):
            obs = len(boards)
            units = [ar.Unit(IDS[i], 2, None, tuple(CARRY_ITEMS[:2]) if i in itemized else (),
                             tuple(CARRY_ITEMS[:2]) if i in itemized else (), False) for i in idx]
            boards.append(ar.normalize_board(obs, ("M", obs), 1, 8, units, {"DA_18_Slayer": 1}, champions))
            variant[obs] = v
    return boards, variant


def merged_sets(boards, variant, strategy, config) -> tuple[list[list[int]], dict[str, int]]:
    to_group, _, checks = ar.merge_variants(boards, variant, strategy, config)
    by_group: dict[int, list[int]] = {}
    for v in sorted({variant[b.obs] for b in boards}):
        by_group.setdefault(to_group[v], []).append(v)
    return sorted(by_group.values()), checks


def test_close_variants_still_merge() -> None:
    """One flex swap on an 8-unit core: the merged core keeps 7 units and every board is 0.88 similar."""
    boards, variant = variants((list(range(8)), 4), ([0, 1, 2, 3, 4, 5, 6, 8], 4))
    for strategy in (B_STRATEGY, C_STRATEGY):
        groups, checks = merged_sets(boards, variant, strategy, ar.ArchetypeConfig())
        assert groups == [[0, 1]], strategy.name
        assert checks["rejected_core"] == checks["rejected_similarity"] == 0


# A -> B -> C erode the merged core one swap at a time; D shares only 5 units with A, B or C (never pairwise
# mergeable with any of them) but satisfies the eroded A+B+C core.
DRIFT = ((list(range(8)), 4), ([0, 1, 2, 3, 4, 5, 6, 8], 4), ([0, 1, 2, 3, 4, 5, 8, 9], 4), ([0, 1, 2, 3, 4, 10, 11, 12], 4))
#: The pre-run-#2 behaviour: no merged-result checks (tau is used by the merge only for the similarity check).
UNCHECKED = ar.ArchetypeConfig(tau=0.0, merge_min_result_core=0)


def test_transitive_drift_is_stopped_by_the_merged_result_check() -> None:
    boards, variant = variants(*DRIFT)
    for v in (0, 1, 2):  # D is not pairwise mergeable with any original variant, even unchecked
        pair = [b for b in boards if variant[b.obs] in (v, 3)]
        assert len(merged_sets(pair, variant, B_STRATEGY, UNCHECKED)[0]) == 2
    unchecked, _ = merged_sets(boards, variant, B_STRATEGY, UNCHECKED)
    assert unchecked == [[0, 1, 2, 3]]  # the old behaviour chains D in through the eroded core
    champions = load_roster().champions
    vecs = [ar.board_vectors(b, B_STRATEGY, ar.ArchetypeConfig(), champions) for b in boards]
    profile = ar.mean_profile(vecs)
    assert min(ar.similarity(v, profile, B_STRATEGY) for v in vecs) < ar.ArchetypeConfig().tau  # incoherent result
    checked, checks = merged_sets(boards, variant, B_STRATEGY, ar.ArchetypeConfig())
    assert checked == [[0, 1, 2], [3]]
    assert checks["rejected_similarity"] >= 1 and checks["rejected_core"] == 0


@pytest.mark.parametrize(("a", "b", "merged"), [
    # both cores 7 units; merged core exactly 5 units: accepted by the size branch
    (list(range(7)), [0, 1, 2, 3, 4, 7, 8], True),
    # merged core 4 units and keeps 4/7 < 60% of the (equal) smaller cores: rejected
    (list(range(7)), [0, 1, 2, 3, 7, 8, 9], False),
    # merged core 3 units but keeps exactly 3/5 = 60% of the smaller core: accepted by the retention branch
    (list(range(5)), [0, 1, 2, 5, 6, 7, 8, 9], True),
    # merged core 2 units, keeps 2/5 = 40% of the smaller core: rejected
    (list(range(5)), [0, 1, 5, 6, 7, 8, 9, 10], False),
])
def test_merged_core_rule_boundaries(a: list[int], b: list[int], merged: bool) -> None:
    boards, variant = variants((a, 4), (b, 4))
    config = ar.ArchetypeConfig(tau=0.0, **PAIRWISE_OFF)  # isolate the core rule
    groups, checks = merged_sets(boards, variant, B_STRATEGY, config)
    assert (groups == [[0, 1]]) is merged
    assert checks["rejected_core"] == (0 if merged else 1)
    assert checks["rejected_similarity"] == 0


def test_default_core_retention_is_implied_by_the_pairwise_overlap() -> None:
    """Documented property: merged core >= shared core >= 60% of the larger core, so with the default fractions
    the core rule never rejects a pair the pairwise check accepted; the similarity check is the binding one."""
    config = ar.ArchetypeConfig()
    assert config.merge_shared_fraction >= config.merge_core_retention
    boards, variant = variants(*DRIFT, (list(range(4)) + [13, 14, 15, 16], 4), ([0, 1, 2, 3, 13, 14, 15, 17], 2))
    assert merged_sets(boards, variant, B_STRATEGY, ar.ArchetypeConfig(tau=0.0))[1]["rejected_core"] == 0


def test_a_board_below_tau_against_the_merged_profile_rejects_the_merge() -> None:
    """One board each, 3 shared + 3 own units: every board is exactly 4.5 / 7.5 = 0.6 similar to the merged
    profile. At tau = 0.6 the merge is kept (>= tau, as in grouping); at the next float above it is rejected."""
    boards, variant = variants(([0, 1, 2, 3, 4, 5], 1), ([0, 1, 2, 6, 7, 8], 1))
    champions = load_roster().champions
    vecs = [ar.board_vectors(b, B_STRATEGY, ar.ArchetypeConfig(), champions) for b in boards]
    assert [ar.similarity(v, ar.mean_profile(vecs), B_STRATEGY) for v in vecs] == [0.6, 0.6]
    at = ar.ArchetypeConfig(tau=0.6, merge_min_result_core=0, **PAIRWISE_OFF)  # isolate the similarity rule
    above = ar.ArchetypeConfig(tau=math.nextafter(0.6, 1.0), merge_min_result_core=0, **PAIRWISE_OFF)
    assert merged_sets(boards, variant, B_STRATEGY, at)[0] == [[0, 1]]
    groups, checks = merged_sets(boards, variant, B_STRATEGY, above)
    assert groups == [[0], [1]] and checks["rejected_similarity"] == 1


def test_c_absorbs_a_full_core_plus_one_itemized_splash() -> None:
    """Satellite = the parent's full 8-unit core + one unit holding 2 items on every board. Without the exception
    C's restriction keeps it apart (the added unit is itemized and not flex in the parent)."""
    parent, satellite = list(range(8)), list(range(8)) + [8]
    boards, variant = variants((parent, 6), (satellite, 3), itemized={0, 8})
    groups, checks = merged_sets(boards, variant, C_STRATEGY, ar.ArchetypeConfig())
    assert groups == [[0, 1]] and checks["rejected_similarity"] == 0
    # The exception needs a fully shared core of >= merge_min_result_core units; raise it past 8 and C keeps them apart.
    assert merged_sets(boards, variant, C_STRATEGY, ar.ArchetypeConfig(merge_min_result_core=9))[0] == [[0], [1]]


@pytest.mark.parametrize(("a", "b"), [
    # only a 4-unit core is shared: too small for the exception
    ([0, 1, 2, 3], [0, 1, 2, 3, 8]),
    # the itemized unit is a swap, not an addition: no exception
    (list(range(8)), [0, 1, 2, 3, 4, 5, 6, 8]),
    # two added itemized units: no exception
    (list(range(8)), list(range(8)) + [8, 9]),
])
def test_c_splash_exception_does_not_merge_different_comps(a: list[int], b: list[int]) -> None:
    boards, variant = variants((a, 6), (b, 3), itemized={0, 8, 9})
    assert merged_sets(boards, variant, C_STRATEGY, ar.ArchetypeConfig())[0] == [[0], [1]]


def test_merge_is_deterministic_and_independent_of_board_order() -> None:
    boards, variant = variants(*DRIFT, (list(range(8)) + [8], 3), itemized={0, 8})
    for strategy in (B_STRATEGY, C_STRATEGY):
        first = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig())
        again = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig())
        reversed_order = ar.merge_variants(list(reversed(boards)), variant, strategy, ar.ArchetypeConfig())
        assert first == again == reversed_order, strategy.name


def test_report_prints_the_merge_checks(store: Path) -> None:
    report, markdown, _ = run(store)
    assert report["strategies"]["A_structural_baseline"]["merge_checks"] == {}  # A never merges
    for strategy in ("B_flex_tolerant", "C_structure_aware"):
        checks = report["strategies"][strategy]["merge_checks"]
        assert set(checks) == {"rejected_core", "rejected_similarity", "variants_with_a_board_below_tau_before_merge"}
    assert sum(line.startswith("- merge_checks: ") for line in markdown) == 3


# ---------------------------------------------------------------- population / names / outputs


def test_population_uses_ranked_unit_observable_boards(store: Path) -> None:
    report, markdown, _ = run(store)
    pop = report["population"]
    assert (pop["window_matches_any_queue"], pop["excluded_non_ranked_matches"], pop["ranked_matches"]) == (14, 1, 13)
    assert (pop["ranked_participants"], pop["ranked_unit_observable_participants"], pop["ranked_participants_without_units"]) == (97, 96, 1)
    assert (pop["ranked_source_empty_participants"], pop["ranked_unexpected_participants_without_units"]) == (1, 0)
    assert pop["ranked_denominators_consistent"] is True
    assert pop["ranked_boards_eligible_for_grouping"] == 96
    assert pop["queue_id"] == 1100
    assert report["read_only_connection"] == "sqlite mode=ro"
    assert markdown[0].startswith("# Board archetype research report (EXPERIMENTAL")
    assert any("Declared configuration" in line for line in markdown)


def test_non_ranked_match_never_reaches_ranked_denominators_or_grouping(store: Path) -> None:
    """Mixed-queue regression: the non-Ranked match counts only toward the
    any-queue and excluded counts, never toward Ranked participants,
    source-empty/unexpected counts or grouping."""
    from tftlab.validate import classify_participants_without_units

    with Database.open_existing(store) as db:
        assert classify_participants_without_units(db, balance_window=WINDOW) == (2, 1)  # window, any queue
        assert classify_participants_without_units(db, balance_window=WINDOW, queue_id=1100) == (1, 0)
        assert classify_participants_without_units(db, balance_window=WINDOW, queue_id=1090) == (1, 1)
        assert classify_participants_without_units(db, queue_id=1090) == (1, 1)  # queue scope alone
    report, _, membership = run(store)
    pop = report["population"]
    assert pop["window_matches_any_queue"] - pop["ranked_matches"] == pop["excluded_non_ranked_matches"] == 1
    assert (pop["ranked_source_empty_participants"], pop["ranked_unexpected_participants_without_units"]) == (1, 0)
    for strategy in report["strategies"].values():
        assert strategy["boards_considered"] == 96  # the 6-unit Normal board is not grouped
    assert {r["observation"] for r in membership} <= set(range(96))


def test_canonical_names_come_from_committed_metadata_or_say_unresolved() -> None:
    names = ar.Names()
    assert names.champion("DA_Scuttlecrab18") == "Scuttlecrab"
    assert names.champion("DA_18_RekSai") == "Rek'Sai"
    assert names.item("DA_ThiefsGloves") == "Thief's Gloves"
    assert names.item("DA_Artifact_LightshieldCrest") == "UNRESOLVED: DA_Artifact_LightshieldCrest"
    assert names.champion("TFT99_Unknown") == "UNRESOLVED: TFT99_Unknown"
    assert names.trait("DA_18_Slayer") == "Ravager"
    assert names.unresolved == {"DA_Artifact_LightshieldCrest", "TFT99_Unknown"}


def test_markdown_shows_names_not_raw_ids(store: Path) -> None:
    _, markdown, _ = run(store)
    text = "\n".join(markdown)
    assert "Rakan" in text and "Rek'Sai" in text or "Rakan" in text
    body = text.split("## Unresolved ids")[0]
    assert "DA_18_" not in body.replace("UNRESOLVED: DA_18_", "")


def partitions(membership: list[dict]) -> dict[str, frozenset[frozenset[int]]]:
    """Label-free membership: per strategy, the set of groups as sets of
    observations (group numbers themselves are arbitrary)."""
    groups: dict[tuple[str, int], set[int]] = {}
    for row in membership:
        groups.setdefault((row["strategy"], row["group"]), set()).add(row["observation"])
    out: dict[str, set[frozenset[int]]] = {}
    for (strategy, _), obs in groups.items():
        out.setdefault(strategy, set()).add(frozenset(obs))
    return {k: frozenset(v) for k, v in out.items()}


@pytest.mark.parametrize("placement_seed", [1, 2, 3])
def test_archetype_membership_is_invariant_to_outcome(tmp_path: Path, placement_seed: int) -> None:
    """ARCHETYPE IDENTITY MUST BE INVARIANT TO OUTCOME: the same board
    structures with shuffled placements give the same groups in every
    strategy (placement is only summarized after grouping)."""
    base, shuffled = tmp_path / "base.sqlite3", tmp_path / f"shuffled{placement_seed}.sqlite3"
    with Database(base) as db:
        for p in build_payloads():
            db.ingest_match(p)
    with Database(shuffled) as db:
        for p in build_payloads(placement_seed=placement_seed):
            db.ingest_match(p)
    a, b = run(base)[2], run(shuffled)[2]
    assert [r["placement"] for r in a] != [r["placement"] for r in b]  # outcomes really changed
    pa, pb = partitions(a), partitions(b)
    assert set(pa) == {s.name for s in ar.STRATEGIES}
    for strategy in pa:
        assert pa[strategy] == pb[strategy], strategy


def test_report_is_read_only_and_deterministic(store: Path, tmp_path: Path) -> None:
    digest = hashlib.sha256(store.read_bytes()).hexdigest()
    first = run(store)
    second = run(store)
    assert hashlib.sha256(store.read_bytes()).hexdigest() == digest  # nothing written
    assert json.dumps(first[0], sort_keys=True, default=str) == json.dumps(second[0], sort_keys=True, default=str)
    assert first[1] == second[1] and first[2] == second[2]

    # Same data inserted in a different order: identical results.
    other = tmp_path / "shuffled.sqlite3"
    payloads = build_payloads()
    random.Random(99).shuffle(payloads)
    with Database(other) as db:
        populate(db, payloads)
    shuffled = run(other)
    assert json.dumps(first[0], sort_keys=True, default=str) == json.dumps(shuffled[0], sort_keys=True, default=str)


def test_validation_sample_is_deterministic_and_unique(store: Path) -> None:
    report, _, _ = run(store)
    for strategy in report["strategies"].values():
        sample = strategy["validation_sample"]
        assert len({s["group"] for s in sample}) == len(sample)
        again = ar.validation_sample(strategy["groups"])
        assert [{"reason": r, "group": g} for r, g in again] == sample


def test_refuses_a_writable_connection(store: Path) -> None:
    with Database(store) as db, pytest.raises(NotReadOnly):
        ar.build_report(db, balance_window=WINDOW)


def test_outputs_hold_no_player_or_match_identifiers(store: Path, tmp_path: Path) -> None:
    report, markdown, membership = run(store)
    paths = ar.write_outputs(report, markdown, membership, tmp_path / "out")
    assert sorted(p.name for p in paths) == ["archetypes_14.6_membership.csv", "archetypes_14.6_report.json", "archetypes_14.6_report.md"]
    for path in paths:
        text = path.read_text()
        for secret in ("PUUID-SECRET", "SecretName", "M000", "M011", "NORMAL1", "EMPTYR"):
            assert secret not in text, (path.name, secret)
    rows = list(csv.DictReader(paths[2].open()))
    assert set(rows[0]) == {"strategy", "observation", "group", "variant", "placement", "shop_units"}


def test_cli_prints_and_writes_without_riot_key_or_network(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.delenv("RIOT_API_KEY", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("the archetype report must not make network requests")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    result = CliRunner().invoke(app, ["archetype-report", "--db", str(store), "--balance-window", WINDOW,
                                      "--out-dir", str(tmp_path / "cli")])
    assert result.exit_code == 0, result.output
    assert "Read-only connection: sqlite mode=ro" in result.output
    assert "## Strategy A. structural baseline (control)" in result.output
    assert (tmp_path / "cli" / "archetypes_14.6_report.json").exists()


def test_cli_never_creates_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sqlite3"
    result = CliRunner().invoke(app, ["archetype-report", "--db", str(missing), "--out-dir", str(tmp_path / "o")])
    assert result.exit_code != 0
    assert not missing.exists()


def test_module_never_writes_sql_or_selects_identity() -> None:
    import re

    source = Path(ar.__file__).read_text()
    statements = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+(TABLE|INDEX)|ALTER\s+TABLE|"
                            r"DROP\s+(TABLE|INDEX)|TRUNCATE)\b", re.IGNORECASE)
    assert not statements.search(source)
    for identity in ("payload_json", "puuid", "riotId"):
        assert identity not in source


POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_postgres_report_runs_in_a_server_enforced_read_only_transaction() -> None:
    db = Database(POSTGRES_TEST_URL)
    try:
        for table in ("match_discoveries", "seed_samples", "ingest_runs", "traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        populate(db, build_payloads())
        db.commit()
    finally:
        db.close()
    with Database.open_existing(POSTGRES_TEST_URL) as ro:
        report, _, _ = ar.build_report(ro, balance_window=WINDOW)
        with pytest.raises(Exception):
            ro.execute("DELETE FROM matches")  # the session refuses writes
    assert report["read_only_connection"] == "postgres transaction_read_only=on"
    assert report["population"]["ranked_boards_eligible_for_grouping"] == 96
    assert (report["population"]["ranked_source_empty_participants"], report["population"]["ranked_unexpected_participants_without_units"]) == (1, 0)


# ---------------------------------------------------------------- workflow


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_is_manual_main_only_and_read_only() -> None:
    text = _text()
    assert "workflow_dispatch" in text
    for trigger in ("\npush:", "\n  push:", "pull_request", "schedule:", "workflow_run", "repository_dispatch"):
        assert trigger not in text
    preflight = text.index("Validate production configuration")
    assert preflight < text.index("refs/heads/main") < text.index("actions/checkout")
    assert '-z "${DATABASE_URL}"' in text and "postgres://*|postgresql://*" in text
    assert "group: read-only-archetype-report" in text and "contents: read" in text
    assert "timeout-minutes: 180" in text


def test_workflow_runs_only_the_archetype_report_and_uploads_it() -> None:
    text = _text()
    assert "tftlab archetype-report" in text
    for other in ("ingest-riot", "verify-riot", "discovery-smoke", "discovery-report", "patch-diagnostics",
                  "validate-live-data", "refresh-game-art", "experiment-"):
        assert other not in text
    assert "actions/upload-artifact" in text and "git push" not in text and "git commit" not in text
    assert "RIOT_API_KEY" not in text
    assert "continue-on-error" not in text and "retry" not in text.lower()


def test_workflow_validates_the_window_input_before_checkout_and_passes_it_via_env() -> None:
    text = _text()
    assert text.index("balance_window must look like") < text.index("actions/checkout")
    run_line = next(line for line in text.splitlines() if line.strip().startswith("run: tftlab archetype-report"))
    assert "${{" not in run_line and '"$BALANCE_WINDOW"' in run_line


def test_workflow_installs_the_postgres_driver() -> None:
    install = next(line for line in _text().splitlines() if "pip install" in line)
    assert '".[postgres]"' in install


def test_workflow_never_prints_the_database_url() -> None:
    for line in _text().splitlines():
        if "echo" in line.lower():
            assert "$DATABASE_URL" not in line and "${DATABASE_URL}" not in line


# ---------------------------------------------------------------- DB lifetime, progress, partial results


def test_inputs_are_fully_materialized_and_analysis_needs_no_database(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pickle

    expected = run(store)
    with Database.open_existing(store) as db:
        inputs = ar.load_inputs(db, WINDOW)
    pickle.dumps(inputs)  # plain data: a live connection or cursor could not be pickled

    def no_database(*args, **kwargs):
        raise AssertionError("analysis touched the database after it was closed")

    for name in ("query_all", "query_one", "execute", "executemany"):
        monkeypatch.setattr(Database, name, no_database)
    got = ar.analyze(inputs.boards, inputs.population, inputs.access, ar.ArchetypeConfig())
    assert json.dumps(got[0], sort_keys=True, default=str) == json.dumps(expected[0], sort_keys=True, default=str)
    assert got[1] == expected[1] and got[2] == expected[2]


def test_cli_closes_the_database_before_any_analysis(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    original_close, original_analyze = Database.close, ar.analyze

    def close(self):
        events.append("database closed")
        return original_close(self)

    def analyze(*args, **kwargs):
        events.append("analysis started")
        return original_analyze(*args, **kwargs)

    monkeypatch.setattr(Database, "close", close)
    monkeypatch.setattr(ar, "analyze", analyze)
    result = CliRunner().invoke(app, ["archetype-report", "--db", str(store), "--balance-window", WINDOW,
                                      "--out-dir", str(tmp_path / "o")])
    assert result.exit_code == 0, result.output
    assert events == ["database closed", "analysis started"]


def _cli(store: Path, out: Path):
    return CliRunner().invoke(app, ["archetype-report", "--db", str(store), "--balance-window", WINDOW, "--out-dir", str(out)])


def test_progress_and_timing_lines_are_printed_and_logged(store: Path, tmp_path: Path) -> None:
    out = tmp_path / "o"
    result = _cli(store, out)
    assert result.exit_code == 0, result.output
    expected = ["database connection established", "read-only verified: sqlite mode=ro", "population loading started",
                "population loading completed", "database connection closed", "normalization completed",
                *[f"{s.name}: {what}" for s in ar.STRATEGIES for what in (
                    "started", "leader pass completed", "refine 1 completed", "summaries/diagnostics started",
                    "summaries/diagnostics completed", "completed,")],
                "report generation completed", "final report written"]
    progress_lines = [line for line in result.output.splitlines() if line.startswith("[progress +")]
    for phrase in expected:
        assert any(phrase in line for line in progress_lines), phrase
    assert result.output.rstrip().splitlines()[-4] == "RESEARCH REPORT COMPLETE"
    log = (out / "archetypes_14.6_progress.log").read_text()
    assert log.splitlines() == progress_lines
    for secret in ("PUUID-SECRET", "SecretName", "M000", "NORMAL1", "EMPTYR"):
        assert secret not in log


def test_success_leaves_only_the_final_report_and_no_partial_label(store: Path, tmp_path: Path) -> None:
    out = tmp_path / "o"
    assert _cli(store, out).exit_code == 0
    assert not (out / "partial").exists()
    final = (out / "archetypes_14.6_report.md").read_text()
    assert ar.PARTIAL_LABEL not in final
    assert sorted(p.name for p in out.iterdir()) == ["archetypes_14.6_membership.csv", "archetypes_14.6_progress.log",
                                                     "archetypes_14.6_report.json", "archetypes_14.6_report.md"]


def _fail_at(monkeypatch: pytest.MonkeyPatch, strategy_name: str) -> None:
    original = ar.cluster

    def cluster(boards, strategy, config, progress=None):
        if strategy.name == strategy_name:
            raise RuntimeError(f"simulated failure in {strategy_name}")
        return original(boards, strategy, config, progress)

    monkeypatch.setattr(ar, "cluster", cluster)


def _status(out: Path) -> dict:
    return json.loads((out / "partial" / "00_STATUS.json").read_text())


def test_population_survives_a_failure_in_the_first_strategy(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_at(monkeypatch, "A_structural_baseline")
    out = tmp_path / "o"
    result = _cli(store, out)
    assert result.exit_code != 0
    status = _status(out)
    assert status["status"] == ar.PARTIAL_LABEL
    assert status["completed_phases"] == ["population"]
    assert status["pending_phases"] == [*(s.name for s in ar.STRATEGIES), "closing"]
    population = json.loads((out / "partial" / "01_population.json").read_text())
    assert population["status"] == ar.PARTIAL_LABEL
    assert population["data"]["population"]["ranked_boards_eligible_for_grouping"] == 96
    assert (out / "partial" / "01_population.md").read_text().startswith(f"# {ar.PARTIAL_LABEL}")
    assert not (out / "archetypes_14.6_report.md").exists() and not (out / "archetypes_14.6_report.json").exists()
    assert "RESEARCH REPORT COMPLETE" not in result.output.splitlines()  # the marker line, not the intro note
    assert "database connection closed" in (out / "archetypes_14.6_progress.log").read_text()


def test_finished_strategies_survive_a_later_strategy_failure(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_at(monkeypatch, "C_structure_aware")
    out = tmp_path / "o"
    result = _cli(store, out)
    assert result.exit_code != 0
    status = _status(out)
    assert status["completed_phases"] == ["population", "A_structural_baseline", "B_flex_tolerant"]
    assert status["pending_phases"] == ["C_structure_aware", "closing"]
    names = sorted(p.name for p in (out / "partial").iterdir())
    assert names == ["00_STATUS.json", "00_STATUS.md", "01_population.json", "01_population.md",
                     "02_A_structural_baseline.json", "02_A_structural_baseline.md",
                     "03_B_flex_tolerant.json", "03_B_flex_tolerant.md"]
    for name in names:
        text = (out / "partial" / name).read_text()
        assert ar.PARTIAL_LABEL in text, name  # every partial file says so
    a = json.loads((out / "partial" / "02_A_structural_baseline.json").read_text())["data"]
    assert a["boards_considered"] == 96 and len(a["groups"]) >= 1  # "groups": the group summaries
    assert "## Strategy B. flex-tolerant structural" in (out / "partial" / "03_B_flex_tolerant.md").read_text()
    assert "## Strategy B. flex-tolerant structural" in result.output  # streamed to the log before the failure
    assert not list(out.glob("archetypes_14.6_report.*")) and not (out / "archetypes_14.6_membership.csv").exists()


def test_a_failed_run_never_leaves_an_older_complete_report_behind(store: Path, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "o"
    assert _cli(store, out).exit_code == 0
    assert (out / "archetypes_14.6_report.md").exists()
    _fail_at(monkeypatch, "B_flex_tolerant")
    assert _cli(store, out).exit_code != 0
    assert not (out / "archetypes_14.6_report.md").exists()
    assert _status(out)["completed_phases"] == ["population", "A_structural_baseline"]


def test_streamed_sections_add_up_to_the_final_report(store: Path) -> None:
    sections: list[tuple[str, list[str]]] = []
    with Database.open_existing(store) as db:
        inputs = ar.load_inputs(db, WINDOW)
    report, markdown, _ = ar.analyze(inputs.boards, inputs.population, inputs.access, ar.ArchetypeConfig(),
                                     on_section=lambda phase, data, lines: sections.append((phase, list(lines))))
    assert [phase for phase, _ in sections] == list(ar.PHASES)
    assert [line for _, lines in sections for line in lines] == markdown


def test_workflow_uploads_artifacts_even_after_a_failed_or_timed_out_analysis() -> None:
    text = _text()
    analysis = text.index("name: Run archetype research report (read-only)")
    upload = text.index("name: Upload report artifact")
    assert analysis < upload
    step = text[upload:]
    assert "if: ${{ always() }}" in step and "path: archetype-report/" in step
    assert "timeout-minutes: 170" in text[analysis:upload]  # inside the 180-minute job budget, leaving time to upload
    assert "continue-on-error" not in text  # a failed analysis still fails the job


def test_runtime_benchmark_runs_the_unchanged_harness_and_restores_it() -> None:
    import importlib.util

    path = Path(__file__).parent.parent / "scripts" / "benchmarks" / "archetype_runtime_benchmark.py"
    spec = importlib.util.spec_from_file_location("archetype_runtime_benchmark", path)
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    before = ar.STRATEGIES
    result = bench.run(120, "high", ["A_structural_baseline"])
    assert ar.STRATEGIES is before  # the benchmark's temporary strategy subset is undone
    assert result["diversity"]["boards"] == 120 and result["diversity"]["distinct_share"] > 0.5
    a = result["strategies"]["A_structural_baseline"]
    assert {"leader_pass_s", "total_s", "leader_pass_groups", "groups", "assigned", "ungrouped"} <= set(a)
