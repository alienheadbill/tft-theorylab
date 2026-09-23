from __future__ import annotations

import re
from dataclasses import dataclass

#: Riot's masked Unreal-era placeholder version shape: four literal `?`
#: segments where a major.minor.build.revision number would normally be
#: (e.g. `"TFT Unreal Version ?.?.?.?"`). Matching this specific shape --
#: not just the word "unreal" -- means a *genuinely parseable* string that
#: happens to mention Unreal (e.g. a future `"TFT Unreal Version
#: 18.3.1234"`) is treated as a normal version string, not sent through
#: timestamp-based resolution; see `patch.patch_from_game_version`, which
#: always tries a normal numeric parse first regardless of this check.
_MASKED_PLACEHOLDER_RE = re.compile(r"\?\.\?\.\?\.\?")


@dataclass(frozen=True)
class UnrealPatchWindow:
    """One TFT client patch's deployment window during the "Unreal" era,
    when Riot Match-V1 returns a masked `game_version` (e.g. `"TFT Unreal
    Version ?.?.?.?"`) with no parseable major.minor version at all.

    `starts_at`/`ends_at` are epoch-millisecond `game_datetime` bounds,
    inclusive/exclusive respectively: a match resolves to this window only
    when `starts_at <= game_datetime < ends_at`. Both bounds are required
    and explicit -- there is no "open-ended latest patch" concept. If a
    patch is still current and its real end date isn't known yet, the
    registry maintainer must still pick an explicit `ends_at` (e.g. a
    deliberately far-future placeholder), rather than the resolver ever
    inferring "extends forever" on its own just because no later window is
    registered yet -- that inference is exactly the kind of confident
    wrong guess this module exists to avoid. A timestamp before every
    window, in a gap between two windows, or after every window's `ends_at`
    simply doesn't resolve (see `resolve_unreal_patch`).

    Kept as an entirely separate registry from
    `tftlab.balance_window.BALANCE_WINDOW_REGISTRY` on purpose: this one
    answers "which *client patch*" (18.2 vs 18.3); that one then answers
    "which half of that patch" for mid-patch balance updates. Merging the
    two concepts would make it impossible to reason about either
    independently.

    `verified` distinguishes an entry backed by an authoritative source
    (Riot's own patch notes or status announcement -- see `source`) from a
    provisional placeholder someone added without confirming the exact
    timestamps. Unlike an earlier version of this module, `verified=False`
    is not merely a cosmetic flag: `resolve_unreal_patch` (used for all
    production classification) skips unverified entries entirely, and a
    verified entry without a `source` is treated as unverified too, since
    "verified" with nothing to verify against isn't meaningful. Tests may
    still construct a fabricated window to exercise resolution -- they
    just need to mark it `verified=True` with a `source` (e.g. `"test
    fixture"`) to do so, the same as any other caller.
    """

    client_patch: str
    starts_at: int
    ends_at: int
    verified: bool
    source: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether this window may actually be used to classify a match.

        Requires both `verified=True` and a non-empty `source`: a
        "verified" entry with no source recorded isn't actually backed by
        anything a reader could check, so it's treated the same as an
        unverified one.
        """
        return self.verified and bool(self.source)


# NEEDS VERIFICATION -- this registry is currently EMPTY.
#
# Riot Match-V1 started returning a masked game_version ("TFT Unreal
# Version ?.?.?.?") for some window of live matches, with no parseable
# major.minor version at all. Resolving which real client patch (18.2,
# 18.3, ...) a masked match belongs to requires an explicit,
# timestamp-based registry, since the version string itself carries no
# usable information anymore.
#
# This sandbox's network egress cannot reach Riot's patch notes / status
# endpoints to confirm real deployment timestamps (the same restriction
# that has blocked raw.communitydragon.org and
# static.developer.riotgames.com throughout this project), and no
# fabricated timestamp belongs here -- an invented cutover would silently
# mislabel real production matches with more confidence than the data
# deserves. Even if an entry were added without verification, it would be
# silently ignored by resolve_unreal_patch (see UnrealPatchWindow.is_usable)
# rather than affect production classification.
#
# To fill this in: run `tftlab patch-diagnostics` (or `validate-live-data`)
# against production -- see its raw game_version distribution and
# earliest/latest game_datetime output -- to read off the actual
# masked-Unreal match timestamp range, cross-reference that against Riot's
# published patch notes for the real deployment dates of 18.2/18.3/etc.,
# then add entries here, e.g.:
#
#   UnrealPatchWindow(
#       client_patch="18.2",
#       starts_at=<epoch_ms patch 18.2 deployed>,
#       ends_at=<epoch_ms patch 18.3 deployed>,  # exclusive; required
#       verified=True,
#       source="<link to Riot's patch notes or status announcement>",
#   ),
#
# Until a verified, sourced entry covers a given masked match's
# game_datetime, resolution deliberately returns UNRESOLVED_UNREAL_PATCH
# rather than guessing.
UNREAL_PATCH_REGISTRY: tuple[UnrealPatchWindow, ...] = ()

#: Returned by `resolve_unreal_patch` (and therefore
#: `tftlab.patch.patch_from_game_version`) when a masked Unreal
#: `game_version` is recognized but no usable registry window covers its
#: `game_datetime`. Distinct from `None` ("this string isn't Unreal-masked
#: at all") so callers -- and `tftlab.validate` -- can tell "we don't
#: classify this kind of string" apart from "we know this needs an
#: Unreal-era patch but don't have one registered yet". Never a plausible-
#: looking patch string and never the raw masked `game_version`: both of
#: those would let every unrelated unresolved match silently collapse into
#: one shared fake bucket, which is exactly the bug this module exists to
#: fix.
UNRESOLVED_UNREAL_PATCH = "unreal-unresolved"


def is_masked_unreal_version(game_version: str | None) -> bool:
    """Whether `game_version` is Riot's masked Unreal-era placeholder --
    specifically the literal `"?.?.?.?"` version-number shape -- rather
    than a normal, parseable `"Version X.Y...."` string.

    Deliberately checks the placeholder shape, not just the word "unreal":
    `patch.patch_from_game_version` always tries a normal numeric parse
    first, but this function is also used standalone (e.g. by the storage
    migration), so it must not itself misclassify a hypothetical future,
    genuinely parseable `"TFT Unreal Version 18.3.1234"` as masked.
    """
    return bool(game_version) and bool(_MASKED_PLACEHOLDER_RE.search(game_version))


def resolve_unreal_patch(
    game_datetime: int | None,
    *,
    registry: tuple[UnrealPatchWindow, ...] | None = None,
) -> str:
    """The client patch for a masked-Unreal match, purely from
    `game_datetime` and `registry` -- deterministic (the same timestamp
    always resolves the same way, forever) and independent of wall-clock
    time (never "today's patch"; `registry` is the only input besides
    `game_datetime` itself).

    `registry` defaults to the real, module-level `UNREAL_PATCH_REGISTRY`,
    looked up fresh on every call (not bound as a function-default value)
    specifically so that adding real cutover entries to that registry --
    the whole point of this module -- takes effect immediately, including
    for already-open `Database` connections re-running
    `_backfill_unreal_patches` on their next reconnect, with no import
    reordering or restart-sensitive caching to worry about.

    Only windows where `UnrealPatchWindow.is_usable` is true (verified,
    with a source) are ever consulted -- an unverified or sourceless entry
    is treated exactly as if it weren't in the registry at all, whether
    that registry is the real production one or one a test injected.

    Returns `UNRESOLVED_UNREAL_PATCH` when `game_datetime` is missing, or
    doesn't fall within any usable window's `[starts_at, ends_at)` range
    (before the earliest window, in a gap between two windows, or after
    the latest window's `ends_at`) -- never the raw masked string, and
    never a guess.
    """
    if registry is None:
        registry = UNREAL_PATCH_REGISTRY
    if game_datetime is None:
        return UNRESOLVED_UNREAL_PATCH

    for window in registry:
        if not window.is_usable:
            continue
        if window.starts_at <= game_datetime < window.ends_at:
            return window.client_patch
    return UNRESOLVED_UNREAL_PATCH
