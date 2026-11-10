#!/bin/sh
# Launch the ChortVPN desktop client (Kivy). First run seeds settings from ./.env.
#   ./run-desktop.sh
#   CHORTVPN_REGISTRATION=1 ./run-desktop.sh   # preview the (inactive) sign-up form
set -eu
cd "$(dirname "$0")"
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -m chortvpn
