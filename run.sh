#!/bin/bash
# One-command start: creates a local virtualenv the first time, then runs the monitor.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec ./.venv/bin/python mic_monitor.py "$@"
