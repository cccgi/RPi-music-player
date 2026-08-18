"""Video/karaoke playback: `mpv` IPC client + flat `.skp` library scanner.

Mirrors ``mpdbus.MpdCommander``'s shape deliberately (persistent connection,
lazy reconnect, one lock) — same problem, different wire protocol (mpv's
JSON-over-Unix-socket instead of MPD's text protocol).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .skp import SkpOffsets, parse_skp, subfile_url, track_title

LOG = logging.getLogger(__name__)

_RECV_CHUNK = 65536


class VideoError(Exception):
    pass


class VideoCommander:
    """Thread-safe client for mpv's JSON IPC socket.

    One request at a time (protected by a lock) — mpv's IPC is a simple
    request/response stream over one socket, not something safe to pipeline
    from multiple threads without a lot more bookkeeping than this needs.
    """

    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        self._path = socket_path
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self._req_id = 0

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            sock.connect(self._path)
            self._sock = sock
            LOG.debug("connected to mpv IPC at %s", self._path)
        return self._sock

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def command(self, args: list) -> dict:
        """Send one command, return its response dict.

        Retries once on a fresh connection if the socket had gone away —
        mpv restarting underneath us (a crash, a manual restart) should not
        require every call site to handle it.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    sock = self._ensure()
                    self._req_id += 1
                    req_id = self._req_id
                    payload = json.dumps({"command": args, "request_id": req_id}) + "\n"
                    sock.sendall(payload.encode("utf-8"))
                    return self._read_response(sock, req_id)
                except (OSError, socket.timeout) as exc:
                    LOG.debug("mpv IPC error (attempt %d): %s", attempt, exc)
                    self._drop()
                    if attempt == 2:
                        raise VideoError(f"mpv IPC unavailable: {exc}") from exc
            raise VideoError("unreachable")

    def _read_response(self, sock: socket.socket, req_id: int) -> dict:
        """Read lines until the one matching our request_id arrives.

        mpv interleaves unsolicited events (audio-reconfig, etc.) with command
        replies on the same stream — those are simply skipped here rather
        than surfaced, since nothing currently needs push-based event
        handling (state is polled instead, same model as the Stream Deck's
        own MPD `_collect_state`).
        """
        buffer = b""
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            chunk = sock.recv(_RECV_CHUNK)
            if not chunk:
                raise VideoError("mpv IPC closed the connection")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if msg.get("request_id") == req_id:
                    return msg
        raise VideoError("mpv IPC timed out waiting for a response")

    def get(self, prop: str, default=None):
        try:
            resp = self.command(["get_property", prop])
        except VideoError:
            return default
        if resp.get("error") != "success":
            return default
        return resp.get("data", default)

    def set(self, prop: str, value) -> bool:
        try:
            resp = self.command(["set_property", prop, value])
        except VideoError:
            return False
        return resp.get("error") == "success"

    # -- playback -----------------------------------------------------------

    def _wait_until_loaded(self, timeout: float = 3.0) -> None:
        """Block until mpv has actually finished opening the just-issued
        `loadfile`, not merely accepted the command.

        `loadfile`'s IPC response only confirms the command was queued —
        actual demuxer probing happens asynchronously. Firing `audio-add`
        immediately after works when mpv is idle (nothing else loading), but
        REPLACING an already-playing file races the old file's teardown: the
        `audio-add` calls can silently attach to nothing, and the load
        finishes with video only. Found live: worked loading the first song
        into an idle player, silently lost both audio tracks advancing to
        the second song. Polling `duration` (0 until the file is actually
        probed) is what closes that race.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            duration = self.get("duration", 0.0) or 0.0
            if duration > 0:
                return
            time.sleep(0.05)
        LOG.warning("mpv did not confirm file load within %.1fs — "
                    "proceeding anyway, audio tracks may not attach", timeout)

    def load_skp(self, offsets: SkpOffsets, start_track: int = 0) -> None:
        """Load a parsed .skp file: video track + all audio tracks attached.

        Audio tracks are attached in order via `audio-add`, which is what
        makes them selectable — matches the reference tool's convention
        (track 1 = vocal, track 2 = karaoke). `start_track` picks which one
        plays first (default 0 = vocal).
        """
        video_url = subfile_url(offsets.path, *offsets.video)
        resp = self.command(["loadfile", video_url])
        if resp.get("error") != "success":
            raise VideoError(f"mpv could not load video track: {resp}")
        self._wait_until_loaded()

        self._attach_audio_tracks(offsets, start_track)

        if offsets.audio:
            self._select_track(start_track + 1)  # mpv track ids are 1-based
        self.set("pause", False)

    def load_plain(self, path: str) -> None:
        """Load an ordinary video file directly — no `.skp` subfile demuxing,
        no separate audio-track attach.

        Unlike `.skp`, a plain container (`.mkv`, `.mp4`, ...) already carries
        its own audio track(s) — mpv opens and plays them natively the moment
        the file loads, the same way any of the other video formats users may
        drop into the library would. `switch_track`'s track-list cycling
        still works unmodified on whatever audio tracks the file actually
        contains; there is just no `audio-add` step to do first.
        """
        resp = self.command(["loadfile", path])
        if resp.get("error") != "success":
            raise VideoError(f"mpv could not load {path}: {resp}")
        self._wait_until_loaded()
        self.set("pause", False)

    def _attach_audio_tracks(self, offsets: SkpOffsets, start_track: int,
                              max_attempts: int = 3) -> None:
        """Attach every audio track, verifying they actually landed.

        `_wait_until_loaded` closed the specific race where the FIRST
        `audio-add` after a `loadfile` silently attached to nothing while the
        previous file was still tearing down. It turns out that same race
        can still fire here even after `duration` confirms the video demuxer
        is ready: the *audio* subfile opens are their own separate demuxer
        probes, and `audio-add`'s IPC response only confirms mpv accepted the
        command, not that the track ended up in `track-list` — found live as
        "3-4 songs fine, then 1-2 with picture but no sound," recovering on
        its own a couple of songs later once the previous file's teardown had
        fully settled.

        Reading `track-list` back after issuing the adds is what actually
        proves anything attached. If it's short, clear whatever partial state
        exists and retry the whole batch from a clean slate — safer than
        trying to figure out which individual add(s) failed, since a
        "successful" IPC response here has already been shown not to mean the
        track exists.
        """
        expected = len(offsets.audio)
        if expected == 0:
            return

        attached: list[dict] = []
        for attempt in range(1, max_attempts + 1):
            for i, (start, end) in enumerate(offsets.audio):
                audio_url = subfile_url(offsets.path, start, end)
                select = "select" if i == start_track else "auto"
                resp = self.command(["audio-add", audio_url, select, track_title(i)])
                if resp.get("error") != "success":
                    LOG.warning("audio-add track %d failed on attempt %d/%d: %s",
                                i, attempt, max_attempts, resp)

            attached = [t for t in (self.get("track-list") or []) if t.get("type") == "audio"]
            if len(attached) >= expected:
                return

            LOG.warning("video: only %d/%d audio track(s) attached after attempt %d/%d "
                        "(likely raced the previous file's teardown) — clearing and retrying",
                        len(attached), expected, attempt, max_attempts)
            self._clear_audio_tracks()
            time.sleep(0.3)

        LOG.error("video: giving up after %d attempts — only %d/%d audio track(s) attached, "
                   "this song will play with picture but no/partial sound",
                   max_attempts, len(attached), expected)

    def _clear_audio_tracks(self) -> None:
        for track in (self.get("track-list") or []):
            if track.get("type") == "audio":
                self.command(["audio-remove", track["id"]])

    def _select_track(self, aid: int, attempts: int = 3) -> None:
        """Set `aid`, verifying it actually took.

        Same defense-in-depth reasoning as `_attach_audio_tracks`: right
        after a fresh `audio-add` batch, mpv can briefly reject or silently
        ignore a property set for a track id that isn't fully registered yet.
        """
        for attempt in range(1, attempts + 1):
            self.set("aid", aid)
            if int(self.get("aid", -1) or -1) == aid:
                return
            time.sleep(0.1)
        LOG.warning("video: aid did not settle on %d after %d attempts (got %r)",
                    aid, attempts, self.get("aid", None))

    def reload_audio(self) -> None:
        """Force mpv to close and reopen its audio output.

        Setting ``audio-device`` to a value it is already set to (most
        commonly ``"auto"``) does not by itself make mpv reconnect —
        mirrors the exact issue ``OutputRouter.reassert_current()`` works
        around on the MPD side: an AO, once opened, stays bound to
        whichever PipeWire sink was default AT THAT MOMENT, and does not
        move just because the default changed later. Found live: routed
        video's audio to Bluetooth while a song was already playing — the
        Stream Deck said "BT", PipeWire's default sink really was the
        headphones, but mpv's already-open AO kept playing to whatever it
        had been connected to before, so the QC45 stayed silent.

        ``ao-reload`` is mpv's own IPC command for exactly this: force-close
        and reopen the current AO, which is what actually makes it notice a
        new default sink (or, for the direct-ALSA Local case, actually grab
        the hw: device fresh rather than keep whatever handle it already
        had). Call this after every ``set_audio_device()``.
        """
        self.command(["ao-reload"])

    def set_audio_device(self, device: str) -> None:
        """Live-switch mpv's audio output device.

        ``"auto"`` lets mpv pick — in practice PipeWire, following whatever
        its default sink is (BT/AirPlay routing is just a PipeWire default-
        sink switch elsewhere; mpv doesn't need to know which one).
        ``"alsa/hw:CARD=...,DEV=..."`` opens that ALSA device directly,
        bypassing PipeWire — the only way to reach the DAC, which is
        deliberately hidden from PipeWire so MPD can have it bit-perfect.
        """
        self.set("audio-device", device)

    def unmute(self) -> None:
        """Clear mpv's global ``mute`` flag.

        ``mute`` is a player-wide property, not scoped to whatever file is
        currently loaded — touch_daemon.py's ``_ensure_idle_video()`` sets it
        True once at startup so the silent DRM-holding idle clip stays
        silent, and nothing ever set it back. Confirmed live: production
        mpv reported ``mute: true`` while a real video's PipeWire stream was
        correctly connected and [active] on the selected sink — routing was
        never the problem, this leftover flag was silencing the output
        before it reached PipeWire at all. Call this before any real
        (non-idle) file starts playing — see ``load_and_play()``, the one
        place every real playback path (``.skp`` karaoke, plain video,
        next/prev, resume) already funnels through.
        """
        self.set("mute", False)

    def play_pause(self) -> None:
        self.command(["cycle", "pause"])

    def pause(self) -> None:
        self.set("pause", True)

    def stop(self) -> None:
        self.command(["stop"])

    def seek(self, seconds: float) -> None:
        self.command(["seek", seconds, "relative"])

    def seek_absolute(self, seconds: float) -> None:
        """Jump to a specific position — used to resume a long video where
        it was left off, as opposed to `seek`'s relative scrub."""
        self.command(["seek", seconds, "absolute"])

    def volume(self) -> int:
        """mpv's own softvol level (0-100) — entirely separate from MPD's,
        since video plays through mpv, a different process. There was no
        way to control this at all before: the Stream Deck's volume keys
        only ever touched MPD's mixer, which is paused/idle during video
        mode, so the video page had no volume control whatsoever.

        Deliberately an explicit None-check, not ``self.get(...) or 100`` —
        0 is a legitimate (muted) volume and is falsy in Python, so the
        `or` form silently reported a muted player as 100% and broke the
        mute toggle's "is it currently muted" check. Found live.
        """
        raw = self.get("volume", 100)
        return int(raw) if raw is not None else 100

    def set_volume(self, value: int) -> int:
        value = max(0, min(100, value))
        self.set("volume", value)
        return value

    def change_volume(self, delta: int) -> int:
        return self.set_volume(self.volume() + delta)

    def eof_reached(self) -> bool:
        """True exactly when mpv has played a file to its natural end.

        Distinct from ``paused`` in ``status()``: with ``--keep-open=yes``
        (required so a finished video stays loaded instead of mpv going
        idle), reaching end-of-file ALSO sets ``pause`` to true — same as a
        manual pause partway through. ``eof-reached`` is the one property
        that only goes true for a genuine "played to the end", which is what
        auto-advance needs to tell "finished" apart from "user paused it".
        Resets to false the moment a fresh ``loadfile`` starts.
        """
        return bool(self.get("eof-reached", False))

    def switch_track(self) -> int:
        """Cycle to the next available audio track, wrapping. Returns the new aid."""
        tracks = [t for t in (self.get("track-list") or []) if t.get("type") == "audio"]
        if len(tracks) < 2:
            return int(self.get("aid", 1) or 1)
        current = self.get("aid", 1) or 1
        ids = sorted(t["id"] for t in tracks)
        idx = ids.index(current) if current in ids else -1
        next_id = ids[(idx + 1) % len(ids)]
        self.set("aid", next_id)
        return next_id

    def status(self) -> dict:
        """Best-effort snapshot for the Stream Deck's render loop.

        Every field defaults to something render-safe if mpv is unreachable
        or idle (no file loaded) — the panel should show "nothing playing",
        never crash the render pass.
        """
        idle = self.get("idle-active", True)
        if idle:
            return {
                "playing": False, "paused": True, "title": "", "singer": "",
                "elapsed": 0.0, "duration": 0.0, "track_label": "", "track_count": 0,
                "loaded": False, "path": "",
            }
        track_list = self.get("track-list") or []
        audio_tracks = [t for t in track_list if t.get("type") == "audio"]
        current_aid = self.get("aid", 1) or 1
        active = next((t for t in audio_tracks if t.get("id") == current_aid), None)
        media_path = self.get("path", "") or ""

        return {
            "playing": not bool(self.get("pause", True)),
            "paused": bool(self.get("pause", True)),
            "title": Path(media_path).stem if media_path else "",
            "elapsed": float(self.get("time-pos", 0.0) or 0.0),
            "duration": float(self.get("duration", 0.0) or 0.0),
            "track_label": (active or {}).get("title", ""),
            "track_count": len(audio_tracks),
            # A file is actually open — not just "not idle" for a flash of a
            # moment mid-transition. Used by both daemons to decide whether
            # the panel/layer should auto-follow into video mode.
            "loaded": True,
            # mpv's own authoritative idea of what's loaded right now — the
            # ONE thing every process sharing this mpv instance can trust,
            # the same role MPD itself plays for music mode. See
            # streamdeck_daemon._collect_video_state's resync using this.
            "path": media_path,
        }


# ---------------------------------------------------------------------------
# Library — flat scan of the Video folder, no MPD/database involved
# ---------------------------------------------------------------------------


# Formats the library scan indexes and mpv can play. `.skp` is the only one
# needing the custom subfile demux (see skp.py) — everything else here is a
# container mpv opens and plays natively, no special handling at all beyond
# picking `load_plain` over `load_skp` (see `load_and_play` below).
# Configurable via [video].extensions in config.toml for anything not listed
# here rather than needing a code change every time a new format shows up.
DEFAULT_VIDEO_EXTENSIONS = (".skp", ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm")


@dataclass(frozen=True)
class VideoEntry:
    path: str
    song: str
    singer: str


def split_song_singer(basename: str) -> tuple[str, str]:
    """Same convention as the reference tool: 'Song - Singer.ext'."""
    if " - " in basename:
        song, singer = basename.split(" - ", 1)
        return song.strip(), singer.strip()
    return basename.strip(), ""


class VideoLibrary:
    """In-memory index of video files under a folder, walked recursively.

    Not MPD-backed — none of the formats here are something MPD can index —
    so this is its own small scan, cached until an explicit rescan (there is
    no `idle`-style event source for an arbitrary folder the way there is for
    MPD's database).

    Not `.skp`-only: any extension in `extensions` is indexed (default
    :data:`DEFAULT_VIDEO_EXTENSIONS`) — a mixed library of `.skp` karaoke
    files alongside plain `.mkv`/`.mp4`/etc. video is expected, not an edge
    case. `.skp` is the one format needing special handling at load time
    (`load_and_play` picks `load_skp` vs `load_plain` per entry, based on
    its extension) — everything else here just plays.

    Recurses into subfolders the same way Music mode's library does — a flat
    `os.listdir` here would silently skip anything organized into a "By
    Artist" or "By Album" style subfolder tree, which is exactly how the
    Music library is laid out, so the same organizing habit applied to Video
    was invisible to this scan until now.
    """

    # Long videos (full karaoke tracks, several minutes) routinely get
    # skipped past mid-song while browsing for something else, then get come
    # back to later — asked for specifically. Remembering where playback
    # left off makes "later" resume instead of restarting from 0.
    # Session-only, not written to disk: resets on a service restart/reboot,
    # which is fine for "I skipped this a minute ago", the actual use case.
    _RESUME_MIN_ELAPSED = 5.0    # don't bother resuming a few seconds in
    _RESUME_END_MARGIN = 10.0    # near the end counts as finished, not "mid-play"

    def __init__(self, root: str, extensions: tuple[str, ...] = DEFAULT_VIDEO_EXTENSIONS) -> None:
        self._root = root
        self._extensions = tuple(e.lower() for e in extensions)
        self._entries: list[VideoEntry] = []
        self._index = 0
        self._positions: dict[str, float] = {}
        self.rescan()

    def rescan(self) -> int:
        entries: list[VideoEntry] = []
        if os.path.isdir(self._root):
            # os.walk, not os.listdir: subfolders (e.g. "By Artist", "By
            # Album" — however the library happens to be organized, same as
            # Music) were previously invisible to this scan entirely.
            # topdown + in-place dirnames.sort() keeps traversal (and so the
            # resulting order) deterministic, same reasoning as sorting the
            # filenames themselves below — otherwise entry order, and so
            # which slot each song lands in on the panel, would depend on
            # the filesystem's arbitrary readdir order and could differ
            # between one rescan and the next with nothing on disk changed.
            for dirpath, dirnames, filenames in os.walk(self._root):
                dirnames.sort()
                for name in sorted(filenames):
                    if not name.lower().endswith(self._extensions):
                        continue
                    base = os.path.splitext(name)[0]
                    song, singer = split_song_singer(base)
                    entries.append(VideoEntry(path=os.path.join(dirpath, name),
                                              song=song, singer=singer))
        else:
            LOG.warning("video library folder not found: %s", self._root)
        self._entries = entries
        self._index = min(self._index, max(0, len(entries) - 1))
        LOG.info("video library: %d file(s) under %s (including subfolders, extensions=%s)",
                  len(entries), self._root, ", ".join(self._extensions))
        return len(entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def current(self) -> VideoEntry | None:
        if not self._entries:
            return None
        return self._entries[self._index]

    @property
    def index(self) -> int:
        return self._index

    @property
    def root(self) -> str:
        """The library's root folder — exposed for video_delete_current's
        safety check (see actions.py), the video-page analogue of
        _resolve_under_music_dir's "never touch anything outside the
        library" guard. Every VideoEntry.path is already root-anchored by
        construction (built from os.walk(root) in rescan(), never from an
        externally-supplied string the way MPD's URIs are), so this exists
        for defense-in-depth, not because a video entry could realistically
        smuggle a '..' the way an MPD URI theoretically could.
        """
        return self._root

    def entries(self) -> list[VideoEntry]:
        return list(self._entries)

    def select(self, index: int) -> VideoEntry | None:
        if not self._entries:
            return None
        self._index = index % len(self._entries)
        return self.current

    def next(self) -> VideoEntry | None:
        if not self._entries:
            return None
        return self.select(self._index + 1)

    def prev(self) -> VideoEntry | None:
        if not self._entries:
            return None
        return self.select(self._index - 1)

    def select_by_path(self, path: str) -> VideoEntry | None:
        """Find and select the entry whose path matches ``path`` exactly.

        Needed by the page-2 grid browser (VideoGridBrowser): its rows come
        from a live directory listing (``list_dir``, below), not from this
        class's own pre-scanned ``entries()`` order, so it has no index to
        hand back the way the "up next" slots (built directly from
        ``entries()``, see streamdeck_daemon._video_window) do — it can only
        identify what was picked by path.
        """
        for i, entry in enumerate(self._entries):
            if entry.path == path:
                return self.select(i)
        return None

    # -- subfolder access -----------------------------------------------------

    def top_folders(self) -> list[str]:
        """Sorted names of top-level subfolders (immediate children of the
        library root) that hold at least one indexed entry, at any depth.

        Powers the video page's direct-subfolder-access keys (four slots,
        see layout.LAYOUT_VIDEO index(1,4)-index(1,7)) — with only four
        slots and no room for a full folder browser, this just lists what's
        directly under the root, e.g. "Favorites"/"Remix", each jumping
        straight to its first entry. A video sitting directly at the root
        (no subfolder) has no folder name to show and is not represented
        here.
        """
        names: set[str] = set()
        for entry in self._entries:
            rel = os.path.relpath(entry.path, self._root)
            parts = rel.split(os.sep)
            if len(parts) > 1:
                names.add(parts[0])
        return sorted(names)

    def first_index_in_folder(self, name: str) -> int | None:
        """Index (in scan order) of the first entry inside top-level
        subfolder ``name``, or None if the folder is empty/unknown."""
        prefix = name + os.sep
        for i, entry in enumerate(self._entries):
            rel = os.path.relpath(entry.path, self._root)
            if rel.startswith(prefix):
                return i
        return None

    def list_dir(self, rel_path: str = "") -> list[tuple[str, bool, str]]:
        """Immediate children of ``rel_path`` (relative to the library
        root): ``(name, is_dir, absolute_path)`` tuples, directories sorted
        before files, each sublist case-insensitive by name — same
        convention as the music library browser (see
        streamdeck.browser.LibraryBrowser). Files are filtered to
        ``self._extensions`` (the same list ``rescan()`` uses — no second,
        separately-maintained extension list); directories are listed
        unconditionally, since a subfolder might hold indexed videos
        several levels further down even if it has none directly in it.

        Powers the video page's page-2 grid browser (VideoGridBrowser).
        Deliberately a live ``os.scandir``, not a lookup against
        ``self._entries`` — a folder created (or a file dropped in) since
        the last ``rescan()`` should still be browsable immediately,
        without requiring a rescan first, since renaming/moving files is
        exactly what browsing here is often used for.
        """
        base = os.path.join(self._root, rel_path) if rel_path else self._root
        if not os.path.isdir(base):
            return []
        dirs: list[str] = []
        files: list[str] = []
        try:
            with os.scandir(base) as it:
                for entry in it:
                    if entry.is_dir():
                        dirs.append(entry.name)
                    elif entry.is_file() and entry.name.lower().endswith(self._extensions):
                        files.append(entry.name)
        except OSError as exc:
            LOG.warning("video list_dir failed for %s: %s", base, exc)
            return []
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        result: list[tuple[str, bool, str]] = []
        for name in dirs:
            result.append((name, True, os.path.join(base, name)))
        for name in files:
            result.append((name, False, os.path.join(base, name)))
        return result

    # -- resume position -----------------------------------------------------

    def remember_position(self, path: str, elapsed: float, duration: float) -> None:
        """Record (or clear) where playback left off for ``path``.

        Skipped in the first few seconds (barely started, resuming would be
        pointless) or within the last ``_RESUME_END_MARGIN`` seconds
        (effectively finished — resuming there would just replay a few
        seconds and stop) both clear any prior saved position instead of
        recording one, so a video watched to completion doesn't oddly
        "resume" near its own end the next time it's picked.
        """
        if elapsed < self._RESUME_MIN_ELAPSED or (duration and elapsed > duration - self._RESUME_END_MARGIN):
            self._positions.pop(path, None)
            return
        self._positions[path] = elapsed

    def take_resume_position(self, path: str) -> float | None:
        """Consume (not just read) any saved position for ``path``.

        One-shot: the caller is about to actually resume from it, so it
        should not be handed out again next time the same entry loads
        unless a NEW mid-play skip saves a fresh one via
        ``remember_position``.
        """
        return self._positions.pop(path, None)

    def forget_position(self, path: str) -> None:
        """Drop any saved resume position for ``path`` outright, with no
        replacement — used when the file at that path is about to stop
        existing there at all (video_curate_current moving it into a
        Curated/ subfolder), so a stale resume entry keyed by the OLD path
        doesn't linger forever pointing at nothing.
        """
        self._positions.pop(path, None)


def load_and_play(commander: VideoCommander, entry: VideoEntry,
                   resume: float | None = None) -> SkpOffsets | None:
    """Load one library entry — `.skp` via the subfile demux, anything else
    (`.mkv`, `.mp4`, ...) as a plain file mpv opens directly. Raises
    SkpParseError/VideoError on failure — callers decide how to surface that
    (toast, log, skip to next file).

    Returns the parsed :class:`SkpOffsets` for a `.skp` entry, or ``None``
    for a plain video (there is nothing to parse — the file IS the track).
    No current caller uses the return value; kept for callers/tests that do.

    ``resume`` seeks to that absolute position once loaded — see
    ``VideoLibrary.take_resume_position``. Skipped for falsy values (None or
    0.0) since a fresh load already starts at 0. Works the same for both
    branches: mpv's `seek ... absolute` doesn't care how the file was loaded.
    """
    # Every real playback path funnels through here — the one place that
    # needs to undo _ensure_idle_video()'s startup mute (see
    # VideoCommander.unmute()'s docstring). Unmute BEFORE loadfile so the
    # very first frame of real audio is never silently dropped waiting for
    # a later property-set to land.
    commander.unmute()
    offsets: SkpOffsets | None = None
    if entry.path.lower().endswith(".skp"):
        offsets = parse_skp(entry.path)
        commander.load_skp(offsets)
    else:
        commander.load_plain(entry.path)
    if resume:
        commander.seek_absolute(resume)
    return offsets
