"""Resilient MPD client wrapper.

Two classes:

``MpdCommander``
    Fire-and-forget command connection. Auto-reconnects. Used by the TourBox
    daemon and by Stream Deck key presses. Never blocks in ``idle``.

``MpdWatcher``
    Dedicated connection that sits in MPD's ``idle`` command and yields the
    names of subsystems that changed. Used by the Stream Deck renderer so it
    redraws on events instead of polling.

Why two connections rather than one: ``idle`` blocks the connection until
something happens. Issuing a command on that same socket requires sending
``noidle`` first and carefully re-entering idle afterwards — a well-known
source of dropped events. MPD handles many concurrent clients cheaply, so two
sockets is the simpler and more reliable design.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

from mpd import ConnectionError as MpdConnectionError
from mpd import MPDClient
from mpd import MPDError

LOG = logging.getLogger(__name__)


def _connect(host: str, port: int, timeout: float) -> MPDClient:
    client = MPDClient()
    client.timeout = timeout
    # python-mpd2 treats a host starting with '/' as a Unix socket path.
    client.connect(host, port)
    return client


class MpdCommander:
    """Thread-safe MPD command connection with transparent reconnect.

    Every public call routes through :meth:`_call`, which reconnects once and
    retries if the socket has gone away. That covers the common case of MPD
    being restarted underneath us without every call site needing try/except.
    """

    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._client: MPDClient | None = None
        self._lock = threading.RLock()

    # -- connection management ---------------------------------------------

    def _ensure(self) -> MPDClient:
        if self._client is None:
            LOG.debug("connecting to MPD at %s:%s", self._host, self._port)
            self._client = _connect(self._host, self._port, self._timeout)
            LOG.info("MPD connected (%s)", self._host)
        return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                    self._client.disconnect()
                except (MpdConnectionError, OSError):
                    pass
                self._client = None

    def _drop(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except (MpdConnectionError, OSError):
                pass
            self._client = None

    def _call(self, method: str, *args: Any) -> Any:
        """Invoke an MPD command, reconnecting once on connection failure."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    client = self._ensure()
                    return getattr(client, method)(*args)
                except (MpdConnectionError, ConnectionError, OSError, socket.timeout) as exc:
                    LOG.warning(
                        "MPD %s failed (attempt %d/2): %s", method, attempt, exc
                    )
                    self._drop()
                    if attempt == 2:
                        return None
                    time.sleep(0.2)
                except MPDError as exc:
                    # A protocol-level error (bad argument, no current song).
                    # Not a connection problem — do not reconnect, just report.
                    LOG.debug("MPD %s rejected: %s", method, exc)
                    return None
        return None

    # -- state -------------------------------------------------------------

    def status(self) -> dict[str, str]:
        return self._call("status") or {}

    def current_song(self) -> dict[str, str]:
        return self._call("currentsong") or {}

    def outputs(self) -> list[dict[str, str]]:
        return self._call("outputs") or []

    def stats(self) -> dict[str, str]:
        return self._call("stats") or {}

    # -- transport ---------------------------------------------------------

    def toggle_pause(self) -> None:
        state = self.status().get("state")
        if state == "play":
            self._call("pause", 1)
        elif state == "pause":
            self._call("pause", 0)
        else:
            self._call("play")

    def stop(self) -> None:
        self._call("stop")

    def next_track(self) -> None:
        self._call("next")

    def prev_track(self) -> None:
        self._call("previous")

    def seek_relative(self, seconds: int) -> None:
        """Seek within the current track, clamped to its bounds.

        MPD's ``seekcur`` accepts a signed relative offset like '+15'. We clamp
        manually because seeking past the end behaves inconsistently across
        decoders, and seeking before zero is an error.
        """
        status = self.status()
        if status.get("state") not in ("play", "pause"):
            return
        try:
            elapsed = float(status.get("elapsed", 0.0))
            duration = float(status.get("duration", status.get("time", "0").split(":")[-1]))
        except (TypeError, ValueError):
            return

        target = elapsed + seconds
        if target < 0:
            target = 0.0
        if duration and target > duration - 1:
            # Past the end: treat as "next track", which is what the user meant.
            self.next_track()
            return
        self._call("seekcur", f"{target:.1f}")

    def seek_to_start(self) -> None:
        self._call("seekcur", "0")

    # -- volume ------------------------------------------------------------

    def change_volume(self, delta: int) -> int | None:
        """Adjust volume by ``delta`` percent, clamped to 0-100.

        Returns the new volume, or None if MPD reports no mixer (which happens
        when the only enabled output has ``mixer_type "none"``).
        """
        status = self.status()
        raw = status.get("volume")
        if raw is None or raw == "-1":
            LOG.debug("no mixer available on the active output")
            return None
        current = int(raw)
        new = max(0, min(100, current + delta))
        if new != current:
            self._call("setvol", new)
        return new

    def set_volume(self, value: int) -> None:
        self._call("setvol", max(0, min(100, value)))

    def set_crossfade(self, seconds: int) -> None:
        """Enable MPD's built-in crossfade for AUTOMATIC track transitions.

        This is runtime MPD state, not persisted config — it resets to 0
        every time mpd.service restarts, so callers should re-apply it at
        daemon startup rather than assuming it sticks. Only smooths a song
        ending and the queue naturally advancing; MPD cannot crossfade a
        manual next/previous (see PlaybackConfig.crossfade_seconds).
        """
        self._call("crossfade", max(0, seconds))

    # -- modes -------------------------------------------------------------

    def toggle_random(self) -> None:
        self._call("random", 0 if self.status().get("random") == "1" else 1)

    def toggle_repeat(self) -> None:
        self._call("repeat", 0 if self.status().get("repeat") == "1" else 1)

    def toggle_single(self) -> None:
        self._call("single", 0 if self.status().get("single") == "1" else 1)

    def set_consume(self, on: bool) -> None:
        self._call("consume", 1 if on else 0)

    def set_single(self, on: bool) -> None:
        self._call("single", 1 if on else 0)

    def toggle_consume(self) -> None:
        self._call("consume", 0 if self.status().get("consume") == "1" else 1)

    # -- outputs -----------------------------------------------------------

    def enable_output(self, index: int) -> None:
        self._call("enableoutput", index)

    def disable_output(self, index: int) -> None:
        self._call("disableoutput", index)

    # -- library -----------------------------------------------------------

    def update_database(self) -> None:
        self._call("update")

    def play_position(self, position: int) -> None:
        self._call("play", position)

    def playlist_info(self) -> list[dict[str, str]]:
        return self._call("playlistinfo") or []

    # -- browsing ----------------------------------------------------------

    def delete_id(self, song_id: str) -> None:
        """Remove one entry from the queue by its songid.

        By id, not position: positions shift as the queue changes, so a
        position captured a moment ago can point at a different track by the
        time the command lands.
        """
        self._call("deleteid", song_id)

    def music_directory(self) -> str | None:
        """Ask MPD where its library root is.

        Only works over a Unix socket (MPD refuses `config` over TCP), which is
        what we use. Returns None on TCP so callers fall back to config.toml.
        """
        cfg = self._call("config")
        if isinstance(cfg, dict):
            return cfg.get("music_directory")
        return None

    def lsinfo(self, uri: str = "") -> list[dict[str, str]]:
        """List one directory level: subdirectories, files and playlists."""
        return self._call("lsinfo", uri) or []

    def clear_queue(self) -> None:
        self._call("clear")

    def add(self, uri: str) -> None:
        """Append a file or an entire directory to the queue."""
        self._call("add", uri)

    def play_uri_from_directory(self, directory: str, uri: str) -> None:
        """Replace the queue with ``directory`` and start at ``uri``.

        Queueing the whole directory rather than the single file is what makes
        picking a track behave like picking a track *on an album* — the rest of
        the album follows instead of playback stopping after one song.
        """
        self.clear_queue()
        self.add(directory) if directory else self.add(uri)

        target = 0
        for index, entry in enumerate(self.playlist_info()):
            if entry.get("file") == uri:
                target = index
                break
        self.play_position(target)


class MpdWatcher:
    """Blocking ``idle`` subscriber.

    Usage::

        for subsystems in watcher.events(["player", "mixer", "output"]):
            redraw(subsystems)

    The generator yields a set of changed subsystem names. It reconnects on
    its own and yields ``{"__reconnected__"}`` after recovering so callers can
    force a full refresh — state may have changed while we were disconnected.
    """

    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._client: MPDClient | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def stop(self) -> None:
        """Unblock the generator and ask it to finish.

        Three approaches were tried against python-mpd2 3.1.1; only the third
        works, so the reasoning is recorded here to stop anyone "simplifying"
        it back:

        1. ``client.noidle()`` from the stopping thread — raises
           ``NotImplementedError: Abstract MPDClientBase does not implement
           noidle``. The library's idle/noidle pairing is not thread-safe.
        2. ``client.idletimeout`` for periodic wakeups — the timeout fires as
           ``TimeoutError`` but leaves the connection permanently broken
           ("cannot read from timed out object"), so every poll would force a
           reconnect.
        3. Shutting the underlying socket down — unblocks the blocked read
           immediately and raises ``ConnectionError``, which the loop already
           handles. This is what we do.

        ``_sock`` is private API. It has been stable across python-mpd2 3.x and
        the failure mode if it ever disappears is graceful: the watcher thread
        is a daemon thread, so a stop that fails to unblock it still lets the
        process exit.
        """
        self._stop.set()
        with self._lock:
            client = self._client
        if client is None:
            return
        sock = getattr(client, "_sock", None)
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed, or never connected

    def events(self, subsystems: list[str]) -> Iterator[set[str]]:
        """Yield sets of changed MPD subsystem names.

        Blocks in MPD's ``idle`` command, so this is true push with no polling
        and no CPU cost while nothing is happening. :meth:`stop` unblocks it.
        """
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self._client is None:
                    client = _connect(self._host, self._port, self._timeout)
                    with self._lock:
                        self._client = client
                    LOG.info("MPD idle watcher connected")
                    backoff = 1.0
                    yield {"__reconnected__"}

                changed = self._client.idle(*subsystems)
                if self._stop.is_set():
                    break
                if changed:
                    yield set(changed)

            except (MpdConnectionError, ConnectionError, OSError, socket.timeout) as exc:
                if self._stop.is_set():
                    break
                LOG.warning("idle watcher lost MPD (%s); retrying in %.0fs", exc, backoff)
                self._drop()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

            except MPDError as exc:
                LOG.error("idle watcher protocol error: %s", exc)
                self._stop.wait(1.0)

        self._drop()

    def _drop(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except (MpdConnectionError, MPDError, OSError):
            pass
