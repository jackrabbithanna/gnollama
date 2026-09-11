#!/bin/sh
set -eu
export GNOLLAMA_REQUIRE_DISPLAY=1 GDK_BACKEND=x11 GSK_RENDERER=cairo
export PYTHONDONTWRITEBYTECODE=1
python3 tools/check_dependencies.py
meson setup _build --prefix=/app
meson compile -C _build
dbus-run-session -- xvfb-run -a meson test -C _build --print-errorlogs
