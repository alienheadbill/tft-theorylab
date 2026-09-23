from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnrealPatchCutover:
    """One TFT client patch's deployment window during the "Unreal" era,
    when Riot Match-V1 returns a masked `game_version` (e.g. `"TFT Unreal
    Version ?.?.?.?"`) with no parseable major.minor version at all.

    `starts_at` is the epoch-millisecond `game_datetime` at which this
    patch began being served; a match resolves to the *last* entry whose
    `starts_at` is `<=` its own `game_datetime` (entries need not be
    contiguous or exhaustive -- a timestamp before every entry, or in a gap
    nothing covers, simply doesn't resolve). Kept as an entirely separate
    registry from `tftlab.balance_window.BALANCE_WINDOW_REGISTRY` on
    purpose: this one answers "which *client patch*" (18.2 vs 18.3); that
    one then answers "which half of that patch" for mid-patch balance
    updates. Merging the two concepts would make it impossible to reason
    about either independently.

    `verified` distinguishes an entry backed by an authoritative source
    (Riot's own patch notes or status announcement -- see `source`) from a
    provisional placeholder someone added without confirming the exact
    timestamp. `resolve_unreal_patch` uses unverified entries the same as
    verified ones (rejecting them would defeat the point of recording a
    provisional value at all), but `tftlab.validate`'s diagnostics surface
    `verified=False` prominently so nobody mistakes a guess for a
    confirmed cutover.
    """

    client_patch: str
    starts_at: int
    verified: bool
    source: str | None = None


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
# deserves.
#
# To fill this in: run `tftlab validate-live-data` against production
# (see its "raw game_version distribution" / "earliest/latest
# game_datetime" diagnostic output, added alongside this registry) to
# read off the actual masked-Unreal match timestamp range, cross-reference
# that against Riot's published patch notes for the real deployment dates
# of 18.2/18.3/etc., then add entries here, oldest first, e.g.:
#
#   UnrealPatchCutover(
#       client_patch="18.2",
#       starts_at=<epoch_ms>,
#       verified=True,
#       source="<link to Riot's patch notes or status announcement>",
#   ),
#
# Until an entry covers a given masked match's game_datetime, resolution
# deliberately returns UNRESOLVED_UNREAL_PATCH rather than guessing.
UNREAL_PATCH_REGISTRY: tuple[UnrealPatchCutover, ...] = ()

#: Returned by `resolve_unreal_patch` (and therefore
#: `tftlab.patch.patch_from_game_version`) when a masked Unreal
#: `game_version` is recognized but no registry entry covers its
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
    """Whether `game_version` is Riot's masked Unreal-era placeholder
    rather than a normal, parseable `"Version X.Y...."` string."""
    return bool(game_version) and "unreal" in game_version.lower()


def resolve_unreal_patch(
    game_datetime: int | None,
    *,
    registry: tuple[UnrealPatchCutover, ...] | None = None,
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

    Returns `UNRESOLVED_UNREAL_PATCH` when `game_datetime` is missing or
    falls before every registered cutover (or the registry is empty) --
    never the raw masked string, and never a guess.
    """
    if registry is None:
        registry = UNREAL_PATCH_REGISTRY
    if game_datetime is None or not registry:
        return UNRESOLVED_UNREAL_PATCH

    resolved = UNRESOLVED_UNREAL_PATCH
    for rule in sorted(registry, key=lambda r: r.starts_at):
        if game_datetime >= rule.starts_at:
            resolved = rule.client_patch
        else:
            break
    return resolved
