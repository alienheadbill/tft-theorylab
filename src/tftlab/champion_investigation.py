"""Champion Investigation: one champion as a carry, in one balance window.

A read-only view model over the existing carry analytics -- no new
statistics. Every number comes straight from `carry_commitment_stats`,
`carry_partner_associations`, `item_package_stats` or
`trait_breakpoint_associations` for a single balance window, and every
section is OBSERVED evidence from indexed matches: nothing here is
inferred, synthesized or taken from the experimental archetype research.

What it adds is player-facing shape: display names and cached art (live
Match-V1 units carry no names, so ids are resolved through the committed
art manifest / roster / item snapshot), a sample-size band built from the
repository's existing thresholds, and a champion directory so a player
can pick a champion by name instead of by Riot id.
"""

from __future__ import annotations

from typing import Any, Sequence

from .analytics import (
    available_balance_windows,
    carry_commitment_stats,
    carry_partner_associations,
    item_package_stats,
    trait_breakpoint_associations,
)
from .analytics.association import Association
from .analytics.commitment import CarryStat
from .game_art import champion_art, champion_name, item_art, item_name, load_manifest, trait_art, trait_name
from .research_report import WEB_DISCOVERY_MIN_SAMPLES
from .roster import id_key, name_key
from .scout import LOW_SAMPLE_COMMITMENT_GAMES
from .storage import Database

#: The only evidence type this page shows today. Variant / theorycrafted
#: evidence does not exist in the backend yet and is never labelled here.
OBSERVED = "observed"

def champion_slug(character_id: str, name: str | None = None) -> str:
    """URL key a player can read and type: "Kha'Zix" -> "khazix"."""
    return name_key(name or champion_name(character_id)) or id_key(character_id) or name_key(character_id)


def sample_info(games: int) -> dict[str, Any]:
    """Player-facing sample context without promoting research-report bands
    into production confidence classes. The only product threshold reused
    here is Discovery's existing LOW SAMPLE cutoff."""
    low_sample = games < LOW_SAMPLE_COMMITMENT_GAMES
    return {
        "games": games,
        "label": "Low sample" if low_sample else "Observed sample",
        "meaning": (
            "Fewer than 30 carry games: treat these results as an early signal, not a stable estimate."
            if low_sample
            else "At least 30 carry games are observed in this window. The results are still observational and can move."
        ),
        "low_sample": low_sample,
    }


def window_carry_stats(db: Database, balance_window: str | None) -> list[CarryStat]:
    """Every champion with at least one carry game in the window, any cost."""
    if balance_window is None:
        return []
    return carry_commitment_stats(db, balance_window=balance_window, min_cost=1, max_cost=5, min_samples=1)


def champion_directory(
    db: Database, balance_window: str | None, *, stats: Sequence[CarryStat] | None = None
) -> list[dict[str, Any]]:
    """Every current-set champion (the art manifest's champion list: shop
    champions and trait-bearing specials, costs 1-5), plus any champion with
    carry games in the window that the manifest lacks, with its carry-game
    count in that window (0 when it was never built as a carry). Sorted by
    cost, then name."""
    if stats is None:
        stats = window_carry_stats(db, balance_window)
    by_id = {s.character_id: s for s in stats}
    entries: dict[str, dict[str, Any]] = {}
    for character_id, meta in (load_manifest().get("champions") or {}).items():
        entries[character_id] = {"character_id": character_id, "name": meta.get("name"), "cost": meta.get("cost")}
    for character_id, stat in by_id.items():
        if character_id not in entries:
            entries[character_id] = {
                "character_id": character_id,
                "name": champion_name(character_id, stat.name),
                "cost": stat.cost,
            }

    taken: set[str] = set()
    directory = []
    for entry in sorted(entries.values(), key=lambda e: (e["cost"] or 0, name_key(e["name"]), e["character_id"])):
        slug = champion_slug(entry["character_id"], entry["name"])
        if slug in taken:  # two champions with one display name: fall back to the id
            slug = name_key(entry["character_id"])
        taken.add(slug)
        stat = by_id.get(entry["character_id"])
        directory.append({
            **entry,
            "name": entry["name"] or entry["character_id"],
            "slug": slug,
            "art_url": champion_art(entry["character_id"], entry["name"]),
            "carry_games": stat.commitment_games if stat else 0,
        })
    return directory


def find_champion(directory: Sequence[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """By exact Riot id, else by slug / name ("khazix", "Kha'Zix", "kha zix")."""
    for entry in directory:
        if entry["character_id"] == key:
            return entry
    wanted = name_key(key)
    if not wanted:
        return None
    for entry in directory:
        if entry["slug"] == wanted or name_key(entry["name"]) == wanted:
            return entry
    return None


# ---------------------------------------------------------------- evidence rows


def _comparison(a: Association) -> dict[str, Any]:
    """Observed with/without numbers for one partner/item/trait, plus the
    existing shrinkage-adjusted Top 4 difference (the value rows are
    ranked by). `limited_sample` when either side of the comparison has
    fewer games than Discovery's own minimum."""
    smaller_side = min(a.games, a.games_without) if a.games_without else a.games
    return {
        "games": a.games,
        "share_of_carry_games": a.inclusion_rate,
        "top4_with": a.top4_rate,
        "avg_placement_with": a.avg_placement,
        "win_with": a.win_rate,
        "games_without": a.games_without,
        "top4_without": a.top4_rate_without,
        "avg_placement_without": a.avg_placement_without,
        "adjusted_top4_difference": a.top4_delta,
        "limited_sample": smaller_side < WEB_DISCOVERY_MIN_SAMPLES,
        "evidence": OBSERVED,
    }


def _item_refs(key: str) -> list[dict[str, Any]]:
    return [
        {"id": part, "name": item_name(part) or part, "art_url": item_art(part)}
        for part in key.split("+")
        if part
    ]


def _item_row(a: Association) -> dict[str, Any]:
    return {"items": _item_refs(a.key), **_comparison(a)}


def _partner_row(a: Association) -> dict[str, Any]:
    return {
        "character_id": a.key,
        "name": champion_name(a.key, a.label),
        "slug": champion_slug(a.key, champion_name(a.key, a.label)),
        "cost": a.cost,
        "art_url": champion_art(a.key, a.label),
        **_comparison(a),
    }


def _trait_row(a: Association) -> dict[str, Any]:
    trait_id, _, tier = a.key.rpartition(":")
    return {
        "trait_id": trait_id,
        "name": trait_name(trait_id) or trait_id,
        # Riot's `tier_current`: which breakpoint of the trait is active
        # (1 = the first), not a unit count.
        "tier": int(tier) if tier.isdigit() else None,
        "art_url": trait_art(trait_id),
        **_comparison(a),
    }


def _appearances(db: Database, character_id: str, balance_window: str) -> int:
    row = db.query_one(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT u.match_id, u.participant_index
            FROM units u
            JOIN matches m ON m.match_id = u.match_id
            WHERE u.character_id = ? AND m.balance_window = ?
        ) AS boards
        """,
        (character_id, balance_window),
    )
    return int(row[0]) if row else 0


def _window_average(stats: Sequence[CarryStat]) -> dict[str, Any] | None:
    """Observed results across every champion's carry games in the window
    (a board with two carries counts once for each), for context."""
    games = sum(s.commitment_games for s in stats)
    if not games:
        return None
    return {
        "carry_games": games,
        "top4_rate": sum(s.top4_rate * s.commitment_games for s in stats) / games,
        "win_rate": sum(s.win_rate * s.commitment_games for s in stats) / games,
        "avg_placement": sum(s.avg_placement * s.commitment_games for s in stats) / games,
    }


def champion_investigation(
    db: Database,
    champion: dict[str, Any],
    balance_window: str | None,
    *,
    stats: Sequence[CarryStat] | None = None,
    top_n: int = 6,
) -> dict[str, Any]:
    """Everything the Champion Investigation page shows for `champion` (a
    `champion_directory` entry) in one balance window. `carry` is None when
    the champion has no carry games there; the evidence lists are empty then."""
    if stats is None:
        stats = window_carry_stats(db, balance_window)
    window = next((w for w in available_balance_windows(db) if w[0] == balance_window), None)
    character_id = champion["character_id"]
    body: dict[str, Any] = {
        "balance_window": balance_window,
        "window": (
            {"balance_window": window[0], "matches": window[1], "latest_game_datetime": window[2]} if window else None
        ),
        "evidence_type": OBSERVED,
        "champion": {k: champion[k] for k in ("character_id", "name", "slug", "cost", "art_url")},
        "carry": None,
        "appearances": 0,
        "window_average": _window_average(stats),
        "items": {"most_common_build": None, "builds": [], "pairs": []},
        "partners": [],
        "traits": [],
    }
    if balance_window is None or window is None:
        return body
    stat = next((s for s in stats if s.character_id == character_id), None)
    if stat is None:
        body["appearances"] = _appearances(db, character_id, balance_window)
        return body

    body["appearances"] = stat.appearances
    body["carry"] = {
        "games": stat.commitment_games,
        "appearances": stat.appearances,
        "appearance_rate": stat.appearance_rate,
        "carry_rate": stat.commitment_rate,
        "carry_conversion_rate": stat.carry_conversion_rate,
        "avg_placement": stat.avg_placement,
        "top4_rate": stat.top4_rate,
        "win_rate": stat.win_rate,
        "three_star": {
            "hit_rate": stat.hit_3star_rate,
            "hit_games": stat.hit_games,
            "miss_games": stat.miss_games,
            "hit_top4_rate": stat.hit_top4_rate,
            "miss_top4_rate": stat.miss_top4_rate,
            "hit_avg_placement": stat.avg_placement_hit,
            "miss_avg_placement": stat.avg_placement_miss,
            "hit_sample": sample_info(stat.hit_games),
            "miss_sample": sample_info(stat.miss_games),
        },
        "sample": sample_info(stat.commitment_games),
        "evidence": OBSERVED,
    }

    item_stats = item_package_stats(db, character_id, balance_window)
    builds = item_stats["packages"]
    if builds:
        most_common = max(builds, key=lambda a: (a.games, a.association_score))
        body["items"]["most_common_build"] = _item_row(most_common)
    body["items"]["builds"] = [_item_row(a) for a in builds[:top_n]]
    body["items"]["pairs"] = [_item_row(a) for a in item_stats["pairs"][:top_n]]
    body["partners"] = [_partner_row(a) for a in carry_partner_associations(db, character_id, balance_window)[:top_n]]
    body["traits"] = [_trait_row(a) for a in trait_breakpoint_associations(db, character_id, balance_window)[:top_n]]
    return body
