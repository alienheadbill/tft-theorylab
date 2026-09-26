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


COHORT_INPUTS = ("challenger_seeds", "grandmaster_seeds", "master_seeds", "diamond_seeds", "platinum_seeds")


def test_ingest_is_sized_by_per_cohort_inputs_and_never_degraded() -> None:
    """The ingest command takes one seed count per cohort (via env vars)
    plus the history depth, and never falls back to rarity+1 costs."""
    text = _text()
    ingest_line = next(line for line in text.splitlines() if "tftlab ingest-riot" in line)
    for cohort in ("challenger", "grandmaster", "master", "diamond", "platinum"):
        assert f'--{cohort}-seeds "${{{cohort.upper()}_SEEDS}}"' in ingest_line
    assert '--matches-per-player "${MATCHES_PER_PLAYER}"' in ingest_line
    assert "--current-trusted-window" in ingest_line  # production never crawls unbounded history
    assert "--start-time" not in ingest_line
    for legacy in ("--players", "--sampling", "--include-master"):
        assert legacy not in ingest_line  # no legacy weighted mode, no combined cohort
    commands = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("--allow-degraded-costs" in line for line in commands)


def test_no_combined_elite_cohort_anywhere() -> None:
    lowered = _text().lower()
    assert "elite" not in lowered
    assert "expanded_high_elo" not in lowered


def test_inputs_have_conservative_defaults() -> None:
    """Defaults reproduce the original 10 x 5 Challenger-only run; every
    cohort is its own visible input."""
    text = _text()
    inputs = text[text.index("inputs:") : text.index("permissions:")]
    for name, kind, default in (
        ("challenger_seeds", "number", "10"),
        ("grandmaster_seeds", "number", "0"),
        ("master_seeds", "number", "0"),
        ("diamond_seeds", "number", "0"),
        ("platinum_seeds", "number", "0"),
        ("matches_per_player", "number", "5"),
    ):
        block = inputs[inputs.index(f"      {name}:") :]
        assert f"type: {kind}" in block.split("default:")[0]
        assert block.split("default:", 1)[1].split("\n", 1)[0].strip() == default
    assert "players:" not in inputs.replace("_seeds:", "").replace("matches_per_player:", "")


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
        "CHALLENGER_SEEDS": "10",
        "GRANDMASTER_SEEDS": "0",
        "MASTER_SEEDS": "0",
        "DIAMOND_SEEDS": "0",
        "PLATINUM_SEEDS": "0",
        "MATCHES_PER_PLAYER": "5",
    }
    return subprocess.run(
        ["bash", "-e", "-c", _preflight_script()], env={**base, **env}, capture_output=True, text=True
    )


_TWENTY_EACH = {f"{c.upper()}": "20" for c in COHORT_INPUTS}


@pytest.mark.parametrize(
    "env",
    [
        _TWENTY_EACH,  # 20 per cohort = 100 total
        {"CHALLENGER_SEEDS": "0", "DIAMOND_SEEDS": "1", "MATCHES_PER_PLAYER": "1"},
        {"CHALLENGER_SEEDS": "100", "MATCHES_PER_PLAYER": "10"},
        {"CHALLENGER_SEEDS": "0", "PLATINUM_SEEDS": "100"},
        {"CHALLENGER_SEEDS": "0", "GRANDMASTER_SEEDS": "30", "MASTER_SEEDS": "30"},
        {},
    ],
)
def test_preflight_accepts_in_bounds_inputs(env: dict) -> None:
    result = _run_preflight(**env)
    assert result.returncode == 0, result.stderr


def test_preflight_prints_every_cohort_and_the_total() -> None:
    result = _run_preflight(**_TWENTY_EACH)
    assert (
        "Planned seed cohorts: challenger=20 grandmaster=20 master=20 diamond=20 platinum=20 (total 100)"
        in result.stdout
    )


@pytest.mark.parametrize(
    "env",
    [
        {"CHALLENGER_SEEDS": "0"},  # total 0
        {**_TWENTY_EACH, "PLATINUM_SEEDS": "21"},  # total 101
        {"CHALLENGER_SEEDS": "101"},
        {"DIAMOND_SEEDS": "-5"},
        {"MASTER_SEEDS": "12.5"},
        {"GRANDMASTER_SEEDS": ""},
        {"PLATINUM_SEEDS": "5; echo hacked"},
        {"DIAMOND_SEEDS": "010"},  # leading zero: would be octal in shell arithmetic
        {"DIAMOND_SEEDS": "99999999999999999999"},
        {"MATCHES_PER_PLAYER": "0"},
        {"MATCHES_PER_PLAYER": "11"},
        {"MATCHES_PER_PLAYER": "100"},  # the CLI allows 100; production does not
        {"MATCHES_PER_PLAYER": "abc"},
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
    for needle in (*(c.upper() for c in COHORT_INPUTS), "TOTAL_SEEDS", "MATCHES_PER_PLAYER", "-gt 100", "-gt 10"):
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
