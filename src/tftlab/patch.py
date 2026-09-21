from __future__ import annotations

import re

_VERSION_RE = re.compile(r"Version\s+(\d+)\.(\d+)")


def patch_from_game_version(game_version: str | None) -> str | None:
    """Extract a normalized `major.minor` patch key from a Riot `game_version` string.

    Riot's raw string looks like `"Version 14.6.579.1234 (...) [PUBLIC] <...>"`;
    we key analytics on the `14.6` prefix so patch filtering groups by balance
    patch rather than by exact build. When the string doesn't match that shape
    (e.g. deterministic demo data uses `"Version DEMO"`), the raw string is used
    as-is so a dataset still gets a single, consistent patch bucket instead of
    silently falling into an unfiltered "no patch" state.
    """
    if not game_version:
        return None
    match = _VERSION_RE.search(game_version)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return game_version.strip() or None
