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
        # Known special items (outside the role-recommendation domain) and an ordinary unlisted item.
        _item("TFT_Item_TacticiansScepter", "Tactician's Shield", composition=["TFT_Item_ChainVest", "TFT_Item_ChainVest"]),
        _item("DA_TacticiansShield", "Tacticians Shield", composition=["DA_Component_ChainVest", "DA_Component_ChainVest"]),
        _item("TFT_Item_ThiefsGloves", "Thief's Gloves", composition=["TFT_Item_RecurveBow", "TFT_Item_RecurveBow"]),
        _item("DA_ThiefsGloves", "Thief's Gloves", composition=["DA_Component_RecurveBow", "DA_Component_RecurveBow"]),
        _item("TFT_Item_NightHarvester", "Steadfast Heart", effects={"Armor": 20, "CritChance": 20},
              composition=["TFT_Item_ChainVest", "TFT_Item_RecurveBow"]),
        _item("DA_SteadfastHeart", "Steadfast Heart", composition=["DA_Component_ChainVest", "DA_Component_RecurveBow"]),
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


# ---------------------------------------------------------------- Riot item intent (TFTCharacterRoleData)

def _role(name, items, *, revamped=None, legacy="TFT_CharacterRole_Champ_ADCarry_Name"):
    role = {"__type": "TFTCharacterRoleData", "name": name, "CharacterRoleNameTra": legacy,
            "items": [f"Maps/Shipping/Map22/Items/{i}" if not i.startswith("{") else i for i in items]}
    if revamped:  # current UI name key, under hashed field names as Riot ships it
        role["{a1ad92a7}"] = role["{0116bd9b}"] = f"TFT_CharacterRole_RolesRevamped_{revamped}_Name"
        role["{886be411}"] = f"TFT_CharacterRole_RolesRevamped_{revamped}_Description"
    return role


ROLE_MAP_BIN = {
    "{2ccf900f}": _role("APTank", ["TFT_Item_GargoyleStoneplate", "TFT_Item_WarmogsArmor"], revamped="APTank"),
    "{47bfb556}": _role("HTank", ["TFT_Item_OddOne"], revamped="HTank"),
    "{34ed6daa}": _role("ADCarry", ["TFT_Item_RapidFireCannon", "TFT_Item_BlueBuff", "TFT_Item_OddOne"], revamped="ADMarksman"),
    "{7009cb66}": _role("ADCarryCrit", ["TFT_Item_RedBuff"]),  # legacy: no current UI name
    "{4334cab4}": _role("TutorialADCarry", ["{9b3faced}"]),
    "{afc39260}": {"__type": "TftItemData", "mName": "TFT_Item_Unrelated"},
}
# Riot item records (map22 TftItemData): "{7ea41d13}" is the tag every
# ordinary completed item carries; "Resistance"/"Mana" are ordinary extras.
ORDINARY = "{7ea41d13}"
for _i, (_name, _tags) in enumerate({
    "TFT_Item_GargoyleStoneplate": [ORDINARY, "Resistance"], "TFT_Item_CorruptedGargoyleStoneplate": [ORDINARY, "Resistance"],
    "DA_GargoyleStoneplate": [ORDINARY, "Resistance"], "TFT_Item_WarmogsArmor": [ORDINARY, "Health"],
    "TFT_Item_RapidFireCannon": [ORDINARY], "DA_RedBuff": [ORDINARY], "TFT_Item_RedBuff": [ORDINARY, "Health"],
    "TFT_Item_BlueBuff": [ORDINARY, "Mana"], "TFT_Item_SeraphsEmbrace": [ORDINARY, "Mana"], "DA_BlueBuff": [ORDINARY, "Mana"],
    "TFT_Item_OddOne": [ORDINARY], "DA_OddOne": [ORDINARY],
    "DA_18_EmblemSlayer": ["TraitItem"],
    # special items: known, resolvable, but outside the recommendation domain
    "TFT_Item_TacticiansScepter": ["{ec243f6b}", "TacticiansItem"], "DA_TacticiansShield": ["{ec243f6b}", "TacticiansItem"],
    "TFT_Item_ThiefsGloves": [ORDINARY, "{2905e581}"], "DA_ThiefsGloves": [ORDINARY, "{2905e581}"],
    "TFT_Item_NightHarvester": [ORDINARY], "DA_SteadfastHeart": [ORDINARY],
}.items()):
    ROLE_MAP_BIN[f"{{item{_i}}}"] = {"__type": "TftItemData", "mName": _name, "ItemTags": _tags}
ROLE_STRINGS = {"entries": {
    "tft_characterrole_rolesrevamped_aptank_name": "Magic Tank",
    "tft_characterrole_rolesrevamped_htank_name": "Hybrid Tank",
    "tft_characterrole_rolesrevamped_admarksman_name": "Attack Marksman",
}}


def test_riot_roles_extracts_role_objects_ui_names_and_families() -> None:
    from tftlab.cdragon import riot_roles

    roles = riot_roles(ROLE_MAP_BIN, ROLE_STRINGS)
    assert set(roles) == {"APTank", "HTank", "ADCarry", "ADCarryCrit", "TutorialADCarry"}
    assert roles["APTank"]["ui_name"] == "Magic Tank" and roles["APTank"]["family"] == "tank"
    assert roles["HTank"]["family"] == "tank"
    assert roles["ADCarry"]["ui_name"] == "Attack Marksman" and roles["ADCarry"]["family"] == "non_tank"
    assert roles["ADCarry"]["recommended_items"] == ["TFT_Item_RapidFireCannon", "TFT_Item_BlueBuff", "TFT_Item_OddOne"]
    assert roles["ADCarryCrit"]["family"] is None and roles["ADCarryCrit"]["ui_name_key"] is None  # legacy: no evidence
    assert roles["TutorialADCarry"]["unresolved_items"] == ["{9b3faced}"]
    assert roles["APTank"]["object"] == "{2ccf900f}"


@pytest.mark.parametrize("bad, strings", [
    ({"{1}": _role("APHerald", [], revamped="APHerald")}, {"entries": {"tft_characterrole_rolesrevamped_apherald_name": "Magic Herald"}}),
    ({"{1}": _role("APTank", [], revamped="APTank")}, {"entries": {}}),  # UI string missing
    ({"{1}": _role("X", []), "{2}": _role("X", [])}, {"entries": {}}),  # duplicate role names
])
def test_unclassifiable_roles_fail_loudly(bad, strings) -> None:
    from tftlab.cdragon import riot_roles

    with pytest.raises(ValueError):
        riot_roles(bad, strings)


def test_item_intent_snapshot_derives_intent_from_riot_recommendations_with_evidence() -> None:
    from tftlab.cdragon import item_intent_snapshot

    snap = item_intent_snapshot(parse_set_metadata(DA_BUNDLE, patch="latest"), ROLE_MAP_BIN, ROLE_STRINGS)
    items = snap["items"]
    assert snap["set_number"] == 18 and set(snap["roles"]) == {"APTank", "HTank", "ADCarry", "ADCarryCrit", "TutorialADCarry"}
    gargoyle = items["DA_GargoyleStoneplate"]  # every same-name copy shares the evidence
    assert gargoyle == {
        "name": "Gargoyle Stoneplate",
        "riot_items": ["TFT_Item_CorruptedGargoyleStoneplate", "TFT_Item_GargoyleStoneplate"],
        "recommended_by_tank_roles": ["APTank"], "recommended_by_non_tank_roles": [],
        "riot_item_tags": ["Resistance", "{7ea41d13}"], "in_recommendation_domain": True, "intent": "tank",
    }
    assert items["TFT_Item_GargoyleStoneplate"]["intent"] == "tank"
    assert items["TFT_Item_CorruptedGargoyleStoneplate"]["intent"] == "known_unlisted"  # own id: not recommended
    red = items["DA_RedBuff"]  # "Red Buff" is TFT_Item_RapidFireCannon, not TFT_Item_RedBuff
    assert red["riot_items"] == ["TFT_Item_RapidFireCannon"] and red["intent"] == "damage"
    assert red["recommended_by_non_tank_roles"] == ["ADCarry"]
    assert items["DA_BlueBuff"]["riot_items"] == ["TFT_Item_BlueBuff", "TFT_Item_SeraphsEmbrace"]
    assert items["DA_BlueBuff"]["intent"] == "damage"
    odd = items["TFT_Item_OddOne"]
    assert (odd["recommended_by_tank_roles"], odd["recommended_by_non_tank_roles"], odd["intent"]) == (["HTank"], ["ADCarry"], "mixed")
    assert items["DA_OddOne"]["riot_items"] == [] and items["DA_OddOne"]["intent"] == "unknown"  # components contradict
    assert items["DA_18_EmblemSlayer"]["riot_items"] == [] and items["DA_18_EmblemSlayer"]["intent"] == "unknown"
    assert items["TFT_Item_RedBuff"]["intent"] == "known_unlisted"  # only a legacy role lists it: no evidence
    assert snap["recommendation_domain"] == {
        "required_item_tags": ["{7ea41d13}"],  # shared by every role-recommended item
        "allowed_item_tags": ["Health", "Mana", "Resistance", "{7ea41d13}"],
    }
    steadfast = items["DA_SteadfastHeart"]  # ordinary completed item, recommended by no role
    assert (steadfast["riot_items"], steadfast["in_recommendation_domain"], steadfast["intent"]) == (
        ["TFT_Item_NightHarvester"], True, "known_unlisted")
    for special in ("DA_TacticiansShield", "TFT_Item_TacticiansScepter", "DA_ThiefsGloves", "TFT_Item_ThiefsGloves"):
        meta = items[special]  # resolvable and known, but outside the domain: absence says nothing
        assert meta["riot_items"] and meta["in_recommendation_domain"] is False and meta["intent"] == "unknown", special
    assert items["DA_ThiefsGloves"]["riot_item_tags"] == ["{2905e581}", "{7ea41d13}"]  # ordinary tag + one no recommended item has
    assert items["DA_18_EmblemSlayer"]["in_recommendation_domain"] is False
    assert not any(i.startswith("DA_Component_") or i in ("TFT_Item_ChainVest", "TFT_Item_RecurveBow") for i in items)
    assert "DA_Hugify18" not in items  # augments stay out, as in the stat snapshot


def test_recommendation_domain_must_be_derivable_from_riot_item_records() -> None:
    from tftlab.cdragon import item_intent_snapshot

    meta = parse_set_metadata(DA_BUNDLE, patch="latest")
    no_record = {k: v for k, v in ROLE_MAP_BIN.items() if v.get("mName") != "TFT_Item_WarmogsArmor"}
    with pytest.raises(ValueError, match="without a TftItemData record"):
        item_intent_snapshot(meta, no_record, ROLE_STRINGS)
    no_shared = {k: ({**v, "ItemTags": [f"tag{k}"]} if v.get("__type") == "TftItemData" else v) for k, v in ROLE_MAP_BIN.items()}
    with pytest.raises(ValueError, match="share no Riot item tag"):
        item_intent_snapshot(meta, no_shared, ROLE_STRINGS)


def test_champion_role_coverage_counts_character_role_links() -> None:
    from tftlab.cdragon import champion_role_coverage

    bundle = {"items": [], "setData": [{"number": 18, "traits": [], "champions": [
        {"apiName": "DA_18_Leona", "name": "Leona", "cost": 1, "traits": ["Solar"]},
        {"apiName": "DA_18_Kobuko", "name": "Kobuko", "cost": 1, "traits": ["Yordle"]},
        {"apiName": "DA_18_Gone", "name": "Gone", "cost": 2, "traits": ["X"]},
        {"apiName": "DA_18_Sentry", "name": "Totem", "cost": 0, "traits": []},  # not a shop champion
    ]}]}
    meta = parse_set_metadata(bundle, patch="latest")
    records = {"DA_18_Leona": {"mCharacterName": "DA_18_Leona"}, "DA_18_Kobuko": {"CharacterRole": "{2ccf900f}"}, "DA_18_Gone": None}
    assert champion_role_coverage(meta, ROLE_MAP_BIN, records) == {
        "shop_champions": 3, "with_role": {"DA_18_Kobuko": "APTank"}, "missing_record": ["DA_18_Gone"],
    }


def test_fetch_game_json_reads_raw_game_exports_uncached(tmp_path) -> None:
    import httpx

    from tftlab.cdragon import MAP22_BIN_PATH, CommunityDragonClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=ROLE_MAP_BIN)

    with CommunityDragonClient(cache_dir=tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        assert client.fetch_game_json(MAP22_BIN_PATH) == ROLE_MAP_BIN
        client.fetch_game_json(MAP22_BIN_PATH)
    assert seen == ["https://raw.communitydragon.org/latest/game/data/maps/shipping/map22/map22.bin.json"] * 2
    assert not list(tmp_path.iterdir())  # nothing cached
