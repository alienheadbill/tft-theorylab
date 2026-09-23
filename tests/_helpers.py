"""Shared helpers for building minimal, deterministic Match-V1-shaped payloads.

Used where tests need exact control over placement/tier/items instead of the
randomized `generate_demo_matches` fixtures.
"""

from __future__ import annotations

from typing import Any


def make_unit(
    character_id: str,
    *,
    name: str | None = None,
    rarity: int = 0,
    tier: int = 2,
    items: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "character_id": character_id,
        "name": name or character_id,
        "rarity": rarity,
        "tier": tier,
        "itemNames": items or [],
    }


def make_match(
    match_id: str,
    *,
    game_version: str = "Version 14.6.579.1234 (Sep 10 2024/13:00:00) [PUBLIC] <Releases/14.6>",
    game_datetime: int = 1_790_000_000_000,
    set_number: int = 14,
    units: list[dict[str, Any]] | None = None,
    placement: int = 4,
    level: int = 8,
    traits: list[dict[str, Any]] | None = None,
    queue_id: int = 1100,
) -> dict[str, Any]:
    """A single-participant match payload (participant_index 0 only).

    `queue_id` defaults to 1100 (`tftlab.riot.RANKED_TFT_QUEUE_ID`, standard
    Ranked TFT); pass e.g. 1090 (Normal) to build a non-target-queue fixture.
    """
    return {
        "metadata": {"match_id": match_id},
        "info": {
            "game_version": game_version,
            "tft_game_type": "standard",
            "queue_id": queue_id,
            "tft_set_number": set_number,
            "tft_set_core_name": f"TFTSet{set_number}",
            "game_datetime": game_datetime,
            "participants": [
                {
                    "placement": placement,
                    "level": level,
                    "augments": [],
                    "units": units or [],
                    "traits": traits or [],
                }
            ],
        },
    }
