"""Runs the Champion Investigation page-load tests (tests/js/champion_load.test.js)
with Node's built-in test runner. The API bodies the page receives come from the
demo app itself, so the page script is exercised against the real response
shapes. No npm packages or browser; skipped when Node is not installed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

JS_TESTS = Path(__file__).parent / "js" / "champion_load.test.js"


def test_champion_page_load_behaviour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TFT_DB_PATH", str(tmp_path / "nonexistent.sqlite3"))
    monkeypatch.setenv("TFT_DEMO_DB_PATH", str(tmp_path / "demo.sqlite3"))
    from tftlab.webapp import create_app

    client = TestClient(create_app())
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    windows = client.get("/api/balance-windows").json()
    # The demo data has one balance window; the window-change tests need a second
    # one to switch to (the detail fixture is served for any window).
    extra = dict(windows["windows"][0], balance_window="0.0-test")
    windows["windows"].append(extra)
    bodies = {
        "balance_windows": windows,
        "champions": client.get("/api/champions").json(),
        "khazix": client.get("/api/champions/khazix", params={"top_n": 6}).json(),
    }
    assert bodies["khazix"]["carry"] is not None
    for name, body in bodies.items():
        (fixtures / f"{name}.json").write_text(json.dumps(body))
    proc = subprocess.run([node, "--test", "--test-reporter=tap", str(JS_TESTS)], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "CHAMPION_FIXTURES": str(fixtures)})
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]
    assert "# fail 0" in proc.stdout
