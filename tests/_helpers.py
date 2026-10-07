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


#: 18.3 trusted window start (2026-09-24T07:00:00Z), for realistic sources.
REAL_18_3_START_MS = 1_790_233_200_000


def live_ingest_like_payloads(count: int = 24, *, seed: int = 11) -> list[dict[str, Any]]:
    """Match-V1-shaped payloads as live ingestion stores them: regional Riot
    match ids, `data_version`, the participant PUUID list (78-char, made up
    here), ranked queue 1100, the masked Unreal `game_version` and
    timestamps inside the 18.3 trusted window. Built on the demo generator's
    boards, so the analytics have something to compute."""
    import copy
    import random
    import string

    from tftlab.demo import generate_demo_matches

    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits + "-_"
    out = []
    for i, match in enumerate(generate_demo_matches(count, seed=seed)):
        match = copy.deepcopy(match)
        puuids = ["".join(rng.choice(alphabet) for _ in range(78)) for _ in match["info"]["participants"]]
        match["metadata"] = {"data_version": "6", "match_id": f"NA1_{5_400_000_000 + i}", "participants": puuids}
        match["info"]["game_version"] = "TFT Unreal Version ?.?.?.?"
        match["info"]["game_datetime"] = REAL_18_3_START_MS + 600_000 * (i + 1)
        for participant, puuid in zip(match["info"]["participants"], puuids):
            participant["puuid"] = puuid
        out.append(match)
    return out


def build_live_ingest_like_source(path, count: int = 24, *, ledger: bool = True, seed: int = 11):
    """A SQLite store as live ingestion leaves it: the matches plus (with
    `ledger`) a completed ingest run, its seed ledger and discovery rows."""
    from tftlab.storage import Database

    payloads = live_ingest_like_payloads(count, seed=seed)
    with Database(path) as db:
        db.ingest_many(payloads)
        if ledger:
            db.start_ingest_run("run-test-1", 1)
            db.finalize_ingest_run(
                "run-test-1",
                [(p["metadata"]["participants"][0], "challenger") for p in payloads[:4]],
                [(p["metadata"]["match_id"], p["metadata"]["participants"][0], "challenger") for p in payloads],
                sampled_at=2, completed_at=3,
            )
    return payloads
