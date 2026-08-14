#!/usr/bin/env bash
#
# Run the offline test suite. No hardware, no MPD, no Pi required.
#
#   ./tests/run-all.sh
#
# Picks an interpreter that actually has the dependencies:
#   1. $PYTHON if you set it
#   2. /opt/rpi-player/venv/bin/python   (on the Pi after install.sh)
#   3. ./venv/bin/python                  (local dev venv)
#   4. python3                            (only works if deps are installed)
#
# These are plain scripts, not unittest TestCases. Do NOT run them with
# `python -m unittest discover` — that re-imports files from a second sys.path
# root and fails with "module incorrectly imported from ...".
#
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD

pick_python() {
    if [[ -n "${PYTHON:-}" ]]; then echo "$PYTHON"; return; fi
    for c in /opt/rpi-player/venv/bin/python "$REPO/venv/bin/python"; do
        [[ -x "$c" ]] && { echo "$c"; return; }
    done
    command -v python3
}
PY=$(pick_python)

printf '\033[1mInterpreter:\033[0m %s (%s)\n' "$PY" "$("$PY" --version 2>&1)"

missing=$("$PY" - <<'PYEOF'
mods = {"serial": "pyserial", "mpd": "python-mpd2",
        "PIL": "Pillow", "StreamDeck": "streamdeck"}
out = []
for mod, pkg in mods.items():
    try:
        __import__(mod)
    except ImportError:
        out.append(pkg)
print(" ".join(out))
PYEOF
)
if [[ -n "$missing" ]]; then
    printf '\033[1;33m!\033[0m missing modules: %s\n' "$missing"
    printf '  install with: %s -m pip install %s\n' "$PY" "$missing"
    printf '  or point at the deployed venv: PYTHON=/opt/rpi-player/venv/bin/python %s\n\n' "$0"
fi

# Tests import `player.*` from the repo copy, not from /opt, so a local edit is
# what gets tested.
export PYTHONPATH="$REPO/opt/rpi-player${PYTHONPATH:+:$PYTHONPATH}"
export RPI_PLAYER_HOME="$REPO/opt/rpi-player"

fail=0
for t in tests/test_*.py; do
    printf '\n\033[1;34m=== %s ===\033[0m\n' "$t"
    if "$PY" "$t"; then
        :
    else
        fail=1
        printf '\033[1;31m%s FAILED\033[0m\n' "$t"
    fi
done

echo
if [[ $fail -eq 0 ]]; then
    printf '\033[1;32mAll test files passed.\033[0m\n'
else
    printf '\033[1;31mSome tests failed.\033[0m\n'
fi
exit $fail
