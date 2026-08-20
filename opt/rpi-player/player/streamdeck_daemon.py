#!/usr/bin/env python3
"""Stream Deck XL display and control daemon.

Two jobs:

  1. Render MPD state onto the 32 keycaps — track info, elapsed, volume,
     play/pause state, output destination.
  2. Dispatch key presses back to MPD through the shared action table, so the
     Stream Deck and the TourBox can never disagree about what a verb means.

Rendering strategy: the panel is driven by MPD's ``idle`` command, not by
polling. We block until MPD says something changed, then redraw only the keys
whose *content* changed — each render is hashed by a content key and skipped if
identical. On a battery build this matters; a naive implementation redrawing 32
JPEGs every second is a measurable power draw for no benefit.

The one exception is the progress key, which ticks once a second while playing
because elapsed time changes without MPD emitting an idle event.

Run standalone for debugging:
    /opt/rpi-player/venv/bin/python -m player.streamdeck_daemon --verbose
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from io import BytesIO
from pathlib import Path

from PIL import Image
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

from . import bt as _bt
from . import cpugov
from . import mode as _mode
from . import wifi as _wifi
from .actions import (
    ActionContext,
    DeleteSettings,
    _apply_music_storage_set,
    _apply_video_storage_set,
    _capture_video_position,
    _clamp_video_volume_if_airplay,
    dispatch,
    music_storage_label,
    music_usb_available,
    query_wifi_enabled,
    usb_mounted,
    video_storage_label,
    video_usb_available,
)
from .config import Config, load_config, setup_logging
from .ipc import BusClient
from .mpdbus import MpdCommander, MpdWatcher
from .outputs import AIRPLAY_SAFE_VOLUME, OutputRouter, PwSink
from .power import read_pi_current_ma
from .skp import SkpParseError
from .streamdeck import layout
from .streamdeck.browser import LibraryBrowser, MusicGridBrowser, VideoGridBrowser
from .streamdeck.render import KeyRenderer, Theme, ensure_fonts
from .video import VideoCommander, VideoError, VideoLibrary, load_and_play

LOG = logging.getLogger("streamdeck")

# Not real cross-process modes (see player/mode.py) — both pickers are
# Stream Deck-only overlays, so these live here, not in mode.py.
PAGE_AIRPLAY_PICKER = "airplay_picker"
PAGE_BT_PICKER = "bt_picker"
# "Page 2" overlays — same idea (Stream Deck-only, not written to the
# cross-process mode file), one per mode. See layout.LAYOUT_BROWSE /
# LAYOUT_VIDEO_BROWSE.
PAGE_MUSIC_BROWSE = "music_browse"
PAGE_VIDEO_BROWSE = "video_browse"

# The BT picker's "Reset" key (layout.LAYOUT_BT_PICKER's "bt_reset" entry) —
# used to force a repaint of just that one tile when its state changes,
# same idea as TOAST_SLOT/TOAST_SLOT_VIDEO.
_BT_RESET_KEY = layout.index(3, 6)

# The AirPlay picker's Wi-Fi fix buttons (layout.LAYOUT_AIRPLAY_PICKER) —
# same idea, one constant per key whose repaint needs to be forced when its
# working/done state changes.
_WIFI_RECONNECT_KEY = layout.index(3, 5)
_WIFI_FIX_KEY = layout.index(3, 6)

# MPD subsystems we watch. 'database' is included so the browser re-reads the
# listing after `mpc update` — MPD emits it once when the scan finishes, not
# per file, so this is cheap.
IDLE_SUBSYSTEMS = ["player", "mixer", "options", "playlist", "output", "database"]

TOAST_SECONDS = 1.8

# How often the eject-detection watcher polls usb_mounted() — see
# StreamDeckDaemon._watch_usb. Short enough that a real yank is noticed
# within a few seconds, long enough that it's not a measurable stat()-call
# tax on the battery build.
USB_POLL_SECONDS = 3.0

# How long the shutdown key stays armed after the first press. Long enough to
# be deliberate, short enough that it disarms itself if you walk away.
SHUTDOWN_CONFIRM_SECONDS = 5.0

# How long with nothing playing before the CPU governor drops to
# LOW_POWER_GOVERNOR (player/cpugov.py) to cut down on idle heat. Restored
# to normal instantly on any key press or the moment playback resumes — see
# _apply_low_power / _on_key.
IDLE_POWER_SECONDS = 120.0

# Volume keys auto-repeat while held, same as every OS volume rocker — a
# single tap for a small nudge is fine, but sweeping from 20% to 100% one
# 3%-step tap at a time is not. The actions themselves already clamp to
# 0-100 (MpdCommander.change_volume / VideoCommander.set_volume), so
# repeating past the bound is harmless — it just stops moving the number,
# which is exactly "continue until 0% or 100%, then stop".
REPEAT_ACTIONS = frozenset({
    "volume_up", "volume_down", "video_volume_up", "video_volume_down",
})
REPEAT_START_DELAY = 0.4     # held-but-not-yet-repeating grace period
REPEAT_INTERVAL = 0.15       # seconds between repeated steps once it starts


class StreamDeckDaemon:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._running = True

        self._deck = None
        self._renderer = KeyRenderer(
            Theme(
                bg=config.streamdeck.theme.bg,
                fg=config.streamdeck.theme.fg,
                muted=config.streamdeck.theme.muted,
                accent=config.streamdeck.theme.accent,
                active=config.streamdeck.theme.active,
                warn=config.streamdeck.theme.warn,
                font_regular=config.streamdeck.theme.font_regular,
                font_bold=config.streamdeck.theme.font_bold,
            )
        )

        self._mpd = MpdCommander(config.mpd.host, config.mpd.port, config.mpd.timeout)
        self._watcher = MpdWatcher(config.mpd.host, config.mpd.port, config.mpd.timeout)
        self._router = OutputRouter(self._mpd, config.routes)

        # Video/karaoke mode. "music" is the panel's own page state — kept
        # local rather than read back from the mode file every tick, but the
        # file IS what the TourBox daemon polls to follow along; see
        # player/mode.py. Writing it happens inside the enter/exit actions.
        self._page = _mode.MUSIC
        self._video = VideoCommander(config.video.mpv_socket, config.video.timeout) \
            if config.video.enabled else None
        self._video_library = VideoLibrary(config.video.library_dir,
                                           extensions=config.video.extensions) \
            if config.video.enabled else None
        self._mode_file = config.video.mode_file

        # Pass the CONFIGURED step sizes. Omitting these silently fell back to
        # the ActionContext dataclass defaults, so the panel's volume and seek
        # keys ignored config.toml entirely and disagreed with the TourBox —
        # which reads the same settings correctly.
        self._ctx = ActionContext(
            mpd=self._mpd,
            router=self._router,
            bus=_LocalPublisher(self),
            volume_step=config.tourbox.steps.volume,
            seek_step=config.tourbox.steps.seek,
            max_volume_jump=config.tourbox.steps.max_volume_jump,
            max_seek_jump=config.tourbox.steps.max_seek_jump,
            seek_step_fast=config.tourbox.steps.seek_fast,
            skip_fade_seconds=config.playback.skip_fade_seconds,
            crossfade_seconds=config.playback.crossfade_seconds,
            crossfade_seconds_alt=config.playback.crossfade_seconds_alt,
            delete=DeleteSettings(
                enabled=config.delete.enabled,
                mode=config.delete.mode,
                trash_dir=config.delete.trash_dir,
                log_path=config.delete.log_path,
                music_dir=config.delete.music_dir,
            ),
            video=self._video,
            video_library=self._video_library,
            mode_file=self._mode_file,
            video_local_device=config.video.local_audio_device,
            # Query the REAL radio state at startup rather than assuming on
            # — a previous session's Wi-Fi-off (see toggle_wifi) could still
            # be in effect if this daemon restarted without an intervening
            # reboot. Blocking, but this runs once at construction, same as
            # the video library's initial rescan() just above.
            wifi_enabled=query_wifi_enabled(),
        )

        # Content-hash cache: key index -> last rendered content key.
        self._rendered: dict[int, str] = {}
        self._deck_lock = threading.RLock()

        self._last_interaction = time.monotonic()
        self._current_brightness = config.streamdeck.brightness
        self._last_state = ""          # for play-start wake detection
        # Shutdown is two-step: first press ARMS, second within this window
        # commits. A single stray press on a 32-key panel must never power the
        # device off mid-album.
        self._shutdown_armed_until = 0.0
        # Delete (music AND video, same cell — see _handle_delete_key) is the
        # same two-step dance, gated by config.toml's [delete] confirm
        # setting. "none" skips the dance entirely (immediate delete, the
        # old behaviour); "two_step" is what actually uses this.
        self._delete_armed_until = 0.0

        # Video page's screen-off toggle (top-right tile). True while the
        # panel has been EXPLICITLY blanked by the user, as opposed to the
        # pre-existing auto dim/blank-after-idle feature (_apply_dimming).
        # Forces brightness to 0 unconditionally until any key press clears
        # it — see _apply_dimming and _on_key's wake-on-any-key handling,
        # which this reuses rather than duplicating.
        self._screen_forced_off = False

        # CPU governor idle power-saving (player/cpugov.py). Captured ONCE,
        # before this process ever changes it, so restoring means "whatever
        # the OS's own default actually was" rather than a hardcoded guess —
        # matters if a future image ships with "schedutil" or anything else
        # instead of "ondemand". None means cpufreq isn't available at all
        # (e.g. this running in a dev sandbox, not on the real Pi) — every
        # governor call below is a no-op in that case, never a crash.
        self._normal_governor = cpugov.read_governor() or "ondemand"
        self._low_power = False

        self._toast_text: str | None = None
        self._toast_until = 0.0

        # Volume-key auto-repeat (see REPEAT_ACTIONS). Only one key can be
        # physically held at a time, so a single slot is enough; a fresh
        # press always stops whatever the previous one was doing first.
        self._repeat_key: int | None = None
        self._repeat_stop = threading.Event()
        self._repeat_thread: threading.Thread | None = None

        self._route_keys: dict[int, str] = {}   # key index -> route id
        self._redraw_event = threading.Event()

        # AirPlay / Bluetooth picker overlays (see PAGE_* above). Both share
        # _picker_return_page since only one can be open at a time.
        self._picker_return_page = _mode.MUSIC
        self._airplay_sinks: list[PwSink] = []
        self._airplay_active_label = ""   # last sink picked, for highlighting
        self._bt_candidates: list[tuple[str, str]] = []
        self._bt_scanning = False
        self._bt_reset_state: str | tuple = "idle"  # "idle" | "working" | ("done", n)
        self._wifi_reconnect_state: str | tuple = "idle"  # "idle" | "working" | ("done", ok)
        self._wifi_fix_state: str | tuple = "idle"  # "idle" | "working" | ("done", ok)

        # Video "up next" browsing — see _video_window().
        self._video_window_offset = 0
        self._video_last_index: int | None = None
        # Folder navigation stack for page-1's mini browser.  Empty = root
        # (normal up-next mode).  Each push descends into a subfolder;
        # the "← Back" folder-slot button pops back one level.
        self._video_browse_stack: list[str] = []

        # Library browser occupies the four keys formerly reserved for art.
        # Constructed lazily on first use so a slow MPD at boot cannot delay
        # the first paint of the transport controls.
        self._browser: LibraryBrowser | None = None

        # Page-2 grid browsers (see PAGE_MUSIC_BROWSE/PAGE_VIDEO_BROWSE
        # above) — same lazy-construction reasoning as self._browser.
        self._music_grid_browser: MusicGridBrowser | None = None
        self._video_grid_browser: VideoGridBrowser | None = None

        # Listen-time and folder-checkpoint tracker. Drives the card shading:
        # blue=playing, green=curated, purple=checkpoint, yellow=listened≥30s.
        from .listen_state import ListenState
        self._listen_state = ListenState()
        self._listen_save_due = time.monotonic() + 60.0  # save every 60 s
        # Load sticker values from MPD into listen_state (best-of-two merge).
        # Done after ListenState.__init__ so the JSON is already loaded first.
        try:
            self._listen_state.load_stickers(self._mpd)
        except Exception:  # noqa: BLE001
            pass
        # Track last-known current_file so we detect song changes for checkpoint.
        self._last_current_file = ""

        self._bus: BusClient | None = None
        if config.ipc.enabled:
            self._bus = BusClient(config.ipc.socket, self._on_bus_event)

    # -- lifecycle ---------------------------------------------------------

    def stop(self, *_: object) -> None:
        LOG.info("shutting down")
        self._running = False
        self._watcher.stop()
        self._redraw_event.set()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        ensure_fonts(self._renderer.theme)
        if self._bus:
            self._bus.start()

        self._bind_route_keys()

        watcher_thread = threading.Thread(target=self._watch_mpd, daemon=True,
                                          name="mpd-idle")
        watcher_thread.start()

        usb_watcher_thread = threading.Thread(target=self._watch_usb, daemon=True,
                                              name="usb-watch")
        usb_watcher_thread.start()

        LOG.info("streamdeck daemon started")

        while self._running:
            if self._deck is None:
                if not self._open_deck():
                    self._sleep(self._config.streamdeck.reconnect_delay)
                    continue
                self._rendered.clear()

            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - USB errors are varied
                LOG.warning("deck error: %s", exc)
                self._close_deck()
                self._sleep(self._config.streamdeck.reconnect_delay)

        self._close_deck()
        self._mpd.close()
        if self._bus:
            self._bus.stop()
        return 0

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(0.1)

    # -- device ------------------------------------------------------------

    def _open_deck(self) -> bool:
        try:
            decks = DeviceManager().enumerate()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("enumerate failed: %s", exc)
            return False

        if not decks:
            LOG.debug("no Stream Deck found")
            return False

        deck = decks[0]
        try:
            deck.open()
            deck.reset()
            deck.set_brightness(self._config.streamdeck.brightness)
            deck.set_key_callback(self._on_key)
        except Exception as exc:  # noqa: BLE001
            LOG.error(
                "cannot open Stream Deck: %s. If this is a permissions error, "
                "install /etc/udev/rules.d/99-streamdeck.rules and confirm the "
                "service user is in the 'plugdev' group.",
                exc,
            )
            try:
                deck.close()
            except Exception:  # noqa: BLE001
                pass
            return False

        self._deck = deck
        self._current_brightness = self._config.streamdeck.brightness
        self._last_interaction = time.monotonic()

        LOG.info(
            "Stream Deck connected: %s (%d keys, %sx%s px)",
            deck.deck_type(),
            deck.key_count(),
            *deck.key_image_format()["size"],
        )
        if deck.key_count() != layout.KEY_COUNT:
            LOG.warning(
                "layout expects %d keys but this deck has %d — "
                "keys beyond the layout will stay blank",
                layout.KEY_COUNT, deck.key_count(),
            )
        return True

    def _close_deck(self) -> None:
        with self._deck_lock:
            if self._deck is not None:
                try:
                    self._deck.reset()
                    self._deck.close()
                except Exception:  # noqa: BLE001
                    pass
                self._deck = None
                self._rendered.clear()
                LOG.info("Stream Deck disconnected")

    def _ensure_browser(self) -> LibraryBrowser | None:
        """Create the browser on first use; never let it break the panel.

        A failure here (MPD down, empty database) must degrade to blank browser
        keys, not take the transport controls with it.
        """
        if self._browser is None:
            try:
                self._browser = LibraryBrowser(self._mpd)
            except Exception:  # noqa: BLE001
                LOG.exception("could not initialise the library browser")
                return None
        return self._browser

    def _invalidate_browser_keys(self) -> None:
        """Force the browser keys to repaint after a navigation."""
        for key, defn in layout.LAYOUT.items():
            if defn.kind in ("browse_entry", "browse_back", "browse_page", "browse_root",
                             "current_folder"):
                self._rendered.pop(key, None)

    def _ensure_music_grid_browser(self) -> MusicGridBrowser | None:
        """Create the page-2 music browser on first use — same
        never-let-it-break-the-panel reasoning as _ensure_browser."""
        if self._music_grid_browser is None:
            try:
                self._music_grid_browser = MusicGridBrowser(self._mpd)
            except Exception:  # noqa: BLE001
                LOG.exception("could not initialise the music page-2 browser")
                return None
        return self._music_grid_browser

    def _ensure_video_grid_browser(self) -> VideoGridBrowser | None:
        if self._video_grid_browser is None:
            if self._video_library is None:
                return None
            try:
                self._video_grid_browser = VideoGridBrowser(self._video_library)
            except Exception:  # noqa: BLE001
                LOG.exception("could not initialise the video page-2 browser")
                return None
        return self._video_grid_browser

    def _invalidate_grid_keys(self, is_video: bool) -> None:
        """Force a page-2 grid's navigable keys to repaint after a
        navigation/page turn — same idea as _invalidate_browser_keys."""
        layout_dict = layout.LAYOUT_VIDEO_BROWSE if is_video else layout.LAYOUT_BROWSE
        for key, defn in layout_dict.items():
            if defn.kind in ("grid_updir", "grid_entry", "grid_page", "storage_select"):
                self._rendered.pop(key, None)

    def _watch_usb(self) -> None:
        """Background thread: poll for the USB drive's physical presence
        and clear every trace of it the moment it disappears.

        Nothing else in this codebase notices an unplug on its own — MPD's
        idle watcher only fires on database/player events, and both page-2
        grid browsers only re-read their listing on the NEXT navigation.
        Without this, a yanked drive left the Stream Deck showing a frozen,
        stale USB listing (and letting you press "play" on files that no
        longer exist) until something else happened to poke a refresh —
        reported live as exactly that: stale cache, unplayable items.

        On the mounted -> unmounted transition this checks music and video
        independently (one can be on USB while the other is on Internal)
        and falls each one back to Internal if it was actively pointed at
        USB, then resets both page-2 grid browsers and the page-1 library
        browser to their root so no key is left referencing vanished
        content. A forced full repaint follows since storage labels, route
        availability, and every browser/grid key just changed at once.
        """
        was_mounted = usb_mounted()
        while self._running:
            time.sleep(USB_POLL_SECONDS)
            if not self._running:
                return
            mounted = usb_mounted()
            if mounted == was_mounted:
                continue
            was_mounted = mounted

            if mounted:
                LOG.info("USB drive detected")
                continue

            LOG.warning("USB drive removed — falling back any active USB source to Internal")
            fell_back: list[str] = []

            if music_storage_label() == "USB":
                _apply_music_storage_set(self._ctx, "internal")
                fell_back.append("Music")
            if video_storage_label() == "USB":
                _apply_video_storage_set(self._ctx, "internal")
                fell_back.append("Video")

            if self._music_grid_browser is not None:
                try:
                    self._music_grid_browser.goto("")
                except Exception:  # noqa: BLE001
                    LOG.exception("failed to reset music page-2 browser after USB removal")
            self._video_browse_stack.clear()
            if self._video_grid_browser is not None:
                try:
                    self._video_grid_browser.goto("")
                except Exception:  # noqa: BLE001
                    LOG.exception("failed to reset video page-2 browser after USB removal")
            if self._browser is not None and not self._browser.at_root:
                try:
                    self._browser.go_root()
                except Exception:  # noqa: BLE001
                    LOG.exception("failed to reset library browser after USB removal")

            if fell_back:
                self._toast_text = ("USB removed: " + "/".join(fell_back))[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS

            self._rendered.clear()
            self._redraw_event.set()

    def _bind_route_keys(self) -> None:
        """Bind ROUTE_SLOTS (now 2 -- index(3,1)/index(3,2)) to every
        configured route EXCEPT "local", which moved to its own fixed key
        on page 2 (LAYOUT_BROWSE/LAYOUT_VIDEO_BROWSE index(3,7)) on both
        pages. Filtering it out BEFORE zipping matters: with only 2 slots
        left, zipping against the unfiltered config.routes (local, bt,
        airplay, airplay_arylic in config.toml order) would bind slot 0 to
        "local" again and silently drop "airplay" off the panel entirely.
        """
        self._route_keys.clear()
        non_local_routes = [r for r in self._config.routes if r.id != "local"]
        for slot, route in zip(layout.ROUTE_SLOTS, non_local_routes):
            self._route_keys[slot] = route.id
        LOG.debug("bound %d route keys", len(self._route_keys))

    # -- MPD event loop ----------------------------------------------------

    def _watch_mpd(self) -> None:
        """Background thread: block in MPD idle, wake the render loop."""
        for subsystems in self._watcher.events(IDLE_SUBSYSTEMS):
            if not self._running:
                break
            LOG.debug("mpd idle: %s", ", ".join(sorted(subsystems)))
            if "__reconnected__" in subsystems:
                self._rendered.clear()   # force a full repaint
            if "database" in subsystems and self._browser is not None:
                # A library scan finished; the listing we are showing is stale.
                try:
                    self._browser.refresh()
                    self._invalidate_browser_keys()
                except Exception:  # noqa: BLE001
                    LOG.exception("browser refresh after database update failed")
            if "database" in subsystems and self._music_grid_browser is not None:
                try:
                    self._music_grid_browser.refresh()
                    self._invalidate_grid_keys(False)
                except Exception:  # noqa: BLE001
                    LOG.exception("music page-2 browser refresh after database update failed")
            self._redraw_event.set()

    def _on_bus_event(self, message: dict) -> None:
        """Optimistic updates from the TourBox daemon.

        AirPlay adds a couple of seconds of buffering, so waiting for MPD to
        confirm a route change makes the panel feel broken. We paint the toast
        immediately and let the subsequent idle event reconcile the truth.
        """
        event = message.get("event")
        if event == "toast":
            self._toast_text = str(message.get("text", ""))[:16]
            self._toast_until = time.monotonic() + TOAST_SECONDS
            self._last_interaction = time.monotonic()
            self._redraw_event.set()
        elif event == "route":
            # tourbox-player runs its OWN OutputRouter instance -- a separate
            # process, separate in-memory _current_id cache (see
            # OutputRouter.mark_current()'s docstring). This event carries
            # the id of the route it just switched to (published from
            # actions.py's cycle_output/cycle_output_back and from this
            # daemon's own startup default-route fallback). Previously this
            # branch only set the redraw flag and left THIS router's cache
            # untouched, so the panel kept re-rendering whatever route IT
            # last switched to itself -- found live: the TourBox switched
            # output to Bluetooth, the Stream Deck kept the Local key lit
            # indefinitely even though audio was actually playing over
            # Bluetooth the whole time, because nothing ever told this
            # router the ground truth had changed.
            route_id = message.get("id")
            if route_id:
                self._router.mark_current(str(route_id))
            else:
                # Defensive: an id-less "route" event should not normally
                # happen (both publish call sites always include one), but
                # if it ever does, re-derive from MPD/PipeWire truth rather
                # than silently keeping a possibly-wrong cached value.
                self._router.detect_current()
            self._redraw_event.set()

    def _sync_page_to_playback(self, video_status: dict) -> None:
        """Follow reality, not just button presses.

        Requested specifically: the panel should switch to video controls
        whenever an .skp is actually playing, not only when the Video key
        was the thing that started it — e.g. the TourBox noticing first, or
        any future trigger that isn't a Stream Deck press. Whichever daemon
        notices first writes the mode file so the other follows (see
        player/mode.py) — safe to do redundantly from both, writing the same
        value twice is a no-op.

        Deliberately one-directional: entering video mode is automatic,
        leaving it is not (the Music key still has to be pressed). Pausing a
        song to read something shouldn't kick you back to the music page.

        Checks "playing", NOT "loaded" — mpv runs with --keep-open=yes, so a
        file stays "loaded" indefinitely even after it is paused, including
        by exit_video_mode itself pausing it on the way out. Checking
        "loaded" here meant this fired on every tick once any video had ever
        played, overriding the Music key the instant it was pressed and
        permanently trapping the panel on the video page — found live, user
        reported being stuck with no way back to music or to shutdown.
        """
        if not video_status.get("playing"):
            return
        if self._page in (_mode.VIDEO, PAGE_AIRPLAY_PICKER, PAGE_BT_PICKER,
                          PAGE_MUSIC_BROWSE, PAGE_VIDEO_BROWSE):
            return
        if self._mpd.status().get("state") == "play":
            self._mpd.toggle_pause()
        self._page = _mode.VIDEO
        self._rendered.clear()
        _mode.write_mode(self._mode_file, _mode.VIDEO)
        self._last_interaction = time.monotonic()
        self._redraw_event.set()
        LOG.info("page -> video [auto: mpv playing]")

    def _tick(self) -> None:
        """One render pass, then wait for the next event or the tick interval."""
        video_status = self._video.status() if self._video is not None else {}
        self._sync_page_to_playback(video_status)
        state = (self._collect_video_state(video_status)
                 if self._page in (_mode.VIDEO, PAGE_VIDEO_BROWSE)
                 else self._collect_state())

        # Starting playback counts as activity: the panel should come back up
        # when a track starts, not stay dark until someone touches a key.
        if state["state"] == "play" and self._last_state != "play":
            self._last_interaction = time.monotonic()
        self._last_state = state["state"]

        if self._shutdown_armed_until:
            if time.monotonic() >= self._shutdown_armed_until:
                self._shutdown_armed_until = 0.0
                LOG.info("shutdown disarmed (timed out)")
            # Power lives at the same cell on both pages now — invalidate it
            # on whichever one is actually showing so the "CONFIRM Ns"
            # countdown visibly ticks down regardless of which page you
            # pressed it from.
            for k, d in layout.LAYOUT.items():
                if d.kind == "power":
                    self._rendered.pop(k, None)
            for k, d in layout.LAYOUT_VIDEO.items():
                if d.kind == "power":
                    self._rendered.pop(k, None)

        if self._delete_armed_until:
            if time.monotonic() >= self._delete_armed_until:
                self._delete_armed_until = 0.0
                LOG.info("delete disarmed (timed out)")
            # Same reasoning as Power above — Delete now lives on FOUR
            # layouts (page 1 and the browse-grid page 2, music and video),
            # so invalidate all four. Not strictly required for the
            # countdown itself to update (its content key already embeds
            # `remaining`, which changes every second — see _render_key's
            # "delete" branch), but keeps this belt-and-suspenders sweep
            # consistent with Power's and correct across a page switch
            # mid-countdown.
            for layout_dict in (layout.LAYOUT, layout.LAYOUT_VIDEO,
                                layout.LAYOUT_BROWSE, layout.LAYOUT_VIDEO_BROWSE):
                for k, d in layout_dict.items():
                    if d.kind == "delete":
                        self._rendered.pop(k, None)

        self._apply_dimming(playing=state["state"] == "play")
        self._apply_low_power(playing=state["state"] == "play")

        # Listen-state tracking: accumulate listen time and update checkpoint.
        is_playing = state["state"] == "play"
        current_file = state.get("current_file", "")
        self._listen_state.tick(current_file or None, is_playing)
        if current_file and current_file != self._last_current_file:
            # Song changed — update checkpoint for the new song's folder.
            from pathlib import Path as _Path
            folder = str(_Path(current_file).parent)
            if folder in (".", "/"):
                folder = ""
            self._listen_state.set_checkpoint(folder, current_file)
            self._last_current_file = current_file
        # Periodically persist listen state to JSON + MPD stickers + mac tags.
        now = time.monotonic()
        if now >= self._listen_save_due:
            self._listen_state.save()
            try:
                self._listen_state.save_stickers(self._mpd)
            except Exception:  # noqa: BLE001
                pass
            try:
                from pathlib import Path as _TagPath
                _music_root = _TagPath("/home/rpi/Music-internal")
                self._listen_state.sync_mac_tags(_music_root)
            except Exception:  # noqa: BLE001
                pass
            self._listen_save_due = now + 60.0

        self._render_all(state)

        # Wake on: an MPD idle event, a bus message, or the tick interval when
        # playing (so elapsed time advances). When paused we can sleep longer.
        # Tick every second while the shutdown or delete countdown is
        # running so the "CONFIRM Ns" label counts down and the key visibly
        # disarms itself.
        armed = (time.monotonic() < self._shutdown_armed_until
                 or time.monotonic() < self._delete_armed_until)
        timeout = (
            self._config.streamdeck.tick_seconds
            if state["state"] == "play" or self._toast_text or armed
            else 5.0
        )
        self._redraw_event.wait(timeout)
        self._redraw_event.clear()

    def _collect_state(self) -> dict:
        status = self._mpd.status()
        song = self._mpd.current_song()

        try:
            elapsed = float(status.get("elapsed", 0.0))
        except (TypeError, ValueError):
            elapsed = 0.0
        try:
            duration = float(status.get("duration", 0.0))
        except (TypeError, ValueError):
            duration = 0.0

        volume_raw = status.get("volume")
        volume = None
        if volume_raw is not None and volume_raw != "-1":
            try:
                volume = int(volume_raw)
            except ValueError:
                volume = None

        current_route = self._router.current()
        available = {r.id for r in self._router.available_routes()}

        return {
            "state": status.get("state", "stop"),
            # The browser highlights the row matching this URI.
            "current_file": song.get("file", ""),
            "title": song.get("title") or Path(song.get("file", "")).stem or "",
            "artist": song.get("artist", ""),
            "album": song.get("album", ""),
            "elapsed": elapsed,
            "duration": duration,
            "volume": volume,
            "random": status.get("random") == "1",
            "repeat": status.get("repeat") == "1",
            "single": status.get("single") == "1",
            "consume": status.get("consume") == "1",
            "song_index": status.get("song"),
            "playlist_length": status.get("playlistlength", "0"),
            "bitrate": status.get("bitrate", ""),
            "audio": status.get("audio", ""),
            "route_id": current_route.id if current_route else "",
            "route_label": current_route.label if current_route else "None",
            "available_routes": available,
        }

    def _collect_video_state(self, status: dict | None = None) -> dict:
        """Same dict SHAPE as ``_collect_state`` (state/title/artist/elapsed/
        duration/song_index/playlist_length) so the video page can reuse the
        existing ``now_playing_title``/``progress``/``playpause``/
        ``queue_position`` render kinds unmodified — only ``audio_track`` and
        ``video_back`` are genuinely new. See layout.LAYOUT_VIDEO.
        """
        if status is None:
            status = self._video.status() if self._video is not None else {}
        library = self._video_library
        current_route = self._router.current()
        available = {r.id for r in self._router.available_routes()}

        # Resync this process's OWN VideoLibrary.current/.index against
        # whatever mpv is ACTUALLY playing right now, before trusting it for
        # anything below. Two separate daemon processes (this one and the
        # TourBox daemon) each hold an independent in-memory VideoLibrary —
        # auto-advance (player/video_continuous.py) and any skip made via the
        # TourBox controller run inside THAT OTHER process and only ever
        # update ITS copy, never this one. Reported live: the "now playing"
        # tile and the page-2 browser's green highlight stayed frozen on a
        # song several tracks back, because this process's `library.current`
        # genuinely never moved — nothing had told it to. mpv itself is the
        # one thing both processes share and can trust (same role MPD plays
        # for music mode, which has never had this problem), so every render
        # pass re-derives the correct entry from mpv's live loaded path
        # rather than assuming this process's own last-known index is still
        # right. Cheap: an in-memory list scan, no I/O, only runs the lookup
        # when the path has actually changed.
        mpv_path = status.get("path", "")
        if library is not None and mpv_path:
            current = library.current
            if current is None or current.path != mpv_path:
                library.select_by_path(mpv_path)

        entry = library.current if library is not None else None
        return {
            "state": "play" if status.get("playing") else "pause",
            "title": (entry.song if entry else status.get("title", "")) or "Nothing loaded",
            "artist": entry.singer if entry else "",
            "elapsed": status.get("elapsed", 0.0),
            "duration": status.get("duration", 0.0),
            "song_index": str(library.index) if library and len(library) else None,
            "playlist_length": str(len(library)) if library else "0",
            "track_label": status.get("track_label", ""),
            "route_id": current_route.id if current_route else "",
            "available_routes": available,
            # The page-2 grid browser's "is this row playing?" check — see
            # _render_grid_key — needs the full path, not just the song
            # title, to unambiguously match a GridEntry.ref.
            "current_video_path": entry.path if entry else "",
            # mpv's own volume, entirely separate from MPD's — see
            # VideoCommander.volume(). Reuses the "volume" render kind
            # unmodified, same as the rest of this dict's music-page shape.
            "volume": self._video.volume() if self._video is not None else None,
        }

    def _video_browse_current_path(self) -> str:
        """Relative path (from library root) of the currently browsed folder.
        Empty string means the library root (= normal up-next mode)."""
        return os.path.join(*self._video_browse_stack) if self._video_browse_stack else ""

    def _video_window(self) -> list:
        """Up to 4 items for the video page's entry slots.

        Normal mode (browse stack empty):
            list of ``(library_index, VideoEntry) | None``
            — the usual "up next" circular window.

        Browse mode (browse stack non-empty):
            list of ``("dir", name, full_path) | ("file", name, full_path) | None``
            — immediate children of the current browse path, dirs first.
            Pressing a dir slot navigates into it; pressing a file slot plays it.
        """
        library = self._video_library
        if library is None or len(library) == 0:
            return [None, None, None, None]

        # ---- Browse mode ----
        if self._video_browse_stack:
            path = self._video_browse_current_path()
            try:
                items = library.list_dir(path)   # (name, is_dir, full_path)
            except Exception:
                LOG.exception("list_dir(%s) failed", path)
                return [None, None, None, None]
            offset = self._video_window_offset
            result = []
            for i in range(4):
                j = offset + i
                if j < len(items):
                    name, is_dir, full = items[j]
                    result.append(("dir" if is_dir else "file", name, full))
                else:
                    result.append(None)
            return result

        # ---- Normal up-next mode ----
        total = len(library)
        current = library.index
        if self._video_last_index != current:
            self._video_last_index = current
            self._video_window_offset = 0
        entries = library.entries()
        start = current + 1 + self._video_window_offset
        return [(idx := (start + i) % total, entries[idx]) for i in range(4)]

    def _enter_airplay_picker(self, return_page: str) -> None:
        """List every currently-live sink for a *configured* AirPlay route.

        Deliberately NOT every `raop_sink` PipeWire can see: module-raop-
        discover creates one sink per _raop._tcp announcer on the LAN, which
        in practice includes laptops with AirPlay receiving enabled, not
        just real speakers (see config.toml's [[routes]] comment — this bit
        a fixed-route match too, before the pw_sink patterns were narrowed
        to specific mDNS hostnames). Restricting the picker to routes
        someone deliberately added with icon="airplay" keeps a stranger's
        MacBook from ever showing up as something to route karaoke audio
        to, while still surfacing every such route — including
        "airplay_arylic", which has no dedicated Stream Deck key of its own.
        """
        self._picker_return_page = return_page
        live_sinks = self._router.pw.list_sinks()
        picks: list[PwSink] = []
        for route in self._config.routes:
            if route.icon != "airplay" or not route.pw_sink:
                continue
            sink = next((s for s in live_sinks if s.matches(route.pw_sink)), None)
            if sink is not None:
                picks.append(sink)
        self._airplay_sinks = picks
        self._page = PAGE_AIRPLAY_PICKER
        self._wifi_reconnect_state = "idle"
        self._wifi_fix_state = "idle"
        self._rendered.clear()
        self._redraw_event.set()

    def _enter_bt_picker(self, return_page: str) -> None:
        """Same two-phase idea as the AirPlay picker, but there is no
        PipeWire node to read a candidate list from — bluetoothd has to
        actually go find nearby devices, which takes a few seconds.

        Show whatever bluetoothd already knows about immediately (usually
        just the QC45, already paired from earlier), then kick a scan in
        the background and repaint the slots once it finishes. Selecting a
        slot works the same whichever list it came from.
        """
        self._picker_return_page = return_page
        self._bt_candidates = _bt.known_audio_devices()
        self._bt_scanning = True
        self._bt_reset_state = "idle"
        self._page = PAGE_BT_PICKER
        self._rendered.clear()
        self._toast_text = "Scanning..."
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._redraw_event.set()

        def on_scan_done(devices: list[tuple[str, str]]) -> None:
            self._bt_candidates = devices
            self._bt_scanning = False
            for k, d in layout.LAYOUT_BT_PICKER.items():
                if d.kind == "bt_entry":
                    self._rendered.pop(k, None)
            self._redraw_event.set()

        _bt.scan_async(on_scan_done)

    def _on_bt_entry_press(self, slot: int) -> None:
        candidate = self._bt_candidates[slot] if slot < len(self._bt_candidates) else None
        if candidate is None:
            return
        mac, name = candidate
        return_page = self._picker_return_page

        self._toast_text = "Connecting..."
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._page = return_page
        self._rendered.clear()
        self._redraw_event.set()

        def on_done(ok: bool) -> None:
            if ok:
                bt_route = next((r for r in self._router.routes if r.id == "bt"), None)
                if bt_route is not None:
                    self._router.switch_to(bt_route)
                if return_page == _mode.VIDEO and self._video is not None:
                    self._video.set_audio_device("auto")
                    self._video.reload_audio()
                    self._ctx.video_local_was_enabled = False
                self._toast_text = name[:16]
            else:
                self._toast_text = "BT failed"
            self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()

        _bt.pair_and_connect_async(mac, on_done)

    def _on_bt_reset_press(self) -> None:
        """Clears every paired-but-disconnected device's stuck bond so it
        can be paired fresh, without needing SSH — see bt.py's
        reset_failed_pairings_blocking for why `bluetoothctl remove` alone
        is sometimes not enough on its own."""
        if self._bt_reset_state == "working":
            return  # already running, ignore a double-press
        self._bt_reset_state = "working"
        self._rendered.pop(_BT_RESET_KEY, None)
        self._toast_text = "Resetting..."
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._redraw_event.set()

        def on_done(count: int) -> None:
            self._bt_reset_state = ("done", count)
            self._rendered.pop(_BT_RESET_KEY, None)
            self._toast_text = f"Reset {count}" if count else "Nothing stuck"
            self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()

        _bt.reset_failed_pairings_async(on_done)

    def _on_wifi_reconnect_press(self) -> None:
        """Forces a fresh DHCP lease + reassociation -- the fix for
        "connected but flaky"/stale-lease symptoms. See player/wifi.py."""
        if self._wifi_reconnect_state == "working":
            return  # already running, ignore a double-press
        self._wifi_reconnect_state = "working"
        self._rendered.pop(_WIFI_RECONNECT_KEY, None)
        self._toast_text = "Reconnecting..."
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._redraw_event.set()

        def on_done(ok: bool) -> None:
            self._wifi_reconnect_state = ("done", ok)
            self._rendered.pop(_WIFI_RECONNECT_KEY, None)
            self._toast_text = "Wifi reconnected" if ok else "Reconnect failed"
            self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()

        _wifi.reconnect_async(on_done)

    def _on_wifi_fix_press(self) -> None:
        """Restarts NetworkManager -- reloads wifi-powersave-off.conf and
        every other conf.d drop-in, and forces a full Wi-Fi stack re-init.
        The heavier fallback for when a plain reconnect doesn't help. See
        player/wifi.py."""
        if self._wifi_fix_state == "working":
            return  # already running, ignore a double-press
        self._wifi_fix_state = "working"
        self._rendered.pop(_WIFI_FIX_KEY, None)
        self._toast_text = "Restarting NM..."
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._redraw_event.set()

        def on_done(ok: bool) -> None:
            self._wifi_fix_state = ("done", ok)
            self._rendered.pop(_WIFI_FIX_KEY, None)
            self._toast_text = "NM restarted" if ok else "NM restart failed"
            self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()

        _wifi.restart_networking_async(on_done)

    # -- rendering ---------------------------------------------------------

    def _apply_dimming(self, playing: bool = False) -> None:
        """Dim, then blank, the panel after inactivity — to save battery.

        Crucially, ``playing`` gates BOTH the dim and the full blank. Without
        it the panel visibly dims or goes completely dark mid-album, because
        ``_last_interaction`` only advances on a key press or a toast —
        playback itself is not "interaction". On a music player that reads as
        the device having crashed or dying, and you have to press a key you
        cannot see to find out otherwise.

        With ``keep_awake_while_playing`` (the default), the panel stays at
        full brightness for as long as something is actively playing — music
        OR video — and only starts dimming/blanking once playback actually
        stops. Set it false if you would rather have the power savings on a
        battery build and don't mind the panel dimming mid-album.
        """
        cfg = self._config.streamdeck

        if self._screen_forced_off:
            # The Video page's screen-off tile was pressed — this wins over
            # everything below, including keep_awake_while_playing, until a
            # key press clears it (see _on_key). Without this override the
            # very next tick would see "still playing, not idle" and
            # immediately re-brighten the panel a fraction of a second after
            # the user explicitly turned it off.
            target = 0
        else:
            idle_for = time.monotonic() - self._last_interaction
            awake = playing and cfg.keep_awake_while_playing

            may_blank = bool(cfg.blank_after_seconds) and not awake
            may_dim = bool(cfg.dim_after_seconds) and not awake

            if may_blank and idle_for > cfg.blank_after_seconds:
                target = 0
            elif may_dim and idle_for > cfg.dim_after_seconds:
                target = cfg.dim_brightness
            else:
                target = cfg.brightness

        if target != self._current_brightness:
            with self._deck_lock:
                if self._deck is not None:
                    try:
                        self._deck.set_brightness(target)
                        self._current_brightness = target
                        LOG.debug("brightness -> %d%%", target)
                    except Exception as exc:  # noqa: BLE001
                        LOG.debug("set_brightness failed: %s", exc)

    def _apply_low_power(self, playing: bool) -> None:
        """Drop the CPU governor after IDLE_POWER_SECONDS of no playback, to
        cut down on idle heat — reported live, the box runs noticeably warm
        sitting doing nothing between songs/videos.

        Independent of _apply_dimming/screen brightness on purpose: the
        panel might still be lit (playing state just changed, or dimming is
        disabled in config) while the CPU is already idle enough to throttle,
        or vice versa — these are two different "nothing is happening"
        signals and gating one on the other would just make both less
        responsive than they need to be.

        Restoring is NOT gated on being "idle" the way engaging it is —
        the instant something actually needs the CPU (playback resumes, on
        its own via auto-advance or from a key press), full speed comes back
        immediately, never waiting out any timer.
        """
        if playing:
            if self._low_power:
                if cpugov.set_governor(self._normal_governor):
                    LOG.info("CPU -> normal power (%s)", self._normal_governor)
                self._low_power = False
            return

        if self._low_power:
            return

        idle_for = time.monotonic() - self._last_interaction
        if idle_for > IDLE_POWER_SECONDS:
            if cpugov.set_governor(cpugov.LOW_POWER_GOVERNOR):
                LOG.info("CPU -> low power (%s, idle %ds, not playing)",
                          cpugov.LOW_POWER_GOVERNOR, int(idle_for))
            # Mark it handled either way — a permission/hardware failure
            # here (see cpugov.set_governor's docstring) should not retry
            # every single tick forever; it'll get another chance the next
            # time playback stops and IDLE_POWER_SECONDS elapses again.
            self._low_power = True

    def _wake_from_low_power(self) -> None:
        """Restore normal CPU speed immediately on any key press, same
        reasoning as waking the screen — see _on_key. Cheap to call even
        when already at normal power (set_governor is idempotent and
        cpugov.py no-ops entirely on a board without cpufreq)."""
        if self._low_power:
            if cpugov.set_governor(self._normal_governor):
                LOG.info("CPU -> normal power (%s) [key press]", self._normal_governor)
            self._low_power = False

    def _render_all(self, state: dict) -> None:
        if self._deck is None:
            return

        toast_active = self._toast_text and time.monotonic() < self._toast_until
        if not toast_active and self._toast_text:
            self._toast_text = None

        key_count = min(self._deck.key_count(), layout.KEY_COUNT)

        if self._page == PAGE_AIRPLAY_PICKER:
            for key in range(key_count):
                definition = layout.airplay_picker_key_def(key)
                if definition is None:
                    self._push(key, "blank", self._renderer.blank)
                    continue
                self._render_key(key, definition, state)
            return

        if self._page == PAGE_BT_PICKER:
            for key in range(key_count):
                definition = layout.bt_picker_key_def(key)
                if definition is None:
                    self._push(key, "blank", self._renderer.blank)
                    continue
                self._render_key(key, definition, state)
            return

        if self._page == _mode.VIDEO:
            for key in range(key_count):
                if toast_active and key == layout.TOAST_SLOT_VIDEO:
                    self._push(key, "toast:" + str(self._toast_text),
                               lambda: self._renderer.toast(self._toast_text or ""))
                    continue
                definition = layout.video_key_def(key)
                if definition is None:
                    self._push(key, "blank", self._renderer.blank)
                    continue
                self._render_key(key, definition, state)
            return

        if self._page == PAGE_MUSIC_BROWSE:
            browser = self._ensure_music_grid_browser()
            for key in range(key_count):
                definition = layout.browse_grid_key_def(key)
                if definition is None or browser is None:
                    self._push(key, "blank", self._renderer.blank)
                    continue
                self._render_grid_key(key, definition, browser, False, state)
            return

        if self._page == PAGE_VIDEO_BROWSE:
            browser = self._ensure_video_grid_browser()
            for key in range(key_count):
                definition = layout.video_browse_grid_key_def(key)
                if definition is None or browser is None:
                    self._push(key, "blank", self._renderer.blank)
                    continue
                self._render_grid_key(key, definition, browser, True, state)
            return

        for key in range(key_count):
            if toast_active and key == layout.TOAST_SLOT:
                self._push(key, "toast:" + str(self._toast_text),
                           lambda: self._renderer.toast(self._toast_text or ""))
                continue

            if key in self._route_keys:
                self._render_route_key(key, state)
                continue

            definition = layout.key_def(key)
            if definition is None:
                self._push(key, "blank", self._renderer.blank)
                continue

            self._render_key(key, definition, state)

    def _render_route_key(self, key: int, state: dict) -> None:
        route_id = self._route_keys[key]
        route = self._config.route_by_id(route_id)
        if route is None:
            self._push(key, "blank", self._renderer.blank)
            return

        active = state["route_id"] == route.id
        available = route.id in state["available_routes"]
        content = f"route:{route.id}:{active}:{available}"
        self._push(
            key, content,
            lambda: self._renderer.route(route.label, active, available),
        )

    def _render_grid_key(self, key: int, definition: layout.KeyDef, browser,
                         is_video: bool, state: dict) -> None:
        """Shared rendering for BOTH page-2 grid browsers (music and video)
        — parametrized by which browser instance and an ``is_video`` flag
        rather than duplicated per page, since the geometry (see
        layout.LAYOUT_BROWSE/LAYOUT_VIDEO_BROWSE) and every kind used here
        are identical between them.
        """
        kind = definition.kind

        if kind == "grid_updir":
            at_root = browser.at_root
            self._push(key, f"grid-updir:{at_root}",
                       lambda: self._renderer.browse_nav("Back", enabled=not at_root))
            return

        if kind == "grid_page":
            symbol = definition.params.get("symbol", "rew")
            # Distinct colour (theme.warn) so the two paging keys read as
            # paging controls, not folders/files — see the params/direction
            # in layout._build_grid_layout.
            self._push(key, f"grid-page:{symbol}",
                       lambda: self._renderer.glyph(symbol, caption="Page",
                                                    colour=self._renderer.theme.warn))
            return

        if kind == "grid_entry":
            slot = definition.params.get("slot", 0)
            window = browser.visible()
            entry = window[slot] if slot < len(window) else None
            if entry is None:
                self._push(key, f"grid-empty:{slot}",
                           lambda: self._renderer.browse_entry("", is_dir=False, empty=True))
                return
            current_ref = (state.get("current_video_path", "") if is_video
                           else state.get("current_file", ""))
            playing = bool(current_ref) and entry.ref == current_ref
            if is_video:
                shd = None
            else:
                shd = self._listen_state.card_shade(
                    entry.ref, playing, browser.path, entry.is_dir)
            self._push(
                key, f"grid:{entry.ref}:{shd}",
                lambda: self._renderer.browse_entry(
                    entry.name, is_dir=entry.is_dir, is_video=is_video,
                    is_playing=playing, shade=shd),
            )
            return

        if kind == "video_back":
            # Upward chevron, matching page 1's "page2_enter" downward one
            # (both theme.accent) -- the two ends of the page-1/page-2
            # relationship read as a matched pair: down to enter, up to
            # leave.
            text = definition.label or "Back"
            self._push(key, f"grid-back:{text}",
                       lambda: self._renderer.glyph(
                           "chevron_up", caption=text, colour=self._renderer.theme.accent))
            return

        if kind == "label":
            # The relocated Scan key (update_database / video_rescan).
            text = definition.label
            sub = definition.params.get("sub", "")
            self._push(key, f"label:{text}:{sub}",
                       lambda: self._renderer.label(text, sub=sub))
            return

        if kind in ("route_fixed", "video_route"):
            # "route_fixed" (music page 2's relocated Local key) mirrors
            # "video_route" exactly (see layout.LAYOUT_BROWSE index(3,7)'s
            # comment) — both look up the route by params["route_id"] from
            # ctx.router.routes and render it identically.
            route_id = definition.params.get("route_id", "")
            route = self._config.route_by_id(route_id)
            if route is None:
                self._push(key, "blank", self._renderer.blank)
            else:
                active = state.get("route_id") == route.id
                available = route.id in state.get("available_routes", set())
                self._push(
                    key, f"groute:{route.id}:{active}:{available}",
                    lambda: self._renderer.route(route.label, active, available),
                )
            return

        if kind == "storage_select":
            # Direct Internal/USB jump keys -- lit when it's the currently
            # active source, dimmed-unavailable for USB specifically when
            # the drive isn't mounted (reusing the same lit/dim/unavailable
            # visual language route() already uses for output routes).
            target = definition.params.get("target", "internal")
            text = definition.label or target.capitalize()
            if is_video:
                current = video_storage_label()
                available = target == "internal" or video_usb_available()
            else:
                current = music_storage_label()
                available = target == "internal" or music_usb_available()
            active = current.lower() == target
            self._push(
                key, f"storage-select:{target}:{current}:{available}",
                lambda: self._renderer.route(text, active, available),
            )
            return

        # Fallback: Curate ("glyph"), Delete ("delete"), and the copied
        # transport keys (Play/Pause, Seek +15s, Next) added directly to the
        # browse grid are handled by the exact same rendering logic page 1
        # already has in _render_key — no need to duplicate it here. `state`
        # is already the correct one for this page (video vs music — see
        # _tick's state = _collect_video_state(...) if PAGE_VIDEO_BROWSE
        # else _collect_state()), so this "just works" the same way it does
        # on page 1.
        self._render_key(key, definition, state)

    def _render_key(self, key: int, definition: layout.KeyDef, state: dict) -> None:
        kind = definition.kind
        params = definition.params

        if kind == "blank":
            self._push(key, "blank", self._renderer.blank)

        elif kind == "playpause":
            playing = state["state"] == "play"
            symbol = "pause" if playing else "play"
            colour = self._renderer.theme.active if playing else self._renderer.theme.fg
            self._push(
                key, f"playpause:{state['state']}",
                lambda: self._renderer.glyph(symbol, colour=colour),
            )

        elif kind == "glyph":
            symbol = params.get("symbol", "speaker")
            # caption_fmt derives the label from config so the key can never
            # claim "+5s" while actually seeking 15. Hardcoding the number in
            # the layout meant changing config.toml silently made the panel lie.
            caption = params.get("caption", "")
            if "caption_fmt" in params:
                caption = params["caption_fmt"].format(
                    seek=self._config.tourbox.steps.seek,
                    seek_fast=self._config.tourbox.steps.seek_fast,
                    volume=self._config.tourbox.steps.volume,
                )
            # Optional static colour override (e.g. Curate's red heart) --
            # every other glyph key leaves this unset and keeps the default
            # theme.fg colour, same as before this was added.
            colour = params.get("colour")
            self._push(
                key, f"glyph:{symbol}:{caption}:{colour}",
                lambda: self._renderer.glyph(symbol, caption=caption, colour=colour),
            )

        elif kind == "label":
            text = definition.label
            # The Internal/USB storage-source tiles render their `sub` LIVE
            # from the filesystem (see actions.music_storage_label /
            # video_storage_label) rather than a static params["sub"] —
            # the whole point is that it always reflects which symlink
            # target is actually active right now, never a stale cached
            # label that could drift after a toggle.
            if definition.action == "toggle_storage_source":
                # Used to show "Scanning..." here while MPD's post-swap
                # `update` finished (a big library took 60-90+ seconds,
                # which made the button feel stuck). toggle_storage_source
                # no longer triggers a scan at all -- see its docstring in
                # actions.py -- so the symlink flip IS the whole action and
                # this can just show the live label unconditionally, same
                # as the video button below always has.
                sub = music_storage_label()
            elif definition.action == "video_toggle_storage_source":
                sub = video_storage_label()
            else:
                sub = params.get("sub", "")
            self._push(
                key, f"label:{text}:{sub}",
                lambda: self._renderer.label(text, sub=sub),
            )

        elif kind == "volume_bar":
            volume = state.get("volume")
            muted = volume == 0
            song_index = state.get("song_index")
            position = (int(song_index) + 1) if song_index is not None else None
            total = state.get("playlist_length")
            if total in (None, "0"):
                total = None
            self._push(
                key, f"volbar:{volume}:{muted}:{position}:{total}",
                lambda: self._renderer.volume_bar(volume, muted, position, total),
            )

        elif kind == "now_playing_title":
            title, artist = state["title"], state["artist"]
            self._push(
                key, f"np:{title}:{artist}",
                lambda: self._renderer.now_playing(title, artist),
            )

        elif kind == "browse_root":
            browser = self._ensure_browser()
            at_root = browser.at_root if browser else True
            self._push(
                key, f"browse-root:{at_root}",
                lambda: self._renderer.browse_nav("Library", enabled=not at_root),
            )

        elif kind == "queue_position":
            song_index = state["song_index"]
            position = (int(song_index) + 1) if song_index is not None else 0
            total = state["playlist_length"]
            self._push(
                key, f"queue:{position}/{total}",
                lambda: self._renderer.label(f"{position}", sub=f"of {total}"),
            )

        elif kind == "progress":
            # Quantise to whole seconds so we do not re-render on sub-second
            # float jitter — this is what keeps the cache effective.
            bucket = int(state["elapsed"])
            self._push(
                key, f"progress:{bucket}:{int(state['duration'])}:{state['state']}",
                lambda: self._renderer.progress(
                    state["elapsed"], state["duration"], state["state"]
                ),
            )

        elif kind == "volume":
            volume = state["volume"]
            muted = volume == 0
            self._push(
                key, f"volume:{volume}:{muted}",
                lambda: self._renderer.volume(volume, muted),
            )

        elif kind == "mode_repeat3":
            # 3-state cycle (see actions.cycle_repeat_mode): Off/Folder/Song,
            # read live from MPD's own repeat+single flags rather than
            # tracked separately, so this can never drift from what MPD is
            # actually doing (same live-read philosophy as every other
            # status tile here).
            repeat = state.get("repeat", False)
            single = state.get("single", False)
            mode_text = "Song" if (repeat and single) else ("Folder" if repeat else "Off")
            self._push(
                key, f"repeat3:{repeat}:{single}",
                lambda: self._renderer.label("Repeat", sub=mode_text,
                                             highlight=bool(repeat)),
            )

        elif kind == "mode_shuffle3":
            # 3-state cycle (see actions.cycle_shuffle_mode): Off/Folder/All.
            # "All" isn't a real MPD status field -- it's ctx.shuffle_all_active,
            # mutated only by cycle_shuffle_mode itself (see that action's
            # docstring for why MPD has nothing to read this back from).
            random_on = state.get("random", False)
            all_active = self._ctx.shuffle_all_active
            mode_text = "All" if (random_on and all_active) else ("Folder" if random_on else "Off")
            self._push(
                key, f"shuffle3:{random_on}:{all_active}",
                lambda: self._renderer.label("Shuffle", sub=mode_text,
                                             highlight=bool(random_on)),
            )

        elif kind.startswith("mode_"):
            mode = kind.removeprefix("mode_")
            on = bool(state.get(mode, False))
            pretty = {"random": "Shuffle", "repeat": "Repeat",
                      "single": "Single", "consume": "Consume"}[mode]
            self._push(
                key, f"mode:{mode}:{on}",
                lambda: self._renderer.status(pretty, on),
            )

        elif kind == "bitrate":
            bitrate = state["bitrate"]
            audio = state["audio"]
            # MPD reports 'audio' as samplerate:bits:channels, e.g. 96000:24:2
            pretty = ""
            if audio:
                parts = audio.split(":")
                if len(parts) >= 2:
                    try:
                        pretty = f"{int(parts[0]) / 1000:g}k/{parts[1]}"
                    except ValueError:
                        pretty = audio
            self._push(
                key, f"bitrate:{bitrate}:{audio}",
                lambda: self._renderer.label(pretty or "—",
                                             sub=f"{bitrate}kbps" if bitrate else ""),
            )

        elif kind == "delete":
            armed = time.monotonic() < self._delete_armed_until
            if armed:
                remaining = int(self._delete_armed_until - time.monotonic()) + 1
                self._push(
                    key, f"delete-armed:{remaining}",
                    lambda: self._renderer.glyph(
                        "trash", caption=f"CONFIRM {remaining}s", colour="#FF453A"),
                )
            else:
                mode = self._config.delete.mode
                enabled = self._config.delete.enabled
                # Colour encodes how destructive it is, so the key never looks
                # harmless when it is not.
                colour = {
                    "queue": self._renderer.theme.muted,
                    "trash": self._renderer.theme.warn,
                    "permanent": "#FF453A",
                }.get(mode, self._renderer.theme.muted)
                caption = {"queue": "Dequeue", "trash": "Trash",
                           "permanent": "DELETE"}.get(mode, "Delete")
                if not enabled:
                    colour, caption = "#48484A", "off"
                self._push(
                    key, f"delete:{mode}:{enabled}",
                    lambda: self._renderer.glyph("trash", caption=caption, colour=colour),
                )

        elif kind == "video_mode_toggle":
            self._push(key, "video_mode_toggle",
                       lambda: self._renderer.label("Video", sub="karaoke"))

        elif kind == "page2_enter":
            # Was rendering as blank -- "page2_enter" had no branch here at
            # all, so it silently fell through to the kind-not-found blank
            # tile at the bottom of this method, an invisible, unlabeled key.
            # A downward chevron -- "going down" into page 2 -- paired with
            # page 2's "video_back" tile below using the upward chevron for
            # the return trip, both in the same accent colour.
            text = definition.label or "Next Page"
            self._push(key, f"page2enter:{text}",
                       lambda: self._renderer.glyph(
                           "chevron_down", caption=text, colour=self._renderer.theme.accent))

        elif kind == "audio_track":
            # track_label is mpv's active track title -- one of skp.py's
            # TRACK_TITLES ("Original (Vocal)" / "Karaoke (Backing Track)")
            # -- or a fallback "Audio Track N" for anything unexpected. The
            # button's own MAIN label now changes with the state instead of
            # staying a fixed "Track" title, so a glance tells you which
            # track is playing without reading the smaller sub-line: "Vocal"
            # on the original/vocal track, "Sing" on the karaoke/backing
            # track (i.e. "press to sing along"), falling back to the old
            # "Track"/full-label pairing for anything else.
            label = state.get("track_label", "")
            is_karaoke = "Karaoke" in label
            if is_karaoke:
                title, sub = "Sing", "Karaoke"
            elif "Vocal" in label:
                title, sub = "Vocal", "Original"
            else:
                title, sub = "Track", label or "—"
            # highlight=True paints the key's background solid accent-blue
            # (see KeyRenderer.label). The main "Sing" title stays its
            # normal white (readable fine on that blue) -- it's the smaller
            # "Karaoke" SUB-line, normally theme.muted grey, that loses
            # contrast on the blue fill, so only that gets flipped to black.
            sub_colour = "#000000" if is_karaoke else None
            self._push(
                key, f"track:{label}",
                lambda: self._renderer.label(title, sub=sub, highlight=is_karaoke,
                                             sub_colour=sub_colour),
            )

        elif kind == "video_route":
            route_id = params.get("route_id", "")
            route = self._config.route_by_id(route_id)
            if route is None:
                self._push(key, "blank", self._renderer.blank)
            else:
                active = state.get("route_id") == route.id
                available = route.id in state.get("available_routes", set())
                self._push(
                    key, f"vroute:{route.id}:{active}:{available}",
                    lambda: self._renderer.route(route.label, active, available),
                )

        elif kind == "video_back":
            text = definition.label or "Music"
            self._push(key, f"video_back:{text}",
                       lambda: self._renderer.label(text, sub="back"))

        elif kind == "video_entry":
            slot = params.get("slot", 0)
            window = self._video_window()
            entry = window[slot] if slot < len(window) else None
            if entry is None:
                self._push(key, f"video-entry-empty:{slot}",
                           lambda: self._renderer.browse_entry("", is_dir=False, empty=True))
            elif isinstance(entry[0], str):
                # Browse mode: ("dir"|"file", name, full_path)
                item_kind, name, _ = entry
                is_dir = item_kind == "dir"
                self._push(
                    key, f"video-browse:{item_kind}:{name}",
                    lambda ik=item_kind, n=name: self._renderer.browse_entry(
                        n, is_dir=(ik == "dir"), is_video=(ik == "file")
                    ),
                )
            else:
                # Normal up-next mode: (library_index, VideoEntry)
                idx, video_entry = entry
                playing = (self._video_library is not None
                          and idx == self._video_library.index)
                self._push(
                    key, f"video-entry:{video_entry.path}:{playing}",
                    lambda ve=video_entry, p=playing: self._renderer.browse_entry(
                        ve.song, is_dir=False, is_video=True, is_playing=p
                    ),
                )

        elif kind == "video_folder":
            slot = params.get("slot", 0)
            library = self._video_library
            if self._video_browse_stack and slot == 0:
                # Back button — shows the name of the folder we'd return to
                parent = self._video_browse_stack[-1]
                self._push(
                    key, f"video-folder-back:{parent}",
                    lambda p=parent: self._renderer.browse_entry(
                        f"← {p}", is_dir=True
                    ),
                )
            elif self._video_browse_stack:
                # Slot 1+ while browsing: blank (entry slots carry the content)
                self._push(key, f"video-folder-browse-empty:{slot}",
                           lambda: self._renderer.browse_entry("", is_dir=False, empty=True))
            else:
                # Root: show top-level folders as before
                folders = library.top_folders() if library is not None else []
                name = folders[slot] if slot < len(folders) else None
                if name is None:
                    self._push(key, f"video-folder-empty:{slot}",
                               lambda: self._renderer.browse_entry("", is_dir=False, empty=True))
                else:
                    self._push(
                        key, f"video-folder:{name}",
                        lambda n=name: self._renderer.browse_entry(n, is_dir=True),
                    )

        elif kind == "airplay_entry":
            slot = params.get("slot", 0)
            sink = self._airplay_sinks[slot] if slot < len(self._airplay_sinks) else None
            if sink is None:
                self._push(key, f"airplay-entry-empty:{slot}",
                           lambda: self._renderer.browse_entry("", is_dir=False, empty=True))
            else:
                name = sink.description or sink.name
                active = name == self._airplay_active_label or sink.is_default
                self._push(
                    key, f"airplay-entry:{sink.id}:{active}",
                    lambda: self._renderer.browse_entry(name, is_dir=False, is_playing=active),
                )

        elif kind == "wifi_status":
            info = _wifi.status()
            if info["connected"]:
                ssid = (info["ssid"] or "?")[:14]
                sig = f"{info['signal']}%" if info["signal"] is not None else "?"
                self._push(key, f"wifi-status:{ssid}:{sig}",
                           lambda: self._renderer.label(ssid, sub=sig))
            else:
                self._push(key, "wifi-status:down",
                           lambda: self._renderer.label(
                               "No Wifi", sub="down", colour=self._renderer.theme.warn))

        elif kind == "wifi_ip":
            info = _wifi.status()
            ip_text = info["ip"] or "--"
            powersave = info["powersave"]
            # "on" is the misconfigured state that caused AirPlay speakers to
            # vanish from the picker -- flag it in warn colour so it's
            # obvious at a glance if wifi-powersave-off.conf ever gets
            # silently dropped (NM update, factory reset, manual nmtui edit).
            ps_label = {"off": "PS off", "on": "PS ON!"}.get(powersave, "PS ?")
            ps_colour = self._renderer.theme.warn if powersave == "on" else None
            self._push(key, f"wifi-ip:{ip_text}:{powersave}",
                       lambda: self._renderer.label(ip_text, sub=ps_label, colour=ps_colour))

        elif kind == "wifi_reconnect":
            state = self._wifi_reconnect_state
            if state == "working":
                self._push(key, "wifi-reconnect:working",
                           lambda: self._renderer.label("Reconnect", sub="..."))
            elif isinstance(state, tuple) and state[0] == "done":
                ok = state[1]
                self._push(
                    key, f"wifi-reconnect:done:{ok}",
                    lambda: self._renderer.label(
                        "Reconnect", sub="OK" if ok else "Failed",
                        colour=None if ok else self._renderer.theme.warn),
                )
            else:
                self._push(key, "wifi-reconnect:idle",
                           lambda: self._renderer.label("Reconnect", sub="Wifi"))

        elif kind == "wifi_fix":
            state = self._wifi_fix_state
            if state == "working":
                self._push(key, "wifi-fix:working",
                           lambda: self._renderer.label("Fix Wifi", sub="..."))
            elif isinstance(state, tuple) and state[0] == "done":
                ok = state[1]
                self._push(
                    key, f"wifi-fix:done:{ok}",
                    lambda: self._renderer.label(
                        "Fix Wifi", sub="OK" if ok else "Failed",
                        colour=None if ok else self._renderer.theme.warn),
                )
            else:
                self._push(key, "wifi-fix:idle",
                           lambda: self._renderer.label("Fix Wifi", sub="Restart NM"))

        elif kind == "bt_entry":
            slot = params.get("slot", 0)
            candidate = self._bt_candidates[slot] if slot < len(self._bt_candidates) else None
            if candidate is None:
                scanning_hint = self._bt_scanning and slot == 0
                self._push(
                    key, f"bt-entry-empty:{slot}:{scanning_hint}",
                    lambda: self._renderer.browse_entry(
                        "Scanning..." if scanning_hint else "",
                        is_dir=False, empty=not scanning_hint,
                    ),
                )
            else:
                mac, name = candidate
                self._push(key, f"bt-entry:{mac}",
                           lambda: self._renderer.browse_entry(name, is_dir=False))

        elif kind == "bt_reset":
            state = self._bt_reset_state  # "idle" | "working" | ("done", n)
            if state == "working":
                self._push(key, "bt-reset:working",
                           lambda: self._renderer.label("Reset", sub="..."))
            elif isinstance(state, tuple) and state[0] == "done":
                count = state[1]
                sub = f"{count} cleared" if count else "none stuck"
                self._push(key, f"bt-reset:done:{count}",
                           lambda: self._renderer.label("Reset", sub=sub))
            else:
                self._push(key, "bt-reset:idle",
                           lambda: self._renderer.label("Reset", sub="Pairs"))

        elif kind == "power":
            armed = time.monotonic() < self._shutdown_armed_until
            if armed:
                remaining = int(self._shutdown_armed_until - time.monotonic()) + 1
                self._push(
                    key, f"power-armed:{remaining}",
                    lambda: self._renderer.glyph(
                        "power", caption=f"CONFIRM {remaining}s", colour="#FF453A",
                        highlight=False),
                )
            else:
                self._push(
                    key, "power-idle",
                    lambda: self._renderer.glyph("power", caption="Shutdown",
                                                 colour=self._renderer.theme.warn),
                )

        elif kind == "wifi_toggle":
            enabled = self._ctx.wifi_enabled
            symbol = "wifi" if enabled else "wifi_off"
            colour = self._renderer.theme.warn if not enabled else None
            caption = "Wifi On" if enabled else "Wifi Off"
            self._push(
                key, f"wifi:{enabled}",
                lambda: self._renderer.glyph(symbol, caption=caption, colour=colour),
            )

        elif kind == "screen_toggle":
            # Reflects _screen_forced_off, not _current_brightness -- while
            # forced off the panel is dark and this never actually gets
            # drawn/seen (see the wake-press branch in _on_key), but the
            # content key still needs to be stable so a redraw right after
            # waking up shows the correct ("off") state rather than a stale
            # cached "on" one.
            off = self._screen_forced_off
            self._push(
                key, f"screen:{off}",
                lambda: self._renderer.label(
                    "Screen", sub="OFF" if off else "ON",
                    colour=self._renderer.theme.warn if off else None,
                ),
            )

        elif kind == "power_draw":
            ma = read_pi_current_ma()
            text = f"{ma:.0f}mA" if ma is not None else "--"
            self._push(
                key, f"power-draw:{text}",
                lambda: self._renderer.label(text, sub="Pi Draw"),
            )

        elif kind == "browse_entry":
            browser = self._ensure_browser()
            slot = params.get("slot", 0)
            entry = browser.visible()[slot] if browser else None
            if entry is None:
                self._push(key, f"browse-empty:{slot}",
                           lambda: self._renderer.browse_entry("", is_dir=False,
                                                               empty=True))
            else:
                playing = entry.uri == state["current_file"]
                folder = browser.path if browser else ""
                shd = self._listen_state.card_shade(
                    entry.uri, playing, folder, entry.is_dir)
                self._push(
                    key,
                    f"browse:{entry.uri}:{shd}",
                    lambda: self._renderer.browse_entry(
                        entry.name, is_dir=entry.is_dir, is_playing=playing,
                        shade=shd,
                    ),
                )

        elif kind == "browse_back":
            # "Up" + a folder-up icon, not "Back" + the old arrow-plus-bar
            # shape — that shape read too close to the seek buttons' rew/
            # ffwd triangles to tell apart at a glance.
            browser = self._ensure_browser()
            crumb = browser.breadcrumb() if browser else "Library"
            at_root = browser.at_root if browser else True
            self._push(
                key, f"browse-back:{crumb}:{at_root}",
                lambda: self._renderer.browse_nav(
                    "Up", sub=crumb[:12], enabled=not at_root, icon="folder_up"
                ),
            )

        elif kind == "browse_page":
            # "More" + a circular-arc "cycle" icon, not "Page" + the old
            # double-down-chevron shape — same reasoning as browse_back
            # above.
            browser = self._ensure_browser()
            if browser is None:
                self._push(key, "browse-page:none",
                           lambda: self._renderer.browse_nav("More", enabled=False, icon="cycle"))
            else:
                sub = f"{browser.page_index + 1}/{browser.page_count}"
                multi = browser.page_count > 1
                self._push(
                    key, f"browse-page:{sub}",
                    lambda: self._renderer.browse_nav("More", sub=sub, enabled=multi, icon="cycle"),
                )

        elif kind == "current_folder":
            browser = self._ensure_browser()
            folder = (os.path.basename(browser.path) if browser and browser.path
                      else "Library")
            self._push(
                key, f"folder:{folder}",
                lambda: self._renderer.current_folder(folder),
            )

        else:
            self._push(key, "blank", self._renderer.blank)

    def _handle_shutdown_key(self, key: int) -> None:
        """Two-step confirm, shared by both pages — Power lives at the same
        cell on the music and video layouts and must behave identically on
        either, including while a video is playing (previously this only
        existed in the music-page branch of _on_key, so there was no way to
        shut down at all from the video page).
        """
        now = time.monotonic()
        if now < self._shutdown_armed_until:
            LOG.warning("shutdown confirmed by second press")
            self._shutdown_armed_until = 0.0
            self._rendered.pop(key, None)
            # A failed poweroff (e.g. polkit denying it) must not look
            # identical to a successful one — surface whatever dispatch
            # returns instead of discarding it.
            toast = dispatch("shutdown", self._ctx, 1)
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            return
        self._shutdown_armed_until = now + SHUTDOWN_CONFIRM_SECONDS
        self._toast_text = "Press again"
        self._toast_until = now + SHUTDOWN_CONFIRM_SECONDS
        self._rendered.pop(key, None)
        LOG.info("shutdown ARMED — press again within %.0fs to confirm",
                 SHUTDOWN_CONFIRM_SECONDS)
        self._redraw_event.set()

    def _handle_delete_key(self, key: int, action: str) -> None:
        """Two-step confirm for a destructive delete — same shape as
        _handle_shutdown_key, and for the same reason: Delete lives at the
        IDENTICAL cell (2,7) on both the music and video pages, so one
        shared arm/confirm state (self._delete_armed_until) behaves the
        same regardless of which page's Delete key triggered it.

        Gated by config.toml's [delete] confirm: "two_step" is the dance
        below; "none" (or anything else) dispatches immediately, exactly
        the old single-press behaviour, for anyone who reverts config.
        "long_press" is documented in config.py but not implemented here —
        no change from before this method existed.

        ``action`` is whichever action name should actually fire on
        confirm — "delete_current" from the music page, "video_delete_
        current" from the video page — so this one method serves both
        without needing to know which page called it.
        """
        if self._config.delete.confirm != "two_step":
            toast = dispatch(action, self._ctx, 1)
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            return

        now = time.monotonic()
        if now < self._delete_armed_until:
            LOG.warning("delete confirmed by second press (%s)", action)
            self._delete_armed_until = 0.0
            self._rendered.pop(key, None)
            # A failed delete must not look identical to a successful one —
            # surface whatever dispatch returns instead of discarding it.
            toast = dispatch(action, self._ctx, 1)
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            return

        confirm_seconds = self._config.delete.confirm_seconds
        self._delete_armed_until = now + confirm_seconds
        self._toast_text = "Press again"
        self._toast_until = now + confirm_seconds
        self._rendered.pop(key, None)
        LOG.info("delete ARMED (%s) — press again within %.0fs to confirm",
                 action, confirm_seconds)
        self._redraw_event.set()

    def _on_video_entry_press(self, slot: int) -> None:
        window = self._video_window()
        entry = window[slot] if slot < len(window) else None
        if entry is None:
            return

        # ---- Browse mode: ("dir"|"file", name, full_path) ----
        if isinstance(entry[0], str):
            item_kind, name, full = entry
            if item_kind == "dir":
                self._video_browse_stack.append(name)
                self._video_window_offset = 0
                for k, d in layout.LAYOUT_VIDEO.items():
                    if d.kind in ("video_entry", "video_folder"):
                        self._rendered.pop(k, None)
                self._redraw_event.set()
            else:
                # file — play it and exit browse mode
                self._video_browse_stack.clear()
                self._video_window_offset = 0
                self._play_video_path(full, name)
                for k, d in layout.LAYOUT_VIDEO.items():
                    if d.kind in ("video_entry", "video_folder",
                                  "now_playing_title", "queue_position"):
                        self._rendered.pop(k, None)
                self._redraw_event.set()
            return

        # ---- Normal up-next mode: (library_index, VideoEntry) ----
        if self._video is None or self._video_library is None:
            return
        idx, video_entry = entry
        _capture_video_position(self._ctx)
        self._video_library.select(idx)
        self._router.reassert_current()
        try:
            resume = self._video_library.take_resume_position(video_entry.path)
            load_and_play(self._video, video_entry, resume=resume)
            _clamp_video_volume_if_airplay(self._ctx)
            self._toast_text = video_entry.song[:16]
        except (SkpParseError, VideoError):
            LOG.exception("video jump failed for %s", video_entry.path)
            self._toast_text = "Load failed"
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._video_window_offset = 0
        self._video_last_index = self._video_library.index
        for k, d in layout.LAYOUT_VIDEO.items():
            if d.kind in ("video_entry", "now_playing_title", "queue_position"):
                self._rendered.pop(k, None)
        self._redraw_event.set()

    def _on_video_folder_press(self, slot: int) -> None:
        """Navigate the page-1 mini folder browser.

        Root (stack empty): push the tapped top-level folder onto the stack
            and switch entry slots to browse mode.
        Inside a folder (stack non-empty): slot 0 = "← Back" (pop stack);
            slot 1+ is blank, do nothing.
        """
        if self._video_browse_stack:
            if slot == 0:
                self._video_browse_stack.pop()
                self._video_window_offset = 0
                for k, d in layout.LAYOUT_VIDEO.items():
                    if d.kind in ("video_entry", "video_folder"):
                        self._rendered.pop(k, None)
                self._redraw_event.set()
            return

        library = self._video_library
        if library is None:
            return
        folders = library.top_folders()
        name = folders[slot] if slot < len(folders) else None
        if name is None:
            return
        self._video_browse_stack.append(name)
        self._video_window_offset = 0
        for k, d in layout.LAYOUT_VIDEO.items():
            if d.kind in ("video_entry", "video_folder"):
                self._rendered.pop(k, None)
        self._redraw_event.set()

    def _play_video_path(self, path: str, toast: str) -> None:
        """Play an arbitrary video file by path — what the page-2 grid
        browser's file presses resolve to (VideoGridBrowser.enter() cannot
        do this itself; it has no VideoCommander/router/resume-position
        access, same "dumb model" reasoning as LibraryBrowser). Mirrors
        _on_video_entry_press's load/resume/clamp/repaint sequence exactly,
        the only difference being how the target entry is found (by path,
        via VideoLibrary.select_by_path, rather than by a pre-known index
        into the up-next window).
        """
        if self._video is None or self._video_library is None:
            return
        _capture_video_position(self._ctx)
        entry = self._video_library.select_by_path(path)
        if entry is None:
            self._toast_text = "Not found"
            self._toast_until = time.monotonic() + TOAST_SECONDS
            return
        self._router.reassert_current()
        try:
            resume = self._video_library.take_resume_position(entry.path)
            load_and_play(self._video, entry, resume=resume)
            _clamp_video_volume_if_airplay(self._ctx)
            self._toast_text = toast[:16]
        except (SkpParseError, VideoError):
            LOG.exception("video grid play failed for %s", entry.path)
            self._toast_text = "Load failed"
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._video_window_offset = 0
        self._video_last_index = self._video_library.index
        for k, d in layout.LAYOUT_VIDEO.items():
            if d.kind in ("video_entry", "now_playing_title", "queue_position"):
                self._rendered.pop(k, None)

    def _on_grid_key_press(self, key: int, definition: layout.KeyDef, browser,
                           is_video: bool) -> None:
        """Shared press handling for BOTH page-2 grid browsers — the
        counterpart to _render_grid_key. Only the "a file was picked"
        outcome differs between music (the browser plays it directly,
        mirroring LibraryBrowser) and video (the browser hands back a
        ``("play", path, toast)`` tuple for _play_video_path to act on,
        since playing a video needs machinery the browser itself
        deliberately doesn't own — see VideoGridBrowser.enter()'s
        docstring).
        """
        kind = definition.kind

        if kind == "grid_page":
            direction = definition.params.get("direction", 1)
            browser.page_down() if direction > 0 else browser.page_up()
            self._invalidate_grid_keys(is_video)
            self._redraw_event.set()
            return

        if kind == "grid_updir":
            toast = browser.back()
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._invalidate_grid_keys(is_video)
            self._redraw_event.set()
            return

        if kind == "grid_entry":
            slot = definition.params.get("slot", 0)
            try:
                result = browser.enter(slot)
            except Exception:  # noqa: BLE001
                LOG.exception("grid browser enter(%d) failed on key %d", slot, key)
                return
            if result is None:
                return
            if is_video and isinstance(result, tuple) and result[0] == "play":
                _, path, toast = result
                self._play_video_path(path, toast)
            else:
                self._toast_text = str(result)[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._invalidate_grid_keys(is_video)
            self._redraw_event.set()
            return

        if kind == "storage_select":
            # dispatch() runs the actual symlink swap + database refresh
            # (actions.py's set_storage_internal/set_storage_usb or their
            # video_ equivalents); jumping the browser to root afterward is
            # UI state this action function has no access to, so it's done
            # here rather than in actions.py.
            toast = dispatch(definition.action, self._ctx, 1)
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            browser.goto("")
            self._invalidate_grid_keys(is_video)
            self._redraw_event.set()
            return

    def _on_airplay_entry_press(self, slot: int) -> None:
        sink = self._airplay_sinks[slot] if slot < len(self._airplay_sinks) else None
        if sink is None:
            return
        if self._router.switch_to_sink(sink):
            self._airplay_active_label = sink.description or sink.name
            if self._picker_return_page == _mode.VIDEO and self._video is not None:
                self._video.set_audio_device("auto")
                self._video.reload_audio()
                self._ctx.video_local_was_enabled = False
                # Router.switch_to_sink() already clamped MPD's mixer; mpv's
                # softvol is a separate mixer needing the same ceiling — see
                # AIRPLAY_SAFE_VOLUME (outputs.py). Every picker entry is an
                # AirPlay sink, so this always applies here.
                if self._video.volume() > AIRPLAY_SAFE_VOLUME:
                    self._video.set_volume(AIRPLAY_SAFE_VOLUME)
                # Sync the drift-detection baseline (see
                # ActionContext.video_last_set_volume) — otherwise the next
                # song skip's clamp has no record of this explicit switch-
                # time value to compare against.
                self._ctx.video_last_set_volume = self._video.volume()
            self._toast_text = (sink.description or sink.name)[:16]
        else:
            self._toast_text = "Switch failed"
        self._toast_until = time.monotonic() + TOAST_SECONDS
        self._page = self._picker_return_page
        self._rendered.clear()
        self._redraw_event.set()

    def _push(self, key: int, content_key: str, render) -> None:
        """Render and upload a key, skipping if its content is unchanged.

        This cache is the difference between a panel that idles at ~0% CPU and
        one that burns a core redrawing identical images.
        """
        if self._rendered.get(key) == content_key:
            return

        try:
            image = render()
        except Exception:  # noqa: BLE001
            LOG.exception("render failed for key %d (%s)", key, content_key)
            return

        with self._deck_lock:
            if self._deck is None:
                return
            try:
                native = PILHelper.to_native_key_format(self._deck, image)
                self._deck.set_key_image(key, native)
                self._rendered[key] = content_key
            except Exception as exc:  # noqa: BLE001
                LOG.debug("set_key_image(%d) failed: %s", key, exc)
                raise

    # -- input -------------------------------------------------------------

    def _start_repeat(self, key: int, action: str) -> None:
        """Begin auto-repeating ``action`` while ``key`` stays held.

        The immediate single step has already been dispatched by the caller
        before this is called — this only handles what happens if the key is
        STILL down after REPEAT_START_DELAY. Cancels any previous repeat
        first: only one key can physically be held at a time, so a fresh
        press always wins over whatever an earlier one was doing (guards
        against a stuck/missed key-up leaving a phantom repeat running).
        """
        self._stop_repeat()
        self._repeat_key = key
        self._repeat_stop.clear()
        stop = self._repeat_stop  # capture: a later _stop_repeat() swaps this

        def _run() -> None:
            if stop.wait(REPEAT_START_DELAY):
                return
            while not stop.is_set():
                toast = dispatch(action, self._ctx, 1)
                if toast:
                    self._toast_text = toast[:16]
                    self._toast_until = time.monotonic() + TOAST_SECONDS
                self._redraw_event.set()
                if stop.wait(REPEAT_INTERVAL):
                    return

        self._repeat_thread = threading.Thread(
            target=_run, daemon=True, name="volume-repeat")
        self._repeat_thread.start()

    def _stop_repeat(self) -> None:
        self._repeat_stop.set()
        self._repeat_key = None

    def _maybe_stop_repeat(self, key: int) -> None:
        if self._repeat_key == key:
            self._stop_repeat()

    def _on_key(self, deck, key: int, pressed: bool) -> None:
        """Key callback. Runs on the library's own reader thread."""
        if not pressed:
            self._maybe_stop_repeat(key)
            return

        self._last_interaction = time.monotonic()
        self._wake_from_low_power()

        # If the panel was dimmed or blank, the first press only wakes it.
        # Pressing a control you cannot see is a bad experience. Also clears
        # an explicit screen-off (see _screen_forced_off / the Video page's
        # screen_toggle tile) the exact same way — any key, not just that
        # tile, brings the panel back, since you cannot see which key is
        # which with the backlight off.
        if self._current_brightness < self._config.streamdeck.brightness:
            LOG.debug("wake press on key %d (ignored as input)", key)
            self._screen_forced_off = False
            self._redraw_event.set()
            return

        if self._page == PAGE_AIRPLAY_PICKER:
            definition = layout.airplay_picker_key_def(key)
            if definition is None:
                return
            if definition.kind == "airplay_entry":
                self._on_airplay_entry_press(definition.params.get("slot", 0))
                return
            if definition.action == "wifi_reconnect":
                self._on_wifi_reconnect_press()
                return
            if definition.action == "wifi_restart_networking":
                self._on_wifi_fix_press()
                return
            if definition.action == "close_airplay_picker":
                self._page = self._picker_return_page
                self._rendered.clear()
                self._redraw_event.set()
                return
            return

        if self._page == PAGE_BT_PICKER:
            definition = layout.bt_picker_key_def(key)
            if definition is None:
                return
            if definition.kind == "bt_entry":
                self._on_bt_entry_press(definition.params.get("slot", 0))
                return
            if definition.action == "reset_bt_pairings":
                self._on_bt_reset_press()
                return
            if definition.action == "close_bt_picker":
                self._page = self._picker_return_page
                self._rendered.clear()
                self._redraw_event.set()
                return
            return

        if self._page == PAGE_MUSIC_BROWSE:
            definition = layout.browse_grid_key_def(key)
            if definition is None:
                return
            if definition.action == "close_page2":
                self._page = _mode.MUSIC
                self._rendered.clear()
                self._redraw_event.set()
                return
            if definition.kind in ("grid_updir", "grid_entry", "grid_page"):
                browser = self._ensure_music_grid_browser()
                if browser is not None:
                    self._on_grid_key_press(key, definition, browser, False)
                return
            # Delete (added to the browse grid at index(2,6)) needs the same
            # two-step confirm as page 1's Delete key, not a blind dispatch —
            # see _handle_delete_key.
            if definition.action == "delete_current":
                self._handle_delete_key(key, "delete_current")
                return
            if not definition.action:
                return
            toast = dispatch(definition.action, self._ctx, 1)
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            return

        if self._page == PAGE_VIDEO_BROWSE:
            definition = layout.video_browse_grid_key_def(key)
            if definition is None:
                return
            if definition.action == "close_video_page2":
                self._page = _mode.VIDEO
                self._rendered.clear()
                self._redraw_event.set()
                return
            if definition.kind in ("grid_updir", "grid_entry", "grid_page"):
                browser = self._ensure_video_grid_browser()
                if browser is not None:
                    self._on_grid_key_press(key, definition, browser, True)
                return
            if definition.action == "video_delete_current":
                self._handle_delete_key(key, "video_delete_current")
                return
            if not definition.action:
                return
            toast = dispatch(definition.action, self._ctx, 1)
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            return

        if self._page == _mode.VIDEO:
            definition = layout.video_key_def(key)
            if definition is None:
                return

            # UI-only actions handled here rather than through the shared
            # action table — same reason the library browser below is
            # special-cased: which library entry a slot means, or where the
            # picker returns to, are Stream Deck display concerns actions.py
            # has no business knowing about.
            if definition.kind == "video_entry":
                self._on_video_entry_press(definition.params.get("slot", 0))
                return

            if definition.kind == "video_folder":
                self._on_video_folder_press(definition.params.get("slot", 0))
                return

            if definition.action in ("video_browse_prev", "video_browse_next"):
                step = 4 if definition.action == "video_browse_next" else -4
                self._video_window_offset += step
                for k, d in layout.LAYOUT_VIDEO.items():
                    if d.kind == "video_entry":
                        self._rendered.pop(k, None)
                self._redraw_event.set()
                return

            # --- storage source toggle: reset the "up next" window + page-2
            # video browser to root ------------------------------------------
            if definition.action == "video_toggle_storage_source":
                # Unlike music, video_toggle_storage_source's rescan is
                # SYNCHRONOUS (see its docstring in actions.py — cheap even
                # for a real library), so ctx.video_library.entries is
                # already correct by the time dispatch() returns here. What's
                # NOT automatically correct is this page's own paging state:
                # self._video_window_offset could be pointing well past the
                # end of a now-different-sized library, and the page-2 video
                # grid browser (if ever opened) could still be sitting deep
                # in a folder that only existed under the OLD source. Reset
                # both, same "land somewhere valid" reasoning as the music
                # toggle's browser.go_root() above.
                toast = dispatch(definition.action, self._ctx, 1)
                self._video_window_offset = 0
                self._video_browse_stack.clear()
                for k, d in layout.LAYOUT_VIDEO.items():
                    if d.kind in ("video_entry", "video_folder"):
                        self._rendered.pop(k, None)
                if self._video_grid_browser is not None:
                    self._video_grid_browser.goto("")
                self._invalidate_grid_keys(True)
                if toast:
                    self._toast_text = toast[:16]
                    self._toast_until = time.monotonic() + TOAST_SECONDS
                self._redraw_event.set()
                return

            if definition.action == "enter_video_page2":
                # Root of whatever library is currently active (internal or
                # USB, whichever /home/rpi/Video resolves to right now) --
                # NOT "now playing"'s folder. Video page 1 has no persistent
                # folder-browse state to mirror the way music's LibraryBrowser
                # does, and "now playing" could easily be a stale reference
                # to the OTHER storage source right after a toggle.
                browser = self._ensure_video_grid_browser()
                if browser is not None:
                    browser.goto("")
                self._page = PAGE_VIDEO_BROWSE
                self._rendered.clear()
                self._redraw_event.set()
                return

            if definition.action == "video_route_airplay":
                self._enter_airplay_picker(_mode.VIDEO)
                return

            if definition.action == "video_route_bt":
                self._enter_bt_picker(_mode.VIDEO)
                return

            if definition.action == "shutdown":
                self._handle_shutdown_key(key)
                return

            if definition.action == "video_delete_current":
                self._handle_delete_key(key, "video_delete_current")
                return

            if definition.action == "toggle_screen":
                # Reaching here means the panel is currently at full
                # brightness -- the wake-on-any-key branch above already
                # intercepts every press (including this key) while it's
                # dimmed/off, so this only ever fires the "turn it off" half.
                # No toast: the backlight is about to go dark, so a toast
                # message here would never actually be seen.
                self._screen_forced_off = True
                self._apply_dimming(playing=self._last_state == "play")
                return

            if not definition.action:
                return

            toast = dispatch(definition.action, self._ctx, 1)
            # Mirror the mode file locally: enter/exit_video_mode already
            # wrote it (actions.py), this just keeps THIS process's own page
            # state in step and forces a full repaint of the panel it's
            # switching to/from.
            if definition.action == "exit_video_mode":
                self._page = _mode.MUSIC
                self._rendered.clear()
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            if definition.action in REPEAT_ACTIONS:
                self._start_repeat(key, definition.action)
            return

        if key in self._route_keys:
            route = self._config.route_by_id(self._route_keys[key])
            if route is not None:
                if route.icon == "airplay":
                    self._enter_airplay_picker(_mode.MUSIC)
                elif route.icon == "bluetooth":
                    self._enter_bt_picker(_mode.MUSIC)
                elif self._router.is_available(route):
                    self._router.switch_to(route)
                    self._toast_text = route.label
                    self._toast_until = time.monotonic() + TOAST_SECONDS
                else:
                    self._toast_text = "Unavailable"
                    self._toast_until = time.monotonic() + TOAST_SECONDS
                self._redraw_event.set()
            return

        definition = layout.key_def(key)
        if definition is None:
            return

        if definition.action == "enter_video_mode":
            toast = dispatch(definition.action, self._ctx, 1)
            self._page = _mode.VIDEO
            self._rendered.clear()
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            return

        if definition.action == "jump_to_playing":
            # Navigate the browser to the folder of the currently playing song
            # and scroll so the song is visible. Does not start/stop playback.
            browser = self._ensure_browser()
            current_file = self._last_current_file or None
            if browser is not None and current_file:
                browser.jump_to_playing(current_file)
                self._invalidate_browser_keys()
                self._redraw_event.set()
            return

        if definition.action == "enter_page2":
            # Mirror wherever page 1's own library browser is CURRENTLY
            # positioned, not "now playing"'s folder -- pressing Next Page
            # should show the same storage source and folder depth you were
            # just looking at on page 1, matching what you'd expect right
            # after toggling Internal/USB (nothing new may be playing yet).
            browser = self._ensure_music_grid_browser()
            if browser is not None:
                page1_browser = self._ensure_browser()
                browser.goto(page1_browser.path if page1_browser is not None else "")
            self._page = PAGE_MUSIC_BROWSE
            self._rendered.clear()
            self._redraw_event.set()
            return

        # --- library browser -------------------------------------------------
        if definition.kind in ("browse_entry", "browse_back", "browse_page", "browse_root"):
            browser = self._ensure_browser()
            if browser is None:
                return
            try:
                if definition.kind == "browse_entry":
                    toast = browser.enter(definition.params.get("slot", 0))
                elif definition.kind == "browse_back":
                    toast = browser.back()
                elif definition.kind == "browse_root":
                    toast = browser.go_root()
                else:
                    browser.page_down()
                    toast = f"Page {browser.page_index + 1}/{browser.page_count}"
            except Exception:  # noqa: BLE001
                LOG.exception("browser action failed on key %d", key)
                return
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._invalidate_browser_keys()
            self._redraw_event.set()
            return

        # --- two-step shutdown ----------------------------------------------
        if definition.action == "shutdown":
            self._handle_shutdown_key(key)
            return

        # --- two-step delete -------------------------------------------------
        if definition.action == "delete_current":
            self._handle_delete_key(key, "delete_current")
            return

        # --- storage source toggle: reset every browser to root -------------
        if definition.action == "toggle_storage_source":
            toast = dispatch(definition.action, self._ctx, 1)
            # The symlink has already flipped by the time dispatch() returns,
            # but MPD's database itself is only EVENTUALLY consistent (its
            # background update finishes later — see the action's own
            # docstring). Jumping every browser back to root right now means
            # the panel never lingers on a subfolder from the OTHER source
            # (reported live: "the folder __Alex Do from USB stays on row 1
            # after switching to Internal") — root is the one listing that
            # was almost certainly valid under the previous source too, so
            # it's the least-wrong place to land immediately. The eventual
            # database-idle-event refresh (see _watch_mpd) then corrects the
            # actual file listing once MPD catches up.
            browser = self._ensure_browser()
            if browser is not None and not browser.at_root:
                browser.go_root()
            if self._music_grid_browser is not None:
                self._music_grid_browser.goto("")
            self._invalidate_browser_keys()
            self._invalidate_grid_keys(False)
            if toast:
                self._toast_text = toast[:16]
                self._toast_until = time.monotonic() + TOAST_SECONDS
            self._redraw_event.set()
            return

        if not definition.action:
            return

        LOG.debug("key %d -> %s", key, definition.action)
        toast = dispatch(definition.action, self._ctx, 1)
        if toast:
            self._toast_text = toast[:16]
            self._toast_until = time.monotonic() + TOAST_SECONDS
        self._redraw_event.set()
        if definition.action in REPEAT_ACTIONS:
            self._start_repeat(key, definition.action)


class _LocalPublisher:
    """Gives actions the same ``ctx.bus.publish("toast", ...)`` interface the
    TourBox's real BusServer offers, but wired straight into this daemon's own
    toast state instead of going out over IPC.

    Needed because of the async Bluetooth-reconnect path (player/bt.py):
    a route key press returns "Connecting..." immediately, and several
    seconds later a background thread has the real answer ("BT" or
    "BT failed") with no render pass in between to carry it — publish() is
    how that later result still reaches the panel.
    """

    def __init__(self, daemon: "StreamDeckDaemon") -> None:
        self._daemon = daemon

    def publish(self, event: str, **payload: object) -> None:
        if event == "toast":
            self._daemon._toast_text = str(payload.get("text", ""))[:16]
            self._daemon._toast_until = time.monotonic() + TOAST_SECONDS
            self._daemon._redraw_event.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stream Deck XL -> MPD daemon")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging("DEBUG" if args.verbose else config.log_level)

    return StreamDeckDaemon(config).run()


if __name__ == "__main__":
    sys.exit(main())
