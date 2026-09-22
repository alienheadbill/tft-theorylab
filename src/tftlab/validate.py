from __future__ import annotations

import json
from dataclasses import dataclass

from .analytics import default_balance_window
from .cdragon import SetMetadata
from .storage import Database


@dataclass(frozen=True)
class IntegrityReport:
    """Data-integrity snapshot for one balance window's ingested data.

    `unknown_champion_ids`/`unknown_item_ids`/`unknown_trait_ids` and
    `metadata_champion_coverage_pct` are `None` (not an empty list/0.0)
    when no CommunityDragon metadata was supplied to cross-check against --
    distinguishing "checked, found none" from "not checked" matters for a
    tool whose whole point is not pretending everything is fine.

    `unit_cost_present_pct` intentionally does NOT mean "authoritative
    CommunityDragon cost": `normalize.cost_from_unit` falls back to
    `rarity + 1` whenever a `cost_lookup` (e.g. CommunityDragon) returns
    `None`, so a unit can have a non-null `cost` purely from that fallback.
    A champion CommunityDragon has never heard of can therefore still show
    up with a numeric cost -- which is exactly why `unknown_champion_ids`
    is computed from *all* observed champion IDs, not from the ones with a
    null cost, and why `metadata_champion_coverage_pct` exists as a
    separate, honest measure of how much of the data is actually backed by
    CommunityDragon rather than the rarity+1 heuristic.
    """

    balance_window: str | None
    total_matches: int
    total_participants: int
    unit_cost_present_pct: float
    metadata_champion_coverage_pct: float | None
    unknown_champion_ids: list[str] | None
    unknown_item_ids: list[str] | None
    unknown_trait_ids: list[str] | None
    matches_missing_balance_window: int
    malformed_placements: int
    duplicate_match_ids: int
    participants_without_units: int

    @property
    def is_severe(self) -> bool:
        """Structural corruption, not data-quality nuance.

        Unknown champion/item/trait IDs and a low cost-presence/metadata-
        coverage rate are surfaced as warnings, not severe failures: they
        can legitimately happen right after a patch before CommunityDragon
        updates, or for rare special units. A missing balance window, an
        out-of-range placement, a duplicate primary key, or a participant
        with no board at all indicate the ingest pipeline itself is broken.
        """
        return bool(
            self.matches_missing_balance_window
            or self.malformed_placements
            or self.duplicate_match_ids
            or self.participants_without_units
        )


def validate_live_data(
    db: Database,
    *,
    balance_window: str | None = None,
    metadata: SetMetadata | None = None,
) -> IntegrityReport:
    """Check ingested data for the active (or given) balance window.

    Pass `metadata` (a `tftlab.cdragon.SetMetadata`, typically from a live
    CommunityDragon fetch) to also cross-check observed champion/item/trait
    IDs against it and compute `metadata_champion_coverage_pct`; without
    it, those four fields report `None` (skipped) rather than a possibly-
    wrong empty list/percentage.
    """
    resolved_window = balance_window or default_balance_window(db)

    champion_unit_counts: dict[str, int] = {}
    observed_items: set[str] = set()
    observed_traits: set[str] = set()
    total_units = 0

    if resolved_window is not None:
        total_matches = db.query_one(
            "SELECT COUNT(*) FROM matches WHERE balance_window = ?", (resolved_window,)
        )[0]
        total_participants = db.query_one(
            """
            SELECT COUNT(*) FROM participants p
            JOIN matches m ON m.match_id = p.match_id
            WHERE m.balance_window = ?
            """,
            (resolved_window,),
        )[0]

        cost_present_units, total_units = db.query_one(
            """
            SELECT SUM(CASE WHEN u.cost IS NOT NULL THEN 1 ELSE 0 END), COUNT(*)
            FROM units u
            JOIN matches m ON m.match_id = u.match_id
            WHERE m.balance_window = ?
            """,
            (resolved_window,),
        ) or (0, 0)
        total_units = total_units or 0
        unit_cost_present_pct = ((cost_present_units or 0) / total_units) if total_units else 0.0

        # Every distinct champion actually observed, regardless of whether
        # its units resolved a cost -- rarity+1 can populate `cost` for a
        # champion CommunityDragon has never heard of, so "unknown" must
        # never be inferred from `cost IS NULL`.
        champion_unit_counts = {
            str(character_id): int(n)
            for character_id, n in db.query_all(
                """
                SELECT u.character_id, COUNT(*) AS n
                FROM units u
                JOIN matches m ON m.match_id = u.match_id
                WHERE m.balance_window = ?
                GROUP BY u.character_id
                """,
                (resolved_window,),
            )
        }

        for (items_json,) in db.query_all(
            """
            SELECT items_json FROM units u
            JOIN matches m ON m.match_id = u.match_id
            WHERE m.balance_window = ?
            """,
            (resolved_window,),
        ):
            observed_items.update(json.loads(items_json))

        observed_traits = {
            str(r[0])
            for r in db.query_all(
                """
                SELECT DISTINCT t.trait_name FROM traits t
                JOIN matches m ON m.match_id = t.match_id
                WHERE m.balance_window = ?
                """,
                (resolved_window,),
            )
        }
    else:
        total_matches = 0
        total_participants = 0
        unit_cost_present_pct = 0.0

    if metadata is not None:
        unknown_champion_ids: list[str] | None = sorted(
            cid for cid in champion_unit_counts if cid not in metadata.champions
        )
        unknown_item_ids: list[str] | None = sorted(i for i in observed_items if i not in metadata.items)
        unknown_trait_ids: list[str] | None = sorted(t for t in observed_traits if t not in metadata.traits)

        known_units = sum(n for cid, n in champion_unit_counts.items() if cid in metadata.champions)
        metadata_champion_coverage_pct: float | None = (known_units / total_units) if total_units else 0.0
    else:
        unknown_champion_ids = None
        unknown_item_ids = None
        unknown_trait_ids = None
        metadata_champion_coverage_pct = None

    matches_missing_balance_window = db.query_one(
        "SELECT COUNT(*) FROM matches WHERE balance_window IS NULL"
    )[0]
    malformed_placements = db.query_one(
        "SELECT COUNT(*) FROM participants WHERE placement < 1 OR placement > 8"
    )[0]
    duplicate_match_ids = db.query_one(
        "SELECT COUNT(*) FROM (SELECT match_id FROM matches GROUP BY match_id HAVING COUNT(*) > 1) dupes"
    )[0]
    participants_without_units = db.query_one(
        """
        SELECT COUNT(*) FROM participants p
        WHERE NOT EXISTS (
            SELECT 1 FROM units u
            WHERE u.match_id = p.match_id AND u.participant_index = p.participant_index
        )
        """
    )[0]

    return IntegrityReport(
        balance_window=resolved_window,
        total_matches=total_matches,
        total_participants=total_participants,
        unit_cost_present_pct=unit_cost_present_pct,
        metadata_champion_coverage_pct=metadata_champion_coverage_pct,
        unknown_champion_ids=unknown_champion_ids,
        unknown_item_ids=unknown_item_ids,
        unknown_trait_ids=unknown_trait_ids,
        matches_missing_balance_window=matches_missing_balance_window,
        malformed_placements=malformed_placements,
        duplicate_match_ids=duplicate_match_ids,
        participants_without_units=participants_without_units,
    )
