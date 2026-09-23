"""Comp Scout v1: fingerprint an experiment and check it against our own
Riot match data.

Nothing here touches the internet. External research (TFT Academy,
MetaTFT, ...) is recorded by a person as field notes; see
`tftlab.sources` and `tftlab.experiments.add_field_note`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .analytics import default_balance_window, discovery_candidate_for
from .analytics.partners import _carry_commitment_games_with_partners
from .experiments import Experiment, add_field_note, list_field_notes
from .roster import Roster, id_key, load_roster, name_key
from .sources import scout_checklist

if TYPE_CHECKING:
    from .storage import Database

FINGERPRINT_VERSION = 1

# Matches the CLI's discovery-smoke and the web dashboard.
LOW_SAMPLE_COMMITMENT_GAMES = 30

# Same commitment definition the analytics layer defaults to: a carry
# "counts" in a game once it holds this many completed items.
_COMMITMENT_ITEMS = 2


# ---------------------------------------------------------------- fingerprint


def _unit_ref(roster: Roster, *, name: str | None, character_id: str | None) -> dict[str, Any] | None:
    if not (name or character_id):
        return None
    if not character_id:
        ids = roster.champion_ids(name)
        character_id = ids[0] if len(ids) == 1 else None
    display = roster.champion_name(character_id) or name or character_id
    return {
        "key": roster.champion_key(name=name, character_id=character_id),
        "name": display,
        "character_id": character_id,
    }


def _unique_sorted(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for ref in refs:
        if ref and ref["key"] and ref["key"] not in seen:
            seen[ref["key"]] = ref
    return [seen[k] for k in sorted(seen)]


def comp_fingerprint(experiment: Experiment, roster: Roster | None = None) -> dict[str, Any]:
    """A deterministic, normalized description of what the idea specifies.

    Only what the entry actually says is included; nothing is inferred, and
    nothing requires a full board. "6 Ravager Kha'Zix" with just a carry and
    a trait target still yields a usable fingerprint. Names are matched to
    current-set ids through the shipped roster where possible, and every
    list is de-duplicated and sorted, so the same idea always produces the
    same fingerprint and `signature`.
    """
    roster = roster or load_roster()
    comp = experiment.comp

    primary = _unit_ref(roster, name=experiment.carry_name, character_id=experiment.carry_character_id)
    secondary_unit = (comp.get("secondary_carry") or {}).get("unit")
    secondary = _unit_ref(roster, name=secondary_unit, character_id=None)

    core = _unique_sorted([_unit_ref(roster, name=u["name"], character_id=u.get("character_id")) for u in comp["core_units"]])
    core_keys = {u["key"] for u in core}
    optional = [
        u for u in _unique_sorted(
            [_unit_ref(roster, name=u["name"], character_id=u.get("character_id")) for u in comp["optional_units"]]
        )
        if u["key"] not in core_keys
    ]

    traits: dict[tuple[str, int], dict[str, Any]] = {}
    for t in comp["target_traits"]:
        ids = roster.trait_ids(t["name"])
        canonical = roster.trait_name(ids[0]) if ids else t["name"]
        key = name_key(canonical)
        bp = t.get("breakpoint") or 0
        traits[(key, bp)] = {"key": key, "name": canonical, "breakpoint": t.get("breakpoint"), "trait_ids": ids}
    trait_list = [traits[k] for k in sorted(traits)]

    items = sorted({id_key(i) or name_key(i) for i in comp["carry_items"]} - {""})
    roll_timing = " ".join((comp.get("roll_timing") or "").lower().split()) or None

    fingerprint = {
        "version": FINGERPRINT_VERSION,
        "primary_carry": primary,
        "secondary_carry": secondary,
        "core_units": core,
        "optional_units": optional,
        "target_traits": trait_list,
        "carry_items": items,
        "carry_item_names": sorted(set(comp["carry_items"]), key=lambda i: (id_key(i) or name_key(i), i)),
        "target_level": comp.get("target_level"),
        "reroll_level": comp.get("reroll_level"),
        "roll_timing": roll_timing,
    }
    parts = [
        ("carry", primary["key"] if primary else ""),
        ("secondary", secondary["key"] if secondary else ""),
        ("core", ",".join(u["key"] for u in core)),
        ("optional", ",".join(u["key"] for u in optional)),
        ("traits", ",".join(f"{t['key']}@{t['breakpoint']}" if t["breakpoint"] else t["key"] for t in trait_list)),
        ("items", ",".join(items)),
        ("target_level", str(fingerprint["target_level"] or "")),
        ("reroll_level", str(fingerprint["reroll_level"] or "")),
    ]
    fingerprint["signature"] = ";".join(f"{k}={v}" for k, v in parts if v)
    fingerprint["specified"] = [k for k, v in parts if v]
    return fingerprint


# ---------------------------------------------------------------- our Riot data


def _ids_in_data(db: Database, ref: dict[str, Any], balance_window: str, roster: Roster) -> list[str]:
    """Every character_id in this window that could be this unit: its own id,
    roster ids with the same name, and ids whose stored unit name matches."""
    ids = set(roster.champion_ids(ref["name"]))
    if ref.get("character_id"):
        ids.add(ref["character_id"])
    rows = db.query_all(
        """SELECT DISTINCT u.character_id, u.unit_name FROM units u
           JOIN matches m ON m.match_id = u.match_id WHERE m.balance_window = ?""",
        (balance_window,),
    )
    for character_id, unit_name in rows:
        if name_key(unit_name) == ref["key"] or roster.champion_key(character_id=character_id) == ref["key"]:
            ids.add(character_id)
    return sorted(ids)


def _slice(games: list[tuple[int, bool]]) -> dict[str, Any]:
    n = sum(1 for _, hit in games if hit)
    top4 = sum(1 for placement, hit in games if hit and placement <= 4)
    return {"games": n, "top4_rate": (top4 / n) if n else None}


def _assoc(a: Any) -> dict[str, Any]:
    return {"label": a.label, "games": a.games, "top4_rate": a.top4_rate, "top4_delta": a.top4_delta}


def riot_evidence(
    db: Database,
    experiment: Experiment,
    *,
    balance_window: str | None = None,
    top_n: int = 3,
) -> dict[str, Any]:
    """What our own match database says about this idea, in one balance window.

    Carry-level numbers come straight from the existing discovery analytics
    (`discovery_candidate_for`), so they match the Discoveries page exactly;
    nothing is re-derived here and no external data is involved. On top of
    that it counts how often the idea's other core units and trait targets
    actually appeared alongside the committed carry.
    """
    roster = load_roster()
    fp = comp_fingerprint(experiment, roster)
    window = balance_window or default_balance_window(db)
    result: dict[str, Any] = {
        "balance_window": window,
        "carry": fp["primary_carry"]["name"] if fp["primary_carry"] else None,
        "status": "ok",
    }
    if window is None:
        return {**result, "status": "no_data", "message": "No balance window with match data yet."}
    carry = fp["primary_carry"]
    if carry is None:
        return {**result, "status": "no_carry", "message": "The entry doesn't name a carry, so there's nothing to look up yet."}

    best = None
    for character_id in _ids_in_data(db, carry, window, roster):
        cand = discovery_candidate_for(db, character_id, balance_window=window, top_n=top_n)
        if cand and (best is None or cand.commitment_games > best.commitment_games):
            best = cand
    if best is None:
        return {**result, "status": "no_committed_games",
                "message": f"{carry['name']} has no committed games in {window}.", "commitment_games": 0}

    n = best.commitment_games
    result.update({
        "character_id": best.character_id,
        "commitment_games": n,
        "low_sample": n < LOW_SAMPLE_COMMITMENT_GAMES,
        "appearance_rate": best.appearance_rate,
        "commitment_rate": best.commitment_rate,
        "carry_conversion_rate": best.carry_conversion_rate,
        "avg_placement": best.avg_placement,
        "top4_rate": best.top4_rate,
        "win_rate": best.win_rate,
        "hit_3star_rate": best.hit_3star_rate,
        "opportunity_score": best.opportunity_score,
        "best_partners": [_assoc(a) for a in best.best_partners[:top_n]],
        "best_item_packages": [_assoc(a) for a in best.best_item_packages[:top_n]],
        "best_trait_breakpoints": [_assoc(a) for a in best.best_trait_breakpoints[:top_n]],
    })

    # How often the idea's own pieces showed up with the committed carry.
    partner_games, _, _ = _carry_commitment_games_with_partners(db, best.character_id, window)
    others = [u for u in fp["core_units"] if u["key"] != carry["key"]]
    unit_ids = {u["key"]: set(_ids_in_data(db, u, window, roster)) for u in others}
    result["core_units"] = [
        {"name": u["name"], **_slice([(p, bool(present & unit_ids[u["key"]])) for p, present in partner_games])}
        for u in others
    ]
    result["core_together"] = (
        {"units": [u["name"] for u in others],
         **_slice([(p, all(present & unit_ids[u["key"]] for u in others)) for p, present in partner_games])}
        if len(others) > 1 else None
    )
    result["trait_targets"] = _trait_target_counts(db, best.character_id, window, fp["target_traits"])
    return result


def _trait_target_counts(
    db: Database, character_id: str, window: str, targets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """For each trait target, the carry's committed games where that trait
    was active with at least the target number of units (e.g. 6 Ravager
    means six Ravager units on the board, not trait tier 6)."""
    if not targets:
        return []
    rows = db.query_all(
        """SELECT c.match_id, c.participant_index, p.placement, t.trait_name, t.num_units
           FROM units c
           JOIN participants p ON p.match_id = c.match_id AND p.participant_index = c.participant_index
           JOIN matches m ON m.match_id = c.match_id
           LEFT JOIN traits t
             ON t.match_id = c.match_id AND t.participant_index = c.participant_index AND t.tier_current >= 1
           WHERE c.character_id = ? AND c.completed_item_count >= ? AND m.balance_window = ?""",
        (character_id, _COMMITMENT_ITEMS, window),
    )
    placements: dict[tuple[str, int], int] = {}
    active: dict[tuple[str, int], dict[str, int]] = {}
    for match_id, participant_index, placement, trait_name, num_units in rows:
        game = (match_id, participant_index)
        placements[game] = int(placement)
        if trait_name:
            active.setdefault(game, {})[trait_name] = max(int(num_units or 0), active.get(game, {}).get(trait_name, 0))

    out = []
    for t in targets:
        ids = set(t["trait_ids"]) | {t["name"]}
        need = t["breakpoint"] or 1
        games = [
            (placement, any(active.get(g, {}).get(tid, 0) >= need for tid in ids))
            for g, placement in placements.items()
        ]
        out.append({"name": t["name"], "breakpoint": t["breakpoint"], "known_trait": bool(t["trait_ids"]), **_slice(games)})
    return out


def evidence_summary(ev: dict[str, Any]) -> str:
    """One plain line for a field note or the CLI."""
    if ev["status"] != "ok":
        return ev["message"]
    pct = lambda v: "—" if v is None else f"{v:.0%}"  # noqa: E731
    text = (
        f"{ev['commitment_games']} committed games in {ev['balance_window']}: avg place {ev['avg_placement']:.2f}, "
        f"top 4 {pct(ev['top4_rate'])}, win {pct(ev['win_rate'])}, 3★ hit {pct(ev['hit_3star_rate'])}."
    )
    return text + (" LOW SAMPLE: too few games to trust." if ev["low_sample"] else "")


# ---------------------------------------------------------------- scout run


def scout(db: Database, experiment: Experiment, *, balance_window: str | None = None) -> dict[str, Any]:
    notes = list_field_notes(db, experiment.experiment_id)
    checklist = scout_checklist(notes)
    return {
        "experiment": experiment.slug,
        "title": experiment.title,
        "fingerprint": comp_fingerprint(experiment),
        "riot_evidence": riot_evidence(db, experiment, balance_window=balance_window),
        "checklist": checklist,
        "external_checks_needed": [c["label"] for c in checklist if c["key"] != "riot" and not c["checked"]],
    }


def record_riot_evidence(db: Database, experiment: Experiment, evidence: dict[str, Any]) -> dict[str, Any]:
    """Append the evidence as a dated `riot_evidence` field note. It never
    changes the experiment's evidence status."""
    return add_field_note(
        db, experiment.slug, kind="riot_evidence", body=evidence_summary(evidence),
        source="riot", data=evidence, system=True,
    )
