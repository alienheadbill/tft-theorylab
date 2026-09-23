"""Tests for Unreal-era masked-`game_version` patch resolution.

Riot Match-V1 started returning a masked placeholder game_version (e.g.
`"TFT Unreal Version ?.?.?.?"`) with no parseable major.minor version for
some matches. `tftlab.unreal_patch` resolves the real client patch from
`game_datetime` via an explicit, timestamp-based registry instead --
entirely separate from `tftlab.balance_window`'s mid-patch registry (that
one splits an already-known client patch; this one determines the client
patch in the first place).

All timestamps used here are fabricated, clearly-labeled test fixtures --
NOT real Riot patch deployment dates. The production `UNREAL_PATCH_REGISTRY`
is intentionally empty pending external verification (see that module).
"""

from pathlib import Path

from tftlab.balance_window import resolve_balance_window
from tftlab.patch import patch_from_game_version
from tftlab.storage import Database
from tftlab.unreal_patch import (
    UNRESOLVED_UNREAL_PATCH,
    UnrealPatchCutover,
    is_masked_unreal_version,
    resolve_unreal_patch,
)

from _helpers import make_match, make_unit

MASKED_VERSION = "TFT Unreal Version ?.?.?.?"

# Fabricated, test-only cutover timestamps -- not real Riot patch dates.
PATCH_18_2_STARTS = 1_000_000
PATCH_18_3_STARTS = 2_000_000
FAKE_REGISTRY = (
    UnrealPatchCutover(client_patch="18.2", starts_at=PATCH_18_2_STARTS, verified=False, source="test fixture"),
    UnrealPatchCutover(client_patch="18.3", starts_at=PATCH_18_3_STARTS, verified=False, source="test fixture"),
)


def test_is_masked_unreal_version() -> None:
    assert is_masked_unreal_version(MASKED_VERSION) is True
    assert is_masked_unreal_version("Version 14.6.579.1234 (Sep 10 2024) [PUBLIC] <Releases/14.6>") is False
    assert is_masked_unreal_version("Version DEMO") is False
    assert is_masked_unreal_version(None) is False
    assert is_masked_unreal_version("") is False


def test_legacy_parseable_version_strings_are_unaffected() -> None:
    """Requirement: normal parseable Riot version strings must resolve
    exactly as before, whether or not a game_datetime is also supplied."""
    raw = "Version 14.6.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/14.6>"
    assert patch_from_game_version(raw) == "14.6"
    assert patch_from_game_version(raw, game_datetime=PATCH_18_3_STARTS) == "14.6"
    assert patch_from_game_version("Version DEMO", game_datetime=PATCH_18_3_STARTS) == "Version DEMO"


def test_masked_unreal_string_resolved_by_timestamp() -> None:
    resolved = resolve_unreal_patch(PATCH_18_2_STARTS, registry=FAKE_REGISTRY)
    assert resolved == "18.2"
    assert patch_from_game_version(MASKED_VERSION) != resolved  # production registry is empty by default


def test_multiple_unreal_patches_resolve_separately() -> None:
    assert resolve_unreal_patch(PATCH_18_2_STARTS, registry=FAKE_REGISTRY) == "18.2"
    assert resolve_unreal_patch(PATCH_18_2_STARTS + 500, registry=FAKE_REGISTRY) == "18.2"
    assert resolve_unreal_patch(PATCH_18_3_STARTS, registry=FAKE_REGISTRY) == "18.3"
    assert resolve_unreal_patch(PATCH_18_3_STARTS + 999, registry=FAKE_REGISTRY) == "18.3"


def test_resolution_is_deterministic_regardless_of_registry_order() -> None:
    """Given the same game_datetime, resolution must always return the same
    client patch -- including when the registry isn't already sorted."""
    shuffled = (FAKE_REGISTRY[1], FAKE_REGISTRY[0])
    assert resolve_unreal_patch(PATCH_18_2_STARTS + 100, registry=shuffled) == "18.2"
    assert resolve_unreal_patch(PATCH_18_3_STARTS + 100, registry=shuffled) == "18.3"


def test_unknown_unreal_timestamp_remains_unresolved() -> None:
    # Before the earliest registered cutover.
    assert resolve_unreal_patch(PATCH_18_2_STARTS - 1, registry=FAKE_REGISTRY) == UNRESOLVED_UNREAL_PATCH
    # Missing timestamp entirely.
    assert resolve_unreal_patch(None, registry=FAKE_REGISTRY) == UNRESOLVED_UNREAL_PATCH
    # Empty registry (production's current real state).
    assert resolve_unreal_patch(PATCH_18_2_STARTS, registry=()) == UNRESOLVED_UNREAL_PATCH
    # Never the raw masked string, whatever happens.
    assert resolve_unreal_patch(None, registry=FAKE_REGISTRY) != MASKED_VERSION


def test_mid_patch_balance_split_still_works_after_client_patch_resolution(tmp_path: Path) -> None:
    """Once a masked match resolves to a real client patch (e.g. "18.2"),
    tftlab.balance_window's existing, separate mid-patch registry must still
    be able to split it into 18.2a/18.2b -- the two registries compose."""
    resolved_patch = resolve_unreal_patch(PATCH_18_2_STARTS, registry=FAKE_REGISTRY)
    assert resolved_patch == "18.2"

    # 18.2's real mid-patch cutover, from tftlab.balance_window's own registry.
    before_mid_patch_cutover = 1_789_000_000_000
    after_mid_patch_cutover = 1_790_000_000_000
    assert resolve_balance_window(resolved_patch, before_mid_patch_cutover) == "18.2a"
    assert resolve_balance_window(resolved_patch, after_mid_patch_cutover) == "18.2b"


def test_ingest_of_masked_unreal_match_with_unresolved_registry(tmp_path: Path) -> None:
    """End-to-end: ingesting a masked-Unreal match against the real
    (currently empty) production registry must land it in the explicit
    unresolved state, never a fake shared patch/balance-window bucket."""
    with Database(tmp_path / "unreal.sqlite3") as db:
        db.ingest_match(
            make_match(
                "UNREAL_1",
                game_version=MASKED_VERSION,
                game_datetime=1_790_000_000_000,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        row = db.query_one("SELECT patch, balance_window FROM matches WHERE match_id = ?", ("UNREAL_1",))

    assert row == (UNRESOLVED_UNREAL_PATCH, None)


def test_two_unresolved_unreal_matches_do_not_share_a_fake_bucket(tmp_path: Path) -> None:
    """The exact bug this milestone fixes: two matches from genuinely
    different (unknown) real patches must never collapse into one shared
    balance_window just because both have a masked game_version."""
    with Database(tmp_path / "unreal_multi.sqlite3") as db:
        db.ingest_match(
            make_match(
                "UNREAL_EARLY",
                game_version=MASKED_VERSION,
                game_datetime=1_000,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        db.ingest_match(
            make_match(
                "UNREAL_LATE",
                game_version=MASKED_VERSION,
                game_datetime=9_999_999,
                units=[make_unit("TFT18_Foo", tier=2, items=[])],
            )
        )
        windows = db.query_all("SELECT balance_window FROM matches")

    # Neither is silently assigned a shared window -- both are None.
    assert windows == [(None,), (None,)]
