#!/bin/bash
# Double-click (macOS) or run ./start-clipper.command to run Clipper on this computer.
# First run installs everything (a few minutes).
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer is needed. Get it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close."; exit 1
fi
if [ ! -x clipper/.venv/bin/python ]; then
  echo "Setting up Clipper for the first time..."
  python3 -m venv clipper/.venv || { read -r -p "Couldn't create the Python environment. Press Enter to close."; exit 1; }
fi
clipper/.venv/bin/python -m pip install --disable-pip-version-check -q -U pip
if ! clipper/.venv/bin/python -m pip install --disable-pip-version-check -q -U -r clipper/requirements.txt; then
  read -r -p "Installing Clipper's parts failed. Check your internet connection and run this again. Press Enter to close."; exit 1
fi
exec clipper/.venv/bin/python -m clipper.app
