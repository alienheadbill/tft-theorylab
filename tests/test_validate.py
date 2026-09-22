from pathlib import Path

import pytest

from tftlab.cdragon import ChampionMeta, ItemMeta, SetMetadata, TraitMeta
from tftlab.storage import Database
from tftlab.validate import validate_live_data

from _helpers import make_match, make_unit


def _fake_metadata() -> SetMetadata:
    return SetMetadata(
        patch="14.6",
        set_number=14,
        champions={
            "TFT14_Foo": ChampionMeta(character_id="TFT14_Foo", name="Foo", cost=2, icon_url=None),
        },
        items={
            "TFT_Item_BlueBuff": ItemMeta(item_id="TFT_Item_BlueBuff", name="Blue Buff", icon_url=None),
            "TFT_Item_Deathcap": ItemMeta(item_id="TFT_Item_Deathcap", name="Deathcap", icon_url=None),
        },
        traits={
            "Juggernaut": TraitMeta(trait_id="Juggernaut", name="Juggernaut", icon_url=None),
        },
    )


def test_clean_data_reports_no_severe_issues(tmp_path: Path) -> None:
    with Database(tmp_path / "clean.sqlite3") as db:
        for i in range(5):
            db.ingest_match(
                make_match(
                    f"CLEAN_{i}",
                    units=[make_unit("TFT14_Foo", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                    traits=[{"name": "Juggernaut", "num_units": 4, "style": 3, "tier_current": 2, "tier_total": 3}],
                )
            )
        report = validate_live_data(db, metadata=_fake_metadata())

    assert report.total_matches == 5
    assert report.unit_cost_present_pct == 1.0
    assert report.metadata_champion_coverage_pct == 1.0
    assert report.unknown_champion_ids == []
    assert report.unknown_item_ids == []
    assert report.unknown_trait_ids == []
    assert report.matches_missing_balance_window == 0
    assert report.malformed_placements == 0
    assert report.duplicate_match_ids == 0
    assert report.participants_without_units == 0
    assert report.is_severe is False


def test_unknown_ids_are_skipped_not_falsely_clean_without_metadata(tmp_path: Path) -> None:
    with Database(tmp_path / "no_metadata.sqlite3") as db:
        db.ingest_match(
            make_match("M1", units=[make_unit("TFT14_Unknown", tier=2, items=["TFT_Item_SomeUnknownItem"])])
        )
        report = validate_live_data(db, metadata=None)

    assert report.unknown_champion_ids is None
    assert report.unknown_item_ids is None
    assert report.unknown_trait_ids is None
    assert report.metadata_champion_coverage_pct is None
    # Not checking IDs is not itself a severe failure.
    assert report.is_severe is False


def test_unknown_champion_and_item_ids_detected_with_metadata(tmp_path: Path) -> None:
    with Database(tmp_path / "unknown.sqlite3") as db:
        db.ingest_match(
            make_match(
                "M1",
                # rarity=9 is out of the 0-4 fallback range, so cost stays
                # None -- and this character_id isn't in the fake metadata.
                units=[make_unit("TFT14_NotInMetadata", rarity=9, tier=2, items=["TFT_Item_NotInMetadata"])],
            )
        )
        report = validate_live_data(db, metadata=_fake_metadata())

    assert report.unknown_champion_ids == ["TFT14_NotInMetadata"]
    assert report.unknown_item_ids == ["TFT_Item_NotInMetadata"]
    assert report.unit_cost_present_pct == 0.0
    assert report.metadata_champion_coverage_pct == 0.0
    # Unknown IDs and low cost resolution are data-quality warnings, not
    # structural corruption.
    assert report.is_severe is False


def test_unknown_champion_with_fallback_cost_is_still_unknown(tmp_path: Path) -> None:
    """Regression test: `normalize.cost_from_unit` falls back to `rarity + 1`
    whenever `cost_lookup` (CommunityDragon) returns `None` for a champion,
    so a champion CommunityDragon has never heard of can still end up with a
    non-null `cost`. `unknown_champion_ids` must be derived from every
    observed champion, never from `cost IS NULL`, and
    `metadata_champion_coverage_pct` must not claim 100% just because a
    fallback cost was present."""
    metadata = _fake_metadata()
    with Database(tmp_path / "fallback_cost.sqlite3") as db:
        db.ingest_match(
            make_match(
                "M1",
                # rarity=1 is in the 0-4 fallback range, so cost_from_unit
                # falls back to rarity + 1 = 2 even though this champion
                # isn't in CommunityDragon's metadata at all.
                units=[make_unit("TFT14_NotInMetadata", rarity=1, tier=1, items=[])],
            ),
            cost_lookup=metadata.cost_for_champion,
        )
        report = validate_live_data(db, metadata=metadata)

    assert report.unknown_champion_ids == ["TFT14_NotInMetadata"]
    assert report.unit_cost_present_pct == 1.0
    assert report.metadata_champion_coverage_pct == 0.0
    assert report.is_severe is False


def test_malformed_placement_is_severe(tmp_path: Path) -> None:
    with Database(tmp_path / "malformed.sqlite3") as db:
        db.ingest_match(
            make_match("BAD_PLACEMENT", placement=99, units=[make_unit("TFT14_Foo", tier=2, items=[])])
        )
        report = validate_live_data(db)

    assert report.malformed_placements == 1
    assert report.is_severe is True


def test_matches_missing_balance_window_is_severe(tmp_path: Path) -> None:
    with Database(tmp_path / "no_window.sqlite3") as db:
        # A match with no game_version at all can't be classified into any
        # balance window.
        db.ingest_match(
            make_match("NO_VERSION", game_version=None, units=[make_unit("TFT14_Foo", tier=2, items=[])])
        )
        report = validate_live_data(db, balance_window="doesnt-matter")

    assert report.matches_missing_balance_window == 1
    assert report.is_severe is True


def test_participants_without_units_is_severe(tmp_path: Path) -> None:
    with Database(tmp_path / "no_units.sqlite3") as db:
        db.ingest_match(make_match("NO_UNITS", units=[]))
        report = validate_live_data(db)

    assert report.participants_without_units == 1
    assert report.is_severe is True


def test_reports_none_data_when_no_balance_window_resolvable(tmp_path: Path) -> None:
    with Database(tmp_path / "empty.sqlite3") as db:
        report = validate_live_data(db)

    assert report.balance_window is None
    assert report.total_matches == 0
    assert report.total_participants == 0
    assert report.is_severe is False
