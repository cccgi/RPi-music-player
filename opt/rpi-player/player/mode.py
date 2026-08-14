"""Cross-process music/video mode flag.

The TourBox daemon (owns the layer state) and the Stream Deck daemon (owns
the page state) are separate processes, and only one of them (TourBox) hosts
the shared IPC bus as a server — the Stream Deck side is a pure subscriber,
with no path to publish back into it without adding a read/dispatch loop to
``BusServer`` for a single boolean flag.

A plain sentinel file is simpler and just as reliable for something this
small: whichever daemon changes mode writes it, the other polls it on its
existing loop (both already run tight, cheap poll loops for other reasons —
this adds one stat()+maybe-read, not a new thread).
"""

from __future__ import annotations

import logging
from pathlib import Path

LOG = logging.getLogger(__name__)

MUSIC = "music"
VIDEO = "video"


def read_mode(path: str) -> str:
    try:
        value = Path(path).read_text().strip()
    except OSError:
        return MUSIC
    return value if value in (MUSIC, VIDEO) else MUSIC


def write_mode(path: str, value: str) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
    except OSError as exc:
        LOG.warning("could not write mode file %s: %s", path, exc)
