from pathlib import Path

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "live-ingest.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_only_manually_triggered() -> None:
    """This workflow pulls real Riot data into production and must never
    run on its own -- no push/pull_request CI trigger, and no schedule
    yet (that's an explicit follow-up milestone, not this one)."""
    text = _text()
    assert "workflow_dispatch" in text
    for trigger in ("\npush:", "\n  push:", "pull_request:", "schedule:"):
        assert trigger not in text, f"unexpected trigger {trigger!r} in live-ingest.yml"


def test_runs_the_four_cli_steps_in_order() -> None:
    text = _text()
    commands = ["tftlab verify-riot", "tftlab ingest-riot", "tftlab validate-live-data", "tftlab discovery-smoke"]
    positions = [text.index(cmd) for cmd in commands]
    assert positions == sorted(positions), "CLI steps must run in the documented order"


def test_first_ingest_is_intentionally_small_and_not_degraded() -> None:
    """Locks in the safety limits for this first controlled run: Challenger
    only via the ingest-riot defaults, 10 seed players, 5 matches/player,
    and never falling back to unauthoritative rarity+1 costs."""
    ingest_line = next(line for line in _text().splitlines() if "tftlab ingest-riot" in line)
    assert "--players 10" in ingest_line
    assert "--matches-per-player 5" in ingest_line
    assert "--allow-degraded-costs" not in ingest_line


def test_never_echoes_secrets() -> None:
    text = _text()
    assert "echo" not in text.lower()


def test_secrets_only_referenced_via_expression_not_hardcoded() -> None:
    text = _text()
    assert "${{ secrets.RIOT_API_KEY }}" in text
    assert "${{ secrets.DATABASE_URL }}" in text
