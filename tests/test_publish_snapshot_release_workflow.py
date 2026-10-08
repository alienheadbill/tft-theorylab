"""`Publish public snapshot release` (publish-public-snapshot-release.yml):
manual only, from main, an explicit verified snapshot artifact, and only the
sanitized .gz + manifest ever reach the release."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "publish-public-snapshot-release.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def _code(text: str) -> str:
    """The workflow without its comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _step(text: str, name: str) -> str:
    start = text.index(f"- name: {name}")
    nxt = text.find("\n      - ", start + 1)
    return text[start:nxt if nxt != -1 else len(text)]


def _validate_script() -> str:
    step = _step(_text(), "Validate inputs")
    return "\n".join(line[10:] for line in step.split("run: |\n", 1)[1].splitlines())


def _validate(**inputs: str) -> int:
    env = {"GITHUB_REF": "refs/heads/main", "SOURCE_RUN_ID": "37791848207",
           "SOURCE_ARTIFACT": "theorylabs-public-snapshot-from-theorylabs-postgres-backup-20261005T121811Z",
           "RELEASE_TAG": "public-snapshot-18.3-20261005",
           "EXPECTED_SHA256": "ac3ff85abbc665ce177757df0c1c9e8c122bb2b74ffa17097ac9fbf41d19b4ed", "PATH": "/usr/bin:/bin"}
    env.update(inputs)
    return subprocess.run(["bash", "-c", _validate_script()], env=env, capture_output=True).returncode


def test_manual_only_from_main_with_minimal_permissions() -> None:
    text = _text()
    triggers = text[text.index("\non:"):text.index("\npermissions:")]
    assert "workflow_dispatch:" in triggers
    for trigger in ("push", "pull_request", "schedule", "issue_comment", "workflow_run", "repository_dispatch",
                    "release:"):
        assert trigger not in triggers, trigger
    permissions = text[text.index("\npermissions:"):text.index("\nconcurrency:")]
    assert re.findall(r"^  (\w+): (\w+)", permissions, re.M) == [("contents", "write"), ("actions", "read")]
    assert 'if [ "${GITHUB_REF}" != "refs/heads/main" ]' in _step(text, "Validate inputs")
    assert text.index("Validate inputs") < text.index("actions/checkout")
    assert _validate() == 0 and _validate(GITHUB_REF="refs/heads/feature") == 1


def test_defaults_are_the_verified_18_3_snapshot_and_an_immutable_tag() -> None:
    text = _text()
    assert 'default: "37791848207"' in text
    assert 'default: "theorylabs-public-snapshot-from-theorylabs-postgres-backup-20261005T121811Z"' in text
    assert 'default: "public-snapshot-18.3-20261005"' in text
    assert 'default: "ac3ff85abbc665ce177757df0c1c9e8c122bb2b74ffa17097ac9fbf41d19b4ed"' in text
    for step in ("Confirm the source is a successful snapshot run on main", "Download the selected sanitized snapshot artifact"):
        assert "latest" not in _step(text, step).lower(), step  # never "whatever is latest"
    assert "--latest" not in text and "releases/latest" not in text


@pytest.mark.parametrize("tag,ok", [
    ("public-snapshot-18.3-20261005", 0), ("public-snapshot-18.4-20261019-r2", 0), ("public-snapshot-18.2b-20260920", 0),
    ("latest", 1), ("public-snapshot-current", 1), ("v1.0", 1), ("public-snapshot-18.3", 1),
])
def test_release_tags_are_versioned(tag: str, ok: int) -> None:
    assert _validate(RELEASE_TAG=tag) == ok


def test_the_source_is_an_explicit_successful_snapshot_run() -> None:
    text = _text()
    download = _step(text, "Download the selected sanitized snapshot artifact")
    assert "actions/download-artifact@v4" in download
    assert "run-id: ${{ github.event.inputs.source_run_id }}" in download
    assert "name: ${{ github.event.inputs.source_artifact_name }}" in download
    confirm = _step(text, "Confirm the source is a successful snapshot run on main")
    assert 'actions/runs/${SOURCE_RUN_ID}' in confirm
    assert '".github/workflows/public-snapshot-from-backup.yml"' in confirm
    assert '"success"' in confirm and '"main"' in confirm
    assert text.index("Confirm the source") < text.index("Download the selected")
    assert _validate(SOURCE_RUN_ID="latest") == 1 and _validate(SOURCE_RUN_ID="") == 1


@pytest.mark.parametrize("artifact,ok", [
    ("theorylabs-public-snapshot-from-theorylabs-postgres-backup-20261005T121811Z", 0),
    ("theorylabs-postgres-backup-20261005T121811Z", 1),  # the ENCRYPTED PRODUCTION BACKUP: never
    ("theorylabs-postgres-backup-20261005T121811Z-public-snapshot", 1),
    ("theorylabs-public-snapshot-from-x; rm -rf /", 1),
    ("", 1),
])
def test_raw_backup_artifacts_can_never_be_selected(artifact: str, ok: int) -> None:
    assert _validate(SOURCE_ARTIFACT=artifact) == ok


def test_only_the_snapshot_and_manifest_are_ever_uploaded() -> None:
    text = _text()
    check = _step(text, "Check the artifact holds exactly the snapshot and its manifest")
    assert '"${FOUND}" != "${EXPECTED}"' in check and "nothing is published" in check
    assert "SNAPSHOT_GZ: theorylabs-public-snapshot.sqlite3.gz" in text
    assert "SNAPSHOT_MANIFEST: theorylabs-public-snapshot.sqlite3.manifest.json" in text
    release = _step(text, "Create the immutable release (or confirm it is unchanged)")
    assert 'gh release create "${RELEASE_TAG}" "${IN}/${SNAPSHOT_GZ}" "${IN}/${SNAPSHOT_MANIFEST}"' in release
    uploads = re.findall(r"gh release (?:create|upload) [^\n]*", release)
    assert len(uploads) == 2
    for command in uploads:
        files = re.findall(r'"\$\{IN\}/([^"]+)"', command)
        assert set(files) <= {"${SNAPSHOT_GZ}", "${SNAPSHOT_MANIFEST}", "${asset}"}, command
    assert 'for asset in "${SNAPSHOT_GZ}" "${SNAPSHOT_MANIFEST}"' in release
    for forbidden in (".dump", ".enc", "backup-encrypted", "backup-plain", "DB_BACKUP_PASSPHRASE", "--clobber"):
        assert forbidden not in _code(text), forbidden
    assert "https://github.com/${GITHUB_REPOSITORY}/releases/download/${RELEASE_TAG}/${SNAPSHOT_GZ}" in release


def test_an_existing_release_is_never_overwritten() -> None:
    release = _step(_text(), "Create the immutable release (or confirm it is unchanged)")
    assert 'cmp -s "${EXISTING}/${asset}" "${IN}/${asset}"' in release
    assert "it is never overwritten" in release and "exit 1" in release


def test_sha256_and_snapshot_verification_happen_before_publication() -> None:
    text = _text()
    verify = _step(text, "Verify SHA-256 and the snapshot")
    assert 'sha256sum "${IN}/${SNAPSHOT_GZ}"' in verify and '"${ACTUAL}" != "${EXPECTED_SHA256}"' in verify
    assert "tftlab verify-public-snapshot" in verify
    assert '"gz sha256 = manifest"' in verify and '"decompressed sha256 = manifest"' in verify
    assert "read_snapshot_provenance" in verify
    assert text.index("Verify SHA-256 and the snapshot") < text.index("gh release create")
    assert _validate(EXPECTED_SHA256="AC3FF8") == 1 and _validate(EXPECTED_SHA256="") == 1


def test_never_contacts_neon_a_database_or_render_and_uses_no_secrets() -> None:
    text = _text()
    code = _code(text).lower()
    for forbidden in ("neon", "database_url", "riot_api_key", "postgres:18", "render", "deploy hook", "git push"):
        assert forbidden not in code.replace("nothing was deployed; render is unchanged", ""), forbidden
    assert re.findall(r"secrets\.(\w+)", text) == []
    assert set(re.findall(r"\$\{\{ github\.token \}\}", text)) == {"${{ github.token }}"}
    cleanup = _step(text, "Remove working files")
    assert "if: ${{ always() }}" in cleanup
