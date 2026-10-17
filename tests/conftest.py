"""Suite-wide fixtures."""

from __future__ import annotations

import pytest

import tftlab.riot as riot


class FakeClock:
    """Monotonic clock whose `sleep` advances time instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def _no_real_riot_sleeping(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """RiotClient paces and backs off with real sleeps in production; in
    tests a fake clock stands in, so no test ever waits on pacing."""
    clock = FakeClock()
    monkeypatch.setattr(riot, "_monotonic", clock)
    monkeypatch.setattr(riot, "_sleep", clock.sleep)
    return clock


@pytest.fixture(autouse=True)
def _no_real_patch_schedule_requests(monkeypatch: pytest.MonkeyPatch):
    """CI never depends on Riot Support: the patch calendar's only network
    call fails fast in every test (tests pass their own fixture pages), and
    windows a test froze/installed never leak into the next test."""
    import tftlab.patch_calendar as patch_calendar
    import tftlab.unreal_patch as unreal_patch

    def _blocked(url: str):
        raise OSError(f"network disabled in tests: {url}")

    monkeypatch.setattr(patch_calendar, "_http_get", _blocked)
    yield
    unreal_patch.install_frozen_windows(())
