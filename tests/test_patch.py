from pathlib import Path

import pytest

from tftlab.analytics import available_balance_windows, carry_commitment_stats, default_balance_window
from tftlab.balance_window import resolve_balance_window
from tftlab.patch import patch_from_game_version, patch_sort_key
from tftlab.storage import Database

from _helpers import make_match, make_unit

# The registered 18.2 mid-patch cutover, and timestamps either side of it.
CUTOFF_MS = 1_789_344_000_000  # 2026-09-14T00:00:00Z
BEFORE_CUTOFF_MS = 1_789_000_000_000  # 2026-09-10
AFTER_CUTOFF_MS = 1_790_000_000_000  # 2026-09-21


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Version 14.6.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/14.6>", "14.6"),
        ("Version 13.24.123.4567", "13.24"),
        (None, None),
        ("", None),
        ("Version DEMO", "Version DEMO"),  # no digits to parse -> use raw string
    ],
)
def test_patch_from_game_version(raw: str | None, expected: str | None) -> None:
    assert patch_from_game_version(raw) == expected


@pytest.mark.parametrize(
    ("smaller", "larger"),
    [
        ("18.9", "18.10"),
        ("18.9a", "18.9b"),
        ("18.2", "18.10"),
        (None, "18.1"),
    ],
)
def test_patch_sort_key_orders_numerically_not_lexicographically(
    smaller: str | None, larger: str
) -> None:
    # Plain string comparison gets "18.9" vs "18.10" backwards; the numeric
    # key must not.
    assert patch_sort_key(smaller) < patch_sort_key(larger)
    assert sorted([larger, smaller], key=patch_sort_key) == [smaller, larger]


def test_resolve_balance_window_splits_on_registered_cutover() -> None:
    assert resolve_balance_window("18.2", BEFORE_CUTOFF_MS) == "18.2a"
    assert resolve_balance_window("18.2", CUTOFF_MS) == "18.2b"  # cutover instant counts as "after"
    assert resolve_balance_window("18.2", AFTER_CUTOFF_MS) == "18.2b"


def test_resolve_balance_window_is_unsuffixed_for_unregistered_patches() -> None:
    assert resolve_balance_window("18.3", BEFORE_CUTOFF_MS) == "18.3"
    assert resolve_balance_window("18.3", AFTER_CUTOFF_MS) == "18.3"


def test_resolve_balance_window_handles_missing_input() -> None:
    assert resolve_balance_window(None, AFTER_CUTOFF_MS) is None
    assert resolve_balance_window("18.2", None) == "18.2"  # no timestamp -> can't split, use client patch


def _seed_two_windows(db: Database) -> None:
    # Balance window 14.5: 3 matches, "OldCarry" commits and hits every time.
    for i in range(3):
        db.ingest_match(
            make_match(
                f"P145_{i}",
                game_version="Version 14.5.500.1 (Aug 1 2024) [PUBLIC] <Releases/14.5>",
                game_datetime=BEFORE_CUTOFF_MS,
                placement=1,
                units=[
                    make_unit(
                        "TFT14_OldCarry",
                        rarity=0,
                        tier=3,
                        items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"],
                    )
                ],
            )
        )

    # Balance window 14.6: 5 matches (more than 14.5's 3) but chronologically
    # earlier data shouldn't matter for match COUNT alone winning the default.
    for i in range(5):
        db.ingest_match(
            make_match(
                f"P146_{i}",
                game_version="Version 14.6.600.1 (Sep 10 2024) [PUBLIC] <Releases/14.6>",
                game_datetime=AFTER_CUTOFF_MS,
                placement=8,
                units=[
                    make_unit(
                        "TFT14_NewCarry",
                        rarity=0,
                        tier=2,
                        items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"],
                    )
                ],
            )
        )


def test_available_balance_windows_and_default(tmp_path: Path) -> None:
    with Database(tmp_path / "windows.sqlite3") as db:
        _seed_two_windows(db)
        windows = available_balance_windows(db)
        counts = {w: n for w, n, _ in windows}
        assert counts == {"14.5": 3, "14.6": 5}
        # 14.6 is both more-played AND chronologically latest here, so this
        # alone doesn't prove which one the ordering is keying off of --
        # test_default_prefers_latest_over_most_played below isolates that.
        assert default_balance_window(db) == "14.6"


def test_default_prefers_latest_over_most_played(tmp_path: Path) -> None:
    """An older balance window with far more matches must NOT win the default."""
    with Database(tmp_path / "latest_wins.sqlite3") as db:
        # 20 old matches on 14.5, chronologically earliest.
        for i in range(20):
            db.ingest_match(
                make_match(
                    f"OLD_{i}",
                    game_version="Version 14.5.500.1 (Aug 1 2024) [PUBLIC] <Releases/14.5>",
                    game_datetime=BEFORE_CUTOFF_MS,
                    units=[make_unit("TFT14_OldCarry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )
        # Just 1 new match on 14.6, chronologically latest.
        db.ingest_match(
            make_match(
                "NEW_0",
                game_version="Version 14.6.600.1 (Sep 10 2024) [PUBLIC] <Releases/14.6>",
                game_datetime=AFTER_CUTOFF_MS,
                units=[make_unit("TFT14_NewCarry", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
            )
        )

        windows = available_balance_windows(db)
        counts = {w: n for w, n, _ in windows}
        assert counts["14.5"] == 20
        assert counts["14.6"] == 1

        assert default_balance_window(db) == "14.6"
        default_stats = carry_commitment_stats(db, min_samples=1)
    assert {s.character_id for s in default_stats} == {"TFT14_NewCarry"}


def test_carry_commitment_stats_does_not_mix_balance_windows(tmp_path: Path) -> None:
    with Database(tmp_path / "windows.sqlite3") as db:
        _seed_two_windows(db)

        old_window_stats = carry_commitment_stats(db, balance_window="14.5", min_samples=1)
        new_window_stats = carry_commitment_stats(db, balance_window="14.6", min_samples=1)

    old_ids = {s.character_id for s in old_window_stats}
    new_ids = {s.character_id for s in new_window_stats}

    assert old_ids == {"TFT14_OldCarry"}
    assert new_ids == {"TFT14_NewCarry"}

    # OldCarry hit 3-star every game on 14.5; that must not bleed into 14.6's
    # results even though both windows are in the same database.
    old_carry = old_window_stats[0]
    assert old_carry.hit_3star_rate == 1.0
    assert old_carry.commitment_games == 3

    new_carry = new_window_stats[0]
    assert new_carry.hit_3star_rate == 0.0
    assert new_carry.commitment_games == 5


def test_mid_patch_cutover_isolates_matches_within_one_client_patch(tmp_path: Path) -> None:
    """18.2a and 18.2b share a client patch but must not be blended."""
    with Database(tmp_path / "cutover.sqlite3") as db:
        # Before the cutover (18.2a): "PreHotfix" is strong.
        for i in range(4):
            db.ingest_match(
                make_match(
                    f"PRE_{i}",
                    game_version="Version 18.2.100.1 (Sep 1 2026) [PUBLIC] <Releases/18.2>",
                    game_datetime=BEFORE_CUTOFF_MS,
                    placement=1,
                    units=[make_unit("TFT18_PreHotfix", tier=3, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )
        # After the cutover (18.2b): same client patch, but a rebalanced
        # "PostHotfix" carry appears instead.
        for i in range(4):
            db.ingest_match(
                make_match(
                    f"POST_{i}",
                    game_version="Version 18.2.150.1 (Sep 16 2026) [PUBLIC] <Releases/18.2>",
                    game_datetime=AFTER_CUTOFF_MS,
                    placement=8,
                    units=[make_unit("TFT18_PostHotfix", tier=1, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])],
                )
            )

        windows = {w for w, _, _ in available_balance_windows(db)}
        assert windows == {"18.2a", "18.2b"}

        pre_stats = carry_commitment_stats(db, balance_window="18.2a", min_samples=1)
        post_stats = carry_commitment_stats(db, balance_window="18.2b", min_samples=1)
        resolved_default = default_balance_window(db)  # chronologically latest

    assert {s.character_id for s in pre_stats} == {"TFT18_PreHotfix"}
    assert {s.character_id for s in post_stats} == {"TFT18_PostHotfix"}
    assert resolved_default == "18.2b"
