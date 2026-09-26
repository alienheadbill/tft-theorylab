from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, database_url: str | None) -> TestClient:
    if database_url is not None:
        monkeypatch.setenv("DATABASE_URL", database_url)
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))

    from tftlab.webapp import create_app

    return TestClient(create_app())


def test_unreachable_database_url_returns_503_not_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DATABASE_URL that's configured but unreachable must surface as a
    clear failure -- never silently downgrade to the demo dataset."""
    # Port 1 has no listener; psycopg should fail fast with connection refused.
    client = _client(monkeypatch, tmp_path, database_url="postgresql://x:x@127.0.0.1:1/x")

    response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["demo"] is False
    assert body["status"] == "error"
    # Never leak the DSN/credentials in the error body.
    assert "x:x" not in body["error"]
    assert "127.0.0.1" not in body["error"]


def test_unreachable_database_url_fails_every_endpoint_not_just_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, tmp_path, database_url="postgresql://x:x@127.0.0.1:1/x")

    for path in ("/api/carries", "/api/discovery"):
        response = client.get(path)
        assert response.status_code == 503, path
        assert response.json()["demo"] is False


def test_reachable_but_empty_database_url_reports_live_not_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connecting successfully with zero matches (a fresh production
    database, initialized by the CLI/ingest, before first ingest) is an
    honest live state, not demo data."""
    # A plain filesystem path (no postgres:// scheme) is treated as SQLite by
    # Database(), so this exercises "DATABASE_URL configured, reachable,
    # empty" without needing a real Postgres server in this test.
    from tftlab.storage import Database

    prod_path = tmp_path / "fresh-production.sqlite3"
    Database(prod_path).close()  # schema set up by the CLI side, never by a request
    client = _client(monkeypatch, tmp_path, database_url=str(prod_path))

    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["demo"] is False
    assert body["matches"] == 0
    assert body["participants"] == 0


def test_uninitialized_database_url_is_503_and_never_created_by_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured database without the schema is a production incident:
    a request must not create tables (or even the file) to paper over it."""
    import sqlite3

    missing = tmp_path / "never-initialized.sqlite3"
    client = _client(monkeypatch, tmp_path, database_url=str(missing))
    response = client.get("/api/health")
    assert response.status_code == 503 and response.json()["demo"] is False
    assert not missing.exists()

    empty = tmp_path / "empty-file.sqlite3"
    sqlite3.connect(empty).close()  # exists, but no schema
    client = _client(monkeypatch, tmp_path, database_url=str(empty))
    assert client.get("/api/carries").status_code == 503
    tables = sqlite3.connect(empty).execute("SELECT name FROM sqlite_master").fetchall()
    assert tables == []


def test_no_database_url_still_falls_back_to_demo_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local/dev behavior (no DATABASE_URL at all) is unchanged: demo is fine."""
    client = _client(monkeypatch, tmp_path, database_url=None)

    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["demo"] is True
    assert body["matches"] > 0
