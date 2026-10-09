#!/bin/bash
# PhonePad launcher. Double-click it in Finder, or run ./start.command [flags].
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys' >/dev/null 2>&1; then
  echo "Python 3 isn't installed yet."
  echo "A macOS window should pop up offering the Command Line Tools (they include Python 3)."
  echo "Click Install, wait for it to finish, then run start.command again."
  xcode-select --install 2>/dev/null
  exit 1
fi

if [ ! -x .venv/bin/python ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Setting up PhonePad (first run only, takes about a minute)..."
  [ -x .venv/bin/python ] || python3 -m venv .venv || exit 1
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt || { echo "Installing requirements failed."; exit 1; }
  touch .venv/.installed
fi

exec .venv/bin/python server.py "$@"
