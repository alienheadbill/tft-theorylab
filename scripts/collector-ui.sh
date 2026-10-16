#!/usr/bin/env bash
# TheoryLabs Collector control panel (macOS / Linux). Runs the canonical
# command `tftlab collector-ui` from the repository folder; it serves
# http://127.0.0.1:8765/ (this computer only) and opens the browser.
# Stop it with Ctrl+C here, or the "Stop the collector app" button.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
TFTLAB="${ROOT}/.venv/bin/tftlab"
if [ ! -x "${TFTLAB}" ]; then
  echo "TheoryLabs is not installed in ${ROOT}/.venv yet (see docs/local-collector.md)." >&2
  exit 1
fi
exec "${TFTLAB}" collector-ui "$@"
