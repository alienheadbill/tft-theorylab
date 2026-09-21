from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3


@dataclass(frozen=True)
class CarryStat:
    character_id: str
    name: str
    cost: int
    appearances: int
    commitment_games: int
    usage_rate: float
    avg_placement: float
    top4_rate: float
    win_rate: float
    hit_3star_rate: float
    hit_top4_rate: float | None
    miss_top4_rate: float | None
    posterior_top4: float
    confidence: float
    opportunity_score: float


def _posterior_rate(successes: int, attempts: int, *, prior: float, strength: float) -> float:
    return (successes + prior * strength) / (attempts + strength)


def carry_commitment_stats(
    conn: sqlite3.Connection,
    *,
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
    """
    total_participants = conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
    if total_participants == 0:
        return []

    rows = conn.execute(
        """
        SELECT
            u.character_id,
            MAX(u.unit_name) AS unit_name,
            u.cost,
            COUNT(*) AS appearances,
            SUM(CASE WHEN u.completed_item_count >= ? THEN 1 ELSE 0 END) AS commitment_games,
            AVG(CASE WHEN u.completed_item_count >= ? THEN p.placement * 1.0 END) AS avg_place,
            SUM(CASE WHEN u.completed_item_count >= ? AND p.placement <= 4 THEN 1 ELSE 0 END) AS top4s,
            SUM(CASE WHEN u.completed_item_count >= ? AND p.placement = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN u.completed_item_count >= ? AND u.tier >= 3 THEN 1 ELSE 0 END) AS hits,
            SUM(CASE WHEN u.completed_item_count >= ? AND u.tier >= 3 AND p.placement <= 4 THEN 1 ELSE 0 END) AS hit_top4s,
            SUM(CASE WHEN u.completed_item_count >= ? AND u.tier < 3 THEN 1 ELSE 0 END) AS misses,
            SUM(CASE WHEN u.completed_item_count >= ? AND u.tier < 3 AND p.placement <= 4 THEN 1 ELSE 0 END) AS miss_top4s
        FROM units u
        JOIN participants p
          ON p.match_id = u.match_id AND p.participant_index = u.participant_index
        WHERE u.cost BETWEEN ? AND ?
        GROUP BY u.character_id, u.cost
        HAVING commitment_games >= ?
        """,
        (
            commitment_items,
            commitment_items,
            commitment_items,
            commitment_items,
            commitment_items,
            commitment_items,
            commitment_items,
            commitment_items,
            min_cost,
            max_cost,
            min_samples,
        ),
    ).fetchall()

    stats: list[CarryStat] = []
    for r in rows:
        n = int(r[4])
        top4s = int(r[6] or 0)
        wins = int(r[7] or 0)
        hits = int(r[8] or 0)
        hit_top4s = int(r[9] or 0)
        misses = int(r[10] or 0)
        miss_top4s = int(r[11] or 0)
        usage = n / total_participants
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
                appearances=int(r[3]),
                commitment_games=n,
                usage_rate=usage,
                avg_placement=float(r[5]),
                top4_rate=top4,
                win_rate=win,
                hit_3star_rate=hit_rate,
                hit_top4_rate=(hit_top4s / hits) if hits else None,
                miss_top4_rate=(miss_top4s / misses) if misses else None,
                posterior_top4=posterior_top4,
                confidence=confidence,
                opportunity_score=score,
            )
        )

    return sorted(stats, key=lambda x: x.opportunity_score, reverse=True)
