"""SMB SERVER status -- NOT an SMB client.

PROMPT #4 part 16 was explicit about this distinction: this product's
Samba usage (system/samba/smb.conf, install.sh's `smbd`/`nmbd.service`
enable) is a passive DROP-FOLDER server that a phone/Mac/PC pushes files
INTO over the network -- there is no SMB client anywhere in this codebase
(nothing here ever mounts a remote share), and building one was
explicitly out of scope: "Do NOT create an SMB mounting/login interface
unless such a backend actually exists."

This module is just a status read for the one thing the Touch UI can
usefully show: is the drop-folder server actually up right now, so
"files copied over SMB, then press Scan" (the real workflow — see
media_index.py) has a place to surface "the drop folder isn't reachable"
instead of a silent, confusing scan that finds nothing.

Same best-effort/never-throws shape as player/wifi.py's status() --
every field degrades to its "unknown" value independently.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess

LOG = logging.getLogger(__name__)

SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"

# Matches install.sh's `systemctl enable mpd.service smbd.service
# nmbd.service avahi-daemon.service` -- the real, already-enabled unit
# names on this project's Pi image (standard Debian samba package units,
# not something this project invents).
_SMBD_UNIT = "smbd.service"
_NMBD_UNIT = "nmbd.service"

# Matches system/samba/smb.conf's share name / install.sh's mDNS
# advertisement (avahi-smb.service) -- purely a display label, not read
# from smb.conf at runtime (that file lives on the Pi's /etc, not
# necessarily on whatever machine calls this).
SHARE_NAME = "Music"


def _is_active(unit: str) -> bool | None:
    try:
        proc = subprocess.run([SYSTEMCTL, "is-active", unit],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.debug("systemctl is-active %s failed: %s", unit, exc)
        return None
    return proc.stdout.strip() == "active"


def status() -> dict:
    """Best-effort snapshot for the AirPlay/network page's SMB status
    line.

    Returns:
      available -- bool | None (None = couldn't determine, e.g. no
                   systemctl / not running under systemd at all -- this
                   module runs fine off-Pi, e.g. in this dev sandbox,
                   where it should degrade to None rather than crash)
      hostname  -- str, this machine's own hostname (matches the
                   avahi-smb.service "%h.local" address the Mac/phone
                   would actually browse to)
      share     -- str, SHARE_NAME
    """
    smbd = _is_active(_SMBD_UNIT)
    nmbd = _is_active(_NMBD_UNIT)
    available = None if (smbd is None and nmbd is None) else bool(smbd)
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "?"
    return {"available": available, "hostname": hostname, "share": SHARE_NAME}
