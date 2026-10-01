"""TEMPORARY probe (PR #51 review): compute, on CommunityDragon's live feed,
the roster snapshot's trait-item guard (CommunityDragon `associatedTraits`
plus items named "<trait name> Emblem") so the committed snapshot can be
filled from the source. Runs only on GitHub Actions; never fails; removed
again before the PR is final."""

import json
import os
import warnings

import pytest


def _trait_item_ids(meta):
    by_name = {}
    for trait_id, trait in meta.traits.items():
        by_name.setdefault(trait.name, []).append(trait_id)
    out = {}
    for item_id, item in meta.items.items():
        refs = set(item.associated_traits)
        if item.name.endswith(" Emblem"):
            refs.add(item.name[: -len(" Emblem")])
        for ref in refs:
            if ref in meta.traits:
                out.setdefault(ref, set()).add(item_id)
            elif len(by_name.get(ref, [])) == 1:
                out.setdefault(by_name[ref][0], set()).add(item_id)
    return {t: sorted(i) for t, i in sorted(out.items())}


@pytest.mark.skipif(not os.environ.get("GITHUB_ACTIONS"), reason="probe runs only in CI")
def test_probe_cdragon_trait_items(tmp_path) -> None:
    try:
        from pathlib import Path

        from tftlab.cdragon import CommunityDragonClient, champion_trait_ids

        with CommunityDragonClient(cache_dir=tmp_path) as client:
            meta = client.get_set_metadata("latest", use_cache=False)
        committed = json.loads((Path(__file__).parents[1] / "src/tftlab/data/set_roster.json").read_text())
        live_traits = {cid: list(t) for cid, t in champion_trait_ids(meta).items()}
        same_membership = all(committed["champions"][cid]["traits"] == live_traits.get(cid) for cid in committed["champions"])
        report = {"set_number": meta.set_number, "trait_items": _trait_item_ids(meta),
                  "champion_traits_match_committed": same_membership,
                  "names_match_committed": {c: {"name": m.name, "cost": m.cost} for c, m in meta.champions.items()}
                  == {c: {"name": e["name"], "cost": e["cost"]} for c, e in committed["champions"].items()}}
        warnings.warn("CDRAGON_TRAIT_ITEMS_PROBE2 " + json.dumps(report, sort_keys=True))
    except Exception as exc:  # never fail CI
        warnings.warn(f"CDRAGON_TRAIT_ITEMS_PROBE2 failed: {type(exc).__name__}: {exc}")
