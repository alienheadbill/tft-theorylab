from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BalanceWindowRule:
    """One client patch's mid-patch balance cutovers.

    `cutoffs` are epoch-millisecond `game_datetime` boundaries, sorted
    ascending, at which a new in-game balance state took effect without the
    client's major.minor version changing. A patch absent from the registry
    (or with no cutoffs) has exactly one balance window: the client patch
    itself, unsuffixed.
    """

    client_patch: str
    cutoffs: tuple[int, ...]


# Known mid-patch balance updates that didn't bump the client's major.minor
# version. Add an entry here as each one is identified; resolution below is
# timestamp-based, so historical matches stay correctly classifiable no
# matter when a registry entry is added.
BALANCE_WINDOW_REGISTRY: tuple[BalanceWindowRule, ...] = (
    # 2026-09-14T00:00:00Z mid-patch balance update within client patch 18.2.
    BalanceWindowRule(client_patch="18.2", cutoffs=(1_789_344_000_000,)),
)

_REGISTRY_BY_PATCH = {rule.client_patch: rule for rule in BALANCE_WINDOW_REGISTRY}


def resolve_balance_window(client_patch: str | None, game_datetime: int | None) -> str | None:
    """The balance window a match belongs to, given its client patch and timestamp.

    Generic by design, not special-cased per patch at the call site: a client
    patch with no registered cutovers simply *is* its own balance window. One
    with N registered cutovers splits into N+1 windows, suffixed a, b, c, ...
    in chronological order (e.g. `18.2a` before the cutover, `18.2b` after).
    """
    if not client_patch:
        return None

    rule = _REGISTRY_BY_PATCH.get(client_patch)
    if rule is None or not rule.cutoffs or game_datetime is None:
        return client_patch

    index = sum(1 for cutoff in rule.cutoffs if game_datetime >= cutoff)
    suffix = chr(ord("a") + index)
    return f"{client_patch}{suffix}"
