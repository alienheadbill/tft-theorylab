"""Deterministic, stratified seed selection (tftlab.sampling)."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from tftlab import sampling
from tftlab.sampling import SAMPLING_MODES, allocate_seats, evenly_spaced, select_seeds


def _ladder(prefix: str, n: int, top_lp: int = 2000) -> dict:
    return {"entries": [{"puuid": f"{prefix}{i:04d}", "leaguePoints": top_lp - i} for i in range(n)]}


def _fetcher(payloads: dict[str, dict], calls: list[str] | None = None):
    def fetch(tier: str) -> dict:
        if calls is not None:
            calls.append(tier)
        return payloads[tier]

    return fetch


FULL = {"challenger": _ladder("c", 250), "grandmaster": _ladder("g", 500, 900), "master": _ladder("m", 3000, 400)}


def test_challenger_only_mode_fetches_only_challenger() -> None:
    calls: list[str] = []
    sel = select_seeds(_fetcher(FULL, calls), total=10, mode="challenger")
    assert calls == ["challenger"]
    assert sel.by_tier == {"challenger": 10}
    assert len(sel.puuids) == 10 and all(p.startswith("c") for p in sel.puuids)


def test_high_elo_fifty_is_twenty_fifteen_fifteen() -> None:
    calls: list[str] = []
    sel = select_seeds(_fetcher(FULL, calls), total=50, mode="high_elo")
    assert calls == ["challenger", "grandmaster", "master"]  # each tier once, nothing else
    assert sel.by_tier == {"challenger": 20, "grandmaster": 15, "master": 15}
    assert len(sel.puuids) == len(set(sel.puuids)) == 50
    assert sum(p.startswith("g") for p in sel.puuids) == 15
    assert sum(p.startswith("m") for p in sel.puuids) == 15


def test_challenger_heavy_ladder_no_longer_crowds_out_lower_tiers() -> None:
    """The old concatenate-then-slice behavior gave 50 Challenger seeds and
    no Grandmaster/Master whenever Challenger alone had 50+ players."""
    sel = select_seeds(_fetcher(FULL), total=50, mode="high_elo")
    assert sel.by_tier["grandmaster"] > 0 and sel.by_tier["master"] > 0


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (1, {"challenger": 1, "grandmaster": 0, "master": 0}),
        (3, {"challenger": 1, "grandmaster": 1, "master": 1}),
        (10, {"challenger": 4, "grandmaster": 3, "master": 3}),
        (25, {"challenger": 10, "grandmaster": 8, "master": 7}),
        (50, {"challenger": 20, "grandmaster": 15, "master": 15}),
        (100, {"challenger": 40, "grandmaster": 30, "master": 30}),
    ],
)
def test_requested_total_is_respected(total: int, expected: dict) -> None:
    sel = select_seeds(_fetcher(FULL), total=total, mode="high_elo")
    assert sel.by_tier == expected
    assert len(sel.puuids) == total


def test_underfilled_tier_redistributes_its_seats() -> None:
    payloads = {"challenger": _ladder("c", 250), "grandmaster": _ladder("g", 4, 900), "master": _ladder("m", 3000, 400)}
    sel = select_seeds(_fetcher(payloads), total=50, mode="high_elo")
    assert sel.by_tier["grandmaster"] == 4  # all it has
    assert sum(sel.by_tier.values()) == 50 == len(sel.puuids)
    # The 11 spare seats are re-split 4:3 between Challenger and Master.
    assert sel.by_tier == {"challenger": 26, "grandmaster": 4, "master": 20}


def test_fewer_players_than_requested_takes_everyone_once() -> None:
    payloads = {"challenger": _ladder("c", 3), "grandmaster": _ladder("g", 2, 900), "master": _ladder("m", 1, 400)}
    sel = select_seeds(_fetcher(payloads), total=50, mode="high_elo")
    assert sel.by_tier == {"challenger": 3, "grandmaster": 2, "master": 1}
    assert len(sel.puuids) == len(set(sel.puuids)) == 6


def test_empty_tier_is_skipped_safely() -> None:
    payloads = {"challenger": _ladder("c", 250), "grandmaster": {"entries": []}, "master": _ladder("m", 3000, 400)}
    sel = select_seeds(_fetcher(payloads), total=50, mode="high_elo")
    assert sel.by_tier["grandmaster"] == 0
    assert sum(sel.by_tier.values()) == 50


def test_duplicate_puuids_across_and_within_payloads_count_once() -> None:
    payloads = {
        "challenger": {"entries": [{"puuid": "x", "leaguePoints": 1500}, {"puuid": "x", "leaguePoints": 1500}, {"puuid": "a", "leaguePoints": 1400}]},
        # "x" was just demoted and is listed again, and "a" too.
        "grandmaster": {"entries": [{"puuid": "x", "leaguePoints": 950}, {"puuid": "a", "leaguePoints": 900}, {"puuid": "g1", "leaguePoints": 800}]},
        "master": {"entries": [{"puuid": "m1", "leaguePoints": 300}, {"puuid": "g1", "leaguePoints": 1}]},
    }
    sel = select_seeds(_fetcher(payloads), total=10, mode="high_elo")
    assert sorted(sel.puuids) == ["a", "g1", "m1", "x"]
    assert sel.ladder_sizes == {"challenger": 2, "grandmaster": 1, "master": 1}  # counted in the higher tier only
    assert sel.by_tier == {"challenger": 2, "grandmaster": 1, "master": 1}


def test_entries_without_a_puuid_are_ignored() -> None:
    payloads = {"challenger": {"entries": [{"summonerId": "old"}, {"puuid": "", "leaguePoints": 5}, {"puuid": "ok", "leaguePoints": 1}]}}
    assert select_seeds(_fetcher(payloads), total=5, mode="challenger").puuids == ("ok",)


def test_selection_is_deterministic_regardless_of_riot_response_order() -> None:
    first = select_seeds(_fetcher(FULL), total=50, mode="high_elo")
    rng = random.Random(7)
    for _ in range(5):
        shuffled = {t: {"entries": rng.sample(p["entries"], len(p["entries"]))} for t, p in FULL.items()}
        assert select_seeds(_fetcher(shuffled), total=50, mode="high_elo") == first


def test_seeds_span_each_tier_rather_than_only_its_top() -> None:
    sel = select_seeds(_fetcher(FULL), total=50, mode="high_elo")
    challenger = [int(p[1:]) for p in sel.puuids if p.startswith("c")]  # rank index 0 = highest LP
    assert challenger == sorted(challenger)
    assert challenger[0] < 10 and challenger[-1] > 240  # from near the top to near the bottom
    assert challenger == [(2 * i + 1) * 250 // 40 for i in range(20)]


def test_evenly_spaced_edges() -> None:
    items = [str(i) for i in range(10)]
    assert evenly_spaced(items, 0) == []
    assert evenly_spaced(items, 10) == items
    assert evenly_spaced(items, 20) == items
    assert evenly_spaced(items, 1) == ["5"]
    assert evenly_spaced(items, 2) == ["2", "7"]


def test_allocate_seats_properties() -> None:
    order = ("challenger", "grandmaster", "master")
    weights = SAMPLING_MODES["high_elo"][1]
    for total in range(0, 121):
        for caps in ({t: 1000 for t in order}, {"challenger": 5, "grandmaster": 1000, "master": 0}, {t: 7 for t in order}):
            alloc = allocate_seats(total, caps, weights, order)
            assert sum(alloc.values()) == min(total, sum(caps.values()))
            assert all(0 <= alloc[t] <= caps[t] for t in order)


def test_unknown_mode_and_negative_total_are_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown sampling mode"):
        select_seeds(_fetcher(FULL), total=5, mode="everyone")
    with pytest.raises(ValueError):
        allocate_seats(-1, {"challenger": 5}, {"challenger": 1}, ("challenger",))


def test_selection_ignores_everything_but_puuid_and_league_points() -> None:
    """Seeds depend only on ladder standing: extra entry data (anything a
    future payload might carry, champion-flavoured or not) changes nothing."""
    decorated = {
        t: {"entries": [{**e, "favoriteChampion": "DA_18_KhaZix", "hotStreak": True, "wins": 99} for e in p["entries"]]}
        for t, p in FULL.items()
    }
    assert select_seeds(_fetcher(decorated), total=50, mode="high_elo") == select_seeds(_fetcher(FULL), total=50, mode="high_elo")


def test_sampling_and_ingest_code_names_no_champion_item_or_trait() -> None:
    """No allowlist or comp-specific crawling: the seed/ingest code never
    mentions a champion, item or trait id or name."""
    root = Path(sampling.__file__).parent
    pattern = re.compile(
        r"DA_\d|TFT\d+_|TFT_Item|KhaZix|Kha'Zix|Cassiopeia|Fiddlesticks|Caitlyn|Warwick|Wolves|Ravager|Slayer",
    )
    for name in ("sampling.py", "ingest.py", "riot.py"):
        code = "\n".join(
            line for line in (root / name).read_text().splitlines() if not line.lstrip().startswith("#")
        )
        assert not pattern.search(code), name
