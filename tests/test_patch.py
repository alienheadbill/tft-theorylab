from pathlib import Path

import pytest

from tftlab.analytics import available_patches, carry_commitment_stats, default_patch
from tftlab.patch import patch_from_game_version
from tftlab.storage import Database

from _helpers import make_match, make_unit


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


def _seed_two_patches(db: Database) -> None:
    # Patch 14.5: 3 matches, "OldCarry" commits and hits every time.
    for i in range(3):
        db.ingest_match(
            make_match(
                f"P145_{i}",
                game_version="Version 14.5.500.1 (Aug 1 2024) [PUBLIC] <Releases/14.5>",
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

    # Patch 14.6: 5 matches (more than 14.5, so it becomes the default patch).
    # "NewCarry" only exists here; "OldCarry" is untouched by this patch's data.
    for i in range(5):
        db.ingest_match(
            make_match(
                f"P146_{i}",
                game_version="Version 14.6.600.1 (Sep 10 2024) [PUBLIC] <Releases/14.6>",
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


def test_available_patches_and_default(tmp_path: Path) -> None:
    with Database(tmp_path / "patches.sqlite3") as db:
        _seed_two_patches(db)
        patches = available_patches(db)
        assert dict(patches) == {"14.5": 3, "14.6": 5}
        # Most matches wins the default.
        assert default_patch(db) == "14.6"


def test_carry_commitment_stats_does_not_mix_patches(tmp_path: Path) -> None:
    with Database(tmp_path / "patches.sqlite3") as db:
        _seed_two_patches(db)

        old_patch_stats = carry_commitment_stats(db, patch="14.5", min_samples=1)
        new_patch_stats = carry_commitment_stats(db, patch="14.6", min_samples=1)
        default_stats = carry_commitment_stats(db, min_samples=1)

    old_ids = {s.character_id for s in old_patch_stats}
    new_ids = {s.character_id for s in new_patch_stats}

    assert old_ids == {"TFT14_OldCarry"}
    assert new_ids == {"TFT14_NewCarry"}

    # OldCarry hit 3-star every game on 14.5; that must not bleed into 14.6's
    # results even though both patches are in the same database.
    old_carry = old_patch_stats[0]
    assert old_carry.hit_3star_rate == 1.0
    assert old_carry.commitment_games == 3

    new_carry = new_patch_stats[0]
    assert new_carry.hit_3star_rate == 0.0
    assert new_carry.commitment_games == 5

    # Omitting `patch` must resolve to the most-played patch (14.6), not an
    # unfiltered blend of both.
    assert {s.character_id for s in default_stats} == new_ids
