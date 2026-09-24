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

ROSTER_FIXTURE = Path(__file__).parents[1] / "src" / "tftlab" / "data" / "set_roster.json"

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
    src/tftlab/data/set_roster.json and tests/test_demo_roster.py)."""
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
        "src/tftlab/data/set_roster.json is out of date. Live roster:\n"
        + json.dumps(live, indent=1, ensure_ascii=False)
    )


@requires_live_network
def test_live_game_art_refresh_integrity(tmp_path) -> None:
    """Runs the real `refresh-game-art` download into a temp dir: every
    selected image must pass the status/content/format checks. When cached
    art is committed, the live result must match it (a mismatch means the
    committed art is stale: run the "Game art refresh" workflow)."""
    import warnings

    from tftlab.game_art import MANIFEST_PATH, refresh_game_art

    with CommunityDragonClient(cache_dir=tmp_path / "cache") as client:
        report = refresh_game_art(client, art_dir=tmp_path / "game", manifest_path=tmp_path / "manifest.json")

    sizes = sorted(p.stat().st_size for p in (tmp_path / "game").rglob("*.png"))
    summary = {
        "set_number": report.set_number,
        "cached": dict(report.cached),
        "missing": report.missing,
        "failures": report.failures[:20],
        "bytes_written": report.bytes_written,
        "largest_files": sizes[-3:],
        "shape": report.shape,
        "selected": report.planned,
    }
    # Surfaced in the pytest warnings summary so a passing run still logs it.
    warnings.warn("game art refresh summary: " + json.dumps(summary, ensure_ascii=False))
    assert report.ok, f"game art refresh failed: {report.failures[:20]}"

    if MANIFEST_PATH.exists():
        live = json.loads((tmp_path / "manifest.json").read_text())
        committed = json.loads(MANIFEST_PATH.read_text())
        for kind in ("champions", "items", "traits"):
            live_hashes = {k: v["sha256"] for k, v in live[kind].items()}
            committed_hashes = {k: v["sha256"] for k, v in committed[kind].items()}
            assert live_hashes == committed_hashes, f"committed {kind} art is stale; run the Game art refresh workflow"
