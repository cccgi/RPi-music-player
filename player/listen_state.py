"""Per-song listen-time and folder-checkpoint tracking.

ListenState is an in-process singleton (not shared with the TourBox daemon)
that accumulates two things:

  * ``listen_seconds(uri)`` — how many seconds the user has actively listened
    to a song this session (state == "play").  Used to shade cards yellow when
    ≥ 30 s have passed so the user knows they've already heard it.

  * ``checkpoint(folder)`` — the last URI that was playing inside a given
    folder.  Used to shade that card purple when re-entering the folder so the
    user can instantly see where they left off.

Both are persisted to a JSON file so they survive daemon restarts.  The file
lives at ``~/.local/share/rpi-player/listen_state.json``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

_STATE_FILE = Path.home() / ".local" / "share" / "rpi-player" / "listen_state.json"

# Minimum accumulated listen time for the yellow "heard" shade.
LISTENED_THRESHOLD_SECS = 30.0

# How often the daemon should call save() while playing (seconds).
SAVE_INTERVAL_SECS = 60.0


class ListenState:
    """Thread-safe listen-time and checkpoint tracker.

    Designed to be updated from the render-loop thread (``tick``) and queried
    from the same thread (``card_shade``), with ``save`` occasionally called
    from a background timer.  All public methods acquire ``_lock``.
    """

    def __init__(self, state_file: Path = _STATE_FILE) -> None:
        self._file = state_file
        self._lock = threading.Lock()
        # uri → accumulated seconds listened
        self._listen: dict[str, float] = {}
        # folder_uri → last-playing song uri (checkpoint)
        self._checkpoint: dict[str, str] = {}
        # Internal tick state — (current_uri, monotonic_time_of_last_tick)
        self._current: tuple[str, float] | None = None
        self._load()

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._file) as f:
                data = json.load(f)
            self._listen = {str(k): float(v) for k, v in data.get("listen", {}).items()}
            self._checkpoint = {str(k): str(v) for k, v in data.get("checkpoint", {}).items()}
            LOG.debug("ListenState: loaded %d songs, %d checkpoints",
                      len(self._listen), len(self._checkpoint))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def save(self) -> None:
        """Write current state to disk.  Call periodically; never blocks the
        render loop (fast even with thousands of entries)."""
        with self._lock:
            data = {"listen": dict(self._listen), "checkpoint": dict(self._checkpoint)}
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._file)
        except OSError as exc:
            LOG.warning("ListenState: save failed: %s", exc)

    # -------------------------------------------------------------------------
    # State updates (called every tick from render loop)
    # -------------------------------------------------------------------------

    def tick(self, uri: str | None, is_playing: bool) -> None:
        """Accumulate listen time.  Call once per second from the render loop.

        Only counts when ``is_playing`` is True and ``uri`` is non-empty.
        Handles song changes and pauses correctly — no double-counting.
        """
        with self._lock:
            if not is_playing or not uri:
                self._current = None
                return
            now = time.monotonic()
            if self._current and self._current[0] == uri:
                # Same song, same play session — add real elapsed wall time
                self._listen[uri] = self._listen.get(uri, 0.0) + (now - self._current[1])
                self._current = (uri, now)
            else:
                # Song changed or playback resumed — start fresh
                self._current = (uri, now)

    def set_checkpoint(self, folder: str, uri: str) -> None:
        """Remember that ``uri`` was the last song playing in ``folder``.

        Called whenever the current song changes so the bookmark always reflects
        the most recent position even without an explicit navigation event.
        """
        with self._lock:
            self._checkpoint[folder] = uri

    # -------------------------------------------------------------------------
    # Queries (called from render loop to compute card shade)
    # -------------------------------------------------------------------------

    def listen_seconds(self, uri: str) -> float:
        with self._lock:
            return self._listen.get(uri, 0.0)

    def is_listened(self, uri: str) -> bool:
        return self.listen_seconds(uri) >= LISTENED_THRESHOLD_SECS

    def checkpoint(self, folder: str) -> str | None:
        with self._lock:
            return self._checkpoint.get(folder)

    @staticmethod
    def is_curated(uri: str) -> bool:
        """True if 'Favorites' appears anywhere in the URI path components.

        A song is considered curated iff it lives inside a Favorites folder,
        no matter how deep — this is consistent with how curate_to_favorites
        places songs and how the reverse-curate action detects them.
        """
        return "Favorites" in Path(uri).parts

    def card_shade(
        self, uri: str, is_playing: bool, folder: str, is_dir: bool
    ) -> str | None:
        """Return the background shade code for this browser/grid card.

        Priority (highest wins):
          ``"blue"``   — currently playing
          ``"green"``  — song lives inside a Favorites folder (curated)
          ``"purple"`` — last position checkpoint for this folder
          ``"yellow"`` — listened ≥ 30 s this session
          ``None``     — default (no tint)

        Folders never get a shade — only songs do.
        """
        if is_dir:
            return None
        if is_playing:
            return "blue"
        if self.is_curated(uri):
            return "green"
        folder_norm = folder.rstrip("/")
        if self.checkpoint(folder_norm) == uri:
            return "purple"
        if self.is_listened(uri):
            return "yellow"
        return None
