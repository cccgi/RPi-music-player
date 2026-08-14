"""Background Bluetooth reconnect helper.

The Stream Deck's BT route key used to just report "Unavailable" when no
`bluez_output` PipeWire sink existed — technically correct (there is nothing
to route audio to) but unhelpful when the headphones are simply switched off,
out of range, or still attached to a phone: those are all things a
`bluetoothctl connect` can usually fix on its own, since the device is already
paired and trusted (see ``bin/bt-connect``, which does this same dance
interactively).

This module is the automatic version of that: given a known MAC, try to bring
the link back up and wait for PipeWire to notice, off the calling thread so a
button press never blocks the render/input loop for the several seconds a
Bluetooth reconnect can take.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

LOG = logging.getLogger(__name__)

BTCTL = shutil.which("bluetoothctl") or "/usr/bin/bluetoothctl"

_DEVICE_LINE_RE = re.compile(r"Device ([0-9A-F:]{17}) (.+)")

# Bluetooth Class of Device is a 24-bit value; bits 8-12 are the 5-bit MAJOR
# device class. 0x04 is "Audio/Video", which covers every kind of audio
# gear regardless of its minor class — headsets, hi-fi, loudspeakers, car
# audio, hands-free car kits, and so on all share this same major class.
# Checking the major class field directly (rather than matching specific
# full first-byte prefixes, which bakes in particular service-class bits
# that only some devices happen to set) is both simpler and materially more
# correct: a car's hands-free kit failed the old prefix-regex check even
# though its class genuinely was Audio/Video, and never showed up in the
# picker no matter how long a scan ran.
_COD_MAJOR_AUDIO_VIDEO = 0x04

# A single BR/EDR page attempt commonly times out even when the remote
# device is right there, powered on, and ready — confirmed live: after a
# plain `bluetoothctl disconnect` (the radio never even left range), a
# reconnect to the Bose SL III failed with `br-connection-page-timeout` on
# 3 straight `bluetoothctl connect` calls before succeeding on the 4th, with
# nothing about the device or the Pi changing between attempts. A phone's
# Bluetooth stack silently retries several times before ever surfacing
# "can't connect" to the user; a single `bluetoothctl connect` call does
# not, which is why reconnecting here previously "usually took 3-4 presses
# of the BT key" — each press was really only ever making ONE attempt.
_CONNECT_ATTEMPTS = 5
_CONNECT_RETRY_DELAY = 1.5  # seconds between attempts


def _connect_with_retry(mac: str, attempts: int = _CONNECT_ATTEMPTS,
                         retry_delay: float = _CONNECT_RETRY_DELAY) -> bool:
    """Issue `bluetoothctl connect` up to `attempts` times, retrying any
    failure (page timeout, profile-unavailable, etc.) rather than giving up
    on the first one. See the module-level comment above for why this
    matters — reconnect failures here are overwhelmingly transient, not a
    real "this device is unreachable" condition.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        out = _bt("connect", mac)
        if "Failed to connect" not in out:
            return True
        last = out.strip().splitlines()[-1] if out.strip() else "?"
        LOG.debug("bluetoothctl connect %s failed (attempt %d/%d): %s",
                  mac, attempt, attempts, last)
        if attempt < attempts:
            time.sleep(retry_delay)
    LOG.warning("bluetoothctl connect %s failed after %d attempts: %s",
                mac, attempts, last)
    return False


def _bt(*args: str, timeout: float = 20.0) -> str:
    try:
        proc = subprocess.run([BTCTL, *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
        return proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.debug("bluetoothctl %s failed: %s", " ".join(args), exc)
        return ""


def _device_info(mac: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in _bt("info", mac).splitlines():
        if ":" in line:
            key, _, value = line.strip().partition(":")
            info[key.strip()] = value.strip()
    return info


def _is_audio(mac: str) -> bool:
    info = _device_info(mac)
    raw_class = info.get("Class", "")
    try:
        cod = int(raw_class, 16) if raw_class else None
    except ValueError:
        cod = None
    if cod is not None and ((cod >> 8) & 0x1F) == _COD_MAJOR_AUDIO_VIDEO:
        return True
    blob = _bt("info", mac)
    return any(marker in blob for marker in
               ("Audio Sink", "Advanced Audio", "Headset", "Handsfree", "Hands-Free"))


def connect_blocking(mac: str, sink_wait_seconds: float = 10.0) -> bool:
    """Power on, trust, connect. Block the CALLING thread until a
    ``bluez_output`` PipeWire sink appears or ``sink_wait_seconds`` elapses.

    Only call this from a background thread — a real reconnect (scanning the
    last known channel, re-negotiating A2DP) commonly takes several seconds.
    """
    from .outputs import PipeWireControl  # local import: avoid a cycle with outputs.py

    _bt("power", "on")
    _bt("trust", mac)
    if not _connect_with_retry(mac):
        return False

    pw = PipeWireControl()
    deadline = time.monotonic() + sink_wait_seconds
    while time.monotonic() < deadline:
        if pw.find_sink("bluez_output") is not None:
            return True
        time.sleep(0.5)
    LOG.warning("bluetoothctl connect %s reported success but no "
                "bluez_output sink appeared within %.0fs", mac, sink_wait_seconds)
    return False


def connect_async(mac: str, on_done: Callable[[bool], None]) -> None:
    """Fire-and-forget: run :func:`connect_blocking` on a daemon thread,
    then call ``on_done(ok)`` — from that same background thread, so
    whatever ``on_done`` does must be thread-safe (a simple attribute write
    plus an Event.set(), in every caller here)."""
    def _run() -> None:
        ok = connect_blocking(mac)
        on_done(ok)

    threading.Thread(target=_run, daemon=True, name="bt-connect").start()


# ---------------------------------------------------------------------------
# Picker support — list nearby/known audio devices, pair+connect a chosen one
# ---------------------------------------------------------------------------
# Mirrors the AirPlay picker's shape (streamdeck_daemon.py): show something
# immediately, then let a slower background step fill in more. Unlike
# AirPlay, there is no "already discovered, just pick one" shortcut here —
# a device has to either already be known to bluetoothd, or be found by an
# active scan, which takes several seconds and must never block the panel.


def known_audio_devices() -> list[tuple[str, str]]:
    """Devices bluetoothd already knows about (paired before, or seen in a
    previous scan this boot) that look like audio gear. Fast — no scan.
    """
    out: list[tuple[str, str]] = []
    for line in _bt("devices").splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if not match:
            continue
        mac, name = match.group(1), match.group(2).strip()
        if _is_audio(mac):
            out.append((mac, name))
    return out


def _has_real_name(mac: str, name: str) -> bool:
    """bluetoothctl lists a device by its bare MAC as a placeholder before
    a real name arrives from the remote device (or never, for anonymous BLE
    beacons) — filtering these out keeps transient scan noise off the
    picker without needing to know anything about the device's declared
    class. Most lightbulbs/sensors/trackers never get a name at all and are
    filtered out for free by this alone."""
    return name.strip().upper() != mac.strip().upper()


def scan_nearby_devices(seconds: int = 10) -> list[tuple[str, str]]:
    """Actively scan for ANY nearby named device — not filtered by the
    audio Class-of-Device heuristic :func:`known_audio_devices` uses.

    That heuristic is deliberately strict for the "already known" quick
    list, where it is virtually always right. It also once silently hid a
    real device: a car's hands-free Bluetooth kit did not report a Class
    the audio regex recognized, so it never appeared in the picker no
    matter how long the scan ran, with the car sitting right there in
    pairing mode the whole time. The point of pressing the BT key at all is
    "I am trying to connect something right now" — a slightly longer list
    of real, named, nearby devices is a far better trade than silently
    hiding the one you actually came here for.

    Blocking — always call this off the render/input thread; see
    :func:`scan_async`. 10s (vs. the older 6s) gives classic Bluetooth
    inquiry — which a car head unit uses, not BLE advertising — a more
    realistic chance to actually complete a full cycle.
    """
    subprocess.run([BTCTL, "--timeout", str(seconds), "scan", "on"],
                   capture_output=True, text=True, timeout=seconds + 8, check=False)
    out: list[tuple[str, str]] = []
    for line in _bt("devices").splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if not match:
            continue
        mac, name = match.group(1), match.group(2).strip()
        if _has_real_name(mac, name):
            out.append((mac, name))
    return out


def discover_devices(seconds: int = 10) -> list[tuple[str, str]]:
    """What the picker actually shows once a scan finishes.

    Three tiers, in order: already-known audio gear (so your regular
    headphones/speaker stay easy to find), then newly-seen devices whose
    Class of Device says Audio/Video (a car kit, a new speaker), then
    everything else named and nearby. Only 6 picker slots exist, and a
    single scan at home turned up ~25 named BLE devices (mostly smart
    bulbs) — bubbling likely-audio devices to the front means the thing you
    actually came here for is far more likely to land in a visible slot,
    without ever completely hiding anything the way the old strict filter
    did.
    """
    known = known_audio_devices()
    seen = {mac for mac, _ in known}
    nearby = [(mac, name) for mac, name in scan_nearby_devices(seconds) if mac not in seen]
    nearby.sort(key=lambda item: 0 if _is_audio(item[0]) else 1)
    return known + nearby


def scan_async(on_done: Callable[[list[tuple[str, str]]], None], seconds: int = 10) -> None:
    def _run() -> None:
        on_done(discover_devices(seconds))

    threading.Thread(target=_run, daemon=True, name="bt-scan").start()


def pair_and_connect_blocking(mac: str, sink_wait_seconds: float = 10.0) -> bool:
    """Full pair/trust/connect dance for a device that may not be paired
    yet — the picker's "connect" action. Superset of :func:`connect_blocking`
    (which assumes pairing already happened); this checks first and only
    pairs if needed, so it is safe to use for both a brand new device and
    one already paired but disconnected.
    """
    from .outputs import PipeWireControl

    _bt("power", "on")
    _bt("agent", "on")
    _bt("default-agent")
    _bt("pairable", "on")

    if _device_info(mac).get("Paired") != "yes":
        out = _bt("pair", mac, timeout=40)
        if "Failed to pair" in out or "not available" in out.lower():
            LOG.warning("bluetoothctl pair %s failed: %s", mac,
                        out.strip().splitlines()[-1] if out.strip() else "?")
            return False

    _bt("trust", mac)
    if not _connect_with_retry(mac):
        return False

    pw = PipeWireControl()
    deadline = time.monotonic() + sink_wait_seconds
    while time.monotonic() < deadline:
        if pw.find_sink("bluez_output") is not None:
            return True
        time.sleep(0.5)
    LOG.warning("bluetoothctl connect %s reported success but no "
                "bluez_output sink appeared within %.0fs", mac, sink_wait_seconds)
    return False


def pair_and_connect_async(mac: str, on_done: Callable[[bool], None]) -> None:
    def _run() -> None:
        on_done(pair_and_connect_blocking(mac))

    threading.Thread(target=_run, daemon=True, name="bt-pair-connect").start()


# ---------------------------------------------------------------------------
# Reset a stuck/failed pairing — the Stream Deck "Reset" button
# ---------------------------------------------------------------------------
# `bluetoothctl remove <mac>` normally clears a device's bond entirely (it
# runs inside bluetoothd, which owns /var/lib/bluetooth/ as root, so the
# unprivileged caller's UID is irrelevant to whether the on-disk record gets
# deleted). But it has been observed live to leave a stale residual record
# behind — a device removed this way still showed up under
# /var/lib/bluetooth/<adapter>/<mac>/ afterward, and a fresh `pair` against
# that same MAC then failed immediately with `org.bluez.Error.AlreadyExists`,
# even though `bluetoothctl devices`/`paired-devices` no longer listed it.
# The only thing that reliably cleared it was restarting bluetoothd itself
# (which forces it to reload its on-disk state from scratch) — so `remove`
# is tried first since it's fast and usually sufficient, and a
# `systemctl restart bluetooth` is only used as a fallback when `remove`
# alone didn't actually make the device info disappear.
_BT_SERVICE = "bluetooth"


def _restart_bluetooth_service() -> None:
    """Requires org.freedesktop.systemd1.manage-units authorization for the
    `rpi` service user, restricted to bluetooth.service — see
    system/polkit/51-rpi-player-bluetooth.rules. Without that rule this call
    just fails silently (systemctl exits non-zero, caught by check=False)
    and reset_pairing_blocking() falls back to reporting whatever `remove`
    alone achieved."""
    try:
        subprocess.run(["systemctl", "restart", _BT_SERVICE],
                       capture_output=True, text=True, timeout=15, check=False)
        time.sleep(2)  # let bluetoothd re-init the adapter before anything else touches it
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("systemctl restart %s failed: %s", _BT_SERVICE, exc)


def list_paired_not_connected() -> list[tuple[str, str]]:
    """Devices bluetoothd considers paired/bonded but not currently
    connected — the set a "reset failed pairings" button should target. A
    device that's actively connected and working is deliberately left
    alone; this is for the "it's paired but won't come back" case, not a
    way to nuke every bond on the adapter."""
    out: list[tuple[str, str]] = []
    for line in _bt("paired-devices").splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if not match:
            continue
        mac, name = match.group(1), match.group(2).strip()
        if _device_info(mac).get("Connected") != "yes":
            out.append((mac, name))
    return out


def reset_pairing_blocking(mac: str) -> bool:
    """Clear one stuck/failed pairing so it can be paired fresh. Returns
    True once bluetoothd no longer has any record of the device (Paired,
    Bonded, and Trusted all gone) -- the caller should follow this with a
    fresh :func:`pair_and_connect_blocking` rather than assuming this alone
    reconnects anything."""
    _bt("remove", mac)
    if not _device_info(mac):
        return True
    LOG.warning("bluetoothctl remove %s left a residual record, "
                "restarting bluetoothd to force it to clear", mac)
    _restart_bluetooth_service()
    _bt("remove", mac)
    return not _device_info(mac)


def reset_failed_pairings_blocking() -> int:
    """Reset every paired-but-disconnected device in one pass. Returns how
    many were actually cleared, so the Stream Deck toast can say something
    useful ("Reset 1", "Nothing to reset") instead of a bare confirmation.
    """
    targets = list_paired_not_connected()
    if not targets:
        return 0
    cleared = 0
    for mac, _name in targets:
        _bt("remove", mac)
        if not _device_info(mac):
            cleared += 1
    if cleared < len(targets):
        # At least one device needed the heavier restart fallback -- do it
        # once for all remaining stragglers rather than once per device.
        _restart_bluetooth_service()
        cleared = 0
        for mac, _name in targets:
            _bt("remove", mac)
            if not _device_info(mac):
                cleared += 1
    return cleared


def reset_failed_pairings_async(on_done: Callable[[int], None]) -> None:
    def _run() -> None:
        on_done(reset_failed_pairings_blocking())

    threading.Thread(target=_run, daemon=True, name="bt-reset").start()
