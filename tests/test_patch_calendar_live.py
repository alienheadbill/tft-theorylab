"""Live smoke test against Riot's real TFT patch schedule page.

Skipped by default: CI must never depend on Riot Support being reachable or
unchanged. Opt in with `TFTLAB_LIVE_PATCH_SCHEDULE_TEST=1` from a machine
with network access (or run `tftlab local-patch-schedule --refresh`). It
makes one HTTPS request and writes only to a temporary folder.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import tftlab.patch_calendar as pc

pytestmark = pytest.mark.skipif(os.environ.get("TFTLAB_LIVE_PATCH_SCHEDULE_TEST") != "1",
                                reason="live Riot Support request; set TFTLAB_LIVE_PATCH_SCHEDULE_TEST=1")


def test_the_live_page_still_parses(tmp_path: Path) -> None:
    now = int(time.time() * 1000)
    result = pc.refresh_schedule(tmp_path, now_ms=now, force=True, http_get=pc.fetch_schedule_page)
    assert result.ok, result.message
    schedule = pc.load_cache(tmp_path)
    assert schedule is not None and len(schedule.events) >= 3
    st = pc.calendar_state(tmp_path, now_ms=now)
    assert st.schedule is not None and st.code
