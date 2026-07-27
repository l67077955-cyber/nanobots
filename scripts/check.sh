#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# check.sh — Phase gate script (PLAN P0.3)
#
# P0 establishes the safety net. From P1 onward every Phase MUST land green
# on this script before merge (see CONTRIBUTING.md).
#
#   1. pytest -q            — full test suite must pass (0 error / 0 fail)
#   2. ruff --select F,E    — only F (pyflakes) + E (pycodestyle errors) for now;
#                            I/N/W (import-sort, naming, warnings) are tightened
#                            in later phases to avoid a giant pre-existing debt
#                            blocking the first refactor.
#
# Usage: scripts/check.sh
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> pytest"
python3 -m pytest -q

echo "==> ruff (F/E only; E501 line-length ignored)"
ruff check --select F,E --ignore E501 nanobot tests

echo "==> all green ✓"
