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
