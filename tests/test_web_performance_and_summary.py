"""Discovery / Champion Investigation: work done per request, equivalence of
the cheaper paths, the Discovery population cache, the "How players carry"
summary rules and item classes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _helpers import make_match

V_NEW = "Version 14.7.580.4321 (Sep 24 2024/13:00:00) [PUBLIC] <Releases/14.7>"
KHA = "DA_18_KhaZix"


@pytest.fixture()
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    import tftlab.webapp as webapp

    webapp._POPULATION_CACHE.clear()
    return TestClient(webapp.create_app())


def _spy(monkeypatch: pytest.MonkeyPatch, module, name: str) -> list:
    calls: list = []
    original = getattr(module, name)

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return calls


def _demo_db(tmp_path: Path):
    from tftlab.storage import Database

    return Database(tmp_path / "web-demo.sqlite3")


# ---------------------------------------------------------------- work per request


def test_champion_picker_never_runs_full_carry_statistics(demo: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import tftlab.champion_investigation as ci

    full = _spy(monkeypatch, ci, "carry_commitment_stats")
    counts = _spy(monkeypatch, ci, "carry_board_counts")
    body = demo.get("/api/champions").json()
    assert full == [] and len(counts) == 1
    kha = next(c for c in body["champions"] if c["slug"] == "khazix")
    assert kha["carry_games"] > 0


def test_champion_detail_aggregates_only_that_champion(demo: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import tftlab.champion_investigation as ci

    full = _spy(monkeypatch, ci, "carry_commitment_stats")
    counts = _spy(monkeypatch, ci, "carry_board_counts")
    average = _spy(monkeypatch, ci, "carry_board_average")
    assert demo.get("/api/champions/khazix").status_code == 200
    assert len(full) == 1 and full[0][1]["character_ids"] == [KHA]
    assert counts == [] and len(average) == 1


def test_discovery_builds_evidence_only_for_requested_costs(demo: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import tftlab.analytics.discovery as discovery

    batches = _spy(monkeypatch, discovery, "carry_partner_associations_for_many")
    body = demo.get("/api/discovery", params={"costs": "2", "min_samples": 1}).json()
    assert body["costs"] == [2] and body["window_carries"] >= len(body["candidates"]) > 0
    assert {c["cost"] for c in body["candidates"]} == {2}
    requested = set(batches[0][0][1])
    assert requested == {c["character_id"] for c in body["candidates"]}  # nothing else enriched
    assert len(batches) == 1  # one batched query for all of them, not one per carry


@pytest.mark.parametrize("costs, expected", [("4", {4}), ("5", {5}), ("1,3,5", {1, 3, 5}), ("3,1", {1, 3})])
def test_discovery_cost_selection_is_exact(demo: TestClient, costs: str, expected: set) -> None:
    body = demo.get("/api/discovery", params={"costs": costs, "min_samples": 1}).json()
    assert body["costs"] == sorted(expected)
    assert {c["cost"] for c in body["candidates"]} <= expected
    assert body["window_carries"] > 0  # the window has data even when a cost has none


@pytest.mark.parametrize("costs", ["0", "6", "1,,2", "a", "1;2"])
def test_discovery_rejects_malformed_costs(demo: TestClient, costs: str) -> None:
    assert demo.get("/api/discovery", params={"costs": costs}).status_code == 422


def test_discovery_without_costs_keeps_the_max_cost_behaviour(demo: TestClient) -> None:
    body = demo.get("/api/discovery", params={"max_cost": 3, "min_samples": 1}).json()
    assert body["costs"] == [1, 2, 3] and all(c["cost"] <= 3 for c in body["candidates"])


def test_filters_that_match_nothing_are_not_reported_as_no_data(demo: TestClient) -> None:
    body = demo.get("/api/discovery", params={"costs": "1", "min_samples": 100000}).json()
    assert body["candidates"] == [] and body["window_carries"] > 0


# ---------------------------------------------------------------- population cache


def test_population_is_reused_until_the_window_changes(
    demo: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tftlab.webapp as webapp

    calls = _spy(monkeypatch, webapp, "discovery_population")
    first = demo.get("/api/discovery", params={"costs": "1,2,3", "min_samples": 1}).json()
    demo.get("/api/discovery", params={"costs": "4,5", "min_samples": 1})
    kha = demo.get(f"/api/discovery/{KHA}").json()["candidate"]
    assert len(calls) == 1  # same window, same matches: one aggregate for all three requests

    window = first["balance_window"]
    with _demo_db(tmp_path) as db:
        latest = db.query_one("SELECT MAX(game_datetime) FROM matches WHERE balance_window = ?", (window,))[0]
        version = db.query_one("SELECT game_version FROM matches WHERE balance_window = ? LIMIT 1", (window,))[0]
        db.ingest_match(make_match("NEW_AFTER_CACHE", game_version=version, game_datetime=latest + 1,
                                   units=[{"character_id": KHA, "rarity": 2, "tier": 2,
                                           "itemNames": ["TFT_Item_BlueBuff", "TFT_Item_JeweledGauntlet"]}]))
    after = demo.get(f"/api/discovery/{KHA}").json()["candidate"]
    assert len(calls) == 2  # a new match in the window invalidates the cached population
    assert after["commitment_games"] == kha["commitment_games"] + 1


# ---------------------------------------------------------------- equivalence of the cheaper paths


def test_cheap_counts_average_and_narrow_stats_equal_the_full_statistics(demo: TestClient, tmp_path: Path) -> None:
    demo.get("/api/health")  # builds the demo database
    from tftlab.analytics import carry_board_average, carry_board_counts, carry_commitment_stats, default_balance_window

    with _demo_db(tmp_path) as db:
        window = default_balance_window(db)
        full = carry_commitment_stats(db, balance_window=window, min_cost=1, max_cost=5, min_samples=1)
        assert carry_board_counts(db, window) == {s.character_id: s.commitment_games for s in full}
        games = sum(s.commitment_games for s in full)
        avg = carry_board_average(db, window)
        assert avg["carry_games"] == games
        assert avg["top4_rate"] == pytest.approx(sum(s.top4_rate * s.commitment_games for s in full) / games)
        assert avg["avg_placement"] == pytest.approx(sum(s.avg_placement * s.commitment_games for s in full) / games)
        for stat in full:
            narrow = carry_commitment_stats(db, balance_window=window, min_cost=1, max_cost=5, min_samples=1,
                                            character_ids=[stat.character_id])
            assert narrow == [stat]
        assert carry_commitment_stats(db, balance_window=window, min_cost=1, max_cost=5, min_samples=1,
                                      character_ids=[]) == []


def test_batched_evidence_equals_per_carry_evidence(demo: TestClient, tmp_path: Path) -> None:
    demo.get("/api/health")
    from tftlab.analytics import (
        carry_commitment_stats, carry_partner_associations, default_balance_window, item_package_stats,
        trait_breakpoint_associations,
    )
    from tftlab.analytics.item_packages import item_package_stats_for_many
    from tftlab.analytics.partners import carry_partner_associations_for_many
    from tftlab.analytics.traits import trait_breakpoint_associations_for_many

    with _demo_db(tmp_path) as db:
        window = default_balance_window(db)
        ids = [s.character_id for s in carry_commitment_stats(db, balance_window=window, min_cost=1, max_cost=5,
                                                               min_samples=1)] + ["TFT99_NotCarried"]
        partners = carry_partner_associations_for_many(db, ids, window)
        items = item_package_stats_for_many(db, ids, window)
        traits = trait_breakpoint_associations_for_many(db, ids, window)
        for cid in ids:
            assert partners[cid] == carry_partner_associations(db, cid, window)
            assert items[cid] == item_package_stats(db, cid, window)
            assert traits[cid] == trait_breakpoint_associations(db, cid, window)
        assert partners["TFT99_NotCarried"] == [] and items["TFT99_NotCarried"]["packages"] == []


def test_association_ties_are_ordered_by_key() -> None:
    from tftlab.analytics.association import compute_associations

    games = [(1, frozenset({"b", "a"})), (8, frozenset({"a", "b"}))]
    assert [a.key for a in compute_associations(games)] == ["a", "b"]
    assert [a.key for a in compute_associations(list(reversed(games)))] == ["a", "b"]


# ---------------------------------------------------------------- summary rules


def _carry(games, hit, miss, hit_top4, miss_top4):
    return {"games": games, "three_star": {"hit_games": hit, "miss_games": miss, "hit_rate": hit / games,
                                           "hit_top4_rate": hit_top4, "miss_top4_rate": miss_top4}}


def _row(name, games, share, with_, without, adj, limited=False, normal=True, games_without=50):
    return {"items": [{"name": name}], "name": name, "games": games, "share_of_carry_games": share,
            "top4_with": with_, "top4_without": without, "adjusted_top4_difference": adj,
            "limited_sample": limited, "normal_build": normal, "games_without": games_without}


STRATEGY_WORDS = ("roll", "level", "econ", "position", "force", "always", "best in slot", "bis", "optimal",
                  "mandatory", "causes")


def _text(summary) -> str:
    return " ".join(summary["observed"] + summary["interpretation"]).lower()


def test_three_star_difference_is_stated_as_observational_signal_with_numbers() -> None:
    from tftlab.champion_investigation import carry_summary

    s = carry_summary("Kha'Zix", _carry(300, 150, 150, 0.674, 0.312), [], [], [])
    assert any("reached 3★" in line for line in s["observed"])
    assert any(
        "higher observed Top 4" in line
        and "67.4%" in line
        and "31.2%" in line
        and "+36.2 percentage points" in line
        and "not proof that 3★ caused" in line
        for line in s["interpretation"]
    )
    assert not any(w in _text(s) for w in STRATEGY_WORDS)


def test_small_or_one_sided_splits_do_not_produce_a_three_star_claim() -> None:
    from tftlab.champion_investigation import carry_summary

    thin = carry_summary("X", _carry(40, 12, 28, 0.9, 0.2), [], [], [])
    assert any("Too few boards on one side" in line for line in thin["interpretation"])
    assert not any("dependent" in line for line in thin["interpretation"])
    tiny = carry_summary("X", _carry(12, 5, 7, 0.8, 0.3), [], [], [])
    assert any("early signal" in line for line in tiny["interpretation"])
    within = carry_summary("X", _carry(200, 100, 100, 0.52, 0.50), [], [], [])
    assert any("higher observed Top 4" in line and "+2.0 percentage points" in line for line in within["interpretation"])
    equal = carry_summary("X", _carry(200, 100, 100, 0.50, 0.50), [], [], [])
    assert any("Observed Top 4 was the same" in line for line in equal["interpretation"])
    none = carry_summary("X", _carry(80, 0, 80, None, 0.5), [], [], [])
    assert any("None of the" in line for line in none["observed"]) and not any("3★" in l for l in none["interpretation"])


def test_normal_build_leads_and_special_items_are_called_out() -> None:
    from tftlab.champion_investigation import carry_summary

    builds = [
        _row("Artifact Build", 90, 0.3, 0.6, 0.5, 0.04, normal=False),
        _row("Normal Build", 60, 0.2, 0.55, 0.5, 0.02),
    ]
    s = carry_summary("X", _carry(300, 150, 150, 0.6, 0.4), builds, [], [])
    assert any(line.startswith("Most common normal full build: Normal Build") for line in s["observed"])
    assert any("includes an Artifact, Radiant or unrecognized item: Artifact Build" in line for line in s["observed"])


def test_unsupported_rows_are_never_called_best_supported() -> None:
    from tftlab.champion_investigation import carry_summary

    partners = [_row("Limited", 8, 0.1, 0.9, 0.4, 0.05, limited=True), _row("Worse", 80, 0.5, 0.4, 0.5, -0.02)]
    s = carry_summary("X", _carry(300, 150, 150, 0.6, 0.4), [], partners, [])
    assert not any("Strongest with-vs-without partner" in line for line in s["observed"])
    assert any("Most frequent partner: Worse" in line for line in s["observed"])
    assert any("associations, not a proven core" in line for line in s["interpretation"])


def test_investigation_payload_carries_the_summary_and_item_classes(demo: TestClient) -> None:
    body = demo.get("/api/champions/khazix").json()
    assert set(body["summary"]) == {"observed", "interpretation"} and body["summary"]["observed"]
    for row in body["items"]["builds"]:
        assert isinstance(row["normal_build"], bool)
        assert all(i["kind"] and i["name_source"] in ("metadata", "id") for i in row["items"])
    text = json.dumps(body).lower()
    for absent in ("variant", "theorycraft", "archetype", "cluster", "association_score"):
        assert absent not in text


# ---------------------------------------------------------------- items


@pytest.mark.parametrize(
    "item_id, kind, name, source",
    [
        ("DA_Artifact_LichBane", "artifact", "Lich Bane", "id"),
        ("DA_EdgeOfNightRadiant", "radiant", "Edge of Night", "id"),
        ("TFT_Item_Artifact_LichBane", "artifact", "Lich Bane", "metadata"),
        ("DA_GiantSlayer", "standard", "Giant Slayer", "metadata"),
        ("TFT_Item_RadiantVirtue", "standard", "Virtue of the Martyr", "metadata"),  # not a Radiant item
        ("TFT_Item_Made_Up", "unknown", "Made Up", "id"),
    ],
)
def test_item_refs_classify_and_name_items_without_inventing_aliases(item_id, kind, name, source) -> None:
    from tftlab.game_art import item_ref

    ref = item_ref(item_id)
    assert (ref["kind"], ref["name"], ref["name_source"]) == (kind, name, source)
    if kind in ("artifact", "radiant", "unknown"):
        assert ref["art_url"] is None  # never another item's icon


# ---------------------------------------------------------------- wording and page contracts

WEB = Path(__file__).parent.parent / "src" / "tftlab" / "web"


def test_player_facing_counts_say_carry_boards() -> None:
    for name in ("static/champion.js", "champion.html", "static/app.js", "index.html"):
        text = (WEB / name).read_text()
        assert "carry game" not in text and "committed games" not in text, name
    assert "carry board" in (WEB / "static" / "champion.js").read_text()
    assert "min. carry boards" in (WEB / "index.html").read_text()


def test_champion_page_leads_with_how_to_play_and_keeps_the_evidence_in_its_own_tab() -> None:
    """The "How to play" redesign: the concise view is the default tab, the
    detail tabs follow, and the dense statistics (observed summary, results,
    methodology) keep their order inside the Evidence tab."""
    js = (WEB / "static" / "champion.js").read_text()
    panels = [js.index(f"panel('{tab}', active, ") for tab in ("play", "items", "teammates", "traits", "evidence")]
    assert panels == sorted(panels)
    evidence = js[panels[-1]:]
    order = [evidence.index(f"{fn}(") for fn in ("summarySection", "carrySection", "trustSection")]
    assert order == sorted(order)
    assert "state.tab || tabFromHash() || 'play'" in js  # How to play is the default
    assert "stamp('interpretation', 'Interpretation')" in js
    assert "2+ completed items, at least one of them a carry item" in js  # still explained, in the trust section
    assert "position" not in js.lower().replace("positioning advice", "").replace("positioning, augments", "")


def test_champion_picker_keeps_loading_state_during_slow_request() -> None:
    js = (WEB / "static" / "champion.js").read_text()
    assert "championsStatus: 'idle'" in js
    assert "state.championsStatus = 'loading'" in js
    assert "state.championsStatus === 'idle' || state.championsStatus === 'loading'" in js
    assert "seq !== state.championsRequestSeq" in js
    assert js.index("state.championsStatus === 'idle' || state.championsStatus === 'loading'") < js.index(
        "No champion list is available yet"
    )


def test_discovery_script_never_calls_loading_empty_and_ignores_stale_responses() -> None:
    js = (WEB / "static" / "app.js").read_text()
    assert "state.status === 'loading'" in js and "seq !== state.requestSeq" in js and "abort()" in js
    empty_at = js.index("No discovery data for this balance window yet")
    assert js.index("state.status === 'loading'") < empty_at and "state.windowCarries === 0" in js
    assert "max_cost: '5'" not in js  # no longer fetches every cost and filters client-side
