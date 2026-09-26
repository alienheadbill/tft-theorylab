"""Patch-wide 18.3 collection guardrails: expired keys, patch end, reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tftlab import cli
from tftlab.riot import RiotApiError
from tftlab.unreal_patch import NoCurrentTrustedWindow, current_trusted_window

from test_ingest_riot import _StubRiotClient
from _helpers import make_match, make_unit

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "live-ingest.yml"
END_18_3_MS = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp() * 1000)


class _Unauthorized:
    status_code = 401
    headers: dict = {}
    text = "Unauthorized"

    class request:
        url = "https://na1.api.riotgames.com/tft/league/v1/challenger"


def test_verify_riot_401_names_the_key_refresh_procedure_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, params=None: _Unauthorized())
    monkeypatch.setenv("RIOT_API_KEY", "RGAPI-super-secret-value")
    result = CliRunner().invoke(cli.app, ["verify-riot"])
    text = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "RIOT_API_KEY is invalid or expired" in text
    assert "Reset the development key in the Riot Developer Portal" in text
    assert "replace the GitHub Actions RIOT_API_KEY secret" in text
    assert "super-secret" not in result.output


def test_expired_key_mid_ingest_prints_the_same_guidance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _StubRiotClient(puuids=["p1"], match_ids_by_puuid={}, matches={})

    def unauthorized():
        raise RiotApiError("Riot API returned 401 for https://na1.api.riotgames.com/x: Unauthorized")

    client.challenger = unauthorized

    class _Ctx:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli, "RiotClient", _Ctx)
    monkeypatch.setattr(cli, "_resolve_cost_lookup", lambda **_: (None, False, 18))
    monkeypatch.setenv("RIOT_API_KEY", "RGAPI-super-secret-value")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "k.sqlite3"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["ingest-riot", "--challenger-seeds", "1"])
    text = " ".join(result.output.split())
    assert result.exit_code != 0
    assert "Reset the development key in the Riot Developer Portal" in text
    assert "super-secret" not in result.output


def test_workflow_verifies_the_key_before_ingesting_and_stays_window_bounded() -> None:
    text = WORKFLOW.read_text()
    assert text.index("tftlab verify-riot") < text.index("tftlab ingest-riot")
    ingest = next(line for line in text.splitlines() if "tftlab ingest-riot" in line)
    assert "--current-trusted-window" in ingest and "--start-time" not in ingest
    assert "schedule:" not in text  # still manual only


@pytest.mark.parametrize("now_ms", [END_18_3_MS, END_18_3_MS + 1, END_18_3_MS + 86_400_000])
def test_18_3_ends_strictly_at_2026_10_06_and_is_never_open_ended(now_ms: int) -> None:
    with pytest.raises(NoCurrentTrustedWindow):
        current_trusted_window(now_ms)


def test_last_millisecond_of_18_3_is_still_inside_the_window() -> None:
    window = current_trusted_window(END_18_3_MS - 1)
    assert window.client_patch == "18.3" and window.ends_at == END_18_3_MS
    assert window.ends_at // 1000 == 1_791_244_800 and window.starts_at // 1000 == 1_790_233_200


def test_ingest_report_shows_collection_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from test_db_concurrency import _cli

    match = make_match("X", units=[make_unit("TFT14_Foo", tier=2, items=[])])
    client = _StubRiotClient(puuids=["p0", "p1", "p2", "p3"], match_ids_by_puuid={"p0": ["X"]}, matches={"X": match})
    result = _cli(monkeypatch, tmp_path, client, ["--challenger-seeds", "4"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert (
        "Collection value (is another run worth it?): never-sampled seeds 100.0%, "
        "previously-sampled seeds 0.0%, zero-history seeds 75.0%"
    ) in text
