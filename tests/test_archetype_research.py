"""Board-archetype research harness (tftlab.archetype_research, `tftlab archetype-report`).

Synthetic fixtures only: these tests check the harness's mechanics (read-only
access, determinism, similarity, flex tolerance, shell separation, canonical
names, workflow safety). They say nothing about whether real TFT boards form
good archetypes -- that is what the harness exists to find out.
"""

from __future__ import annotations

import csv
import dataclasses
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
    assert names == ["A_structural_baseline", "B_flex_tolerant", "C_structure_aware", "B_S2_experimental",
                     "C_S2_experimental"]
    a, b, c, bs2, cs2 = ar.STRATEGIES
    assert (a.weighted, a.trait_share, a.merge) == (False, 0.0, None)
    assert (b.weighted, b.trait_share, b.merge) == (False, 0.0, "structural")
    assert (c.weighted, c.trait_share, c.merge) == (True, 0.25, "structure_aware")
    # the controls keep the strict rule and form their own variants
    assert {(s.similarity_rule, s.variants_from) for s in (a, b, c)} == {("all_boards", None)}
    # the experimental strategies differ from their controls ONLY in the merged-result similarity rule
    for exp, control in ((bs2, b), (cs2, c)):
        assert exp.similarity_rule == "s2" and exp.variants_from == control.name
        assert (exp.weighted, exp.trait_share, exp.merge) == (control.weighted, control.trait_share, control.merge)


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
    assert sum(line.startswith("- merge_checks: ") for line in markdown) == 5  # A, B, C and the two experimental


# ---------------------------------------------------------------- merge diagnostics (validation run #3; measurement only)

TAU = ar.ArchetypeConfig().tau
SHELL8 = list(range(8))


def diagnosed(boards, variant, strategy=B_STRATEGY, config=None):
    config = config or ar.ArchetypeConfig()
    collector = ar.MergeDiagnostics(config.tau)
    result = ar.merge_variants(boards, variant, strategy, config, diagnostics=collector)
    return result, collector


def knife_edge(extra_units: list[list[int]]) -> tuple[list[ar.Board], dict[int, int]]:
    """Variant 0: nine 8-unit shell boards + one borderline board X = shell[0:6] + two rare units, 62/98 = 0.633
    similar to its own profile. Variant 1: six boards, the shell plus the given extra unit(s) (cycled). The merged
    profile of 16 boards leaves X at 98/164 = 0.5976 < tau and every other board >= 0.9."""
    specs = [(SHELL8, 9), ([0, 1, 2, 3, 4, 5, 8, 9], 1)] + [(SHELL8 + extra, 6 // len(extra_units)) for extra in extra_units]
    boards, variant = variants(*specs)
    return boards, {k: 0 if v <= 1 else 1 for k, v in variant.items()}


def test_diagnostics_never_change_merge_decisions() -> None:
    """Same groups, merges, log and rejection counts with or without the collector, for both merging strategies."""
    cases = [variants(*DRIFT, (SHELL8 + [8], 3), itemized={0, 8}), knife_edge([[11]]), knife_edge([[11], [12]])]
    for boards, variant in cases:
        for strategy in (B_STRATEGY, C_STRATEGY):
            plain = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig())
            (instrumented, collector) = diagnosed(boards, variant, strategy)
            assert instrumented == plain, strategy.name
            summary = collector.summary()
            _, _, checks = plain
            assert summary["rejected_similarity_attempts"] == checks["rejected_similarity"], strategy.name
            assert summary["accepted_attempts"] == len(plain[1]), strategy.name


def test_cluster_groups_match_an_uninstrumented_merge(store: Path) -> None:
    with Database.open_existing(store) as db:
        boards = ar.load_inputs(db, WINDOW).boards
    for strategy in (B_STRATEGY, C_STRATEGY):
        grouping = ar.cluster(boards, strategy, ar.ArchetypeConfig())
        members = [b for b in boards if b.obs in grouping.variant]
        to_group, log, checks = ar.merge_variants(members, grouping.variant, strategy, ar.ArchetypeConfig())
        assert grouping.group == {k: to_group[v] for k, v in grouping.variant.items()}, strategy.name
        assert (grouping.merges, grouping.merge_checks) == (len(log), checks), strategy.name
        assert grouping.merge_diagnostics["attempts_evaluated"] == grouping.merges + checks["rejected_similarity"]


def test_one_board_knife_edge_rejection_is_measured_exactly() -> None:
    boards, variant = knife_edge([[11]])
    (_, _, checks), collector = diagnosed(boards, variant)
    assert checks["rejected_similarity"] == 1 and len(collector.rows) == 1
    r = collector.rows[0]
    assert (r["outcome"], r["a_boards"], r["b_boards"], r["merged_boards"]) == ("rejected_similarity", 10, 6, 16)
    assert (r["below_tau"], r["below_tau_from_a"], r["below_tau_from_b"]) == (1, 1, 0)
    assert r["below_tau_share"] == pytest.approx(1 / 16)
    w = r["weakest"]
    assert (w["side"], w["observation"], w["tau"]) == ("a", 9, TAU)
    assert w["pre_merge_similarity"] == pytest.approx(62 / 98)
    assert w["post_merge_similarity"] == pytest.approx(98 / 164)
    assert w["delta"] == pytest.approx(98 / 164 - 62 / 98)
    assert w["shortfall_below_tau"] == pytest.approx(TAU - 98 / 164)
    assert w["pre_margin_above_tau"] == pytest.approx(62 / 98 - TAU)
    assert r["shortfall_max"] == r["shortfall_median"] == pytest.approx(TAU - 98 / 164)
    assert r["post"]["median"] > 0.9 and r["post"]["min"] == pytest.approx(98 / 164)
    assert (r["b_only"], r["a_only"], r["splash_exception_applies"]) == ([IDS[11]], [], False)  # B: no C exception
    agg = collector.summary()["rejected"]
    assert agg["below_tau_count_buckets"]["exactly 1"] == 1
    assert agg["weakest_distance_from_tau_buckets"]["(0.001, 0.0025]"] == 1  # 0.0024: a knife-edge miss
    assert agg["weakest_pre_merge_margin_buckets"]["(0.02, 0.05]"] == 1  # 0.0327 above tau before the merge
    assert agg["weakest_degradation_buckets"]["drop (0.02, 0.05]"] == 1  # 0.6327 -> 0.5976
    assert agg["failing_boards_by_side"] == {"larger side only": 1, "smaller side only": 0, "both sides": 0}
    assert agg["merged_size_bands"]["10-29"]["exactly 1"] == 1


def test_identical_core_rejection_exposes_the_single_failing_board() -> None:
    """Variant 1 alternates two different extra units, so both cores are exactly the 8-unit shell (overlap 1.0), yet
    the one borderline board of variant 0 still falls below tau."""
    boards, variant = knife_edge([[11], [12]])
    (_, _, checks), collector = diagnosed(boards, variant)
    assert checks["rejected_similarity"] == 1
    r = collector.rows[0]
    assert r["identical_cores"] and r["core_overlap"] == 1.0 and r["core_a"] == r["core_b"] == r["merged_core"]
    assert r["below_tau"] == 1 and r["weakest"]["post_merge_similarity"] == pytest.approx(98 / 164)
    summary = collector.summary()
    assert summary["rejected"]["core_overlap_buckets"]["identical cores"] == 1
    assert {s["reason"] for s in summary["sample"]} >= {"smallest threshold miss"}
    assert all(s["attempt"] == 0 for s in summary["sample"])  # one attempt, sampled once (deduplicated)


def test_material_failure_is_measured_across_all_failing_boards() -> None:
    """DRIFT: the eroded A+B+C group tentatively absorbs D; all four D boards fall to 5.75 / 10.25 = 0.561."""
    boards, variant = variants(*DRIFT)
    _, collector = diagnosed(boards, variant)
    [r] = [r for r in collector.rows if r["outcome"] == "rejected_similarity" and r["b_root_variant"] == 3]
    assert (r["a_boards"], r["b_boards"], r["below_tau"], r["below_tau_from_b"]) == (12, 4, 4, 4)
    assert r["below_tau_share"] == pytest.approx(0.25)
    assert r["shortfall_max"] == r["shortfall_mean"] == pytest.approx(TAU - 5.75 / 10.25)
    assert r["weakest"]["pre_merge_similarity"] == pytest.approx(1.0) and r["weakest"]["side"] == "b"
    assert r["weakest"]["delta"] == pytest.approx(5.75 / 10.25 - 1.0)
    agg = collector.summary()["rejected"]
    assert agg["below_tau_count_buckets"]["2+, > 10%"] == 1
    assert agg["weakest_distance_from_tau_buckets"]["(0.02, 0.05]"] == 1  # 0.039 below tau
    assert agg["weakest_degradation_buckets"]["drop > 0.1"] == 1
    assert agg["failing_boards_by_side"]["smaller side only"] == 1


def test_accepted_merges_are_measured_without_changing_acceptance() -> None:
    boards, variant = variants((SHELL8, 4), ([0, 1, 2, 3, 4, 5, 6, 8], 4))
    (to_group, log, checks), collector = diagnosed(boards, variant)
    assert len(log) == 1 and to_group[0] == to_group[1] and checks["rejected_similarity"] == 0
    [r] = collector.rows
    assert (r["outcome"], r["below_tau"], r["merged_boards"]) == ("accepted", 0, 8)
    assert r["weakest"]["post_merge_similarity"] == pytest.approx(7.5 / 8.5)
    assert r["weakest"]["pre_merge_similarity"] == pytest.approx(1.0)
    assert r["weakest"]["shortfall_below_tau"] == pytest.approx(TAU - 7.5 / 8.5)  # negative: above tau
    summary = collector.summary()
    assert (summary["accepted_attempts"], summary["rejected_similarity_attempts"]) == (1, 0)
    assert summary["accepted"]["below_tau_count_buckets"]["0"] == 1
    assert summary["accepted"]["weakest_distance_from_tau_buckets"]["> 0.1"] == 1  # 0.28 above tau
    assert summary["rejected"]["attempts"] == 0 and summary["rejected"]["quantiles"]["merged_boards"] is None
    assert [s["reason"] for s in summary["sample"]] == ["accepted: closest to tau"]


def test_diagnostics_are_deterministic_and_independent_of_board_order() -> None:
    boards, variant = variants(*DRIFT, (SHELL8 + [8], 3), ([0, 1, 2, 3, 4, 5, 8, 9], 2), itemized={0, 8})
    for strategy in (B_STRATEGY, C_STRATEGY):
        first = diagnosed(boards, variant, strategy)[1].summary()
        again = diagnosed(boards, variant, strategy)[1].summary()
        reordered = diagnosed(list(reversed(boards)), variant, strategy)[1].summary()
        assert first == again == reordered, strategy.name


def test_report_carries_bounded_anonymous_merge_diagnostics(store: Path) -> None:
    report, markdown, _ = run(store)
    assert report["strategies"]["A_structural_baseline"]["merge_diagnostics"] == {}
    for strategy in ("B_flex_tolerant", "C_structure_aware"):
        d = report["strategies"][strategy]["merge_diagnostics"]
        assert d["attempts_evaluated"] == d["accepted_attempts"] + d["rejected_similarity_attempts"]
        assert d["accepted_attempts"] == report["strategies"][strategy]["merges"]
        assert d["rejected_similarity_attempts"] == report["strategies"][strategy]["merge_checks"]["rejected_similarity"]
        assert len(d["sample"]) <= 10 * ar.DIAGNOSTIC_SAMPLE_PER_REASON
        assert len({s["attempt"] for s in d["sample"]}) == len(d["sample"])
        text = json.dumps(d)
        for secret in ("PUUID", "SecretName", "M000", "M011", "NORMAL1", "EMPTYR", "match_id", "puuid"):
            assert secret not in text, (strategy, secret)
    assert sum(line.startswith("### Merge-result similarity diagnostics") for line in markdown) == 4
    assert report["strategies"]["B_flex_tolerant"]["merge_diagnostics"]["attempts_evaluated"] >= 1


def test_diagnostic_buckets_are_fixed_and_exhaustive() -> None:
    assert ar.DIAGNOSTIC_EDGES == (0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1)
    assert ar._magnitude_bucket(0.0001) == "<= 0.001" and ar._magnitude_bucket(0.001) == "<= 0.001"
    assert ar._magnitude_bucket(0.0011) == "(0.001, 0.0025]" and ar._magnitude_bucket(0.15) == "> 0.1"
    assert [ar._below_tau_bucket(k, n) for k, n in ((0, 5), (1, 5), (2, 400), (2, 100), (6, 100), (11, 100), (2, 4))] == [
        "0", "exactly 1", "2+, <= 1%", "2+, (1%, 5%]", "2+, (5%, 10%]", "2+, > 10%", "2+, > 10%"]



# ---------------------------------------------------------------- shadow merge rules (report only; validation run #5 design)


def shadow_row(larger: list[float], smaller: list[float], *, overlap: float = 0.9, identical: bool = False,
               splash: bool = False, tau: float = TAU) -> dict:
    """One diagnostics row recorded by the real collector from chosen post-merge similarities: side a = the larger
    side (lower id), side b = the smaller side. Pre-merge similarities are irrelevant to S0-S4 and set to 1.0."""
    post = {i: v for i, v in enumerate(larger + smaller)}
    side = {i: "a" if i < len(larger) else "b" for i in post}
    core_a = {"x", "y"}
    core_b = core_a if identical else {"x", "z"}
    collector = ar.MergeDiagnostics(tau)
    collector.record(accepted=min(post.values()) >= tau, a=0, b=1, variants_a=1, variants_b=1, core_a=core_a,
                     core_b=core_b, merged_core=core_a & core_b, overlap=1.0 if identical else overlap, splash=splash,
                     pre={k: 1.0 for k in post}, post=post, side=side)
    return collector.rows[0]


def decisions(row: dict) -> dict[str, bool]:
    return {c: all(v.values()) for c, v in ar.shadow_conditions(row, TAU).items()}


def test_shadow_s0_is_the_actual_similarity_decision(store: Path) -> None:
    cases = [variants(*DRIFT, (SHELL8 + [8], 3), itemized={0, 8}), knife_edge([[11]]), knife_edge([[11], [12]]),
             variants((SHELL8, 4), ([0, 1, 2, 3, 4, 5, 6, 8], 4))]
    for boards, variant in cases:
        for strategy in (B_STRATEGY, C_STRATEGY):
            (_, log, checks), collector = diagnosed(boards, variant, strategy)
            shadow = collector.summary()["shadow"]
            assert shadow["s0_mismatches_with_actual_decision"] == 0, strategy.name
            s0 = shadow["candidates"]["S0"]
            assert (s0["shadow_accepted"], s0["recovered"], s0["lost_vs_actual"]) == (len(log), 0, 0), strategy.name
            assert s0["shadow_rejected"] == checks["rejected_similarity"], strategy.name
    report, _, _ = run(store)
    for strategy in ("B_flex_tolerant", "C_structure_aware"):
        assert report["strategies"][strategy]["merge_diagnostics"]["shadow"]["s0_mismatches_with_actual_decision"] == 0


def test_side_fields_follow_board_counts_with_a_deterministic_tie() -> None:
    r = shadow_row([0.8] * 5, [0.55, 0.7])
    assert (r["larger_side"], r["larger_side_boards"], r["smaller_side_boards"]) == ("a", 5, 2)
    assert (r["larger_side_below_tau"], r["smaller_side_below_tau"], r["smaller_side_below_tau_share"]) == (0, 1, 0.5)
    assert (r["larger_side_min_post"], r["smaller_side_min_post"]) == (0.8, 0.55)
    tie = shadow_row([0.8, 0.8], [0.55, 0.7])
    assert tie["larger_side"] == "a" and tie["smaller_side_below_tau"] == 1  # equal sizes: side a (lower id)


def test_denominator_counterexample_s1_accepts_s2_and_s3_reject() -> None:
    """A large established side plus a tiny variant whose EVERY board fails: 2/202 < 1% of the merged group, all
    boards above the tau - 0.10 floor -- the Run #4 Ahri/Sett/Morgana + Yorick shape."""
    r = shadow_row([0.8] * 200, [0.55, 0.52], overlap=0.56)
    assert r["below_tau_share"] <= 0.01 and r["smaller_side_below_tau"] == r["smaller_side_boards"]
    d = decisions(r)
    assert (d["S0"], d["S1"], d["S2"], d["S3"]) == (False, True, False, False)
    assert d["S4"]  # S4 has no side logic by design: its bulk (p10, median) is healthy
    assert set(ar._dangers(r, TAU)) == {ar.DANGER_LABELS[0], ar.DANGER_LABELS[1], ar.DANGER_LABELS[2]}


def test_legitimate_bounded_family_tail_is_accepted_by_s2_and_s3() -> None:
    r = shadow_row([0.85] * 300, [0.57, 0.56] + [0.8] * 28, overlap=0.889)  # 2/30 = 6.7% smaller, 2/330 = 0.6% merged
    d = decisions(r)
    assert (d["S0"], d["S1"], d["S2"], d["S3"], d["S4"]) == (False, True, True, True, True)
    assert ar._dangers(r, TAU) == [] and ar.FAMILY_LABELS[0] in ar._families(r, TAU)
    # one larger-side board below tau is enough for S2/S3 to reject (D: both sides)
    both = shadow_row([0.59] + [0.85] * 299, [0.57, 0.56] + [0.8] * 28, overlap=0.889)
    assert decisions(both)["S2"] is False and ar.DANGER_LABELS[3] in ar._dangers(both, TAU)
    # smaller-side limit: exactly 10% (3/30) passes; 13.3% (4/30) fails even though only 4/430 < 1% of the merged group
    assert decisions(shadow_row([0.85] * 300, [0.57] * 3 + [0.8] * 27, overlap=0.889))["S2"] is True  # exactly 10%
    assert decisions(shadow_row([0.85] * 400, [0.57] * 4 + [0.8] * 26, overlap=0.889))["S2"] is False  # 13.3%


@pytest.mark.parametrize(("overlap", "identical", "splash", "s3"), [
    (0.80, False, False, True), (0.7999, False, False, False), (0.5, True, False, True), (0.7, False, True, True),
])
def test_s3_structural_gate(overlap: float, identical: bool, splash: bool, s3: bool) -> None:
    r = shadow_row([0.85] * 300, [0.57] + [0.8] * 29, overlap=overlap, identical=identical, splash=splash)
    d = decisions(r)
    assert d["S2"] is True and d["S3"] is s3


@pytest.mark.parametrize(("larger", "smaller", "s4"), [
    ([0.9] * 18, [0.52, 0.8], True),  # p10 0.8 >= tau, median 0.9 >= 0.65, min 0.52 >= 0.5
    ([0.9] * 16, [0.55, 0.55, 0.55, 0.8], False),  # p10 = 0.55 < tau
    ([0.62] * 18, [0.61, 0.62], False),  # median 0.62 < tau + 0.05
    ([0.9] * 18, [0.49, 0.8], False),  # floor: 0.49 < tau - 0.10
    ([0.65] * 18, [0.6, 0.65], True),  # boundaries: p10 0.65, median exactly tau + 0.05
])
def test_s4_lower_tail_rule(larger: list[float], smaller: list[float], s4: bool) -> None:
    assert decisions(shadow_row(larger, smaller))["S4"] is s4


def test_danger_and_family_counters_and_difference_sets() -> None:
    rows = [
        shadow_row([0.8] * 200, [0.55, 0.52], overlap=0.56),  # S1 only: whole smaller side fails, low overlap
        shadow_row([0.85] * 300, [0.57, 0.56] + [0.8] * 28, overlap=0.889),  # S1, S2, S3, S4
        shadow_row([0.85] * 300, [0.57, 0.56] + [0.8] * 28, overlap=0.7),  # S1, S2, S4 but not S3 (gate)
        shadow_row([0.8] * 20, [0.8] * 20),  # actually accepted by every candidate
        shadow_row([0.45] + [0.9] * 30, [0.9] * 3),  # below the floor: nobody accepts (E)
    ]
    for i, r in enumerate(rows):
        r["attempt"] = i
    sh = ar.shadow_evaluation(rows, TAU)
    c = sh["candidates"]
    assert sh["s0_mismatches_with_actual_decision"] == 0
    assert [c[k]["recovered"] for k in ar.SHADOW_CANDIDATES] == [0, 3, 2, 1, 3]
    assert [c[k]["shadow_accepted"] for k in ar.SHADOW_CANDIDATES] == [1, 4, 3, 2, 4]
    s1 = c["S1"]["recovery_set"]
    assert s1["dangers"][ar.DANGER_LABELS[0]] == 1 and s1["dangers"][ar.DANGER_LABELS[1]] == 1
    assert s1["dangers"][ar.DANGER_LABELS[2]] == 1 and s1["dangers"][ar.DANGER_LABELS[4]] == 0
    assert s1["smaller_side_share_buckets"] == {"0%": 0, "(0, 10%]": 2, "(10%, 25%]": 0, "(25%, 50%]": 0,
                                                "(50%, 100%)": 0, "100%": 1}
    assert c["S3"]["recovery_set"]["families"][ar.FAMILY_LABELS[0]] == 1
    assert c["S3"]["recovery_set"]["dangers"] == dict.fromkeys(ar.DANGER_LABELS, 0)
    assert ar.DANGER_LABELS[4] in ar._dangers(rows[4], TAU)
    diff = sh["differences"]
    assert diff["S1 recovered, S2 did not"]["attempts"] == 1
    assert diff["S1 recovered, S2 did not"]["failed_conditions_of_S2"] == {"smaller side: <= 10% below tau": 1}
    assert diff["S2 recovered, S3 did not"]["failed_conditions_of_S3"] == {
        "core overlap >= 0.8, identical cores or C splash exception": 1}
    assert diff["S4 recovered, S3 did not"]["attempts"] == 2 and diff["S3 recovered, S4 did not"]["attempts"] == 0
    assert sh["recovery_overlap"]["S1"]["S4"] == 3
    sample = {s["attempt"]: s["reason"] for s in c["S1"]["sample"]}
    assert sample[0].startswith("DANGER A") or sample[0] == "core overlap < 0.6"
    assert all(s["attempt"] != 3 for k in ar.SHADOW_CANDIDATES for s in c[k]["sample"])  # samples hold recoveries only


def test_report_prints_the_shadow_evaluation(store: Path) -> None:
    report, markdown, _ = run(store)
    for strategy in ("B_flex_tolerant", "C_structure_aware"):
        sh = report["strategies"][strategy]["merge_diagnostics"]["shadow"]
        assert set(sh["candidates"]) == set(ar.SHADOW_CANDIDATES)
        text = json.dumps(sh)
        for secret in ("PUUID", "SecretName", "M000", "M011", "NORMAL1", "EMPTYR", "match_id", "puuid"):
            assert secret not in text, (strategy, secret)
    assert sum(line.startswith("### Shadow merge-rule evaluation (REPORT ONLY") for line in markdown) == 2
    assert sum(line.startswith("| S0 |") for line in markdown) == 2
    assert any(line.startswith("- S0 sanity check: 0 attempts") for line in markdown)



# ---------------------------------------------------------------- experimental S2 strategies (validation run #6 design)

B_S2, C_S2 = ar.STRATEGIES[3], ar.STRATEGIES[4]


def boards_by_variant(*groups: list[list[int]]) -> tuple[list[ar.Board], dict[int, int]]:
    """Variant v = groups[v], one board per unit-index list (so a variant can mix a clean shell and tail boards)."""
    specs = [(units, 1) for group in groups for units in group]
    owner = [v for v, group in enumerate(groups) for _ in group]
    boards, variant = variants(*specs)
    return boards, {k: owner[k] for k in variant}


def final_sets(boards, variant, strategy, config=None) -> tuple[list[list[int]], dict[str, int], ar.MergeDiagnostics]:
    collector = ar.MergeDiagnostics(TAU)
    to_group, _, checks = ar.merge_variants(boards, variant, strategy, config or ar.ArchetypeConfig(), diagnostics=collector)
    by_group: dict[int, list[int]] = {}
    for v in sorted({variant[b.obs] for b in boards}):
        by_group.setdefault(to_group[v], []).append(v)
    return sorted(by_group.values()), checks, collector


def tail_board(i: int, extras: int = 6) -> list[int]:
    """The shell + unit 8 + `extras` units no other board has: a real but poorly fitting member."""
    return SHELL8 + [8] + list(range(20 + extras * i, 20 + extras * (i + 1)))


LARGE = [SHELL8] * 300  # a large, perfectly clean established variant
TAILED = [SHELL8 + [8]] * 27 + [tail_board(i) for i in range(3)]  # 3/30 = 10% of this side, 3/330 < 1% merged
CLEAN_T = [SHELL8 + [10]] * 20


def test_s2_conditions_exact_boundaries() -> None:
    def ok(**kw) -> bool:
        base = dict(larger_below=0, smaller_below=0, smaller_boards=30, merged_below=0, merged_boards=330,
                    min_post=0.7, tau=TAU)
        return all(ar.s2_conditions(**{**base, **kw}).values())
    assert ok() and not ok(larger_below=1, merged_below=1)  # zero larger-side failures required
    assert ok(smaller_below=3, merged_below=3)  # exactly 10% of the smaller side
    assert not ok(smaller_below=4, merged_below=4)  # 13.3%
    assert ok(smaller_below=3, smaller_boards=300, merged_below=3, merged_boards=300)  # exactly 1% merged
    assert not ok(smaller_below=4, smaller_boards=300, merged_below=4, merged_boards=300)  # 1.33% merged
    assert ok(min_post=TAU - 0.10) and ok(min_post=0.5)  # exactly tau - 0.10 (0.5 in floating point)
    assert not ok(min_post=math.nextafter(TAU - 0.10, 0.0))
    assert ar.SHADOW_SMALLER_TAIL == 10 and ar.SHADOW_MERGED_TAIL == 100 and ar.SHADOW_FLOOR == 0.10  # frozen


def test_experimental_strategies_differ_only_in_the_similarity_rule_and_controls_stay_strict() -> None:
    boards, variant = boards_by_variant(LARGE, TAILED)
    for control, exp in ((B_STRATEGY, B_S2), (C_STRATEGY, C_S2)):
        same = dataclasses.replace(exp, name=control.name, label=control.label, description=control.description,
                                   similarity_rule=control.similarity_rule, variants_from=control.variants_from)
        assert same == control  # every other field (weights, traits, merge mode) is the control's
    strict, checks, _ = final_sets(boards, variant, B_STRATEGY)
    assert strict == [[0], [1]] and checks["rejected_similarity"] == 1  # the control B stays strict
    with pytest.raises(ValueError):
        ar.merge_variants(boards, variant, dataclasses.replace(B_STRATEGY, similarity_rule="no-such-rule"),
                          ar.ArchetypeConfig())


def test_s2_rejects_the_denominator_counterexample() -> None:
    """200 clean boards + a 2-board variant with the same core whose every board fails (0.57, above the floor):
    2/202 < 1% of the merged group, but 100% of the smaller side."""
    boards, variant = boards_by_variant([SHELL8] * 200, [SHELL8 + list(range(20, 26)), SHELL8 + list(range(26, 32))])
    groups, checks, collector = final_sets(boards, variant, B_S2)
    assert groups == [[0], [1]] and checks["rejected_similarity"] == 1
    [r] = collector.rows
    assert (r["smaller_side_below_tau"], r["smaller_side_boards"], r["larger_side_below_tau"]) == (2, 2, 0)
    assert 100 * r["below_tau"] <= r["merged_boards"] and r["post"]["min"] >= TAU - 0.10
    d = decisions(r)
    assert d["S1"] and not d["S2"]  # the merged-share-only rule would have let it in


def test_s2_accepts_a_legitimate_bounded_tail_and_the_merge_really_happens() -> None:
    boards, variant = boards_by_variant(LARGE, TAILED)
    groups, checks, collector = final_sets(boards, variant, B_S2)
    assert groups == [[0, 1]] and checks["rejected_similarity"] == 0
    [r] = collector.rows
    assert r["outcome"] == "accepted" and (r["below_tau"], r["larger_side_below_tau"], r["smaller_side_below_tau"]) == (3, 0, 3)
    assert TAU - 0.10 <= r["post"]["min"] < TAU
    summary = collector.summary("S2")
    assert summary["decision_rule_mismatches"] == 0 and "shadow" not in summary
    t = summary["trajectory"]
    assert (t["accepted_merges"], t["with_any_board_below_tau"]) == (1, 1)
    [m] = t["merges"]
    assert (m["merged_boards"], m["larger_side_boards"], m["smaller_side_boards"], m["variants_after_merge"]) == (330, 300, 30, 2)
    assert (m["below_tau"], m["smaller_side_below_tau"], m["smaller_side_below_tau_share"]) == (3, 3, 0.1)
    assert t["by_variants_after_merge"]["2"]["accepted_merges"] == 1


def test_c_s2_uses_the_s2_rule_with_c_similarity() -> None:
    """C's similarity blends traits, so its tail boards need more foreign units to fall below tau."""
    boards, variant = boards_by_variant(LARGE, [SHELL8 + [8]] * 27 + [tail_board(i, extras=12) for i in range(3)])
    assert final_sets(boards, variant, C_STRATEGY)[0] == [[0], [1]]
    groups, _, collector = final_sets(boards, variant, C_S2)
    assert groups == [[0, 1]] and collector.rows[0]["smaller_side_below_tau"] == 3


def test_s2_is_recursive_not_a_replay_of_the_control_trajectory() -> None:
    """L (300 clean) ~ S (27 clean + 3 tail boards) ~ T (20 clean); both pairs have core overlap 0.889 and (L, S) is
    judged first (lower ids). The control rejects L+S and then merges L+T. B-S2 accepts L+S, so the three tail boards
    now belong to the LARGER side of every later attempt and the otherwise clean L+T is rejected. The one-step shadow
    evaluation of the control's attempts says S2 would accept all of them -- the real S2 trajectory does not."""
    boards, variant = boards_by_variant(LARGE, TAILED, CLEAN_T)
    control, _, control_diag = final_sets(boards, variant, B_STRATEGY)
    assert control == [[0, 2], [1]]
    shadow = control_diag.summary()["shadow"]["candidates"]["S2"]
    # one-step shadow on the control's attempts -- (L, S), (L, T), (L+T, S): S2 "would accept" every one of them
    assert (shadow["attempts"], shadow["shadow_accepted"], shadow["recovered"]) == (3, 3, 2)
    experimental, checks, diag = final_sets(boards, variant, B_S2)
    assert experimental == [[0, 1], [2]] and checks["rejected_similarity"] == 1
    later = diag.rows[1]
    assert (later["b_root_variant"], later["larger_side_boards"], later["larger_side_below_tau"]) == (2, 330, 3)
    assert later["outcome"] == "rejected_similarity"


def test_s2_merge_and_diagnostics_are_deterministic() -> None:
    boards, variant = boards_by_variant(LARGE, TAILED, CLEAN_T, [SHELL8 + [9]] * 5)
    for strategy in (B_S2, C_S2):
        first = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig())
        again = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig())
        reordered = ar.merge_variants(list(reversed(boards)), variant, strategy, ar.ArchetypeConfig())
        assert first == again == reordered, strategy.name
        sums = [final_sets(b, variant, strategy)[2].summary("S2") for b in (boards, list(reversed(boards)))]
        assert sums[0] == sums[1], strategy.name


def test_final_group_diagnostics_measure_members_against_the_final_profile() -> None:
    boards, variant = boards_by_variant(LARGE, TAILED)
    to_group, log, checks = ar.merge_variants(boards, variant, B_S2, ar.ArchetypeConfig())
    grouping = ar.Grouping(variant=variant, group={k: to_group[v] for k, v in variant.items()}, log=[], converged=True,
                           refine_moves=[], merges=len(log), merge_checks=checks)
    final = ar.final_group_diagnostics(boards, grouping, B_S2, ar.ArchetypeConfig())
    assert (final["groups"], final["grouped_boards"], final["boards_below_tau"]) == (1, 330, 3)
    assert final["groups_with_any_board_below_tau"] == 1
    assert (final["groups_over_1pct_below_tau"], final["groups_over_5pct_below_tau"]) == (0, 0)  # 3/330 = 0.9%
    [g] = final["per_group"]
    assert (g["variants"], g["final_core_size"]) == (2, 8) and g["core_drift"] == 0.0
    assert TAU - 0.10 <= g["min_similarity"] < TAU


def test_experimental_strategies_reuse_their_controls_variants_and_report_the_comparison(store: Path) -> None:
    report, markdown, membership = run(store)
    strategies = report["strategies"]
    for exp, control in (("B_S2_experimental", "B_flex_tolerant"), ("C_S2_experimental", "C_structure_aware")):
        assert strategies[exp]["log"][:-1] == strategies[control]["log"][:-1]  # same leader pass and refinement
        assert strategies[exp]["variants_before_merge"] == strategies[control]["variants_before_merge"]
        variants_of = lambda name: {m["observation"]: m["variant"] for m in membership if m["strategy"] == name}  # noqa: E731
        assert variants_of(exp) == variants_of(control)
        e = strategies[exp]["experimental_s2"]
        assert e["control"] == control and e["decision_rule_mismatches"] == 0
        assert {r["metric"] for r in e["comparison"]} >= {"groups", "accepted merges", "near-duplicate groups (nearest other >= tau)",
                                                         "grouped boards below tau (final profile)", "groups with > 10% below tau"}
        assert [f["label"] for f in e["family_review"]] == [label for label, _ in ar.FAMILY_ANCHORS]
        assert [r["label"] for r in e["regression_review"]] == [label for label, _ in ar.REGRESSION_ANCHORS]
        text = json.dumps(e)
        for secret in ("PUUID", "SecretName", "M000", "M011", "NORMAL1", "EMPTYR", "match_id", "puuid"):
            assert secret not in text, (exp, secret)
    for heading in ("### EXPERIMENTAL B-S2.", "### EXPERIMENTAL C-S2.", "#### Accepted S2 merges",
                    "#### Final groups after ALL recursive merges", "#### Composition-family review",
                    "#### Regression-pattern review", "#### Chaining review"):
        assert any(line.startswith(heading) for line in markdown), heading
    assert sum(line.startswith("- S2 decision consistency: 0 attempts") for line in markdown) == 2
    assert "shadow" not in strategies["B_S2_experimental"]["merge_diagnostics"]  # shadow evaluation stays on the controls


def test_control_report_sections_are_unaffected_by_the_experimental_strategies(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A/B/C's report (JSON, Markdown section, membership) is identical with and without the experimental strategies."""
    full_report, full_md, full_members = run(store)
    monkeypatch.setattr(ar, "STRATEGIES", ar.STRATEGIES[:3])
    report, md, members = run(store)
    for name in ("A_structural_baseline", "B_flex_tolerant", "C_structure_aware"):
        assert full_report["strategies"][name] == report["strategies"][name], name
    cut = next(i for i, line in enumerate(full_md) if line.startswith("## Strategy B-S2"))
    assert full_md[:cut] == md[:next(i for i, line in enumerate(md) if line.startswith("## Statistical warnings"))]
    assert [m for m in full_members if not m["strategy"].endswith("_experimental")] == members


# ---------------------------------------------------------------- recursive lock-in and merge-order sensitivity (report only)

LOCK_T = ([SHELL8] * 300, TAILED, CLEAN_T)  # L ~ S (3 tails admitted) then (L+S) ~ T blocked by those tails
#: A board X = shell[0:6] + units 8 and 30: >= tau inside its 300-board variant and at the first merge, below tau at the second.
WEAK_X = [0, 1, 2, 3, 4, 5, 8, 30]
PARTIAL = ([SHELL8] * 299 + [WEAK_X], TAILED, [SHELL8 + [10, 11]] * 60)


def lock_run(groups, strategy=B_S2, config=None):
    boards, variant = boards_by_variant(*groups)
    diag = ar.MergeDiagnostics(TAU)
    result = ar.merge_variants(boards, variant, strategy, config or ar.ArchetypeConfig(), diagnostics=diag)
    return boards, variant, result, diag


def random_variants(seed: int) -> tuple[list[ar.Board], dict[int, int]]:
    """Variants drawn from a few overlapping shells (many exact core-overlap ties), with occasional poorly fitting
    member boards so S2 admits and later trips over below-tau tails."""
    rng = random.Random(seed)
    shells = [SHELL8, SHELL8[:7] + [8], SHELL8[:6] + [8, 9], SHELL8 + [9], [0, 1, 2, 3, 4, 10, 11, 12]]
    groups = []
    for _ in range(rng.randint(5, 10)):
        base = rng.choice(shells) + rng.sample(range(13, 19), rng.randint(0, 1))
        boards = [base] * rng.randint(2, 30)
        boards += [base + rng.sample(range(20, 60), rng.randint(3, 6)) for _ in range(rng.randint(0, 2))]
        groups.append(boards)
    return boards_by_variant(*groups)


def test_s2_condition_names_are_the_rules_own() -> None:
    assert ar.S2_CONDITION_NAMES == tuple(ar.s2_conditions(larger_below=1, smaller_below=0, smaller_boards=1, merged_below=1,
                                                           merged_boards=1, min_post=0.0, tau=TAU))


def test_accepted_s2_merge_marks_its_below_tau_boards_as_historical_tails() -> None:
    boards, _, (to_group, _, checks), diag = lock_run(LOCK_T)
    accepted = diag.lock[0]
    assert accepted["outcome"] == "accepted" and accepted["admitted_below_tau"] == accepted["newly_admitted_below_tau"] == 3
    tails = {b.obs for b in boards if len(b.identity) > 9}  # the three tail boards of S
    assert set(diag.provenance) == tails
    for obs, event in diag.provenance.items():
        assert (event["attempt"], event["side"], event["side_role"], event["tau"]) == (0, "b", "smaller", TAU)
        assert TAU - 0.10 <= event["similarity"] < TAU
        assert event["similarity"] == pytest.approx(diag.rows[0]["post"]["min"], abs=1e-3)
    assert len(diag.admissions) == 3
    summary = ar.recursive_lock_in_summary(diag)["accepted_tail_provenance_summary"]
    assert (summary["accepted_merges"], summary["accepted_merges_with_any_board_below_tau"], summary["boards_admitted_below_tau"]) == (1, 1, 3)
    assert summary["admission_events_by_side_role"] == {"larger": 0, "smaller": 3}


def test_board_above_tau_at_its_accepted_merge_is_never_a_historical_tail() -> None:
    """X (shell[0:6] + 8 + 9) is 0.62 against the accepted V0+V1 profile and 0.598 at the later attempt: below tau
    then, but it was never ADMITTED below tau, so it is not historical and the rejection is 'no historical-tail'."""
    x = [0, 1, 2, 3, 4, 5, 8, 9]
    boards, _, (_, _, checks), diag = lock_run(([SHELL8] * 9 + [x], [SHELL8] * 5, [SHELL8 + [11]] * 6))
    assert [e["outcome"] for e in diag.lock] == ["accepted", "rejected_similarity"]
    assert diag.rows[0]["below_tau"] == 0 and diag.provenance == {} and diag.admissions == []
    later = diag.lock[1]
    assert diag.rows[1]["larger_side_below_tau"] == 1
    assert (later["larger_side_below_tau_historical"], later["larger_side_below_tau_not_historical"]) == (0, 1)
    assert later["condition1_attribution"] == "no historical-tail" and later["counterfactual"] is None


def test_later_merge_blocked_by_a_historical_tail_on_the_larger_side() -> None:
    _, _, (to_group, _, checks), diag = lock_run(LOCK_T)
    assert to_group == {0: 0, 1: 0, 2: 1} and checks["rejected_similarity"] == 1
    r, e = diag.rows[1], diag.lock[1]
    assert (r["outcome"], r["larger_side_boards"], r["larger_side_below_tau"], r["smaller_side_below_tau"]) == ("rejected_similarity", 330, 3, 0)
    assert e["failed_conditions"] == ["larger side: no board below tau"]
    assert (e["larger_side_historical_tails"], e["larger_side_below_tau_historical"], e["larger_side_below_tau_not_historical"]) == (3, 3, 0)
    assert e["condition1_attribution"] == "all historical-tail"
    attribution = ar.recursive_lock_in_summary(diag)["similarity_rejection_attribution"]
    assert attribution["s2_similarity_rejections"] == 1
    assert attribution["condition1_attribution"]["all historical-tail"] == 1
    assert attribution["categories"]["condition 1 failed: all failing larger-side boards are historical tails"] == 1
    assert attribution["categories"]["only condition 1 failed"] == 1
    assert attribution["failing_larger_side_boards_historical_tails"] == 3


def test_tail_removal_counterfactual_recomputes_the_merged_profile_and_passes() -> None:
    boards, _, _, diag = lock_run(LOCK_T)
    cf = diag.lock[1]["counterfactual"]
    assert cf["evaluable"] and cf["passes"] and cf["removed_boards"] == 3
    assert cf["removed_share_of_larger_side"] == 3 / 330
    assert cf["before"]["merged_boards"] == 350 and cf["before"]["failed_conditions"] == ["larger side: no board below tau"]
    after = cf["after"]
    assert (after["merged_boards"], after["larger_side_boards"], after["smaller_side_boards"]) == (347, 327, 20)
    assert (after["larger_side_below_tau"], after["smaller_side_below_tau"], after["merged_below_tau"]) == (0, 0, 0)
    assert after["failed_conditions"] == [] and not after["sides_flipped"]
    # independently recompute the profile of the remaining boards
    champions = load_roster().champions
    kept = [b for b in boards if b.obs not in diag.provenance]
    vecs = [ar.board_vectors(b, B_S2, ar.ArchetypeConfig(), champions) for b in kept]
    profile = ar.mean_profile(vecs)
    assert after["min_similarity"] == min(ar.similarity(v, profile, B_S2) for v in vecs) >= TAU
    c = ar.recursive_lock_in_summary(diag)["historical_tail_removal_counterfactual"]
    assert (c["eligible_rejections"], c["would_pass_all_unchanged_s2_conditions"], c["would_still_fail"]) == (1, 1, 0)
    assert c["failure_transitions"] == {"larger side: no board below tau -> passes": 1}


def test_counterfactual_removes_only_historical_tails_never_other_weak_boards() -> None:
    boards, _, _, diag = lock_run(PARTIAL)
    weak = next(b.obs for b in boards if b.identity == frozenset(IDS[i] for i in WEAK_X))
    assert weak not in diag.provenance  # below tau at the later attempt, but never admitted below tau
    cf = diag.lock[1]["counterfactual"]
    assert cf["removed_boards"] == 3 == len(diag.provenance)  # the three tails, not X
    assert not cf["passes"]
    assert cf["after"]["larger_side_below_tau"] == 1 and cf["after"]["failed_conditions"] == ["larger side: no board below tau"]
    assert cf["after"]["merged_boards"] == diag.rows[1]["merged_boards"] - 3


def test_condition1_rejection_by_a_non_tail_board_is_no_historical_tail() -> None:
    """knife_edge: the only failing board X sits on the larger side and comes from its original variant."""
    boards, variant = knife_edge([[11]])
    diag = ar.MergeDiagnostics(TAU)
    ar.merge_variants(boards, variant, B_S2, ar.ArchetypeConfig(), diagnostics=diag)
    [e] = diag.lock
    assert e["outcome"] == "rejected_similarity" and "larger side: no board below tau" in e["failed_conditions"]
    assert e["condition1_attribution"] == "no historical-tail" and e["counterfactual"] is None and diag.provenance == {}
    cats = ar.recursive_lock_in_summary(diag)["similarity_rejection_attribution"]["categories"]
    assert cats["condition 1 failed: no failing larger-side board is a historical tail"] == 1


def test_partial_historical_tail_attribution() -> None:
    _, _, _, diag = lock_run(PARTIAL)
    e = diag.lock[1]
    assert (e["larger_side_below_tau_historical"], e["larger_side_below_tau_not_historical"]) == (3, 1)
    assert e["condition1_attribution"] == "partial historical-tail"
    s = ar.recursive_lock_in_summary(diag)
    assert s["similarity_rejection_attribution"]["condition1_attribution"]["partial historical-tail"] == 1
    assert s["historical_tail_removal_counterfactual"]["by_condition1_attribution"]["partial historical-tail"] == {"eligible": 1, "would_pass": 0}
    assert [c["reason"] for c in s["deterministic_review_samples"]] == ["condition 1 failed: PARTIAL historical-tail attribution"]


def test_smaller_side_failure_is_not_recursive_lock_in() -> None:
    _, _, _, diag = lock_run(([SHELL8] * 200, [SHELL8 + list(range(20, 26)), SHELL8 + list(range(26, 32))]))
    [e] = diag.lock
    assert e["failed_conditions"] == ["smaller side: <= 10% below tau"]
    assert e["condition1_attribution"] == "condition 1 did not fail" and e["counterfactual"] is None
    a = ar.recursive_lock_in_summary(diag)["similarity_rejection_attribution"]
    assert a["categories"]["only the smaller-side tail condition failed"] == 1
    assert sum(a["categories"][c] for c in ar.REJECTION_CATEGORIES[:4]) == 0


def test_core_rule_rejections_are_outside_the_similarity_rejection_denominator() -> None:
    config = ar.ArchetypeConfig(tau=0.0, **PAIRWISE_OFF)
    _, _, (_, _, checks), diag = lock_run(([list(range(7))] * 4, [[0, 1, 2, 3, 7, 8, 9]] * 4), config=config)
    assert checks["rejected_core"] == 1 and checks["rejected_similarity"] == 0
    assert diag.lock == [] and ar.recursive_lock_in_summary(diag)["similarity_rejection_attribution"]["s2_similarity_rejections"] == 0


def test_historical_tail_back_at_or_above_tau_is_not_a_current_blocker() -> None:
    """The tails share 5 extra units; a later 100-board variant with two of them lifts the tails back above tau.
    They stay historical, but they are not blockers and the merge is accepted."""
    extras = list(range(20, 25))
    _, _, (to_group, _, _), diag = lock_run(([SHELL8] * 300, [SHELL8 + [8]] * 27 + [SHELL8 + [8] + extras] * 3,
                                             [SHELL8 + [20, 21]] * 100))
    assert to_group == {0: 0, 1: 0, 2: 0} and len(diag.provenance) == 3
    later = diag.lock[1]
    assert later["outcome"] == "accepted" and later["larger_side_historical_tails"] == 3
    assert later["larger_side_historical_tails_at_or_above_tau"] == 3 and later["larger_side_below_tau_historical"] == 0
    assert later["condition1_attribution"] == "condition 1 did not fail" and later["counterfactual"] is None
    trajectory = ar.recursive_lock_in_summary(diag)["tail_trajectory"]
    assert trajectory["later_attempts_with_historical_tails_on_larger_side"] == 1
    assert trajectory["later_s2_rejections_with_historical_tails_on_larger_side"] == 0


@pytest.mark.parametrize("strategy", [B_S2, C_S2], ids=lambda s: s.name)
def test_lock_in_diagnostics_never_change_the_s2_partition(strategy: ar.Strategy, store: Path) -> None:
    cases = [boards_by_variant(*LOCK_T), boards_by_variant(*PARTIAL), knife_edge([[11]])] + [random_variants(s) for s in range(12)]
    for boards, variant in cases:
        plain = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig())
        diag = ar.MergeDiagnostics(TAU)
        assert ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig(), diagnostics=diag) == plain
        assert len(diag.lock) == len(diag.rows) == plain[2]["rejected_similarity"] + len(plain[1])
        assert diag.summary("S2")["decision_rule_mismatches"] == 0
    with Database.open_existing(store) as db:
        boards = ar.load_inputs(db, WINDOW).boards
    control = ar.cluster(boards, next(s for s in ar.STRATEGIES if s.name == strategy.variants_from), ar.ArchetypeConfig())
    grouping = ar.cluster_reusing_variants(boards, strategy, ar.ArchetypeConfig(), control)
    members = [b for b in boards if b.obs in grouping.variant]
    to_group, log, checks = ar.merge_variants(members, grouping.variant, strategy, ar.ArchetypeConfig())  # no diagnostics
    assert grouping.group == {k: to_group[v] for k, v in grouping.variant.items()}
    assert (grouping.merges, grouping.merge_checks) == (len(log), checks)
    assert grouping.lock_in["counts"]["accepted_merges"] == len(log)
    assert [r["replay"] for r in grouping.order_replays] == [o.label for o in ar.ORDER_REPLAYS]


def test_controls_get_no_lock_in_or_replays_and_stay_unchanged(store: Path) -> None:
    with Database.open_existing(store) as db:
        boards = ar.load_inputs(db, WINDOW).boards
    for strategy in ar.STRATEGIES[:3]:
        grouping = ar.cluster(boards, strategy, ar.ArchetypeConfig())
        assert grouping.lock_in == {} and grouping.order_replays == []
        if strategy.merge:
            members = [b for b in boards if b.obs in grouping.variant]
            to_group, log, checks = ar.merge_variants(members, grouping.variant, strategy, ar.ArchetypeConfig())
            assert grouping.group == {k: to_group[v] for k, v in grouping.variant.items()} and grouping.merges == len(log)
            assert "recursive_lock_in" not in grouping.merge_diagnostics and "trajectory" not in grouping.merge_diagnostics
    for boards_, variant in [boards_by_variant(*LOCK_T)] + [random_variants(s) for s in range(6)]:
        for strategy in (B_STRATEGY, C_STRATEGY):  # an S0 merge never admits a below-tau board
            diag = ar.MergeDiagnostics(TAU)
            ar.merge_variants(boards_, variant, strategy, ar.ArchetypeConfig(), diagnostics=diag)
            assert diag.provenance == {} and all(e["counterfactual"] is None for e in diag.lock)


@pytest.mark.parametrize("strategy", [B_STRATEGY, C_STRATEGY, B_S2, C_S2], ids=lambda s: s.name)
def test_baseline_order_replay_reproduces_the_real_merge(strategy: ar.Strategy) -> None:
    for boards, variant in [boards_by_variant(*LOCK_T), boards_by_variant(*PARTIAL)] + [random_variants(s) for s in range(25)]:
        real_diag, replay_diag = ar.MergeDiagnostics(TAU), ar.MergeDiagnostics(TAU)
        real = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig(), diagnostics=real_diag)
        replay = ar.merge_variants(boards, variant, strategy, ar.ArchetypeConfig(), diagnostics=replay_diag,
                                   tie_order=ar.TieOrder("baseline"))
        assert replay == real
        assert replay_diag.rows == real_diag.rows and replay_diag.lock == real_diag.lock


def test_tie_replays_never_judge_a_lower_overlap_candidate_first() -> None:
    differed = 0
    for seed in range(25):
        boards, variant = random_variants(seed)
        baseline = ar.merge_variants(boards, variant, B_S2, ar.ArchetypeConfig())
        for order in ar.ORDER_REPLAYS:
            trace: list = []
            result = ar.merge_variants(boards, variant, B_S2, ar.ArchetypeConfig(), tie_order=order, trace=trace)
            for chosen, best, tied in trace:  # brute force over every current pair: nothing valid scores higher
                assert best is not None and chosen == best and tied >= 1
            differed += result[0] != baseline[0]
    assert differed  # the random cases do contain real tie-order sensitivity
    # two pairs at different overlap: the reversed order still judges the higher one first
    boards, variant = variants((SHELL8, 5), (SHELL8, 5), (SHELL8[:6] + [8, 9], 5), (SHELL8[:6] + [8, 10], 5))
    for order in ar.ORDER_REPLAYS:
        diag = ar.MergeDiagnostics(TAU)
        ar.merge_variants(boards, variant, B_S2, ar.ArchetypeConfig(), diagnostics=diag, tie_order=order)
        assert diag.rows[0]["core_overlap"] == 1.0 and (diag.rows[0]["a_root_variant"], diag.rows[0]["b_root_variant"]) == (0, 1)


def test_order_replays_are_deterministic() -> None:
    assert ar.TieOrder("seeded", 1).key(3, 9, 0, 2) == ar.TieOrder("seeded", 1).key(3, 9, 0, 2) != ar.TieOrder("seeded", 2).key(3, 9, 0, 2)
    assert ar.TieOrder("reversed").key(1, 2, 0, 0) > ar.TieOrder("reversed").key(1, 3, 0, 0)
    with pytest.raises(ValueError):
        ar.TieOrder("best-placement").key(0, 1, 0, 0)
    for seed in range(8):
        boards, variant = random_variants(seed)
        champions = load_roster().champions
        vecs = {b.obs: ar.board_vectors(b, B_S2, ar.ArchetypeConfig(), champions) for b in boards}
        first = ar.order_replays(boards, vecs, variant, B_S2, ar.ArchetypeConfig())
        again = ar.order_replays(list(reversed(boards)), vecs, variant, B_S2, ar.ArchetypeConfig())
        assert first == again
        assert all(r["decision_rule_mismatches"] == 0 for r in first)


def test_partition_disagreement_on_a_hand_checkable_example() -> None:
    """Baseline {1,2,3} {4,5} {6}; replay {1,2} {3,4,5} {6}. Together in baseline: 12 13 23 45 (4); in replay:
    12 34 35 45 (4); in both: 12 45 (2). Disagreeing: 13 23 (baseline only) + 34 35 (replay only) = 4 of 15 pairs."""
    baseline = {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}
    replay = {1: 7, 2: 7, 3: 8, 4: 8, 5: 8}
    d = ar.partition_disagreement(range(1, 7), baseline, replay)
    assert (d["board_pairs"], d["pairs_together_in_baseline"], d["pairs_together_in_replay"], d["pairs_together_in_both"]) == (15, 4, 4, 2)
    assert (d["together_in_baseline_apart_in_replay"], d["together_in_replay_apart_in_baseline"], d["disagreeing_pairs"]) == (2, 2, 4)
    assert d["disagreement_share_of_all_pairs"] == 4 / 15 and d["disagreement_share_of_pairs_together_in_either"] == 4 / 6
    assert (d["boards_with_identical_group_membership"], d["boards_with_changed_group_membership"]) == (1, 5)  # only 6
    same = ar.partition_disagreement(range(1, 7), baseline, {k: g + 10 for k, g in baseline.items()})
    assert same["disagreeing_pairs"] == 0 and same["boards_with_changed_group_membership"] == 0


@pytest.mark.parametrize("anchors", ["FAMILY_ANCHORS", "REGRESSION_ANCHORS"])
def test_family_and_regression_anchors_are_report_only(anchors: str, store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, membership = run(store)
    monkeypatch.setattr(ar, anchors, (("anything", frozenset({"DA_18_Leona", "DA_18_Kayle"})),))
    report, _, changed = run(store)
    assert changed == membership  # every strategy's groups, variants and order are unchanged
    lock = report["strategies"]["B_S2_experimental"]["experimental_s2"]["recursive_lock_in"]["order_sensitivity"]
    key = "family_anchors" if anchors == "FAMILY_ANCHORS" else "regression_anchors"
    assert [f["label"] for f in lock["baseline"][key]] == ["anything"]


def test_report_carries_the_recursive_lock_in_section_without_identifiers(store: Path) -> None:
    report, markdown, _ = run(store)
    for name in ("B_S2_experimental", "C_S2_experimental"):
        e = report["strategies"][name]["experimental_s2"]
        assert e["decision_rule_mismatches"] == 0
        lock = e["recursive_lock_in"]
        assert set(lock) == {"definitions", "tau", "counts", "accepted_tail_provenance_summary", "similarity_rejection_attribution",
                             "historical_tail_removal_counterfactual", "tail_trajectory", "deterministic_review_samples",
                             "order_sensitivity"}
        for word in ("historically admitted tail", "order_sensitivity", "evidence", "historical_tail_removal_counterfactual"):
            assert word in lock["definitions"]
        o = lock["order_sensitivity"]
        assert o["baseline_replay_identical_to_real_grouping"] is True
        assert [r["replay"] for r in o["replays"]] == [t.label for t in ar.ORDER_REPLAYS]
        assert all(r["decision_rule_mismatches"] == 0 for r in o["replays"])
        assert [f["label"] for f in o["baseline"]["family_anchors"]] == [label for label, _ in ar.FAMILY_ANCHORS]
        # the replays' light core lookup agrees with the full family review on the real grouping
        assert [(f["groups"], f["largest_sizes"]) for f in o["baseline"]["family_anchors"]] == \
            [(f["experimental_groups"], f["experimental_sizes"]) for f in e["family_review"]]
        assert [(f["groups"], f["largest_sizes"]) for f in o["baseline"]["regression_anchors"]] == \
            [(f["experimental_groups"], f["experimental_sizes"]) for f in e["regression_review"]]
        assert [f["label"] for f in o["baseline"]["regression_anchors"]] == [label for label, _ in ar.REGRESSION_ANCHORS]
        text = json.dumps(lock, default=str)
        for secret in ("PUUID", "SecretName", "M000", "M011", "NORMAL1", "EMPTYR", "match_id", "puuid"):
            assert secret not in text, (name, secret)
    for name in ("A_structural_baseline", "B_flex_tolerant", "C_structure_aware"):
        assert "experimental_s2" not in report["strategies"][name]
    assert sum(line.startswith("#### Recursive lock-in diagnostics (REPORT ONLY") for line in markdown) == 2
    assert sum(line.startswith("##### Merge-order sensitivity") for line in markdown) == 2
    text = "\n".join(markdown)
    for secret in ("PUUID-SECRET", "SecretName", "M000", "M011"):
        assert secret not in text



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
                    "started", "variants reused from" if s.variants_from else "leader pass completed",
                    "variant merge completed" if s.variants_from else "refine 1 completed", "summaries/diagnostics started",
                    "summaries/diagnostics completed", "completed,")],
                *[f"{s.name}: experimental comparison completed" for s in ar.STRATEGIES if s.variants_from],
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
    assert status["pending_phases"] == ["C_structure_aware", "B_S2_experimental", "C_S2_experimental", "closing"]
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
