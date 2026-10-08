#!/usr/bin/env bash
# TheoryLabs local collector launcher (macOS / Linux).
#
# Finds this repository's virtual environment and runs the canonical command,
# `tftlab local-collect`, from the repository folder (where .env lives). All
# collection logic is in the Python CLI; this file only locates it.
# Extra arguments are passed through, e.g. ./scripts/local-collect.sh --help
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
TFTLAB="${ROOT}/.venv/bin/tftlab"
if [ ! -x "${TFTLAB}" ]; then
  echo "TheoryLabs is not installed in ${ROOT}/.venv yet." >&2
  echo "One-time setup (see README, 'Collect data on your own computer'):" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi
exec "${TFTLAB}" local-collect "$@"
