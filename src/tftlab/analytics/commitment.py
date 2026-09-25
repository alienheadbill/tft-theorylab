from __future__ import annotations

from dataclasses import dataclass

from ..patch import patch_sort_key
from ..storage import Database


@dataclass(frozen=True)
class CarryStat:
    character_id: str
    name: str
    cost: int
    appearances: int
    commitment_games: int
    #: Deprecated alias for `commitment_rate`, kept for backward
    #: compatibility with existing API/CLI consumers. New code should read
    #: `appearance_rate`/`commitment_rate`/`carry_conversion_rate` instead of
    #: treating this single number as "the" rarity measure -- see those
    #: fields' docstrings below for why they're not interchangeable.
    usage_rate: float
    #: Champion pick/presence rate: appearances / unit-observable
    #: participants (see `carry_commitment_stats`). How often this champion
    #: shows up on a board at all, regardless of build.
    appearance_rate: float
    #: Carry-commitment rate: commitment_games / unit-observable
    #: participants. How often a board features THIS unit built as a
    #: >=2-item carry. This is what `usage_rate` has always actually measured.
    commitment_rate: float
    #: Of the games this champion appeared in at all, how often it was
    #: built as a committed carry: commitment_games / appearances. Distinct
    #: from the two rates above: a champion can be common (high
    #: appearance_rate) yet almost always committed when picked (high
    #: carry_conversion_rate), or rare (low appearance_rate) yet rarely
    #: converted into a real carry even then (low carry_conversion_rate).
    carry_conversion_rate: float
    avg_placement: float
    top4_rate: float
    win_rate: float
    hit_3star_rate: float
    hit_top4_rate: float | None
    miss_top4_rate: float | None
    #: Raw commitment-game counts behind hit_top4_rate/miss_top4_rate,
    #: exposed so callers can apply their own shrinkage to these (typically
    #: small) subgroups instead of treating the raw rates as fully reliable.
    hit_games: int
    miss_games: int
    avg_placement_hit: float | None
    avg_placement_miss: float | None
    posterior_top4: float
    confidence: float
    opportunity_score: float


def _posterior_rate(successes: int, attempts: int, *, prior: float, strength: float) -> float:
    return (successes + prior * strength) / (attempts + strength)


def available_balance_windows(db: Database) -> list[tuple[str, int, int]]:
    """All balance windows present in the store, chronologically latest first.

    Returns `(balance_window, match_count, latest_game_datetime)` tuples.
    Ordering is by each window's most recent match timestamp, with a
    numeric (not lexicographic) parse of the window string as a tiebreaker
    -- so e.g. `18.10` sorts after `18.9` even if their matches happen to
    share a timestamp, and match *count* never decides the order.
    """
    rows = db.query_all(
        """
        SELECT balance_window, COUNT(*) AS n, MAX(game_datetime) AS latest
        FROM matches
        WHERE balance_window IS NOT NULL
        GROUP BY balance_window
        """
    )
    windows = [(str(r[0]), int(r[1]), int(r[2] or 0)) for r in rows]
    windows.sort(key=lambda w: (w[2], patch_sort_key(w[0])), reverse=True)
    return windows


def default_balance_window(db: Database) -> str | None:
    """The balance window analytics should use when the caller doesn't pin one.

    Always the chronologically latest window in the store (by its matches'
    actual timestamps), never the one with the most samples -- an older
    window can easily have more matches without being the current balance
    state.
    """
    windows = available_balance_windows(db)
    return windows[0][0] if windows else None


def carry_commitment_stats(
    db: Database,
    *,
    balance_window: str | None = None,
    min_cost: int = 1,
    max_cost: int = 3,
    commitment_items: int = 2,
    min_samples: int = 3,
    prior_strength: float = 60.0,
) -> list[CarryStat]:
    """Return low-usage carry stats from final-board Match-V1 data.

    A participant is a commitment game for a unit when that unit finishes with
    >= `commitment_items` non-component items. This deliberately includes 2-star
    misses, avoiding the survivorship bias of analyzing only successful 3-stars.

    Results are always scoped to a single balance window: different windows
    (client patches, or mid-patch balance updates within the same client
    patch -- see `tftlab.balance_window`) can rebalance a champion or its
    items entirely, so mixing them by default would quietly blend unrelated
    data. When `balance_window` is omitted, the chronologically latest window
    in the store is used.
    """
    resolved_window = balance_window or default_balance_window(db)
    if resolved_window is None:
        return []

    # Denominator: UNIT-OBSERVABLE participants -- those with at least one
    # stored unit. A participant Riot sent with no units at all (a
    # "source-empty" board, see `tftlab.validate`) has no observable board,
    # so it can never contribute an appearance; counting it would quietly
    # deflate every champion's appearance/commitment rate. Its match,
    # placement and row stay stored and count everywhere else.
    total_participants_row = db.query_one(
        """
        SELECT COUNT(*)
        FROM participants p
        JOIN matches m ON m.match_id = p.match_id
        WHERE m.balance_window = ?
          AND EXISTS (
              SELECT 1 FROM units u
              WHERE u.match_id = p.match_id AND u.participant_index = p.participant_index
          )
        """,
        (resolved_window,),
    )
    total_participants = total_participants_row[0] if total_participants_row else 0
    if not total_participants:
        return []

    rows = db.query_all(
        """
        WITH per_champion AS (
            -- Collapse a participant's (possibly multiple) instances of one
            -- champion into a single row *before* aggregating across
            -- participants: a real board can field more than one instance of
            -- the same character_id (see `units.unit_index`), and without
            -- this step each duplicate copy would inflate appearances,
            -- commitment games, top4/win counts, etc. by counting the same
            -- participant/game more than once. `committed`/`hit_3star` are
            -- true if *any* instance of the champion in this game qualifies
            -- -- matching the "commit/hit if at least one instance does"
            -- semantics used throughout this module.
            SELECT
                u.match_id,
                u.participant_index,
                u.character_id,
                MAX(u.unit_name) AS unit_name,
                MAX(u.cost) AS cost,
                MAX(CASE WHEN u.completed_item_count >= ? THEN 1 ELSE 0 END) AS committed,
                MAX(CASE WHEN u.completed_item_count >= ? AND u.tier >= 3 THEN 1 ELSE 0 END) AS hit_3star
            FROM units u
            JOIN matches m ON m.match_id = u.match_id
            WHERE m.balance_window = ?
            GROUP BY u.match_id, u.participant_index, u.character_id
        )
        SELECT
            pc.character_id,
            MAX(pc.unit_name) AS unit_name,
            pc.cost,
            COUNT(*) AS appearances,
            SUM(pc.committed) AS commitment_games,
            AVG(CASE WHEN pc.committed = 1 THEN p.placement * 1.0 END) AS avg_place,
            SUM(CASE WHEN pc.committed = 1 AND p.placement <= 4 THEN 1 ELSE 0 END) AS top4s,
            SUM(CASE WHEN pc.committed = 1 AND p.placement = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pc.committed = 1 AND pc.hit_3star = 1 THEN 1 ELSE 0 END) AS hits,
            SUM(CASE WHEN pc.committed = 1 AND pc.hit_3star = 1 AND p.placement <= 4 THEN 1 ELSE 0 END) AS hit_top4s,
            AVG(CASE WHEN pc.committed = 1 AND pc.hit_3star = 1 THEN p.placement * 1.0 END) AS avg_place_hit,
            SUM(CASE WHEN pc.committed = 1 AND pc.hit_3star = 0 THEN 1 ELSE 0 END) AS misses,
            SUM(CASE WHEN pc.committed = 1 AND pc.hit_3star = 0 AND p.placement <= 4 THEN 1 ELSE 0 END) AS miss_top4s,
            AVG(CASE WHEN pc.committed = 1 AND pc.hit_3star = 0 THEN p.placement * 1.0 END) AS avg_place_miss
        FROM per_champion pc
        JOIN participants p
          ON p.match_id = pc.match_id AND p.participant_index = pc.participant_index
        WHERE pc.cost BETWEEN ? AND ?
        GROUP BY pc.character_id, pc.cost
        HAVING SUM(pc.committed) >= ?
        """,
        (
            commitment_items,
            commitment_items,
            resolved_window,
            min_cost,
            max_cost,
            min_samples,
        ),
    )

    stats: list[CarryStat] = []
    for r in rows:
        appearances = int(r[3])
        n = int(r[4])
        top4s = int(r[6] or 0)
        wins = int(r[7] or 0)
        hits = int(r[8] or 0)
        hit_top4s = int(r[9] or 0)
        avg_place_hit = r[10]
        misses = int(r[11] or 0)
        miss_top4s = int(r[12] or 0)
        avg_place_miss = r[13]
        usage = n / total_participants
        appearance_rate = appearances / total_participants
        carry_conversion_rate = (n / appearances) if appearances else 0.0
        top4 = top4s / n
        win = wins / n
        hit_rate = hits / n
        posterior_top4 = _posterior_rate(top4s, n, prior=0.5, strength=prior_strength)

        # Confidence rises smoothly with sample size; n=60 -> 0.5, n=240 -> 0.8.
        confidence = n / (n + prior_strength)

        # Rarity reaches 1.0 at zero usage and 0 at 8%+ carry usage.
        rarity = max(0.0, min(1.0, 1.0 - usage / 0.08))
        top4_edge = max(-0.5, min(0.5, posterior_top4 - 0.5))
        top4_signal = 0.5 + top4_edge
        win_posterior = _posterior_rate(wins, n, prior=0.125, strength=prior_strength)
        win_signal = max(0.0, min(1.0, win_posterior / 0.25))

        # Early discovery score: power + rarity + evidence. It intentionally
        # does NOT award 3-star hit rate, because low hit rate can itself be a
        # valuable risk signal rather than a reason to hide a line.
        score = 100.0 * (
            0.45 * top4_signal
            + 0.20 * win_signal
            + 0.25 * rarity
            + 0.10 * confidence
        )

        stats.append(
            CarryStat(
                character_id=str(r[0]),
                name=str(r[1]),
                cost=int(r[2]),
                appearances=appearances,
                commitment_games=n,
                usage_rate=usage,
                appearance_rate=appearance_rate,
                commitment_rate=usage,
                carry_conversion_rate=carry_conversion_rate,
                avg_placement=float(r[5]),
                top4_rate=top4,
                win_rate=win,
                hit_3star_rate=hit_rate,
                hit_top4_rate=(hit_top4s / hits) if hits else None,
                miss_top4_rate=(miss_top4s / misses) if misses else None,
                hit_games=hits,
                miss_games=misses,
                avg_placement_hit=(float(avg_place_hit) if avg_place_hit is not None else None),
                avg_placement_miss=(float(avg_place_miss) if avg_place_miss is not None else None),
                posterior_top4=posterior_top4,
                confidence=confidence,
                opportunity_score=score,
            )
        )

    return sorted(stats, key=lambda x: x.opportunity_score, reverse=True)
