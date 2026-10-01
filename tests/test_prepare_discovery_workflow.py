"""The manual prepared-Discovery workflow: main only, production secret only
through an expression, serialized with live ingest, bounded, no Riot."""

from pathlib import Path

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "prepare-discovery.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def _commands() -> str:
    return "\n".join(l for l in _text().splitlines() if not l.lstrip().startswith("#"))


def test_manual_dispatch_only() -> None:
    text = _text()
    assert "workflow_dispatch:" in text
    for trigger in ("schedule:", "push:", "pull_request", "issue_comment", "workflow_run", "repository_dispatch"):
        assert trigger not in _commands(), trigger


def test_preflight_requires_main_and_a_postgres_url_before_checkout() -> None:
    text = _text()
    preflight = text[text.index("Validate production configuration") : text.index("actions/checkout")]
    assert 'if [ "${GITHUB_REF}" != "refs/heads/main" ]' in preflight
    assert '-z "${DATABASE_URL}"' in preflight and "postgres://*|postgresql://*" in preflight
    assert preflight.count("exit 1") >= 3


def test_secrets_never_printed_or_hardcoded() -> None:
    text = _text()
    assert "${{ secrets.NEON_DATABASE_URL }}" in text
    assert "${{ secrets.DATABASE_URL }}" not in text and "RIOT_API_KEY" not in text
    for line in text.splitlines():
        if "echo" in line.lower():
            assert "$DATABASE_URL" not in line and "${DATABASE_URL}" not in line, line


def test_serialized_with_live_ingest_bounded_and_read_only_permissions() -> None:
    text = _text()
    assert "group: live-ingest-production" in text and "cancel-in-progress: false" in text
    assert "timeout-minutes: 30" in text
    assert "contents: read" in text and "contents: write" not in text


def test_runs_only_the_prepare_command() -> None:
    commands = _commands()
    assert "tftlab prepare-discovery --force" in commands and "tftlab prepare-discovery\n" in commands
    assert commands.count("tftlab ") == 2
    for forbidden in ("ingest-riot", "verify-riot", "gh workflow", "continue-on-error"):
        assert forbidden not in commands, forbidden
