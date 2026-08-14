"""Wi-Fi status/diagnostics for the Stream Deck AirPlay picker's Wi-Fi tiles.

Every "iffy Wi-Fi" incident traced live on this box turned out to be one of
two things: the brcmfmac driver's power-save mode sleeping the radio between
beacon intervals and dropping/delaying mDNS multicast (which is exactly what
made AirPlay speakers vanish from the picker despite excellent signal), or a
stale/duplicate DHCP lease leaving wlan0 with only an IPv6 link-local address.
Both were previously diagnosable/fixable only with a LAN cable and SSH
(`iw dev wlan0 get power_save`, `ip -4 addr show wlan0`, `nmcli` by hand).
This module is the offroad version: a status snapshot for the two tiles that
just display info, and the two fixes that actually resolved it live --
forcing a fresh DHCP lease + reassociation, and restarting NetworkManager
itself (which reloads every conf.d drop-in, including
system/networkmanager/wifi-powersave-off.conf, in case something ever
silently drops that override again).

Follows the same subprocess-wrapper/blocking+async-pair shape as bt.py.
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

NMCLI = shutil.which("nmcli") or "/usr/bin/nmcli"
IW = shutil.which("iw") or "/usr/sbin/iw"
IFACE = "wlan0"


def _run(argv: list[str], timeout: float = 8.0) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.debug("%s failed: %s", " ".join(argv), exc)
        return ""


def status() -> dict:
    """Best-effort snapshot for the two display tiles.

    Returns a dict with:
      connected -- bool, whether nmcli reports an active Wi-Fi connection
      ssid      -- str or None
      signal    -- int 0-100 or None (nmcli's own signal quality estimate)
      ip        -- str or None (IPv4 address, no /prefix)
      powersave -- "on" / "off" / None (None if `iw` couldn't be read at all,
                   e.g. interface down) -- "on" is the misconfigured state
                   that caused the original AirPlay-vanishing bug.
    Every field degrades to its "unknown" value independently rather than
    the whole thing throwing, since a Wi-Fi tile that crashes the render
    loop over a transient `nmcli` hiccup would be worse than a blank field.
    """
    result: dict = {"connected": False, "ssid": None, "signal": None,
                    "ip": None, "powersave": None}

    # `-t` (terse) with a fixed field order survives locale/column-width
    # changes that would break screen-scraping plain `nmcli dev wifi`.
    for line in _run([NMCLI, "-t", "-f", "active,ssid,signal", "dev", "wifi"]).splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "yes":
            result["connected"] = True
            result["ssid"] = parts[1] or None
            try:
                result["signal"] = int(parts[2])
            except ValueError:
                pass
            break

    ip_out = _run([NMCLI, "-g", "IP4.ADDRESS", "device", "show", IFACE]).strip()
    if ip_out:
        first_line = ip_out.splitlines()[0].strip()
        result["ip"] = first_line.split("/")[0] or None

    ps_match = re.search(r"Power save:\s*(on|off)", _run([IW, "dev", IFACE, "get", "power_save"]))
    if ps_match:
        result["powersave"] = ps_match.group(1)

    return result


def reconnect_blocking() -> bool:
    """Force a fresh DHCP lease + reassociation: `nmcli device disconnect`
    then `connect`. Deliberately NetworkManager-native rather than a raw
    `ip link`/`iw` toggle that could drift out of sync with what NM itself
    thinks the radio state is -- same reasoning as the existing radio
    on/off toggle in actions.py. This is the fix for "connected but flaky"
    and stale-lease symptoms; it does NOT touch power-save (see
    :func:`restart_networking_blocking` for that).
    """
    _run([NMCLI, "device", "disconnect", IFACE], timeout=10)
    time.sleep(1)
    out = _run([NMCLI, "device", "connect", IFACE], timeout=20)
    return "error" not in out.lower() and out.strip() != ""


def reconnect_async(on_done: Callable[[bool], None]) -> None:
    def _run_() -> None:
        on_done(reconnect_blocking())

    threading.Thread(target=_run_, daemon=True, name="wifi-reconnect").start()


def restart_networking_blocking() -> bool:
    """Restart NetworkManager itself. Reloads every /etc/NetworkManager/
    conf.d/*.conf drop-in -- including wifi-powersave-off.conf -- and forces
    a full re-init of the Wi-Fi stack, so this also re-asserts the
    power-save-disable fix in case an NM update, factory reset, or manual
    `nmtui` edit ever silently dropped it. The heavier fallback for when a
    plain reconnect doesn't help.

    Needs org.freedesktop.systemd1.manage-units authorization for
    NetworkManager.service specifically -- see
    system/polkit/52-rpi-player-network.rules. Without that rule this just
    fails (non-zero exit, caught below) rather than hanging, since there is
    no session/agent for the unattended `rpi` service account to answer an
    interactive polkit prompt.
    """
    try:
        proc = subprocess.run(["systemctl", "restart", "NetworkManager"],
                              capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("systemctl restart NetworkManager failed: %s", exc)
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        LOG.warning("systemctl restart NetworkManager exited %d: %s",
                    proc.returncode, detail[-1] if detail else "no output")
        return False
    time.sleep(3)  # give NM a moment to reassociate before anything else checks status
    return True


def restart_networking_async(on_done: Callable[[bool], None]) -> None:
    def _run_() -> None:
        on_done(restart_networking_blocking())

    threading.Thread(target=_run_, daemon=True, name="wifi-restart").start()
