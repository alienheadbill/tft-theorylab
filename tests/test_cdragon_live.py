"""Live smoke test against the real CommunityDragon feed.

Skipped by default: this hits real network (`raw.communitydragon.org`),
which is undesirable in most CI environments and is blocked outright by some
sandboxes' egress policy. Opt in with `TFTLAB_LIVE_CDRAGON_TEST=1` from an
environment that has outbound network access.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tftlab.cdragon import CommunityDragonClient, SetMetadata

ROSTER_FIXTURE = Path(__file__).parent / "fixtures" / "current_set_roster.json"

RUN_LIVE = os.environ.get("TFTLAB_LIVE_CDRAGON_TEST") == "1"

requires_live_network = pytest.mark.skipif(
    not RUN_LIVE,
    reason="Set TFTLAB_LIVE_CDRAGON_TEST=1 (from an environment with network access) to run this",
)


@requires_live_network
def test_live_communitydragon_feed_parses_with_sensible_costs(tmp_path) -> None:
    with CommunityDragonClient(cache_dir=tmp_path) as client:
        meta = client.get_set_metadata("latest", use_cache=False)

    assert meta.set_number is not None and meta.set_number > 0
    assert meta.champions, "expected at least one champion in the current set"
    assert meta.items, "expected the bundle-wide items list to be non-empty"
    assert meta.traits, "expected at least one trait in the current set"

    sample = list(meta.champions.values())[:5]
    for champion in sample:
        assert champion.name, f"{champion.character_id} resolved to an empty name"
        # A real TFT champion is always a 1-5 cost; anything else means the
        # bundle shape (or our parsing of it) has drifted.
        assert 1 <= champion.cost <= 5, f"{champion.character_id} has implausible cost {champion.cost}"

    print(f"Set {meta.set_number} sample champions:")
    for champion in sample:
        print(f"  {champion.character_id}: {champion.name} (cost {champion.cost})")


def roster_snapshot(meta: SetMetadata) -> dict:
    """The committed-fixture shape of a set's roster (see
    tests/fixtures/current_set_roster.json and tests/test_demo_roster.py)."""
    return {
        "set_number": meta.set_number,
        "champions": {
            cid: {"name": c.name, "cost": c.cost} for cid, c in sorted(meta.champions.items())
        },
        "traits": {tid: t.name for tid, t in sorted(meta.traits.items())},
    }


@requires_live_network
def test_committed_roster_fixture_matches_live_set(tmp_path) -> None:
    """The offline demo-roster check (tests/test_demo_roster.py) trusts a
    committed copy of the current set's roster. This catches that copy going
    stale when a new set ships. On a mismatch, the message contains the full
    live roster as JSON so the fixture can be refreshed from the log."""
    with CommunityDragonClient(cache_dir=tmp_path) as client:
        live = roster_snapshot(client.get_set_metadata("latest", use_cache=False))

    committed = json.loads(ROSTER_FIXTURE.read_text()) if ROSTER_FIXTURE.exists() else {}
    committed = {k: committed.get(k) for k in ("set_number", "champions", "traits")}
    assert committed == live, (
        "tests/fixtures/current_set_roster.json is out of date. Live roster:\n"
        + json.dumps(live, indent=1, ensure_ascii=False)
    )
