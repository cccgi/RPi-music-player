#!/usr/bin/env python3
"""Touch UI daemon for the touchscreen profile (Pi 5 + 7" DSI panel).

Not used at all on the Stream Deck (rpi-audio) profile — see
docs/DEPLOY.md's profile split and install.sh's --profile flag.

Owns three things:

  1. Reading raw touch events from the panel's evdev multitouch device and
     turning them into logical (x, y) taps/drags — see touchui/layout.py's
     module docstring for the 180-degree rotation contract this and
     touchui/render.py both implement.
  2. Rendering the control overlay (touchui/render.py, Pillow) from current
     MPD/video state and pushing it onto mpv's video output via IPC
     ``overlay-add`` — mpv already owns everything that appears on screen
     (see ARCHITECTURE.md / README's design notes), so the overlay
     composites directly onto whatever mpv is currently showing: black in
     music mode (mpv sits idle), a real video frame in video mode.
  3. Dispatching taps to the SAME action table TourBox and the Stream Deck
     use (player/actions.py's ``dispatch()``), so all three control
     surfaces stay behaviorally identical by construction, not convention.

First-pass status: built and deployed without access to the actual
hardware to test evdev axis ranges, BTN_TOUCH vs. ABS_MT_TRACKING_ID
event shape, or mpv's overlay-add pixel format in practice. Run with
--verbose and watch the log for the touch-device probe and the first few
raw events if taps don't register or land in the wrong place — see
find_touch_device()'s and _handle_touch_event()'s docstrings for what
they log and why.

Run standalone for debugging:
    sudo -u rpi /opt/rpi-player/venv/bin/python -m player.touch_daemon --verbose
"""

from __future__ import annotations

import argparse
import io
import logging
import math
import os
import select
import signal
import sys
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .actions import (
    ActionContext,
    DeleteSettings,
    dispatch,
    music_storage_label,
    video_storage_label,
)
from .audio_visualizer import AudioVisualizer
from . import bt as _bt
from .config import Config, load_config, setup_logging
from . import mode as _mode
from .ipc import BusServer, NullBus
from .mpdbus import MpdCommander
from .outputs import OutputRouter
from . import smb as _smb
from .touchui import layout
from .touchui.render import OverlayState, Renderer, Theme
from .video import VideoCommander, VideoLibrary
from . import wifi as _wifi

LOG = logging.getLogger("touch")

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover - reported at runtime, see main()
    evdev = None
    ecodes = None

# How long to hold BTN_TOUCH/tracking-id "up" before treating a touch as
# released — some controllers report a spurious single up/down blip mid-drag.
# Kept tiny; this is a debounce, not a deliberate delay.
_RELEASE_DEBOUNCE = 0.0

# Re-render (and, if drawn, re-push to mpv) at most this often while a drag
# is in progress on the scrub/volume bar — keeps a live-feeling drag without
# hammering the mpv IPC socket + a ~1.5MB file write on every single evdev
# sample, which can arrive far faster than the eye needs a redraw.
_DRAG_RENDER_INTERVAL = 0.05

# Auto-hide: video mode only, per explicit request ("fade and go away after
# a video starts playing for 2 sec"). Rearmed on every playback-start
# transition (initial play OR resume from pause), cleared while paused or
# out of video mode. Implemented as an instant hide rather than an animated
# alpha fade — the render loop's poll cadence ([touch].tick_seconds, ~1s by
# default) is too coarse to animate smoothly without pushing a new overlay
# frame over the mpv IPC socket every video frame, and "goes away" is the
# behavior that actually matters here, not the transition style.
_AUTO_HIDE_DELAY = 2.0

# Content-area gestures: a touch that starts in the open content area (not
# on a specific control) is a plain tap (toggles overlay visibility) until
# it moves more than this many logical pixels, at which point it locks into
# either a "seek" (dominant horizontal movement) or "volume" (dominant
# vertical movement) gesture for the rest of that touch — see
# _handle_content_gesture(). The deadzone exists purely so an ordinary tap
# with a little natural finger wobble doesn't misfire as a 1%-volume-change
# swipe.
_GESTURE_DEADZONE = 14  # logical pixels

# How long the transient volume HUD (render.py's _draw_volume_hud) stays
# visible after the most recent volume-swipe sample — re-armed on every
# sample during an active swipe (see _handle_content_gesture), so it only
# starts counting down once the finger actually stops/lifts. Per the spec:
# "automatically disappear after approximately 1-2 seconds."
_VOLUME_HUD_HOLD = 1.5

# How long the video-mode scan "activity" state stays visually on after a
# rescan tap — video.py's VideoLibrary.rescan() is synchronous with no
# progress/completion signal to poll, unlike MPD's own updating_db field
# (used directly for music-mode scanning below), so this is a fixed pulse
# rather than a true "still running" indicator. Long enough to read as
# real feedback for a directory walk of a typical library, short enough
# that it can't get stuck "on" if rescan() throws.
_VIDEO_SCAN_PULSE_HOLD = 2.0

# How long a page-level status message (BT "Connecting...", "Paired",
# "Pairing failed", etc.) or the player screen's small storage-switch
# toast stays visible before clearing itself. Long enough to read, short
# enough not to look stuck once the operation is actually done.
_BT_STATUS_HOLD = 2.5
_TOAST_HOLD = 1.8


def find_touch_device(explicit_path: str = "") -> "evdev.InputDevice | None":
    """Pick the touchscreen's evdev device.

    Explicit path wins if given (config.touch.device). Otherwise scans
    every /dev/input device for multitouch capability (ABS_MT_POSITION_X
    and _Y) first, falling back to single-touch (ABS_X/ABS_Y + BTN_TOUCH)
    — covers both protocol A and protocol B touch controllers without
    needing to know which this specific Hosyond panel uses in advance.

    Logs every candidate it considers at INFO so a wrong pick (or "found
    nothing") is diagnosable from the journal alone, without needing
    evtest installed.
    """
    if explicit_path:
        try:
            return evdev.InputDevice(explicit_path)
        except OSError as exc:
            LOG.error("touch.device=%r configured but could not be opened: %s",
                      explicit_path, exc)
            return None

    candidates = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities()
        abs_caps = dict(caps.get(ecodes.EV_ABS, []))
        key_caps = set(caps.get(ecodes.EV_KEY, []))
        is_multitouch = ecodes.ABS_MT_POSITION_X in abs_caps and ecodes.ABS_MT_POSITION_Y in abs_caps
        is_singletouch = (ecodes.ABS_X in abs_caps and ecodes.ABS_Y in abs_caps
                           and ecodes.BTN_TOUCH in key_caps)
        LOG.info("evdev candidate %s (%s): multitouch=%s singletouch=%s",
                  path, dev.name, is_multitouch, is_singletouch)
        if is_multitouch or is_singletouch:
            candidates.append((dev, is_multitouch))

    if not candidates:
        LOG.error("no touch-capable evdev device found — is the panel connected? "
                   "set [touch].device explicitly in config.toml to skip auto-detect")
        return None
    # Prefer a real multitouch device over a single-touch fallback if both
    # somehow matched (e.g. a USB mouse also exposes ABS_X/ABS_Y on some
    # kernels — BTN_TOUCH narrows that, but multitouch is the stronger
    # signal when there's a choice).
    candidates.sort(key=lambda pair: pair[1], reverse=True)
    chosen = candidates[0][0]
    LOG.info("using touch device: %s (%s)", chosen.path, chosen.name)
    return chosen


class TouchDaemon:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._running = True

        self._mpd = MpdCommander(config.mpd.host, config.mpd.port, config.mpd.timeout)
        self._router = OutputRouter(self._mpd, config.routes)

        # Only ONE process should run BusServer — tourbox-player already
        # does (it starts first; see After= in the systemd unit). This
        # daemon publishes toasts nowhere yet in v1 (no on-screen toast
        # surface designed yet), so a real bus connection has nothing to
        # do — NullBus is a correct no-op, not a placeholder for a bug.
        self._bus: BusServer | NullBus = NullBus()

        self._video = VideoCommander(config.video.mpv_socket, config.video.timeout) \
            if config.video.enabled else None
        self._video_library = VideoLibrary(config.video.library_dir,
                                           extensions=config.video.extensions) \
            if config.video.enabled else None
        self._mode_file = config.video.mode_file

        self._ctx = ActionContext(
            mpd=self._mpd,
            router=self._router,
            bus=self._bus,
            volume_step=5,
            seek_step=15,
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
        )

        # v3 redesign: the touch UI's palette is now the exact hex spec from
        # the design doc (see Theme's field defaults in touchui/render.py) —
        # it deliberately does NOT inherit from config.streamdeck.theme
        # anymore. The Stream Deck's theme is a different device with a
        # different set of fields (bg/fg/muted/accent/active/warn) that no
        # longer line up 1:1 with this dataclass (which replaced `warn` with
        # per-element accent colors: favorite/scan/danger/accent_video, and
        # added surface/elevated/border/fg_secondary). Only the font paths
        # are still worth sharing, since they're just filesystem paths, not
        # design-language choices.
        theme = Theme(
            font_regular=config.streamdeck.theme.font_regular,
            font_bold=config.streamdeck.theme.font_bold,
        )
        self._renderer = Renderer(theme)

        self._touch = config.touch
        self._overlay_visible = True
        self._last_render_at = 0.0
        self._last_pushed: bytes | None = None
        self._overlay_error_logged = False
        self._overlay_confirmed = False

        # Auto-hide state — see _AUTO_HIDE_DELAY module constant and
        # _apply_auto_hide()'s docstring.
        self._overlay_auto_hide_at: float | None = None
        self._was_playing_video = False

        # touch-state machine
        self._down = False
        self._down_button: layout.Button | None = None
        self._down_x = 0
        self._down_y = 0
        self._down_at = 0.0
        self._x = 0
        self._y = 0
        self._mt_x: int | None = None
        self._mt_y = None
        # Which raw evdev signal is authoritative for touch-down/up — set
        # for real once the actual device's capabilities are known, in
        # run(). Defaults True (ABS_MT_TRACKING_ID) here only so the
        # attribute exists before that; see _handle_raw_event's docstring.
        self._uses_tracking_id: bool = True
        # Content-area gesture lock ("seek" | "volume" | None) — see
        # _GESTURE_DEADZONE and _handle_content_gesture().
        self._gesture: str | None = None
        # Which action's button is currently held down, if it needs a
        # visual "pressed" state — currently only Delete's armed-red
        # warning (design spec: neutral until actually being touched).
        # See _on_touch_down/_on_touch_up.
        self._pressed_action: str = ""

        # Transient volume HUD deadline (monotonic time) — see
        # _VOLUME_HUD_HOLD, _handle_content_gesture, and _apply_volume_hud.
        # 0.0 (the default) is always "expired" since time.monotonic() is
        # never negative, so the HUD starts hidden with no special-casing.
        self._volume_hud_until: float = 0.0

        # Real album-art cache, keyed by MPD song file path — decoded once
        # per track (touch_daemon._get_album_art), not re-fetched every
        # render tick. Capped crudely (see _get_album_art) so leaving the
        # daemon running for weeks browsing a huge library can't grow this
        # without bound; a full LRU wasn't judged worth the complexity for
        # a cache of ~168x168 thumbnails.
        self._art_cache: dict[str, "Image.Image | None"] = {}

        # Real audio-reactive visualizer (music mode only) — see
        # audio_visualizer.py's module docstring. Instantiated here
        # unconditionally (cheap: just sets up state, no fifo open happens
        # until .start()); the fifo path comes from config so it stays in
        # sync with system/mpd.conf's "Visualizer" output block without
        # duplicating the path as a second hardcoded constant.
        self._visualizer = AudioVisualizer(
            fifo_path=config.touch.visualizer_fifo_path,
            bar_count=layout.VISUALIZER_BAR_COUNT,
        )

        # Video-mode scan pulse — VideoLibrary.rescan() (video.py) is
        # synchronous with no async "still running" signal to poll (unlike
        # MPD's own status()['updating_db'] for music, read live in
        # _collect_state). So a video rescan tap just arms a short timed
        # pulse here, mirrored into state.scanning the same way the volume
        # HUD mirrors self._volume_hud_until — good enough to satisfy the
        # spec's "obvious but subtle activity state while running" without
        # needing a second thread or a completion callback video.py doesn't
        # have. See _dispatch_button and _apply_scan_pulse.
        self._video_scan_pulse_until: float = 0.0

        # -- navigation pages (PROMPT #4) -------------------------------------
        # See touchui/layout.py's PAGE_* module docstring for why this
        # exists: BT/AP taps used to blindly auto-switch to the first
        # available route with no page to show for it. This reproduces the
        # StreamDeck XL's real PAGE_BT_PICKER/PAGE_AIRPLAY_PICKER behavior
        # (same player/bt.py and player/wifi.py calls) on the touchscreen.
        # Navigating to/from a page never touches player/mode.py's mode
        # file, so it can never affect which of Music/Video is "current" or
        # disturb playback — see _enter_bt_page/_enter_airplay_page.
        self._page: str = layout.PAGE_PLAYER

        # Bluetooth page state.
        self._bt_entries: list[tuple[str, str, bool]] = []  # (mac, name, connected)
        self._bt_scanning: bool = False
        self._bt_status: str = ""
        self._bt_status_until: float = 0.0
        self._bt_reset_busy: bool = False
        self._bt_connected_mac: str = ""  # last MAC successfully paired this run

        # AirPlay page state -- self._airplay_sinks holds the live PwSink
        # objects (needed by switch_to_sink on selection), not just labels.
        self._airplay_sinks: list = []
        self._airplay_active_label: str = ""

        # Wi-Fi status (used on the AirPlay page) -- cached and refreshed
        # only while that page is open (see _enter_airplay_page /
        # _collect_state), not polled every render tick on the player
        # screen where it's irrelevant.
        self._wifi_status_cache: dict = {}
        self._wifi_reconnect_busy: bool = False
        self._wifi_fix_busy: bool = False
        self._smb_status_cache: dict = {}

        # Generic small transient status toast for the player screen itself
        # (currently just storage-switch feedback — PROMPT #4 part 15:
        # "currently a storage change can appear as though nothing
        # happened"). Same re-armed-deadline pattern as _volume_hud_until.
        self._toast_text: str = ""
        self._toast_until: float = 0.0

    # -- lifecycle -----------------------------------------------------------

    def stop(self, *_: object) -> None:
        LOG.info("shutting down")
        self._running = False
        self._visualizer.stop()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        if evdev is None:
            LOG.error("python-evdev is not installed in this venv — "
                      "pip install evdev (see requirements.txt)")
            return 1

        dev = find_touch_device(self._touch.device)
        if dev is None:
            return 1
        self._axis_x = self._axis_info(dev, ecodes.ABS_MT_POSITION_X, ecodes.ABS_X)
        self._axis_y = self._axis_info(dev, ecodes.ABS_MT_POSITION_Y, ecodes.ABS_Y)
        LOG.info("touch axis ranges: x=%s y=%s", self._axis_x, self._axis_y)

        # Duplicate touch-lifecycle fix: most real touchscreen controllers
        # (very likely including this panel) report a single physical
        # contact through BOTH the protocol-B multitouch event
        # (ABS_MT_TRACKING_ID going from -1 to a real id, and back to -1 on
        # release) AND the legacy protocol-A single-touch mirror (BTN_TOUCH
        # 1/0) -- the Linux kernel's own multitouch-protocol documentation
        # explicitly recommends drivers emit both, for backward
        # compatibility with single-touch-only software. Previously
        # _handle_raw_event() treated each as an independent trigger for
        # _on_touch_down()/_on_touch_up(), so ONE physical tap produced TWO
        # (occasionally more) logical touch-down/up cycles -- confirmed live
        # via journalctl: every content-area tap logged "touch tap ->
        # _toggle_overlay" immediately followed by "restoring overlay" (the
        # second, redundant touch-up landing after the first one had already
        # hidden the overlay), a single Scan tap logged THREE
        # "update_database" dispatches back to back, and rapid-fire
        # Next/Prev taps (each really a duplicated pair) triggered mpv's
        # documented loadfile-race warning ("did not confirm file load
        # within 3.0s").
        #
        # Fix: pick ONE authoritative lifecycle source, on this axis-info
        # call, based on the device's actual advertised capabilities --
        # ABS_MT_TRACKING_ID when the panel reports it (the kernel's own
        # docs call this the authoritative per-contact signal on protocol-B
        # devices, with BTN_TOUCH being redundant/optional there), falling
        # back to BTN_TOUCH only for a genuine single-touch-only device that
        # never reports a tracking id at all. This is a capability check,
        # not a timing-based debounce -- no delay is added to real taps.
        abs_caps = dict(dev.capabilities().get(ecodes.EV_ABS, []))
        self._uses_tracking_id = ecodes.ABS_MT_TRACKING_ID in abs_caps
        LOG.info("touch lifecycle source: %s",
                 "ABS_MT_TRACKING_ID (BTN_TOUCH ignored)" if self._uses_tracking_id
                 else "BTN_TOUCH (no ABS_MT_TRACKING_ID reported)")

        self._ensure_idle_video()
        self._visualizer.start()

        LOG.info("touch daemon started")
        self._render_and_push(force=True)

        while self._running:
            try:
                ready, _, _ = select.select([dev.fd], [], [], self._touch.tick_seconds)
            except (OSError, ValueError) as exc:
                LOG.warning("select on touch device failed: %s — reconnecting", exc)
                time.sleep(1.0)
                new_dev = find_touch_device(self._touch.device)
                if new_dev is None:
                    time.sleep(2.0)
                    continue
                dev = new_dev
                # Re-derive the lifecycle source for whichever device we
                # just reconnected to -- a hot-swap could land on hardware
                # with different capabilities than what we started with.
                abs_caps = dict(dev.capabilities().get(ecodes.EV_ABS, []))
                self._uses_tracking_id = ecodes.ABS_MT_TRACKING_ID in abs_caps
                continue

            if ready:
                try:
                    for event in dev.read():
                        self._handle_raw_event(event)
                except (OSError, BlockingIOError):
                    pass  # transient — next select() will recover or reconnect

            now = time.monotonic()
            if now - self._last_render_at >= self._touch.tick_seconds:
                self._render_and_push()

        return 0

    @staticmethod
    def _axis_info(dev, mt_code, single_code):
        caps = dict(dev.capabilities().get(ecodes.EV_ABS, []))
        info = caps.get(mt_code) or caps.get(single_code)
        if info is None:
            LOG.warning("touch device reports no usable X/Y axis info — "
                        "falling back to assuming 0-%d/0-%d raw range",
                        layout.W, layout.H)
            return (0, layout.W)
        return (info.min, info.max)

    # -- touch event handling -------------------------------------------------

    def _handle_raw_event(self, event) -> None:
        """Update touch state from ONE raw evdev event.

        Logged at DEBUG per-event (very chatty — only useful with
        --verbose while diagnosing a misbehaving panel) and at INFO for
        the down/up/dispatch transitions that actually matter.

        Only ONE of ABS_MT_TRACKING_ID / BTN_TOUCH is treated as the
        touch-lifecycle signal (see ``self._uses_tracking_id``, set once in
        ``run()`` from the device's real capabilities) — a controller that
        reports both for the same physical contact (the common case; see the
        kernel's multitouch-protocol docs) would otherwise fire
        _on_touch_down()/_on_touch_up() twice per tap. BTN_TOUCH's position
        (x/y) is still read unconditionally below since some single-touch
        devices only ever populate ABS_X/ABS_Y, never the MT_POSITION pair.
        """
        if event.type == ecodes.EV_ABS:
            if event.code in (ecodes.ABS_MT_POSITION_X, ecodes.ABS_X):
                self._mt_x = event.value
            elif event.code in (ecodes.ABS_MT_POSITION_Y, ecodes.ABS_Y):
                self._mt_y = event.value
            elif event.code == ecodes.ABS_MT_TRACKING_ID and self._uses_tracking_id:
                if event.value == -1:
                    self._on_touch_up()
                else:
                    self._on_touch_down()
        elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
            if not self._uses_tracking_id:
                if event.value == 1:
                    self._on_touch_down()
                else:
                    self._on_touch_up()
        elif event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
            self._on_position_update()

    def _to_logical(self, raw_x: int, raw_y: int) -> tuple[int, int]:
        xmin, xmax = self._axis_x
        ymin, ymax = self._axis_y
        xspan = max(1, xmax - xmin)
        yspan = max(1, ymax - ymin)
        lx = int((raw_x - xmin) / xspan * (layout.W - 1))
        ly = int((raw_y - ymin) / yspan * (layout.H - 1))
        lx = max(0, min(layout.W - 1, lx))
        ly = max(0, min(layout.H - 1, ly))
        if self._touch.rotate_180:
            lx, ly = layout.rotate_point_180(lx, ly)
        return lx, ly

    def _on_touch_down(self) -> None:
        if self._mt_x is None or self._mt_y is None:
            return
        self._x, self._y = self._to_logical(self._mt_x, self._mt_y)
        self._down = True
        self._down_x, self._down_y = self._x, self._y
        self._down_at = time.monotonic()
        self._gesture = None
        if self._page != layout.PAGE_PLAYER:
            # Navigation pages (BT/AirPlay) have their own button set and
            # no drag/gesture/auto-hide behavior at all -- see
            # layout.hit_test_page and _dispatch_page_button.
            self._down_button = layout.hit_test_page(self._x, self._y, self._page)
            LOG.debug("touch down at (%d, %d) on page %s -> %s", self._x, self._y,
                      self._page, self._down_button.action if self._down_button else None)
            return
        mode = _mode.read_mode(self._mode_file)
        self._down_button = layout.hit_test(self._x, self._y, mode)
        LOG.debug("touch down at (%d, %d) -> %s", self._x, self._y,
                  self._down_button.action if self._down_button else None)
        # Delete's "armed" red warning state (design spec: DELETE stays
        # neutral until actually being touched) — tracked as a plain
        # action-name string rather than a bool so render.py can compare
        # it against whichever Delete rect is live for the current mode
        # without touch_daemon needing to know render.py's per-button
        # visual rules. Cleared unconditionally in _on_touch_up.
        if self._down_button is not None and self._down_button.action in (
                "delete_current", "video_delete_current"):
            self._pressed_action = self._down_button.action
            self._render_and_push(force=True)
        if self._down_button is not None and self._down_button.action == "_seek_absolute":
            self._apply_drag(self._down_button)

    def _on_position_update(self) -> None:
        if not self._down or self._mt_x is None or self._mt_y is None:
            return
        self._x, self._y = self._to_logical(self._mt_x, self._mt_y)
        if self._down_button is not None and self._down_button.action == "_seek_absolute":
            now = time.monotonic()
            if now - self._last_render_at >= _DRAG_RENDER_INTERVAL:
                self._apply_drag(self._down_button)
        elif self._down_button is layout.CONTENT_AREA:
            self._handle_content_gesture()

    def _on_touch_up(self) -> None:
        if not self._down:
            return
        self._down = False
        button = self._down_button
        self._down_button = None
        gesture = self._gesture
        self._gesture = None
        was_pressed = bool(self._pressed_action)
        self._pressed_action = ""
        if button is None:
            return

        if self._page != layout.PAGE_PLAYER:
            # Same "release must land back on the same button" tap-cancel
            # rule as the player screen, just against the page's own
            # button set/hit-test (see layout.hit_test_page). No gestures,
            # no overlay auto-hide, no CONTENT_AREA catch-all on a
            # sub-page -- an empty tap here is simply a no-op.
            released_on = layout.hit_test_page(self._x, self._y, self._page)
            if released_on is not button:
                return
            LOG.info("touch tap (page=%s) -> %s", self._page, button.action)
            self._dispatch_page_button(button)
            self._render_and_push(force=True)
            return

        if was_pressed:
            # Clear Delete's armed-red visual immediately rather than
            # waiting for the next periodic tick (up to ~1s away per
            # [touch].tick_seconds) — every early-return below this point
            # would otherwise leave a stale red button on screen briefly
            # after the finger lifts.
            self._render_and_push(force=True)

        # If the overlay is currently hidden (auto-hidden or manually
        # hidden), ANY touch just brings it back — swallow it rather than
        # also dispatching whatever button happens to sit at that screen
        # location, since the user couldn't see a control was even there.
        # Per explicit request: "Any tap will bring UI back instantly." A
        # content-area gesture (seek/volume) already took effect live
        # during the drag itself (see _handle_content_gesture) even while
        # hidden, so this is just revealing the result, not re-applying it.
        if not self._overlay_visible:
            LOG.info("touch while overlay hidden -> restoring overlay")
            self._overlay_visible = True
            # Rearm the auto-hide window from a fresh 2s if video is still
            # playing, rather than leaving it hidden-again on the very next
            # tick because the old deadline already passed.
            self._was_playing_video = False
            self._render_and_push(force=True)
            return

        if button.action == "_seek_absolute":
            self._apply_drag(button)
            return

        if button is layout.CONTENT_AREA:
            if gesture is not None:
                # A seek/volume swipe just finished — already applied live
                # in _handle_content_gesture, nothing left to do (and
                # definitely don't also toggle the overlay, which a plain
                # content-area tap does below).
                return
            LOG.info("touch tap -> %s", button.action)
            self._dispatch_button(button, _mode.read_mode(self._mode_file))
            self._render_and_push(force=True)
            return

        # Tap-style buttons only fire if release is still over the SAME
        # button that was pressed — avoids firing e.g. Delete because a
        # drag that started on it happened to end elsewhere, and vice
        # versa lets the user "cancel" a tap by dragging off the button.
        mode = _mode.read_mode(self._mode_file)
        released_on = layout.hit_test(self._x, self._y, mode)
        if released_on is not button:
            LOG.debug("touch up off the original button (%s -> %s) — ignored",
                      button.action, released_on.action if released_on else None)
            return

        LOG.info("touch tap -> %s", button.action)
        self._dispatch_button(button, mode)
        self._render_and_push(force=True)

    def _apply_drag(self, button: layout.Button) -> None:
        """Absolute-position drag on a dedicated bar (SCRUB_BAR — VOLUME_BAR
        was removed in v4; volume is swipe-only now, see
        _handle_content_gesture and layout.py's module docstring item 2).
        """
        rect = button.rect
        fraction = (self._x - rect[0]) / max(1, rect[2])
        fraction = max(0.0, min(1.0, fraction))
        mode = _mode.read_mode(self._mode_file)

        if button.action == "_seek_absolute":
            if mode == "video" and self._video is not None:
                try:
                    duration = self._video.status().get("duration", 0.0)
                    self._video.seek_absolute(fraction * duration)
                except Exception:  # noqa: BLE001 - mpv may be idle/unreachable
                    LOG.debug("video seek_absolute failed", exc_info=True)
            else:
                self._mpd.seek_absolute(fraction)

        self._render_and_push()

    def _handle_content_gesture(self) -> None:
        """Live seek/volume while dragging anywhere in the open content
        area (not on a specific control) — the swipe gestures requested to
        replace the old dedicated bottom volume bar. Direction locks in on
        whichever axis moves first past _GESTURE_DEADZONE: horizontal ->
        scrub (mirrors SCRUB_BAR's own absolute-position drag, just not
        confined to that thin strip), vertical -> volume (top of the
        content area = 100%, bottom = 0%, matching every phone/media-player
        swipe-for-volume convention). Applies continuously as the finger
        moves, same throttling as the scrub-bar/old-volume-bar drags did.
        """
        dx = self._x - self._down_x
        dy = self._y - self._down_y
        if self._gesture is None:
            if abs(dx) < _GESTURE_DEADZONE and abs(dy) < _GESTURE_DEADZONE:
                return  # could still just be a plain tap — don't lock yet
            self._gesture = "seek" if abs(dx) >= abs(dy) else "volume"
            LOG.info("content gesture locked: %s", self._gesture)

        now = time.monotonic()
        if now - self._last_render_at < _DRAG_RENDER_INTERVAL:
            return

        area = layout.CONTENT_AREA.rect
        mode = _mode.read_mode(self._mode_file)

        if self._gesture == "seek":
            fraction = (self._x - area[0]) / max(1, area[2])
            fraction = max(0.0, min(1.0, fraction))
            if mode == "video" and self._video is not None:
                try:
                    duration = self._video.status().get("duration", 0.0)
                    self._video.seek_absolute(fraction * duration)
                except Exception:  # noqa: BLE001 - mpv may be idle/unreachable
                    LOG.debug("video seek_absolute (gesture) failed", exc_info=True)
            else:
                self._mpd.seek_absolute(fraction)
        else:  # "volume"
            fraction = 1.0 - (self._y - area[1]) / max(1, area[3])
            fraction = max(0.0, min(1.0, fraction))
            volume = int(round(fraction * 100))
            if mode == "video" and self._video is not None:
                try:
                    self._video.set_volume(volume)
                    self._ctx.video_last_set_volume = volume
                except Exception:  # noqa: BLE001
                    LOG.debug("video set_volume (gesture) failed", exc_info=True)
            else:
                self._mpd.set_volume(volume)
            # Show the transient volume HUD (see _apply_volume_hud) for a
            # bit after the most recent change — re-armed on every sample
            # so it stays up for the whole swipe, then counts down once the
            # finger stops moving/lifts.
            self._volume_hud_until = now + _VOLUME_HUD_HOLD

        self._render_and_push()

    def _dispatch_button(self, button: layout.Button, mode: str) -> None:
        action = button.action
        if action == "_toggle_mode":
            dispatch("exit_video_mode" if mode == "video" else "enter_video_mode", self._ctx)
        elif action == "_toggle_storage":
            # Restored to the SAME real action the StreamDeck XL uses
            # (actions.py's toggle_storage_source/video_toggle_storage_source
            # — symlink swap + mpd/video library refresh). Tracing this
            # this round found it was already wired correctly; the only
            # real gap was zero visible feedback (PROMPT #4 part 15), which
            # is what the toast below adds — it does NOT fake success
            # before the swap actually happens, since dispatch() runs
            # synchronously and we only set the toast from its real result.
            result = dispatch("video_toggle_storage_source" if mode == "video"
                              else "toggle_storage_source", self._ctx)
            self._toast_text = result or "Storage switch failed"
            self._toast_until = time.monotonic() + _TOAST_HOLD
        elif action == "_toggle_overlay":
            self._overlay_visible = not self._overlay_visible
        elif action == "_open_bt":
            self._enter_bt_page()
        elif action == "_open_airplay":
            self._enter_airplay_page()
        elif action == "update_database":
            # Debounce: MPD itself refuses a second concurrent update, but
            # it does so by returning an error we'd otherwise just swallow
            # -- checking updating_db first avoids spamming that error and
            # matches the spec's explicit "prevent duplicate/overlapping
            # scan triggers" requirement.
            if self._mpd.status().get("updating_db"):
                LOG.info("scan tap ignored -- a scan is already in progress")
            else:
                dispatch(action, self._ctx)
        elif action == "video_rescan":
            # No async completion signal exists for this synchronous call
            # (see __init__'s comment on _video_scan_pulse_until), so a
            # rescan that's already "pulsing" just re-arms the same pulse
            # instead of firing a second directory walk.
            now = time.monotonic()
            if now < self._video_scan_pulse_until:
                LOG.info("video scan tap ignored -- a scan pulse is already active")
            else:
                dispatch(action, self._ctx)
                self._video_scan_pulse_until = now + _VIDEO_SCAN_PULSE_HOLD
        elif action.startswith("_"):
            LOG.warning("unhandled pseudo-action %r", action)
        else:
            dispatch(action, self._ctx)

    # -- navigation pages (PROMPT #4) -----------------------------------------
    # Reproduces streamdeck_daemon.py's PAGE_BT_PICKER/PAGE_AIRPLAY_PICKER
    # behavior on the touchscreen, calling the SAME player/bt.py and
    # player/wifi.py backend functions (see this module's imports) rather
    # than reimplementing pairing/Wi-Fi. Entering a page never writes to
    # player/mode.py, so Music/Video mode and playback are untouched by
    # navigation — see _collect_state, which still reads the real mode
    # file on every render regardless of self._page.

    def _enter_bt_page(self) -> None:
        """Mirrors streamdeck_daemon._enter_bt_picker: show whatever
        bluetoothd already knows about immediately (fast, no scan), then
        kick a background scan and refresh the list once it completes.
        """
        self._page = layout.PAGE_BT
        self._bt_entries = [(mac, name, self._bt_is_connected(mac))
                            for mac, name in _bt.known_audio_devices()]
        self._bt_scanning = True
        self._bt_reset_busy = False
        self._bt_status = "Scanning..."
        self._bt_status_until = time.monotonic() + _BT_STATUS_HOLD

        def on_scan_done(devices: list[tuple[str, str]]) -> None:
            self._bt_entries = [(mac, name, self._bt_is_connected(mac)) for mac, name in devices]
            self._bt_scanning = False
            self._render_and_push(force=True)

        _bt.scan_async(on_scan_done)

    def _bt_is_connected(self, mac: str) -> bool:
        """Whether this device is the one the router currently has active
        — used to put a "Connected" tag + accent on the right row. Cheap:
        only compares against the single current route, no extra bt.py
        call per row per render.
        """
        route = self._router.current()
        return bool(route) and route.icon == "bluetooth" and self._bt_connected_mac == mac

    def _on_bt_entry_press(self, slot: int) -> None:
        if slot >= len(self._bt_entries):
            return
        mac, name, _connected = self._bt_entries[slot]
        self._bt_status = "Connecting..."
        self._bt_status_until = time.monotonic() + _BT_STATUS_HOLD

        def on_done(ok: bool) -> None:
            if ok:
                bt_route = next((r for r in self._router.routes if r.id == "bt"), None)
                if bt_route is not None:
                    self._router.switch_to(bt_route)
                self._bt_connected_mac = mac
                self._bt_status = f"Paired: {name[:20]}"
            else:
                self._bt_status = "Pairing failed"
            self._bt_status_until = time.monotonic() + _BT_STATUS_HOLD
            # Refresh connected-state tags on whatever's still in the list.
            self._bt_entries = [(m, n, self._bt_is_connected(m)) for m, n, _c in self._bt_entries]
            self._render_and_push(force=True)

        _bt.pair_and_connect_async(mac, on_done)

    def _on_bt_reset_press(self) -> None:
        if self._bt_reset_busy:
            return  # already running -- ignore a double-tap
        self._bt_reset_busy = True
        self._bt_status = "Resetting..."
        self._bt_status_until = time.monotonic() + _BT_STATUS_HOLD

        def on_done(count: int) -> None:
            self._bt_reset_busy = False
            self._bt_status = f"Reset {count} pairing(s)" if count else "Nothing stuck"
            self._bt_status_until = time.monotonic() + _BT_STATUS_HOLD
            self._render_and_push(force=True)

        _bt.reset_failed_pairings_async(on_done)

    def _enter_airplay_page(self) -> None:
        """Mirrors streamdeck_daemon._enter_airplay_picker: only sinks
        matching a configured icon="airplay" route, not every raop_sink on
        the LAN (see that method's docstring on why — a stranger's laptop
        with AirPlay receiving on should never show up here).
        """
        self._page = layout.PAGE_AIRPLAY
        self._refresh_airplay_sinks()
        self._wifi_status_cache = _wifi.status()
        # SMB (drop-folder server) status -- see player/smb.py's module
        # docstring for why this lives here rather than a dedicated page:
        # this is already the network-diagnostics screen, and there is no
        # separate SMB header button/action to navigate from (part 16 was
        # explicit there's no SMB client to build a picker for).
        self._smb_status_cache = _smb.status()

    def _refresh_airplay_sinks(self) -> None:
        live_sinks = self._router.pw.list_sinks()
        picks = []
        for route in self._router.routes:
            if route.icon != "airplay" or not route.pw_sink:
                continue
            sink = next((s for s in live_sinks if s.matches(route.pw_sink)), None)
            if sink is not None:
                picks.append(sink)
        self._airplay_sinks = picks

    def _on_airplay_entry_press(self, slot: int) -> None:
        if slot >= len(self._airplay_sinks):
            return
        sink = self._airplay_sinks[slot]
        if self._router.switch_to_sink(sink):
            self._airplay_active_label = sink.description or sink.name
            # Navigating here must never stop playback (PROMPT #4 part 14)
            # — switch_to_sink() only changes PipeWire's default sink and
            # MPD's active output, exactly like the header AP pill used to
            # do; it does not touch mpd.stop()/mpv at all, so an
            # already-playing track keeps playing straight through this.
            if self._video is not None and _mode.read_mode(self._mode_file) == "video":
                self._video.set_audio_device("auto")
                self._video.reload_audio()
        self._render_and_push(force=True)

    def _on_wifi_reconnect_press(self) -> None:
        if self._wifi_reconnect_busy:
            return
        self._wifi_reconnect_busy = True
        self._render_and_push(force=True)

        def on_done(ok: bool) -> None:
            self._wifi_reconnect_busy = False
            self._wifi_status_cache = _wifi.status()
            self._render_and_push(force=True)

        _wifi.reconnect_async(on_done)

    def _on_wifi_fix_press(self) -> None:
        if self._wifi_fix_busy:
            return
        self._wifi_fix_busy = True
        self._render_and_push(force=True)

        def on_done(ok: bool) -> None:
            self._wifi_fix_busy = False
            self._wifi_status_cache = _wifi.status()
            self._render_and_push(force=True)

        _wifi.restart_networking_async(on_done)

    def _dispatch_page_button(self, button: layout.Button) -> None:
        action = button.action
        if action == "_page_back":
            # Never touches player/mode.py — returning to the player
            # screen always shows whichever of Music/Video was already
            # active (PROMPT #4 part 3/12), since that's read fresh from
            # the mode file on every render regardless of self._page.
            self._page = layout.PAGE_PLAYER
            return
        if self._page == layout.PAGE_BT:
            if action == "_bt_scan":
                self._enter_bt_page()
            elif action == "_bt_reset":
                self._on_bt_reset_press()
            elif action.startswith("_bt_entry:"):
                self._on_bt_entry_press(int(action.split(":", 1)[1]))
            return
        if self._page == layout.PAGE_AIRPLAY:
            if action == "_wifi_reconnect":
                self._on_wifi_reconnect_press()
            elif action == "_wifi_fix":
                self._on_wifi_fix_press()
            elif action.startswith("_airplay_entry:"):
                self._on_airplay_entry_press(int(action.split(":", 1)[1]))
            return
        LOG.warning("unhandled page button %r on page %r", action, self._page)

    # -- idle video plane ----------------------------------------------------

    def _ensure_idle_video(self) -> None:
        """Load a muted, looping, near-empty black clip so mpv actually
        claims the DSI panel's DRM output before anything real is played.

        Found live on real hardware: mpv started with --idle=yes and NOTHING
        ever loaded does not perform a DRM modeset at all on this Pi/driver
        combination (drm-rp1-dsi) — it just never touches the display, and
        the kernel's own text console (whatever getty is on the active VT)
        keeps the screen indefinitely. overlay-add has nothing to composite
        onto in that state, and fails completely silently (see the
        overlay-add error-checking comment in _render_and_push — same
        underlying discovery). This clip's only job is to give mpv a reason
        to grab the display; entering video mode simply replaces it via the
        normal load_and_play path, same as switching between two real
        videos, so nothing else needs to know this clip exists.
        """
        if self._video is None or not self._touch.idle_clip_path:
            return
        try:
            resp = self._video.command(["loadfile", self._touch.idle_clip_path, "replace"])
            if not (isinstance(resp, dict) and resp.get("error") == "success"):
                LOG.warning("could not load idle clip %r to claim the display: %s",
                            self._touch.idle_clip_path, resp)
                return
            self._video.set("loop-file", "inf")
            self._video.set("mute", True)
            LOG.info("loaded idle clip %s to hold the DRM output", self._touch.idle_clip_path)
        except Exception:  # noqa: BLE001 - mpv may not be up yet; not fatal, just no display
            LOG.debug("failed to load idle clip (mpv not reachable yet?)", exc_info=True)

    # -- rendering -------------------------------------------------------------

    def _collect_state(self) -> OverlayState:
        mode = _mode.read_mode(self._mode_file)
        state = OverlayState(mode=mode, overlay_visible=self._overlay_visible,
                              pressed_action=self._pressed_action, page=self._page)

        if self._page == layout.PAGE_BT:
            # Sub-pages are complete, independent frames (see render.py's
            # render()) -- no need to also collect music/video/art state
            # nobody will draw while this page is open.
            now = time.monotonic()
            state.bt_entries = list(self._bt_entries)
            state.bt_scanning = self._bt_scanning
            state.bt_status = self._bt_status if now < self._bt_status_until else ""
            state.bt_reset_busy = self._bt_reset_busy
            return state

        if self._page == layout.PAGE_AIRPLAY:
            # Re-queries live PipeWire sinks on every render tick (a cheap
            # local query, not a network call) so a speaker appearing or
            # disappearing on the LAN while this page is open is reflected
            # without needing to leave and reopen the page — PROMPT #4
            # part 10 ("display the actual AirPlay devices currently
            # available"), which a one-shot snapshot at page-open time
            # would not satisfy.
            self._refresh_airplay_sinks()
            state.airplay_entries = [
                (s.description or s.name,
                 (s.description or s.name) == self._airplay_active_label or s.is_default)
                for s in self._airplay_sinks
            ]
            state.wifi_status = self._wifi_status_cache
            state.wifi_reconnect_busy = self._wifi_reconnect_busy
            state.wifi_fix_busy = self._wifi_fix_busy
            state.smb_status = self._smb_status_cache
            return state

        route = self._router.current()
        state.route_icon = route.icon if route else ""
        state.bt_available = any(r.icon == "bluetooth" and self._router.is_available(r)
                                  for r in self._router.routes)
        state.airplay_available = any(r.icon == "airplay" and self._router.is_available(r)
                                       for r in self._router.routes)

        if mode == "video" and self._video is not None:
            vstat = self._video.status()
            state.title = vstat.get("title", "")
            state.artist = vstat.get("track_label", "")
            state.elapsed = vstat.get("elapsed", 0.0)
            state.duration = vstat.get("duration", 0.0)
            state.playing = vstat.get("playing", False)
            try:
                state.volume = self._video.volume()
            except Exception:  # noqa: BLE001
                state.volume = 0
            # aid 1 = "Original (Vocal)", aid 2 = "Karaoke (Backing Track)" —
            # see skp.py's TRACK_TITLES (0-indexed there, 1-indexed here as
            # mpv's own audio-track id). Querying mpv's live aid directly
            # rather than pattern-matching track_label's text is the same
            # source of truth switch_track()/actions.py's _switch_track use.
            try:
                state.vocal_active = int(self._video.get("aid", 1) or 1) == 1
            except Exception:  # noqa: BLE001 - mpv may be idle/unreachable
                state.vocal_active = True
            state.storage = "USB" if video_storage_label() == "USB" else "INT"
            # "curate_current"/"video_curate_current" (actions.py) don't
            # toggle a flag anywhere — they physically MOVE the file into a
            # "Curated/" subfolder next to itself. There's no separate
            # curated bit to query, but that move IS observable: if the
            # currently loaded file's own path already runs through a
            # "Curated" directory, it's already been curated. Cheap, exact,
            # and needs no new state to keep in sync with the real action.
            media_path = vstat.get("path", "")
            state.curated = bool(media_path) and "Curated" in Path(media_path).parts
            state.tags = self._video_format_tags()
            state.scanning = time.monotonic() < self._video_scan_pulse_until
        else:
            mstat = self._mpd.status()
            song = self._mpd.current_song()
            state.title = song.get("title") or Path(song.get("file", "")).stem
            state.artist = song.get("artist", "")
            try:
                state.elapsed = float(mstat.get("elapsed", 0.0) or 0.0)
                state.duration = float(mstat.get("duration", 0.0) or 0.0)
                state.volume = max(0, int(mstat.get("volume", 0) or 0))
            except (TypeError, ValueError):
                pass
            state.playing = mstat.get("state") == "play"
            state.storage = "USB" if music_storage_label() == "USB" else "INT"
            song_file = song.get("file", "")
            state.curated = bool(song_file) and "Curated" in Path(song_file).parts
            state.meta_line = _album_meta_line(song)
            state.tags = _music_format_tags(song_file, mstat.get("audio", ""))
            state.art_image = self._get_album_art(song_file) if song_file else None
            # MPD's own status field -- exactly true while update_database's
            # scan job is running, no polling/timing guesswork needed (see
            # the debounce check in _dispatch_button, which reads the same
            # field before allowing a new scan to start).
            state.scanning = bool(mstat.get("updating_db"))
            # Real spectrum data (see audio_visualizer.py). Only sourced in
            # music mode, matching render.py's draw-gate
            # (`state.mode == "music" and state.title`) -- no cost paid in
            # video mode beyond this one attribute read, and get_levels()
            # decays toward silence on its own once MPD stops writing to
            # the fifo (paused/stopped), so no extra "is it playing" check
            # is needed here.
            state.visualizer_bars = self._visualizer.get_levels()

        return state

    def _get_album_art(self, song_file: str) -> "Image.Image | None":
        """Real album art for the art panel (render.py's module docstring,
        item 1) — cached per file path so this only hits MPD once per
        track, not once per render tick. Returns a pre-cropped-to-square
        RGBA thumbnail, or None (cached too, so a track with no art isn't
        re-queried every tick either).

        Every failure mode here — no art embedded, MPD too old for
        readpicture/albumart, a corrupt/undecodable image blob — is
        treated as "no art", never an error: falling back to the
        placeholder icon is always a safe, expected outcome, matching how
        the rest of this module treats optional metadata (format tags,
        meta_line, etc).
        """
        if song_file in self._art_cache:
            return self._art_cache[song_file]

        if len(self._art_cache) > 200:
            # Crude unbounded-growth guard (see __init__) rather than a
            # real LRU — simplest thing that keeps a long-running daemon
            # browsing a big library from accumulating thumbnails forever.
            LOG.debug("album art cache exceeded 200 entries — clearing")
            self._art_cache.clear()

        art: "Image.Image | None" = None
        try:
            data = self._mpd.album_art(song_file)
            if data:
                img = Image.open(io.BytesIO(data))
                img.load()  # force full decode now, inside this try/except
                img = img.convert("RGBA")
                art = _center_crop_square(img)
        except (UnidentifiedImageError, OSError, ValueError):
            LOG.debug("album art for %r fetched but could not be decoded", song_file, exc_info=True)
        except Exception:  # noqa: BLE001 - never let a bad cover art blob affect playback
            LOG.debug("album art fetch failed for %r", song_file, exc_info=True)

        self._art_cache[song_file] = art
        return art

    def _apply_volume_hud(self, state: OverlayState) -> None:
        """Mirror self._volume_hud_until (set on every volume-swipe sample,
        see _handle_content_gesture) into state.volume_hud_visible. Called
        once per render, same pattern as _apply_auto_hide — every code path
        that produces a frame (tick, tap, drag) sees the same up-to-date
        visibility, and the periodic tick in run() is what actually causes
        the HUD to disappear once its hold window elapses (no dedicated
        timer thread needed, same tradeoff _apply_auto_hide already makes).
        """
        state.volume_hud_visible = time.monotonic() < self._volume_hud_until

    def _apply_toast(self, state: OverlayState) -> None:
        """Mirror self._toast_text/_toast_until (set by e.g. the storage
        switch, see _dispatch_button) into state.toast_visible/toast_text
        — same per-render mirroring pattern as _apply_volume_hud."""
        if time.monotonic() < self._toast_until and self._toast_text:
            state.toast_visible = True
            state.toast_text = self._toast_text
        else:
            state.toast_visible = False
            state.toast_text = ""

    def _video_format_tags(self) -> list[str]:
        """Resolution/codec/aspect/fps chips for the header — mirrors the
        mockup's "1920x1080 | H.264 | 16:9 | 29.97 fps" row. Queried live
        from mpv rather than cached anywhere; best-effort, since none of
        this is essential to playback (missing properties just produce
        fewer chips, never an error).
        """
        if self._video is None:
            return []
        try:
            w = self._video.get("video-params/w") or self._video.get("width")
            h = self._video.get("video-params/h") or self._video.get("height")
            codec = self._video.get("video-codec", "") or ""
            fps = self._video.get("container-fps") or self._video.get("estimated-vf-fps")
        except Exception:  # noqa: BLE001 - mpv may be idle/unreachable
            return []
        tags = []
        if w and h:
            try:
                tags.append(f"{int(w)}x{int(h)}")
            except (TypeError, ValueError):
                pass
        if codec:
            tags.append(_short_codec(str(codec)))
        if w and h:
            try:
                tags.append(_aspect_ratio(int(w), int(h)))
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if fps:
            try:
                tags.append(f"{float(fps):.2f} fps")
            except (TypeError, ValueError):
                pass
        return tags

    def _apply_auto_hide(self, state: OverlayState) -> None:
        """Video-mode auto-hide: 2s after playback starts (or resumes from
        pause), hide the overlay so it doesn't sit over the video
        indefinitely. Mutates self._overlay_visible and state.overlay_visible
        in place; called once per render from _render_and_push() so every
        code path that produces a frame (tick, tap, drag) sees the same
        up-to-date visibility.
        """
        now = time.monotonic()
        playing_video = state.mode == "video" and state.playing
        if playing_video and not self._was_playing_video:
            self._overlay_auto_hide_at = now + _AUTO_HIDE_DELAY
        elif not playing_video:
            self._overlay_auto_hide_at = None
        self._was_playing_video = playing_video

        if (self._overlay_auto_hide_at is not None
                and now >= self._overlay_auto_hide_at
                and self._overlay_visible):
            LOG.info("auto-hiding overlay %.0fs after video playback started",
                      _AUTO_HIDE_DELAY)
            self._overlay_visible = False
            self._overlay_auto_hide_at = None

        state.overlay_visible = self._overlay_visible

    def _render_and_push(self, force: bool = False) -> None:
        try:
            state = self._collect_state()
        except Exception:  # noqa: BLE001 - never let a bad status() poll kill the daemon
            LOG.exception("failed to collect state for rendering")
            return

        self._apply_auto_hide(state)
        self._apply_volume_hud(state)
        self._apply_toast(state)

        frame = self._renderer.render(state, self._touch.rotate_180)
        self._last_render_at = time.monotonic()
        if not force and frame == self._last_pushed:
            return
        self._last_pushed = frame

        if self._video is None:
            return
        try:
            Path(self._touch.overlay_path).write_bytes(frame)
            stride = layout.W * 4
            resp = self._video.command([
                "overlay-add", self._touch.overlay_id, 0, 0,
                self._touch.overlay_path, 0, "bgra", layout.W, layout.H, stride,
            ])
            # VideoCommander.command() only raises on a SOCKET-level failure
            # (connection refused/reset/timeout) — an mpv-level rejection of
            # the command itself comes back as a normal JSON response like
            # {"error": "invalid parameter", "request_id": N}, which is not
            # an exception at all. Without this check, a bad overlay-add call
            # (wrong path, bad format string, mpv not yet ready to accept
            # overlays) fails completely silently: no crash, no log, nothing
            # on screen, and no way to tell push-succeeded-but-did-nothing
            # apart from push-never-happened. Found live: exactly this
            # silent-failure shape on first hardware bring-up.
            if isinstance(resp, dict) and resp.get("error") not in (None, "success"):
                if not self._overlay_error_logged:
                    LOG.warning("mpv rejected overlay-add: %s (path=%s, will keep "
                                "retrying silently after this)",
                                resp.get("error"), self._touch.overlay_path)
                    self._overlay_error_logged = True
            elif self._overlay_error_logged:
                LOG.info("overlay-add succeeded after previous failure(s)")
                self._overlay_error_logged = False
            elif not self._overlay_confirmed:
                LOG.info("overlay-add succeeded — overlay id %d should now be visible",
                          self._touch.overlay_id)
                self._overlay_confirmed = True
        except Exception:  # noqa: BLE001 - mpv may not be up yet; retried next tick
            LOG.debug("overlay push failed (mpv not reachable?)", exc_info=True)


# -- format-tag / meta-line / art helpers (module-level: no daemon state needed) --

def _center_crop_square(img: "Image.Image") -> "Image.Image":
    """Crop the larger dimension down so the image is square, centered —
    most embedded cover art already is square, but this guards against the
    occasional oddly-cropped or panorama-shaped embedded image looking
    stretched in the (also square) art panel. render.py resizes the result
    to the panel's exact pixel size separately; this only fixes aspect
    ratio.
    """
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _album_meta_line(song: dict) -> str:
    """"Album (Year)" line under the artist, music mode only — matches the
    mockup's "A Night At The Opera (1975)". Degrades gracefully: album
    only, year only, or neither (empty string, meaning render.py just
    skips that line).
    """
    album = song.get("album", "")
    date = song.get("date", "")
    year = date[:4] if date else ""
    if album and year:
        return f"{album} ({year})"
    return album or year


def _music_format_tags(song_file: str, audio_field: str) -> list[str]:
    """FLAC/24-bit/96 kHz/2ch chips — matches the mockup's format row.
    ``audio_field`` is MPD status()'s ``audio`` value, "samplerate:bits:
    channels" (e.g. "96000:24:2"), present only while actually playing.
    """
    tags = []
    ext = Path(song_file).suffix.lstrip(".").upper()
    if ext:
        tags.append(ext)
    parts = audio_field.split(":") if audio_field else []
    if len(parts) == 3:
        rate_str, bits, chans = parts
        try:
            rate_khz = int(rate_str) / 1000
            tags.append(f"{bits}-bit")
            tags.append(f"{rate_khz:g} kHz")
            tags.append(f"{chans}ch")
        except ValueError:
            pass
    return tags


# mpv's video-codec property returns short internal codec names, not the
# marketing-friendly labels the mockup uses — map the common ones, fall
# back to just upper-casing whatever mpv reported for anything else.
_CODEC_LABELS = {
    "h264": "H.264", "avc": "H.264", "hevc": "HEVC", "h265": "HEVC",
    "vp9": "VP9", "vp8": "VP8", "av1": "AV1", "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2", "theora": "Theora",
}


def _short_codec(codec: str) -> str:
    key = codec.strip().lower()
    return _CODEC_LABELS.get(key, codec.upper())


def _aspect_ratio(w: int, h: int) -> str:
    """Reduce w:h to a small integer ratio, snapping to the common
    16:9/4:3/21:9 video ratios within a small tolerance so 1920x1080 reads
    as "16:9" rather than a technically-correct but unfamiliar "16:9"-ish
    fraction from rounding (e.g. some encodes are 1920x1078).
    """
    if h <= 0:
        return ""
    ratio = w / h
    for label, target in (("16:9", 16 / 9), ("4:3", 4 / 3), ("21:9", 21 / 9), ("1:1", 1.0)):
        if abs(ratio - target) < 0.02:
            return label
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}" if g else f"{w}:{h}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Touchscreen control overlay daemon")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging("DEBUG" if args.verbose else config.log_level)

    if not config.touch.enabled:
        LOG.error("[touch].enabled is false in config.toml — nothing to do")
        return 1
    if evdev is None:
        LOG.error("python-evdev is not installed — run: "
                  "/opt/rpi-player/venv/bin/pip install evdev")
        return 1

    return TouchDaemon(config).run()


if __name__ == "__main__":
    sys.exit(main())
