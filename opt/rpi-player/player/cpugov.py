"""CPU frequency governor control -- cut clock speed (and so heat) while the
Pi is idle, restore full speed instantly the moment it's needed again.

Reported live: the box runs noticeably warm sitting doing nothing between
songs/videos. The kernel's default governor on this board is "ondemand",
which already scales down under light load but still permits full clock
speed at any moment -- switching to "powersave" while genuinely idle (no
playback, no recent key press) pins every core to its minimum frequency
instead, and switching back is a plain sysfs write with no latency worth
noticing (see streamdeck_daemon.py's _apply_low_power / the wake handling in
_on_key, which restores it on any key press or as soon as playback resumes).

Writing ``scaling_governor`` normally requires root. This build's daemons run
as the unprivileged ``rpi`` service user, so system/tmpfiles.d/rpi-player-
cpufreq.conf grants that user's group write access to these sysfs nodes at
every boot (they are kernel-created, not real files -- a one-time chmod would
not survive a reboot). Without that file installed, every write here fails
and read_governor()/set_governor() degrade to harmless no-ops (see their
docstrings) rather than crashing the daemon.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOG = logging.getLogger(__name__)

_CPU_ROOT = Path("/sys/devices/system/cpu")

# Pins every core to its lowest frequency. The most aggressive of the
# available governors that still leaves the CPU fully usable (as opposed to
# e.g. actually suspending/halting anything) -- appropriate here because
# "idle" specifically means no active playback, so a `mpc play`/key press
# needing to spin the CPU back up promptly is the whole point.
LOW_POWER_GOVERNOR = "powersave"


def _governor_paths() -> list[Path]:
    return sorted(_CPU_ROOT.glob("cpu[0-9]*/cpufreq/scaling_governor"))


def read_governor() -> str | None:
    """The current governor (read from cpu0; every core is kept in sync by
    this module, so any one of them represents the whole board).

    Returns None if cpufreq isn't present at all -- a dev sandbox, a kernel
    without cpufreq, anything not an actual Pi -- so callers can degrade
    gracefully instead of crashing on a board this feature doesn't apply to.
    """
    paths = _governor_paths()
    if not paths:
        return None
    try:
        return paths[0].read_text().strip()
    except OSError as exc:
        LOG.debug("read scaling_governor failed: %s", exc)
        return None


def set_governor(name: str) -> bool:
    """Set every CPU core's governor to ``name``.

    Returns True only if EVERY core's write succeeded -- a partial write
    (some cores changed, some not, e.g. because the tmpfiles.d permission
    fix was never installed) is treated as a failure by the caller rather
    than silently leaving the board in a mixed state and claiming success.
    """
    paths = _governor_paths()
    if not paths:
        return False
    ok = True
    for path in paths:
        try:
            path.write_text(name)
        except OSError as exc:
            LOG.warning(
                "failed to set governor on %s -> %r: %s "
                "(is system/tmpfiles.d/rpi-player-cpufreq.conf installed?)",
                path, name, exc,
            )
            ok = False
    return ok
