#!/usr/bin/env bash
#
# diagnose-deploy.sh — report exactly what is deployed vs. what should be.
#
# Run this on the Pi when the daemons will not start. It makes no changes.
#
#   bash ~/rpi-music-player/tests/diagnose-deploy.sh
#
set -uo pipefail

DEST=/opt/rpi-player
hdr()  { printf '\n\033[1;34m── %s ─────────────────────────────\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[1;31m✗\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }

hdr "Stray nested copy"
if [[ -d /opt/opt ]]; then
    bad "/opt/opt exists — a 'cp -r opt /opt/' created a nested tree"
    printf '      contents: %s\n' "$(ls /opt/opt 2>/dev/null | tr '\n' ' ')"
else
    ok "no /opt/opt"
fi

hdr "Are the deployed .py files actually Python?"
# THE key check. A shell script with a .py extension is the failure that
# produces 'SyntaxError: invalid syntax' on an 'export' line.
for f in "$DEST"/player/tourbox_daemon.py "$DEST"/player/streamdeck_daemon.py \
         "$DEST"/player/config.py "$DEST"/player/actions.py \
         "$DEST"/player/outputs.py "$DEST"/player/mpdbus.py; do
    if [[ ! -f "$f" ]]; then
        bad "MISSING  $f"
        continue
    fi
    first=$(head -n1 "$f")
    size=$(stat -c%s "$f")
    if [[ "$first" == *bash* || "$first" == *"/bin/sh"* ]]; then
        bad "SHELL STUB ($size B)  $f"
        printf '      line 1: %s\n' "$first"
    elif [[ "$size" -lt 500 ]]; then
        warn "suspiciously small ($size B)  $f"
        printf '      line 1: %s\n' "$first"
    else
        ok "python ($size B)  $(basename "$f")"
    fi
done

hdr "Config content (not just existence)"
for f in "$DEST/etc/config.toml" "$DEST/etc/keymap.toml"; do
    if [[ ! -f "$f" ]]; then bad "MISSING $f"; continue; fi
    size=$(stat -c%s "$f")
    printf '  %s (%s B)\n' "$f" "$size"
done
"$DEST/venv/bin/python" - <<'PY' 2>/dev/null || bad "could not parse the TOML files"
import tomllib, sys
try:
    c = tomllib.load(open("/opt/rpi-player/etc/config.toml", "rb"))
    k = tomllib.load(open("/opt/rpi-player/etc/keymap.toml", "rb"))
except Exception as exc:
    print(f"  \033[1;31m✗\033[0m parse error: {exc}"); sys.exit(1)
routes = c.get("routes", [])
buttons = k.get("buttons", {})
rotary = k.get("rotary", {})
mark = lambda good: "\033[1;32m✓\033[0m" if good else "\033[1;31m✗\033[0m"
print(f"  {mark(len(routes) >= 1)} routes: {len(routes)}  {[r.get('id') for r in routes]}")
print(f"  {mark(len(buttons) >= 1)} buttons: {len(buttons)}")
print(f"  {mark(len(rotary) >= 1)} rotaries: {len(rotary)}")
if not routes:
    print("      -> config.toml is a stub. Expected 4 routes: local, bt, airplay, hdmi")
if not buttons:
    print("      -> keymap.toml is a stub. Expected 12 buttons + 3 rotaries")
PY

hdr "Do the entry-point modules import?"
for mod in player.config player.actions player.outputs player.mpdbus \
           player.tourbox_daemon player.streamdeck_daemon; do
    if out=$(cd "$DEST" && "$DEST/venv/bin/python" -c "import $mod" 2>&1); then
        ok "import $mod"
    else
        bad "import $mod"
        printf '      %s\n' "$(echo "$out" | tail -n1)"
    fi
done

hdr "Are the modules systemd invokes actually runnable?"
for mod in player.tourbox_daemon player.streamdeck_daemon; do
    if out=$(cd "$DEST" && timeout 5 "$DEST/venv/bin/python" -m "$mod" --help 2>&1); then
        ok "python -m $mod --help works"
    else
        bad "python -m $mod --help failed"
        printf '      %s\n' "$(echo "$out" | tail -n2)"
    fi
done
# These are PACKAGES in the correct layout, not entry points. If a unit file
# references them, that unit came from a different scaffold.
for mod in player.tourbox player.streamdeck; do
    if cd "$DEST" && "$DEST/venv/bin/python" -c "import $mod.__main__" >/dev/null 2>&1; then
        warn "$mod has a __main__ (unexpected in this layout)"
    fi
done

hdr "systemd units"
for u in tourbox-player streamdeck-player; do
    p="/etc/systemd/system/$u.service"
    if [[ ! -f "$p" ]]; then bad "MISSING $p"; continue; fi
    exec_line=$(grep -m1 '^ExecStart=' "$p" || echo "(none)")
    desc=$(grep -m1 '^Description=' "$p" || echo "(none)")
    printf '  %s\n    %s\n    %s\n' "$u" "$desc" "$exec_line"
    if [[ "$exec_line" == *"-m player."*"_daemon"* ]]; then
        ok "ExecStart uses the correct module form"
    else
        bad "ExecStart does not use '-m player.<name>_daemon' — wrong scaffold"
    fi
done

hdr "Python dependencies in the venv"
for pkg in serial mpd StreamDeck PIL; do
    if "$DEST/venv/bin/python" -c "import $pkg" >/dev/null 2>&1; then
        ok "$pkg"
    else
        bad "$pkg missing from the venv"
    fi
done

hdr "Source tree on disk"
for d in ~/rpi-music-player ~/uploaded_opt; do
    if [[ -d "$d" ]]; then
        printf '  %s:\n' "$d"
        ls -1 "$d" 2>/dev/null | sed 's/^/      /'
    fi
done

printf '\n\033[1mIf any .py file above is a SHELL STUB, the deployed tree is not this\n'
printf 'repository. See docs/RECOVERY.md.\033[0m\n'
