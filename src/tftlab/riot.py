from __future__ import annotations

import re
import time
from typing import Any

import httpx


class RiotApiError(RuntimeError):
    pass


#: Riot's `queue_id` for standard Ranked Teamfight Tactics, per Riot's public
#: queue metadata (https://static.developer.riotgames.com/docs/lol/queues.json,
#: unreachable from this sandbox's network egress -- this value is taken from
#: well-established, widely-documented TFT tooling convention instead, and
#: matches what this codebase's own test fixtures (`tests/_helpers.py`'s
#: `make_match`) have defaulted to since Milestone 1). Distinct from Normal
#: (unranked) TFT (1090), Hyper Roll (1130), and Double Up (1160): a
#: Challenger-ladder PUUID's recent match history can include any of these,
#: but this project's discovery dataset is scoped to standard ranked only --
#: see `ingest.ingest_ladder`. Verify against the real values seen in a live
#: ingest before trusting this beyond that scope.
RANKED_TFT_QUEUE_ID = 1100

#: TFT-LEAGUE-V1 `queue` query value for standard ranked (league endpoints
#: take the queue name, Match-V1 bodies carry the numeric id above).
RANKED_TFT_QUEUE = "RANKED_TFT"

#: Tiers served by `/tft/league/v1/entries/{tier}/{division}` (every tier
#: below Master; the apex tiers have their own league-list endpoints).
DIVISIONAL_TIERS: tuple[str, ...] = ("DIAMOND", "EMERALD", "PLATINUM", "GOLD", "SILVER", "BRONZE", "IRON")
LEAGUE_DIVISIONS: tuple[str, ...] = ("I", "II", "III", "IV")


_STATUS_RE = re.compile(r"returned (\d+)")


def classify_riot_error(message: str) -> str:
    """Categorize a `RiotApiError` message into a short, actionable label.

    Pulled out as a pure function (rather than inlined where errors are
    handled) so callers -- and tests -- can classify a Riot failure without
    needing a live API key or network access.
    """
    if "Repeated rate limiting" in message:
        return "rate_limited"
    if "Network error contacting Riot API" in message:
        return "network_error"
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
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                # httpx.RequestError's own message is just the underlying
                # network failure (DNS, timeout, connection refused, ...);
                # the API key is sent only via a header and never appears in
                # it or in `url`, so this is safe to surface as-is.
                raise RiotApiError(
                    f"Network error contacting Riot API ({type(exc).__name__}) for {url}"
                ) from exc
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

    def league_entries(
        self, tier: str, division: str, *, page: int = 1, queue: str = RANKED_TFT_QUEUE
    ) -> list[dict[str, Any]]:
        """One page of a divisional tier's league entries.

        TFT-LEAGUE-V1 `GET /tft/league/v1/entries/{tier}/{division}` on the
        platform route: `tier` is e.g. `DIAMOND` or `PLATINUM`, `division`
        is `I`-`IV`, and the optional query parameters are `queue` (default
        `RANKED_TFT`) and `page` (default 1, starting at 1). Returns a list
        of LeagueEntryDTO (`puuid`, `tier`, `rank` = division,
        `leaguePoints`, ...). Riot does not document the page size or a
        last-page marker, so callers stop on an empty page or their own
        page cap. The apex tiers are not served here -- they have their own
        league-list endpoints (`challenger()` etc.).
        """
        if tier not in DIVISIONAL_TIERS:
            raise ValueError(f"tier must be one of {', '.join(DIVISIONAL_TIERS)}")
        if division not in LEAGUE_DIVISIONS:
            raise ValueError(f"division must be one of {', '.join(LEAGUE_DIVISIONS)}")
        if page < 1:
            raise ValueError("page starts at 1")
        return self._get(
            f"https://{self.platform}.api.riotgames.com/tft/league/v1/entries/{tier}/{division}",
            params={"queue": queue, "page": page},
        )

    def match_ids(
        self,
        puuid: str,
        *,
        count: int = 20,
        start: int = 0,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[str]:
        """Recent match IDs for `puuid`, newest first.

        `start_time`/`end_time` optionally bound the history by match time.
        They are epoch **seconds**, as Riot documents for TFT-MATCH-V1
        `GET /tft/match/v1/matches/by-puuid/{puuid}/ids` (`startTime` /
        `endTime`: "Epoch timestamp in seconds"; `game_datetime` in match
        bodies is milliseconds, so callers convert). Omitted when `None`, so
        existing callers still get ordinary recent history.
        """
        params: dict[str, Any] = {"start": start, "count": count}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self._get(
            f"https://{self.region}.api.riotgames.com/tft/match/v1/matches/by-puuid/{puuid}/ids",
            params=params,
        )

    def match(self, match_id: str) -> dict[str, Any]:
        return self._get(
            f"https://{self.region}.api.riotgames.com/tft/match/v1/matches/{match_id}"
        )
