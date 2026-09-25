from __future__ import annotations

import json
from dataclasses import dataclass

from .analytics import default_balance_window
from .cdragon import SetMetadata
from .riot import RANKED_TFT_QUEUE_ID
from .storage import Database
from .unreal_patch import UNREAL_PATCH_REGISTRY, UNRESOLVED_UNREAL_PATCH


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
    #: How many of `total_matches` are `RANKED_TFT_QUEUE_ID` vs. anything
    #: else (Normal/Hyper Roll/Double Up/unrecorded). Visibility only -- a
    #: partially-completed live run (e.g. one that failed partway through
    #: ingestion) may already have committed non-target-queue matches to
    #: production; this reports that, it does not delete or filter them.
    target_queue_matches: int
    non_target_queue_matches: int
    unit_cost_present_pct: float
    metadata_champion_coverage_pct: float | None
    unknown_champion_ids: list[str] | None
    unknown_item_ids: list[str] | None
    unknown_trait_ids: list[str] | None
    #: Every match with `balance_window IS NULL`, regardless of cause --
    #: this includes matches intentionally left unresolved because their
    #: masked-Unreal `game_version` doesn't fall in any usable
    #: `UNREAL_PATCH_REGISTRY` window (see `unresolved_unreal_matches`)
    #: alongside anything genuinely broken. Kept for backward-compatible
    #: visibility into the raw total; `unexpected_missing_balance_window`
    #: below is what actually drives `is_severe`.
    matches_missing_balance_window: int
    #: Matches missing a `balance_window` for a reason OTHER than the
    #: intentional, documented Unreal rollout-gap exclusion:
    #: `balance_window IS NULL AND (patch IS NULL OR patch !=
    #: UNRESOLVED_UNREAL_PATCH)`. A `NULL` patch counts as unexpected too --
    #: `patch != UNRESOLVED_UNREAL_PATCH` alone would silently exclude it
    #: under SQL's three-valued logic (`NULL != 'x'` is neither true nor
    #: false), which would hide a genuinely broken row. This is the field
    #: `is_severe` actually checks: a store with only intentionally-
    #: unresolved Unreal matches must never fail validation just because
    #: they exist, but anything else missing a balance window still should.
    unexpected_missing_balance_window: int
    malformed_placements: int
    duplicate_match_ids: int
    #: Every stored participant with no stored unit rows (store-wide) --
    #: the sum of the two fields below, kept for backward compatibility.
    participants_without_units: int
    #: How many matches, store-wide, have a masked-Unreal `game_version`
    #: that `resolve_unreal_patch` couldn't place in any registered cutover
    #: (`patch == UNRESOLVED_UNREAL_PATCH`). Always <= `matches_missing_
    #: balance_window` (an unresolved match's `balance_window` is always
    #: `None`, by design -- see `normalize.normalize_match`); broken out
    #: separately so "the ingest pipeline is broken" and "we just haven't
    #: registered this Unreal-era patch cutover yet" aren't conflated.
    unresolved_unreal_matches: int
    #: The `game_datetime` range specifically among unresolved-Unreal
    #: matches (`None` if there are none) -- exactly what's needed to
    #: derive a real `UnrealPatchWindow` for `UNREAL_PATCH_REGISTRY`
    #: without running another ingest just to see it.
    unresolved_unreal_earliest_game_datetime: int | None
    unresolved_unreal_latest_game_datetime: int | None
    #: How many `UNREAL_PATCH_REGISTRY` entries exist vs. how many are
    #: actually usable for resolution (`UnrealPatchWindow.is_usable`:
    #: verified and sourced). A gap between these two numbers means someone
    #: added a provisional/unsourced window that is silently NOT being used
    #: to classify any match -- surfaced here so that never goes unnoticed.
    unreal_registry_total_windows: int
    unreal_registry_usable_windows: int
    #: Diagnostic-only, store-wide (not scoped to `balance_window` like the
    #: fields above -- their whole point is to be useful even when nothing
    #: has resolved into a balance window at all): distinct raw
    #: `game_version` strings, resolved `patch` values, and resolved
    #: `balance_window` values, each mapped to how many matches have them,
    #: plus the earliest/latest `game_datetime` seen. `None` is a valid key
    #: in the patch/balance_window distributions (an unparseable/missing
    #: value). Never includes full match payloads.
    game_version_distribution: dict[str, int]
    client_patch_distribution: dict[str | None, int]
    balance_window_distribution: dict[str | None, int]
    earliest_game_datetime: int | None
    latest_game_datetime: int | None
    #: Participants without units whose own raw Riot payload entry (same
    #: position in `info.participants`, same placement) has a `units` field
    #: that is missing or `[]`: ingestion stored exactly what Riot sent.
    #: A visible warning, never severe on its own. The rows stay stored.
    source_empty_participants: int = 0
    #: Participants without units where the raw payload lists at least one
    #: unit, or the raw/stored mapping can't be trusted (payload unreadable,
    #: no raw participant at that index, placement or participant count
    #: mismatch, `units` not a list). This is lost data -- severe.
    unexpected_participants_without_units: int = 0

    @property
    def is_severe(self) -> bool:
        """Structural corruption, not data-quality nuance.

        Unknown champion/item/trait IDs and a low cost-presence/metadata-
        coverage rate are surfaced as warnings, not severe failures: they
        can legitimately happen right after a patch before CommunityDragon
        updates, or for rare special units. An out-of-range placement, a
        duplicate primary key, a participant with no board at all, or a
        match missing its balance window for anything other than the
        intentional, documented Unreal rollout-gap exclusion
        (`unexpected_missing_balance_window`), or a participant whose units
        were lost between Riot's payload and storage
        (`unexpected_participants_without_units`) indicate the ingest
        pipeline itself is broken. A participant Riot itself sent with no
        units (`source_empty_participants`) is a warning, not corruption.

        Deliberately checks `unexpected_missing_balance_window`, NOT
        `matches_missing_balance_window`: a production database can have
        dozens of matches sitting in the Unreal registry's deliberate
        18.2/18.3 gap (see `tftlab.unreal_patch`) that are correctly,
        intentionally excluded from every balance-window-scoped analytics
        query -- that is expected, safe, and must never fail validation on
        its own. Those matches are still reported prominently (via
        `unresolved_unreal_matches`/`matches_missing_balance_window`), just
        not as a structural-corruption failure.
        """
        return bool(
            self.unexpected_missing_balance_window
            or self.malformed_placements
            or self.duplicate_match_ids
            or self.unexpected_participants_without_units
        )


def _is_source_empty(payload_json: str, participant_index: int, placement: int, stored_participants: int) -> bool:
    """Whether a stored participant with no units was sent that way by Riot.

    `normalize_match` assigns `participant_index` by position in
    `info.participants`, so the raw entry is at that index; placement and
    participant count must agree for the mapping to be trusted. Anything
    that can't be confirmed counts as unexpected (returns False).
    """
    try:
        raw = json.loads(payload_json)["info"]["participants"]
    except (TypeError, ValueError, KeyError):
        return False
    if not isinstance(raw, list) or len(raw) != stored_participants or not 0 <= participant_index < len(raw):
        return False
    entry = raw[participant_index]
    if not isinstance(entry, dict) or entry.get("placement") != placement:
        return False
    return "units" not in entry or entry["units"] == []


def classify_participants_without_units(db: Database) -> tuple[int, int]:
    """(source-empty, unexpected) counts for participants with no stored
    units. Reads raw payloads only for the matches involved."""
    rows = db.query_all(
        """
        SELECT p.match_id, p.participant_index, p.placement
        FROM participants p
        WHERE NOT EXISTS (
            SELECT 1 FROM units u
            WHERE u.match_id = p.match_id AND u.participant_index = p.participant_index
        )
        ORDER BY p.match_id, p.participant_index
        """
    )
    source_empty = 0
    unexpected = 0
    matches: dict[str, tuple[str, int]] = {}
    for match_id, participant_index, placement in rows:
        if match_id not in matches:
            payload = db.query_one("SELECT payload_json FROM matches WHERE match_id = ?", (match_id,))
            stored = db.query_one("SELECT COUNT(*) FROM participants WHERE match_id = ?", (match_id,))[0]
            matches[match_id] = (payload[0] if payload else None, int(stored))
        payload_json, stored_participants = matches[match_id]
        if _is_source_empty(payload_json, int(participant_index), int(placement), stored_participants):
            source_empty += 1
        else:
            unexpected += 1
    return source_empty, unexpected


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
        target_queue_matches = db.query_one(
            "SELECT COUNT(*) FROM matches WHERE balance_window = ? AND queue_id = ?",
            (resolved_window, RANKED_TFT_QUEUE_ID),
        )[0]
        non_target_queue_matches = total_matches - target_queue_matches
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
        target_queue_matches = 0
        non_target_queue_matches = 0
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
    # A NULL patch must count as unexpected too: `patch != ?` alone would
    # silently exclude it under SQL's three-valued logic (NULL != 'x' is
    # neither true nor false), hiding a genuinely broken row behind the
    # intentional-Unreal-gap exclusion it doesn't actually qualify for.
    unexpected_missing_balance_window = db.query_one(
        "SELECT COUNT(*) FROM matches WHERE balance_window IS NULL AND (patch IS NULL OR patch != ?)",
        (UNRESOLVED_UNREAL_PATCH,),
    )[0]
    unresolved_unreal_matches = db.query_one(
        "SELECT COUNT(*) FROM matches WHERE patch = ?", (UNRESOLVED_UNREAL_PATCH,)
    )[0]
    unresolved_unreal_earliest_game_datetime, unresolved_unreal_latest_game_datetime = db.query_one(
        "SELECT MIN(game_datetime), MAX(game_datetime) FROM matches WHERE patch = ?", (UNRESOLVED_UNREAL_PATCH,)
    ) or (None, None)
    unreal_registry_total_windows = len(UNREAL_PATCH_REGISTRY)
    unreal_registry_usable_windows = sum(1 for w in UNREAL_PATCH_REGISTRY if w.is_usable)
    game_version_distribution = {
        str(v): int(n) for v, n in db.query_all("SELECT game_version, COUNT(*) FROM matches GROUP BY game_version")
    }
    client_patch_distribution = {
        v: int(n) for v, n in db.query_all("SELECT patch, COUNT(*) FROM matches GROUP BY patch")
    }
    balance_window_distribution = {
        v: int(n) for v, n in db.query_all("SELECT balance_window, COUNT(*) FROM matches GROUP BY balance_window")
    }
    earliest_game_datetime, latest_game_datetime = db.query_one(
        "SELECT MIN(game_datetime), MAX(game_datetime) FROM matches"
    ) or (None, None)
    malformed_placements = db.query_one(
        "SELECT COUNT(*) FROM participants WHERE placement < 1 OR placement > 8"
    )[0]
    duplicate_match_ids = db.query_one(
        "SELECT COUNT(*) FROM (SELECT match_id FROM matches GROUP BY match_id HAVING COUNT(*) > 1) dupes"
    )[0]
    source_empty_participants, unexpected_participants_without_units = classify_participants_without_units(db)
    participants_without_units = source_empty_participants + unexpected_participants_without_units

    return IntegrityReport(
        balance_window=resolved_window,
        total_matches=total_matches,
        target_queue_matches=target_queue_matches,
        non_target_queue_matches=non_target_queue_matches,
        total_participants=total_participants,
        unit_cost_present_pct=unit_cost_present_pct,
        metadata_champion_coverage_pct=metadata_champion_coverage_pct,
        unknown_champion_ids=unknown_champion_ids,
        unknown_item_ids=unknown_item_ids,
        unknown_trait_ids=unknown_trait_ids,
        matches_missing_balance_window=matches_missing_balance_window,
        unexpected_missing_balance_window=unexpected_missing_balance_window,
        malformed_placements=malformed_placements,
        duplicate_match_ids=duplicate_match_ids,
        participants_without_units=participants_without_units,
        unresolved_unreal_matches=unresolved_unreal_matches,
        unresolved_unreal_earliest_game_datetime=unresolved_unreal_earliest_game_datetime,
        unresolved_unreal_latest_game_datetime=unresolved_unreal_latest_game_datetime,
        unreal_registry_total_windows=unreal_registry_total_windows,
        unreal_registry_usable_windows=unreal_registry_usable_windows,
        game_version_distribution=game_version_distribution,
        client_patch_distribution=client_patch_distribution,
        balance_window_distribution=balance_window_distribution,
        earliest_game_datetime=earliest_game_datetime,
        latest_game_datetime=latest_game_datetime,
        source_empty_participants=source_empty_participants,
        unexpected_participants_without_units=unexpected_participants_without_units,
    )
