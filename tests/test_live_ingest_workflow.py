import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

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


def test_ingest_is_sized_by_inputs_and_never_degraded() -> None:
    """The ingest command takes its sizes from the validated inputs (via
    env vars), and never falls back to unauthoritative rarity+1 costs."""
    text = _text()
    ingest_line = next(line for line in text.splitlines() if "tftlab ingest-riot" in line)
    assert '--players "${PLAYERS}"' in ingest_line
    assert '--matches-per-player "${MATCHES_PER_PLAYER}"' in ingest_line
    assert '--sampling "${SAMPLING}"' in ingest_line
    assert "--current-trusted-window" in ingest_line  # production never crawls unbounded history
    assert "--start-time" not in ingest_line
    commands = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("--allow-degraded-costs" in line for line in commands)
    assert "SAMPLING=high_elo" in text and "SAMPLING=challenger" in text


def test_inputs_have_conservative_defaults() -> None:
    """Defaults reproduce the original 10 x 5 Challenger-only run."""
    text = _text()
    inputs = text[text.index("inputs:") : text.index("permissions:")]
    for name, kind, default in (
        ("players", "number", "10"),
        ("matches_per_player", "number", "5"),
        ("expanded_high_elo", "boolean", "false"),
    ):
        block = inputs[inputs.index(f"{name}:") :]
        assert f"type: {kind}" in block.split("default:")[0]
        assert block.split("default:", 1)[1].split("\n", 1)[0].strip() == default


def test_inputs_reach_shell_only_through_env_vars() -> None:
    """Never `${{ inputs.x }}` inside a script body (script injection);
    only as `NAME: ${{ inputs.x }}` environment entries."""
    for line in _text().splitlines():
        if "${{ inputs." in line:
            assert re.match(r"^\s+[A-Z_]+: \$\{\{ inputs\.[a-z_]+ \}\}$", line), line


def _preflight_script() -> str:
    text = _text()
    step = text[text.index("Validate production configuration") : text.index("- uses: actions/checkout")]
    return textwrap.dedent(step.split("run: |\n", 1)[1])


def _run_preflight(**env: str) -> subprocess.CompletedProcess:
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_REF": "refs/heads/main",
        "RIOT_API_KEY": "fake-riot-key-value",
        "DATABASE_URL": "postgresql://user:fake-password@example.invalid/db",
        "PLAYERS": "10",
        "MATCHES_PER_PLAYER": "5",
        "EXPANDED_HIGH_ELO": "false",
    }
    return subprocess.run(
        ["bash", "-e", "-c", _preflight_script()], env={**base, **env}, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "env",
    [
        {"PLAYERS": "50", "MATCHES_PER_PLAYER": "5", "EXPANDED_HIGH_ELO": "true"},
        {"PLAYERS": "1", "MATCHES_PER_PLAYER": "1"},
        {"PLAYERS": "100", "MATCHES_PER_PLAYER": "10"},
        {},
    ],
)
def test_preflight_accepts_in_bounds_inputs(env: dict) -> None:
    result = _run_preflight(**env)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "env",
    [
        {"PLAYERS": "0"},
        {"PLAYERS": "101"},
        {"PLAYERS": "-5"},
        {"PLAYERS": "12.5"},
        {"PLAYERS": ""},
        {"PLAYERS": "50; echo hacked"},
        {"MATCHES_PER_PLAYER": "0"},
        {"MATCHES_PER_PLAYER": "11"},
        {"MATCHES_PER_PLAYER": "100"},  # the CLI allows 100; production does not
        {"MATCHES_PER_PLAYER": "abc"},
        {"EXPANDED_HIGH_ELO": "yes"},
        {"EXPANDED_HIGH_ELO": ""},
        {"GITHUB_REF": "refs/heads/claude/some-feature"},
        {"RIOT_API_KEY": ""},
        {"DATABASE_URL": ""},
        {"DATABASE_URL": "sqlite:///tmp/x.db"},
    ],
)
def test_preflight_rejects_bad_inputs_without_echoing_secrets(env: dict) -> None:
    result = _run_preflight(**env)
    assert result.returncode == 1
    assert "hacked" not in result.stdout
    for secret in ("fake-riot-key-value", "fake-password"):
        assert secret not in result.stdout + result.stderr


def test_preflight_validates_inputs_before_checkout() -> None:
    text = _text()
    preflight = text[text.index("Validate production configuration") : text.index("actions/checkout")]
    for needle in ("PLAYERS", "MATCHES_PER_PLAYER", "EXPANDED_HIGH_ELO", "-gt 100", "-gt 10"):
        assert needle in preflight


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
    for later_step in ("actions/checkout", "Verify Riot API", "Ingest live sample"):
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
    minutes = int(re.search(r"timeout-minutes: (\d+)", text).group(1))
    assert 30 <= minutes <= 60
