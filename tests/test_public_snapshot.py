"""Sanitized public snapshots (`tftlab export-public-snapshot`): sanitization,
opaque match ids, fail-closed provenance, integrity checks, and the website
serving only certified snapshots as observed evidence."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from _helpers import REAL_18_3_START_MS, build_live_ingest_like_source
from tftlab import public_snapshot as ps
from tftlab.cli import app
from tftlab.demo import generate_demo_matches
from tftlab.experiments import create_experiment, seed_demo_experiments
from tftlab.storage import Database

POSTGRES_TEST_URL = os.environ.get("TFTLAB_TEST_DATABASE_URL")


def _export(source_path: Path, out: Path, **kw) -> ps.ExportResult:
    with Database.open_existing(source_path) as db:
        return ps.export_public_snapshot(db, out, **kw)


@pytest.fixture()
def source(tmp_path: Path) -> tuple[Path, list[dict]]:
    path = tmp_path / "source.sqlite3"
    payloads = build_live_ingest_like_source(path, 24)
    return path, payloads


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


# ---------------------------------------------------------------- export and sanitization


def test_export_writes_a_verified_sanitized_snapshot(source, tmp_path) -> None:
    src, payloads = source
    out = tmp_path / "pub" / "public.sqlite3"
    result = _export(src, out, compress=True, secrets=[str(src)])
    assert out.is_file() and result.gzip_path and result.gzip_path.is_file()
    assert sorted(p.name for p in out.parent.iterdir()) == [  # one self-contained file: no WAL/-shm sidecars
        "public.sqlite3", "public.sqlite3.gz", "public.sqlite3.manifest.json"]
    assert sqlite3.connect(out).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert set(result.verification["checks"]) == {
        "read_schema", "provenance", "public_read_path", "no_collection_tables", "no_puuid_columns", "no_raw_payloads",
        "no_augment_payloads", "opaque_match_ids", "referential_consistency", "trusted_windows_only",
        "windows_match_provenance", "requested_windows_only", "counts_match_provenance", "counts_reconcile_with_source",
        "no_secrets_or_identifiers", "participant_levels_plausible", "prepared_champions_published"}
    tables = _tables(out)
    assert not tables & {"seed_samples", "match_discoveries", "ingest_runs"}
    assert {"matches", "participants", "units", "traits", "experiments", ps.METADATA_TABLE} <= tables
    conn = sqlite3.connect(out)
    try:
        assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 24
        assert conn.execute("SELECT DISTINCT payload_json FROM matches").fetchall() == [("{}",)]
        assert conn.execute("SELECT DISTINCT augments_json FROM participants").fetchall() == [("[]",)]
        # Real final player levels are kept (non-identifying gameplay context).
        source_levels = sorted(r[0] for r in sqlite3.connect(src).execute("SELECT level FROM participants"))
        assert sorted(r[0] for r in conn.execute("SELECT level FROM participants")) == source_levels
        assert set(source_levels) - {0}
        assert conn.execute("SELECT DISTINCT queue_id, balance_window FROM matches").fetchall() == [(1100, "18.3")]
        ids = [r[0] for r in conn.execute("SELECT match_id FROM matches ORDER BY game_datetime")]
        assert ids == [f"S{i:07d}" for i in range(1, 25)]  # opaque, numbered by game time
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    for payload in payloads:  # no Riot match id and no PUUID anywhere in the file
        assert payload["metadata"]["match_id"] not in dump
        assert all(puuid not in dump for puuid in payload["metadata"]["participants"])
    with gzip.open(result.gzip_path) as fh:
        assert fh.read() == out.read_bytes()
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["files"][out.name]["bytes"] == out.stat().st_size
    assert manifest["provenance"] == result.provenance


def test_provenance_records_what_was_exported_and_how(source, tmp_path) -> None:
    src, _ = source
    prov = _export(src, tmp_path / "p.sqlite3", exported_at_ms=REAL_18_3_START_MS).provenance
    assert (prov["format"], prov["format_version"], prov["synthetic"], prov["observed"], prov["source_kind"]) == (
        "theorylabs-public-snapshot", 1, False, True, "riot-match-v1-ranked-tft")
    assert prov["exported_at"] == "2026-09-24T07:00:00Z"
    assert prov["balance_windows"] == ["18.3"] and prov["matches"] == 24 and prov["boards"] == 24 * 8
    assert prov["latest_game_datetime"] == REAL_18_3_START_MS + 600_000 * 24
    assert prov["real_ingestion_checks"]["passed"] is True
    assert prov["real_ingestion_checks"]["completed_ingest_runs"] == 1
    assert prov["real_ingestion_checks"]["discovery_ledger_coverage"] == 1.0
    assert len(prov["source_population_fingerprint"]["value"]) == 64
    assert prov["excluded_tables"] == ["seed_samples", "match_discoveries", "ingest_runs"]
    assert prov["sanitized_columns"]["matches"]["payload_json"] == "{}"
    assert "PUUIDs" in prov["exclusions"] and "no mapping" in prov["exclusions"]
    assert "analytics_version" in prov["code_version"]


def test_match_id_remapping_is_deterministic_and_consistent(source, tmp_path) -> None:
    src, _ = source
    a, b = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"
    _export(src, a, prepare=False)
    _export(src, b, prepare=False)

    def rows(path: Path) -> list:
        conn = sqlite3.connect(path)
        try:
            return [conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in ("matches", "participants", "units")]
        finally:
            conn.close()

    assert rows(a) == rows(b)
    conn = sqlite3.connect(a)
    try:  # every board/unit/trait points at an exported match
        for t in ("participants", "units", "traits"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t} WHERE match_id NOT IN (SELECT match_id FROM matches)").fetchone()[0] == 0
    finally:
        conn.close()


def test_window_scoped_export_contains_only_the_requested_windows(tmp_path) -> None:
    src = tmp_path / "two.sqlite3"
    build_live_ingest_like_source(src, 10)
    with Database(src) as db:  # move half of the matches into the 18.2 trusted window
        ids = [r[0] for r in db.query_all("SELECT match_id FROM matches ORDER BY match_id")][:5]
        for i, match_id in enumerate(ids):
            db.execute("UPDATE matches SET game_datetime = ?, patch = '18.2', balance_window = '18.2' WHERE match_id = ?",
                       (1_789_084_800_000 + i, match_id))
        db.commit()
    result = _export(src, tmp_path / "w.sqlite3", balance_windows=["18.3"])
    assert result.provenance["balance_windows"] == ["18.3"] and result.provenance["matches"] == 5
    assert result.verification["balance_windows"] == ["18.3"]
    with pytest.raises(ps.SnapshotExportError, match="not present"):
        _export(src, tmp_path / "x.sqlite3", balance_windows=["18.9"])


def test_only_the_operators_own_experiments_are_exported(source, tmp_path) -> None:
    src, _ = source
    with Database(src) as db:
        seed_demo_experiments(db)  # origin 'demo': never exported
        create_experiment(db, {"title": "Owner idea", "carry_name": "Kha'Zix"})
    out = tmp_path / "e.sqlite3"
    _export(src, out)
    conn = sqlite3.connect(out)
    try:
        assert conn.execute("SELECT title, origin FROM experiments").fetchall() == [("Owner idea", "manual")]
    finally:
        conn.close()


# ---------------------------------------------------------------- synthetic data can never become "observed"


def test_the_demo_dataset_cannot_be_exported(tmp_path) -> None:
    demo = tmp_path / "demo.sqlite3"
    with Database(demo) as db:
        db.ingest_many(generate_demo_matches(30))
    with pytest.raises(ps.SnapshotExportError, match="not trusted patch balance windows"):
        _export(demo, tmp_path / "out.sqlite3")
    with pytest.raises(ps.SnapshotExportError, match="not trusted patch balance windows"):
        _export(demo, tmp_path / "out.sqlite3", balance_windows=["Version DEMO"])
    assert not (tmp_path / "out.sqlite3").exists()


def test_demo_matches_fail_even_with_a_forged_ledger_and_a_trusted_window(tmp_path) -> None:
    demo = tmp_path / "forged.sqlite3"
    payloads = generate_demo_matches(12)
    with Database(demo) as db:
        db.ingest_many(payloads)
        db.execute("UPDATE matches SET patch = '18.3', balance_window = '18.3'")
        db.start_ingest_run("r", 1)
        db.finalize_ingest_run("r", [("p", "challenger")], [(p["metadata"]["match_id"], "p", "challenger") for p in payloads],
                               sampled_at=2, completed_at=3)
    with pytest.raises(ps.SnapshotExportError) as err:
        _export(demo, tmp_path / "out.sqlite3")
    message = str(err.value)
    assert "match id is not a regional Riot Match-V1 id: 12" in message
    assert "payload has no Match-V1 data_version: 12" in message
    assert not (tmp_path / "out.sqlite3").exists() and not (tmp_path / "out.sqlite3.building").exists()


def test_a_source_without_completed_ingestion_provenance_is_refused(tmp_path) -> None:
    src = tmp_path / "noledger.sqlite3"
    build_live_ingest_like_source(src, 6, ledger=False)
    with pytest.raises(ps.SnapshotExportError, match="no completed live-ingest run"):
        _export(src, tmp_path / "out.sqlite3")


def test_every_exported_match_linked_to_a_completed_run_passes_and_says_so(source, tmp_path) -> None:
    src, _ = source
    checks = _export(src, tmp_path / "all.sqlite3").provenance["real_ingestion_checks"]
    assert checks["matches_exported"] == checks["matches_linked_to_completed_runs"] == 24
    assert checks["every_match_linked_to_a_completed_run"] is True and checks["discovery_ledger_coverage"] == 1.0


def _first_match(path: Path) -> str:
    with Database.open_existing(path) as db:
        return db.query_one("SELECT match_id FROM matches ORDER BY match_id LIMIT 1")[0]


def test_one_match_without_any_completed_run_link_blocks_the_export(source, tmp_path) -> None:
    src, _ = source
    orphan = _first_match(src)
    with Database(src) as db:
        db.execute("DELETE FROM match_discoveries WHERE match_id = ?", (orphan,))
        db.commit()
    # 1 of 24 is more than MAX_UNLINKED_SHARE: not a stopped run, so the export refuses.
    with pytest.raises(ps.SnapshotExportError, match=r"1 of 24 matches in \['18.3'\] are not linked to a completed"):
        _export(src, tmp_path / "out.sqlite3")
    assert not (tmp_path / "out.sqlite3").exists()


@pytest.mark.parametrize("status", ["failed", "started"])
def test_a_match_linked_only_to_an_unfinished_run_blocks_the_export(status, source, tmp_path) -> None:
    src, _ = source
    orphan = _first_match(src)
    with Database(src) as db:
        db.start_ingest_run("run-unfinished", 10)
        db.execute("UPDATE ingest_runs SET status = ? WHERE run_id = 'run-unfinished'", (status,))
        puuid = db.query_one("SELECT puuid FROM match_discoveries WHERE match_id = ?", (orphan,))[0]
        db.execute("DELETE FROM match_discoveries WHERE match_id = ?", (orphan,))
        db.execute("INSERT INTO match_discoveries (match_id, run_id, puuid, cohort, discovered_at) VALUES (?, ?, ?, ?, ?)",
                   (orphan, "run-unfinished", puuid, "challenger", 11))
        db.commit()
    with pytest.raises(ps.SnapshotExportError, match="1 of 24 matches"):
        _export(src, tmp_path / "out.sqlite3")


def _unlink(path: Path, match_ids: list[str], *, failed_run: bool = False) -> None:
    """What a collection run stopped by an expired key leaves behind: stored
    matches without discovery rows (or, `failed_run`, linked only to that run)."""
    with Database(path) as db:
        if failed_run:
            db.start_ingest_run("run-stopped", 10)
            db.execute("UPDATE ingest_runs SET status = 'failed' WHERE run_id = 'run-stopped'")
        for match_id in match_ids:
            puuid = db.query_one("SELECT puuid FROM match_discoveries WHERE match_id = ?", (match_id,))[0]
            db.execute("DELETE FROM match_discoveries WHERE match_id = ?", (match_id,))
            if failed_run:
                db.execute("INSERT INTO match_discoveries (match_id, run_id, puuid, cohort, discovered_at) "
                           "VALUES (?, 'run-stopped', ?, 'challenger', 11)", (match_id, puuid))
        db.commit()


def _match_ids(path: Path) -> list[str]:
    with Database.open_existing(path) as db:
        return [r[0] for r in db.query_all("SELECT match_id FROM matches ORDER BY game_datetime, match_id")]


@pytest.mark.parametrize("failed_run", [False, True])
def test_a_few_matches_from_a_stopped_run_are_left_out_and_counted(failed_run, tmp_path) -> None:
    src = tmp_path / "big.sqlite3"
    build_live_ingest_like_source(src, 100)
    stopped = _match_ids(src)[-2:]  # 2% of 100: the most that is left out instead of refused
    _unlink(src, stopped, failed_run=failed_run)
    result = _export(src, tmp_path / "out.sqlite3")
    checks = result.provenance["real_ingestion_checks"]
    assert result.provenance["matches"] == checks["matches_exported"] == checks["matches_linked_to_completed_runs"] == 98
    assert checks["every_match_linked_to_a_completed_run"] is True and checks["discovery_ledger_coverage"] == 1.0
    assert checks["matches_excluded_without_completed_run"] == 2
    assert checks["excluded_without_completed_run_per_window"] == {"18.3": 2}
    assert checks["max_excluded_share"] == ps.MAX_UNLINKED_SHARE == 0.02
    assert result.provenance["per_window"]["18.3"]["matches"] == 98
    assert result.verification["counts"]["matches"] == 98
    with Database.open_existing(src) as db:  # the left-out matches are the last two games: the snapshot ends earlier
        kept_latest = db.query_one("SELECT game_datetime FROM matches WHERE match_id = ?", (_match_ids(src)[-3],))[0]
    assert result.provenance["latest_game_datetime"] == kept_latest


def test_more_than_the_share_of_unlinked_matches_still_refuses(tmp_path) -> None:
    src = tmp_path / "big.sqlite3"
    build_live_ingest_like_source(src, 100)
    _unlink(src, _match_ids(src)[:3])
    with pytest.raises(ps.SnapshotExportError, match=r"3 of 100 matches .* more than 2%"):
        _export(src, tmp_path / "out.sqlite3")
    assert not (tmp_path / "out.sqlite3").exists()


def test_nothing_left_out_says_zero(source, tmp_path) -> None:
    src, _ = source
    checks = _export(src, tmp_path / "all.sqlite3").provenance["real_ingestion_checks"]
    assert checks["matches_excluded_without_completed_run"] == 0
    assert checks["excluded_without_completed_run_per_window"] == {}
    assert checks["games_with_bots_excluded"] == 0 and checks["games_with_bots_excluded_per_window"] == {}


def _rewrite_payload(path: Path, match_id: str, change) -> None:
    with Database(path) as db:
        payload = json.loads(db.query_one("SELECT payload_json FROM matches WHERE match_id = ?", (match_id,))[0])
        change(payload)
        db.execute("UPDATE matches SET payload_json = ? WHERE match_id = ?", (json.dumps(payload), match_id))
        db.commit()


def _with_bots(payload: dict) -> None:
    """What Riot returned for these lobbies: one real player in the metadata,
    the seven other boards all repeating one placeholder PUUID."""
    boards = payload["info"]["participants"]
    payload["metadata"]["participants"] = [boards[0]["puuid"]]
    for board in boards[1:]:
        board["puuid"] = "BOT" * 26


def test_a_game_with_bots_is_left_out_and_counted(tmp_path) -> None:
    src = tmp_path / "big.sqlite3"
    build_live_ingest_like_source(src, 100)
    bot_game = _match_ids(src)[50]
    _rewrite_payload(src, bot_game, _with_bots)
    result = _export(src, tmp_path / "out.sqlite3")
    checks = result.provenance["real_ingestion_checks"]
    assert result.provenance["matches"] == checks["matches_exported"] == 99 and result.provenance["boards"] == 99 * 8
    assert checks["games_with_bots_excluded"] == 1 and checks["games_with_bots_excluded_per_window"] == {"18.3": 1}
    assert checks["matches_excluded_without_completed_run"] == 0
    conn = sqlite3.connect(result.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 99 * 8
    finally:
        conn.close()


def test_stopped_run_matches_and_bot_games_share_one_limit(tmp_path) -> None:
    src = tmp_path / "big.sqlite3"
    build_live_ingest_like_source(src, 100)
    ids = _match_ids(src)
    _unlink(src, ids[:2])
    _rewrite_payload(src, ids[50], _with_bots)
    with pytest.raises(ps.SnapshotExportError, match=r"2 matches without a completed live-ingest run and 1 games with bots"):
        _export(src, tmp_path / "out.sqlite3")
    assert not (tmp_path / "out.sqlite3").exists()


def _drop_a_player(payload: dict) -> None:
    payload["metadata"]["participants"] = payload["metadata"]["participants"][:7]


def _two_placeholder_ids(payload: dict) -> None:
    _with_bots(payload)
    payload["info"]["participants"][1]["puuid"] = "OTHER" * 16


def _real_player_not_on_a_board(payload: dict) -> None:
    _with_bots(payload)
    payload["metadata"]["participants"] = ["SOMEONE" * 11]


@pytest.mark.parametrize("change", [_drop_a_player, _two_placeholder_ids, _real_player_not_on_a_board])
def test_any_other_participant_mismatch_still_blocks_the_export(change, tmp_path) -> None:
    src = tmp_path / "big.sqlite3"
    build_live_ingest_like_source(src, 100)
    _rewrite_payload(src, _match_ids(src)[50], change)
    with pytest.raises(ps.SnapshotExportError, match="payload participant list does not match the stored boards: 1"):
        _export(src, tmp_path / "out.sqlite3")


@pytest.mark.parametrize(("sql", "reason"), [
    ("UPDATE matches SET queue_id = 1090 WHERE rowid = 1", "queue_id is not ranked TFT (1100): 1"),
    ("UPDATE matches SET payload_json = '{}' WHERE rowid = 2", "payload metadata.match_id does not match: 1"),
])
def test_one_non_conforming_match_blocks_the_whole_export(source, tmp_path, sql, reason) -> None:
    src, _ = source
    with Database(src) as db:
        db.execute(sql)
        db.commit()
    with pytest.raises(ps.SnapshotExportError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        _export(src, tmp_path / "out.sqlite3")


# ---------------------------------------------------------------- verification catches leaks and tampering


@pytest.mark.parametrize(("tamper", "check"), [
    ("CREATE TABLE seed_samples (run_id TEXT, puuid TEXT)", "no_collection_tables"),
    ("UPDATE matches SET payload_json = '{\"metadata\": {}}' WHERE rowid = 1", "no_raw_payloads"),
    ("UPDATE participants SET augments_json = '[\"TFT_Augment_X\"]' WHERE rowid = 1", "no_augment_payloads"),
    ("UPDATE units SET unit_name = 'RGAPI-00000000-0000' WHERE rowid = 1", "no_secrets_or_identifiers"),
    ("UPDATE units SET unit_name = '" + "a" * 78 + "' WHERE rowid = 1", "no_secrets_or_identifiers"),
    ("UPDATE experiments SET summary = 'see postgresql://u:p@h/db'", "no_secrets_or_identifiers"),
    ("UPDATE participants SET match_id = 'S9999999' WHERE rowid = 1", "referential_consistency"),
])
def test_verification_rejects_tampered_or_leaky_snapshots(source, tmp_path, tamper, check) -> None:
    src, _ = source
    with Database(src) as db:
        create_experiment(db, {"title": "Owner idea"})
    out = tmp_path / "t.sqlite3"
    _export(src, out)
    conn = sqlite3.connect(out)
    conn.execute(tamper)
    conn.commit()
    conn.close()
    with pytest.raises(ps.SnapshotExportError, match=check):
        ps.verify_public_snapshot(out)


@pytest.mark.parametrize("change", [
    {"synthetic": True}, {"observed": False}, {"format_version": 99}, {"source_kind": "demo"},
    {"real_ingestion_checks": {"passed": False}},
])
def test_provenance_that_does_not_certify_observed_data_is_rejected(source, tmp_path, change) -> None:
    src, _ = source
    out = tmp_path / "p.sqlite3"
    prov = _export(src, out).provenance
    conn = sqlite3.connect(out)
    conn.execute(f"UPDATE {ps.METADATA_TABLE} SET value = ?", (json.dumps({**prov, **change}),))
    conn.commit()
    conn.close()
    with Database.open_existing(out) as db, pytest.raises(ps.SnapshotProvenanceError):
        ps.read_snapshot_provenance(db)


# ---------------------------------------------------------------- the website serves only certified snapshots


def _site(monkeypatch, tmp_path, snapshot: Path) -> TestClient:
    for key in ("DATABASE_URL", "RIOT_API_KEY", "TFT_STALE_AFTER_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TFT_DATA_SOURCE", "snapshot")
    monkeypatch.setenv("TFT_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("TFT_SNAPSHOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    from tftlab.webapp import create_app

    return TestClient(create_app())


@pytest.mark.parametrize("compressed", [False, True])
def test_website_serves_an_exported_snapshot_as_observed(source, tmp_path, monkeypatch, compressed) -> None:
    src, payloads = source
    result = _export(src, tmp_path / "pub" / "public.sqlite3", compress=compressed)
    client = _site(monkeypatch, tmp_path, result.gzip_path if compressed else result.path)
    body = client.get("/api/source").json()
    assert (body["mode"], body["observed"], body["synthetic"], body["demo"]) == ("snapshot", True, False, False)
    assert body["snapshot"]["format"] == "theorylabs-public-snapshot" and body["snapshot"]["matches"] == 24
    assert body["snapshot"]["balance_windows"] == ["18.3"]
    discovery = client.get("/api/discovery").json()
    assert discovery["demo"] is False and discovery["prepared"]["status"] == "current"  # prepared inside the snapshot
    texts = [client.get(p).text for p in ("/api/discovery", "/api/champions", "/api/champions/khazix",
                                          "/api/balance-windows", "/api/source", "/api/health")]
    assert all('"demo":false' in t.replace(" ", "") for t in texts[:2])
    for payload in payloads:
        assert all(payload["metadata"]["match_id"] not in t for t in texts)
    assert not (tmp_path / "web-demo.sqlite3").exists()  # never fell back to demo


def test_a_structurally_valid_demo_database_is_never_served_as_observed(tmp_path, monkeypatch) -> None:
    fake = tmp_path / "looks-fine.sqlite3"
    with Database(fake) as db:  # passes the read schema, but no exporter provenance
        db.ingest_many(generate_demo_matches(20))
    client = _site(monkeypatch, tmp_path, fake)
    response = client.get("/api/source")
    assert response.status_code == 503 and response.json()["demo"] is False
    assert response.json()["error"] == "the configured analytics snapshot is unavailable"
    assert client.get("/api/discovery").status_code == 503


def test_a_snapshot_with_tampered_provenance_is_refused_by_the_website(source, tmp_path, monkeypatch) -> None:
    src, _ = source
    out = tmp_path / "pub.sqlite3"
    prov = _export(src, out).provenance
    conn = sqlite3.connect(out)
    conn.execute(f"UPDATE {ps.METADATA_TABLE} SET value = ?", (json.dumps({**prov, "synthetic": True}),))
    conn.commit()
    conn.close()
    assert _site(monkeypatch, tmp_path, out).get("/api/source").status_code == 503


def test_a_corrupt_compressed_snapshot_is_unavailable(source, tmp_path, monkeypatch) -> None:
    src, _ = source
    result = _export(src, tmp_path / "pub.sqlite3", compress=True)
    broken = tmp_path / "broken.sqlite3.gz"
    data = bytearray(result.gzip_path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    broken.write_bytes(bytes(data))
    assert _site(monkeypatch, tmp_path, broken).get("/api/source").status_code == 503


# ---------------------------------------------------------------- CLI


def test_cli_exports_and_verifies(source, tmp_path) -> None:
    src, _ = source
    out = tmp_path / "cli" / "public.sqlite3"
    result = CliRunner().invoke(app, ["export-public-snapshot", "--source", str(src), "--out", str(out), "--compress"])
    assert result.exit_code == 0, result.output
    assert "Snapshot:" in result.output and "sha256" in result.output and out.is_file()
    check = CliRunner().invoke(app, ["verify-public-snapshot", str(out)])
    assert check.exit_code == 0 and "Valid public snapshot" in check.output


def test_cli_refuses_a_demo_source_and_writes_nothing(tmp_path) -> None:
    demo = tmp_path / "demo.sqlite3"
    with Database(demo) as db:
        db.ingest_many(generate_demo_matches(10))
    out = tmp_path / "public.sqlite3"
    result = CliRunner().invoke(app, ["export-public-snapshot", "--source", str(demo), "--out", str(out)])
    assert result.exit_code == 1 and "NOT written" in result.output and not out.exists()
    bad = CliRunner().invoke(app, ["verify-public-snapshot", str(demo)])
    assert bad.exit_code == 1 and "NOT a valid public snapshot" in bad.output


def test_cli_requires_an_explicit_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")  # never used as a fallback
    result = CliRunner().invoke(app, ["export-public-snapshot", "--out", str(tmp_path / "o.sqlite3")])
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)  # rich may colour/wrap the usage error
    assert result.exit_code == 2 and "source" in plain  # a usage error: --source is required
    assert not (tmp_path / "o.sqlite3").exists()


# ---------------------------------------------------------------- Postgres source (a restored backup)


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Set TFTLAB_TEST_DATABASE_URL to run")
def test_export_from_a_postgres_source_matches_the_sqlite_export(tmp_path) -> None:
    from _helpers import live_ingest_like_payloads

    payloads = live_ingest_like_payloads(16)
    db = Database(POSTGRES_TEST_URL)
    try:
        for table in ("match_discoveries", "seed_samples", "ingest_runs", "discovery_prepared_candidates",
                      "discovery_prepared_runs", "experiment_field_notes", "experiment_tags", "experiments",
                      "traits", "units", "participants", "matches"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        db.ingest_many(payloads)
        db.start_ingest_run("run-pg", 1)
        db.finalize_ingest_run("run-pg", [(payloads[0]["metadata"]["participants"][0], "challenger")],
                               [(p["metadata"]["match_id"], p["metadata"]["participants"][0], "challenger") for p in payloads],
                               sampled_at=2, completed_at=3)
    finally:
        db.close()
    with Database.open_existing(POSTGRES_TEST_URL) as ro:  # the restored backup is only ever read
        pg = ps.export_public_snapshot(ro, tmp_path / "pg.sqlite3", secrets=[POSTGRES_TEST_URL])
    lite_src = tmp_path / "lite.sqlite3"
    with Database(lite_src) as lite:
        lite.ingest_many(payloads)
        lite.start_ingest_run("run-pg", 1)
        lite.finalize_ingest_run("run-pg", [(payloads[0]["metadata"]["participants"][0], "challenger")],
                                 [(p["metadata"]["match_id"], p["metadata"]["participants"][0], "challenger") for p in payloads],
                                 sampled_at=2, completed_at=3)
    lite = _export(lite_src, tmp_path / "lite-out.sqlite3")
    assert pg.provenance["source_population_fingerprint"] == lite.provenance["source_population_fingerprint"]
    assert pg.verification["counts"] == lite.verification["counts"]

    def rows(path: Path) -> list:
        conn = sqlite3.connect(path)
        try:
            return [conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall() for t in ("matches", "units", "traits")]
        finally:
            conn.close()

    assert rows(pg.path) == rows(lite.path)
    shutil.rmtree(tmp_path / "cache", ignore_errors=True)
