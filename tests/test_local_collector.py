"""The local collector (`tftlab local-*`, tftlab.local_collector): one SQLite
file on a personal computer, the existing ingest/validation/preparation/
export code, no cloud database, no live Riot access in tests."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import tftlab.ingest as ingest_module
import tftlab.local_collector as lc
from tftlab import cli
from tftlab.cdragon import SetMetadata
from tftlab.riot import RiotApiError
from tftlab.storage import Database
from tftlab.unreal_patch import UNREAL_PATCH_REGISTRY, UnrealPatchWindow

from _helpers import build_live_ingest_like_source, live_ingest_like_payloads

START_18_4_MS = 1_791_442_800_000  # 2026-10-08T07:00:00Z
END_18_4_MS = 1_792_454_400_000  # 2026-10-20T00:00:00Z
AT_MS = START_18_4_MS + 2 * 3600 * 1000  # 2026-10-08T09:00:00Z, inside 18.4
FAKE_KEY = "RGAPI-0123abcd-4567-89ef-0123-456789abcdef"
COHORT_SIZES = {"challenger": 30, "grandmaster": 30, "master": 40, "diamond": 50, "platinum": 50}


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _puuid(cohort: str, i: int) -> str:
    return f"PUUID-{cohort}-{i:04d}-".ljust(78, "x")


def _payloads_18_4(count: int = 40, *, seed: int = 21) -> list[dict]:
    payloads = live_ingest_like_payloads(count, seed=seed)
    for i, payload in enumerate(payloads):
        payload["info"]["game_datetime"] = START_18_4_MS + 60_000 * (i + 1)
        payload["metadata"]["match_id"] = f"NA1_{5_700_000_000 + i}"
    return payloads


class StubRiot:
    """Duck-types the RiotClient surface ingest_ladder and the preflight
    use: five cohorts' ladders, time-bounded histories, match bodies,
    telemetry, and the context manager. No network."""

    def __init__(self, payloads: list[dict], *, key_error: str | None = None, fail_match_after: int | None = None,
                 fail_status: int = 401, history_error: int | None = None) -> None:
        self.payloads = {p["metadata"]["match_id"]: p for p in payloads}
        self.ids = list(self.payloads)
        self.key_error = key_error
        self.fail_match_after = fail_match_after
        self.fail_status = fail_status
        self.history_error = history_error
        self.calls: list[tuple] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def _apex(self, cohort: str) -> dict:
        self.calls.append(("ladder", cohort))
        if cohort == "challenger" and self.key_error:
            raise RiotApiError(f"Riot API returned {self.key_error} for https://na1.api.riotgames.com/tft/league/v1/challenger")
        return {"entries": [{"puuid": _puuid(cohort, i), "leaguePoints": 2000 - i, "rank": "I"}
                            for i in range(COHORT_SIZES[cohort])]}

    def challenger(self):
        return self._apex("challenger")

    def grandmaster(self):
        return self._apex("grandmaster")

    def master(self):
        return self._apex("master")

    def league_entries(self, tier, division, *, page=1, queue="RANKED_TFT"):
        self.calls.append(("league", tier, division, page))
        cohort = tier.lower()
        if page > 1:
            return []
        per = COHORT_SIZES[cohort] // 4
        return [{"puuid": _puuid(cohort, i + 100 * "I II III IV".split().index(division)),
                 "leaguePoints": 99 - i, "rank": division, "tier": tier} for i in range(per)]

    def match_ids(self, puuid, *, count=20, start=0, start_time=None, end_time=None):
        self.calls.append(("history", count, start_time, end_time))
        if self.history_error:
            raise RiotApiError(f"Riot API returned {self.history_error} for https://americas.api.riotgames.com/"
                               f"tft/match/v1/matches/by-puuid/{puuid}/ids", status=self.history_error)
        offset = sum(map(ord, puuid)) % len(self.ids)
        return [self.ids[(offset + k) % len(self.ids)] for k in range(count)]

    def match(self, match_id):
        self.calls.append(("match", match_id))
        fetched = sum(1 for c in self.calls if c[0] == "match")
        if self.fail_match_after is not None and fetched > self.fail_match_after:
            raise RiotApiError(f"Riot API returned {self.fail_status} for https://americas.api.riotgames.com/tft/"
                               f"match/v1/matches/{match_id}", status=self.fail_status)
        return copy.deepcopy(self.payloads[match_id])

    def telemetry_snapshot(self):
        return {"requests": len(self.calls), "successes": len(self.calls), "rate_limited": 0, "elapsed_s": 1.0}


def _metadata() -> SetMetadata:
    return SetMetadata(patch="latest", set_number=18, champions={}, items={}, traits={})


class Harness:
    def __init__(self, tmp_path: Path, stub: StubRiot | None = None) -> None:
        self.db = tmp_path / "data" / "local" / "theorylabs.sqlite3"
        self.stub = stub or StubRiot(_payloads_18_4())
        self.factory_calls: list[tuple] = []
        self.metadata_calls = 0

    def factory(self, api_key, *, platform, region):
        self.factory_calls.append((platform, region))
        return self.stub

    def fetch_metadata(self):
        self.metadata_calls += 1
        return _metadata()

    def config(self, **overrides) -> lc.CollectConfig:
        return lc.CollectConfig(db_path=self.db, backup_dir=lc.local_dirs(self.db)["backups"], **overrides)

    def run(self, *, at_ms: int = AT_MS, api_key: str | None = FAKE_KEY, config=None, **kwargs) -> lc.CollectReport:
        return lc.run_local_collect(
            config or self.config(), api_key=api_key, platform="na1", region="americas", at_ms=at_ms,
            client_factory=self.factory, fetch_metadata=self.fetch_metadata, **kwargs,
        )


def _rows(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _bytes_under(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------- 1-2: local-init


def test_local_init_creates_the_folders_and_a_env_from_the_example(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("RIOT_API_KEY=RGAPI-your-key-here\nTFT_PLATFORM=na1\n")
    result = CliRunner().invoke(cli.app, ["local-init"])
    assert result.exit_code == 0, result.output
    for folder in ("data/local", "data/local/backups", "data/local/public", "data/local/reports"):
        assert (tmp_path / folder).is_dir()
    assert (tmp_path / ".env").read_text() == "RIOT_API_KEY=RGAPI-your-key-here\nTFT_PLATFORM=na1\n"
    if os.name == "posix":
        assert (tmp_path / ".env").stat().st_mode & 0o077 == 0  # owner-only
    assert not (tmp_path / "data/local/theorylabs.sqlite3").exists()  # the first collect creates it
    assert "will be created by the first" in result.output


def test_local_init_never_overwrites_env_or_the_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("RIOT_API_KEY=RGAPI-your-key-here\n")
    (tmp_path / ".env").write_text(f"RIOT_API_KEY={FAKE_KEY}\nCUSTOM=1\n")
    db = tmp_path / "data/local/theorylabs.sqlite3"
    db.parent.mkdir(parents=True)
    with Database(db):
        pass
    before = db.read_bytes()
    for _ in range(2):
        result = CliRunner().invoke(cli.app, ["local-init"])
        assert result.exit_code == 0, result.output
    assert (tmp_path / ".env").read_text() == f"RIOT_API_KEY={FAKE_KEY}\nCUSTOM=1\n"
    assert db.read_bytes() == before
    assert "left unchanged" in result.output and FAKE_KEY not in result.output


def test_local_set_key_saves_the_key_hidden_and_keeps_other_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("# comment\nRIOT_API_KEY=RGAPI-your-key-here\nTFT_PLATFORM=na1\n")
    result = CliRunner().invoke(cli.app, ["local-set-key"], input=f"{FAKE_KEY}\n")
    assert result.exit_code == 0, result.output
    assert FAKE_KEY not in result.output  # hidden prompt, never echoed
    assert (tmp_path / ".env").read_text() == f"# comment\nRIOT_API_KEY={FAKE_KEY}\nTFT_PLATFORM=na1\n"
    bad = CliRunner().invoke(cli.app, ["local-set-key"], input="not-a-key\n")
    assert bad.exit_code == 1 and (tmp_path / ".env").read_text().count(FAKE_KEY) == 1


# ---------------------------------------------------------------- 3-4: local SQLite only


@pytest.mark.parametrize("target", [
    "postgres://user:hunter2@db.example.com/theorylabs",
    "postgresql://user:hunter2@ep-cool.neon.tech/neondb?sslmode=require",
    "sqlite:///data/local/theorylabs.sqlite3",
    "postgresql:relative",
])
def test_the_local_collector_refuses_database_urls(target: str, tmp_path: Path) -> None:
    with pytest.raises(lc.LocalTargetError) as exc:
        lc.resolve_local_db(target)
    assert "hunter2" not in str(exc.value)
    with pytest.raises(lc.LocalTargetError):
        lc.resolve_local_db(None, env={"TFT_LOCAL_DB_PATH": target})


@pytest.mark.parametrize("command", ["local-collect", "local-status", "local-snapshot"])
def test_local_commands_refuse_a_postgres_target_before_doing_anything(command: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: pytest.fail("Riot contacted"))
    monkeypatch.setattr(Database, "_connect_postgres", staticmethod(lambda *a, **k: pytest.fail("Postgres opened")))
    result = CliRunner().invoke(cli.app, [command, "--db", "postgresql://u:hunter2@ep-x.neon.tech/db"])
    assert result.exit_code == 2
    assert "Refused" in result.output and "hunter2" not in result.output
    assert not (tmp_path / "data").exists()


def test_a_non_sqlite_file_or_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(lc.LocalTargetError):
        lc.resolve_local_db(tmp_path / "notes.txt")
    with pytest.raises(lc.LocalTargetError):
        lc.resolve_local_db(tmp_path)
    assert lc.resolve_local_db(None, env={}) == (Path.cwd() / "data/local/theorylabs.sqlite3").resolve()


def test_database_url_never_redirects_local_collection(tmp_path: Path, monkeypatch) -> None:
    """DATABASE_URL (even a Neon-looking one, set in .env) is ignored: the
    collection lands in the local SQLite file and no Postgres connection is
    ever attempted."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"RIOT_API_KEY={FAKE_KEY}\nDATABASE_URL=postgresql://u:hunter2@ep-x.neon.tech/db\n")
    monkeypatch.delenv("RIOT_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TFT_LOCAL_DB_PATH", raising=False)
    monkeypatch.setattr(Database, "_connect_postgres", staticmethod(lambda *a, **k: pytest.fail("Postgres opened")))
    stub = StubRiot(_payloads_18_4(12))
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: stub)
    monkeypatch.setattr(cli, "_fetch_current_metadata", _metadata)
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS)
    result = CliRunner().invoke(cli.app, ["local-collect"])
    assert result.exit_code == 0, result.output
    assert "DATABASE_URL: set in the environment but ignored" in result.output
    assert "hunter2" not in result.output and "neon" not in result.output.lower()
    db = tmp_path / "data/local/theorylabs.sqlite3"
    assert _rows(db, "SELECT COUNT(*) FROM matches")[0][0] == 12
    source = Path(lc.__file__).read_text()
    assert "NEON" not in source and "DATABASE_URL\")" not in source and "database_url" not in source.replace(
        "database_url_set", "")


# ---------------------------------------------------------------- 5-6, 19: fail before ingest


def test_an_expired_key_stops_before_the_database_is_touched(tmp_path: Path) -> None:
    h = Harness(tmp_path, StubRiot(_payloads_18_4(), key_error="401"))
    ingested = []
    report = h.run(ingest=lambda *a, **k: ingested.append(1))
    assert report.outcome == lc.PREFLIGHT_FAILED and report.exit_code == 1
    assert report.message == lc.EXPIRED_KEY_MESSAGE
    assert "expired" in report.message and "Developer Portal" in report.message
    assert "No collection was performed" in report.message
    assert not ingested and not h.db.exists() and h.metadata_calls == 0
    assert not lc.local_dirs(h.db)["backups"].exists()

    # An existing database is not modified (or backed up) either.
    build_live_ingest_like_source(h.db, 6)
    before = h.db.read_bytes()
    report = h.run(ingest=lambda *a, **k: ingested.append(1))
    assert report.message == lc.EXPIRED_KEY_MESSAGE and not ingested
    assert h.db.read_bytes() == before and not lc.local_dirs(h.db)["backups"].exists()


@pytest.mark.parametrize("key", [None, "", "RGAPI-your-key-here"])
def test_a_missing_key_stops_before_riot_or_the_database(key, tmp_path: Path) -> None:
    h = Harness(tmp_path)
    report = h.run(api_key=key)
    assert report.outcome == lc.PREFLIGHT_FAILED and report.message == lc.KEY_NOT_SET_MESSAGE
    assert not h.factory_calls and not h.db.exists()


@pytest.mark.parametrize("when", [
    "2026-10-07T12:00:00+00:00",  # the 18.3 -> 18.4 gap: neither patch
    "2026-10-08T06:59:59+00:00",  # one second before 18.4 starts
    "2026-10-20T00:00:00+00:00",  # 18.4's exclusive end
    "2026-11-15T12:00:00+00:00",  # later: a new window must be registered first
    "2031-01-01T00:00:00+00:00",
])
def test_outside_a_trusted_window_nothing_is_collected(when: str, tmp_path: Path) -> None:
    h = Harness(tmp_path)
    report = h.run(at_ms=_ms(when))
    assert report.outcome == lc.PREFLIGHT_FAILED and report.message == lc.NO_WINDOW_MESSAGE
    assert "does not currently have a verified collection window" in report.message
    assert "No Riot data was collected" in report.message
    assert not h.factory_calls and not h.db.exists()


def test_the_window_comes_from_the_registry_never_a_hardcoded_patch(tmp_path: Path, monkeypatch) -> None:
    import tftlab.unreal_patch as up

    future = UnrealPatchWindow(client_patch="19.1", starts_at=_ms("2027-01-06T08:00:00+00:00"),
                               ends_at=_ms("2027-01-20T00:00:00+00:00"), verified=True, source="fixture")
    monkeypatch.setattr(up, "UNREAL_PATCH_REGISTRY", (*UNREAL_PATCH_REGISTRY, future))
    payloads = _payloads_18_4(8)
    for i, p in enumerate(payloads):
        p["info"]["game_datetime"] = future.starts_at + 60_000 * (i + 1)
    h = Harness(tmp_path, StubRiot(payloads))
    seen = []

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return ingest_module.ingest_ladder(*args, **kwargs)

    report = h.run(at_ms=future.starts_at + 3_600_000, ingest=spy)
    assert report.patch == "19.1"
    assert seen[0]["history_start_time"] == future.starts_at // 1000
    assert seen[0]["history_end_time"] == future.ends_at // 1000
    source = Path(lc.__file__).read_text()
    assert "18.4" not in source and "1_791_442_800_000" not in source


# ---------------------------------------------------------------- 7-9: the existing ingest, bounded


def test_defaults_are_the_bounded_production_settings() -> None:
    assert dict(lc.DEFAULT_SEED_ALLOCATION) == {"challenger": 15, "grandmaster": 15, "master": 20,
                                                "diamond": 25, "platinum": 25}
    config = lc.CollectConfig(db_path=Path("x.sqlite3"), backup_dir=Path("b"))
    assert config.seed_allocation == dict(lc.DEFAULT_SEED_ALLOCATION)
    assert config.matches_per_seed == 10 and lc.DEFAULT_RATE_CEILING == "10:10"
    assert config.keep_backups == 7


def test_local_collect_reuses_ingest_ladder_with_the_bounded_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    stub = StubRiot(_payloads_18_4(30))
    calls = []
    real = ingest_module.ingest_ladder

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    assert lc.ingest_ladder is real  # the one ingestion implementation
    monkeypatch.setattr(lc, "ingest_ladder", spy)
    monkeypatch.setattr(lc.run_local_collect, "__kwdefaults__", {**lc.run_local_collect.__kwdefaults__, "ingest": spy})
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: stub)
    monkeypatch.setattr(cli, "_fetch_current_metadata", _metadata)
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS)
    result = CliRunner().invoke(cli.app, ["local-collect"])
    assert result.exit_code == 0, result.output
    (kwargs,) = calls
    assert kwargs["seed_allocation"] == {"challenger": 15, "grandmaster": 15, "master": 20, "diamond": 25, "platinum": 25}
    assert kwargs["matches_per_player"] == 10
    assert kwargs["history_start_time"] == START_18_4_MS // 1000 and kwargs["history_end_time"] == END_18_4_MS // 1000
    assert kwargs["max_ladder_pages"] == ingest_module.DEFAULT_MAX_LADDER_PAGES
    histories = [c for c in stub.calls if c[0] == "history"]
    assert len(histories) == 100 and {c[1:] for c in histories} == {(10, START_18_4_MS // 1000, END_18_4_MS // 1000)}
    # Orchestration only: the collector never calls Riot's match endpoints itself.
    source = Path(lc.__file__).read_text()
    for own_ingest in ("client.match_ids(", "client.match(", "ingest_match", "league_entries(", "finalize_ingest_run"):
        assert own_ingest not in source, own_ingest


def test_the_production_rate_ceiling_is_applied_to_the_real_client() -> None:
    client = cli._local_riot_client(FAKE_KEY, platform="na1", region="americas")
    try:
        assert [str(c) for c in client.limiter.ceilings] == [str(c) for c in cli.parse_rate_limits("10:10", "x")]
    finally:
        client.close()


def test_seed_rotation_and_provenance_finalize_normally(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.run()
    assert first.outcome == lc.SUCCESS, first.message
    runs = _rows(h.db, "SELECT run_id, status FROM ingest_runs")
    assert [r[1] for r in runs] == ["completed"]
    assert _rows(h.db, "SELECT COUNT(*) FROM seed_samples")[0][0] == 100
    assert _rows(h.db, "SELECT COUNT(DISTINCT match_id) FROM match_discoveries")[0][0] == \
        _rows(h.db, "SELECT COUNT(*) FROM matches")[0][0]
    second = h.run(at_ms=AT_MS + 3_600_000)
    assert second.outcome == lc.SUCCESS, second.message
    by_run = {}
    for run_id, puuid, cohort in _rows(h.db, "SELECT run_id, puuid, cohort FROM seed_samples WHERE cohort = 'challenger'"):
        by_run.setdefault(run_id, set()).add(puuid)
    a, b = by_run.values()
    assert len(a) == len(b) == 15 and not a & b  # never-sampled players first: the rotation advanced
    assert second.ingest["inserted"] == 0 and second.ingest["duplicates_skipped"] > 0  # deduplicated


# ---------------------------------------------------------------- 10-11: backups


def test_first_run_needs_no_backup_and_later_runs_back_up_consistently(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.run()
    assert first.backup["path"] is None and "no pre-run backup was needed" in first.backup["note"]
    matches_before = _rows(h.db, "SELECT COUNT(*) FROM matches")[0][0]
    h.stub = StubRiot(_payloads_18_4(50, seed=33))
    second = h.run(at_ms=AT_MS + 60_000)
    backup = Path(second.backup["path"])
    assert backup.name == "theorylabs-20261008T090100Z.sqlite3" and backup.parent == tmp_path / "data/local/backups"
    assert _rows(backup, "SELECT COUNT(*) FROM matches")[0][0] == matches_before  # the pre-run state
    assert _rows(backup, "PRAGMA quick_check")[0][0] == "ok"
    assert _rows(backup, "PRAGMA journal_mode")[0][0] == "delete"  # one self-contained file
    assert not list(backup.parent.glob("*.partial"))
    assert _rows(h.db, "SELECT COUNT(*) FROM matches")[0][0] > matches_before


def test_backup_retention_keeps_the_newest_and_touches_nothing_else(tmp_path: Path) -> None:
    db = tmp_path / "local" / "theorylabs.sqlite3"
    build_live_ingest_like_source(db, 4)
    backups = tmp_path / "local" / "backups"
    backups.mkdir()
    for day in range(1, 10):
        (backups / f"theorylabs-202609{day:02d}T120000Z.sqlite3").write_bytes(b"old")
    (backups / "my-notes.txt").write_text("keep me")
    (backups / "theorylabs.sqlite3").write_text("not a collector backup name")
    result = lc.backup_local_db(db, backups, at_ms=AT_MS, keep=7)
    names = sorted(p.name for p in backups.iterdir())
    kept = [n for n in names if lc.BACKUP_NAME_RE.match(n)]
    assert len(kept) == 7 and result.path.name in kept
    assert kept[0] == "theorylabs-20260904T120000Z.sqlite3"  # the 6 newest old ones + the new one
    assert len(result.removed) == 3 and "theorylabs-20260901T120000Z.sqlite3" in result.removed
    assert "my-notes.txt" in names and "theorylabs.sqlite3" in names and db.exists()
    again = lc.backup_local_db(db, backups, at_ms=AT_MS, keep=7)  # same second: a distinct name
    assert again.path.name == "theorylabs-20261008T090000Z-1.sqlite3"


def test_a_failed_backup_stops_collection_and_deletes_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    build_live_ingest_like_source(h.db, 4)
    blocker = lc.local_dirs(h.db)["backups"]
    blocker.write_text("a file where the backup folder should be")
    ingested = []
    report = h.run(ingest=lambda *a, **k: ingested.append(1))
    assert report.outcome == lc.PREFLIGHT_FAILED and "backup failed" in report.message
    assert not ingested and blocker.read_text().startswith("a file")


# ---------------------------------------------------------------- 12-14: validate, then prepare the window


def test_validation_runs_after_ingestion_for_the_current_window(tmp_path: Path) -> None:
    from tftlab.validate import validate_live_data

    h = Harness(tmp_path)
    order = []

    def ingest(*a, **k):
        order.append("ingest")
        return ingest_module.ingest_ladder(*a, **k)

    def validate(db, *, balance_window, metadata):
        order.append(("validate", balance_window))
        assert metadata is not None
        return validate_live_data(db, balance_window=balance_window, metadata=metadata)

    report = h.run(ingest=ingest, validate=validate)
    assert order == ["ingest", ("validate", "18.4")]
    assert report.validation["18.4"]["is_severe"] is False and report.outcome == lc.SUCCESS


def test_severe_validation_blocks_success_and_keeps_the_data(tmp_path: Path) -> None:
    from tftlab.validate import validate_live_data

    h = Harness(tmp_path)
    prepared = []

    def broken(db, *, balance_window, metadata):
        return dataclasses.replace(validate_live_data(db, balance_window=balance_window, metadata=metadata),
                                   malformed_placements=3)

    report = h.run(validate=broken, prepare=lambda *a, **k: prepared.append(a))
    assert report.outcome == lc.VALIDATION_FAILED and report.exit_code == 1
    assert "validation found severe integrity problems" in report.message
    assert "Do NOT create a public snapshot" in report.message and not report.ready_for_snapshot
    assert report.validation["18.4"]["severe"] == ["malformed placements"]
    assert not prepared  # nothing prepared from data that failed validation
    assert _rows(h.db, "SELECT COUNT(*) FROM matches")[0][0] > 0  # collected matches are not deleted


def test_source_empty_boards_stay_a_warning(tmp_path: Path) -> None:
    payloads = _payloads_18_4(12)
    payloads[0]["info"]["participants"][1]["units"] = []
    h = Harness(tmp_path, StubRiot(payloads))
    report = h.run()
    assert report.outcome == lc.SUCCESS, report.message
    assert any("without units" in w for w in report.validation["18.4"]["warnings"])


def test_discovery_preparation_targets_only_the_current_window(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    build_live_ingest_like_source(h.db, 20)  # an older 18.3 history in the same database
    calls = []
    from tftlab.prepared_discovery import prepare_window

    def spy(db, window, **kwargs):
        calls.append(window)
        return prepare_window(db, window, **kwargs)

    report = h.run(prepare=spy)
    assert report.outcome == lc.SUCCESS, report.message
    assert calls == ["18.4"] and report.prepared["18.4"]["status"] == "published"
    windows = {r[0] for r in _rows(h.db, "SELECT DISTINCT balance_window FROM discovery_prepared_runs")}
    assert windows == {"18.4"}


def test_a_preparation_failure_is_reported_without_losing_data(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def boom(db, window, **kwargs):
        raise RuntimeError("prepare broke")

    report = h.run(prepare=boom)
    assert report.outcome == lc.PREPARE_FAILED and report.prepared["18.4"]["status"] == "failed"
    assert "data is safe" in report.message and _rows(h.db, "SELECT COUNT(*) FROM matches")[0][0] > 0


# ---------------------------------------------------------------- collection failures


def test_a_key_expiring_mid_run_leaves_a_valid_database_and_no_rotation(tmp_path: Path) -> None:
    h = Harness(tmp_path, StubRiot(_payloads_18_4(30), fail_match_after=5, fail_status=401))
    report = h.run()
    assert report.outcome == lc.COLLECTION_FAILED and report.exit_code == 1
    assert report.database_ok is True
    assert "expired during the run" in report.message and "still valid" in report.message
    assert "run is recorded as incomplete" in report.message and "seed rotation did not advance" in report.message
    assert _rows(h.db, "SELECT status FROM ingest_runs") == [("failed",)]
    assert _rows(h.db, "SELECT COUNT(*) FROM seed_samples")[0][0] == 0
    assert _rows(h.db, "SELECT COUNT(*) FROM matches")[0][0] == 5  # stored before the failure, kept
    assert "PUUID-" not in json.dumps(report.as_dict()) and "NA1_" not in json.dumps(report.as_dict())


def test_riot_returning_no_history_at_all_is_not_success(tmp_path: Path) -> None:
    h = Harness(tmp_path, StubRiot(_payloads_18_4(10), history_error=503))
    report = h.run()
    assert report.outcome == lc.COLLECTION_FAILED and "did not return any match history" in report.message
    assert report.database_ok is True


def test_a_second_concurrent_collection_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "local" / "theorylabs.sqlite3"
    with lc.collector_lock(db):
        with pytest.raises(lc.CollectorBusy):
            with lc.collector_lock(db):
                pass
    with lc.collector_lock(db):  # released afterwards
        pass
    lock = db.parent / f".{db.name}.collect.lock"
    lock.write_text("{}")
    os.utime(lock, (1, 1))  # a stale lock from a run that died long ago
    with lc.collector_lock(db):
        pass
    assert not lock.exists()


# ---------------------------------------------------------------- the CLI report


def test_local_collect_cli_prints_a_plain_report_without_identifiers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    stub = StubRiot(_payloads_18_4(30))
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: stub)
    monkeypatch.setattr(cli, "_fetch_current_metadata", _metadata)
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS)
    result = CliRunner().invoke(cli.app, ["local-collect"])
    assert result.exit_code == 0, result.output
    out = re.sub(r"\s+", " ", result.output)
    for expected in ("OK Riot key valid", "OK Trusted patch window: patch 18.4", "first collection",
                     "no pre-run backup was needed", "SUCCESS", "Balance window: patch 18.4",
                     "New matches inserted: 30", "Already stored (duplicates skipped):", "Patch matches now: 30",
                     "Boards (players' final boards) in the patch:", "challenger 15/15", "platinum 25/25",
                     "Riot requests:", "Rate-limit events (429): 0", "Validation: 18.4: passed",
                     "Discovery preparation: 18.4: prepared (", "Latest game: 2026-10-08",
                     "Ready to create a public snapshot: yes", "Report saved:"):
        assert expected in out, expected
    _assert_no_identifiers(result.output, stub)
    (report_file,) = (tmp_path / "data/local/reports").glob("local-collect-*.json")
    _assert_no_identifiers(report_file.read_text(), stub)


def _assert_no_identifiers(text: str, stub: StubRiot) -> None:
    assert FAKE_KEY not in text
    assert "PUUID-" not in text and not re.search(r"[A-Za-z0-9_-]{78}", text)
    assert not any(match_id in text for match_id in stub.ids) and "NA1_" not in text


def test_local_collect_cli_expired_key_message_and_exit_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: StubRiot([], key_error="401"))
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS)
    result = CliRunner().invoke(cli.app, ["local-collect"])
    assert result.exit_code == 1
    out = re.sub(r"\s+", " ", result.output)
    assert "Riot development key is expired" in out and "No collection was performed." in out
    assert FAKE_KEY not in result.output and not (tmp_path / "data/local/theorylabs.sqlite3").exists()


def test_local_collect_cli_outside_a_window(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: pytest.fail("Riot contacted"))
    monkeypatch.setattr(lc, "now_ms", lambda: _ms("2026-10-21T00:00:00+00:00"))
    result = CliRunner().invoke(cli.app, ["local-collect"])
    assert result.exit_code == 1
    assert "does not currently have a verified collection window" in re.sub(r"\s+", " ", result.output)
    assert not (tmp_path / "data/local/theorylabs.sqlite3").exists()


# ---------------------------------------------------------------- 15-16: local-status


def test_local_status_is_network_free_and_has_no_identifiers(tmp_path: Path, monkeypatch) -> None:
    import httpx

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    h = Harness(tmp_path)
    assert h.run().outcome == lc.SUCCESS
    h.run(at_ms=AT_MS + 60_000)  # a second run, so there is a backup
    build_live_ingest_like_source(tmp_path / "other.sqlite3", 2)  # (unrelated; status must not read it)

    def no_network(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(cli, "_local_riot_client", no_network)
    monkeypatch.setattr(cli, "_fetch_current_metadata", no_network)
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS + 120_000)
    result = CliRunner().invoke(cli.app, ["local-status"])
    assert result.exit_code == 0, result.output
    out = re.sub(r"\s+", " ", result.output)
    for expected in ("Local database:", "File size:", "Total matches: 40", "Total boards: 320", "Latest game:",
                     "Collection runs: 2 completed, 0 incomplete", "Sampling ledger size (seed samples): 200",
                     "18.4: 40 matches", "Prepared Discovery: 18.4: current", "Latest local backup: theorylabs-2026",
                     "Current trusted patch: 18.4", "now is inside it", "Riot key: configured (not checked"):
        assert expected in out, expected
    _assert_no_identifiers(result.output, h.stub)


def test_local_status_before_any_collection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RIOT_API_KEY", raising=False)
    monkeypatch.setattr(lc, "now_ms", lambda: _ms("2026-10-21T00:00:00+00:00"))
    result = CliRunner().invoke(cli.app, ["local-status"])
    assert result.exit_code == 0, result.output
    out = re.sub(r"\s+", " ", result.output)
    assert "not created yet" in out and "Current trusted patch: none" in out and "Riot key: NOT SET" in out
    assert not (tmp_path / "data").exists()  # read-only: creates nothing


# ---------------------------------------------------------------- 17-18: public snapshot, the key never leaks


def test_local_snapshot_uses_the_verified_exporter(tmp_path: Path, monkeypatch) -> None:
    import tftlab.public_snapshot as ps

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    h = Harness(tmp_path)
    assert h.run().outcome == lc.SUCCESS
    calls = []
    real_export, real_verify = ps.export_public_snapshot, ps.verify_public_snapshot
    monkeypatch.setattr(ps, "export_public_snapshot", lambda *a, **k: calls.append("export") or real_export(*a, **k))
    monkeypatch.setattr(ps, "verify_public_snapshot", lambda *a, **k: calls.append("verify") or real_verify(*a, **k))
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS + 60_000)
    result = CliRunner().invoke(cli.app, ["local-snapshot"])
    assert result.exit_code == 0, result.output
    assert calls[0] == "export" and calls[-1] == "verify"
    public = tmp_path / "data/local/public"
    gz = public / "theorylabs-public-snapshot.sqlite3.gz"
    assert gz.exists() and (public / "theorylabs-public-snapshot.sqlite3.manifest.json").exists()
    out = re.sub(r"\s+", " ", result.output)
    assert "Public snapshot created and verified" in out and "Windows: 18.4; matches 40; boards 320" in out
    assert "theorylabs-public-snapshot.sqlite3.gz:" in out and "sha256" in out
    assert "Nothing was uploaded or deployed" in out
    assert ps.read_snapshot_provenance  # the website reads exactly this file format
    _assert_no_identifiers(result.output, h.stub)


def test_local_snapshot_refuses_demo_data(tmp_path: Path, monkeypatch) -> None:
    from tftlab.demo import generate_demo_matches

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data/local/theorylabs.sqlite3"
    db.parent.mkdir(parents=True)
    with Database(db) as database:
        database.ingest_many(generate_demo_matches(20))
    result = CliRunner().invoke(cli.app, ["local-snapshot", "--balance-window", "18.3"])
    assert result.exit_code == 1 and "Public snapshot NOT created" in result.output
    assert not (tmp_path / "data/local/public/theorylabs-public-snapshot.sqlite3.gz").exists()


def test_local_snapshot_without_a_current_window_needs_an_explicit_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data/local/theorylabs.sqlite3"
    build_live_ingest_like_source(db, 12)  # 18.3 data
    monkeypatch.setattr(lc, "now_ms", lambda: _ms("2026-10-07T12:00:00+00:00"))  # the gap
    result = CliRunner().invoke(cli.app, ["local-snapshot"])
    assert result.exit_code == 1 and "--balance-window" in result.output
    result = CliRunner().invoke(cli.app, ["local-snapshot", "--balance-window", "18.3"])
    assert result.exit_code == 0, result.output
    assert "Windows: 18.3; matches 12" in re.sub(r"\s+", " ", result.output)


def test_the_api_key_never_reaches_the_database_backups_reports_or_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    stub = StubRiot(_payloads_18_4(20))
    monkeypatch.setattr(cli, "_local_riot_client", lambda *a, **k: stub)
    monkeypatch.setattr(cli, "_fetch_current_metadata", _metadata)
    for offset in (0, 60_000):
        monkeypatch.setattr(lc, "now_ms", lambda o=offset: AT_MS + o)
        assert CliRunner().invoke(cli.app, ["local-collect"]).exit_code == 0
    result = CliRunner().invoke(cli.app, ["local-snapshot"])
    assert result.exit_code == 0, result.output
    files = _bytes_under(tmp_path / "data")
    assert any(p.suffix == ".gz" for p in files) and any("backups" in p.parts for p in files)
    assert any("reports" in p.parts for p in files)
    for path, content in files.items():
        assert FAKE_KEY.encode() not in content, path
    import gzip
    assert FAKE_KEY.encode() not in gzip.decompress(
        (tmp_path / "data/local/public/theorylabs-public-snapshot.sqlite3.gz").read_bytes())


# ---------------------------------------------------------------- launchers and git safety


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["local-collect.sh", "local-collect.ps1", "local-collect.cmd"])
def test_launchers_only_invoke_the_canonical_cli(name: str) -> None:
    text = (ROOT / "scripts" / name).read_text()
    assert "local-collect" in text
    for logic in ("ingest-riot", "sqlite3 ", "validate-live-data", "prepare-discovery", "DATABASE_URL", "RIOT_API_KEY="):
        assert logic not in text, logic


def test_launcher_sh_is_executable() -> None:
    assert os.access(ROOT / "scripts" / "local-collect.sh", os.X_OK)


@pytest.mark.parametrize("path", [
    ".env", "data/local/theorylabs.sqlite3", "data/local/theorylabs.sqlite3-wal", "data/local/theorylabs.sqlite3-shm",
    "data/local/theorylabs.sqlite3-journal", "data/local/backups/theorylabs-20261008T090000Z.sqlite3",
    "data/local/public/theorylabs-public-snapshot.sqlite3.gz", "data/local/reports/local-collect-x.json",
    "data/local/.theorylabs.sqlite3.collect.lock", "elsewhere/theorylabs-public-snapshot.sqlite3.gz",
    "elsewhere/theorylabs-public-snapshot.sqlite3.manifest.json", "out/snap.sqlite3.building",
    "data/tftlab.sqlite3", "x/some.sqlite3-wal",
])
def test_private_data_cannot_be_committed(path: str) -> None:
    import subprocess

    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    proc = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT)
    assert proc.returncode == 0, f"{path} is not ignored"


@pytest.mark.parametrize("path", [".env.example", "tests/test_local_collector.py", "scripts/local-collect.sh",
                                  "src/tftlab/data/item_stats.json"])
def test_repository_files_are_not_ignored(path: str) -> None:
    import subprocess

    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 1


# ---------------------------------------------------------------- the real Riot client, mocked HTTP


def test_end_to_end_with_the_real_riot_client_over_a_mock_transport(tmp_path: Path) -> None:
    """The production client path: RiotClient (with the local 10:10 ceiling)
    talking to a mocked Riot API. The key goes out only as the X-Riot-Token
    header and never into the database, backups or report."""
    import httpx

    from tftlab.riot import RiotClient
    from tftlab.riot_limits import parse_rate_limits

    stub = StubRiot(_payloads_18_4(24))
    seen_tokens = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.add(request.headers.get("X-Riot-Token"))
        path = request.url.path
        headers = {"X-App-Rate-Limit": "20:1,100:120", "X-App-Rate-Limit-Count": "1:1,1:120"}
        if path.startswith("/tft/league/v1/entries/"):
            _, tier, division = path.rsplit("/", 2)
            body = stub.league_entries(tier, division, page=int(request.url.params.get("page", 1)))
        elif path.startswith("/tft/league/v1/"):
            body = getattr(stub, path.rsplit("/", 1)[1])()
        elif path.endswith("/ids"):
            p = request.url.params
            body = stub.match_ids(path.split("/")[-2], count=int(p["count"]), start_time=int(p["startTime"]),
                                  end_time=int(p["endTime"]))
        else:
            body = stub.match(path.rsplit("/", 1)[1])
        return httpx.Response(200, json=body, headers=headers)

    def factory(api_key, *, platform, region):
        return RiotClient(api_key, platform=platform, region=region, transport=httpx.MockTransport(handler),
                          rate_ceilings=parse_rate_limits(lc.DEFAULT_RATE_CEILING, "x"))

    h = Harness(tmp_path)
    report = lc.run_local_collect(h.config(), api_key=FAKE_KEY, platform="na1", region="americas", at_ms=AT_MS,
                                  client_factory=factory, fetch_metadata=_metadata)
    assert report.outcome == lc.SUCCESS, report.message
    assert seen_tokens == {FAKE_KEY}
    assert report.ingest["inserted"] == 24 and report.riot["requests"] > 100 and report.riot["rate_limited"] == 0
    lc.write_report(report, lc.local_dirs(h.db)["reports"], at_ms=AT_MS)
    for path, content in _bytes_under(tmp_path).items():
        assert FAKE_KEY.encode() not in content, path


def test_local_status_explains_an_unreadable_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data/local/theorylabs.sqlite3"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(lc, "now_ms", lambda: AT_MS)
    result = CliRunner().invoke(cli.app, ["local-status"])
    assert result.exit_code == 0, result.output
    assert "could not be read (SchemaUnavailable)" in re.sub(r"\s+", " ", result.output)


def test_run_ids_are_local_even_inside_ci(tmp_path: Path, monkeypatch) -> None:
    """GITHUB_RUN_ID must not leak into local run ids (it would give every
    run in one CI job the same id and make the second run fail)."""
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    h = Harness(tmp_path)
    assert h.run().outcome == lc.SUCCESS
    assert h.run(at_ms=AT_MS + 60_000).outcome == lc.SUCCESS
    assert [r[0] for r in _rows(h.db, "SELECT run_id FROM ingest_runs ORDER BY started_at")] == \
        [f"local-{AT_MS}", f"local-{AT_MS + 60_000}"]
