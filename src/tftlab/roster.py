"""Current-set game metadata shipped with the package: champion and trait
names and their Riot/CommunityDragon ids (data/set_roster.json, refreshed
from CommunityDragon; see tests/test_cdragon_live.py).

Used to match what a person writes ("Kha'Zix", "Ravager") to the ids that
appear in Riot match data (DA_18_KhaZix, DA_18_Slayer). Offline and
read-only; nothing here makes a network call.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROSTER_PATH = Path(__file__).parent / "data" / "set_roster.json"

# Id prefixes/suffixes that carry no identity: set markers, item prefixes,
# and the AD/AP form suffixes some champions have.
_ID_NOISE = re.compile(r"^(?:TFT\d*_Item_|TFT\d*_|DA_\d*_?)|(?:_(?:AD|AP|Base))$|\d+$")


def name_key(text: str | None) -> str:
    """Case-, accent- and punctuation-insensitive key: "Kha'Zix" and
    "khazix" and "KhaZix" all become "khazix"."""
    if not text:
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def id_key(identifier: str | None) -> str:
    """Best-effort key for a raw id with no roster entry, e.g.
    "TFT_Item_InfinityEdge" -> "infinityedge", "DA_Fiddlesticks18" -> "fiddlesticks"."""
    if not identifier:
        return ""
    stripped = identifier
    for _ in range(3):
        stripped = _ID_NOISE.sub("", stripped)
    return name_key(stripped)


@dataclass(frozen=True)
class Roster:
    set_number: int
    champions: dict[str, dict]
    traits: dict[str, str]

    def champion_name(self, character_id: str | None) -> str | None:
        entry = self.champions.get(character_id or "")
        return entry["name"] if entry else None

    def champion_ids(self, name: str | None) -> list[str]:
        """Shop-champion ids whose name matches, e.g. "Lux" matches only the
        base Lux, while "Lux (Fae)" matches that form."""
        key = name_key(name)
        return sorted(
            cid for cid, c in self.champions.items() if 1 <= c["cost"] <= 5 and name_key(c["name"]) == key
        ) if key else []

    def champion_key(self, *, name: str | None = None, character_id: str | None = None) -> str:
        return name_key(name or self.champion_name(character_id)) or id_key(character_id)

    def trait_name(self, trait_id: str | None) -> str | None:
        return self.traits.get(trait_id or "")

    def trait_ids(self, name: str | None) -> list[str]:
        key = name_key(name)
        return sorted(tid for tid, t in self.traits.items() if name_key(t) == key) if key else []

    def trait_key(self, name_or_id: str | None) -> str:
        return name_key(self.trait_name(name_or_id) or name_or_id)


@lru_cache(maxsize=1)
def load_roster() -> Roster:
    data = json.loads(ROSTER_PATH.read_text())
    return Roster(set_number=data["set_number"], champions=data["champions"], traits=data["traits"])
