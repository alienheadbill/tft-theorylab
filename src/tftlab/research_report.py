"""Read-only Discovery research report.

Runs the canonical analytics (`carry_commitment_stats`, `discover_candidates`)
over one balance window of an existing database and returns everything as
plain data: dataset integrity counts, every carry candidate with its full
evidence, sample-aware evidence bands, and (optionally) a comparison of the
current carry rule against the PR #21 stat-only baseline on the same rows.

It never writes. The CLI opens the database with `Database.open_existing`
(Postgres `default_transaction_read_only=on`, SQLite `mode=ro`) and
`assert_read_only` refuses to continue unless the server itself reports a
read-only transaction; every statement here is a SELECT. Output holds only
aggregates and item/champion/trait ids -- no payloads, match ids, PUUIDs or
Riot IDs.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import carry
from .analytics import carry_commitment_stats, discover_candidates
from .analytics.commitment import default_balance_window
from .items import ITEM_INTENT_PATH, ITEM_STATS_PATH, is_component
from .scout import LOW_SAMPLE_COMMITMENT_GAMES
from .storage import Database

# ---------------------------------------------------------------- evidence bands

#: Reporting aid only -- not a production classification. Each boundary is
#: an existing repo threshold: the web Discovery default `min_samples`
#: (10), the product's LOW SAMPLE label (`LOW_SAMPLE_COMMITMENT_GAMES`,
#: 30), and the commitment-stats confidence prior strength (60 games, where
#: confidence = n / (n + 60) reaches 0.5 and the data outweighs the prior).
WEB_DISCOVERY_MIN_SAMPLES = 10
CONFIDENCE_PRIOR_GAMES = 60
EVIDENCE_BANDS = (
    ("A_substantial", CONFIDENCE_PRIOR_GAMES),
    ("B_moderate", LOW_SAMPLE_COMMITMENT_GAMES),
    ("C_early", WEB_DISCOVERY_MIN_SAMPLES),
    ("D_too_little", 0),
)


def evidence_band(commitment_games: int) -> str:
    for band, minimum in EVIDENCE_BANDS:
        if commitment_games >= minimum:
            return band
    return EVIDENCE_BANDS[-1][0]


# ---------------------------------------------------------------- PR #21 baseline

#: PR #21's stat vocabulary (tftlab.carry at 9c6354d), kept only to
#: reproduce that rule for comparison: a board was excluded only when every
#: completed item was stat-DEFENSIVE (a defensive stat and no offensive one).
_PR21_OFFENSIVE_EFFECTS = frozenset({
    "AD", "AP", "AS", "CritChance", "AD_NotStatBar", "AP_NotStatBar", "ADIncrease", "APIncrease",
    "StackingAD", "StackingSP", "ADOnAttack", "ADPerBonus", "APPerBonus", "ASPerStack",
    "AttackSpeedPerStack", "ADAPPerTakedown", "CritDamageToGive", "CritDamageBonusPercent", "DamageAmp",
})
_PR21_OFFENSIVE_TAGS = frozenset({"AttackDamage", "AbilityPower", "AttackSpeed", "CritChance"})
_PR21_DEFENSIVE_EFFECTS = frozenset({"Health", "Armor", "MagicResist"})
_PR21_DEFENSIVE_TAGS = frozenset({"Health"})


def pr21_defensive(meta: Mapping[str, Any]) -> bool:
    effects, tags = set(meta.get("stat_effects") or ()), set(meta.get("tags") or ())
    if effects & _PR21_OFFENSIVE_EFFECTS or tags & _PR21_OFFENSIVE_TAGS:
        return False
    return bool(effects & _PR21_DEFENSIVE_EFFECTS or tags & _PR21_DEFENSIVE_TAGS)


def pr21_baseline_intents(item_stats: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, dict[str, str]]:
    """The PR #21 rule expressed in the current machinery: its defensive
    items as TANK (no carry evidence), everything else unlisted (UNKNOWN,
    carry evidence) -- "excluded only when every completed item is
    defensive", exactly."""
    stats = item_stats if item_stats is not None else json.loads(ITEM_STATS_PATH.read_text())["items"]
    return {
        item_id: {"intent": carry.TANK}
        for item_id, meta in stats.items()
        if not is_component(item_id) and "component" not in (meta.get("tags") or ()) and pr21_defensive(meta)
    }


@contextmanager
def eligibility_intents(intents: Mapping[str, Mapping[str, Any]]) -> Iterator[None]:
    """Evaluate the canonical analytics under another item-intent map (for
    the baseline comparison). Process-local; restores the committed one."""
    original = carry.load_item_intent
    carry.load_item_intent = lambda path=None: dict(intents)  # type: ignore[assignment]
    try:
        yield
    finally:
        carry.load_item_intent = original  # type: ignore[assignment]


# ---------------------------------------------------------------- read-only guard


class NotReadOnly(RuntimeError):
    pass


def assert_read_only(db: Database) -> str:
    """Refuse to report unless the connection cannot write: it must come
    from `Database.open_existing`, and a Postgres server must itself report
    `transaction_read_only = on` (SQLite read-only connections are opened
    `mode=ro`)."""
    if not db.read_only:
        raise NotReadOnly("open the database with Database.open_existing")
    if db.dialect == "postgres":
        value = str(db.query_one("SHOW transaction_read_only")[0]).lower()
        if value != "on":
            raise NotReadOnly(f"transaction_read_only = {value}")
        return "postgres transaction_read_only=on"
    return "sqlite mode=ro"


# ---------------------------------------------------------------- dataset


def dataset_summary(db: Database, balance_window: str) -> dict[str, Any]:
    """Counts and integrity checks, store-wide and for `balance_window`."""
    q1 = lambda sql, params=(): db.query_one(sql, params)  # noqa: E731
    window_matches, earliest, latest = q1(
        "SELECT COUNT(*), MIN(game_datetime), MAX(game_datetime) FROM matches WHERE balance_window = ?", (balance_window,)
    )
    participants, unit_observable = q1(
        """SELECT COUNT(*),
                  SUM(CASE WHEN EXISTS (SELECT 1 FROM units u WHERE u.match_id = p.match_id
                                        AND u.participant_index = p.participant_index) THEN 1 ELSE 0 END)
           FROM participants p JOIN matches m ON m.match_id = p.match_id WHERE m.balance_window = ?""",
        (balance_window,),
    )
    rows = db.query_all("SELECT patch, COUNT(*) FROM matches GROUP BY patch ORDER BY patch")
    windows = db.query_all("SELECT balance_window, COUNT(*) FROM matches GROUP BY balance_window ORDER BY balance_window")
    total, distinct = q1("SELECT COUNT(*), COUNT(DISTINCT match_id) FROM matches")
    bad_placements = q1(
        """SELECT COUNT(*) FROM participants p JOIN matches m ON m.match_id = p.match_id
           WHERE m.balance_window = ? AND (p.placement IS NULL OR p.placement < 1 OR p.placement > 8)""",
        (balance_window,),
    )[0]
    irregular = q1(
        """SELECT COUNT(*) FROM (
             SELECT p.match_id FROM participants p JOIN matches m ON m.match_id = p.match_id
             WHERE m.balance_window = ?
             GROUP BY p.match_id
             HAVING COUNT(*) <> 8 OR COUNT(DISTINCT p.placement) <> COUNT(*)
           ) t""",
        (balance_window,),
    )[0]
    return {
        "balance_window": balance_window,
        "window_matches": int(window_matches or 0),
        "window_participants": int(participants or 0),
        "window_unit_observable_participants": int(unit_observable or 0),
        "window_source_empty_participants": int((participants or 0) - (unit_observable or 0)),
        "window_earliest_game_datetime_ms": earliest,
        "window_latest_game_datetime_ms": latest,
        "store_matches": int(total or 0),
        "store_duplicate_match_ids": int((total or 0) - (distinct or 0)),
        "store_patch_distribution": {str(p): int(n) for p, n in rows},
        "store_balance_window_distribution": {str(w): int(n) for w, n in windows},
        "store_matches_without_balance_window": int(sum(n for w, n in windows if w is None)),
        "window_malformed_placements": int(bad_placements or 0),
        "window_matches_not_8_distinct_placements": int(irregular or 0),
    }


# ---------------------------------------------------------------- candidates


def _association(a: Any) -> dict[str, Any]:
    return {k: v for k, v in asdict(a).items()}


def _candidates(db: Database, window: str, top_n: int) -> dict[str, dict[str, Any]]:
    """Every carry with >= 1 commitment game (cost 1-5): CarryStat fields
    plus the Discovery candidate's score, components and associations."""
    stats = {s.character_id: s for s in carry_commitment_stats(db, balance_window=window, min_cost=1, max_cost=5, min_samples=1)}
    found = discover_candidates(db, balance_window=window, min_cost=1, max_cost=5, min_samples=1, top_n=top_n)
    out: dict[str, dict[str, Any]] = {}
    for c in found:
        s = stats[c.character_id]
        out[c.character_id] = {
            "character_id": c.character_id, "name": c.name, "cost": c.cost,
            "appearances": s.appearances, "appearance_rate": s.appearance_rate,
            "commitment_games": s.commitment_games, "commitment_rate": s.commitment_rate,
            "carry_conversion_rate": s.carry_conversion_rate,
            "avg_placement": s.avg_placement, "top4_rate": s.top4_rate, "win_rate": s.win_rate,
            "hit_3star_games": s.hit_games, "hit_3star_rate": s.hit_3star_rate, "miss_games": s.miss_games,
            "avg_placement_hit": s.avg_placement_hit, "hit_top4_rate": s.hit_top4_rate,
            "avg_placement_miss": s.avg_placement_miss, "miss_top4_rate": s.miss_top4_rate,
            "posterior_top4": s.posterior_top4, "confidence": c.confidence,
            "opportunity_score": c.opportunity_score, "opportunity_components": dict(c.opportunity_components),
            "evidence_band": evidence_band(s.commitment_games),
            "best_partners": [_association(a) for a in c.best_partners],
            "best_item_packages": [_association(a) for a in c.best_item_packages],
            "best_trait_breakpoints": [_association(a) for a in c.best_trait_breakpoints],
        }
    ranked = sorted(out.values(), key=lambda r: -r["opportunity_score"])
    for i, row in enumerate(ranked, 1):
        row["rank_overall"] = i
    tiers: dict[int, int] = defaultdict(int)
    web_rank = 0
    for row in ranked:
        tiers[row["cost"]] += 1
        row["rank_in_cost"] = tiers[row["cost"]]
        if row["cost"] <= 3 and row["commitment_games"] >= WEB_DISCOVERY_MIN_SAMPLES:
            web_rank += 1
            row["rank_web_discovery"] = web_rank  # the default /api/discovery list (cost <= 3, >= 10 games)
        else:
            row["rank_web_discovery"] = None
    return out


# ---------------------------------------------------------------- baseline comparison


def _packages(items_json: str) -> tuple[str, ...]:
    return tuple(sorted(i for i in json.loads(items_json or "[]") if i and not is_component(i)))


def _package_changes(db: Database, window: str, baseline: Mapping[str, Mapping[str, Any]], top: int) -> dict[str, Any]:
    """Per champion, the unit-instance item packages that qualify under only
    one rule, most common first, with each item's current Riot intent and
    PR #21 stat class -- the exact reason a commitment moved."""
    current = carry.load_item_intent()
    lost: dict[str, Counter] = defaultdict(Counter)
    gained: dict[str, Counter] = defaultdict(Counter)
    rows = db.query_all(
        "SELECT u.character_id, u.items_json FROM units u JOIN matches m ON m.match_id = u.match_id WHERE m.balance_window = ?",
        (window,),
    )
    for character_id, items_json in rows:
        items = json.loads(items_json or "[]")
        old = carry.is_carry_observation(items, item_intents=baseline)
        new = carry.is_carry_observation(items, item_intents=current)
        if old and not new:
            lost[character_id][_packages(items_json)] += 1
        elif new and not old:
            gained[character_id][_packages(items_json)] += 1

    def describe(counter: Counter) -> list[dict[str, Any]]:
        return [
            {"items": list(package), "unit_instances": n,
             "current_intents": {i: carry.item_intent(i, current) for i in package},
             "pr21_defensive": {i: i in baseline for i in package}}
            for package, n in counter.most_common(top)
        ]

    champions = sorted(set(lost) | set(gained))
    return {c: {"lost_unit_instances": sum(lost[c].values()), "gained_unit_instances": sum(gained[c].values()),
                "top_lost_packages": describe(lost[c]), "top_gained_packages": describe(gained[c])} for c in champions}


def champion_appearances(db: Database, window: str) -> dict[str, dict[str, Any]]:
    """Every champion seen in the window: games it appeared in (one per
    participant board, duplicate copies counted once), name and cost."""
    rows = db.query_all(
        """SELECT t.character_id, MAX(t.unit_name), MAX(t.cost), COUNT(*) FROM (
             SELECT u.character_id, MAX(u.unit_name) AS unit_name, MAX(u.cost) AS cost
             FROM units u JOIN matches m ON m.match_id = u.match_id
             WHERE m.balance_window = ?
             GROUP BY u.character_id, u.match_id, u.participant_index
           ) t GROUP BY t.character_id""",
        (window,),
    )
    return {cid: {"name": name, "cost": int(cost or 0), "appearances": int(n)} for cid, name, cost, n in rows}


def compare_rules(current: dict[str, dict[str, Any]], baseline: dict[str, dict[str, Any]],
                  appearances: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Per champion (every champion seen, committed or not) under both rules."""
    seen = appearances or {}
    ids = sorted(set(current) | set(baseline) | set(seen))
    rows = []
    for cid in ids:
        new, old = current.get(cid), baseline.get(cid)
        ref = new or old or seen[cid]
        rows.append({
            "character_id": cid, "name": ref["name"], "cost": ref["cost"],
            "appearances": seen[cid]["appearances"] if cid in seen else ref["appearances"],
            "commitment_games_pr21": old["commitment_games"] if old else 0,
            "commitment_games_pr22": new["commitment_games"] if new else 0,
            "commitment_rate_pr21": old["commitment_rate"] if old else 0.0,
            "commitment_rate_pr22": new["commitment_rate"] if new else 0.0,
            "opportunity_score_pr21": old["opportunity_score"] if old else None,
            "opportunity_score_pr22": new["opportunity_score"] if new else None,
            "rank_overall_pr21": old["rank_overall"] if old else None,
            "rank_overall_pr22": new["rank_overall"] if new else None,
            "rank_web_discovery_pr21": old["rank_web_discovery"] if old else None,
            "rank_web_discovery_pr22": new["rank_web_discovery"] if new else None,
        })
    for r in rows:
        r["commitment_change"] = r["commitment_games_pr22"] - r["commitment_games_pr21"]
    total_old = sum(r["commitment_games_pr21"] for r in rows)
    total_new = sum(r["commitment_games_pr22"] for r in rows)
    return {
        "total_commitment_games_pr21": total_old,
        "total_commitment_games_pr22": total_new,
        "absolute_change": total_new - total_old,
        "percent_change": (total_new - total_old) / total_old if total_old else None,
        "champions": rows,
    }


# ---------------------------------------------------------------- report


def build_report(db: Database, *, balance_window: str | None = None, top_n: int = 8, baseline: bool = True,
                 package_top: int = 8) -> dict[str, Any]:
    read_only = assert_read_only(db)  # before any query
    window = balance_window or default_balance_window(db)
    if window is None:
        raise ValueError("no balance window in the store")
    intent_snapshot = json.loads(ITEM_INTENT_PATH.read_text())
    report: dict[str, Any] = {
        "read_only_connection": read_only,
        "carry_rule": "PR #22 Riot item intent (tftlab.carry)",
        "item_intent_snapshot": intent_snapshot.get("_fetched"),
        "evidence_bands": {band: f">= {minimum} commitment games" for band, minimum in EVIDENCE_BANDS},
        "evidence_bands_note": "reporting aid, not a production classification",
        "dataset": dataset_summary(db, window),
        "candidates": sorted(_candidates(db, window, top_n).values(), key=lambda r: r["rank_overall"]),
    }
    if baseline:
        intents = pr21_baseline_intents()
        with eligibility_intents(intents):
            old = _candidates(db, window, top_n)
        current = {r["character_id"]: r for r in report["candidates"]}
        report["pr21_comparison"] = compare_rules(current, old, champion_appearances(db, window))
        report["pr21_package_changes"] = _package_changes(db, window, intents, package_top)
        report["pr21_baseline_candidates"] = sorted(old.values(), key=lambda r: r["rank_overall"])
    return report


_CSV_FIELDS = (
    "rank_overall", "rank_web_discovery", "rank_in_cost", "evidence_band", "character_id", "name", "cost",
    "appearances", "appearance_rate", "commitment_games", "commitment_rate", "carry_conversion_rate",
    "avg_placement", "top4_rate", "win_rate", "hit_3star_games", "hit_3star_rate", "miss_games",
    "avg_placement_hit", "hit_top4_rate", "avg_placement_miss", "miss_top4_rate", "posterior_top4",
    "confidence", "opportunity_score",
)


def write_report(report: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = str(report["dataset"]["balance_window"]).replace("/", "_").replace(" ", "_")
    paths = [out_dir / f"discovery_{tag}_full.json", out_dir / f"discovery_{tag}_candidates.csv"]
    paths[0].write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    with paths[1].open("w", newline="") as fh:
        writer = csv.writer(fh)
        components = sorted({k for r in report["candidates"] for k in r["opportunity_components"]})
        writer.writerow([*_CSV_FIELDS, *(f"component_{k}" for k in components), "top_partner", "top_item_package", "top_trait"])
        for r in report["candidates"]:
            top = lambda key: (r[key][0]["label"] if r[key] else "")  # noqa: E731
            writer.writerow([*(r[f] for f in _CSV_FIELDS), *(r["opportunity_components"].get(k) for k in components),
                             top("best_partners"), top("best_item_packages"), top("best_trait_breakpoints")])
    if "pr21_comparison" in report:
        path = out_dir / f"discovery_{tag}_pr21_vs_pr22.csv"
        rows = report["pr21_comparison"]["champions"]
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["character_id"])
            writer.writeheader()
            writer.writerows(rows)
        paths.append(path)
    return paths
