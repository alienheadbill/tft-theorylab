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
    """One TFT client patch's **trusted classification window** during the
    "Unreal" era, when Riot Match-V1 returns a masked `game_version` (e.g.
    `"TFT Unreal Version ?.?.?.?"`) with no parseable major.minor version
    at all.

    Deliberately named a "classification window", not a "deployment
    window": `starts_at`/`ends_at` are NOT a claim about the exact second
    Riot flipped the patch live, nor about exactly when every region
    received it. They mark a conservative interval this codebase is
    confident belongs entirely to one patch, derived from Riot's public
    patch schedule with a safety margin on both ends. Real rollouts are
    messy -- a patch can ship late, early, or region-by-region (see the
    real 18.2/18.3 case this was built for: NA reportedly received 18.3
    roughly a day early) -- and a window here is intentionally drawn to
    exclude that whole ambiguous rollout period rather than guess which
    side of it a given match falls on. Losing a small number of matches to
    "unresolved" during a patch-day transition is the intended, accepted
    cost: it is strictly better than silently mixing two different balance
    states into one bucket.

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
    simply doesn't resolve (see `resolve_unreal_patch`) -- gaps are a
    deliberate, load-bearing feature of this design, not an oversight: the
    space between two windows is exactly where real-world rollout
    ambiguity is meant to land.

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


# Riot Match-V1 started returning a masked game_version ("TFT Unreal
# Version ?.?.?.?") for some window of live matches, with no parseable
# major.minor version at all. Resolving which real client patch (18.2,
# 18.3, ...) a masked match belongs to requires an explicit,
# timestamp-based registry, since the version string itself carries no
# usable information anymore.
#
# These two windows are deliberately CONSERVATIVE CLASSIFICATION windows,
# not exact deployment windows -- see UnrealPatchWindow's docstring. Source
# for both: Riot's official TFT patch schedule,
# https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics/
# (18.2 scheduled 2026-09-10, 18.3 scheduled 2026-09-23 Pacific Time, 18.4
# scheduled 2026-10-07). Community reports say NA actually received 18.3
# roughly a day early (~2026-09-22); that entire ambiguous transition
# period is deliberately EXCLUDED from both windows below, left as a gap
# that resolves to UNRESOLVED_UNREAL_PATCH -- this sandbox has no way to
# confirm the exact real rollout second for either region or patch, so it
# does not guess one. A handful of matches from the patch-day transition
# staying unresolved is the accepted, correct cost of never mixing two
# different balance states into one bucket.
#
#   18.2 window: 2026-09-11T00:00:00Z .. 2026-09-22T00:00:00Z (exclusive)
#     Starts a full day after the scheduled 2026-09-10 release (well past
#     any normal same-day rollout); ends before the earliest reported NA
#     18.3 sighting on 2026-09-22, so it can't bleed into the transition.
#   18.3 window: 2026-09-24T07:00:00Z .. 2026-10-06T00:00:00Z (exclusive)
#     Starts after the full scheduled 2026-09-23 Pacific-Time release day
#     has elapsed everywhere in that timezone (2026-09-24T07:00:00Z is
#     2026-09-23T24:00:00 -07:00, i.e. midnight PDT rolling into the 24th);
#     ends before the next scheduled patch (18.4) on 2026-10-07.
#
# Anything in the gap between these two windows (2026-09-22T00:00:00Z
# through 2026-09-24T07:00:00Z) -- exactly the reported early-NA-18.3
# transition period -- remains UNRESOLVED_UNREAL_PATCH by design.
#
# To extend this as new patches ship: read off the actual masked-Unreal
# match timestamp range via `tftlab patch-diagnostics` (or
# `validate-live-data`), cross-reference Riot's published patch schedule,
# and add another conservative window following the same pattern -- margin
# in from both the previous patch's scheduled end and this patch's
# scheduled start, wide enough to exclude the rollout transition, backed
# by the schedule URL as `source`.
UNREAL_PATCH_REGISTRY: tuple[UnrealPatchWindow, ...] = (
    UnrealPatchWindow(
        client_patch="18.2",
        starts_at=1_789_084_800_000,  # 2026-09-11T00:00:00Z
        ends_at=1_790_035_200_000,  # 2026-09-22T00:00:00Z (exclusive)
        verified=True,
        source="https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics/",
    ),
    UnrealPatchWindow(
        client_patch="18.3",
        starts_at=1_790_233_200_000,  # 2026-09-24T07:00:00Z
        ends_at=1_791_244_800_000,  # 2026-10-06T00:00:00Z (exclusive)
        verified=True,
        source="https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics/",
    ),
)

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
