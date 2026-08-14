"""Continuous playback across .skp files, for headless operation.

The video-mode equivalent of :mod:`continuous` (which does this for MPD
folders). Without this, video mode plays exactly one file and then just sits
there paused-at-the-end with nothing telling anyone why — same silent-stop
problem the music side already solved, different player process.

MPD has a blocking ``idle`` command to wait on; mpv's JSON IPC does not offer
anything as convenient here (it CAN push unsolicited events, but
:class:`~player.video.VideoCommander` is a simple request/response client, not
built for a separate event-reading loop), so this watcher polls instead.
Cheap enough at a low rate: one ``get_property`` round-trip over a local Unix
socket every ``poll_seconds``.
"""

from __future__ import annotations

import logging
import threading

from . import mode as _mode
from .actions import ActionContext, dispatch
from .video import VideoCommander

LOG = logging.getLogger(__name__)


class VideoContinuous:
    """Advance to the next .skp file when the current one plays to the end.

    Runs on its own daemon thread, same as :class:`continuous.ContinuousPlayback`
    — headless-critical, must work with no Stream Deck attached, so this lives
    in the TourBox daemon, not the display one.
    """

    def __init__(
        self,
        ctx: ActionContext,
        video: VideoCommander | None,
        mode_file: str,
        enabled: bool = True,
        poll_seconds: float = 1.0,
    ) -> None:
        self._ctx = ctx
        self._video = video
        self._mode_file = mode_file
        self._enabled = enabled and video is not None
        self._poll_seconds = poll_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Edge-detected, not level-detected: eof-reached stays true until the
        # next loadfile actually lands, so without this the moment right
        # after advancing would immediately look like ANOTHER finish and
        # trigger a second advance before the new file has even started.
        self._last_eof = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            LOG.info("video auto-advance disabled by config")
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="video-continuous")
        self._thread.start()
        LOG.info("video auto-advance watcher started")

    def stop(self) -> None:
        self._stop.set()

    # -- the watcher -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - never kill the watcher thread
                LOG.exception("video auto-advance watcher error")

    def _tick(self) -> None:
        assert self._video is not None

        # Only act while video mode is actually the thing on screen/in
        # control. mpv keeps its last file loaded (paused) via --keep-open
        # even after leaving video mode, or while music is playing on top of
        # it — advancing then would silently swap out the video someone is
        # about to come back to, for no visible reason.
        if _mode.read_mode(self._mode_file) != _mode.VIDEO:
            self._last_eof = False
            return

        eof = self._video.eof_reached()
        if eof and not self._last_eof:
            self._last_eof = True
            LOG.info("video finished -> advancing to next")
            dispatch("video_next_song", self._ctx, 1)
        elif not eof:
            self._last_eof = False
