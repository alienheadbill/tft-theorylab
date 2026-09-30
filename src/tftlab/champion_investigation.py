"""Champion Investigation: one champion as a carry, in one balance window.

A read-only view model over the existing carry analytics -- no new
statistics. Every number comes straight from `carry_commitment_stats`,
`carry_partner_associations`, `item_package_stats` or `trait_profile`
for a single balance window, and every
section is OBSERVED evidence from indexed matches: nothing here is
inferred, synthesized or taken from the experimental archetype research.

What it adds is player-facing shape: display names and cached art (live
Match-V1 units carry no names, so ids are resolved through the committed
art manifest / roster / item snapshot), cautious sample-size context using
Discovery's existing LOW SAMPLE threshold, and a champion directory so a
player can pick a champion by name instead of by Riot id.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .analytics import (
    available_balance_windows,
    carry_board_average,
    carry_board_counts,
    carry_commitment_stats,
    carry_partner_associations,
    item_package_stats,
    trait_profile,
)
from .analytics.association import Association
from .analytics.traits import TraitProfile, split_trait_count_key
from .game_art import (
    NORMAL_ITEM_KINDS,
    champion_art,
    champion_name,
    item_ref,
    load_manifest,
    trait_art,
    trait_name,
)
from .research_report import WEB_DISCOVERY_MIN_SAMPLES
from .roster import id_key, load_roster, name_key
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
            "Fewer than 30 carry boards: treat these results as an early signal, not a stable estimate."
            if low_sample
            else "At least 30 carry boards are observed in this window. The results are still observational and can move."
        ),
        "low_sample": low_sample,
    }


def champion_directory(
    db: Database, balance_window: str | None, *, counts: Mapping[str, int] | None = None
) -> list[dict[str, Any]]:
    """Every current-set champion (the art manifest's champion list: shop
    champions and trait-bearing specials, costs 1-5), plus any champion with
    carry boards in the window that the manifest lacks, with its carry-board
    count in that window (0 when it was never built as a carry). Sorted by
    cost, then name.

    Counts come from `carry_board_counts` -- one light aggregate -- unless
    `counts` is passed; the picker never runs the full carry statistics."""
    if counts is None:
        counts = carry_board_counts(db, balance_window)
    roster = load_roster()
    entries: dict[str, dict[str, Any]] = {}
    for character_id, meta in (load_manifest().get("champions") or {}).items():
        entries[character_id] = {"character_id": character_id, "name": meta.get("name"), "cost": meta.get("cost")}
    for character_id in counts:
        if character_id not in entries:
            entries[character_id] = {
                "character_id": character_id,
                "name": champion_name(character_id, character_id),
                "cost": (roster.champions.get(character_id) or {}).get("cost"),
            }

    taken: set[str] = set()
    directory = []
    for entry in sorted(entries.values(), key=lambda e: (e["cost"] or 0, name_key(e["name"]), e["character_id"])):
        slug = champion_slug(entry["character_id"], entry["name"])
        if slug in taken:  # two champions with one display name: fall back to the id
            slug = name_key(entry["character_id"])
        taken.add(slug)
        directory.append({
            **entry,
            "name": entry["name"] or entry["character_id"],
            "slug": slug,
            "art_url": champion_art(entry["character_id"], entry["name"]),
            # Committed player boards (8 per match), not matches.
            "carry_games": int(counts.get(entry["character_id"], 0)),
        })
    return directory


def _observed_character_ids(db: Database, balance_window: str) -> list[str]:
    rows = db.query_all(
        "SELECT DISTINCT u.character_id FROM units u JOIN matches m ON m.match_id = u.match_id "
        "WHERE m.balance_window = ?",
        (balance_window,),
    )
    return [str(r[0]) for r in rows]


def resolve_champion(db: Database, balance_window: str | None, key: str) -> dict[str, Any] | None:
    """The champion a page key names, without computing any carry statistics:
    the manifest's champions first; only an unmatched key costs one cheap
    query for the champions observed in the window (e.g. a unit the cached
    manifest lacks)."""
    entry = find_champion(champion_directory(db, balance_window, counts={}), key)
    if entry is not None or balance_window is None:
        return entry
    observed = {cid: 0 for cid in _observed_character_ids(db, balance_window)}
    return find_champion(champion_directory(db, balance_window, counts=observed), key)


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


def _item_row(a: Association) -> dict[str, Any]:
    items = [item_ref(part) for part in a.key.split("+") if part]
    return {
        "items": items,
        # Every item is a standard completed item or an emblem: a build a
        # player can craft. Artifact / Radiant / unrecognized items stay in
        # the observed lists, labelled, but never lead the build summary.
        "normal_build": all(i["kind"] in NORMAL_ITEM_KINDS for i in items),
        **_comparison(a),
    }


def _partner_row(a: Association) -> dict[str, Any]:
    return {
        "character_id": a.key,
        "name": champion_name(a.key, a.label),
        "slug": champion_slug(a.key, champion_name(a.key, a.label)),
        "cost": a.cost,
        "art_url": champion_art(a.key, a.label),
        **_comparison(a),
    }


def _trait_count_row(a: Association) -> dict[str, Any]:
    trait_id, num_units = split_trait_count_key(a.key)
    return {
        "trait_id": trait_id,
        "name": trait_name(trait_id) or trait_id,
        # Riot's own `num_units` for this trait on the board. Riot's
        # `tier_current` (an ordinal) is never shown as a unit count.
        "num_units": num_units,
        "art_url": trait_art(trait_id),
        **_comparison(a),
        # The existing association ranking score; secondary evidence only.
        "association_score": a.association_score,
    }


def _trait_rows(profile: TraitProfile) -> list[dict[str, Any]]:
    """One row per active trait (most common first; ties by display name,
    then id), each with its observed unit counts (lowest count first).
    Shares are of the champion's carry boards; one board is counted under
    every trait it had active."""
    rows = [
        {
            "trait_id": a.key,
            "name": trait_name(a.key) or a.key,
            "art_url": trait_art(a.key),
            **_comparison(a),
            "counts": [_trait_count_row(c) for c in profile.counts.get(a.key, [])],
        }
        for a in profile.active
    ]
    return sorted(rows, key=lambda r: (-r["games"], r["name"].casefold(), r["trait_id"]))


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


def champion_investigation(
    db: Database,
    champion: dict[str, Any],
    balance_window: str | None,
    *,
    top_n: int = 6,
    trait_top_n: int = 8,
) -> dict[str, Any]:
    """Everything the Champion Investigation page shows for `champion` (a
    `champion_directory` entry) in one balance window. `carry` is None when
    the champion has no carry boards there; the evidence lists are empty then.

    Only this champion's carry statistics are aggregated (`character_ids`);
    the window-wide comparison is one light `carry_board_average` query."""
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
        "window_average": None,
        "summary": None,
        "items": {"most_common_build": None, "most_common_normal_build": None, "builds": [], "pairs": []},
        "partners": [],
        # Active traits on the carry boards, each with Riot's observed unit
        # counts (`num_units`). Denominator: `carry.games`.
        "traits": [],
    }
    if balance_window is None or window is None:
        return body
    body["window_average"] = carry_board_average(db, balance_window)
    stats = carry_commitment_stats(
        db, balance_window=balance_window, min_cost=1, max_cost=5, min_samples=1, character_ids=[character_id]
    )
    stat = stats[0] if stats else None
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
    rows = [_item_row(a) for a in item_stats["packages"]]  # ranked by association score
    frequency = lambda r: (r["games"], r["adjusted_top4_difference"] or 0.0)  # noqa: E731
    if rows:
        body["items"]["most_common_build"] = max(rows, key=frequency)
        normal = [r for r in rows if r["normal_build"]]
        body["items"]["most_common_normal_build"] = max(normal, key=frequency) if normal else None
    body["items"]["builds"] = rows[:top_n]
    body["items"]["pairs"] = [_item_row(a) for a in item_stats["pairs"][:top_n]]
    partners = [_partner_row(a) for a in carry_partner_associations(db, character_id, balance_window)]
    traits = _trait_rows(trait_profile(db, character_id, balance_window))
    body["partners"] = partners[:top_n]
    body["traits"] = traits[:trait_top_n]
    body["summary"] = carry_summary(champion["name"], body["carry"], rows, partners, traits)
    return body


# ---------------------------------------------------------------- "How players carry"

def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _boards(n: int) -> str:
    return f"{n:,} carry board" + ("" if n == 1 else "s")


def _items_label(row: dict[str, Any]) -> str:
    return " + ".join(i["name"] for i in row["items"])


def _supported(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The top-ranked row (existing association ranking) if its evidence
    actually points the positive way: enough games on both sides, a
    positive shrinkage-adjusted difference and a higher raw Top 4 with it."""
    for row in rows[:1]:
        if (
            not row["limited_sample"]
            and row["games_without"]
            and (row["adjusted_top4_difference"] or 0) > 0
            and row["top4_with"] > (row["top4_without"] or 0)
        ):
            return row
    return None


def _most_frequent(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    return max(rows, key=lambda r: (r["games"], r["adjusted_top4_difference"] or 0.0)) if rows else None


def _units(n: int | None) -> str:
    return f"{n} unit" + ("" if n == 1 else "s")


def _count_label(row: dict[str, Any]) -> str:
    return f"{row['name']} · {_units(row['num_units'])}"


def _ranked_trait_counts(traits: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every trait/count row on 2+ boards, in the existing association
    ranking (the same shrinkage score partners and items use), for the
    secondary with-vs-without line only."""
    rows = [c for t in traits for c in t.get("counts", []) if c["games"] >= 2]
    return sorted(rows, key=lambda r: (-r["association_score"], r["trait_id"], r["num_units"] or 0))


def carry_summary(
    name: str,
    carry: dict[str, Any],
    builds: Sequence[dict[str, Any]],
    partners: Sequence[dict[str, Any]],
    traits: Sequence[dict[str, Any]],
) -> dict[str, list[str]]:
    """"How players carry with {name}": OBSERVED facts restated from the
    page's own numbers, and a separate INTERPRETATION produced by fixed rules
    from those same numbers (never generated, never strategy: no roll timing,
    leveling, positioning or causal claims)."""
    observed: list[str] = []
    interpretation: list[str] = []
    n = carry["games"]
    ts = carry["three_star"]
    hit, miss = ts["hit_games"], ts["miss_games"]

    # 3-star dependency
    if hit and miss:
        observed.append(f"{hit:,} of {n:,} carry boards reached 3★ ({_pct(ts['hit_rate'])}).")
        observed.append(
            f"Top 4 was {_pct(ts['hit_top4_rate'])} on boards that reached 3★ and "
            f"{_pct(ts['miss_top4_rate'])} on boards that stayed below 3★."
        )
    elif not hit:
        observed.append(f"None of the {_boards(n)} reached 3★: every result here is from 2★ or lower.")
    else:
        observed.append(f"All {_boards(n)} reached 3★, so there is nothing to compare a miss against.")

    # items
    normal = _most_frequent([b for b in builds if b["normal_build"]])
    overall = _most_frequent(builds)
    if normal:
        observed.append(
            f"Most common normal full build: {_items_label(normal)}, on {_boards(normal['games'])} "
            f"({_pct(normal['share_of_carry_games'])})."
        )
    else:
        observed.append("No normal 3-item build appears on 2 or more carry boards yet.")
    if overall and not overall["normal_build"] and (not normal or overall["games"] > normal["games"]):
        observed.append(
            f"The most common full build overall includes an Artifact, Radiant or unrecognized item: "
            f"{_items_label(overall)} ({_boards(overall['games'])})."
        )
    best_build = _supported(builds)
    if best_build:
        observed.append(
            f"Best with-vs-without result among full builds: {_items_label(best_build)} "
            f"(Top 4 {_pct(best_build['top4_with'])} with vs {_pct(best_build['top4_without'])} without, "
            f"{_boards(best_build['games'])})."
        )

    # partners
    frequent = _most_frequent(partners)
    if frequent:
        observed.append(
            f"Most frequent partner: {frequent['name']}, on {_pct(frequent['share_of_carry_games'])} of carry boards."
        )
    best = _supported(partners)
    if best:
        observed.append(
            f"Strongest with-vs-without partner: {best['name']} (Top 4 {_pct(best['top4_with'])} with vs "
            f"{_pct(best['top4_without'])} without)."
        )

    # traits: how often each was active, then Riot's observed unit counts
    if traits:
        top = traits[0]
        counts = sorted(top["counts"], key=lambda c: (-c["games"], c["num_units"] or 0))[:2]
        common = ", then ".join(f"{_units(c['num_units'])} ({_boards(c['games'])})" for c in counts)
        observed.append(
            f"Most common active trait: {top['name']}, active on {_pct(top['share_of_carry_games'])} of carry boards "
            f"({top['games']:,} of {n:,}); most often at {common}."
        )
        others = traits[1:3]
        if others:
            observed.append(
                "Next most common active traits: "
                + ", ".join(f"{t['name']} ({_pct(t['share_of_carry_games'])})" for t in others)
                + "."
            )
    best_count = _supported(_ranked_trait_counts(traits))
    if best_count:
        observed.append(
            f"Strongest with-vs-without trait count: {_count_label(best_count)} (Top 4 "
            f"{_pct(best_count['top4_with'])} on those {_boards(best_count['games'])} vs "
            f"{_pct(best_count['top4_without'])} on its other carry boards)."
        )

    # interpretation: fixed rules only
    if n < LOW_SAMPLE_COMMITMENT_GAMES:
        interpretation.append(
            f"Fewer than {LOW_SAMPLE_COMMITMENT_GAMES} carry boards: treat everything above as an early signal, "
            "not a pattern."
        )
    if hit and miss and min(hit, miss) >= LOW_SAMPLE_COMMITMENT_GAMES:
        a, b = ts["hit_top4_rate"], ts["miss_top4_rate"]
        gap = (a - b) * 100
        if a > b:
            interpretation.append(
                f"Boards that reached 3★ had higher observed Top 4 in this sample: {_pct(a)} vs {_pct(b)} "
                f"below 3★ ({gap:+.1f} percentage points). Treat that as a signal, not proof that 3★ caused "
                "the difference."
            )
        elif a < b:
            interpretation.append(
                f"Boards that reached 3★ had lower observed Top 4 in this sample: {_pct(a)} vs {_pct(b)} "
                f"below 3★ ({gap:+.1f} percentage points). These observational data do not show a positive "
                "3★ association here."
            )
        else:
            interpretation.append(
                f"Observed Top 4 was the same at 3★ and below 3★ in this sample ({_pct(a)})."
            )
    elif hit and miss:
        interpretation.append(
            f"Too few boards on one side of the 3★ split ({hit:,} reached 3★, {miss:,} did not) to judge how much "
            f"hitting 3★ matters; each side needs at least {LOW_SAMPLE_COMMITMENT_GAMES}."
        )
    if normal and best_build:
        if best_build is normal:
            interpretation.append("The normal build players use most is also the best-supported one here.")
        else:
            interpretation.append(
                f"The best-supported build differs from the most common normal build and has "
                f"{best_build['games']:,} boards behind it versus {normal['games']:,}: weigh both."
            )
    if partners or traits:
        interpretation.append(
            "Frequent partners and traits describe the boards players built. They are associations, not a proven "
            "core, and not a cause of the results."
        )
    return {"observed": observed, "interpretation": interpretation}
