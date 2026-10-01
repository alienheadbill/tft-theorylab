import json
import os
import re
import sys
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "live-ingest.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_triggers_are_schedule_manual_and_exact_owner_ops_smoke_only() -> None:
    """Production ingestion is schedule/manual plus one narrow owner-only
    Ops Control command; never push/PR/general dispatch or arbitrary comments."""
    text = _text()
    on = text[text.index("\non:\n") : text.index("\npermissions:")]
    assert re.findall(r"^  ([a-z_]+):", on, flags=re.M) == [
        "schedule",
        "workflow_dispatch",
        "issue_comment",
    ]
    for trigger in ("\npush:", "\n  push:", "pull_request:", "workflow_run:", "repository_dispatch:"):
        assert trigger not in text, f"unexpected trigger {trigger!r} in live-ingest.yml"

    job = text[text.index("jobs:") : text.index("    concurrency:")]
    assert "github.event.issue.number == 38" in job
    assert "github.event.comment.user.login == 'alienheadbill'" in job
    assert "github.event.comment.body == '/ingest smoke'" in job


def test_schedule_is_every_six_hours_off_the_hour() -> None:
    """Four conservative collection opportunities a day (UTC), at minute 41
    rather than :00, where GitHub delays or drops scheduled runs."""
    crons = re.findall(r'^\s+- cron: "([^"]+)"', _text(), flags=re.M)
    assert crons == ["41 */6 * * *"]


#: What a scheduled run uses (the proven bounded configuration of production
#: runs 36286242686 / 36289198154), keyed by the job env var it sets.
RUN_SETTINGS = {
    # env var: (workflow_dispatch input, scheduled value, exact Ops smoke value)
    "CHALLENGER_SEEDS": ("challenger_seeds", "15", "3"),
    "GRANDMASTER_SEEDS": ("grandmaster_seeds", "15", "0"),
    "MASTER_SEEDS": ("master_seeds", "20", "0"),
    "DIAMOND_SEEDS": ("diamond_seeds", "25", "0"),
    "PLATINUM_SEEDS": ("platinum_seeds", "25", "0"),
    "MATCHES_PER_PLAYER": ("matches_per_player", "10", "3"),
    "COLLECTION_MODE": ("collection_mode", "bounded", "bounded"),
    "MAX_DURATION_MINUTES": ("max_duration_minutes", "60", "60"),
    "MAX_REQUESTS": ("max_requests", "3000", "3000"),
    "MAX_MATCH_FETCHES": ("max_match_fetches", "2000", "2000"),
}


def test_schedule_and_owner_smoke_use_fixed_settings_manual_uses_inputs() -> None:
    """Each setting is resolved once at job level: fixed values for schedule,
    a tiny fixed bounded configuration for the guarded issue-comment smoke,
    and the operator's inputs for workflow_dispatch."""
    text = _text()
    job = text[text.index("jobs:") : text.index("    steps:")]
    assert "      RUN_TRIGGER: ${{ github.event_name }}" in job
    for var, (input_name, scheduled, smoke) in RUN_SETTINGS.items():
        line = (
            f"      {var}: ${{{{ github.event_name == 'schedule' && '{scheduled}' || "
            f"github.event_name == 'issue_comment' && '{smoke}' || inputs.{input_name} }}}}"
        )
        assert line in job, var
        assert scheduled != ""
        assert smoke != ""
    assert RUN_SETTINGS["COLLECTION_MODE"][1:] == ("bounded", "bounded")
    steps = text[text.index("    steps:") :]
    assert "${{ inputs." not in steps  # every shell step reads resolved job env only


def test_runs_the_four_cli_steps_in_order() -> None:
    text = _text()
    commands = ["tftlab verify-riot", "tftlab ingest-riot", "tftlab validate-live-data", "tftlab discovery-smoke"]
    positions = [text.index(cmd) for cmd in commands]
    assert positions == sorted(positions), "CLI steps must run in the documented order"


def test_prepares_discovery_after_the_ingest_is_validated() -> None:
    """The website's prepared Discovery is refreshed right after new matches
    are stored and validated, with the production database secret only."""
    text = _text()
    step = text[text.index("- name: Prepare Discovery analytics") : text.index("- name: Summarize collection")]
    assert text.index("tftlab discovery-smoke") < text.index("tftlab prepare-discovery")
    assert "run: tftlab prepare-discovery" in step and "--force" not in step
    assert "DATABASE_URL: ${{ secrets.NEON_DATABASE_URL }}" in step
    assert "RIOT_API_KEY" not in step and "id: prepare" in step


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


def test_inputs_reach_shell_only_through_resolved_env_vars() -> None:
    """Dispatch inputs may appear only in the job's resolver expressions,
    never directly inside a shell script."""
    for line in _text().splitlines():
        if "inputs." in line and "${{" in line:
            if line.strip().startswith("timeout-minutes:"):  # job setting, never a shell
                continue
            assert re.match(
                r"^\s+[A-Z_]+: \$\{\{ github\.event_name == 'schedule' && '[a-z0-9]+' "
                r"\|\| github\.event_name == 'issue_comment' && '[a-z0-9]+' "
                r"\|\| inputs\.[a-z_]+ \}\}$",
                line,
            ), line


def _preflight_script() -> str:
    text = _text()
    step = text[text.index("Validate production configuration") : text.index("- uses: actions/checkout")]
    return textwrap.dedent(step.split("run: |\n", 1)[1])


def _run_preflight(**env: str) -> subprocess.CompletedProcess:
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_REF": "refs/heads/main",
        "RUN_TRIGGER": "workflow_dispatch",
        "RIOT_API_KEY": "fake-riot-key-value",
        "DATABASE_URL": "postgresql://user:fake-password@example.invalid/db",
        "CHALLENGER_SEEDS": "10",
        "GRANDMASTER_SEEDS": "0",
        "MASTER_SEEDS": "0",
        "DIAMOND_SEEDS": "0",
        "PLATINUM_SEEDS": "0",
        "MATCHES_PER_PLAYER": "5",
        "COLLECTION_MODE": "bounded",
        "MAX_DURATION_MINUTES": "60",
        "MAX_REQUESTS": "3000",
        "MAX_MATCH_FETCHES": "2000",
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
    assert "Collection mode: bounded." in result.stdout
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
        {"RUN_TRIGGER": "push"},
        {"RUN_TRIGGER": "pull_request"},
        {"RUN_TRIGGER": ""},
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
    assert "${{ secrets.NEON_DATABASE_URL }}" in text
    assert "${{ secrets.DATABASE_URL }}" not in text


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
    able to leave this production workflow running indefinitely: 60
    minutes for bounded runs, 240 for maximum runs (whose ingest stops
    itself at max_duration_minutes <= 200)."""
    text = _text()
    line = next(l for l in text.splitlines() if "timeout-minutes:" in l)
    assert line.strip() == "timeout-minutes: ${{ inputs.collection_mode == 'maximum' && 240 || 60 }}"
    assert "check_range max_duration_minutes \"${MAX_DURATION_MINUTES}\" 1 200" in text


def _ingest_commands() -> list[str]:
    return [line.strip() for line in _text().splitlines() if line.strip().startswith("tftlab ingest-riot")]


def test_maximum_mode_is_opt_in_with_explicit_budget_inputs() -> None:
    text = _text()
    inputs = text[text.index("inputs:") : text.index("permissions:")]
    mode = inputs[inputs.index("      collection_mode:") :]
    assert "type: choice" in mode.split("default:")[0]
    assert "- bounded" in mode and "- maximum" in mode
    assert mode.split("default:", 1)[1].split("\n", 1)[0].strip() == "bounded"
    for name, default in (("max_duration_minutes", "60"), ("max_requests", "3000"), ("max_match_fetches", "2000")):
        block = inputs[inputs.index(f"      {name}:") :]
        assert "type: number" in block.split("default:")[0]
        assert block.split("default:", 1)[1].split("\n", 1)[0].strip() == default
    # Stay within the 10-input workflow_dispatch limit GitHub long enforced.
    assert len(re.findall(r"^      [a-z_]+:$", inputs, flags=re.M)) <= 10


def test_both_modes_are_bounded_rate_capped_and_emit_telemetry() -> None:
    bounded, maximum = _ingest_commands()
    assert "--collection-mode" not in bounded
    for command in (bounded, maximum):
        assert "--current-trusted-window" in command
        assert '--rate-ceiling "${RIOT_RATE_CEILING}"' in command
        assert "--telemetry-out ingest-telemetry/telemetry.json" in command
        assert "--allow-degraded-costs" not in command and "--start-time" not in command
    assert "--collection-mode maximum" in maximum
    for flag, var in (("--max-duration-minutes", "MAX_DURATION_MINUTES"), ("--max-requests", "MAX_REQUESTS"),
                      ("--max-match-fetches", "MAX_MATCH_FETCHES")):
        assert f'{flag} "${{{var}}}"' in maximum
    assert "-seeds" not in maximum and "--matches-per-player" not in maximum
    text = _text()
    assert 'RIOT_RATE_CEILING: "10:10"' in text
    assert "rate_ceiling:" not in text  # a reviewed code change, never a dispatch-time knob
    upload = text[text.index("Upload collection telemetry") :]
    assert "if: always()" in upload.split("- name:")[0]
    assert "if-no-files-found: ignore" in upload


def test_never_launches_or_retries_runs() -> None:
    text = _text()
    for forbidden in ("gh workflow", "workflow_run", "repository_dispatch", "createWorkflowDispatch",
                      "continue-on-error", "retry"):
        commands = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        assert forbidden not in commands, forbidden


@pytest.mark.parametrize(
    "env",
    [
        {"COLLECTION_MODE": "maximum"},
        {"COLLECTION_MODE": "maximum", "MAX_DURATION_MINUTES": "200", "MAX_REQUESTS": "12000", "MAX_MATCH_FETCHES": "12000"},
        {"COLLECTION_MODE": "maximum", "MAX_DURATION_MINUTES": "1", "MAX_REQUESTS": "1", "MAX_MATCH_FETCHES": "0"},
    ],
)
def test_preflight_accepts_maximum_mode_within_budget_bounds(env: dict) -> None:
    result = _run_preflight(**env)
    assert result.returncode == 0, result.stderr
    assert "Collection mode: maximum" in result.stdout
    assert "ignored in maximum mode" in result.stdout


@pytest.mark.parametrize(
    "env",
    [
        {"COLLECTION_MODE": "huge"},
        {"COLLECTION_MODE": ""},
        {"MAX_DURATION_MINUTES": "0"},
        {"MAX_DURATION_MINUTES": "201"},
        {"MAX_DURATION_MINUTES": "90.5"},
        {"MAX_REQUESTS": "0"},
        {"MAX_REQUESTS": "12001"},
        {"MAX_REQUESTS": "999999"},
        {"MAX_REQUESTS": "0100"},
        {"MAX_MATCH_FETCHES": "-1"},
        {"MAX_MATCH_FETCHES": "12001"},
        {"MAX_MATCH_FETCHES": "5; echo hacked"},
    ],
)
def test_preflight_rejects_bad_mode_or_budgets(env: dict) -> None:
    result = _run_preflight(**env)
    assert result.returncode == 1
    assert "hacked" not in result.stdout


def test_preflight_accepts_the_scheduled_settings_and_says_so() -> None:
    scheduled = {var: value for var, (_, value, _) in RUN_SETTINGS.items()}
    result = _run_preflight(RUN_TRIGGER="schedule", **scheduled)
    assert result.returncode == 0, result.stderr
    assert "Trigger: scheduled run" in result.stdout
    assert "Collection mode: bounded." in result.stdout
    assert (
        "Planned seed cohorts: challenger=15 grandmaster=15 master=20 diamond=25 platinum=25 (total 100)"
        in result.stdout
    )
    assert "Planned histories: 10 matches per seed player." in result.stdout


def test_preflight_accepts_the_fixed_ops_smoke_settings_and_says_so() -> None:
    smoke = {var: value for var, (_, _, value) in RUN_SETTINGS.items()}
    result = _run_preflight(RUN_TRIGGER="issue_comment", **smoke)
    assert result.returncode == 0, result.stderr
    assert "Trigger: owner-only Ops Control smoke" in result.stdout
    assert "Collection mode: bounded." in result.stdout
    assert (
        "Planned seed cohorts: challenger=3 grandmaster=0 master=0 diamond=0 platinum=0 (total 3)"
        in result.stdout
    )
    assert "Planned histories: 3 matches per seed player." in result.stdout


def test_preflight_labels_manual_runs() -> None:
    result = _run_preflight()
    assert result.returncode == 0, result.stderr
    assert "Trigger: manual dispatch" in result.stdout


def _summary_script() -> str:
    text = _text()
    step = text[text.index("- name: Summarize collection") :]
    body = textwrap.dedent(step.split("run: |\n", 1)[1])
    after_heredoc_line = body.split("<<'PY'", 1)[1].split("\n", 1)[1]
    return after_heredoc_line.split("\nPY\n", 1)[0]


def _run_summary(tmp_path: Path, telemetry: dict | None, **env: str) -> subprocess.CompletedProcess:
    if telemetry is not None:
        (tmp_path / "ingest-telemetry").mkdir()
        (tmp_path / "ingest-telemetry" / "telemetry.json").write_text(json.dumps(telemetry))
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RUN_TRIGGER": "schedule", "COLLECTION_MODE": "bounded"}
    return subprocess.run(
        [sys.executable, "-c", _summary_script()], env={**base, **env}, cwd=tmp_path, capture_output=True, text=True
    )


def test_summary_reports_run_level_collection_metrics(tmp_path: Path) -> None:
    telemetry = {
        "mode": "bounded", "run_id": "gh-1-1", "outcome": "completed",
        "collection": {"matches_inserted": 433, "duplicates_skipped": 121, "matches_fetched": 478,
                       "non_target_matches_skipped": 45, "failed_requests": 0, "seed_players": 100,
                       "requested_seeds": 100, "failed_history_requests": 0, "seeds_with_empty_history": 20,
                       "seed_ledger_rows": 100, "discovery_rows": 586},
        "riot": {"requests": 605, "by_status": {"200": 605}, "rate_limited": 0, "elapsed_s": 933.7},
    }
    result = _run_summary(tmp_path, telemetry, VALIDATE_OUTCOME="success", SMOKE_OUTCOME="success",
                          PREPARE_OUTCOME="success")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "Trigger: `schedule`; mode: `bounded`" in out
    assert "Outcome: `completed`; run id: `gh-1-1`" in out
    for row in ("| Matches inserted (new) | 433 |", "| Already stored, skipped before fetch | 121 |",
                "| Match-detail fetches | 478 |", "| Seeds (sampled / requested) | 100 / 100 |",
                "| Seed ledger rows finalized | 100 |", "| Riot requests (total) | 605 |",
                "| Riot 429s | 0 |", "| Ingest elapsed (s) | 933.7 |"):
        assert row in out, row
    assert "Validate ingested data: `success`; discovery smoke: `success`; prepared Discovery: `success`" in out


def test_summary_of_a_failed_ingest_shows_the_outcome_without_collection_rows(tmp_path: Path) -> None:
    telemetry = {"mode": "bounded", "run_id": "gh-2-1", "outcome": "failed (RiotApiError)",
                 "riot": {"requests": 40, "by_status": {"200": 39, "401": 1}, "rate_limited": 0, "elapsed_s": 50.0}}
    result = _run_summary(tmp_path, telemetry, VALIDATE_OUTCOME="skipped", SMOKE_OUTCOME="skipped")
    assert result.returncode == 0, result.stderr
    assert "Outcome: `failed (RiotApiError)`" in result.stdout
    assert "| Matches inserted (new) | n/a |" in result.stdout
    assert "{'200': 39, '401': 1}" in result.stdout


def test_summary_when_the_run_stopped_before_ingest(tmp_path: Path) -> None:
    """E.g. an expired development key: Verify Riot API fails first, so no
    telemetry exists. The summary still renders and points at the cause."""
    result = _run_summary(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert "Ingest did not produce telemetry" in result.stdout
    assert "Verify Riot API" in result.stdout
    assert "Validate ingested data: `not run`" in result.stdout


def test_summary_step_never_fails_the_job_or_reads_secrets() -> None:
    text = _text()
    step = text[text.index("- name: Summarize collection") :]
    assert "if: always()" in step.split("run: |", 1)[0]
    assert '>> "${GITHUB_STEP_SUMMARY}" || true' in step
    assert "secrets." not in step and "RIOT_API_KEY" not in step and "DATABASE_URL" not in step
