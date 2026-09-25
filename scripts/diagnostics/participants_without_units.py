"""Read-only diagnostic: stored participants that have no stored units.

Run by `.github/workflows/read-only-data-diagnostics.yml` against the
production database. It is deliberately NOT built on `tftlab.storage`:
opening `Database(...)` creates schema and runs migrations/backfills, and
none of that may happen here. It connects with psycopg directly and:

1. opens the session with `default_transaction_read_only=on` (server-side),
2. runs `BEGIN TRANSACTION READ ONLY` and aborts unless the server then
   reports `transaction_read_only = on`,
3. executes only the fixed SELECT statements in this file (every statement
   goes through `_read`, which refuses anything else),
4. always ends with `ROLLBACK`.

No SQL is accepted from outside. Output is limited to counts and a few
scalar game fields -- never the payload itself, PUUIDs or Riot IDs.

`participant_index` is the participant's position in the payload's
`info.participants` list (see `normalize_match` in `tftlab/normalize.py`,
`for idx, p in enumerate(info.get("participants", []))`), so stored
participant N is raw participant N. A raw `units` value that is missing
becomes `[]`; a JSON null would make normalization raise and the whole match
would never have been stored; every element of a non-empty list becomes one
unit row (nothing is filtered).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Sequence

#: Participants with no rows in `units`.
LOCATE_SQL = """
SELECT p.match_id, p.participant_index, p.placement, p.level,
       m.game_datetime, m.patch, m.balance_window, m.queue_id,
       (SELECT COUNT(*) FROM units u
         WHERE u.match_id = p.match_id AND u.participant_index = p.participant_index) AS stored_units
FROM participants p
JOIN matches m ON m.match_id = p.match_id
WHERE NOT EXISTS (
    SELECT 1 FROM units u
    WHERE u.match_id = p.match_id AND u.participant_index = p.participant_index
)
ORDER BY p.match_id, p.participant_index
"""

#: Every raw participant of the given matches next to its stored rows. Only
#: named scalar fields are extracted from the payload, inside the database.
RAW_VS_STORED_SQL = """
SELECT m.match_id,
       (t.ord - 1)::int                                          AS participant_index,
       t.rp ->> 'placement'                                      AS raw_placement,
       (t.rp ? 'units')                                          AS has_units_key,
       jsonb_typeof(t.rp -> 'units')                             AS units_type,
       CASE WHEN jsonb_typeof(t.rp -> 'units') = 'array'
            THEN jsonb_array_length(t.rp -> 'units') END         AS raw_units,
       (SELECT COUNT(*) FROM units u
         WHERE u.match_id = m.match_id AND u.participant_index = t.ord - 1) AS stored_units,
       (SELECT p.placement FROM participants p
         WHERE p.match_id = m.match_id AND p.participant_index = t.ord - 1) AS stored_placement,
       t.rp ->> 'level'                                          AS level,
       t.rp ->> 'last_round'                                     AS last_round,
       t.rp ->> 'time_eliminated'                                AS time_eliminated,
       t.rp ->> 'players_eliminated'                             AS players_eliminated,
       t.rp ->> 'gold_left'                                      AS gold_left,
       t.rp ->> 'total_damage_to_players'                        AS total_damage_to_players,
       CASE WHEN jsonb_typeof(t.rp -> 'augments') = 'array'
            THEN jsonb_array_length(t.rp -> 'augments') END      AS augment_count
FROM matches m,
     jsonb_array_elements(m.payload_json::jsonb -> 'info' -> 'participants') WITH ORDINALITY AS t(rp, ord)
WHERE m.match_id = ANY(%s)
ORDER BY m.match_id, t.ord
"""

#: Stored-side totals for the same matches.
STORED_TOTALS_SQL = """
SELECT m.match_id,
       (SELECT COUNT(*) FROM participants p WHERE p.match_id = m.match_id) AS stored_participants,
       (SELECT COUNT(*) FROM units u WHERE u.match_id = m.match_id)        AS stored_units
FROM matches m
WHERE m.match_id = ANY(%s)
ORDER BY m.match_id
"""

QUERIES = (LOCATE_SQL, RAW_VS_STORED_SQL, STORED_TOTALS_SQL)

_READ_ONLY_START = re.compile(r"^\s*(SELECT|SHOW)\b", re.IGNORECASE)
_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|CALL|COPY|VACUUM|REINDEX|CLUSTER|LOCK|COMMIT)\b",
    re.IGNORECASE,
)

CASE_A = "CASE A — SOURCE-EMPTY BOARD"
CASE_B = "CASE B — INGESTION / NORMALIZATION BUG"
CASE_C = "CASE C — INCONCLUSIVE"
NO_ANOMALY = "NO ANOMALY — every stored participant has stored units"


class NotReadOnly(RuntimeError):
    pass


def _read(cur: Any, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """The only way this script runs SQL: SELECT/SHOW, nothing else."""
    if not _READ_ONLY_START.match(sql) or _WRITE_WORDS.search(sql):
        raise NotReadOnly("refusing to run a non-SELECT statement")
    cur.execute(sql, params)
    return cur.fetchall()


@dataclass(frozen=True)
class RawParticipant:
    match_id: str
    participant_index: int
    raw_placement: str | None
    has_units_key: bool
    units_type: str | None
    raw_units: int | None
    stored_units: int
    stored_placement: int | None
    level: str | None
    last_round: str | None
    time_eliminated: str | None
    players_eliminated: str | None
    gold_left: str | None
    total_damage_to_players: str | None
    augment_count: int | None


def classify_participant(p: RawParticipant) -> str:
    """One anomalous participant (stored with zero units)."""
    if p.stored_placement is None or str(p.stored_placement) != str(p.raw_placement):
        return CASE_C  # stored row doesn't line up with the raw one
    if p.stored_units != 0:
        return CASE_C
    if not p.has_units_key or (p.units_type == "array" and p.raw_units == 0):
        return CASE_A
    if p.units_type == "array" and (p.raw_units or 0) >= 1:
        return CASE_B
    return CASE_C  # e.g. units is JSON null or not a list


def classify(
    anomalies: Sequence[tuple],
    raw_rows: Sequence[RawParticipant],
    stored_participants: dict[str, int],
) -> str:
    if not anomalies:
        return NO_ANOMALY
    by_key = {(r.match_id, r.participant_index): r for r in raw_rows}
    verdicts = []
    for anomaly in anomalies:
        raw = by_key.get((anomaly[0], anomaly[1]))
        verdicts.append(CASE_C if raw is None else classify_participant(raw))
    # Any other participant in these matches whose raw and stored unit
    # counts disagree is evidence of a broader ingestion mismatch.
    for r in raw_rows:
        if r.units_type == "array" and r.raw_units != r.stored_units:
            verdicts.append(CASE_B)
    for match_id in {a[0] for a in anomalies}:
        raw_count = sum(1 for r in raw_rows if r.match_id == match_id)
        if raw_count != stored_participants.get(match_id):
            verdicts.append(CASE_C)
    for case in (CASE_B, CASE_C):
        if case in verdicts:
            return case
    return CASE_A


def _fmt(value: Any) -> str:
    return "(missing)" if value is None else str(value)


def run(conn: Any, out=sys.stdout) -> str:
    """Run the diagnostic on an open psycopg connection (autocommit mode)."""
    with conn.cursor() as cur:
        cur.execute("BEGIN TRANSACTION READ ONLY")
        try:
            if _read(cur, "SHOW transaction_read_only") != [("on",)]:
                raise NotReadOnly("server did not confirm a read-only transaction")
            print("Read-only transaction confirmed (transaction_read_only = on).", file=out)

            anomalies = _read(cur, LOCATE_SQL)
            print(f"\n[Query 1] Participants without stored units: {len(anomalies)}", file=out)
            print("match_id | participant_index | placement | level | game_datetime | patch | balance_window | queue_id | stored_units", file=out)
            for a in anomalies:
                print(" | ".join(_fmt(v) for v in a), file=out)

            match_ids = sorted({a[0] for a in anomalies})
            raw_rows = [RawParticipant(*row) for row in _read(cur, RAW_VS_STORED_SQL, (match_ids,))] if match_ids else []
            totals = _read(cur, STORED_TOTALS_SQL, (match_ids,)) if match_ids else []
            stored_participants = {t[0]: int(t[1]) for t in totals}
            stored_units_total = {t[0]: int(t[2]) for t in totals}

            for match_id in match_ids:
                rows = [r for r in raw_rows if r.match_id == match_id]
                print(f"\n[Query 2] Match {match_id}", file=out)
                print(
                    "participant_index | placement | has_units_key | units_type | raw_units | stored_units | level | "
                    "last_round | time_eliminated | players_eliminated | gold_left | total_damage_to_players | augment_count",
                    file=out,
                )
                for r in rows:
                    print(
                        " | ".join(
                            _fmt(v)
                            for v in (
                                r.participant_index, r.raw_placement, r.has_units_key, r.units_type, r.raw_units,
                                r.stored_units, r.level, r.last_round, r.time_eliminated, r.players_eliminated,
                                r.gold_left, r.total_damage_to_players, r.augment_count,
                            )
                        ),
                        file=out,
                    )
                print(f"raw participant count: {len(rows)}", file=out)
                print(f"stored participant count: {stored_participants.get(match_id, 0)}", file=out)
                print(f"total raw unit count: {sum(r.raw_units or 0 for r in rows)}", file=out)
                print(f"total stored unit count: {stored_units_total.get(match_id, 0)}", file=out)

            verdict = classify(anomalies, raw_rows, stored_participants)
            print(f"\nCLASSIFICATION: {verdict}", file=out)
            return verdict
        finally:
            cur.execute("ROLLBACK")


def connect(url: str) -> Any:
    import psycopg

    # Server-side session default: every transaction is read-only unless a
    # statement explicitly asks otherwise (and this script never does).
    return psycopg.connect(
        url,
        autocommit=True,
        options="-c default_transaction_read_only=on -c statement_timeout=60000",
    )


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        print("DATABASE_URL must be set to a postgres:// or postgresql:// URL.", file=sys.stderr)
        return 1
    try:
        with connect(url) as conn:
            run(conn)
    except NotReadOnly as exc:
        print(f"Aborted: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # never print the URL; the type is enough to debug
        print(f"Diagnostic failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
