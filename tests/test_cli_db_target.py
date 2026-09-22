import pytest

from tftlab.cli import _resolve_db_target


def test_resolve_db_target_preserves_postgres_url_double_slash() -> None:
    """Regression test: an earlier version of `--db` was typed as typer's
    `Path`, which collapses `postgresql://host/db`'s `//` into a single `/`,
    silently turning a valid Postgres URL into a broken one that `Database`
    would then misidentify as a SQLite file path."""
    # A placeholder URL is enough: this test never connects, it only checks
    # that the string passes through _resolve_db_target unmodified.
    url = "postgresql://user:pass@db.example.invalid:5432/tftlab"
    assert _resolve_db_target(url) == url


def test_resolve_db_target_falls_back_to_settings_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@example.invalid/db")
    assert _resolve_db_target(None) == "postgresql://a:b@example.invalid/db"

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", "data/somewhere.sqlite3")
    assert _resolve_db_target(None) == "data/somewhere.sqlite3"
