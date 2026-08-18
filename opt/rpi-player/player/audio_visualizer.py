"""Real audio-reactive visualizer data — a background reader thread that
turns MPD's fifo-output tap into a small, smoothed array of per-band
levels, without decoding audio a second time or touching the playback
path at all.

--------------------------------------------------------------------------
WHERE THE DATA COMES FROM
--------------------------------------------------------------------------
MPD already decodes audio once and sends it to whichever real output is
enabled (Local ALSA / Network PipeWire / HDMI ALSA — see system/mpd.conf).
This module does NOT add a second decode pipeline, re-read the media file,
or shell out to anything. Instead, system/mpd.conf now also enables a 4th
output:

    audio_output {
        type   "fifo"
        name   "Visualizer"
        path   "/run/mpd/visualizer.fifo"
        format "44100:16:2"
    }

MPD's fifo output writes a second copy of the SAME already-decoded PCM
frames into a named pipe, purely so something else can read them — this
is the standard low-cost mechanism tools like cava use against MPD, and
costs MPD essentially nothing extra (no re-decode, just an extra write()
of a buffer it already has in memory). ``format`` pins the fifo to a
known, fixed layout regardless of what's actually playing, so this reader
never has to handle format changes mid-track.

--------------------------------------------------------------------------
WHY A BACKGROUND THREAD, NOT INLINE IN THE RENDER LOOP
--------------------------------------------------------------------------
Reading a fifo blocks until data (or EOF) arrives. touch_daemon.py's
render loop must never block on that — "do not perform expensive
metadata/audio operations from the render loop" (this round's spec,
section 25). So AudioVisualizer runs its own daemon thread that blocks
happily in the pipe's read() call, and touch_daemon just calls
``get_levels()``, which reads a small cached array under a lock — O(1),
no I/O, safe to call every render tick.

--------------------------------------------------------------------------
COST
--------------------------------------------------------------------------
- The reader thread only wakes when MPD actually writes new PCM (a
  blocking read, not a poll loop) — effectively idle CPU while paused/
  stopped, since MPD stops writing to the fifo output when nothing is
  playing.
- FFT runs on a small window (1024 samples, mono-downmixed) at a capped
  rate (~18 Hz, see _MIN_UPDATE_INTERVAL) using numpy's rfft — a handful
  of microseconds of CPU per call on a Pi 5, not a sustained load.
- Smoothing (attack/decay per band) is a few float multiplications per
  band per update — negligible.
This was NOT measured against real hardware from this environment (no
device access) — see the accompanying message for what to check on the
actual Pi (CPU% of touch-ui-player.service while music plays, watched
with ``top``/``htop`` for a few minutes) before considering this settled.

--------------------------------------------------------------------------
GRACEFUL DEGRADATION
--------------------------------------------------------------------------
If numpy isn't installed, the fifo doesn't exist yet (old mpd.conf not
deployed), or the fifo can't be opened for any reason, ``is_available``
is False and ``get_levels()`` returns an all-zero array forever — the
renderer already treats an all-zero/empty visualizer as "hide it" (see
render.py's module docstring), so a fresh checkout with an unmodified
mpd.conf just quietly shows no visualizer instead of crashing the touch
daemon.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time

LOG = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:  # pragma: no cover - reported once at runtime
    np = None

_SAMPLE_RATE = 44100
_CHANNELS = 2
_BYTES_PER_SAMPLE = 2  # 16-bit
_FRAME_BYTES = _CHANNELS * _BYTES_PER_SAMPLE
_WINDOW_SAMPLES = 1024  # ~23ms of audio per analysis window

# Cap how often a new FFT is computed even if data arrives faster --
# 18 Hz sits in the spec's requested 15-24 FPS range for the visualizer,
# decoupled from the touch UI's own render tick.
_MIN_UPDATE_INTERVAL = 1.0 / 18.0

# Attack (level rising) reacts fast; decay (level falling, or nothing
# playing) is slower -- this is what keeps the bars looking like a smooth
# instrument instead of jittering per-sample. Also what makes "paused"
# look like a gentle fade rather than a hard cut (see module docstring in
# render.py, and PAUSED/STOPPED behavior in touch_daemon._collect_state).
_ATTACK = 0.6
_DECAY = 0.15


class AudioVisualizer:
    """Owns the fifo reader thread and the current smoothed band levels.

    ``bar_count`` bands are produced by log-spaced frequency bucketing
    (bass through treble compressed into few enough bars to read as a
    premium accent, not a giant equalizer — see layout.py's
    VISUALIZER_BAR_COUNT).
    """

    def __init__(self, fifo_path: str, bar_count: int) -> None:
        self._fifo_path = fifo_path
        self._bar_count = bar_count
        self._levels = [0.0] * bar_count
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._available = np is not None
        if np is None:
            LOG.warning("numpy not installed -- audio visualizer disabled "
                        "(pip install numpy in the venv to enable it)")
        # Precompute band edges once: log-spaced from ~40Hz to Nyquist,
        # which is what makes bass/mid/treble each get a fair share of
        # bars instead of most bars landing in the (perceptually crowded)
        # low end that a LINEAR split would produce.
        nyquist = _SAMPLE_RATE / 2
        low_hz = 40.0
        self._band_edges = [
            low_hz * (nyquist / low_hz) ** (i / bar_count)
            for i in range(bar_count + 1)
        ]

    @property
    def is_available(self) -> bool:
        return self._available

    def start(self) -> None:
        if not self._available:
            return
        self._thread = threading.Thread(target=self._run, name="audio-visualizer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # The thread is likely blocked in a fifo read(); it'll notice
        # _stop on its next loop iteration (open/read cycle), same
        # "best-effort, daemon thread" tradeoff MpdWatcher.stop() documents
        # for its own blocking read -- acceptable here since this thread
        # never holds anything that needs a clean unwind.

    def get_levels(self) -> list[float]:
        """Current smoothed per-band levels, each 0.0-1.0. Safe to call
        every render tick — just reads a cached list under a lock, no I/O.
        """
        with self._lock:
            return list(self._levels)

    # -- reader thread ---------------------------------------------------------

    def _run(self) -> None:
        import os

        while not self._stop.is_set():
            try:
                # Opened read-write (not read-only) so THIS reader never
                # blocks waiting for a writer to show up (and never sees
                # spurious EOF the instant MPD briefly closes/reopens the
                # fifo between tracks) -- a well-known fifo-handling trick,
                # not a mistake: a fifo opened O_RDWR always has "a
                # writer" (itself) from the reader's point of view.
                fd = os.open(self._fifo_path, os.O_RDWR)
            except OSError:
                LOG.debug("could not open visualizer fifo %r yet — retrying",
                          self._fifo_path, exc_info=True)
                if self._stop.wait(2.0):
                    return
                continue

            LOG.info("audio visualizer connected to %s", self._fifo_path)
            buf = b""
            last_update = 0.0
            try:
                while not self._stop.is_set():
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        time.sleep(0.05)
                        continue
                    buf += chunk
                    window_bytes = _WINDOW_SAMPLES * _FRAME_BYTES
                    if len(buf) < window_bytes:
                        continue
                    # Keep only the most recent window; drop anything
                    # older rather than let the buffer grow unbounded if
                    # analysis ever falls behind real-time.
                    buf = buf[-window_bytes:]
                    now = time.monotonic()
                    if now - last_update < _MIN_UPDATE_INTERVAL:
                        continue
                    last_update = now
                    self._analyze(buf)
            except OSError:
                LOG.debug("visualizer fifo read failed — reconnecting", exc_info=True)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                self._decay_to_silence()

    def _analyze(self, pcm: bytes) -> None:
        try:
            samples = np.frombuffer(pcm, dtype="<i2")
            stereo = samples.reshape(-1, _CHANNELS).astype(np.float32)
            mono = stereo.mean(axis=1) / 32768.0
            # Hann window -- reduces spectral leakage from analyzing a
            # short, non-periodic chunk; cheap (one multiply per sample).
            windowed = mono * np.hanning(len(mono))
            spectrum = np.abs(np.fft.rfft(windowed))
            freqs = np.fft.rfftfreq(len(windowed), d=1.0 / _SAMPLE_RATE)

            new_levels = []
            for i in range(self._bar_count):
                lo, hi = self._band_edges[i], self._band_edges[i + 1]
                mask = (freqs >= lo) & (freqs < hi)
                magnitude = float(spectrum[mask].mean()) if mask.any() else 0.0
                # Rough perceptual compression -- log1p keeps quiet
                # passages visible instead of flatlining near zero, and
                # caps how much one loud band can dominate.
                level = min(1.0, math.log1p(magnitude * 40) / 4.0)
                new_levels.append(level)

            with self._lock:
                for i in range(self._bar_count):
                    target = new_levels[i]
                    current = self._levels[i]
                    rate = _ATTACK if target > current else _DECAY
                    self._levels[i] = current + (target - current) * rate
        except Exception:  # noqa: BLE001 - never let a bad audio frame kill the reader
            LOG.debug("visualizer analysis failed on one window", exc_info=True)

    def _decay_to_silence(self) -> None:
        """Called when the fifo read loop exits (MPD stopped writing —
        paused/stopped/track ended) so the bars fade out instead of
        freezing at whatever they last were mid-transition. A few decay
        steps here get it most of the way; get_levels() between calls
        just keeps returning the last value, and the next successful
        _analyze() call (once playback resumes) will pull levels back up
        via the same attack/decay smoothing.
        """
        with self._lock:
            for _ in range(6):
                for i in range(self._bar_count):
                    self._levels[i] *= (1.0 - _DECAY)
