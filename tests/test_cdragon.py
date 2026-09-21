import pytest

from tftlab.cdragon import parse_set_metadata
from tftlab.normalize import cost_from_unit

# A trimmed fixture in the real shape of CommunityDragon's
# `cdragon/tft/en_us.json` bundle: items are bundle-wide, `setData` holds one
# entry per TFT set with its own champions/traits.
FIXTURE_PAYLOAD = {
    "items": [
        {"apiName": "TFT_Item_BlueBuff", "name": "Blue Buff", "icon": "ASSETS/Items/BlueBuff.png"},
        {"apiName": "TFT_Item_Deathcap", "name": "Rabadon's Deathcap", "icon": "ASSETS/Items/Deathcap.png"},
    ],
    "setData": [
        {
            "number": 13,
            "champions": [
                {"apiName": "TFT13_Ahri", "name": "Ahri", "cost": 4, "squareIcon": "ASSETS/Characters/Ahri.png"},
            ],
            "traits": [
                {"apiName": "TFT13_StarGuardian", "name": "Star Guardian", "icon": "ASSETS/Traits/SG.png"},
            ],
        },
        {
            "number": 14,
            "champions": [
                {"apiName": "TFT14_Aatrox", "name": "Aatrox", "cost": 1, "squareIcon": "ASSETS/Characters/Aatrox.png"},
                {"apiName": "TFT14_Ezreal", "name": "Ezreal", "cost": 4, "squareIcon": "ASSETS/Characters/Ezreal.png"},
            ],
            "traits": [
                {"apiName": "TFT14_Juggernaut", "name": "Juggernaut", "icon": "ASSETS/Traits/Juggernaut.png"},
            ],
        },
    ],
}


def test_parse_set_metadata_defaults_to_highest_set_number() -> None:
    meta = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6")
    assert meta.set_number == 14
    assert set(meta.champions) == {"TFT14_Aatrox", "TFT14_Ezreal"}
    # Set 13's trait must not leak into set 14's metadata.
    assert "TFT13_StarGuardian" not in meta.traits


def test_parse_set_metadata_explicit_set_number() -> None:
    meta = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6", set_number=13)
    assert meta.set_number == 13
    assert set(meta.champions) == {"TFT13_Ahri"}


def test_cost_for_champion_lookup() -> None:
    meta = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6")
    assert meta.cost_for_champion("TFT14_Aatrox") == 1
    assert meta.cost_for_champion("TFT14_Ezreal") == 4
    assert meta.cost_for_champion("TFT_Nonexistent") is None


def test_items_are_bundle_wide_not_per_set() -> None:
    meta = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6")
    assert meta.items["TFT_Item_BlueBuff"].name == "Blue Buff"
    assert meta.items["TFT_Item_Deathcap"].name == "Rabadon's Deathcap"


def test_icon_urls_are_lowercased_and_patch_scoped() -> None:
    meta = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6")
    aatrox = meta.champions["TFT14_Aatrox"]
    assert aatrox.icon_url == "https://raw.communitydragon.org/14.6/game/assets/characters/aatrox.png"

    blue_buff = meta.items["TFT_Item_BlueBuff"]
    assert blue_buff.icon_url == "https://raw.communitydragon.org/14.6/game/assets/items/bluebuff.png"

    juggernaut = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6").traits["TFT14_Juggernaut"]
    assert juggernaut.icon_url == "https://raw.communitydragon.org/14.6/game/assets/traits/juggernaut.png"


def test_parse_set_metadata_requires_set_data() -> None:
    with pytest.raises(ValueError):
        parse_set_metadata({"items": []}, patch="14.6")


def test_parse_set_metadata_missing_set_number_raises() -> None:
    with pytest.raises(ValueError):
        parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6", set_number=99)


def test_cost_from_unit_prefers_cdragon_over_rarity() -> None:
    meta = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6")
    # rarity=0 would normally imply cost 1 via the rarity+1 fallback, but
    # CommunityDragon says this apiName is a 4-cost champion; static metadata
    # must win.
    unit = {"character_id": "TFT14_Ezreal", "rarity": 0}
    assert cost_from_unit(unit, cost_lookup=meta.cost_for_champion) == 4


def test_cost_from_unit_falls_back_to_rarity_when_unknown_to_cdragon() -> None:
    meta = parse_set_metadata(FIXTURE_PAYLOAD, patch="14.6")
    # Deterministic demo data uses synthetic apiNames CommunityDragon has
    # never heard of; the rarity+1 fallback must still apply.
    unit = {"character_id": "TFT99_NotReal", "rarity": 2}
    assert cost_from_unit(unit, cost_lookup=meta.cost_for_champion) == 3
