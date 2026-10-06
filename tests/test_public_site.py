"""Zero-cloud-database public site: explicit data sources, truthful source
labelling, Riot compliance pages, the Riot legal boilerplate and `/riot.txt`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tftlab.demo import generate_demo_matches
from tftlab.storage import Database

WEB = Path(__file__).parent.parent / "src" / "tftlab" / "web"
PAGES = ("/", "/champions", "/champions/khazix", "/experiments", "/about", "/methodology", "/data", "/privacy", "/terms")
HTML_FILES = sorted(WEB.glob("*.html"))
#: Riot General Policies (last updated March 11, 2025): legal boilerplate, verbatim with the product name.
RIOT_BOILERPLATE = ("TheoryLabs isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or "
                    "anyone officially involved in producing or managing Riot Games properties. Riot Games, and all "
                    "associated properties are trademarks or registered trademarks of Riot Games, Inc.")
MAIN_NAV = ["/#discoveries", "/champions", "/experiments", "/about", "/methodology"]
FOOTER_NAV = [*MAIN_NAV, "/privacy", "/terms"]


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, mode: str | None = "demo",
            database_url: str | None = None, **env: str) -> TestClient:
    for key in ("DATABASE_URL", "TFT_DATA_SOURCE", "TFT_SNAPSHOT_PATH", "RIOT_SITE_VERIFICATION", "RIOT_API_KEY",
                "TFT_STALE_AFTER_DAYS"):
        monkeypatch.delenv(key, raising=False)
    if mode is not None:
        monkeypatch.setenv("TFT_DATA_SOURCE", mode)
    if database_url is not None:
        monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "no-local.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "web-demo.sqlite3"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from tftlab.webapp import create_app

    return TestClient(create_app())


def _snapshot(tmp_path: Path, matches: int = 12) -> Path:
    path = tmp_path / "snapshot" / "theorylabs-snapshot.sqlite3"
    with Database(path) as db:  # built by the owner's local engine; the web app only ever reads it
        db.ingest_many(generate_demo_matches(matches))
    return path


# ---------------------------------------------------------------- zero-cloud-database mode


def test_app_starts_and_serves_every_page_without_database_url(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True and health.json()["demo"] is True and health.json()["source"] == "demo"
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path


def test_concurrent_first_requests_build_the_demo_dataset_once(monkeypatch, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    client = _client(monkeypatch, tmp_path)
    paths = ["/api/source", "/api/health", "/api/balance-windows", "/api/discovery", "/api/champions"] * 3
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda p: client.get(p).status_code, paths))
    assert statuses == [200] * len(paths)
    assert client.get("/api/source").json()["matches"] == 180


def test_player_flows_work_in_zero_cloud_db_mode(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    for path in ("/api/balance-windows", "/api/discovery", "/api/champions", "/api/champions/khazix",
                 "/api/experiments", "/api/carries"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text[:200])
        assert response.json()["demo"] is True, path  # every response says it is the synthetic dataset
    assert client.get("/api/discovery").json()["candidates"]  # demo has candidates to show


def test_demo_source_is_labelled_synthetic_never_observed(monkeypatch, tmp_path) -> None:
    body = _client(monkeypatch, tmp_path).get("/api/source").json()
    assert (body["mode"], body["synthetic"], body["observed"], body["demo"]) == ("demo", True, False, True)
    assert "synthetic" in body["label"] and "None of it is observed Riot match evidence" in body["description"]
    assert body["configured"] == "explicit"
    assert body["latest_game_date_is_synthetic"] is True
    assert body["stale"] is None and body["age_days"] is None  # no freshness verdict for generated dates
    assert body["matches"] > 0


def test_automatic_mode_keeps_local_development_behaviour(monkeypatch, tmp_path) -> None:
    local = tmp_path / "local.sqlite3"
    with Database(local) as db:
        db.ingest_many(generate_demo_matches(5))
    client = _client(monkeypatch, tmp_path, mode=None, TFT_DB_PATH=str(local))
    body = client.get("/api/source").json()
    assert (body["mode"], body["observed"], body["synthetic"], body["configured"]) == ("local", True, False, "automatic")
    empty = _client(monkeypatch, tmp_path, mode=None)  # nothing local: the labelled demo dataset
    assert empty.get("/api/source").json()["mode"] == "demo"


def test_snapshot_mode_serves_a_bundled_read_only_snapshot_with_freshness(monkeypatch, tmp_path) -> None:
    path = _snapshot(tmp_path)
    before = path.read_bytes()
    Path(str(path) + ".json").write_text(json.dumps({"exported_at": "2026-10-01T00:00:00Z", "generator": "test"}))
    client = _client(monkeypatch, tmp_path, mode="snapshot", TFT_SNAPSHOT_PATH=str(path))
    body = client.get("/api/source").json()
    assert (body["mode"], body["observed"], body["synthetic"], body["demo"]) == ("snapshot", True, False, False)
    assert body["snapshot"] == {"exported_at": "2026-10-01T00:00:00Z", "generator": "test"}
    assert body["matches"] == 12 and body["stale"] is True  # its latest game is far older than 14 days
    assert client.get("/api/discovery").json()["demo"] is False
    assert client.get("/api/champions").status_code == 200
    assert path.read_bytes() == before  # opened read-only: never written by a request


def test_source_status_freshness_is_computed_against_the_latest_game() -> None:
    from tftlab.webapp import _source, source_status

    with Database(":memory:") as db:
        db.ingest_many(generate_demo_matches(3))
        latest = db.query_one("SELECT MAX(game_datetime) FROM matches")[0]
        fresh = source_status(db, _source("snapshot", "explicit"), now_ms=latest + 2 * 86_400_000)
        old = source_status(db, _source("snapshot", "explicit"), now_ms=latest + 30 * 86_400_000)
    assert (fresh["stale"], fresh["age_days"]) == (False, 2.0)
    assert (old["stale"], old["age_days"]) == (True, 30.0)


# ---------------------------------------------------------------- loud failures, never silent fake data


def test_configured_unreachable_production_database_still_fails_loudly(monkeypatch, tmp_path) -> None:
    for mode in (None, "database"):
        client = _client(monkeypatch, tmp_path, mode=mode, database_url="postgresql://x:x@127.0.0.1:1/x")
        for path in ("/api/health", "/api/source", "/api/discovery"):
            response = client.get(path)
            assert response.status_code == 503, (mode, path)
            body = response.json()
            assert body["demo"] is False and body["ok"] is False
            assert body["error"] == "DATABASE_URL is configured but the database is unreachable"
            assert "x:x" not in response.text and "127.0.0.1" not in response.text
        assert client.get("/about").status_code == 200  # the information pages never need the database


@pytest.mark.parametrize(("mode", "database_url", "error"), [
    ("demo", "postgresql://x:x@127.0.0.1:1/x", "the data source is misconfigured"),
    ("snapshot", "postgresql://x:x@127.0.0.1:1/x", "the data source is misconfigured"),
    ("database", None, "the data source is misconfigured"),
    ("everything", None, "the data source is misconfigured"),
    ("snapshot", None, "the configured analytics snapshot is unavailable"),
])
def test_invalid_or_contradictory_sources_are_503_not_demo(monkeypatch, tmp_path, mode, database_url, error) -> None:
    missing = tmp_path / "missing" / "snapshot.sqlite3"
    client = _client(monkeypatch, tmp_path, mode=mode, database_url=database_url, TFT_SNAPSHOT_PATH=str(missing))
    for path in ("/api/source", "/api/health", "/api/champions"):
        response = client.get(path)
        assert response.status_code == 503, (mode, path)
        assert response.json()["demo"] is False and response.json()["error"] == error
        assert "x:x" not in response.text and str(tmp_path) not in response.text
    assert not missing.exists() and not (tmp_path / "web-demo.sqlite3").exists()  # nothing created, no demo built


def test_empty_snapshot_is_unavailable_not_demo(monkeypatch, tmp_path) -> None:
    path = tmp_path / "empty.sqlite3"
    Database(path).close()
    client = _client(monkeypatch, tmp_path, mode="snapshot", TFT_SNAPSHOT_PATH=str(path))
    response = client.get("/api/source")
    assert response.status_code == 503 and response.json()["source_mode"] == "snapshot"


# ---------------------------------------------------------------- compliance pages, boilerplate, navigation


def test_about_page_explains_the_product_and_what_it_is_not(monkeypatch, tmp_path) -> None:
    text = _client(monkeypatch, tmp_path).get("/about").text
    for phrase in ("aggregate Teamfight Tactics analytics and theorycrafting tool", "Discover", "Champion Investigation",
                   "Experiments", "historical ranked", "Observed", "Theorycrafted", "not a live gameplay assistant",
                   "does not track or scout opponents"):
        assert phrase in text, phrase


def test_methodology_page_covers_data_carries_samples_and_limits(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    text = client.get("/methodology").text
    assert client.get("/data").text == text
    for phrase in ("balance window", "at least two completed", "boards", "Sample sizes", "shrunk", "Limitations",
                   "Association, not causation", "Freshness", "demo mode", "Inferred", "Theorycrafted"):
        assert phrase in text, phrase


def test_privacy_page_matches_what_the_site_actually_does(monkeypatch, tmp_path) -> None:
    text = _client(monkeypatch, tmp_path).get("/privacy").text
    for phrase in ("No accounts", "No cookies", "No analytics, tracking or advertising", "Riot Games API",
                   "PUUIDs", "hosting provider", "Last updated"):
        assert phrase in text, phrase
    static = "\n".join(p.read_text() for p in (WEB / "static").glob("*.js"))
    for api in ("document.cookie", "localStorage", "sessionStorage", "indexedDB", "navigator.sendBeacon"):
        assert api not in static, api  # the policy's claims hold for the shipped scripts
    for page in HTML_FILES:  # no third-party scripts, styles or fonts
        assert not re.search(r'<(script|link)[^>]+(src|href)="https?://', page.read_text()), page.name


def test_terms_page(monkeypatch, tmp_path) -> None:
    text = _client(monkeypatch, tmp_path).get("/terms").text
    for phrase in ("Terms of Service", "non-commercial", "as is", "not a live in-game tool", "Riot Games", "Privacy Policy"):
        assert phrase in text, phrase
    assert "LLC" not in text and "Inc." not in text.replace("Riot Games, Inc.", "")  # no invented legal entity


@pytest.mark.parametrize("path", PAGES)
def test_every_page_has_the_riot_boilerplate_and_consistent_navigation(monkeypatch, tmp_path, path) -> None:
    text = _client(monkeypatch, tmp_path).get(path).text
    assert RIOT_BOILERPLATE in text
    main = re.search(r'<nav class="cover-nav" aria-label="Main">(.*?)</nav>', text, re.S)
    site = re.search(r'<nav class="footer-nav" aria-label="Site">(.*?)</nav>', text, re.S)
    assert main and site, path
    assert re.findall(r'href="([^"]+)"', main.group(1)) == MAIN_NAV
    assert re.findall(r'href="([^"]+)"', site.group(1)) == FOOTER_NAV
    assert '<meta name="viewport" content="width=device-width, initial-scale=1" />' in text
    assert 'class="skip-link"' in text and '<html lang="en">' in text
    assert text.count('aria-current="page"') <= 1


def test_public_positioning_is_static_study_not_live_assistance() -> None:
    home = (WEB / "index.html").read_text()
    assert "aggregate TFT analytics and theorycrafting tool" in home and "not a live in-game assistant" in home
    pages = " ".join(p.read_text().lower() for p in HTML_FILES)
    for claim in ("scouting notebook", "real-time recommendation", "live recommendation", "overlay that", "live riot data"):
        assert claim not in pages, claim
    for script in (WEB / "static").glob("*.js"):
        assert "live Riot data" not in script.read_text(), script.name


# ---------------------------------------------------------------- /riot.txt and secrets


def test_riot_txt_returns_exactly_the_configured_string(monkeypatch, tmp_path) -> None:
    token = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    client = _client(monkeypatch, tmp_path, RIOT_SITE_VERIFICATION=f"  {token}\n")
    response = client.get("/riot.txt")
    assert response.status_code == 200
    assert response.content == token.encode()  # nothing before or after, no HTML
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"


def test_riot_txt_without_a_configured_string_exposes_no_token(monkeypatch, tmp_path) -> None:
    response = _client(monkeypatch, tmp_path).get("/riot.txt")
    assert response.status_code == 404 and response.content == b"Not Found"
    assert response.headers["content-type"].startswith("text/plain")


def test_riot_txt_never_publishes_a_riot_api_key(monkeypatch, tmp_path) -> None:
    for value, api_key in (("RGAPI-12345678-aaaa-bbbb-cccc-1234567890ab", None), ("same-secret", "same-secret")):
        env = {"RIOT_SITE_VERIFICATION": value, **({"RIOT_API_KEY": api_key} if api_key else {})}
        response = _client(monkeypatch, tmp_path, **env).get("/riot.txt")
        assert response.status_code == 404 and value.encode() not in response.content


def test_riot_api_key_never_reaches_the_browser(monkeypatch, tmp_path) -> None:
    sentinel = "RGAPI-sentinel-0000-never-in-a-page"
    client = _client(monkeypatch, tmp_path, RIOT_API_KEY=sentinel)
    paths = [*PAGES, "/api/source", "/api/health", "/api/discovery", "/api/champions", "/api/champions/khazix",
             "/api/experiments", "/api/balance-windows", "/riot.txt",
             *(f"/static/{p.name}" for p in (WEB / "static").glob("*.*"))]
    for path in paths:
        assert sentinel not in client.get(path).text, path
    for p in WEB.rglob("*"):
        if p.is_file() and p.suffix in (".html", ".js", ".css"):
            assert "RIOT_API_KEY" not in p.read_text(), p.name
