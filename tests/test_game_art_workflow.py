from pathlib import Path

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "game-art-refresh.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def test_only_manually_triggered() -> None:
    text = _text()
    assert "workflow_dispatch" in text
    for trigger in ("\npush:", "\n  push:", "pull_request:", "schedule:"):
        assert trigger not in text


def test_uses_no_secrets_database_or_riot() -> None:
    text = _text()
    assert "secrets." not in text
    assert "DATABASE_URL:" not in text and "RIOT_API_KEY:" not in text
    for command in ("ingest-riot", "verify-riot", "patch-diagnostics", "validate-live-data", "discovery-smoke"):
        assert command not in text
    assert "tftlab refresh-game-art" in text


def test_never_commits_to_main() -> None:
    text = _text()
    assert 'Refusing to commit to main' in text
    assert text.index("Check branch") < text.index("actions/checkout")
    assert "git push origin \"HEAD:${GITHUB_REF_NAME}\"" in text
    assert "if: ${{ inputs.commit }}" in text


def test_permissions_are_contents_only() -> None:
    text = _text()
    block = text.split("permissions:", 1)[1].split("\n\n", 1)[0]
    assert block.strip() == "contents: write"
