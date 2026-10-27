#!/usr/bin/env bash
# Start MBMeister. Creates a virtual environment (.venv) next to this script on
# first run and installs the Python dependencies from requirements.txt into it.
#
# Usage:   ./start_mbmeister.sh [registers.csv] [--host IP] [--port 502] [--unit 1]
# Options: PYTHON=python3.12 ./start_mbmeister.sh   (choose a specific interpreter)
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

VENV=".venv"
PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Error: '$PY' not found. Please install Python 3.9 or newer." >&2
    exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Error: Python 3.9 or newer is required (found: $("$PY" --version 2>&1))." >&2
    exit 1
fi

if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
    cat >&2 << 'MSG'
Error: Python's tkinter module is missing (needed for the UI). Install it with:
  Debian/Ubuntu: sudo apt install python3-tk
  Fedora:        sudo dnf install python3-tkinter
  Arch:          sudo pacman -S tk
MSG
    exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtual environment in $VENV ..."
    if ! "$PY" -m venv "$VENV"; then
        rm -rf "$VENV"
        echo "Error: could not create the virtual environment." >&2
        echo "On Debian/Ubuntu try: sudo apt install python3-venv" >&2
        exit 1
    fi
fi

# (Re)install dependencies only when requirements.txt changed
STAMP="$VENV/.requirements.sha256"
CURRENT="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$CURRENT" ]; then
    echo "Installing dependencies ..."
    "$VENV/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
    echo "$CURRENT" > "$STAMP"
fi

# Without arguments, load register.csv if it exists
if [ $# -eq 0 ] && [ -f register.csv ]; then
    set -- register.csv
fi

exec "$VENV/bin/python" mbmeister.py "$@"
