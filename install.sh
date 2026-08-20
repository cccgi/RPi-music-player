#!/usr/bin/env bash
#
# install.sh — deploy the RPi music player onto a Raspberry Pi OS Lite system.
#
# Idempotent: safe to re-run after a git pull. Preserves your edited
# etc/config.toml and etc/keymap.toml (the learned byte codes especially —
# clobbering those would be infuriating).
#
# Usage:
#   git clone <repo> ~/rpi-music-player
#   cd ~/rpi-music-player
#   sudo ./install.sh
#
set -euo pipefail

DEST=/opt/rpi-player
SERVICE_USER="${SUDO_USER:-pi}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
id "$SERVICE_USER" &>/dev/null || die "user '$SERVICE_USER' does not exist"

# ---------------------------------------------------------------------------
log "Preflight: verifying the source tree"
# ---------------------------------------------------------------------------
# Deploying a broken tree produces a daemon that starts, fails, and restarts
# forever with an error that points at the wrong thing. Catch it here instead.
#
# The specific failure this guards against: a .py file that is actually a shell
# script. systemd runs it through python, python hits `export PYTHONPATH=...`,
# and reports "SyntaxError: invalid syntax" — which looks like a code bug and
# is not.

[[ -d "$SRC/opt/rpi-player/player" ]] || die \
    "no player/ package at $SRC/opt/rpi-player — are you running this from the repo root?"

preflight_fail=0

# Every module the daemons import must be real Python of plausible size.
declare -A MIN_SIZE=(
    [player/config.py]=4000
    [player/mpdbus.py]=4000
    [player/outputs.py]=4000
    [player/actions.py]=4000
    [player/ipc.py]=2000
    [player/tourbox_daemon.py]=4000
    [player/streamdeck_daemon.py]=6000
    [player/tourbox/protocol.py]=4000
    [player/streamdeck/render.py]=6000
    [player/streamdeck/layout.py]=1000
)
for rel in "${!MIN_SIZE[@]}"; do
    f="$SRC/opt/rpi-player/$rel"
    if [[ ! -f "$f" ]]; then
        warn "MISSING  $rel"; preflight_fail=1; continue
    fi
    first=$(head -n1 "$f")
    if [[ "$first" == *bash* || "$first" == *"/bin/sh"* ]]; then
        warn "$rel is a SHELL SCRIPT, not Python (line 1: $first)"
        preflight_fail=1; continue
    fi
    size=$(stat -c%s "$f")
    if (( size < ${MIN_SIZE[$rel]} )); then
        warn "$rel is only ${size}B (expected >= ${MIN_SIZE[$rel]}B) — looks like a stub"
        preflight_fail=1
    fi
done

# Syntax-check with the system python before we build a venv around it.
while IFS= read -r -d '' f; do
    python3 -m py_compile "$f" 2>/dev/null || {
        warn "syntax error in ${f#"$SRC/"}"; preflight_fail=1; }
done < <(find "$SRC/opt/rpi-player/player" -name '*.py' -print0)

# Config files must have real content, not just exist. A stub config.toml
# yields "0 routes" and output switching silently does nothing.
python3 - "$SRC" <<'PY' || preflight_fail=1
import sys
src = sys.argv[1]
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        # Debian Trixie ships Python 3.13, so this should never fire there.
        # If it does, skip the check rather than block a valid install.
        print("  (skipping TOML content check: no tomllib/tomli)")
        sys.exit(0)
try:
    c = tomllib.load(open(f"{src}/opt/rpi-player/etc/config.toml", "rb"))
    k = tomllib.load(open(f"{src}/opt/rpi-player/etc/keymap.toml", "rb"))
except Exception as exc:
    print(f"  config/keymap TOML will not parse: {exc}"); sys.exit(1)
bad = False
if len(c.get("routes", [])) < 1:
    print("  config.toml has no [[routes]] — output switching would be dead"); bad = True
if len(k.get("buttons", {})) < 1:
    print("  keymap.toml has no [buttons] — the TourBox would do nothing"); bad = True
if len(k.get("rotary", {})) < 1:
    print("  keymap.toml has no [rotary] — knobs would do nothing"); bad = True
sys.exit(1 if bad else 0)
PY

if (( preflight_fail )); then
    die "source tree is incomplete or corrupt — refusing to deploy.
    You are probably running install.sh from the wrong directory, or the
    transfer to the Pi was partial. Re-copy the repository and try again:
      rsync -av --delete <laptop>:/path/to/RPi-Music-player/ ~/rpi-music-player/"
fi
ok_msg="source tree verified"; log "$ok_msg"

# ---------------------------------------------------------------------------
log "Installing system packages"
# ---------------------------------------------------------------------------
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev \
    build-essential pkg-config \
    mpd mpc \
    pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth \
    pipewire-audio-client-libraries \
    bluez bluez-tools \
    libhidapi-libusb0 libusb-1.0-0 \
    libjpeg-dev zlib1g-dev libfreetype6-dev \
    fonts-dejavu-core \
    samba samba-common-bin avahi-daemon avahi-utils \
    python3-gpiozero python3-lgpio \
    mpv ffmpeg \
    rsync socat

# ModemManager probes CDC-ACM ports with AT commands and will fight us for the
# TourBox. A dedicated player has no use for it. The udev rule below is still
# installed as belt-and-braces in case something reinstalls it later.
if dpkg -l modemmanager 2>/dev/null | grep -q '^ii'; then
    warn "Removing ModemManager (it probes the TourBox serial port)"
    apt-get purge -y modemmanager || warn "could not purge; udev rule will handle it"
fi

# ---------------------------------------------------------------------------
log "Creating $DEST"
# ---------------------------------------------------------------------------
mkdir -p "$DEST"

# Preserve user-edited config. keymap.toml in particular may contain byte codes
# learned from the actual device — losing those means redoing the capture.
for f in etc/config.toml etc/keymap.toml; do
    if [[ -f "$DEST/$f" ]]; then
        log "Preserving existing $f (new version at $f.dist)"
        mkdir -p "$DEST/$(dirname "$f")"
        cp "$SRC/opt/rpi-player/$f" "$DEST/$f.dist"
    fi
done

# Copy the tree, but never overwrite a preserved config.
rsync -a --exclude='etc/config.toml' --exclude='etc/keymap.toml' \
      "$SRC/opt/rpi-player/" "$DEST/"

for f in etc/config.toml etc/keymap.toml; do
    [[ -f "$DEST/$f" ]] || cp "$SRC/opt/rpi-player/$f" "$DEST/$f"
done

chmod +x "$DEST/bin/"* 2>/dev/null || true

# ---------------------------------------------------------------------------
log "Building the Python virtualenv"
# ---------------------------------------------------------------------------
if [[ ! -d "$DEST/venv" ]]; then
    python3 -m venv "$DEST/venv"
fi
"$DEST/venv/bin/pip" install --quiet --upgrade pip wheel
"$DEST/venv/bin/pip" install --quiet -r "$DEST/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$DEST"

# ---------------------------------------------------------------------------
log "Postflight: verifying the deployed tree actually runs"
# ---------------------------------------------------------------------------
# Import every module and invoke the two entry points exactly the way systemd
# will. If this passes, a subsequent service failure is a runtime/permissions
# problem, not a broken deploy — which narrows debugging enormously.
postflight_fail=0
for mod in player.config player.mpdbus player.outputs player.actions player.ipc \
           player.tourbox.protocol player.streamdeck.render player.streamdeck.layout; do
    if ! out=$(cd "$DEST" && "$DEST/venv/bin/python" -c "import $mod" 2>&1); then
        warn "import $mod failed: $(echo "$out" | tail -n1)"
        postflight_fail=1
    fi
done

for mod in player.tourbox_daemon player.streamdeck_daemon; do
    if ! out=$(cd "$DEST" && timeout 20 "$DEST/venv/bin/python" -m "$mod" --help 2>&1); then
        warn "'python -m $mod --help' failed: $(echo "$out" | tail -n2)"
        postflight_fail=1
    fi
done

# Assert the deployed config has real content — a stub would start cleanly and
# then do nothing, which is far harder to diagnose than a crash.
cd "$DEST" && "$DEST/venv/bin/python" - <<'PY' || postflight_fail=1
import sys
from player.config import load_config, load_keymap, setup_logging
setup_logging("ERROR")
c, k = load_config(), load_keymap()
print(f"    routes={len(c.routes)} {[r.id for r in c.routes]}")
print(f"    buttons={len(k.buttons)} rotaries={len(k.rotaries)} modifier={k.modifier}")
sys.exit(0 if (c.routes and k.buttons and k.rotaries) else 1)
PY

(( postflight_fail )) && die "deployed tree does not run — see the warnings above"
log "deployed tree verified"

# ---------------------------------------------------------------------------
log "Installing udev rules"
# ---------------------------------------------------------------------------
install -m 0644 "$SRC/system/udev/99-tourbox.rules"    /etc/udev/rules.d/
install -m 0644 "$SRC/system/udev/99-streamdeck.rules" /etc/udev/rules.d/
# Pins the Sound Blaster Play! 3's hardware gain to 0dB on every plug/boot —
# it powers up at -20dB and MPD's software mixer never touches this control.
# See system/udev/99-soundblaster-gain.rules for the full story.
install -m 0644 "$SRC/system/udev/99-soundblaster-gain.rules" /etc/udev/rules.d/
install -m 0755 "$SRC/opt/rpi-player/bin/soundblaster-gain"   /opt/rpi-player/bin/
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty --subsystem-match=usb --subsystem-match=sound || true

# ---------------------------------------------------------------------------
log "Installing polkit shutdown rule"
# ---------------------------------------------------------------------------
# Without this, systemctl poweroff run as $SERVICE_USER fails with
# "Interactive authentication required" and the Stream Deck's shutdown key
# silently does nothing on the second (confirm) press.
mkdir -p /etc/polkit-1/rules.d
sed "s/subject.user == \"rpi\"/subject.user == \"$SERVICE_USER\"/" \
    "$SRC/system/polkit/49-rpi-player-shutdown.rules" \
    > /etc/polkit-1/rules.d/49-rpi-player-shutdown.rules
chmod 0644 /etc/polkit-1/rules.d/49-rpi-player-shutdown.rules

# ---------------------------------------------------------------------------
log "Installing polkit Wi-Fi radio rule"
# ---------------------------------------------------------------------------
# Same reasoning as the shutdown rule above, for the Video page's Wi-Fi
# toggle key (actions.py's toggle_wifi) -- `nmcli radio wifi off/on` run as
# $SERVICE_USER needs this or it fails silently the same way poweroff did.
sed "s/subject.user == \"rpi\"/subject.user == \"$SERVICE_USER\"/" \
    "$SRC/system/polkit/50-rpi-player-wifi.rules" \
    > /etc/polkit-1/rules.d/50-rpi-player-wifi.rules
chmod 0644 /etc/polkit-1/rules.d/50-rpi-player-wifi.rules

# ---------------------------------------------------------------------------
log "Installing polkit Bluetooth service-restart rule"
# ---------------------------------------------------------------------------
# Same reasoning again, for the Bluetooth picker's "Reset" key (bt.py's
# reset_pairing_blocking/reset_failed_pairings_blocking) -- its fallback
# `systemctl restart bluetooth` (used when `bluetoothctl remove` alone
# leaves a stale on-disk pairing record behind) needs this or it fails
# silently the same way poweroff and Wi-Fi toggle did before their rules
# existed. Scoped to bluetooth.service only.
sed "s/subject.user == \"rpi\"/subject.user == \"$SERVICE_USER\"/" \
    "$SRC/system/polkit/51-rpi-player-bluetooth.rules" \
    > /etc/polkit-1/rules.d/51-rpi-player-bluetooth.rules
chmod 0644 /etc/polkit-1/rules.d/51-rpi-player-bluetooth.rules

# ---------------------------------------------------------------------------
log "Installing polkit NetworkManager-restart rule"
# ---------------------------------------------------------------------------
# Same reasoning again, for the AirPlay picker's "Fix Wifi" key (wifi.py's
# restart_networking_blocking) -- `systemctl restart NetworkManager` needs
# this or it fails silently the same way the other three did before their
# rules existed. Scoped to NetworkManager.service only.
sed "s/subject.user == \"rpi\"/subject.user == \"$SERVICE_USER\"/" \
    "$SRC/system/polkit/52-rpi-player-network.rules" \
    > /etc/polkit-1/rules.d/52-rpi-player-network.rules
chmod 0644 /etc/polkit-1/rules.d/52-rpi-player-network.rules

# ---------------------------------------------------------------------------
log "Installing polkit video-mpv-restart rule"
# ---------------------------------------------------------------------------
# Same reasoning again, for the video page's "Reinit" key (actions.py's
# video_reinit) -- `systemctl restart video-mpv.service` needs this or it
# fails silently the same way the other four did before their rules
# existed. Scoped to video-mpv.service only.
sed "s/subject.user == \"rpi\"/subject.user == \"$SERVICE_USER\"/" \
    "$SRC/system/polkit/53-rpi-player-video-mpv.rules" \
    > /etc/polkit-1/rules.d/53-rpi-player-video-mpv.rules
chmod 0644 /etc/polkit-1/rules.d/53-rpi-player-video-mpv.rules
systemctl restart polkit 2>/dev/null || true

# ---------------------------------------------------------------------------
log "Disabling Wi-Fi power save"
# ---------------------------------------------------------------------------
# brcmfmac (the Pi's onboard Broadcom chip) runs with power save ON by
# default, which sleeps the radio between beacon intervals and causes
# exactly the kind of "iffy Wi-Fi" this build cannot tolerate: SSH sessions
# timing out and, worse, AirPlay speakers vanishing from the Video page's
# picker because mDNS multicast announcements get delayed/dropped while the
# radio naps. Looks nothing like a signal problem -- found live with
# excellent signal (-41 dBm) right next to the AP. No per-user permission
# issue here (this is root-owned NetworkManager config), just needs to
# exist and NetworkManager restarted to pick it up.
mkdir -p /etc/NetworkManager/conf.d
cp "$SRC/system/networkmanager/wifi-powersave-off.conf" \
    /etc/NetworkManager/conf.d/wifi-powersave-off.conf
chmod 0644 /etc/NetworkManager/conf.d/wifi-powersave-off.conf
systemctl restart NetworkManager 2>/dev/null || true

# ---------------------------------------------------------------------------
log "Installing CPU governor permission fix"
# ---------------------------------------------------------------------------
# Lets $SERVICE_USER write /sys/.../scaling_governor without root, so
# player/cpugov.py can drop clock speed while idle (streamdeck_daemon.py's
# _apply_low_power) and restore it instantly on any key press. `z` lines
# just adjust an existing path's ownership, so this is a no-op (logged, not
# fatal) on a board with fewer than 4 cores or without cpufreq at all.
sed "s/ - rpi  -/ - $SERVICE_USER  -/" \
    "$SRC/system/tmpfiles.d/rpi-player-cpufreq.conf" \
    > /etc/tmpfiles.d/rpi-player-cpufreq.conf
chmod 0644 /etc/tmpfiles.d/rpi-player-cpufreq.conf
systemd-tmpfiles --create /etc/tmpfiles.d/rpi-player-cpufreq.conf

# ---------------------------------------------------------------------------
log "Configuring groups for $SERVICE_USER"
# ---------------------------------------------------------------------------
getent group plugdev >/dev/null || groupadd plugdev
for grp in dialout plugdev audio video render gpio; do
    getent group "$grp" >/dev/null && usermod -aG "$grp" "$SERVICE_USER"
done
# MPD needs to read the music directory.
usermod -aG "$SERVICE_USER" mpd 2>/dev/null || true

# ---------------------------------------------------------------------------
log "Enabling lingering for $SERVICE_USER"
# ---------------------------------------------------------------------------
# PipeWire, and therefore Bluetooth/AirPlay routing AND video mode's mpv, all
# live at /run/user/<uid>/ — a per-user runtime directory that systemd only
# creates once that user has an active session. Without lingering, it does
# not exist until an interactive login happens, so every service that starts
# at boot with no one logged in (all of them, on an appliance with no
# monitor/keyboard) fails to reach PipeWire with errors like "Could not
# connect to PipeWire" or "Host is down" that look like a PipeWire bug but
# are actually this ordering problem.
loginctl enable-linger "$SERVICE_USER"

# ---------------------------------------------------------------------------
log "Installing WirePlumber DAC-exclusion rule"
# ---------------------------------------------------------------------------
# Without this, WirePlumber claims the USB DAC and MPD cannot open hw:
# exclusively — bit-perfect playback fails with 'device busy'.
mkdir -p /etc/wireplumber/wireplumber.conf.d
install -m 0644 "$SRC/system/pipewire/51-mpd-dac-ignore.conf" \
    /etc/wireplumber/wireplumber.conf.d/
warn "EDIT /etc/wireplumber/wireplumber.conf.d/51-mpd-dac-ignore.conf"
warn "  to match your DAC (see 'wpctl status'), or MPD will fight PipeWire."

# ---------------------------------------------------------------------------
log "Disabling WirePlumber's logind seat gate on the Bluetooth monitor"
# ---------------------------------------------------------------------------
# Without this, Bluetooth pairing/reconnect fails almost every time with
# `br-connection-profile-unavailable` — WirePlumber's bluez5 monitor waits
# for logind to report the seat as "active" before it starts at all, and a
# headless box running under `loginctl enable-linger` (no console/graphical
# login, ever) never reaches "active" — only "online". See the in-file
# comment for the full trace of how this was found.
install -m 0644 "$SRC/system/pipewire/52-headless-bluez-seat.conf" \
    /etc/wireplumber/wireplumber.conf.d/

# ---------------------------------------------------------------------------
log "Enabling AirPlay speaker discovery"
# ---------------------------------------------------------------------------
# Without this, the "airplay" route can never find anything -- PipeWire
# never creates raop_sink nodes at all until this module is loaded, no
# matter what speakers are on the network.
mkdir -p /etc/pipewire/pipewire.conf.d
install -m 0644 "$SRC/system/pipewire/10-raop-discover.conf" \
    /etc/pipewire/pipewire.conf.d/
warn "EDIT config.toml's [[routes]] airplay pw_sink patterns to match YOUR"
warn "  speakers' mDNS hostnames (avahi-browse -rt _raop._tcp) -- the"
warn "  generic 'raop_sink' substring matches every _raop._tcp announcer on"
warn "  the LAN, including other people's laptops, not just real speakers."

# ---------------------------------------------------------------------------
log "Configuring Samba"
# ---------------------------------------------------------------------------
MUSIC_DIR="/home/$SERVICE_USER/Music"
mkdir -p "$MUSIC_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$MUSIC_DIR"
chmod 0775 "$MUSIC_DIR"

# Video/karaoke library — created here (not just in the later "Creating
# video/karaoke library" step, which still runs and is a harmless no-op
# mkdir -p) because the Samba share needs the directory to exist by the
# time smb.conf is written and testparm validates it below. Matches
# config.toml's [video] library_dir default.
VIDEO_DIR="/home/$SERVICE_USER/Video"
mkdir -p "$VIDEO_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$VIDEO_DIR"
chmod 0775 "$VIDEO_DIR"

if [[ -f /etc/samba/smb.conf && ! -f /etc/samba/smb.conf.orig ]]; then
    cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
    log "Backed up original smb.conf to smb.conf.orig"
fi
sed "s|/home/pi/Music|$MUSIC_DIR|g; s|/home/pi/Video|$VIDEO_DIR|g; \
     s|valid users = pi|valid users = $SERVICE_USER|g; \
     s|force user = pi|force user = $SERVICE_USER|g; s|force group = pi|force group = $SERVICE_USER|g" \
    "$SRC/system/samba/smb.conf" > /etc/samba/smb.conf

mkdir -p /etc/avahi/services
install -m 0644 "$SRC/system/samba/avahi-smb.service" /etc/avahi/services/smb.service

# smbpasswd is per-user and NOT set by this script (needs an interactive
# password prompt) — see the "Video/karaoke mode" note near the end of this
# script's summary output for the exact command to run once, by hand.
if ! (sudo -u "$SERVICE_USER" true 2>/dev/null; pdbedit -L 2>/dev/null | grep -q "^$SERVICE_USER:"); then
    warn "no Samba password set for $SERVICE_USER yet — the Music/Video shares"
    warn "  will prompt for credentials and reject them until you run:"
    warn "    sudo smbpasswd -a $SERVICE_USER"
fi

testparm -s >/dev/null 2>&1 || warn "testparm reported problems with smb.conf"

# ---------------------------------------------------------------------------
log "Configuring MPD"
# ---------------------------------------------------------------------------
if [[ -f /etc/mpd.conf && ! -f /etc/mpd.conf.orig ]]; then
    cp /etc/mpd.conf /etc/mpd.conf.orig
    log "Backed up original mpd.conf to mpd.conf.orig"
fi
if [[ ! -f /etc/mpd.conf.rpi-player ]]; then
    sed "s|/home/pi/Music|$MUSIC_DIR|g" "$SRC/system/mpd.conf" > /etc/mpd.conf.rpi-player
    warn "New MPD config written to /etc/mpd.conf.rpi-player — it is NOT active yet."
    warn "  Set the correct 'device' for your DAC (see 'aplay -l'), then:"
    warn "    sudo cp /etc/mpd.conf.rpi-player /etc/mpd.conf && sudo systemctl restart mpd"
fi

# ---------------------------------------------------------------------------
log "Running MPD as $SERVICE_USER, not the system 'mpd' account"
# ---------------------------------------------------------------------------
# Debian's stock mpd.service runs as the system account 'mpd', which cannot
# see the per-user PipeWire socket at /run/user/<uid>/pipewire-0 — Bluetooth
# and AirPlay routing are physically unreachable until this override is in
# place, failing with the misleading "Failed to connect stream: Host is
# down". See system/systemd/mpd.service.d-override.conf for the full story.
mkdir -p /etc/systemd/system/mpd.service.d
sed "s|User=pi|User=$SERVICE_USER|; s|Group=pi|Group=$SERVICE_USER|" \
    "$SRC/system/systemd/mpd.service.d-override.conf" \
    > /etc/systemd/system/mpd.service.d/override.conf
chmod 0644 /etc/systemd/system/mpd.service.d/override.conf

# MPD's database/state/playlists directory must be owned by whoever mpd.service
# now runs as, or it cannot write its own database on the first scan.
mkdir -p /var/lib/mpd/playlists
chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/mpd

REAL_UID="$(id -u "$SERVICE_USER")"
if [[ "$REAL_UID" != "1000" ]]; then
    warn "your user's UID is $REAL_UID, not 1000 — several unit files"
    warn "  (mpd.service.d/override.conf, tourbox-player, streamdeck-player,"
    warn "  video-mpv) hardcode /run/user/1000 for XDG_RUNTIME_DIR. Edit"
    warn "  their Environment= lines to /run/user/$REAL_UID after this install."
fi

# ---------------------------------------------------------------------------
log "Creating video/karaoke library and trash directories"
# ---------------------------------------------------------------------------
# Matches config.toml's [video] library_dir and [delete] trash_dir defaults.
# Both are harmless if video mode / trash mode are never used — an empty
# Video folder just means "no videos found" rather than a startup failure.
VIDEO_DIR="/home/$SERVICE_USER/Video"
TRASH_DIR="/home/$SERVICE_USER/Music-trash"
mkdir -p "$VIDEO_DIR" "$TRASH_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$VIDEO_DIR" "$TRASH_DIR"

# ---------------------------------------------------------------------------
log "Installing systemd units"
# ---------------------------------------------------------------------------
for unit in tourbox-player streamdeck-player shutdown-button video-mpv rpi-player-wifi-on avrcp-bridge; do
    sed "s|User=pi|User=$SERVICE_USER|; s|Group=pi|Group=$SERVICE_USER|" \
        "$SRC/system/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
    chmod 0644 "/etc/systemd/system/$unit.service"
done
systemd-analyze verify /etc/systemd/system/tourbox-player.service 2>&1 | head -5 || true
systemctl daemon-reload
systemctl enable tourbox-player.service streamdeck-player.service video-mpv.service \
    rpi-player-wifi-on.service avrcp-bridge.service
# Force Wi-Fi on right now too, in case this install is re-run on a box where
# a previous session left it off -- the new unit only guarantees this on the
# NEXT boot otherwise.
nmcli radio wifi on 2>/dev/null || true
# shutdown-button stays disabled until the user actually fits a button.
systemctl enable mpd.service smbd.service nmbd.service avahi-daemon.service

cat <<EOF

$(printf '\033[1;32m')Installation complete.$(printf '\033[0m')

BEFORE STARTING, these need your attention — see docs/DEPLOY.md for the full
walkthrough with verification steps for each one:

  1. MPD output device
       aplay -l                                  # find your DAC
       sudoedit /etc/mpd.conf.rpi-player         # set device "hw:CARD=...,DEV=0"
       sudo cp /etc/mpd.conf.rpi-player /etc/mpd.conf
       sudo systemctl restart mpd

  2. WirePlumber DAC exclusion
       wpctl status                              # find the card name
       sudoedit /etc/wireplumber/wireplumber.conf.d/51-mpd-dac-ignore.conf
       systemctl --user restart wireplumber

  3. TourBox byte codes — the shipped keymap is UNVERIFIED
       sudo systemctl stop tourbox-player
       $DEST/venv/bin/python $DEST/bin/tourbox-capture --learn --out /tmp/keymap.toml
       diff $DEST/etc/keymap.toml /tmp/keymap.toml
       sudo cp /tmp/keymap.toml $DEST/etc/keymap.toml

  4. Pair Bluetooth headphones (if you use the BT route)
       bluetoothctl
         power on; agent on; default-agent; scan on
         pair <MAC>; trust <MAC>; connect <MAC>
       Then set config.toml's [[routes]] bt_mac to match.

  5. AirPlay speakers (if you use the airplay route) — needs BOTH:
       - system/pipewire/10-raop-discover.conf already installed (this script
         did it), but PipeWire needs a restart to load it:
           systemctl --user restart pipewire pipewire-pulse wireplumber
       - config.toml's [[routes]] airplay pw_sink must match YOUR speaker's
         mDNS hostname (avahi-browse -rt _raop._tcp) — the generic
         "raop_sink" substring matches every AirPlay receiver on the LAN,
         including other people's laptops.

  6. Video/karaoke mode (.skp files) — only if you use it
       - Drop .skp files into $VIDEO_DIR (or edit config.toml's [video]
         library_dir)
       - video-mpv.service is installed and enabled; it needs a monitor
         plugged in at boot for video, but plays audio-only fine without one

Also set a Samba password (the Unix account is not enough):
       sudo smbpasswd -a $SERVICE_USER

Then:
       sudo systemctl start mpd tourbox-player streamdeck-player video-mpv
       $DEST/bin/rpi-player-doctor

NOTE: group changes AND lingering (for PipeWire access at boot with no one
logged in) need a re-login or reboot to fully take effect for $SERVICE_USER.
A reboot now is the simplest way to confirm everything survives a cold start:
       sudo reboot

EOF
