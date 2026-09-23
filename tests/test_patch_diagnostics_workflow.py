from pathlib import Path

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "patch-diagnostics.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_only_manually_triggered() -> None:
    text = _text()
    assert "workflow_dispatch" in text
    for trigger in ("\npush:", "\n  push:", "pull_request:", "schedule:"):
        assert trigger not in text, f"unexpected trigger {trigger!r} in patch-diagnostics.yml"


def test_runs_only_patch_diagnostics() -> None:
    """This workflow must never run an ingest or a Riot call -- it's a
    read-only diagnostic. Only tftlab patch-diagnostics runs, and no other
    tftlab subcommand is invoked."""
    text = _text()
    assert "tftlab patch-diagnostics" in text
    for other_command in ("verify-riot", "ingest-riot", "discovery-smoke", "validate-live-data"):
        assert other_command not in text


def test_never_requires_riot_api_key() -> None:
    """A comment may mention RIOT_API_KEY to explain why it's absent, but
    the workflow must never actually reference the secret or set it as an
    env var for any step."""
    text = _text()
    assert "secrets.RIOT_API_KEY" not in text
    assert "RIOT_API_KEY:" not in text


def test_never_prints_secret_values() -> None:
    for line in _text().splitlines():
        lowered = line.lower()
        if "echo" in lowered or "printf" in lowered:
            for var in ("DATABASE_URL",):
                assert f"${var}" not in line, f"line prints secret variable {var}: {line!r}"
                assert f"${{{var}}}" not in line, f"line prints secret variable {var}: {line!r}"


def test_secrets_only_referenced_via_expression_not_hardcoded() -> None:
    assert "${{ secrets.DATABASE_URL }}" in _text()


def test_production_preflight_runs_before_database_step() -> None:
    text = _text()
    preflight_pos = text.index("Validate production configuration")
    for later_step in ("actions/checkout", "Run patch diagnostics"):
        assert preflight_pos < text.index(later_step)


def test_preflight_rejects_missing_or_malformed_database_url() -> None:
    text = _text()
    assert '-z "${DATABASE_URL}"' in text
    assert "postgres://*|postgresql://*" in text
    preflight = text[text.index("Validate production configuration") : text.index("actions/checkout")]
    assert preflight.count("exit 1") >= 2


def test_preflight_hard_requires_main_branch() -> None:
    text = _text()
    assert "GITHUB_REF" in text
    assert "refs/heads/main" in text
    preflight_pos = text.index("Validate production configuration")
    branch_check_pos = text.index("refs/heads/main")
    assert preflight_pos < branch_check_pos < text.index("actions/checkout")


def test_concurrency_serializes_production_runs() -> None:
    text = _text()
    assert "concurrency:" in text
    assert "group: patch-diagnostics-production" in text
    assert "cancel-in-progress: false" in text


def test_permissions_are_read_only() -> None:
    text = _text()
    assert "permissions:" in text
    assert "contents: read" in text
    assert "contents: write" not in text


def test_job_has_a_bounded_timeout() -> None:
    assert "timeout-minutes:" in _text()
