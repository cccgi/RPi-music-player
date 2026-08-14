"""Continuous playback across folders, for headless operation.

The problem this solves: MPD plays a *queue*. When the queue runs out it stops.
Loading one folder at a time — which is what the browser and the folder-jump
actions do — therefore gives you silence at the end of every folder, with no
screen to tell you why.

This watcher notices the queue running dry and loads the next folder.

Two MPD settings interact badly with this and are actively defended against:

``consume``
    Removes each track from the queue once played. It drains the queue as you
    listen, so:
      * playback stops early and unpredictably, and
      * ``previous`` has nothing to go back to and merely restarts the track.
    Both symptoms look like bugs in the controller. Consume is forced off at
    startup unless explicitly allowed in config.

``single``
    Stops after every track. Same silence, different cause.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .mpdbus import MpdCommander, MpdWatcher

LOG = logging.getLogger(__name__)


class ContinuousPlayback:
    """Advance to the next library folder when the queue is exhausted.

    Runs its own MPD ``idle`` connection on a daemon thread so it works with
    the Stream Deck unplugged — this is headless-critical behaviour and must
    not live in the display daemon.
    """

    def __init__(
        self,
        mpd: MpdCommander,
        host: str,
        port: int,
        timeout: float = 10.0,
        enabled: bool = True,
        force_consume_off: bool = True,
    ) -> None:
        self._mpd = mpd
        self._enabled = enabled
        self._force_consume_off = force_consume_off
        self._watcher = MpdWatcher(host, port, timeout)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_state = ""
        # Guards against a runaway loop when every remaining folder is empty
        # or unplayable: we will not chain more than this many advances
        # without a track actually starting.
        self._consecutive_advances = 0
        self._max_consecutive = 8

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            LOG.info("continuous playback disabled by config")
            return
        self.enforce_modes()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="continuous")
        self._thread.start()
        LOG.info("continuous playback watcher started")

    def stop(self) -> None:
        self._stop.set()
        self._watcher.stop()

    def enforce_modes(self) -> None:
        """Turn off the modes that silently break continuous play.

        Done at startup and again whenever we see them come back on, because
        they are one Stream Deck key press away and the failure they cause
        (playback stopping early, previous not working) is not obviously
        connected to the setting.
        """
        if not self._force_consume_off:
            return
        status = self._mpd.status()
        if status.get("consume") == "1":
            LOG.warning(
                "consume was ON — turning it off. It drains the queue as you "
                "listen, which stops playback early and leaves 'previous' with "
                "nothing to go back to."
            )
            self._mpd.set_consume(False)
        if status.get("single") == "1":
            LOG.warning("single was ON — turning it off (it stops after every track)")
            self._mpd.set_single(False)

    # -- the watcher -------------------------------------------------------

    def _run(self) -> None:
        for subsystems in self._watcher.events(["player", "options"]):
            if self._stop.is_set():
                break
            try:
                if "options" in subsystems:
                    self.enforce_modes()
                if "player" in subsystems or "__reconnected__" in subsystems:
                    self._on_player_change()
            except Exception:  # noqa: BLE001 - never kill the watcher thread
                LOG.exception("continuous playback watcher error")

    def _on_player_change(self) -> None:
        status = self._mpd.status()
        state = status.get("state", "")

        if state == "play":
            self._consecutive_advances = 0
        self._last_state, previous = state, self._last_state

        # Only act on a genuine play -> stop transition. A user-initiated stop
        # also lands here, which is why we additionally require the queue to be
        # exhausted before doing anything.
        if state != "stop" or previous != "play":
            return

        try:
            length = int(status.get("playlistlength", "0"))
        except (TypeError, ValueError):
            length = 0

        # A GENUINELY exhausted queue that just finished still has its songs
        # listed (MPD doesn't remove them on stop, only `consume` mode does
        # that) -- so length is the original nonzero count. length == 0 here
        # means the queue was actively CLEARED, not that it played to the
        # end, and that happens on every ordinary folder jump, library browse,
        # and delete: `clear_queue()` + `add()` + `play_position()` all pass
        # through a real, observable state == "stop", playlistlength == "0"
        # moment before the `add` lands. Found live: queued the whole library
        # (260 tracks) and had this watcher silently swap it for an unrelated
        # folder about a second later, having sampled MPD's status in exactly
        # that gap. This is the mechanism behind "I pressed play on one
        # thing and something else started" whenever it raced a folder
        # switch triggered by anything else at the same time.
        if length == 0:
            LOG.debug("stop with an empty queue — likely mid-reload elsewhere, not a real "
                      "end-of-queue; ignoring without even a debounce")
            return

        # MPD clears `song` when it stops at the end. If a song position is
        # still reported and it is not the last entry, the user pressed stop.
        song = status.get("song")
        if song is not None and int(song) < length - 1:
            LOG.debug("stopped mid-queue; leaving it alone")
            return

        # Debounce: even with the length==0 guard above, a reload elsewhere
        # can still be sampled AFTER its `add` lands but BEFORE its
        # `play_position` actually resumes playback — a moment that looks
        # identical to a genuine "reached the end, stopped" from here. Wait
        # briefly and re-check: any real reload (folder jump, browser press,
        # delete's own advance) completes well within this window, so if
        # something is now actually playing, or the queue no longer matches
        # what we just sampled, this was a false alarm — abandon it and let
        # whatever ELSE is driving the queue keep driving it.
        self._stop.wait(0.4)
        if self._stop.is_set():
            return
        recheck = self._mpd.status()
        if recheck.get("state") == "play":
            LOG.debug("false alarm — something else already resumed playback")
            return
        try:
            recheck_length = int(recheck.get("playlistlength", "0"))
        except (TypeError, ValueError):
            recheck_length = 0
        if recheck_length != length:
            LOG.debug("false alarm — queue length changed underneath us (%d -> %d)",
                      length, recheck_length)
            return

        if self._consecutive_advances >= self._max_consecutive:
            LOG.warning(
                "advanced %d folders without playing anything; stopping to "
                "avoid a loop", self._consecutive_advances,
            )
            return

        self._consecutive_advances += 1
        self._advance()

    def _advance(self) -> None:
        """Load the folder after the one we just finished, and play it."""
        from .actions import library_folders_for  # local import: avoids a cycle

        folders = library_folders_for(self._mpd)
        if not folders:
            LOG.info("queue exhausted but the library has no folders")
            return

        last_uri = self._last_played_uri()
        current_dir = str(Path(last_uri).parent) if last_uri else ""
        if current_dir in (".", "/"):
            current_dir = ""

        index = folders.index(current_dir) + 1 if current_dir in folders else 0
        index %= len(folders)                     # wrap: the library loops
        target = folders[index]

        LOG.info("queue exhausted -> advancing to folder %r", target)
        self._mpd.clear_queue()
        self._mpd.add(target)
        self._mpd.play_position(0)

    def _last_played_uri(self) -> str:
        """URI of the track that just finished.

        ``currentsong`` is empty once MPD stops at the end of the queue, so we
        read the last entry of the queue instead — the queue is still intact
        because consume is forced off.
        """
        song = self._mpd.current_song()
        if song.get("file"):
            return song["file"]
        playlist = self._mpd.playlist_info()
        return playlist[-1].get("file", "") if playlist else ""
