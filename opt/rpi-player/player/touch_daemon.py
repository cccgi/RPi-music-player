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
import logging
import os
import select
import signal
import sys
import time
from pathlib import Path

from .actions import (
    ActionContext,
    DeleteSettings,
    dispatch,
    music_storage_label,
    video_storage_label,
)
from .config import Config, load_config, setup_logging
from . import mode as _mode
from .ipc import BusServer, NullBus
from .mpdbus import MpdCommander
from .outputs import OutputRouter
from .touchui import layout
from .touchui.render import OverlayState, Renderer, Theme
from .video import VideoCommander, VideoLibrary

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

        theme = Theme(
            bg=config.streamdeck.theme.bg, fg=config.streamdeck.theme.fg,
            muted=config.streamdeck.theme.muted, accent=config.streamdeck.theme.accent,
            active=config.streamdeck.theme.active, warn=config.streamdeck.theme.warn,
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

        # touch-state machine
        self._down = False
        self._down_button: layout.Button | None = None
        self._x = 0
        self._y = 0
        self._mt_x: int | None = None
        self._mt_y = None

    # -- lifecycle -----------------------------------------------------------

    def stop(self, *_: object) -> None:
        LOG.info("shutting down")
        self._running = False

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

        self._ensure_idle_video()

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
        """
        if event.type == ecodes.EV_ABS:
            if event.code in (ecodes.ABS_MT_POSITION_X, ecodes.ABS_X):
                self._mt_x = event.value
            elif event.code in (ecodes.ABS_MT_POSITION_Y, ecodes.ABS_Y):
                self._mt_y = event.value
            elif event.code == ecodes.ABS_MT_TRACKING_ID:
                if event.value == -1:
                    self._on_touch_up()
                else:
                    self._on_touch_down()
        elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
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
        mode = _mode.read_mode(self._mode_file)
        self._down_button = layout.hit_test(self._x, self._y, mode)
        LOG.debug("touch down at (%d, %d) -> %s", self._x, self._y,
                  self._down_button.action if self._down_button else None)
        if self._down_button is not None and self._down_button.action in (
                "_seek_absolute", "_volume_absolute"):
            self._apply_drag(self._down_button)

    def _on_position_update(self) -> None:
        if not self._down or self._mt_x is None or self._mt_y is None:
            return
        self._x, self._y = self._to_logical(self._mt_x, self._mt_y)
        if self._down_button is not None and self._down_button.action in (
                "_seek_absolute", "_volume_absolute"):
            now = time.monotonic()
            if now - self._last_render_at >= _DRAG_RENDER_INTERVAL:
                self._apply_drag(self._down_button)

    def _on_touch_up(self) -> None:
        if not self._down:
            return
        self._down = False
        button = self._down_button
        self._down_button = None
        if button is None:
            return

        if button.action in ("_seek_absolute", "_volume_absolute"):
            self._apply_drag(button)
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
        elif button.action == "_volume_absolute":
            volume = int(round(fraction * 100))
            if mode == "video" and self._video is not None:
                try:
                    self._video.set_volume(volume)
                    self._ctx.video_last_set_volume = volume
                except Exception:  # noqa: BLE001
                    LOG.debug("video set_volume failed", exc_info=True)
            else:
                self._mpd.set_volume(volume)

        self._render_and_push()

    def _dispatch_button(self, button: layout.Button, mode: str) -> None:
        action = button.action
        if action == "_toggle_mode":
            dispatch("exit_video_mode" if mode == "video" else "enter_video_mode", self._ctx)
        elif action == "_toggle_storage":
            dispatch("video_toggle_storage_source" if mode == "video"
                      else "toggle_storage_source", self._ctx)
        elif action == "_toggle_overlay":
            self._overlay_visible = not self._overlay_visible
        elif action == "_open_bt":
            self._tap_route_icon("bluetooth")
        elif action == "_open_airplay":
            self._tap_route_icon("airplay")
        elif action.startswith("_"):
            LOG.warning("unhandled pseudo-action %r", action)
        else:
            dispatch(action, self._ctx)

    def _tap_route_icon(self, icon: str) -> None:
        """Route icon tap, v1: switch straight to the first available route
        with this icon. No picker UI yet (the wireframe's BT/AirPlay icons
        were reviewed as single-tap-to-connect for a first pass — a real
        multi-device picker, like the Stream Deck's, is follow-up work,
        same open item as the AirPlay picker for the TourBox path).
        """
        route = next((r for r in self._router.routes
                       if r.icon == icon and self._router.is_available(r)), None)
        if route is None:
            LOG.info("route tap: no available %s route right now", icon)
            return
        LOG.info("route tap -> %s", route.id)
        self._router.switch_to(route)

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
        state = OverlayState(mode=mode, overlay_visible=self._overlay_visible)

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

        return state

    def _render_and_push(self, force: bool = False) -> None:
        try:
            state = self._collect_state()
        except Exception:  # noqa: BLE001 - never let a bad status() poll kill the daemon
            LOG.exception("failed to collect state for rendering")
            return

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
