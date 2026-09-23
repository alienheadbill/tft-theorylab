from __future__ import annotations

import random
from typing import Any


# Real current-set champions (ids, names and shop costs as CommunityDragon
# lists them), so demo screens never show a champion that isn't in the set.
# The match results generated from them are entirely synthetic.
# tests/test_demo_roster.py checks these against the committed roster.
CARRIES = [
    ("DA_18_Cassiopeia", "Cassiopeia", 3, 0.60),
    ("DA_18_KhaZix", "Kha'Zix", 3, 0.57),
    ("DA_18_Warwick", "Warwick", 2, 0.54),
    ("DA_18_Caitlyn", "Caitlyn", 2, 0.49),
]
FILLERS = [
    ("DA_Fiddlesticks18", "Fiddlesticks", 3),
    ("DA_18_Ivern", "Ivern", 5),
    ("DA_18_Sejuani", "Sejuani", 2),
    ("DA_18_Leona", "Leona", 1),
]


def _placement(rng: random.Random, top4_probability: float) -> int:
    if rng.random() < top4_probability:
        return rng.choice([1, 2, 3, 4])
    return rng.choice([5, 6, 7, 8])


def generate_demo_matches(count: int = 120, seed: int = 7) -> list[dict[str, Any]]:
    """Generate deterministic fake Match-V1-shaped payloads for pipeline tests."""
    rng = random.Random(seed)
    matches: list[dict[str, Any]] = []
    for m in range(count):
        participants = []
        for slot in range(8):
            carry_id, carry_name, cost, top4_prob = rng.choices(
                CARRIES, weights=[1, 2, 3, 8], k=1
            )[0]
            committed = rng.random() < 0.70
            hit = committed and rng.random() < (0.58 if cost <= 2 else 0.45)
            placement = _placement(rng, top4_prob if committed else 0.47)
            carry_items = (
                ["TFT_Item_BlueBuff", "TFT_Item_JeweledGauntlet", "TFT_Item_Deathcap"]
                if committed
                else ["TFT_Item_TearOfTheGoddess"]
            )
            units = [
                {
                    "character_id": carry_id,
                    "name": carry_name,
                    "rarity": cost - 1,
                    "tier": 3 if hit else 2,
                    "itemNames": carry_items,
                }
            ]
            for fid, fname, fcost in rng.sample(FILLERS, k=3):
                units.append(
                    {
                        "character_id": fid,
                        "name": fname,
                        "rarity": fcost - 1,
                        "tier": 2,
                        "itemNames": [],
                    }
                )
            participants.append(
                {
                    "placement": placement,
                    "level": rng.choice([6, 7, 8]),
                    "augments": ["TFT_Augment_Demo"],
                    "units": units,
                    "traits": [],
                }
            )
        matches.append(
            {
                "metadata": {"match_id": f"DEMO_{m:05d}"},
                "info": {
                    "game_datetime": 1_790_000_000_000 + m,
                    "game_version": "Version DEMO",
                    "tft_game_type": "standard",
                    "queue_id": 1100,
                    "tft_set_number": 18,
                    "tft_set_core_name": "TFTSet18",
                    "participants": participants,
                },
            }
        )
    return matches
