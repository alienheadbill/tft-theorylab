from __future__ import annotations

import re

from .unreal_patch import is_masked_unreal_version, resolve_unreal_patch

_VERSION_RE = re.compile(r"Version\s+(\d+)\.(\d+)")


def patch_from_game_version(game_version: str | None, game_datetime: int | None = None) -> str | None:
    """Extract a normalized `major.minor` client patch key from a Riot `game_version` string.

    Riot's raw string looks like `"Version 14.6.579.1234 (...) [PUBLIC] <...>"`;
    we key on the `14.6` prefix rather than the exact build. This is the
    *client* patch only -- it does not account for mid-patch balance updates
    that don't bump the client version (see `tftlab.balance_window`), which
    is what analytics actually groups by.

    Riot has also started returning a masked "Unreal"-era placeholder
    (`"TFT Unreal Version ?.?.?.?"`) with no parseable version at all, for
    some matches. A normal numeric `Version X.Y` is always tried FIRST,
    regardless of whether the string also happens to mention "Unreal" --
    a hypothetical future `"TFT Unreal Version 18.3.1234"` must resolve to
    `"18.3"` via the normal parse below, not get routed into timestamp
    resolution just because of the word "Unreal". Only when no numeric
    version can be parsed AND the string matches the actual masked
    placeholder shape (literal `"?.?.?.?"` -- see
    `tftlab.unreal_patch.is_masked_unreal_version`) is it resolved from
    `game_datetime` via `tftlab.unreal_patch.resolve_unreal_patch` instead
    (see that module -- this can return `UNRESOLVED_UNREAL_PATCH` when
    `game_datetime` doesn't fall in any registered window, which callers
    must handle explicitly, not treat as a normal patch string).

    For any other unparseable shape (e.g. deterministic demo data uses
    `"Version DEMO"`), the raw string is used as-is so a dataset still gets
    a single, consistent patch bucket instead of silently falling into an
    unfiltered "no patch" state.
    """
    if not game_version:
        return None
    match = _VERSION_RE.search(game_version)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    if is_masked_unreal_version(game_version):
        return resolve_unreal_patch(game_datetime)
    return game_version.strip() or None


_SORT_KEY_RE = re.compile(r"(\d+)\.(\d+)(.*)")


def patch_sort_key(value: str | None) -> tuple[int, int, str]:
    """A numerically-comparable sort key for a client patch or balance window.

    Sorting by this key (ascending) puts `18.10` after `18.9`, which plain
    string comparison gets backwards (`"18.10" < "18.9"` lexicographically).
    Any trailing balance-window suffix (e.g. the `b` in `18.2b`) sorts after
    the unsuffixed/`a` form of the same `major.minor`. Values that don't
    start with `major.minor` (e.g. `"Version DEMO"`) sort before everything
    real, so they never masquerade as the "latest" patch.
    """
    if not value:
        return (-1, -1, "")
    match = _SORT_KEY_RE.match(value)
    if not match:
        return (-1, -1, value)
    major, minor, suffix = match.groups()
    return (int(major), int(minor), suffix)
