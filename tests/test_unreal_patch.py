"""Tests for Unreal-era masked-`game_version` patch resolution.

Riot Match-V1 started returning a masked placeholder game_version (e.g.
`"TFT Unreal Version ?.?.?.?"`) with no parseable major.minor version for
some matches. `tftlab.unreal_patch` resolves the real client patch from
`game_datetime` via an explicit, bounded, timestamp-based registry instead
-- entirely separate from `tftlab.balance_window`'s mid-patch registry
(that one splits an already-known client patch; this one determines the
client patch in the first place).

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
    UnrealPatchWindow,
    is_masked_unreal_version,
    resolve_unreal_patch,
)

from _helpers import make_match, make_unit

MASKED_VERSION = "TFT Unreal Version ?.?.?.?"

# Fabricated, test-only cutover timestamps -- not real Riot patch dates.
PATCH_18_2_STARTS = 1_000_000
PATCH_18_2_ENDS = 2_000_000  # == PATCH_18_3_STARTS: contiguous, no gap
PATCH_18_3_STARTS = 2_000_000
PATCH_18_3_ENDS = 3_000_000

VERIFIED_REGISTRY = (
    UnrealPatchWindow(
        client_patch="18.2",
        starts_at=PATCH_18_2_STARTS,
        ends_at=PATCH_18_2_ENDS,
        verified=True,
        source="test fixture",
    ),
    UnrealPatchWindow(
        client_patch="18.3",
        starts_at=PATCH_18_3_STARTS,
        ends_at=PATCH_18_3_ENDS,
        verified=True,
        source="test fixture",
    ),
)

# Same timestamps, but with a real gap between 18.2 and 18.3 -- used for the
# gap-must-stay-unresolved test.
GAP_REGISTRY = (
    UnrealPatchWindow(
        client_patch="18.2", starts_at=1_000_000, ends_at=1_500_000, verified=True, source="test fixture"
    ),
    UnrealPatchWindow(
        client_patch="18.3", starts_at=2_500_000, ends_at=3_000_000, verified=True, source="test fixture"
    ),
)


def test_is_masked_unreal_version_matches_only_the_placeholder_shape() -> None:
    assert is_masked_unreal_version(MASKED_VERSION) is True
    assert is_masked_unreal_version("Version 14.6.579.1234 (Sep 10 2024) [PUBLIC] <Releases/14.6>") is False
    assert is_masked_unreal_version("Version DEMO") is False
    assert is_masked_unreal_version(None) is False
    assert is_masked_unreal_version("") is False
    # The word "Unreal" alone, without the "?.?.?.?" shape, is NOT masked --
    # this is the exact bug this milestone fixes: a genuinely parseable
    # future string must never be misclassified just for mentioning Unreal.
    assert is_masked_unreal_version("TFT Unreal Version 18.3.1234") is False


# --- Requirement 1: numeric parse always wins first ------------------------


def test_legacy_parseable_version_string_unchanged() -> None:
    raw = "Version 18.3.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/18.3>"
    assert patch_from_game_version(raw) == "18.3"


def test_parseable_unreal_branded_string_is_parsed_normally_not_masked() -> None:
    """If Riot ever ships a genuinely parseable "TFT Unreal Version
    18.3.1234", it must resolve via the normal numeric parse, never get
    routed into timestamp-based resolution just because it mentions
    Unreal."""
    assert patch_from_game_version("TFT Unreal Version 18.3.1234") == "18.3"
    # Even with a game_datetime that wouldn't resolve in the registry at all
    # -- proving it never even reached resolve_unreal_patch.
    assert patch_from_game_version("TFT Unreal Version 18.3.1234", game_datetime=999_999_999_999) == "18.3"


def test_genuinely_masked_string_still_goes_through_timestamp_registry() -> None:
    resolved = patch_from_game_version(MASKED_VERSION, game_datetime=PATCH_18_2_STARTS)
    # Production registry is empty, so this can't resolve to a real patch --
    # but it must take the Unreal path (sentinel), not the raw-string path.
    assert resolved == UNRESOLVED_UNREAL_PATCH
    assert resolved != MASKED_VERSION


# --- Requirement 2: bounded windows, no infinite-latest-patch -------------


def test_before_earliest_window_is_unresolved() -> None:
    assert resolve_unreal_patch(PATCH_18_2_STARTS - 1, registry=VERIFIED_REGISTRY) == UNRESOLVED_UNREAL_PATCH


def test_inside_first_window_resolves() -> None:
    assert resolve_unreal_patch(PATCH_18_2_STARTS, registry=VERIFIED_REGISTRY) == "18.2"
    assert resolve_unreal_patch(PATCH_18_2_STARTS + 500, registry=VERIFIED_REGISTRY) == "18.2"
    # ends_at is exclusive.
    assert resolve_unreal_patch(PATCH_18_2_ENDS - 1, registry=VERIFIED_REGISTRY) == "18.2"


def test_gap_between_windows_is_unresolved() -> None:
    """The core bug being fixed: a timestamp in a gap the registry doesn't
    cover must never inherit the nearest earlier (or later) patch."""
    assert resolve_unreal_patch(1_750_000, registry=GAP_REGISTRY) == UNRESOLVED_UNREAL_PATCH


def test_inside_second_window_resolves() -> None:
    assert resolve_unreal_patch(PATCH_18_3_STARTS, registry=VERIFIED_REGISTRY) == "18.3"
    assert resolve_unreal_patch(PATCH_18_3_ENDS - 1, registry=VERIFIED_REGISTRY) == "18.3"


def test_after_latest_window_is_unresolved() -> None:
    """The exact regression this milestone fixes: the newest registered
    patch must NOT extend forever just because nothing later is registered."""
    assert resolve_unreal_patch(PATCH_18_3_ENDS, registry=VERIFIED_REGISTRY) == UNRESOLVED_UNREAL_PATCH
    assert resolve_unreal_patch(PATCH_18_3_ENDS + 999_999_999, registry=VERIFIED_REGISTRY) == UNRESOLVED_UNREAL_PATCH


def test_missing_game_datetime_is_unresolved() -> None:
    assert resolve_unreal_patch(None, registry=VERIFIED_REGISTRY) == UNRESOLVED_UNREAL_PATCH


def test_empty_registry_is_unresolved() -> None:
    assert resolve_unreal_patch(PATCH_18_2_STARTS, registry=()) == UNRESOLVED_UNREAL_PATCH


# --- Requirement 3: verified+sourced entries only --------------------------


def test_unverified_window_does_not_resolve_a_timestamp_it_would_otherwise_cover() -> None:
    unverified = (
        UnrealPatchWindow(
            client_patch="18.2", starts_at=1_000_000, ends_at=2_000_000, verified=False, source="guess, not confirmed"
        ),
    )
    assert resolve_unreal_patch(1_500_000, registry=unverified) == UNRESOLVED_UNREAL_PATCH


def test_verified_window_without_a_source_does_not_resolve() -> None:
    """`verified=True` with no source recorded isn't actually backed by
    anything checkable -- treated the same as unverified."""
    no_source = (
        UnrealPatchWindow(client_patch="18.2", starts_at=1_000_000, ends_at=2_000_000, verified=True, source=None),
    )
    assert resolve_unreal_patch(1_500_000, registry=no_source) == UNRESOLVED_UNREAL_PATCH
    empty_source = (
        UnrealPatchWindow(client_patch="18.2", starts_at=1_000_000, ends_at=2_000_000, verified=True, source=""),
    )
    assert resolve_unreal_patch(1_500_000, registry=empty_source) == UNRESOLVED_UNREAL_PATCH


def test_unverified_window_is_ignored_even_alongside_a_usable_one() -> None:
    """An unverified entry must not shadow or interfere with a genuinely
    usable one elsewhere in the same registry."""
    mixed = (
        UnrealPatchWindow(
            client_patch="18.2", starts_at=1_000_000, ends_at=2_000_000, verified=False, source="guess"
        ),
        UnrealPatchWindow(
            client_patch="18.3", starts_at=2_000_000, ends_at=3_000_000, verified=True, source="test fixture"
        ),
    )
    assert resolve_unreal_patch(1_500_000, registry=mixed) == UNRESOLVED_UNREAL_PATCH
    assert resolve_unreal_patch(2_500_000, registry=mixed) == "18.3"


def test_production_registry_currently_has_no_usable_entries() -> None:
    """Locks in the documented, intentional current state: the shipped
    UNREAL_PATCH_REGISTRY must not silently classify production matches
    until real, verified entries are added."""
    assert resolve_unreal_patch(1_790_000_000_000) == UNRESOLVED_UNREAL_PATCH


# --- Composition with the (separate) mid-patch balance-window registry ----


def test_mid_patch_balance_split_still_works_after_client_patch_resolution(tmp_path: Path) -> None:
    """Once a masked match resolves to a real client patch (e.g. "18.2"),
    tftlab.balance_window's existing, separate mid-patch registry must still
    be able to split it into 18.2a/18.2b -- the two registries compose."""
    resolved_patch = resolve_unreal_patch(PATCH_18_2_STARTS, registry=VERIFIED_REGISTRY)
    assert resolved_patch == "18.2"

    # 18.2's real mid-patch cutover, from tftlab.balance_window's own registry.
    before_mid_patch_cutover = 1_789_000_000_000
    after_mid_patch_cutover = 1_790_000_000_000
    assert resolve_balance_window(resolved_patch, before_mid_patch_cutover) == "18.2a"
    assert resolve_balance_window(resolved_patch, after_mid_patch_cutover) == "18.2b"


# --- End-to-end ingest behavior --------------------------------------------


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
