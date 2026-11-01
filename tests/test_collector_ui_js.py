"""Runs the Collector dashboard's view-logic tests (tests/js/collector_view.test.js)
with Node's built-in test runner. No npm packages or browser; skipped when Node
is not installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js" / "collector_view.test.js"


def test_collector_dashboard_view_logic() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    proc = subprocess.run([node, "--test", "--test-reporter=tap", str(JS_TESTS)], capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "# fail 0" in proc.stdout
