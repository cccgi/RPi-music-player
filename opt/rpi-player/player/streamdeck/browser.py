"""Library browser state for the Stream Deck.

A small, deliberately dumb model: it holds a current directory, the entries in
it, and a scroll offset. All navigation is a method call; rendering and input
live in the daemon.

Design constraints that shaped this:

* **Only four visible slots.** The browser uses the keys reserved for album art
  (0-3) plus two spare keys, so it must page rather than scroll continuously.
* **Directories sort before files.** On a music library the directories are
  albums, which is what you almost always want first.
* **Picking a track queues its whole directory.** Selecting one file and
  playing only that file makes an album stop after one song, which feels
  broken. We replace the queue with the containing directory and seek to the
  chosen track.
* **No caching.** `lsinfo` on a local library is fast, and a stale listing
  after an `mpc update` is worse than a re-fetch.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

from ..mpdbus import MpdCommander

LOG = logging.getLogger(__name__)

VISIBLE_SLOTS = 4


@dataclass(frozen=True)
class Entry:
    """One row in the listing."""

    name: str          # display name (basename)
    uri: str           # full MPD URI
    is_dir: bool

    @property
    def sort_key(self) -> tuple[int, str]:
        # Directories first, then case-insensitive by name.
        return (0 if self.is_dir else 1, self.name.lower())


class LibraryBrowser:
    """
    Thread safety: this object is genuinely touched from THREE different
    threads — the Stream Deck's key-callback thread (enter/back/page_down),
    the main render/tick loop (visible/page_index/breadcrumb, several times a
    second), and the MPD idle-watcher background thread, which calls
    refresh() on its own whenever a "database" event arrives (e.g. every
    delete_current call schedules an `mpd update`, which fires one of these
    a moment later). None of that was synchronized before — found live as
    "the song shown doesn't match what plays when pressed": a refresh landing
    mid-render (path/entries/offset are three separate attributes, updated
    non-atomically across a network round-trip to MPD) could leave different
    keys in the SAME render pass reflecting different moments of browser
    state, and a press reads whatever is truest AT THAT INSTANT — which by
    then may already differ from what was on screen when the decision to
    press was made. `_lock` (an RLock, since enter/back/go_root call
    refresh() internally) makes every public method atomic with respect to
    every other one, so a render pass and a refresh can no longer tear.
    """

    def __init__(self, mpd: MpdCommander) -> None:
        self._mpd = mpd
        self._lock = threading.RLock()
        self.path = ""                      # "" is the library root
        self.offset = 0
        self.entries: list[Entry] = []
        self.refresh()

    # -- state -------------------------------------------------------------

    def refresh(self) -> None:
        with self._lock:
            raw = self._mpd.lsinfo(self.path)
            entries: list[Entry] = []
            for item in raw:
                if "directory" in item:
                    uri = item["directory"]
                    entries.append(Entry(os.path.basename(uri) or uri, uri, True))
                elif "file" in item:
                    uri = item["file"]
                    # Prefer the tagged title; fall back to the filename. WAV
                    # files in particular often carry a title derived from the
                    # filename and no artist at all.
                    name = item.get("title") or os.path.basename(uri)
                    entries.append(Entry(name, uri, False))
                # playlists are ignored for now
            entries.sort(key=lambda e: e.sort_key)
            self.entries = entries

            max_offset = max(0, len(entries) - 1)
            if self.offset > max_offset:
                self.offset = 0
            LOG.debug("browser: %r -> %d entries", self.path or "/", len(entries))

    @property
    def at_root(self) -> bool:
        with self._lock:
            return self.path == ""

    @property
    def page_count(self) -> int:
        with self._lock:
            if not self.entries:
                return 1
            return (len(self.entries) + VISIBLE_SLOTS - 1) // VISIBLE_SLOTS

    @property
    def page_index(self) -> int:
        with self._lock:
            return self.offset // VISIBLE_SLOTS if self.entries else 0

    def visible(self) -> list[Entry | None]:
        """Exactly VISIBLE_SLOTS items, padded with None."""
        with self._lock:
            window = self.entries[self.offset:self.offset + VISIBLE_SLOTS]
            return list(window) + [None] * (VISIBLE_SLOTS - len(window))

    # -- navigation --------------------------------------------------------

    def page_down(self) -> None:
        """Advance one page, wrapping at the end.

        Wrapping (rather than clamping) means a single key can cycle the whole
        listing, which matters when there are only two spare keys for
        navigation.
        """
        with self._lock:
            if len(self.entries) <= VISIBLE_SLOTS:
                return
            self.offset += VISIBLE_SLOTS
            if self.offset >= len(self.entries):
                self.offset = 0

    def page_up(self) -> None:
        with self._lock:
            if len(self.entries) <= VISIBLE_SLOTS:
                return
            self.offset -= VISIBLE_SLOTS
            if self.offset < 0:
                last = (len(self.entries) - 1) // VISIBLE_SLOTS
                self.offset = last * VISIBLE_SLOTS

    def enter(self, slot: int) -> str | None:
        """Activate the entry in ``slot``. Returns a toast string, or None.

        A directory descends. A file replaces the queue with its containing
        directory and starts playing at that file.
        """
        with self._lock:
            window = self.visible()
            if not (0 <= slot < len(window)):
                return None
            entry = window[slot]
            if entry is None:
                return None

            if entry.is_dir:
                self.path = entry.uri
                self.offset = 0
                self.refresh()
                return entry.name[:16]

            LOG.info("browser: playing %s (from %r)", entry.uri, self.path or "/")
            self._mpd.play_uri_from_directory(self.path, entry.uri)
            return entry.name[:16]

    def back(self) -> str | None:
        """Go up one level. Returns a toast, or None if already at the root."""
        with self._lock:
            if self.at_root:
                return None
            parent = os.path.dirname(self.path)
            self.path = parent
            self.offset = 0
            self.refresh()
            return os.path.basename(parent) or "Library"

    def go_root(self) -> str | None:
        """Jump straight to the top of the library in one press, however
        deep the current listing is — the complement to back()'s one-level-
        at-a-time navigation. Bound to the Stream Deck's former Album key,
        which had no use in a headless, art-less browser."""
        with self._lock:
            if self.at_root:
                return None
            self.path = ""
            self.offset = 0
            self.refresh()
            return "Library"

    # -- presentation helpers ---------------------------------------------

    def breadcrumb(self) -> str:
        with self._lock:
            return os.path.basename(self.path) or "Library"

    def is_playing_uri(self, uri: str, current_file: str) -> bool:
        return bool(current_file) and uri == current_file
