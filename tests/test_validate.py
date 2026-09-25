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
    assert report.target_queue_matches == 5
    assert report.non_target_queue_matches == 0
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
    """A NULL game_version -> NULL patch is genuinely broken data, not the
    intentional Unreal rollout-gap exclusion, so it must count as
    unexpected and stay severe."""
    with Database(tmp_path / "no_window.sqlite3") as db:
        # A match with no game_version at all can't be classified into any
        # balance window.
        db.ingest_match(
            make_match("NO_VERSION", game_version=None, units=[make_unit("TFT14_Foo", tier=2, items=[])])
        )
        report = validate_live_data(db, balance_window="doesnt-matter")

    assert report.matches_missing_balance_window == 1
    assert report.unexpected_missing_balance_window == 1
    assert report.is_severe is True


def test_source_empty_participant_is_a_warning_not_severe(tmp_path: Path) -> None:
    """Riot itself sent `units: []`: storage is faithful, so this warns."""
    with Database(tmp_path / "no_units.sqlite3") as db:
        db.ingest_match(make_match("NO_UNITS", units=[]))
        report = validate_live_data(db)

    assert report.participants_without_units == 1
    assert report.source_empty_participants == 1
    assert report.unexpected_participants_without_units == 0
    assert report.is_severe is False


def test_reports_none_data_when_no_balance_window_resolvable(tmp_path: Path) -> None:
    with Database(tmp_path / "empty.sqlite3") as db:
        report = validate_live_data(db)

    assert report.balance_window is None
    assert report.total_matches == 0
    assert report.target_queue_matches == 0
    assert report.non_target_queue_matches == 0
    assert report.total_participants == 0
    assert report.is_severe is False


def test_queue_distribution_reports_non_target_matches_without_deleting_them(tmp_path: Path) -> None:
    """A partially-completed live run can leave non-ranked matches already
    committed to production (e.g. a Challenger PUUID's Hyper Roll game
    fetched before queue filtering existed). validate-live-data must make
    that visible, not silently fold it into "total matches" or delete it."""
    with Database(tmp_path / "queues.sqlite3") as db:
        for i in range(3):
            db.ingest_match(make_match(f"RANKED_{i}", units=[make_unit("TFT14_Foo", tier=2, items=[])]))
        for i in range(2):
            db.ingest_match(
                make_match(f"NORMAL_{i}", queue_id=1090, units=[make_unit("TFT14_Foo", tier=2, items=[])])
            )
        report = validate_live_data(db)
        # Visibility only -- validate-live-data must never delete rows itself.
        stored_matches = db.query_one("SELECT COUNT(*) FROM matches")[0]

    assert report.total_matches == 5
    assert report.target_queue_matches == 3
    assert report.non_target_queue_matches == 2
    assert stored_matches == 5


def test_intentional_unreal_unresolved_match_reported_but_not_severe(tmp_path: Path) -> None:
    """A masked-Unreal match whose game_datetime falls in the Unreal
    registry's deliberate 18.2/18.3 rollout gap (see tftlab.unreal_patch)
    is intentionally, safely excluded from balance-window-scoped analytics
    -- it must still be surfaced prominently (matches_missing_balance_window
    / unresolved_unreal_matches), but must NOT make validation fail. A
    healthy production database full of nothing but these rows must end
    "No severe integrity issues detected.", not a false alarm."""
    with Database(tmp_path / "unreal.sqlite3") as db:
        db.ingest_match(
            make_match(
                "UNREAL_1",
                game_version="TFT Unreal Version ?.?.?.?",
                game_datetime=1_790_121_600_000,  # 2026-09-23T00:00:00Z: in the gap
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        report = validate_live_data(db)

    assert report.unresolved_unreal_matches == 1
    assert report.matches_missing_balance_window == 1
    assert report.unexpected_missing_balance_window == 0
    assert report.is_severe is False


def test_diagnostic_distributions_are_store_wide_not_window_scoped(tmp_path: Path) -> None:
    """The game_version/patch/balance_window distributions and timestamp
    range must stay useful even when nothing resolves into a balance
    window at all -- that's exactly the situation an unresolved Unreal-era
    dataset is in, and the whole reason these diagnostics exist."""
    with Database(tmp_path / "diagnostics.sqlite3") as db:
        db.ingest_match(
            make_match(
                "UNREAL_1",
                game_version="TFT Unreal Version ?.?.?.?",
                game_datetime=1_000,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        db.ingest_match(
            make_match(
                "UNREAL_2",
                game_version="TFT Unreal Version ?.?.?.?",
                game_datetime=5_000,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        report = validate_live_data(db)

    # No balance window resolves at all, yet the diagnostics still work.
    assert report.balance_window is None
    assert report.game_version_distribution == {"TFT Unreal Version ?.?.?.?": 2}
    assert report.client_patch_distribution == {"unreal-unresolved": 2}
    assert report.balance_window_distribution == {None: 2}
    assert report.earliest_game_datetime == 1_000
    assert report.latest_game_datetime == 5_000


def test_mixed_expected_and_unexpected_missing_balance_windows_reports_correct_counts(
    tmp_path: Path,
) -> None:
    """A production-realistic mix: some matches intentionally unresolved in
    the Unreal rollout gap (safe), one genuinely broken row with no
    game_version at all (not safe). matches_missing_balance_window must
    count both; unexpected_missing_balance_window must count only the
    genuinely broken one; is_severe must be True because of that one row,
    not the intentional ones."""
    with Database(tmp_path / "mixed.sqlite3") as db:
        for i in range(3):
            db.ingest_match(
                make_match(
                    f"UNREAL_{i}",
                    game_version="TFT Unreal Version ?.?.?.?",
                    game_datetime=1_790_121_600_000 + i,  # in the registry's deliberate gap
                    units=[make_unit("TFT18_Foo", tier=2, items=[])],
                )
            )
        db.ingest_match(
            make_match("BROKEN_1", game_version=None, units=[make_unit("TFT14_Foo", tier=2, items=[])])
        )
        report = validate_live_data(db, balance_window="doesnt-matter")

    assert report.matches_missing_balance_window == 4
    assert report.unresolved_unreal_matches == 3
    assert report.unexpected_missing_balance_window == 1
    assert report.is_severe is True
