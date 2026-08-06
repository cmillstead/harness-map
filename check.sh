#!/bin/sh
# harness-map check pipeline. Green before any task is called done.
# Spec: fable-upgrades/audits/2026-07-18/harness-map/spec/SPEC_2 §1.
# The mypy step's strictness is governed by mypy.ini — tightened in S1.M1,
# never loosened thereafter (loosening requires a spec amendment).
set -e
cd "$(dirname "$0")"
echo "== ruff ==" && ruff check .
echo "== mypy ==" && python3 -m mypy --config-file mypy.ini collector.py render_html.py serve.py
echo "== pytest ==" && python3 -m pytest -q
if [ -z "$HARNESS_MAP_REAL_ROOT" ]; then
  echo "notice: HARNESS_MAP_REAL_ROOT is unset -- the real-input acceptance check did not run"
fi
echo "CHECK GREEN"
