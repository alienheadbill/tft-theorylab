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


def test_never_prints_secret_values() -> None:
    """Static diagnostic messages (e.g. `echo "::error::..."`) are fine and
    expected -- what must never happen is a secret's *value* ending up in a
    printed string via shell variable expansion."""
    for line in _text().splitlines():
        lowered = line.lower()
        if "echo" in lowered or "printf" in lowered:
            for var in ("RIOT_API_KEY", "DATABASE_URL"):
                assert f"${var}" not in line, f"line prints secret variable {var}: {line!r}"
                assert f"${{{var}}}" not in line, f"line prints secret variable {var}: {line!r}"


def test_secrets_only_referenced_via_expression_not_hardcoded() -> None:
    text = _text()
    assert "${{ secrets.RIOT_API_KEY }}" in text
    assert "${{ secrets.DATABASE_URL }}" in text


def test_production_preflight_runs_before_any_riot_or_database_step() -> None:
    """The preflight must gate everything else: it needs to run (and fail,
    if it's going to) before checkout/install/Riot verification/ingestion,
    not merely somewhere in the file."""
    text = _text()
    preflight_pos = text.index("Validate production configuration")
    for later_step in ("actions/checkout", "Verify Riot API", "Ingest first live sample"):
        assert preflight_pos < text.index(later_step)


def test_preflight_rejects_missing_or_malformed_database_url() -> None:
    """The CLI itself falls back to a local SQLite file when DATABASE_URL
    is unset -- fine for local dev, never acceptable for this production
    workflow. The preflight must explicitly reject an empty DATABASE_URL
    (not just let a later step discover the problem) and one that isn't a
    postgres:// or postgresql:// URL."""
    text = _text()
    assert '-z "${DATABASE_URL}"' in text
    assert '-z "${RIOT_API_KEY}"' in text
    assert "postgres://*|postgresql://*" in text
    # Every one of the preflight's checks must actually stop the job.
    preflight = text[text.index("Validate production configuration") : text.index("actions/checkout")]
    assert preflight.count("exit 1") >= 3


def test_preflight_hard_requires_main_branch() -> None:
    """The repo's GitHub default branch is not `main`, so the workflow UI's
    default selection can't be trusted -- this must be an explicit,
    enforced check, not a README instruction alone."""
    text = _text()
    assert "GITHUB_REF" in text
    assert "refs/heads/main" in text
    preflight_pos = text.index("Validate production configuration")
    branch_check_pos = text.index("refs/heads/main")
    assert preflight_pos < branch_check_pos < text.index("actions/checkout")


def test_concurrency_serializes_production_ingests() -> None:
    """Two manually-triggered runs must never write to production at once.
    Serialization (queue), not cancellation of an in-flight ingest."""
    text = _text()
    assert "concurrency:" in text
    assert "group: live-ingest-production" in text
    assert "cancel-in-progress: false" in text


def test_permissions_are_read_only() -> None:
    text = _text()
    assert "permissions:" in text
    assert "contents: read" in text
    assert "contents: write" not in text


def test_job_has_a_bounded_timeout() -> None:
    """A hung network call or exhausted rate-limit retry loop must not be
    able to leave this production workflow running indefinitely."""
    text = _text()
    assert "timeout-minutes:" in text
