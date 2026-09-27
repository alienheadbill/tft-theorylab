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


def make_payload(match_id: str, boards: list[list[dict]], *, queue_id: int = 1100, puuid_prefix: str = "PUUID-SECRET") -> dict:
    return {
        "metadata": {"match_id": match_id, "participants": [f"{puuid_prefix}-{i}" for i in range(len(boards))]},
        "info": {
            "game_version": "Version 14.6.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/14.6>",
            "tft_game_type": "standard", "queue_id": queue_id, "tft_set_number": 18, "tft_set_core_name": "TFTSet18",
            "game_datetime": 1_790_000_000_000,
            "participants": [
                {"placement": i + 1, "level": 8, "augments": [], "units": b, "puuid": f"{puuid_prefix}-{i}",
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


def build_payloads(seed: int = 3, matches: int = 12) -> list[dict]:
    rng = random.Random(seed)
    kinds = [x_board] * 4 + [y_board] * 2 + [x_other_shell] * 1 + [noise]
    payloads = []
    for m in range(matches):
        boards = [kind(rng) for kind in kinds]
        rng.shuffle(boards)
        payloads.append(make_payload(f"M{m:03d}", boards))
    return payloads


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store.sqlite3"
    with Database(path) as db:
        for p in build_payloads():
            db.ingest_match(p)
        db.ingest_match(make_payload("NORMAL1", [[unit(c) for c in C1[:6]]], queue_id=1090))  # non-ranked
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


# ---------------------------------------------------------------- population / names / outputs


def test_population_uses_ranked_unit_observable_boards(store: Path) -> None:
    report, markdown, _ = run(store)
    pop = report["population"]
    assert (pop["window_matches_any_queue"], pop["matches"], pop["excluded_non_ranked_matches"]) == (13, 12, 1)
    assert (pop["participants"], pop["unit_observable_participants"], pop["boards_eligible_for_grouping"]) == (96, 96, 96)
    assert pop["queue_id"] == 1100
    assert report["read_only_connection"] == "sqlite mode=ro"
    assert markdown[0].startswith("# Board archetype research report (EXPERIMENTAL")
    assert any("Declared configuration" in line for line in markdown)


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
        for p in payloads:
            db.ingest_match(p)
        db.ingest_match(make_payload("NORMAL1", [[unit(c) for c in C1[:6]]], queue_id=1090))
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
        for secret in ("PUUID-SECRET", "SecretName", "M000", "M011", "NORMAL1"):
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
        for p in build_payloads():
            db.ingest_match(p)
        db.commit()
    finally:
        db.close()
    with Database.open_existing(POSTGRES_TEST_URL) as ro:
        report, _, _ = ar.build_report(ro, balance_window=WINDOW)
        with pytest.raises(Exception):
            ro.execute("DELETE FROM matches")  # the session refuses writes
    assert report["read_only_connection"] == "postgres transaction_read_only=on"
    assert report["population"]["boards_eligible_for_grouping"] == 96


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
    assert "timeout-minutes: 60" in text


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
