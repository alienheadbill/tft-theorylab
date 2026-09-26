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

from tftlab.cdragon import CommunityDragonClient, SetMetadata, item_stats_snapshot

ROSTER_FIXTURE = Path(__file__).parents[1] / "src" / "tftlab" / "data" / "set_roster.json"
ITEM_STATS_FIXTURE = Path(__file__).parents[1] / "src" / "tftlab" / "data" / "item_stats.json"

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

    from tftlab.game_art_refresh import MANIFEST_PATH, refresh_game_art

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
    assert not report.shape["duplicate_item_names"], "two cached items share a display name"

    if MANIFEST_PATH.exists():
        live = json.loads((tmp_path / "manifest.json").read_text())
        committed = json.loads(MANIFEST_PATH.read_text())
        for kind in ("champions", "items", "traits"):
            live_hashes = {k: v["sha256"] for k, v in live[kind].items()}
            committed_hashes = {k: v["sha256"] for k, v in committed[kind].items()}
            assert live_hashes == committed_hashes, f"committed {kind} art is stale; run the Game art refresh workflow"


def metadata_inventory(raw: dict) -> dict:
    """What CommunityDragon exposes about champion roles and item stats for
    the current set -- printed by the live test below so carry-eligibility
    metadata can be checked against the real feed (field names, raw values,
    coverage, special units). On 2026-09-26 (Set 18) every champion had a
    `role` key but only 2 of 74 shop champions had a non-null value, so a
    role-based carry rule could not be built on it; this test's output shows
    when that changes."""
    from collections import Counter

    sets = raw.get("setData") or raw.get("sets") or []
    current = max(sets, key=lambda s: int(s.get("number") or 0))
    champions = current.get("champions", [])
    champion_keys = Counter(k for c in champions for k in c)
    role_like = sorted(k for k in champion_keys if "role" in k.lower() or "class" in k.lower() or "archetype" in k.lower())
    shop = [c for c in champions if 1 <= int(c.get("cost") or 0) <= 5 and c.get("traits")]
    items = [i for i in raw.get("items", []) if str(i.get("apiName", "")).startswith(("TFT_Item_", f"TFT{current.get('number')}_Item"))]
    effect_keys = Counter(k for i in items for k in (i.get("effects") or {}))
    tag_values = Counter(t for i in items for t in (i.get("tags") or []))
    samples = [i for i in items if any(s in str(i.get("apiName")) for s in (
        "Warmog", "Gargoyle", "DragonsClaw", "Rabadon", "InfinityEdge", "GuinsoosRageblade", "BrambleVest", "JeweledGauntlet",
    ))] + [i for i in items if i.get("associatedTraits")][:4]
    return {
        "set_number": current.get("number"),
        "champion_keys": dict(champion_keys),
        "role_like_fields": {k: dict(Counter(str(c.get(k)) for c in champions)) for k in role_like},
        "shop_champions": [
            {k: c.get(k) for k in ("apiName", "name", "cost", *role_like, "traits")} for c in shop
        ],
        "non_shop_units": [
            {k: c.get(k) for k in ("apiName", "name", "cost", *role_like, "traits")} for c in champions if c not in shop
        ],
        "shop_champions_missing_role_fields": [c.get("apiName") for c in shop if role_like and not all(c.get(k) for k in role_like)],
        "item_keys": dict(Counter(k for i in items for k in i)),
        "item_effect_keys": dict(effect_keys.most_common()),
        "item_tag_values": dict(tag_values.most_common(40)),
        "sample_items": [
            {k: i.get(k) for k in ("apiName", "name", "composition", "effects", "tags", "associatedTraits", "incompatibleTraits", "unique")}
            for i in samples
        ],
        "elise": [c for c in champions if "Elise" in str(c.get("apiName"))],
        # Where emblems live in the bundle-wide item list (name/prefix/stats).
        "item_prefixes": dict(Counter("_".join(str(i.get("apiName", "")).split("_")[:2]) for i in raw.get("items", [])).most_common(25)),
        "emblems": [
            {k: i.get(k) for k in ("apiName", "name", "composition", "effects", "tags", "associatedTraits", "isAugment")}
            for i in raw.get("items", [])
            if "emblem" in str(i.get("apiName", "")).lower() and str(current.get("number")) in str(i.get("apiName", ""))
        ][:12],
    }


@requires_live_network
def test_print_role_and_item_metadata_inventory(tmp_path, capsys) -> None:
    with CommunityDragonClient(cache_dir=tmp_path) as client:
        raw = client.fetch_raw("latest", use_cache=False)
    inventory = metadata_inventory(raw)
    with capsys.disabled():
        print("\nCDRAGON METADATA INVENTORY")
        print(json.dumps(inventory, indent=1, ensure_ascii=False, default=str))
    assert inventory["shop_champions"]



@requires_live_network
def test_committed_item_stats_match_live_set(tmp_path) -> None:
    """Carry eligibility reads item stats from a committed snapshot, never the
    network. This catches the snapshot going stale; on a mismatch the message
    contains the full live snapshot so the file can be refreshed from the log."""
    with CommunityDragonClient(cache_dir=tmp_path) as client:
        live = item_stats_snapshot(client.get_set_metadata("latest", use_cache=False))
    committed = json.loads(ITEM_STATS_FIXTURE.read_text()) if ITEM_STATS_FIXTURE.exists() else {}
    committed = {k: committed.get(k) for k in ("set_number", "items")}
    assert committed == live, (
        "src/tftlab/data/item_stats.json is out of date. Live snapshot:\n"
        + "ITEM_STATS_SNAPSHOT_BEGIN\n" + json.dumps(live, ensure_ascii=False, separators=(",", ":")) + "\nITEM_STATS_SNAPSHOT_END"
    )


def da_namespace_inventory(raw: dict) -> dict:
    """Every `DA_*` item in the bundle-wide items list (the namespace Set 18
    Match-V1 boards store), with readable stats and any `TFT_Item_*` entry
    sharing its display name."""
    items = raw.get("items", [])
    by_name: dict = {}
    for i in items:
        if str(i.get("apiName", "")).startswith("TFT_Item_"):
            by_name.setdefault(i.get("name"), []).append(i["apiName"])

    def readable(values):
        return sorted(str(v) for v in (values or []) if not str(v).startswith("{"))

    rows = []
    for i in items:
        api = str(i.get("apiName", ""))
        if not api.startswith("DA_"):
            continue
        rows.append({
            "apiName": api,
            "name": i.get("name"),
            "effects": readable((i.get("effects") or {}).keys()),
            "tags": readable(i.get("tags")),
            "hashed_tags": sum(1 for t in (i.get("tags") or []) if str(t).startswith("{")),
            "composition": i.get("composition") or [],
            "isAugment": i.get("isAugment"),
            "tft_item_same_name": by_name.get(i.get("name"), []),
        })
    return {"count": len(rows), "items": rows}


@requires_live_network
def test_print_da_item_namespace(tmp_path, capsys) -> None:
    with CommunityDragonClient(cache_dir=tmp_path) as client:
        raw = client.fetch_raw("latest", use_cache=False)
    inventory = da_namespace_inventory(raw)
    with capsys.disabled():
        print("\nDA ITEM NAMESPACE BEGIN")
        print(json.dumps(inventory, ensure_ascii=False))
        print("DA ITEM NAMESPACE END")
