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


# Shapes copied from the live Set 18 bundle (cdragon-live inventory, 2026-09-26).
SET18_PAYLOAD = {
    "setData": [
        {
            "number": 18,
            "champions": [
                {"apiName": "DA_18_Elise", "name": "Elise", "cost": 2, "role": None, "traits": ["Coven", "Vanguard"]},
                {"apiName": "DA_18_Kobuko", "name": "Kobuko", "cost": 1, "role": "APTank", "traits": ["Sprykin", "Brawler"]},
            ],
            "traits": [],
        }
    ],
    "items": [
        {
            "apiName": "TFT_Item_WarmogsArmor", "name": "Warmog's Armor",
            "composition": ["TFT_Item_GiantsBelt", "TFT_Item_GiantsBelt"],
            "effects": {"BonusPercentHP": 0.18, "Health": 500.0}, "tags": ["{7ea41d13}", "Health"],
            "associatedTraits": [],
        },
        {
            "apiName": "TFT_Item_RabadonsDeathcap", "name": "Rabadon's Deathcap",
            "composition": ["TFT_Item_NeedlesslyLargeRod", "TFT_Item_NeedlesslyLargeRod"],
            "effects": {"AP": 55.0, "BonusDamage": 0.15, "{1543aa48}": 1.0}, "tags": ["{7ea41d13}", "AbilityPower"],
            "associatedTraits": [],
        },
    ],
}


def test_champion_role_is_preserved_exactly_and_null_stays_none() -> None:
    meta = parse_set_metadata(SET18_PAYLOAD, patch="latest")
    assert meta.champions["DA_18_Kobuko"].role == "APTank"
    assert meta.champions["DA_18_Elise"].role is None  # never guessed


def test_item_stat_effects_and_tags_drop_unresolved_hashes() -> None:
    items = parse_set_metadata(SET18_PAYLOAD, patch="latest").items
    assert items["TFT_Item_WarmogsArmor"].stat_effects == ("BonusPercentHP", "Health")
    assert items["TFT_Item_WarmogsArmor"].tags == ("Health",)
    assert items["TFT_Item_RabadonsDeathcap"].stat_effects == ("AP", "BonusDamage")
    assert items["TFT_Item_RabadonsDeathcap"].tags == ("AbilityPower",)
    assert items["TFT_Item_RabadonsDeathcap"].associated_traits == ()


# ---------------------------------------------------------------- item stats snapshot (DA_ namespace)

def _item(api, name, *, effects=None, tags=None, composition=None):
    return {"apiName": api, "name": name, "effects": effects or {}, "tags": tags or [], "composition": composition or []}


DA_BUNDLE = {
    "setData": [{"number": 18, "champions": [], "traits": []}],
    "items": [
        _item("TFT_Item_ChainVest", "Chain Vest", effects={"Armor": 20}, tags=["component"]),
        _item("TFT_Item_NegatronCloak", "Negatron Cloak", effects={"MagicResist": 20}, tags=["component"]),
        _item("TFT_Item_RecurveBow", "Recurve Bow", effects={"AS": 10}, tags=["component"]),
        _item("TFT_Item_TearOfTheGoddess", "Tear of the Goddess", effects={"Mana": 15}, tags=["component"]),
        _item("DA_Component_ChainVest", "Chain Vest", tags=["component"]),
        _item("DA_Component_NegatronCloak", "Negatron Cloak", tags=["component"]),
        _item("DA_Component_RecurveBow", "Recurve Bow", tags=["component"]),
        _item("DA_Component_TearOfTheGoddess", "Tear Of The Goddess", tags=["component"]),
        # Same name twice with identical stats (a Corrupted copy): unambiguous.
        _item("TFT_Item_GargoyleStoneplate", "Gargoyle Stoneplate", effects={"Armor": 25, "MagicResist": 25, "Health": 100},
              tags=["{7ea41d13}"], composition=["TFT_Item_ChainVest", "TFT_Item_NegatronCloak"]),
        _item("TFT_Item_CorruptedGargoyleStoneplate", "Gargoyle Stoneplate", effects={"Armor": 25, "MagicResist": 25, "Health": 100},
              composition=["TFT_Item_ChainVest", "TFT_Item_NegatronCloak"]),
        _item("DA_GargoyleStoneplate", "Gargoyle Stoneplate", tags=["{7ea41d13}", "{15b72700}"],
              composition=["DA_Component_ChainVest", "DA_Component_NegatronCloak"]),
        # Legacy api names: the display name is the bridge, not the id.
        _item("TFT_Item_RapidFireCannon", "Red Buff", effects={"AS": 10}, composition=["TFT_Item_RecurveBow", "TFT_Item_RecurveBow"]),
        _item("TFT_Item_RedBuff", "Sunfire Cape", effects={"Health": 250, "Armor": 20}),
        _item("DA_RedBuff", "Red Buff", tags=["AttackSpeed"], composition=["DA_Component_RecurveBow", "DA_Component_RecurveBow"]),
        # Two same-name candidates with different stats: ambiguous, no alias.
        _item("TFT_Item_BlueBuff", "Blue Buff", effects={"AP": 20, "Mana": 10}, composition=["TFT_Item_TearOfTheGoddess"] * 2),
        _item("TFT_Item_SeraphsEmbrace", "Blue Buff", effects={"Mana": 15}, composition=["TFT_Item_TearOfTheGoddess"] * 2),
        _item("DA_BlueBuff", "Blue Buff", tags=["Mana"], composition=["DA_Component_TearOfTheGoddess"] * 2),
        # Same name but contradicting components: rejected.
        _item("TFT_Item_OddOne", "Odd One", effects={"Health": 300}, composition=["TFT_Item_ChainVest", "TFT_Item_ChainVest"]),
        _item("DA_OddOne", "Odd One", composition=["DA_Component_RecurveBow", "DA_Component_RecurveBow"]),
        # Emblem: no readable stats; augment-like entry with Health: excluded from the snapshot.
        _item("DA_18_EmblemSlayer", "Ravager Emblem", tags=["{ebcd1bac}"], composition=["DA_Component_ChainVest", "DA_Component_NegatronCloak"]),
        _item("DA_Hugify18", "Hugify", effects={"Health": 100}),
        _item("DA_18_YordleSpirit", "Yordle Spirit", effects={"DodgeChance": 1}),
    ],
}


def test_snapshot_keeps_exact_da_ids_with_verified_aliases_only() -> None:
    from tftlab.cdragon import item_stats_snapshot

    items = item_stats_snapshot(parse_set_metadata(DA_BUNDLE, patch="latest"))["items"]
    gargoyle = items["DA_GargoyleStoneplate"]
    assert gargoyle["alias_of"] == ["TFT_Item_CorruptedGargoyleStoneplate", "TFT_Item_GargoyleStoneplate"]
    assert gargoyle["stat_effects"] == ["Armor", "Health", "MagicResist"] and gargoyle["own_stat_effects"] == []
    red = items["DA_RedBuff"]
    assert red["alias_of"] == ["TFT_Item_RapidFireCannon"] and red["stat_effects"] == ["AS"]  # not TFT_Item_RedBuff
    assert items["DA_BlueBuff"]["alias_of"] == [] and items["DA_BlueBuff"]["tags"] == ["Mana"]  # ambiguous
    assert items["DA_OddOne"]["alias_of"] == []  # components contradict
    assert items["DA_18_EmblemSlayer"]["stat_effects"] == [] and items["DA_18_EmblemSlayer"]["tags"] == []
    assert items["DA_Component_ChainVest"]["tags"] == ["component"]
    assert "DA_Hugify18" not in items and "DA_18_YordleSpirit" not in items  # augments stay out
