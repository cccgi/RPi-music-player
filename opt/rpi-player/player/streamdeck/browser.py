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
                    # .strip() before the truthiness check -- some files in
                    # the wild carry a Title tag that is literally a single
                    # space (blank-but-not-empty), which is truthy in Python
                    # and used to win over the real filename here. Rendered
                    # as: a music-note icon with NO visible label, since
                    # render._wrap()'s word-splitting turns " " into zero
                    # words. Reproduced live against 31 real .wav files on
                    # the USB library this way. A whitespace-only title is
                    # exactly as useless as no title at all, so it now falls
                    # through to the filename same as a missing tag would.
                    name = (item.get("title") or "").strip() or os.path.basename(uri)
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


# ---------------------------------------------------------------------------
# Page-2 grid browsers — a bigger, unified 22-slot browser (see
# layout.LAYOUT_BROWSE / LAYOUT_VIDEO_BROWSE) shared between the music and
# video "page 2" overlays. Deliberately NOT LibraryBrowser subclasses —
# LibraryBrowser's 4-slot behaviour and Music page 1's browse_entry/
# browse_back/browse_root/browse_page keys are explicitly frozen (a
# different, existing feature); this is new state for a new, bigger grid,
# just built on the same "dumb model, lock every public method" pattern for
# the same thread-safety reasons documented on LibraryBrowser above (the
# Stream Deck key-callback thread, the render/tick loop, and the MPD
# idle-watcher thread all touch this too).
# ---------------------------------------------------------------------------

# 19, not 21 -- row 2's content span was shrunk from 6 slots to 4 (index(2,1)
# and index(2,6) freed up for the Curate/Delete keys added directly to the
# browse grid) — see layout.py's GRID_SLOT_KEYS comment for the full
# geometry change.
GRID_VISIBLE_SLOTS = 19


@dataclass(frozen=True)
class GridEntry:
    """One row in a page-2 grid listing."""

    name: str          # display name (basename)
    is_dir: bool
    ref: str           # MPD URI (music) or absolute filesystem path (video)


class _GridBrowserBase:
    """Shared pagination/navigation behind MusicGridBrowser and
    VideoGridBrowser — the 21-content-slot windowing, page wrap and ".."
    handling are identical between the two; only how ``entries`` gets
    (re)populated for a given ``path``, and what "enter a file" means,
    differ per mode. Subclasses implement :meth:`refresh`.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.path = ""                      # "" is the library root
        self.offset = 0
        self.entries: list[GridEntry] = []

    def refresh(self) -> None:
        raise NotImplementedError

    @property
    def at_root(self) -> bool:
        with self._lock:
            return self.path == ""

    @property
    def page_count(self) -> int:
        with self._lock:
            if not self.entries:
                return 1
            return (len(self.entries) + GRID_VISIBLE_SLOTS - 1) // GRID_VISIBLE_SLOTS

    @property
    def page_index(self) -> int:
        with self._lock:
            return self.offset // GRID_VISIBLE_SLOTS if self.entries else 0

    def visible(self) -> list[GridEntry | None]:
        """Exactly GRID_VISIBLE_SLOTS items, padded with None."""
        with self._lock:
            window = self.entries[self.offset:self.offset + GRID_VISIBLE_SLOTS]
            return list(window) + [None] * (GRID_VISIBLE_SLOTS - len(window))

    def page_down(self) -> None:
        """Advance one page, wrapping at the end — same semantics as
        LibraryBrowser.page_down()."""
        with self._lock:
            if len(self.entries) <= GRID_VISIBLE_SLOTS:
                return
            self.offset += GRID_VISIBLE_SLOTS
            if self.offset >= len(self.entries):
                self.offset = 0

    def page_up(self) -> None:
        with self._lock:
            if len(self.entries) <= GRID_VISIBLE_SLOTS:
                return
            self.offset -= GRID_VISIBLE_SLOTS
            if self.offset < 0:
                last = (len(self.entries) - 1) // GRID_VISIBLE_SLOTS
                self.offset = last * GRID_VISIBLE_SLOTS

    def back(self) -> str | None:
        """Go up one level. Returns a toast, or None if already at the
        root — the ".." slot is inert there, same as LibraryBrowser.back()."""
        with self._lock:
            if self.at_root:
                return None
            parent = os.path.dirname(self.path)
            self.path = parent
            self.offset = 0
            self.refresh()
            return os.path.basename(parent) or "Library"

    def goto(self, path: str) -> None:
        """Jump straight to ``path`` (an MPD-relative URI for music, a
        library-relative path for video), resetting to page 0. Used both
        when entering page 2 (mirroring wherever page 1's own browser is
        currently positioned, or the library root for video — see
        streamdeck_daemon's enter_page2/enter_video_page2 handlers) and by
        the page-2 Internal/USB quick-select keys, which jump straight back
        to "" (root) after switching storage source so the grid reflects
        the new source immediately rather than showing a now-stale path
        from whichever source was active before."""
        with self._lock:
            self.path = path
            self.offset = 0
            self.refresh()


class MusicGridBrowser(_GridBrowserBase):
    """Page 2's music folder/file browser — same MPD ``lsinfo`` approach as
    LibraryBrowser, just a 21-entry content window instead of 4.
    """

    def __init__(self, mpd: MpdCommander) -> None:
        super().__init__()
        self._mpd = mpd
        self.refresh()

    def refresh(self) -> None:
        with self._lock:
            raw = self._mpd.lsinfo(self.path)
            entries: list[GridEntry] = []
            for item in raw:
                if "directory" in item:
                    uri = item["directory"]
                    entries.append(GridEntry(os.path.basename(uri) or uri, True, uri))
                elif "file" in item:
                    uri = item["file"]
                    # .strip() before the truthiness check -- some files in
                    # the wild carry a Title tag that is literally a single
                    # space (blank-but-not-empty), which is truthy in Python
                    # and used to win over the real filename here. Rendered
                    # as: a music-note icon with NO visible label, since
                    # render._wrap()'s word-splitting turns " " into zero
                    # words. Reproduced live against 31 real .wav files on
                    # the USB library this way. A whitespace-only title is
                    # exactly as useless as no title at all, so it now falls
                    # through to the filename same as a missing tag would.
                    name = (item.get("title") or "").strip() or os.path.basename(uri)
                    entries.append(GridEntry(name, False, uri))
            entries.sort(key=lambda e: (0 if e.is_dir else 1, e.name.lower()))
            self.entries = entries

            max_offset = max(0, len(entries) - 1)
            if self.offset > max_offset:
                self.offset = 0

    def enter(self, slot: int) -> str | None:
        """Activate the entry in ``slot`` (0-based within the visible
        window). A directory descends (resetting to page 0); a file
        replaces the queue with its containing directory and starts
        playing at that file — the exact same MPD call as
        LibraryBrowser.enter()'s file branch."""
        with self._lock:
            window = self.visible()
            if not (0 <= slot < len(window)):
                return None
            entry = window[slot]
            if entry is None:
                return None

            if entry.is_dir:
                self.path = entry.ref
                self.offset = 0
                self.refresh()
                return entry.name[:16]

            LOG.info("grid browser: playing %s (from %r)", entry.ref, self.path or "/")
            self._mpd.play_uri_from_directory(self.path, entry.ref)
            return entry.name[:16]

    def goto_now_playing(self, current_file: str) -> None:
        """Jump straight to the directory containing ``current_file`` (an
        MPD-relative URI), or the library root if nothing is playing —
        called when page 2 is entered, per the confirmed design: it always
        opens positioned at what's currently playing, never "remembers"
        the last-visited page-2 location."""
        with self._lock:
            self.path = os.path.dirname(current_file) if current_file else ""
            if self.path == ".":
                self.path = ""
            self.offset = 0
            self.refresh()


class VideoGridBrowser(_GridBrowserBase):
    """Page 2's video folder/file browser. Unlike MusicGridBrowser, there is
    no MPD database to list against — entries come from a live
    ``VideoLibrary.list_dir()`` (an ``os.scandir`` of the current directory,
    filtered to the library's already-configured video extensions).
    """

    def __init__(self, library) -> None:
        super().__init__()
        self._library = library
        self.refresh()

    def refresh(self) -> None:
        with self._lock:
            rows = self._library.list_dir(self.path)
            self.entries = [GridEntry(name, is_dir, full) for name, is_dir, full in rows]

            max_offset = max(0, len(self.entries) - 1)
            if self.offset > max_offset:
                self.offset = 0

    def enter(self, slot: int) -> str | tuple[str, str, str] | None:
        """Activate the entry in ``slot``. A directory descends (resetting
        to page 0) and returns a toast string, same shape as
        MusicGridBrowser.enter(). A file cannot be played from inside this
        "dumb" browser (playing a video needs the VideoCommander/router/
        resume-position machinery the daemon already owns for the up-next
        slots — see streamdeck_daemon._play_video_path) — instead this
        returns a ``("play", absolute_path, toast)`` tuple for the daemon
        to act on.
        """
        with self._lock:
            window = self.visible()
            if not (0 <= slot < len(window)):
                return None
            entry = window[slot]
            if entry is None:
                return None

            if entry.is_dir:
                self.path = os.path.relpath(entry.ref, self._library.root)
                if self.path == ".":
                    self.path = ""
                self.offset = 0
                self.refresh()
                return entry.name[:16]

            return ("play", entry.ref, entry.name[:16])

    def goto_now_playing(self) -> None:
        """Jump straight to the directory containing whatever's currently
        playing (VideoLibrary.current), or the library root if nothing is
        loaded — same "always jump to now-playing" entry behaviour as
        MusicGridBrowser.goto_now_playing()."""
        with self._lock:
            current = self._library.current
            if current is None:
                self.path = ""
            else:
                rel = os.path.relpath(current.path, self._library.root)
                self.path = os.path.dirname(rel)
                if self.path == ".":
                    self.path = ""
            self.offset = 0
            self.refresh()
