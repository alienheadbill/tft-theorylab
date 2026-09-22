from __future__ import annotations

import re
import time
from typing import Any, Iterable

import httpx


class RiotApiError(RuntimeError):
    pass


_STATUS_RE = re.compile(r"returned (\d+)")


def classify_riot_error(message: str) -> str:
    """Categorize a `RiotApiError` message into a short, actionable label.

    Pulled out as a pure function (rather than inlined where errors are
    handled) so callers -- and tests -- can classify a Riot failure without
    needing a live API key or network access.
    """
    if "Repeated rate limiting" in message:
        return "rate_limited"
    match = _STATUS_RE.search(message)
    if not match:
        return "unknown"
    status = int(match.group(1))
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    return f"http_{status}"


class RiotClient:
    """Small TFT API client with conservative 429 handling.

    Ladder endpoints use a platform route (e.g. na1), while Match-V1 uses a
    regional route (e.g. americas).
    """

    def __init__(
        self,
        api_key: str,
        *,
        platform: str = "na1",
        region: str = "americas",
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise ValueError("A Riot API key is required")
        self.api_key = api_key
        self.platform = platform
        self.region = region
        self._client = httpx.Client(
            timeout=timeout,
            headers={"X-Riot-Token": api_key, "User-Agent": "tft-theory-lab/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RiotClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(4):
            response = self._client.get(url, params=params)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", "1"))
                time.sleep(max(wait, 0.25))
                continue
            if 200 <= response.status_code < 300:
                return response.json()
            raise RiotApiError(
                f"Riot API returned {response.status_code} for {response.request.url}: "
                f"{response.text[:300]}"
            )
        raise RiotApiError(f"Repeated rate limiting for {url}")

    def challenger(self) -> dict[str, Any]:
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/challenger"
        )

    def grandmaster(self) -> dict[str, Any]:
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/grandmaster"
        )

    def master(self) -> dict[str, Any]:
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/master"
        )

    def ladder_puuids(self, leagues: Iterable[str] = ("challenger",)) -> list[str]:
        puuids: list[str] = []
        seen: set[str] = set()
        for league in leagues:
            payload = getattr(self, league)()
            for entry in payload.get("entries", []):
                puuid = entry.get("puuid")
                if puuid and puuid not in seen:
                    seen.add(puuid)
                    puuids.append(puuid)
        return puuids

    def match_ids(self, puuid: str, *, count: int = 20, start: int = 0) -> list[str]:
        return self._get(
            f"https://{self.region}.api.riotgames.com/tft/match/v1/matches/by-puuid/{puuid}/ids",
            params={"start": start, "count": count},
        )

    def match(self, match_id: str) -> dict[str, Any]:
        return self._get(
            f"https://{self.region}.api.riotgames.com/tft/match/v1/matches/{match_id}"
        )
