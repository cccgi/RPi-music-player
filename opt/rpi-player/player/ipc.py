"""Lightweight NDJSON broadcast bus over a Unix domain socket.

Scope note: this is deliberately NOT the main control path. MPD is already an
IPC bus and already broadcasts state changes via ``idle`` — reimplementing that
would be duplicated, lossier machinery.

This bus carries only what MPD does not model:

    * toast/OSD messages  ("Volume 45%", "Bluetooth connected")
    * route-change hints so the Stream Deck can update optimistically before
      MPD confirms (matters on AirPlay, where there is ~2s of buffering)
    * shutdown coordination

Both daemons run correctly if this socket never appears. Every call is
best-effort and failures are logged at debug level, never raised.

Debug it by hand:
    socat - UNIX-CONNECT:/run/rpi-player/bus.sock
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


class BusServer:
    """Accepts clients and fans out newline-delimited JSON to all of them.

    Runs its own daemon thread. A slow or dead client is dropped rather than
    allowed to block the publisher — an unresponsive display must never stall
    the control path.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                self._path.unlink()

            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(self._path))
            self._server.listen(8)
            self._server.settimeout(1.0)
            os.chmod(self._path, 0o660)
        except OSError as exc:
            LOG.warning("IPC bus unavailable at %s: %s", self._path, exc)
            self._server = None
            return False

        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="ipc-accept")
        self._thread.start()
        LOG.info("IPC bus listening on %s", self._path)
        return True

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._server is not None:
            try:
                conn, _ = self._server.accept()
                conn.setblocking(False)
                with self._lock:
                    self._clients.append(conn)
                LOG.debug("IPC client connected (%d total)", len(self._clients))
            except socket.timeout:
                continue
            except OSError:
                break

    def publish(self, event: str, **payload: Any) -> None:
        message = json.dumps({"event": event, "ts": time.time(), **payload}) + "\n"
        data = message.encode("utf-8")

        with self._lock:
            dead: list[socket.socket] = []
            for conn in self._clients:
                try:
                    conn.sendall(data)
                except (BlockingIOError, BrokenPipeError, OSError):
                    dead.append(conn)
            for conn in dead:
                self._clients.remove(conn)
                try:
                    conn.close()
                except OSError:
                    pass
            if dead:
                LOG.debug("dropped %d dead IPC client(s)", len(dead))

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for conn in self._clients:
                try:
                    conn.close()
                except OSError:
                    pass
            self._clients.clear()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass


class BusClient:
    """Subscriber with automatic reconnect.

    ``on_event`` is called from a background thread with the decoded dict.
    Keep the callback fast; hand real work to the consumer's own loop.
    """

    def __init__(self, path: str, on_event: Callable[[dict[str, Any]], None]) -> None:
        self._path = path
        self._on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ipc-client")
        self._thread.start()

    def _loop(self) -> None:
        buffer = b""
        while not self._stop.is_set():
            conn: socket.socket | None = None
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.settimeout(2.0)
                conn.connect(self._path)
                conn.settimeout(None)
                LOG.debug("IPC subscriber connected to %s", self._path)
                buffer = b""

                while not self._stop.is_set():
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            self._on_event(json.loads(line))
                        except (ValueError, TypeError) as exc:
                            LOG.debug("bad IPC message: %s", exc)

            except OSError as exc:
                LOG.debug("IPC subscriber: %s", exc)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
            self._stop.wait(2.0)

    def stop(self) -> None:
        self._stop.set()


class NullBus:
    """No-op stand-in used when IPC is disabled in config."""

    def start(self) -> bool:
        return False

    def publish(self, event: str, **payload: Any) -> None:
        return None

    def stop(self) -> None:
        return None
