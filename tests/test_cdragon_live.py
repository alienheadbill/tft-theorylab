"""Live smoke test against the real CommunityDragon feed.

Skipped by default: this hits real network (`raw.communitydragon.org`),
which is undesirable in most CI environments and is blocked outright by some
sandboxes' egress policy. Opt in with `TFTLAB_LIVE_CDRAGON_TEST=1` from an
environment that has outbound network access.
"""

from __future__ import annotations

import os

import pytest

from tftlab.cdragon import CommunityDragonClient

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
