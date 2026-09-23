"""Personal theorycraft notebook entries ("experiments").

An experiment is a comp idea the owner wants to keep, e.g. "Kha'Zix + 6
Ravager, try rerolling him". It can start almost empty and fill in over time.

Storage layout (see `EXPERIMENT_TABLES_SQL`):

- `experiments`: one row per idea. Everything we filter or sort on is a
  real column (slug, carry, evidence status, lifecycle, timestamps).
- `experiment_tags`: normalized so tag filtering is a plain portable join on
  both SQLite and Postgres (no JSON operators).
- `comp_json`: the structured comp (units, traits, items, levels, notes) as
  one validated JSON document. It is always read and written whole and never
  queried field by field, so a JSON column keeps the schema stable while the
  comp format grows, and behaves identically on both backends.
- `experiment_field_notes`: the dated research log (Riot evidence snapshots,
  scout reports, mechanic notes, sightings, status changes). Written only
  through `add_field_note` (the owner's CLI); see `FIELD_NOTE_KINDS` and
  `tftlab.sources` for the vocabulary.

Evidence status and notebook lifecycle are separate on purpose: evidence
says what we can prove about the idea, lifecycle says what the owner is
doing with it.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .sources import RESEARCH_LABELS, SOURCES_BY_KEY, resolve_source

if TYPE_CHECKING:  # storage imports this module's DDL, so no runtime import back
    from .storage import Database

EVIDENCE_STATUSES = ("THEORYCRAFTED", "VARIANT", "OBSERVED")
DEFAULT_EVIDENCE_STATUS = "THEORYCRAFTED"
# OBSERVED means real statistical evidence is attached. Nothing can attach
# evidence yet, so it can't be set by hand.
MANUAL_EVIDENCE_STATUSES = ("THEORYCRAFTED", "VARIANT")

LIFECYCLES = ("idea", "testing", "watching", "archived")
DEFAULT_LIFECYCLE = "idea"

ORIGINS = ("manual", "demo")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

EXPERIMENT_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    carry_character_id TEXT,
    carry_name TEXT,
    evidence_status TEXT NOT NULL DEFAULT 'THEORYCRAFTED'
        CHECK (evidence_status IN ('THEORYCRAFTED', 'VARIANT', 'OBSERVED')),
    lifecycle TEXT NOT NULL DEFAULT 'idea'
        CHECK (lifecycle IN ('idea', 'testing', 'watching', 'archived')),
    summary TEXT,
    author_notes TEXT,
    comp_json TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'demo')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_tags (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (experiment_id, tag)
);

CREATE TABLE IF NOT EXISTS experiment_field_notes (
    note_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    noted_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    evidence_status TEXT,
    body TEXT,
    source_name TEXT,
    source_url TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    source_key TEXT,
    research_label TEXT
);
"""

# Columns added to experiment_field_notes after its first release (PR #12
# created the table without them). Applied by storage on connect.
FIELD_NOTE_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("source_key", "TEXT"),
    ("research_label", "TEXT"),
)

EXPERIMENT_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_experiments_carry ON experiments(carry_character_id);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(evidence_status);
CREATE INDEX IF NOT EXISTS idx_experiment_tags_tag ON experiment_tags(tag);
CREATE INDEX IF NOT EXISTS idx_field_notes_experiment ON experiment_field_notes(experiment_id, noted_at);
"""


class ExperimentError(ValueError):
    """Invalid experiment input. The message is meant for the CLI user."""


class ExperimentNotFound(LookupError):
    pass


# ---------------------------------------------------------------- comp spec

_SCALAR_TEXT_FIELDS = ("roll_timing", "positioning_notes", "augment_notes")
_LEVEL_FIELDS = ("target_level", "reroll_level")
_ITEM_LIST_FIELDS = ("carry_items", "tank_items")
_UNIT_LIST_FIELDS = ("core_units", "optional_units")
COMP_FIELDS = (
    *_UNIT_LIST_FIELDS,
    "target_traits",
    *_ITEM_LIST_FIELDS,
    "secondary_carry",
    *_LEVEL_FIELDS,
    *_SCALAR_TEXT_FIELDS,
)


def empty_comp() -> dict[str, Any]:
    return {
        "core_units": [],
        "optional_units": [],
        "target_traits": [],
        "carry_items": [],
        "tank_items": [],
        "secondary_carry": None,
        "target_level": None,
        "reroll_level": None,
        "roll_timing": None,
        "positioning_notes": None,
        "augment_notes": None,
    }


def _clean_text(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExperimentError(f"{where} must be text, got {type(value).__name__}")
    value = value.strip()
    return value or None


def _normalize_unit(raw: Any, where: str) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise ExperimentError(f"{where} must be a name or an object with a 'name'")
    unknown = set(raw) - {"name", "character_id", "star", "note"}
    if unknown:
        raise ExperimentError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
    name = _clean_text(raw.get("name"), f"{where}.name")
    if not name:
        raise ExperimentError(f"{where} needs a name")
    star = raw.get("star")
    if star is not None and (not isinstance(star, int) or isinstance(star, bool) or star not in (1, 2, 3, 4)):
        raise ExperimentError(f"{where}.star must be 1-4")
    return {
        "name": name,
        "character_id": _clean_text(raw.get("character_id"), f"{where}.character_id"),
        "star": star,
        "note": _clean_text(raw.get("note"), f"{where}.note"),
    }


_TRAIT_SHORTHAND_RE = re.compile(r"^\s*(\d{1,2})\s+(.+?)\s*$")


def _normalize_trait(raw: Any, where: str) -> dict[str, Any]:
    if isinstance(raw, str):
        # "6 Ravager" -> Ravager at 6; "Ravager" -> no breakpoint given.
        m = _TRAIT_SHORTHAND_RE.match(raw)
        raw = {"name": m.group(2), "breakpoint": int(m.group(1))} if m else {"name": raw}
    if not isinstance(raw, dict):
        raise ExperimentError(f"{where} must be text like '6 Ravager' or an object with a 'name'")
    unknown = set(raw) - {"name", "breakpoint", "note"}
    if unknown:
        raise ExperimentError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
    name = _clean_text(raw.get("name"), f"{where}.name")
    if not name:
        raise ExperimentError(f"{where} needs a name")
    bp = raw.get("breakpoint")
    if bp is not None and (not isinstance(bp, int) or isinstance(bp, bool) or not 1 <= bp <= 20):
        raise ExperimentError(f"{where}.breakpoint must be a whole number 1-20")
    return {"name": name, "breakpoint": bp, "note": _clean_text(raw.get("note"), f"{where}.note")}


def _normalize_items(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ExperimentError(f"{where} must be a list of item names")
    items = [_clean_text(item, f"{where}[{i}]") for i, item in enumerate(raw)]
    return [item for item in items if item]


def _normalize_level(raw: Any, where: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= 11:
        raise ExperimentError(f"{where} must be a level from 1 to 11")
    return raw


def normalize_comp(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a (possibly partial) comp spec into its canonical shape.

    Every key is optional and unknown keys are rejected, so a typo in a
    hand-written or LLM-generated JSON file fails loudly instead of being
    silently dropped.
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ExperimentError("comp must be an object")
    unknown = set(raw) - set(COMP_FIELDS)
    if unknown:
        raise ExperimentError(
            f"comp has unknown field(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(COMP_FIELDS)}"
        )
    comp = empty_comp()
    for key in _UNIT_LIST_FIELDS:
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise ExperimentError(f"comp.{key} must be a list")
        comp[key] = [_normalize_unit(u, f"comp.{key}[{i}]") for i, u in enumerate(value)]
    traits = raw.get("target_traits") or []
    if not isinstance(traits, list):
        raise ExperimentError("comp.target_traits must be a list")
    comp["target_traits"] = [_normalize_trait(t, f"comp.target_traits[{i}]") for i, t in enumerate(traits)]
    for key in _ITEM_LIST_FIELDS:
        comp[key] = _normalize_items(raw.get(key), f"comp.{key}")
    secondary = raw.get("secondary_carry")
    if secondary is not None:
        if isinstance(secondary, str):
            secondary = {"unit": secondary}
        if not isinstance(secondary, dict) or set(secondary) - {"unit", "items"}:
            raise ExperimentError("comp.secondary_carry must be an object with 'unit' and/or 'items'")
        unit = _clean_text(secondary.get("unit"), "comp.secondary_carry.unit")
        items = _normalize_items(secondary.get("items"), "comp.secondary_carry.items")
        comp["secondary_carry"] = {"unit": unit, "items": items} if (unit or items) else None
    for key in _LEVEL_FIELDS:
        comp[key] = _normalize_level(raw.get(key), f"comp.{key}")
    for key in _SCALAR_TEXT_FIELDS:
        comp[key] = _clean_text(raw.get(key), f"comp.{key}")
    return comp


def _normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ExperimentError("tags must be a list")
    tags = []
    for tag in raw:
        cleaned = _clean_text(tag, "tag")
        if cleaned:
            cleaned = cleaned.lower().lstrip("#")
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
    return tags


# ---------------------------------------------------------------- model


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    slug: str
    title: str
    carry_character_id: str | None
    carry_name: str | None
    evidence_status: str
    lifecycle: str
    summary: str | None
    author_notes: str | None
    comp: dict[str, Any]
    tags: list[str]
    origin: str
    created_at: str
    updated_at: str
    field_notes: list[dict[str, Any]] = field(default_factory=list)

    def to_input(self) -> dict[str, Any]:
        """The editable fields, in the exact shape `create_experiment` /
        `update_experiment` accept, so `experiment-show --json` output can be
        edited and fed straight back through `--from-json`."""
        return {
            "title": self.title,
            "slug": self.slug,
            "carry_character_id": self.carry_character_id,
            "carry_name": self.carry_name,
            "evidence_status": self.evidence_status,
            "lifecycle": self.lifecycle,
            "summary": self.summary,
            "author_notes": self.author_notes,
            "comp": self.comp,
            "tags": self.tags,
        }

    def to_api(self, *, include_field_notes: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.experiment_id,
            "slug": self.slug,
            "title": self.title,
            "carry": {"character_id": self.carry_character_id, "name": self.carry_name}
            if (self.carry_character_id or self.carry_name)
            else None,
            "evidence_status": self.evidence_status,
            "lifecycle": self.lifecycle,
            "summary": self.summary,
            "author_notes": self.author_notes,
            "comp": self.comp,
            "tags": self.tags,
            "is_example": self.origin == "demo",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_field_notes:
            body["field_notes"] = self.field_notes
        return body


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    ascii_text = ascii_text.replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:80].strip("-")


def _unique_slug(db: Database, base: str) -> str:
    slug, n = base, 2
    while db.query_one("SELECT 1 FROM experiments WHERE slug = ?", (slug,)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def _check_choice(value: str, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise ExperimentError(f"{what} must be one of: {', '.join(allowed)} (got {value!r})")
    return value


def _check_manual_evidence(value: str) -> str:
    value = value.upper()
    if value == "OBSERVED":
        raise ExperimentError(
            "OBSERVED is reserved for ideas with real statistical evidence attached, which isn't "
            "supported yet. Use THEORYCRAFTED (the default) or VARIANT."
        )
    return _check_choice(value, MANUAL_EVIDENCE_STATUSES, "evidence status")


_ENTRY_FIELDS = {
    "title", "slug", "carry_character_id", "carry_name", "evidence_status",
    "lifecycle", "summary", "author_notes", "comp", "tags",
}


def _check_entry_keys(data: dict[str, Any]) -> None:
    unknown = set(data) - _ENTRY_FIELDS
    if unknown:
        raise ExperimentError(
            f"unknown field(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(_ENTRY_FIELDS))}"
        )


def _row_to_experiment(db: Database, row: tuple[Any, ...], *, with_field_notes: bool = False) -> Experiment:
    (experiment_id, slug, title, carry_id, carry_name, evidence, lifecycle, summary,
     notes, comp_json, origin, created_at, updated_at) = row
    tags = [r[0] for r in db.query_all(
        "SELECT tag FROM experiment_tags WHERE experiment_id = ? ORDER BY tag", (experiment_id,)
    )]
    field_notes = list_field_notes(db, experiment_id) if with_field_notes else []
    return Experiment(
        experiment_id=experiment_id, slug=slug, title=title, carry_character_id=carry_id,
        carry_name=carry_name, evidence_status=evidence, lifecycle=lifecycle, summary=summary,
        author_notes=notes, comp={**empty_comp(), **json.loads(comp_json or "{}")}, tags=tags,
        origin=origin, created_at=created_at, updated_at=updated_at, field_notes=field_notes,
    )


_SELECT = """SELECT experiment_id, slug, title, carry_character_id, carry_name, evidence_status,
                    lifecycle, summary, author_notes, comp_json, origin, created_at, updated_at
             FROM experiments"""


def _replace_tags(db: Database, experiment_id: str, tags: list[str]) -> None:
    db.execute("DELETE FROM experiment_tags WHERE experiment_id = ?", (experiment_id,))
    for tag in tags:
        db.execute("INSERT INTO experiment_tags (experiment_id, tag) VALUES (?, ?)", (experiment_id, tag))


# ---------------------------------------------------------------- operations


def create_experiment(db: Database, data: dict[str, Any], *, origin: str = "manual") -> Experiment:
    """Create an entry. Only `title` is required; everything else can come later."""
    _check_entry_keys(data)
    title = _clean_text(data.get("title"), "title")
    if not title:
        raise ExperimentError("an experiment needs at least a title")
    _check_choice(origin, ORIGINS, "origin")

    evidence = _check_manual_evidence(data.get("evidence_status") or DEFAULT_EVIDENCE_STATUS)
    lifecycle = _check_choice((data.get("lifecycle") or DEFAULT_LIFECYCLE).lower(), LIFECYCLES, "lifecycle")
    comp = normalize_comp(data.get("comp"))
    tags = _normalize_tags(data.get("tags"))

    requested_slug = _clean_text(data.get("slug"), "slug")
    if requested_slug:
        if not _SLUG_RE.match(requested_slug):
            raise ExperimentError("slug may only use lowercase letters, digits and single hyphens")
        if db.query_one("SELECT 1 FROM experiments WHERE slug = ?", (requested_slug,)):
            raise ExperimentError(f"slug {requested_slug!r} is already taken")
        slug = requested_slug
    else:
        slug = _unique_slug(db, slugify(title) or "experiment")

    experiment_id = f"exp_{uuid.uuid4().hex[:12]}"
    now = _now()
    try:
        db.execute(
            """INSERT INTO experiments (
                   experiment_id, slug, title, carry_character_id, carry_name, evidence_status,
                   lifecycle, summary, author_notes, comp_json, origin, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                experiment_id, slug, title,
                _clean_text(data.get("carry_character_id"), "carry_character_id"),
                _clean_text(data.get("carry_name"), "carry_name"),
                evidence, lifecycle,
                _clean_text(data.get("summary"), "summary"),
                _clean_text(data.get("author_notes"), "author_notes"),
                json.dumps(comp, separators=(",", ":")), origin, now, now,
            ),
        )
        _replace_tags(db, experiment_id, tags)
    except Exception:
        db.conn.rollback()
        raise
    db.commit()
    return get_experiment(db, experiment_id)


def update_experiment(
    db: Database,
    key: str,
    changes: dict[str, Any],
    *,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
) -> Experiment:
    """Apply a partial update. Only keys present in `changes` are touched.

    `comp` is merged key by key: sending `{"comp": {"carry_items": [...]}}`
    replaces just the carry items and leaves the rest of the comp alone.
    `tags` replaces the tag list; `add_tags`/`remove_tags` edit it.
    """
    _check_entry_keys(changes)
    current = get_experiment(db, key)
    sets: dict[str, Any] = {}

    if "title" in changes:
        title = _clean_text(changes["title"], "title")
        if not title:
            raise ExperimentError("title can't be empty")
        sets["title"] = title
    if "slug" in changes:
        slug = _clean_text(changes["slug"], "slug")
        if not slug or not _SLUG_RE.match(slug):
            raise ExperimentError("slug may only use lowercase letters, digits and single hyphens")
        if slug != current.slug and db.query_one("SELECT 1 FROM experiments WHERE slug = ?", (slug,)):
            raise ExperimentError(f"slug {slug!r} is already taken")
        sets["slug"] = slug
    for text_key in ("carry_character_id", "carry_name", "summary", "author_notes"):
        if text_key in changes:
            sets[text_key] = _clean_text(changes[text_key], text_key)
    if "evidence_status" in changes:
        requested = (changes["evidence_status"] or DEFAULT_EVIDENCE_STATUS).upper()
        if requested != current.evidence_status:
            sets["evidence_status"] = _check_manual_evidence(requested)
    if "lifecycle" in changes:
        sets["lifecycle"] = _check_choice((changes["lifecycle"] or "").lower(), LIFECYCLES, "lifecycle")
    if "comp" in changes:
        patch = changes["comp"] or {}
        if not isinstance(patch, dict):
            raise ExperimentError("comp must be an object")
        normalize_comp(patch)  # validate the patch itself (catches unknown keys)
        merged = {**current.comp, **patch}
        sets["comp_json"] = json.dumps(normalize_comp(merged), separators=(",", ":"))

    tags = current.tags
    if "tags" in changes:
        tags = _normalize_tags(changes["tags"])
    if add_tags:
        tags = tags + [t for t in _normalize_tags(add_tags) if t not in tags]
    if remove_tags:
        drop = set(_normalize_tags(remove_tags))
        tags = [t for t in tags if t not in drop]
    tags_changed = tags != current.tags

    if not sets and not tags_changed:
        return current

    sets["updated_at"] = _now()
    assignments = ", ".join(f"{column} = ?" for column in sets)
    try:
        db.execute(
            f"UPDATE experiments SET {assignments} WHERE experiment_id = ?",
            (*sets.values(), current.experiment_id),
        )
        if tags_changed:
            _replace_tags(db, current.experiment_id, tags)
        if "evidence_status" in sets:
            # Every evidence-status transition is logged, so the research
            # log shows when (and from what) an idea's standing changed.
            _insert_field_note(
                db, current.experiment_id, kind="status_change",
                body=f"Evidence status {current.evidence_status} \u2192 {sets['evidence_status']}",
                evidence_status=sets["evidence_status"], noted_at=sets["updated_at"],
            )
    except Exception:
        db.conn.rollback()
        raise
    db.commit()
    return get_experiment(db, current.experiment_id)


def get_experiment(db: Database, key: str) -> Experiment:
    """Look up by slug or id."""
    row = db.query_one(f"{_SELECT} WHERE slug = ? OR experiment_id = ?", (key, key))
    if row is None:
        raise ExperimentNotFound(key)
    return _row_to_experiment(db, row, with_field_notes=True)


def list_experiments(
    db: Database,
    *,
    evidence_status: str | None = None,
    lifecycle: str | None = None,
    carry: str | None = None,
    tag: str | None = None,
) -> list[Experiment]:
    """Most recently updated first. `carry` matches the carry's id or name,
    case-insensitively."""
    clauses: list[str] = []
    params: list[Any] = []
    if evidence_status:
        clauses.append("evidence_status = ?")
        params.append(evidence_status.upper())
    if lifecycle:
        clauses.append("lifecycle = ?")
        params.append(lifecycle.lower())
    if carry:
        clauses.append("(LOWER(carry_character_id) = ? OR LOWER(carry_name) = ?)")
        params += [carry.lower(), carry.lower()]
    if tag:
        clauses.append("experiment_id IN (SELECT experiment_id FROM experiment_tags WHERE tag = ?)")
        params.append(tag.lower().lstrip("#"))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query_all(f"{_SELECT}{where} ORDER BY updated_at DESC, created_at DESC, slug", params)
    return [_row_to_experiment(db, row) for row in rows]


# ---------------------------------------------------------------- field notes

FIELD_NOTE_KINDS: dict[str, str] = {
    "riot_evidence": "Our data",
    "scout_report": "Scout report",
    "mechanic_note": "Mechanic note",
    "community_sighting": "Community sighting",
    "tournament_sighting": "Tournament sighting",
    "my_note": "My note",
    "status_change": "Status change",
}
# Labels a person may attach when recording what they found. NO_PUBLIC_MATCH_FOUND
# is a claim about specific sources, so it must name the source it came from.
# Kinds only Theory Lab writes, so nobody can hand-type "our data".
_SYSTEM_KINDS = {
    "riot_evidence": "run `tftlab experiment-scout <slug> --save`",
    "status_change": "logged automatically when evidence status changes",
}
_LABELLED_KINDS = {"scout_report", "community_sighting", "tournament_sighting", "my_note"}
_MAX_URL_LENGTH = 2000


def _precise_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_source_url(url: str | None) -> str | None:
    """http(s) URLs only, with a real host and no embedded credentials."""
    url = _clean_text(url, "url")
    if url is None:
        return None
    if len(url) > _MAX_URL_LENGTH or any(ch.isspace() for ch in url):
        raise ExperimentError("url must be a single http(s) link without spaces")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise ExperimentError(f"url must be an http(s) link to a real host (got {url!r})")
    if parts.username or parts.password:
        raise ExperimentError("url must not contain a username or password")
    return url


def _parse_noted_at(value: str | None) -> str:
    """ISO date or datetime -> canonical UTC timestamp. Defaults to now.

    A note records research that already happened, so it can't be dated
    later than the current UTC time. No clock-skew allowance: the date is
    checked against the clock of the same machine that parses it. A bare
    date means midnight UTC, so today's date is always accepted."""
    if value is None:
        return _now()
    text = _clean_text(value, "noted_at") or ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperimentError(f"noted_at must be a date like 2026-09-24 (got {value!r})") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed > datetime.now(timezone.utc):
        raise ExperimentError("noted_at can't be in the future (research that hasn't happened yet)")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_field_note(
    db: Database,
    experiment_id: str,
    *,
    kind: str,
    body: str,
    noted_at: str,
    source_key: str | None = None,
    source_name: str | None = None,
    source_url: str | None = None,
    evidence_status: str | None = None,
    research_label: str | None = None,
    data: dict[str, Any] | None = None,
) -> str:
    note_id = f"note_{uuid.uuid4().hex[:12]}"
    db.execute(
        """INSERT INTO experiment_field_notes (
               note_id, experiment_id, noted_at, kind, evidence_status, body, source_name,
               source_url, data_json, created_at, source_key, research_label
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            note_id, experiment_id, noted_at, kind, evidence_status, body, source_name, source_url,
            json.dumps(data or {}, separators=(",", ":"), sort_keys=True), _precise_now(),
            source_key, research_label,
        ),
    )
    return note_id


def add_field_note(
    db: Database,
    key: str,
    *,
    kind: str,
    body: str | None,
    source: str | None = None,
    source_url: str | None = None,
    evidence_status: str | None = None,
    research_label: str | None = None,
    noted_at: str | None = None,
    data: dict[str, Any] | None = None,
    system: bool = False,
) -> dict[str, Any]:
    """Append one dated entry to an experiment's research log.

    `source` may be a known source ("TFT Academy", "metatft", ...; see
    `tftlab.sources.SOURCES`) or any other name, which is kept as written.
    Adding a note never changes the experiment's evidence status.
    """
    experiment = get_experiment(db, key)
    kind = (kind or "").strip().lower()
    if kind not in FIELD_NOTE_KINDS:
        raise ExperimentError(f"kind must be one of: {', '.join(FIELD_NOTE_KINDS)} (got {kind!r})")
    if kind in _SYSTEM_KINDS and not system:
        raise ExperimentError(f"{kind} notes are written by Theory Lab itself ({_SYSTEM_KINDS[kind]})")
    body = _clean_text(body, "body")
    if not body:
        raise ExperimentError("a field note needs a body")

    source_name = _clean_text(source, "source")
    known = resolve_source(source_name)
    source_key = known.key if known else ("other" if source_name else None)
    if known:
        source_name = known.label
    if kind == "my_note" and source_key is None:
        source_key, source_name = "user", SOURCES_BY_KEY["user"].label
    url = validate_source_url(source_url)

    status = None
    if evidence_status:
        status = _check_manual_evidence(evidence_status)

    label = None
    if research_label:
        label = research_label.strip().upper().replace(" ", "_")
        if label not in RESEARCH_LABELS:
            raise ExperimentError(f"research label must be one of: {', '.join(RESEARCH_LABELS)}")
        if kind not in _LABELLED_KINDS:
            raise ExperimentError(f"a research label can't go on a {kind} note")
        if label == "NO_PUBLIC_MATCH_FOUND" and not (source_name or url):
            raise ExperimentError("NO_PUBLIC_MATCH_FOUND must name the source that was checked (--source/--url)")

    if data is not None:
        if not isinstance(data, dict):
            raise ExperimentError("data must be a JSON object")
        try:
            json.dumps(data)
        except (TypeError, ValueError) as exc:
            raise ExperimentError(f"data must be plain JSON ({exc})") from exc

    when = _parse_noted_at(noted_at)
    try:
        note_id = _insert_field_note(
            db, experiment.experiment_id, kind=kind, body=body, noted_at=when, source_key=source_key,
            source_name=source_name, source_url=url, evidence_status=status, research_label=label, data=data,
        )
        db.execute("UPDATE experiments SET updated_at = ? WHERE experiment_id = ?", (_now(), experiment.experiment_id))
    except Exception:
        db.conn.rollback()
        raise
    db.commit()
    return next(n for n in list_field_notes(db, experiment.experiment_id) if n["id"] == note_id)


def list_field_notes(db: Database, experiment_id: str) -> list[dict[str, Any]]:
    """An experiment's research log, oldest first (ties keep insertion order)."""
    rows = db.query_all(
        """SELECT note_id, noted_at, kind, evidence_status, body, source_key, source_name, source_url,
                  research_label, data_json
           FROM experiment_field_notes WHERE experiment_id = ?
           ORDER BY noted_at, created_at, note_id""",
        (experiment_id,),
    )
    return [
        {
            "id": r[0], "noted_at": r[1], "kind": r[2], "kind_label": FIELD_NOTE_KINDS.get(r[2], r[2]),
            "evidence_status": r[3], "body": r[4], "source_key": r[5], "source_name": r[6],
            "source_url": r[7], "research_label": r[8],
            "research_label_text": RESEARCH_LABELS[r[8]][0] if r[8] in RESEARCH_LABELS else None,
            "data": json.loads(r[9] or "{}"),
        }
        for r in rows
    ]


# ---------------------------------------------------------------- examples

#: Clearly-labeled example notebook entries for the local demo dataset only
#: (never seeded into a real database). Personal theorycraft, so no stats.
#: Champion and trait names must exist in the current set; see
#: tests/test_demo_roster.py.
DEMO_EXPERIMENTS: tuple[dict[str, Any], ...] = (
    {
        "title": "6 Ravager Kha'Zix",
        "carry_character_id": "DA_18_KhaZix",
        "carry_name": "Kha'Zix",
        "lifecycle": "idea",
        "summary": "Can Kha'Zix be the reroll carry inside 6 Ravager instead of the standard shell?",
        "author_notes": "Example entry. Needs testing: does he survive front-line splash once 3★?",
        "comp": {
            "core_units": [{"name": "Kha'Zix", "star": 3, "note": "the whole point"}],
            "target_traits": ["6 Ravager"],
            "carry_items": ["Infinity Edge", "Last Whisper"],
            "reroll_level": 7,
            "roll_timing": "slow roll at 7 above 50 gold",
        },
        "tags": ["reroll", "ravager", "example"],
    },
    {
        "title": "Cassiopeia / Fiddlesticks reroll",
        "carry_character_id": "DA_18_Cassiopeia",
        "carry_name": "Cassiopeia",
        "lifecycle": "testing",
        "summary": "Double reroll: Cassiopeia carries, Fiddlesticks holds the tank items.",
        "author_notes": "Example entry. Unsure whether both 3★ is realistic in one lobby.",
        "comp": {
            "core_units": [
                {"name": "Cassiopeia", "star": 3},
                {"name": "Fiddlesticks", "star": 3, "note": "secondary tank"},
            ],
            "optional_units": ["Leona"],
            "carry_items": ["Blue Buff", "Jeweled Gauntlet", "Deathcap"],
            "tank_items": ["Warmog's Armor", "Dragon's Claw"],
            "target_level": 8,
            "reroll_level": 7,
            "positioning_notes": "Cass corner, Fiddle in front of her.",
        },
        "tags": ["reroll", "double-reroll", "example"],
    },
    {
        "title": "Caitlyn reroll, odd shell",
        "carry_character_id": "DA_18_Caitlyn",
        "carry_name": "Caitlyn",
        "lifecycle": "watching",
        "summary": "Caitlyn as a 2-cost reroll without the usual frontline. Just a sketch so far.",
        "comp": {"core_units": ["Caitlyn"]},
        "tags": ["reroll", "example"],
    },
)


def seed_demo_experiments(db: Database) -> int:
    """Seed the example entries into an empty experiments table. Returns the
    number added (0 if any experiment already exists)."""
    if db.query_one("SELECT 1 FROM experiments LIMIT 1"):
        return 0
    for entry in DEMO_EXPERIMENTS:
        create_experiment(db, entry, origin="demo")
    return len(DEMO_EXPERIMENTS)
