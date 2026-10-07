"""`Build public snapshot from encrypted backup` (public-snapshot-from-backup.yml):
manual only, never touches Neon, reuses db-backup.yml's encryption exactly,
and only the sanitized snapshot ever leaves the runner."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "public-snapshot-from-backup.yml"
BACKUP = WORKFLOWS / "db-backup.yml"
OPENSSL = "-aes-256-cbc -pbkdf2 -iter 250000 -md sha256"


def _text() -> str:
    return WORKFLOW.read_text()


def _code(text: str) -> str:
    """The workflow without its comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _step(text: str, name: str) -> str:
    start = text.index(f"- name: {name}")
    nxt = text.find("\n      - ", start + 1)
    return text[start:nxt if nxt != -1 else len(text)]


def test_manual_only_from_main_with_minimal_permissions() -> None:
    text = _text()
    triggers = text[text.index("\non:"):text.index("\npermissions:")]
    assert "workflow_dispatch:" in triggers
    for trigger in ("push", "pull_request", "schedule", "issue_comment", "workflow_run", "repository_dispatch"):
        assert trigger not in triggers, trigger
    permissions = text[text.index("\npermissions:"):text.index("\nconcurrency:")]
    assert re.findall(r"^  (\w+): (\w+)", permissions, re.M) == [("contents", "read"), ("actions", "read")]
    validate = _step(text, "Validate inputs and configuration")
    assert 'if [ "${GITHUB_REF}" != "refs/heads/main" ]' in validate
    assert text.index("Validate inputs and configuration") < text.index("actions/checkout")


def test_the_backup_is_an_explicit_input_defaulting_to_the_verified_october_5_backup() -> None:
    text = _text()
    assert 'default: "37308579783"' in text
    assert 'default: "theorylabs-postgres-backup-20261005T121811Z"' in text
    download = _step(text, "Download the selected encrypted backup artifact")
    assert "actions/download-artifact@v4" in download
    assert "run-id: ${{ github.event.inputs.backup_run_id }}" in download
    assert "name: ${{ github.event.inputs.backup_artifact_name }}" in download
    assert "latest" not in download.lower()
    validate = _step(text, "Validate inputs and configuration")
    assert "^theorylabs-postgres-backup-[0-9]{8}T[0-9]{6}Z$" in validate and "^[0-9]{1,20}$" in validate


def test_never_contacts_neon_or_any_configured_database() -> None:
    text = _text()
    for forbidden in ("NEON_DATABASE_URL", "secrets.DATABASE_URL", "DATABASE_URL:", "RIOT_API_KEY", "neon"):
        assert forbidden not in text, forbidden
    assert "postgres:18" in text and "127.0.0.1:55432" in text  # only the runner's own ephemeral container
    assert re.findall(r"secrets\.(\w+)", text) == ["DB_BACKUP_PASSPHRASE", "DB_BACKUP_PASSPHRASE"]


def test_decryption_matches_the_backup_workflow_exactly() -> None:
    backup, text = BACKUP.read_text(), _text()
    assert f"openssl enc {OPENSSL}" not in backup  # the backup encrypts with -salt ...
    assert f"openssl enc -d {OPENSSL}" in backup  # ... and verifies with these exact parameters
    restore = _step(text, "Verify checksum, decrypt and restore into ephemeral Postgres 18")
    assert f"openssl enc -d {OPENSSL}" in restore and "-pass env:DB_BACKUP_PASSPHRASE" in restore
    assert restore.index('sha256sum --check --strict "${CHECKSUM}"') < restore.index("openssl enc -d")
    assert 'ENC="theorylabs-${STAMP}.dump.enc"' in restore  # db-backup.yml's file naming
    assert "pg_restore --no-owner --no-acl" in restore and "postgres:18" in backup


def test_plaintext_and_connection_details_never_leave_the_runner_or_the_logs() -> None:
    text = _text()
    restore = _step(text, "Verify checksum, decrypt and restore into ephemeral Postgres 18")
    assert "umask 077" in restore and "trap 'rm -f \"${PLAIN}/restore.dump\"' EXIT" in restore
    assert 'rm -f "${PLAIN}/restore.dump"' in restore.split("pg_restore", 1)[1]
    assert '::add-mask::${PGPASS}' in restore and "GITHUB_ENV" not in _code(text)  # never in step env headers
    upload = _step(text, "Upload the sanitized snapshot (gzip + manifest only)")
    paths = re.findall(r"\$\{\{ runner\.temp \}\}/(\S+)", upload)
    assert paths == ["public-snapshot/theorylabs-public-snapshot.sqlite3.gz",
                     "public-snapshot/theorylabs-public-snapshot.sqlite3.manifest.json"]
    assert "retention-days: 7" in upload and ".dump" not in upload
    cleanup = _step(text, "Remove all production backup material from the runner")
    assert "if: ${{ always() }}" in cleanup
    for removed in ('docker rm -f -v "${RESTORE_CONTAINER}"', '"${RUNNER_TEMP}/backup-plain"',
                    '"${RUNNER_TEMP}/backup-encrypted"', 'rm -f "${RUNNER_TEMP}/restore-db-url"'):
        assert removed in cleanup, removed
    for line in text.splitlines():  # nothing echoes a secret or the restore URL
        if re.search(r"\becho\b", line):
            assert "DB_BACKUP_PASSPHRASE" not in line.replace("DB_BACKUP_PASSPHRASE repository secret", "")
            assert "restore-db-url" not in line and "PGPASS}" not in line.replace("::add-mask::${PGPASS}", "")


def test_the_snapshot_is_exported_and_verified_but_never_deployed() -> None:
    text = _text()
    export = _step(text, "Export the sanitized public snapshot")
    assert "tftlab export-public-snapshot" in export and "--compress" in export
    assert '--source "$(cat "${RUNNER_TEMP}/restore-db-url")"' in export
    assert "tftlab verify-public-snapshot" in _step(text, "Verify the snapshot independently")
    for deploy in ("render", "git push", "git commit", "gh release", "deploy"):
        assert deploy not in _code(text).lower().replace("nothing was deployed", ""), deploy
