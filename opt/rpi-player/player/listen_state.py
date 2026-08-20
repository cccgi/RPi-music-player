"""Per-song listen-time and folder-checkpoint tracking.

ListenState is an in-process singleton (not shared with the TourBox daemon)
that accumulates two things:

  * ``listen_seconds(uri)`` — how many seconds the user has actively listened
    to a song this session (state == "play").  Used to shade cards yellow when
    ≥ 30 s have passed so the user knows they've already heard it.

  * ``checkpoint(folder)`` — the last URI that was playing inside a given
    folder.  Used to shade that card purple when re-entering the folder so the
    user can instantly see where they left off.

Both are persisted to a JSON file AND to MPD stickers so they survive daemon
restarts, Pi reboots, and even a full reflash (stickers live in MPD's own
database alongside the music files).

Sticker key: ``rpi-listen-secs``  (float seconds as a string, e.g. "47.3")

On startup:
  1. JSON file is loaded first (fast, in-process).
  2. ``load_stickers(mpd)`` merges MPD sticker values — takes the MAX of the
     two so whichever source has more time wins (no data is lost if the JSON
     and sticker databases diverge).

On save (every ~60 s):
  1. JSON file is written atomically.
  2. ``save_stickers(mpd)`` writes/updates stickers for every URI whose
     accumulated time has changed since the last sticker flush.

Mac color tags (internal storage only):
  ``sync_mac_tags(music_root)`` bulk-applies yellow xattrs to all songs that
  have crossed the listened threshold and are not already curated (green).
  This is called from the daemon's periodic save path.
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

# MPD sticker key used to persist listen time per song.
_STICKER_KEY = "rpi-listen-secs"

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
        # uri → seconds value at the time we last wrote it to MPD stickers
        # (so we only issue sticker set commands when the value actually changed)
        self._sticker_flushed: dict[str, float] = {}
        # Internal tick state — (current_uri, monotonic_time_of_last_tick)
        self._current: tuple[str, float] | None = None
        self._load()

    # -------------------------------------------------------------------------
    # Persistence — JSON
    # -------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._file) as f:
                data = json.load(f)
            self._listen = {str(k): float(v) for k, v in data.get("listen", {}).items()}
            self._checkpoint = {str(k): str(v) for k, v in data.get("checkpoint", {}).items()}
            LOG.debug("ListenState: loaded %d songs, %d checkpoints from JSON",
                      len(self._listen), len(self._checkpoint))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def save(self) -> None:
        """Write current state to disk atomically."""
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
    # Persistence — MPD stickers
    # -------------------------------------------------------------------------

    def load_stickers(self, mpd) -> int:
        """Merge MPD sticker values into the in-memory listen table.

        Takes the MAX of (JSON value, sticker value) for each URI so no time
        is lost if the two databases diverged (e.g. after a crash).

        Returns the number of URIs updated from stickers.
        """
        updated = 0
        try:
            # sticker find returns a list of dicts: {"file": uri, "sticker": "key=value"}
            results = mpd._call("sticker_find", "song", "", _STICKER_KEY)
            if not results:
                return 0
            with self._lock:
                for item in results:
                    uri = item.get("file", "")
                    raw = item.get("sticker", "")
                    if not uri or not raw:
                        continue
                    # raw is "rpi-listen-secs=47.3"
                    if "=" in raw:
                        raw = raw.split("=", 1)[1]
                    try:
                        secs = float(raw)
                    except ValueError:
                        continue
                    current = self._listen.get(uri, 0.0)
                    if secs > current:
                        self._listen[uri] = secs
                        self._sticker_flushed[uri] = secs
                        updated += 1
            if updated:
                LOG.info("ListenState: merged %d sticker values from MPD", updated)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("ListenState: load_stickers failed (MPD may not support stickers): %s", exc)
        return updated

    def save_stickers(self, mpd) -> int:
        """Write changed listen times to MPD stickers.

        Only issues sticker set commands for URIs whose value has changed
        since the last flush — avoids hammering MPD on every save cycle.

        Returns the number of stickers written.
        """
        with self._lock:
            to_flush = {
                uri: secs
                for uri, secs in self._listen.items()
                if secs != self._sticker_flushed.get(uri, -1.0)
            }
        written = 0
        for uri, secs in to_flush.items():
            try:
                mpd._call("sticker_set", "song", uri, _STICKER_KEY, f"{secs:.1f}")
                with self._lock:
                    self._sticker_flushed[uri] = secs
                written += 1
            except Exception as exc:  # noqa: BLE001
                LOG.debug("ListenState: sticker_set failed for %s: %s", uri, exc)
        if written:
            LOG.debug("ListenState: flushed %d stickers to MPD", written)
        return written

    # -------------------------------------------------------------------------
    # Mac color tags (internal storage only)
    # -------------------------------------------------------------------------

    def sync_mac_tags(self, music_root: Path) -> int:
        """Bulk-apply yellow xattrs to all heard-but-not-curated songs.

        Only affects internal storage (ext4 supports xattrs; exFAT does not).
        Curated songs already carry a green tag — left unchanged.

        Call from the daemon's periodic save path (every ~60 s).
        Returns the number of files tagged.
        """
        try:
            from .mac_tags import sync_yellow
        except ImportError:
            LOG.warning("ListenState: mac_tags module not available")
            return 0
        with self._lock:
            listen_copy = dict(self._listen)
        return sync_yellow(music_root, listen_copy, LISTENED_THRESHOLD_SECS)

    # -------------------------------------------------------------------------
    # State updates (called every tick from render loop)
    # -------------------------------------------------------------------------

    def tick(self, uri: str | None, is_playing: bool) -> None:
        """Accumulate listen time.  Call once per render cycle from the loop.

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
        """Remember that ``uri`` was the last song playing in ``folder``."""
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
        """True if 'Favorites' appears anywhere in the URI path components."""
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
