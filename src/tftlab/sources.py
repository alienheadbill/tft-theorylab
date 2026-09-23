"""Where scout evidence comes from, and the words we use about novelty.

Only OUR DATA (our own Riot match database) feeds Theory Lab statistics
such as the Opportunity Score. Every other source is recorded as separate
scout evidence in an experiment's field notes and is never blended into
our numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    role: str
    external: bool
    aliases: tuple[str, ...] = ()


SOURCES: tuple[Source, ...] = (
    Source("riot", "Our Riot data", "Statistical source of truth for Theory Lab metrics", False,
           ("riot", "our data", "our riot data", "theory lab")),
    Source("communitydragon", "CommunityDragon", "Game metadata (and, later, cached art)", False,
           ("cdragon", "community dragon")),
    Source("tft_academy", "TFT Academy", "Curated / established-comp scout signal", True, ("tftacademy",)),
    Source("metatft", "MetaTFT", "External comp, meta and tournament corroboration", True, ("meta tft",)),
    Source("tactics_tools", "tactics.tools", "External statistical relationship corroboration", True,
           ("tactics tools", "tacticstools")),
    Source("little_buddy_bot", "Little Buddy Bot", "Mechanics, odds, loot/shop/encounter/system notes", True,
           ("littlebuddybot", "lbb")),
    Source("community", "Community / Reddit", "Emerging player sightings", True, ("reddit", "community")),
    Source("tournament", "Tournament / high-Elo", "Competitive sightings", True,
           ("tournament", "high elo", "high-elo", "challenger")),
    Source("user", "My note", "Personal hypothesis or observation", False, ("me", "my note", "user", "mine")),
)
SOURCES_BY_KEY = {s.key: s for s in SOURCES}


def _k(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_ALIASES = {_k(alias): s for s in SOURCES for alias in (s.key, s.label, *s.aliases)}


def resolve_source(text: str | None) -> Source | None:
    """The known source a free-text name refers to ("TFT Academy",
    "tft academy", "tft_academy" all resolve), or None for anything else.
    Matching is by name only; URLs are never used to guess a source."""
    return _ALIASES.get(_k(text)) if text else None


# Novelty/research labels. Prepared vocabulary only: nothing assigns these
# automatically. A person records one on a field note after actually
# checking a source.
RESEARCH_LABELS: dict[str, tuple[str, str]] = {
    "KNOWN": ("Known", "A sufficiently similar established comp exists."),
    "VARIANT": ("Variant", "A recognizable established shell, but this version materially differs."),
    "EMERGING": ("Emerging", "Outside evidence exists, but it isn't an established listed comp."),
    "NO_PUBLIC_MATCH_FOUND": (
        "No public match found",
        "Nothing sufficiently similar was found in the sources actually checked (listed with the date). "
        "Never read as 'nobody has played this'.",
    ),
    "THEORYCRAFTED": ("Theorycrafted", "Primarily our own hypothesis at present."),
}

# The research bookkeeping shown on an experiment page. A line is ticked
# only when a field note actually came from that source (or is of that
# kind); an unticked line means "not recorded as checked", nothing more.
CHECKLIST: tuple[tuple[str, str, frozenset[str], frozenset[str]], ...] = (
    # (key, label, source keys that satisfy it, note kinds that satisfy it)
    ("riot", "Our Riot data", frozenset({"riot"}), frozenset({"riot_evidence"})),
    ("tft_academy", "TFT Academy", frozenset({"tft_academy"}), frozenset()),
    ("metatft", "MetaTFT", frozenset({"metatft"}), frozenset()),
    ("tactics_tools", "tactics.tools", frozenset({"tactics_tools"}), frozenset()),
    ("mechanics", "Mechanics check", frozenset({"little_buddy_bot"}), frozenset({"mechanic_note"})),
    ("community", "Community sightings", frozenset({"community"}), frozenset({"community_sighting"})),
    ("tournament", "High-Elo / tournament sightings", frozenset({"tournament"}), frozenset({"tournament_sighting"})),
)


def scout_checklist(notes: list[dict]) -> list[dict]:
    """Checklist state from an experiment's field notes (oldest first)."""
    items = []
    for key, label, source_keys, kinds in CHECKLIST:
        hits = [n for n in notes if n.get("source_key") in source_keys or n.get("kind") in kinds]
        items.append({
            "key": key,
            "label": label,
            "checked": bool(hits),
            "last_noted_at": hits[-1]["noted_at"] if hits else None,
        })
    return items
