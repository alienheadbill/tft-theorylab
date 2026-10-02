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


#: Explains an intrinsic trait to a player (see `Roster.intrinsic_traits`).
INTRINSIC_TRAIT_REASON = (
    "Only {champion} has this trait, so it comes with picking {champion}: "
    "it describes the champion, not the units built around it."
)


@dataclass(frozen=True)
class Roster:
    set_number: int
    champions: dict[str, dict]
    traits: dict[str, str]
    #: Trait id -> items that can add it (emblems/trait items, from
    #: CommunityDragon `associatedTraits`; see `tftlab.cdragon.trait_item_ids`).
    #: None when the snapshot has no verified trait-item data.
    trait_items: dict[str, list[str]] | None = None
    #: Current-set emblems CommunityDragon lists that `trait_items` could not
    #: link to a trait (`tftlab.cdragon.unresolved_emblem_ids`). None when the
    #: snapshot did not check; anything but an empty list means the
    #: trait-item guard is incomplete.
    unresolved_emblems: list[str] | None = None

    @property
    def trait_item_guard_verified(self) -> bool:
        """True when the snapshot has trait-item data AND checked that every
        current-set emblem links to a trait."""
        return self.trait_items is not None and self.unresolved_emblems == []

    def champion_traits(self, character_id: str | None) -> tuple[str, ...]:
        """The champion's own traits (canonical trait ids), from static data."""
        return tuple((self.champions.get(character_id or "") or {}).get("traits") or ())

    def is_shop_champion(self, character_id: str | None) -> bool:
        """A player-selectable current-set shop champion: a roster entry with a
        shop cost of 1-5 and at least one set trait. Cost-1 jungle camps,
        training dummies and summons in the feed (`TFT_Krug`, `TFT_Voidspawn`,
        ...) carry no trait and are not; neither is any id outside the roster."""
        entry = self.champions.get(character_id or "") or {}
        return 1 <= int(entry.get("cost") or 0) <= 5 and bool(entry.get("traits"))

    def trait_champions(self, trait_id: str) -> tuple[str, ...]:
        """Shop champions (cost 1-5) whose own traits include `trait_id`, sorted."""
        return tuple(sorted(
            cid for cid, c in self.champions.items()
            if 1 <= int(c.get("cost") or 0) <= 5 and trait_id in (c.get("traits") or ())
        ))

    def singleton_provider_traits(self, character_id: str | None) -> tuple[str, ...]:
        """The champion's own traits that no other shop champion provides
        naturally (static trait membership only). Picking this champion
        guarantees each of them on the board at one unit, so that baseline
        says nothing about the units built around it (see
        `tftlab.analytics.traits`). Says nothing about whether an emblem or
        another mechanic can extend the trait: that is `intrinsic_traits`.
        Never a name list."""
        return tuple(t for t in self.champion_traits(character_id) if self.trait_champions(t) == (character_id,))

    def intrinsic_traits(self, character_id: str | None) -> tuple[str, ...]:
        """Traits that come with picking this champion and cannot be built
        any other way: its `singleton_provider_traits` that no item can add
        either -- the snapshot's verified `trait_items` lists no emblem or
        trait item for them. A stronger claim than "singleton provider": it
        needs complete item knowledge, so it fails closed: unless the guard is
        verified (`trait_item_guard_verified`: trait-item data present and no
        unresolved emblem) nothing is intrinsic. Classification for the API
        only; trait evidence uses the singleton-provider baseline rule
        instead, which does not depend on this."""
        if not self.trait_item_guard_verified:
            return ()
        return tuple(t for t in self.singleton_provider_traits(character_id) if not self.trait_items.get(t))

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
    return Roster(
        set_number=data["set_number"], champions=data["champions"], traits=data["traits"],
        trait_items=data.get("trait_items"), unresolved_emblems=data.get("unresolved_emblems"),
    )
