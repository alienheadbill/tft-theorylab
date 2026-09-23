"""Demo and example data must only use champions (and traits) that are in
the current TFT set, with their real shop costs, so a demo screen never
implies an out-of-set champion is playable.

Offline on purpose: it checks against the committed roster in
src/tftlab/data/set_roster.json, not the live CommunityDragon feed.
tests/test_cdragon_live.py keeps that fixture honest (nightly, opt-in).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tftlab.demo import CARRIES, FILLERS, generate_demo_matches
from tftlab.experiments import DEMO_EXPERIMENTS, normalize_comp

ROSTER = json.loads((Path(__file__).parents[1] / "src" / "tftlab" / "data" / "set_roster.json").read_text())
CHAMPIONS: dict[str, dict] = ROSTER["champions"]
# Shop champions only: the roster also lists camps, anvils and other 0/8/11-"cost" units.
PLAYABLE_NAMES = {c["name"] for c in CHAMPIONS.values() if 1 <= c["cost"] <= 5}
TRAIT_NAMES = set(ROSTER["traits"].values())


def test_roster_fixture_looks_like_a_real_set() -> None:
    assert isinstance(ROSTER["set_number"], int) and ROSTER["set_number"] > 0
    assert len(PLAYABLE_NAMES) >= 40
    assert TRAIT_NAMES


@pytest.mark.parametrize("character_id, name, cost", [c[:3] for c in CARRIES] + list(FILLERS))
def test_demo_units_are_real_current_set_champions(character_id: str, name: str, cost: int) -> None:
    assert character_id in CHAMPIONS, f"{name} ({character_id}) is not in the current set roster"
    assert CHAMPIONS[character_id]["name"] == name
    assert CHAMPIONS[character_id]["cost"] == cost, f"{name} costs {CHAMPIONS[character_id]['cost']}, not {cost}"


def test_generated_demo_matches_only_use_current_set_units() -> None:
    matches = generate_demo_matches(40)
    assert {m["info"]["tft_set_number"] for m in matches} == {ROSTER["set_number"]}
    seen = {
        (u["character_id"], u["name"], u["rarity"] + 1)
        for m in matches
        for p in m["info"]["participants"]
        for u in p["units"]
    }
    for character_id, name, cost in seen:
        assert CHAMPIONS.get(character_id) == {"name": name, "cost": cost}, (character_id, name, cost)


@pytest.mark.parametrize("entry", DEMO_EXPERIMENTS, ids=lambda e: e["title"])
def test_example_notebook_entries_only_name_current_set_champions_and_traits(entry: dict) -> None:
    carry_id = entry.get("carry_character_id")
    if carry_id:
        assert carry_id in CHAMPIONS, f"carry {carry_id} is not in the current set"
        assert CHAMPIONS[carry_id]["name"] == entry.get("carry_name")

    comp = normalize_comp(entry.get("comp"))
    units = comp["core_units"] + comp["optional_units"]
    for unit in units:
        assert unit["name"] in PLAYABLE_NAMES, f"{unit['name']} is not a current-set champion"
        if unit["character_id"]:
            assert CHAMPIONS[unit["character_id"]]["name"] == unit["name"]
    if comp["secondary_carry"] and comp["secondary_carry"]["unit"]:
        assert comp["secondary_carry"]["unit"] in PLAYABLE_NAMES
    for trait in comp["target_traits"]:
        assert trait["name"] in TRAIT_NAMES, f"{trait['name']} is not a current-set trait"


def test_known_out_of_set_champion_is_caught() -> None:
    """Guard the guard: the check really does reject a champion missing
    from the roster (Garen isn't in Set 18)."""
    assert "Garen" not in PLAYABLE_NAMES
    assert not any(c["name"] == "Garen" for c in CHAMPIONS.values())
