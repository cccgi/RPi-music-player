"""Pi board power draw, read from the Pi 5's onboard PMIC.

For the Video page's power-draw tile (streamdeck_daemon.py). There is
deliberately no attempt to report the Stream Deck's own current draw here --
it is a separate bus-powered USB device with no power-monitoring hardware
anywhere in this build's signal chain, so there is no honest number to show
for it. This only ever reports the Pi's own board draw.

``vcgencmd pmic_read_adc`` (Pi 5 only -- the PMIC this reads did not exist on
earlier boards) prints one line per ADC channel, each either a CURRENT rail
(``<NAME>_A current(<n>)=<amps>A``) or a VOLTAGE rail
(``<NAME>_V volt(<n>)=<volts>V``). The parenthesised index is just an ADC
channel number and does NOT line up between the two -- e.g. ``VDD_CORE_A``
is ``current(7)`` but ``VDD_CORE_V`` is ``volt(15)``. The two lists have to
be matched by the shared ``<NAME>`` prefix instead.

Different rails run at different voltages, so the rails' raw amp figures are
NOT directly comparable or summable into one meaningful "current" on their
own -- a naive sum-of-amps across rails mixes 3.3V and 0.8V domains as if
they were the same. What this module reports instead is total board POWER
(sum of V*I over every rail present on both lists), converted to an
"5V-equivalent" input current by dividing by the Pi 5's nominal 5V USB-C
input rail -- i.e. "how many mA this would be if it all came in at 5V",
which is the one figure that is actually meaningful as a single number and
comparable to a USB power meter's reading on the supply cable. It is a
computed equivalent, not a literal single ammeter reading.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

LOG = logging.getLogger(__name__)

_VCGENCMD = shutil.which("vcgencmd") or "/usr/bin/vcgencmd"

# The Pi 5 is powered over USB-C at a nominal 5V -- see this module's
# docstring for why total power is converted to an equivalent current at
# this voltage rather than summing raw rail amps.
_INPUT_VOLTAGE = 5.0

_RAIL_RE = re.compile(r"^\s*(?P<name>\S+?)_(?P<kind>[AV])\s+\w+\(\d+\)=(?P<value>[\d.]+)[AV]\s*$")


def parse_pmic_adc(text: str) -> float | None:
    """Parse ``vcgencmd pmic_read_adc`` output into total board power (W).

    Pure and hardware-free so it can be unit tested against captured real
    output. Returns ``None`` if nothing parseable was found (e.g. empty
    input, or run on a non-Pi-5 board where this command doesn't exist).
    """
    currents: dict[str, float] = {}
    voltages: dict[str, float] = {}
    for line in text.splitlines():
        match = _RAIL_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        value = float(match.group("value"))
        if match.group("kind") == "A":
            currents[name] = value
        else:
            voltages[name] = value

    if not currents:
        return None

    # Rails with a current reading but no matching voltage reading (can
    # happen if a channel is disabled/not populated) are skipped rather than
    # guessed at -- undercounting slightly is far less misleading than
    # inventing a voltage.
    watts = sum(currents[name] * voltages[name] for name in currents if name in voltages)
    return watts


def read_pi_current_ma(timeout: float = 3.0) -> float | None:
    """Total Pi board current draw in mA, as a 5V-equivalent figure.

    Returns ``None`` on any failure (command missing, times out, non-zero
    exit, unparseable output, or a board without this PMIC) rather than a
    fabricated number -- the Video page's power tile shows "--" in that
    case instead of a reading that looks real but isn't.
    """
    try:
        result = subprocess.run(
            [_VCGENCMD, "pmic_read_adc"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.debug("vcgencmd pmic_read_adc failed: %s", exc)
        return None
    if result.returncode != 0:
        LOG.debug("vcgencmd pmic_read_adc exited %d", result.returncode)
        return None

    watts = parse_pmic_adc(result.stdout)
    if watts is None:
        return None
    return (watts / _INPUT_VOLTAGE) * 1000.0
