""""How to play {champion}": the concise, player-facing layer of Champion
Investigation.

Nothing here queries the database or computes a new statistic. Every
function reshapes evidence rows that `tftlab.champion_investigation` has
already built from the existing analytics (`item_package_stats`,
`carry_partner_associations`, `trait_profile`, `carry_commitment_stats`) and
picks a few of them for a quick read. The rules are fixed and documented
below; the deeper tabs keep every row.

Common rows. The concise view answers "what do players commonly build?":
an item, pair or full build qualifies when it is on at least `MIN_BOARDS`
carry boards (Discovery's existing minimum sample) and is ordered most
boards first (ties by display name, then id). That is a sample/frequency
rule only -- it says nothing about how those boards placed, so the page
labels these "common", never "supported", "strong" or "best". A common row
can have a negative with-vs-without result; the ITEMS tab keeps the
existing analytics order and every with-vs-without comparison.

Component direction is DERIVED, not observed. Match-V1 lists a unit's final
items only, never which components a player held or when, so this reads the
committed CommunityDragon recipes (`tftlab.items.item_recipe`) of the
common completed items shown and counts the components those recipes share.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence

from .game_art import NORMAL_ITEM_KINDS, item_ref
from .itemization import thiefs_gloves_item_ids
from .items import item_recipe
from .research_report import WEB_DISCOVERY_MIN_SAMPLES
from .roster import load_roster

#: A concise row needs at least this many carry boards (Discovery's own
#: minimum sample, also the page's "Limited sample" cutoff). Sample size only.
MIN_BOARDS = WEB_DISCOVERY_MIN_SAMPLES
#: Common individual items shown, which are also the component direction's input.
DIRECTION_ITEMS = 6
#: Components shown in the concise recipe direction.
DIRECTION_COMPONENTS = 3
CONCISE_PAIRS = 3
CONCISE_BUILDS = 3
CONCISE_TEAMMATES = 4
CONCISE_TRAITS = 3

COMPONENT_DIRECTION_BASIS = (
    "Derived from the recipes of this champion's most common completed items (each on 10+ carry boards): the "
    "components those recipes share. Match-V1 records final items only, so this is not what players actually "
    "held or opened with."
)


def meets_board_floor(row: dict[str, Any]) -> bool:
    """On at least `MIN_BOARDS` carry boards: a sample-size rule, nothing more."""
    return row["games"] >= MIN_BOARDS


def common_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Item/pair/build rows most carry boards first; ties by display name,
    then ids, so the order never depends on the analytics ranking."""
    return sorted(rows, key=lambda r: (
        -r["games"],
        " + ".join(i["name"] for i in r["items"]).casefold(),
        "+".join(i["id"] for i in r["items"]),
    ))


def _normal(row: dict[str, Any]) -> bool:
    """Every item in the row is a normal craftable class (standard or emblem):
    never an Artifact, Radiant, component or unrecognized item."""
    return bool(row["items"]) and all(i["kind"] in NORMAL_ITEM_KINDS for i in row["items"])


def common_items(item_rows: Sequence[dict[str, Any]], limit: int = DIRECTION_ITEMS) -> list[dict[str, Any]]:
    """The `limit` most common normal individual items on `MIN_BOARDS`+
    carry boards (`common_rows` order). Thief's Gloves is excluded outright:
    its holder is never a carry, and its random rolls are never equipped."""
    gloves = thiefs_gloves_item_ids()
    rows = [
        r for r in item_rows
        if len(r["items"]) == 1 and _normal(r) and r["items"][0]["id"] not in gloves and meets_board_floor(r)
    ]
    return common_rows(rows)[:limit]


def component_direction(selected: Sequence[dict[str, Any]], limit: int = DIRECTION_COMPONENTS) -> dict[str, Any]:
    """Recipe direction over `selected` (the `common_items` rows, the same
    items the concise view shows):

    1. read each selected item's verified two-component recipe; items
       without one are listed in `items_without_recipe` and ignored;
    2. count, per component, how many of those recipes contain it (a recipe
       holding a component twice counts once; `copies` keeps the total);
    3. order by that count, then the summed carry boards of the contributing
       item rows, then component name and id;
    4. keep the first `limit`.

    Never described as observed components."""
    recipes: list[tuple[dict[str, Any], tuple[str, str]]] = []
    without_recipe: list[str] = []
    for row in selected:
        item_id = row["items"][0]["id"]
        recipe = item_recipe(item_id)
        if recipe is None:
            without_recipe.append(item_id)
        else:
            recipes.append((row, recipe))

    contributions: dict[str, list[tuple[dict[str, Any], int]]] = defaultdict(list)
    for row, recipe in recipes:
        for component in sorted(set(recipe)):
            contributions[component].append((row, recipe.count(component)))

    components = []
    for component, rows in contributions.items():
        ref = item_ref(component)
        components.append({
            "component": ref,
            "recipes": len(rows),
            "copies": sum(n for _, n in rows),
            "games": sum(row["games"] for row, _ in rows),
            "items": [{**row["items"][0], "games": row["games"], "copies": n} for row, n in rows],
        })
    components.sort(key=lambda c: (-c["recipes"], -c["games"], c["component"]["name"].casefold(), c["component"]["id"]))
    return {
        "status": "available" if components else "insufficient",
        "derived_from_recipes": True,
        "observed_components": False,
        "basis": COMPONENT_DIRECTION_BASIS,
        "recipes_considered": len(recipes),
        "items_considered": [item_id for item_id in (r["items"][0]["id"] for r in selected)],
        "items_without_recipe": without_recipe,
        "components": components[:limit],
    }


def star_signal(carry: dict[str, Any]) -> dict[str, Any]:
    """The existing 3★ hit/miss split, labelled for a quick read. `status`:
    `compared` (both sides have `MIN_BOARDS`+ boards), `limited`
    (both sides exist, one is smaller), `no_hits` or `all_hits`. Never a
    causal or "reroll" claim."""
    ts = carry["three_star"]
    hit, miss = ts["hit_games"], ts["miss_games"]
    if not hit:
        status = "no_hits"
    elif not miss:
        status = "all_hits"
    elif min(hit, miss) < MIN_BOARDS:
        status = "limited"
    else:
        status = "compared"
    return {
        "status": status,
        "min_boards": MIN_BOARDS,
        "hit_games": hit,
        "miss_games": miss,
        "hit_rate": ts["hit_rate"],
        "hit_top4_rate": ts["hit_top4_rate"] if hit else None,
        "miss_top4_rate": ts["miss_top4_rate"] if miss else None,
    }


def teammates(partner_rows: Sequence[dict[str, Any]], limit: int = CONCISE_TEAMMATES) -> list[dict[str, Any]]:
    """The most frequent teammates: carry boards together, most first (ties
    by name, then id). Frequency only -- no new score; the TEAMMATES tab
    keeps the with-vs-without evidence."""
    return sorted(partner_rows, key=lambda r: (-r["games"], r["name"].casefold(), r["character_id"]))[:limit]


def natural_provider_count(trait_id: str) -> int:
    """How many shop champions (cost 1-5) naturally have this trait, from
    the committed roster's static trait membership."""
    return len(load_roster().trait_champions(trait_id))


def trait_directions(trait_rows: Sequence[dict[str, Any]], limit: int = CONCISE_TRAITS) -> list[dict[str, Any]]:
    """A few buildable trait directions from the corrected trait evidence.
    Traits with at least `MIN_BOARDS` boards, most boards first, each with
    its most common observed unit count (Riot `num_units`; ties go to the
    lower count). Not a canonical breakpoint.

    Not actionable, so skipped here: a trait with exactly one natural
    shop-champion provider whose selected count is 1 unit. That only says
    its one champion was on the board -- the carry itself, or a teammate
    the teammates list already shows (e.g. a 1-unit trait that only that
    teammate has). The same trait at a selected 2+ units stays eligible
    (it may have been extended). The TRAITS tab keeps every row."""
    out = []
    for t in trait_rows:
        if t["games"] < MIN_BOARDS or not t["counts"]:
            continue
        top = min(t["counts"], key=lambda c: (-c["games"], c["num_units"] or 0))
        if top["num_units"] == 1 and natural_provider_count(t["trait_id"]) == 1:
            continue
        out.append({
            "trait_id": t["trait_id"],
            "name": t["name"],
            "art_url": t["art_url"],
            "num_units": top["num_units"],
            "count_games": top["games"],
            "count_share": top["share_of_carry_games"],
            "games": t["games"],
            "share_of_carry_games": t["share_of_carry_games"],
            "above_baseline_only": t.get("above_baseline_only", False),
        })
    return out[:limit]


def _names(rows: Sequence[dict[str, Any]], key: str = "name") -> str:
    names = [r[key] for r in rows]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def evidence_summary(
    name: str, boards: int, items: Sequence[dict[str, Any]], mates: Sequence[dict[str, Any]],
    traits: Sequence[dict[str, Any]],
) -> str | None:
    """One cautious sentence restating the concise picks by frequency.
    Observed evidence only: no "best", mechanic, causal or strategy claim.
    The trait clause appears only when a trait direction survived the
    concise filter."""
    parts = []
    if items:
        parts.append(f"the most commonly used items are {_names([r['items'][0] for r in items[:2]])}")
    if mates:
        parts.append(f"frequent teammates include {_names(mates[:2])}")
    if traits:
        t = traits[0]
        parts.append(f"the most common buildable trait direction is {t['name']} at {t['num_units']} units")
    if not parts:
        return None
    return (
        f"Across {boards:,} {name} carry boards in this window, " + "; ".join(parts)
        + ". These are observed associations, not proof of what causes a result."
    )


def how_to_play(
    name: str,
    carry: dict[str, Any],
    *,
    item_rows: Sequence[dict[str, Any]],
    pair_rows: Sequence[dict[str, Any]],
    build_rows: Sequence[dict[str, Any]],
    partner_rows: Sequence[dict[str, Any]],
    trait_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """The concise view: existing evidence rows on `MIN_BOARDS`+ carry
    boards, most boards first, shortened (see "Common rows" above). Empty
    lists are honest empty states. The component direction reads exactly
    the common items shown."""
    items = common_items(item_rows)
    pairs = common_rows([r for r in pair_rows if _normal(r) and meets_board_floor(r)])[:CONCISE_PAIRS]
    builds = common_rows([r for r in build_rows if _normal(r) and meets_board_floor(r)])[:CONCISE_BUILDS]
    mates = teammates(partner_rows)
    traits = trait_directions(trait_rows)
    return {
        "evidence": "observed",
        # Kept under its original key for API compatibility: the board floor.
        "support_min_boards": MIN_BOARDS,
        "component_direction": component_direction(items),
        "items": items,
        "pairs": pairs,
        "builds": builds,
        "teammates": mates,
        "trait_directions": traits,
        "star_signal": star_signal(carry),
        "summary": evidence_summary(name, carry["games"], items, mates, traits),
    }
