"""TEMPORARY probe (PR #51 review): print CommunityDragon's live
`associatedTraits` for the current set so the roster snapshot's trait-item
guard can be filled from the source. Runs only on GitHub Actions; never
fails; removed again before the PR is final."""

import json
import os
import warnings

import pytest


@pytest.mark.skipif(not os.environ.get("GITHUB_ACTIONS"), reason="probe runs only in CI")
def test_probe_cdragon_trait_items(tmp_path) -> None:
    try:
        import httpx

        from tftlab.cdragon import parse_set_metadata

        payload = httpx.get("https://raw.communitydragon.org/latest/cdragon/tft/en_us.json", timeout=120).json()
        meta = parse_set_metadata(payload, patch="latest")
        trait_ids = set(meta.traits)
        trait_names = {t.name for t in meta.traits.values()}
        refs = []
        for item in payload.get("items", []):
            assoc = item.get("associatedTraits") or []
            if any(a in trait_ids or a in trait_names for a in assoc):
                refs.append({"apiName": item.get("apiName"), "name": item.get("name"), "associatedTraits": assoc,
                             "incompatibleTraits": item.get("incompatibleTraits") or []})
        sample_other = [
            {"apiName": i.get("apiName"), "associatedTraits": i.get("associatedTraits")}
            for i in payload.get("items", []) if i.get("associatedTraits")
        ][:5]
        champs = {c.character_id: sorted(c.traits) for c in meta.champions.values() if c.traits}
        report = {"set_number": meta.set_number, "set_items_with_set_trait_refs": refs,
                  "first_items_with_any_refs": sample_other, "champion_trait_names": champs,
                  "traits": {k: v.name for k, v in meta.traits.items()}}
        warnings.warn("CDRAGON_TRAIT_ITEMS_PROBE " + json.dumps(report, sort_keys=True))
    except Exception as exc:  # never fail CI
        warnings.warn(f"CDRAGON_TRAIT_ITEMS_PROBE failed: {type(exc).__name__}: {exc}")
