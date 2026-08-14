#!/usr/bin/env python3
"""TourBox control daemon.

Reads raw bytes from the TourBox CDC-ACM serial port and drives MPD directly.

Explicitly NOT using uinput. Every existing TourBox Linux driver synthesises
keystrokes through /dev/uinput, which requires a graphical session with a
focused window to receive them. This is a headless appliance: there is no
focus, so a synthetic keystroke goes nowhere. We decode the protocol and issue
MPD commands over a socket instead, which also means the controller works
identically over SSH, on the console, and with no display attached at all.

Run standalone for debugging:
    sudo -u pi /opt/rpi-player/venv/bin/python -m player.tourbox_daemon --verbose
"""

from __future__ import annotations

import argparse
import errno
import glob
import logging
import os
import select
import signal
import sys
import time
from pathlib import Path

import serial

from .actions import ActionContext, DeleteSettings, dispatch
from .config import Config, Keymap, load_config, load_keymap, setup_logging
from . import mode as _mode
from .continuous import ContinuousPlayback
from .ipc import BusServer, NullBus
from .mpdbus import MpdCommander
from .outputs import OutputRouter
from .tourbox.protocol import Decoder, Event, EventKind
from .video import VideoCommander, VideoLibrary
from .video_continuous import VideoContinuous

# Index of the "video" layer within [layer_order] in keymap.toml. Not cycled
# by Top (see Decoder.cycle_layer) — only ever entered by the mode file
# below, written by the Stream Deck's enter_video_mode/exit_video_mode.
_VIDEO_LAYER = 2
_MUSIC_LAYER = 0

# How often to check the mode file for a change. Cheap (one stat + maybe a
# few-byte read); riding on the same _SELECT_TIMEOUT cadence would be
# needlessly frequent for something that changes on human timescales.
_MODE_POLL_SECONDS = 0.5

# Volume controls auto-repeat while held, same as every OS volume rocker and
# the Stream Deck's own Vol-/Vol+ keys (see streamdeck_daemon.py's
# REPEAT_ACTIONS) — a single tap for a small nudge is fine, sweeping the
# whole range one 3%-step tap at a time is not. The actions themselves
# already clamp to 0-100, so repeating past the bound is harmless: it just
# stops moving the number, which is "continue until 0% or 100%, then stop".
_REPEAT_ACTIONS = frozenset({
    "volume_up", "volume_down", "video_volume_up", "video_volume_down",
})
_REPEAT_START_DELAY = 0.4     # held-but-not-yet-repeating grace period
_REPEAT_INTERVAL = 0.15       # seconds between repeated steps once it starts

LOG = logging.getLogger("tourbox")

# Read timeout on the serial port. Also sets how often we get to run tick()
# for long-press and rotary-flush detection, so keep it well under the
# long-press threshold.
_SELECT_TIMEOUT = 0.02

# A single read this large or larger is a device RESPONSE FRAME, not input.
#
# This guard is not paranoia. The TourBox replies to the unlock/config command
# with a ~26-byte status frame whose tail is a run of 0x00 bytes — and 0x00 is
# the Tall button's press code. Feeding such a frame to the decoder injects a
# burst of phantom presses with no matching releases, which latches a button
# down forever; if it lands on the modifier, every subsequent control silently
# fires its shifted action.
#
# Real input is always one byte per event, so a burst this large is never a
# legitimate sequence of presses. Frames can arrive any time something else
# writes to the port — another process probing it, or a reply we did not
# consume after a reconnect.
_RESPONSE_FRAME_MIN = 8

# Reverse-engineered from a Windows capture (see AndyCappDev/tuxbox). Required
# by the Elite/Elite Plus. NOT required by the NEO, which streams button events
# with no handshake at all, so it is opt-in via config.
_UNLOCK_COMMAND = bytes.fromhex("5500078894001afe")


class TourBoxDaemon:
    def __init__(self, config: Config, keymap: Keymap) -> None:
        self._config = config
        self._keymap = keymap
        self._running = True

        self._serial: serial.Serial | None = None
        self._decoder = Decoder(
            keymap,
            long_press_seconds=config.tourbox.long_press_seconds,
            rotary_coalesce_seconds=config.tourbox.rotary_coalesce_seconds,
        )

        self._mpd = MpdCommander(
            config.mpd.host, config.mpd.port, config.mpd.timeout
        )
        self._router = OutputRouter(self._mpd, config.routes)

        if config.ipc.enabled:
            self._bus: BusServer | NullBus = BusServer(config.ipc.socket)
            self._bus.start()
        else:
            self._bus = NullBus()

        # Headless continuous playback. Lives HERE, not in the Stream Deck
        # daemon, because the display is optional and this behaviour is not.
        self._continuous = ContinuousPlayback(
            self._mpd, config.mpd.host, config.mpd.port, config.mpd.timeout,
            enabled=config.playback.auto_advance_folders,
            force_consume_off=config.playback.force_consume_off,
        )

        self._video = VideoCommander(config.video.mpv_socket, config.video.timeout) \
            if config.video.enabled else None
        self._video_library = VideoLibrary(config.video.library_dir,
                                           extensions=config.video.extensions) \
            if config.video.enabled else None
        self._mode_file = config.video.mode_file
        self._last_mode = _mode.MUSIC
        self._last_mode_check = 0.0

        # Volume-control auto-repeat (see _REPEAT_ACTIONS). Keyed by control
        # name -> monotonic time of its last repeat-fired step; entries are
        # dropped as soon as the control is no longer physically held.
        self._repeat_last_fire: dict[str, float] = {}

        self._ctx = ActionContext(
            mpd=self._mpd,
            router=self._router,
            bus=self._bus,
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
        )

        # Headless video auto-advance — same rationale as _continuous above,
        # video's equivalent of "queue ran dry, load the next folder".
        self._video_continuous = VideoContinuous(
            self._ctx, self._video, self._mode_file,
            enabled=config.video.enabled and config.video.auto_advance,
        )

    # -- lifecycle ---------------------------------------------------------

    def stop(self, *_: object) -> None:
        LOG.info("shutting down")
        self._running = False

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        self._notify_systemd("READY=1")
        LOG.info("tourbox daemon started")
        self._continuous.start()
        self._video_continuous.start()

        # MPD's crossfade is runtime-only state, reset to 0 on every mpd.service
        # restart — re-apply it every time this daemon starts so a crossfade
        # that "used to work" doesn't silently vanish after an unrelated MPD
        # restart. Only covers automatic transitions; see PlaybackConfig.
        if self._config.playback.crossfade_seconds > 0:
            self._mpd.set_crossfade(self._config.playback.crossfade_seconds)
            LOG.info("crossfade: %ds (automatic transitions only)",
                     self._config.playback.crossfade_seconds)

        # Reflect the real output state at startup rather than assuming.
        route = self._router.detect_current()
        if route:
            LOG.info("current output route: %s", route.label)
            self._bus.publish("route", id=route.id, label=route.label)
            # MPD persists volume and enabled-output state across restarts —
            # if the daemon is coming up with AirPlay already the active
            # route (crash, reboot, service restart), nothing "switches" to
            # it, so the clamp that normally lives in switch_to() never
            # runs. reassert_current() now also clamps AirPlay volume; call
            # it here so a stale 100% from a prior Local session can never
            # survive into the first AirPlay playback after a restart.
            self._router.reassert_current()
        else:
            # MPD can come up with EVERY output disabled — its state_file
            # persists whatever was enabled/disabled at last shutdown, and a
            # stale or blank one means no output is enabled at all. Found
            # live: this left the box silently accepting `play` with no
            # audible result and no error surfaced anywhere a driver would
            # see — `mpc status` was the only place "All audio outputs are
            # disabled" showed up. Force the configured default route on so
            # this can never again mean "no sound, no explanation".
            default = next((r for r in self._router.routes
                             if r.id == self._config.playback.default_route_id), None)
            if default is not None and self._router.is_available(default):
                LOG.warning("no output was enabled at startup — forcing default route %r",
                            default.id)
                if self._router.switch_to(default):
                    self._bus.publish("route", id=default.id, label=default.label)
            else:
                LOG.error("no output enabled at startup AND default route %r is unavailable "
                          "— audio will not play until a route is selected manually",
                          self._config.playback.default_route_id)

        while self._running:
            if self._serial is None:
                if not self._open_device():
                    self._sleep(self._config.tourbox.reconnect_delay)
                    continue
            try:
                self._pump()
            except (serial.SerialException, OSError) as exc:
                LOG.warning("serial error: %s", exc)
                self._close_device()
                self._sleep(self._config.tourbox.reconnect_delay)

        self._close_device()
        self._continuous.stop()
        self._video_continuous.stop()
        self._mpd.close()
        self._bus.stop()
        return 0

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(0.1)

    # -- device ------------------------------------------------------------

    def _candidate_ports(self) -> list[str]:
        """Preferred device first, then the fallback globs.

        The udev rule should give us a stable /dev/tourbox. The globs exist for
        the case where the rule has not been installed yet (or the VID/PID
        differs on a newer firmware) so the daemon is still usable during
        bring-up rather than failing opaquely.
        """
        candidates: list[str] = []
        primary = self._config.tourbox.device
        if os.path.exists(primary):
            candidates.append(primary)
        for pattern in self._config.tourbox.fallback_globs:
            for path in sorted(glob.glob(pattern)):
                if path not in candidates:
                    candidates.append(path)
        return candidates

    def _open_device(self) -> bool:
        for path in self._candidate_ports():
            try:
                port = serial.Serial(
                    port=path,
                    baudrate=self._config.tourbox.baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0,             # non-blocking; we select() instead
                    exclusive=True,        # fail loudly if something else has it
                )
            except serial.SerialException as exc:
                # EBUSY here almost always means ModemManager grabbed the port.
                if getattr(exc, "errno", None) == errno.EBUSY or "Busy" in str(exc):
                    LOG.error(
                        "%s is busy — ModemManager is probably probing it. "
                        "Install /etc/udev/rules.d/99-tourbox.rules, then check "
                        "with: sudo fuser -v %s",
                        path,
                        path,
                    )
                else:
                    LOG.debug("cannot open %s: %s", path, exc)
                continue

            self._serial = port
            self._decoder.reset()

            if self._config.tourbox.send_unlock:
                # Elite/Elite Plus need this before they report anything.
                # The reply is a ~26-byte frame; drain it here rather than let
                # it reach the decoder as phantom Tall presses.
                try:
                    port.reset_input_buffer()
                    port.write(_UNLOCK_COMMAND)
                    port.flush()
                    time.sleep(0.4)
                    reply = port.read(200)
                    LOG.info("unlock sent, %d-byte reply%s", len(reply),
                             f": {reply.hex()}" if reply else " (none)")
                    port.reset_input_buffer()
                except (serial.SerialException, OSError) as exc:
                    LOG.warning("unlock failed: %s", exc)

            LOG.info("TourBox connected on %s @ %d baud",
                     path, self._config.tourbox.baud)
            self._bus.publish("device", name="tourbox", state="connected", path=path)
            return True

        LOG.debug("no TourBox found (looked at: %s)",
                  ", ".join(self._candidate_ports()) or "nothing")
        return False

    def _close_device(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except (serial.SerialException, OSError):
                pass
            self._serial = None
            # Critical: clear held-button state. A button held while the cable
            # was pulled would otherwise stay latched forever, and a stuck
            # modifier makes every control fire its shifted action.
            self._decoder.reset()
            self._bus.publish("device", name="tourbox", state="disconnected")
            LOG.info("TourBox disconnected")

    # -- main loop ---------------------------------------------------------

    def _check_mode(self) -> None:
        """Keep the active layer in sync with music/video mode.

        Two ways this can change: the Stream Deck's mode-switch key (written
        to the mode file — see player/mode.py), or noticing directly that
        mpv actually has something loaded, which covers anything that isn't
        a Stream Deck press. Requested specifically: the TourBox should
        follow into the video layer whenever an .skp is actually playing,
        the same way the Stream Deck's panel does. Whichever daemon notices
        first writes the mode file so the other follows; writing the same
        value twice is harmless.

        One-directional, same as the Stream Deck side: entering video mode
        is automatic, LEAVING is only ever the Stream Deck's Music key —
        pausing to switch tracks shouldn't silently hand Side back to being
        a plain shift key mid-song.
        """
        now = time.monotonic()
        if now - self._last_mode_check < _MODE_POLL_SECONDS:
            return
        self._last_mode_check = now

        if self._video is not None and self._last_mode != _mode.VIDEO:
            # "playing", NOT "loaded" -- mpv runs with --keep-open=yes, so a
            # file stays "loaded" indefinitely after it is paused or even
            # after exit_video_mode explicitly pauses it. Checking "loaded"
            # here meant this condition re-fired on every single poll once
            # ANY video had ever been played, overriding the Music key the
            # instant it was pressed and permanently trapping the panel in
            # video mode. Found live: user reported being stuck with no way
            # back to music or to shutdown. "playing" is false the moment
            # exit_video_mode pauses mpv, so leaving actually sticks.
            if bool(self._video.status().get("playing")):
                _mode.write_mode(self._mode_file, _mode.VIDEO)
                self._last_mode = _mode.VIDEO
                self._decoder.set_layer(_VIDEO_LAYER)
                LOG.info("mode -> video (layer %d) [auto: mpv playing]", _VIDEO_LAYER)
                return

        current = _mode.read_mode(self._mode_file)
        if current == self._last_mode:
            return
        self._last_mode = current
        target_layer = _VIDEO_LAYER if current == _mode.VIDEO else _MUSIC_LAYER
        self._decoder.set_layer(target_layer)
        LOG.info("mode -> %s (layer %d)", current, target_layer)

    def _check_repeats(self) -> None:
        """Auto-repeat volume controls that are still physically held.

        The button's own initial press already fired one step through the
        normal event path (_handle) — this only covers what happens if it is
        STILL down after _REPEAT_START_DELAY, firing again every
        _REPEAT_INTERVAL until released. Uses Decoder.held_actions(), which
        reflects real physical state even for a plain single-press control
        with no long/double binding — one never emits its own release EVENT
        (see Decoder._feed_release), so there is nothing else here to key
        off of.
        """
        now = time.monotonic()
        held = self._decoder.held_actions()
        active = {name for name, action in held.items() if action in _REPEAT_ACTIONS}

        for name in list(self._repeat_last_fire):
            if name not in active:
                del self._repeat_last_fire[name]

        for name in active:
            since = self._decoder.held_since(name)
            if since is None or now - since < _REPEAT_START_DELAY:
                continue
            # Default of `since`, not `since + _REPEAT_START_DELAY`: the
            # outer guard above already ensures we never get here before the
            # grace period has elapsed, so the first-ever fire should land
            # as soon as that guard passes — not one MORE full interval
            # after it, which is what defaulting to the boundary itself
            # would cause.
            last = self._repeat_last_fire.get(name, since)
            if now - last < _REPEAT_INTERVAL:
                continue
            self._repeat_last_fire[name] = now
            dispatch(held[name], self._ctx, 1)

    def _pump(self) -> None:
        self._check_mode()
        self._check_repeats()
        assert self._serial is not None
        readable, _, errored = select.select(
            [self._serial.fileno()], [], [self._serial.fileno()], _SELECT_TIMEOUT
        )

        if errored:
            raise serial.SerialException("select reported error on serial fd")

        events: list[Event] = []

        if readable:
            chunk = self._serial.read(64)
            if chunk == b"":
                # A zero-length read on a readable fd means the far end went
                # away — the cable was pulled.
                raise serial.SerialException("device closed the connection")

            # Discriminate a device status frame from a burst of real input.
            #
            # Length alone is NOT sufficient: a fast rotary spin legitimately
            # delivers a dozen bytes in a single read. What separates them is
            # content — every byte of real input is a code in the keymap, while
            # a status frame carries arbitrary payload. Requiring BOTH "large"
            # and "contains unrecognised bytes" keeps fast spins working while
            # still catching the frame whose 0x00 padding would otherwise
            # register as a stream of phantom Tall presses.
            if len(chunk) >= _RESPONSE_FRAME_MIN and not self._decoder.all_known(chunk):
                LOG.debug(
                    "ignoring %d-byte response frame: %s",
                    len(chunk), chunk.hex(),
                )
                return

            now = time.monotonic()
            for byte in chunk:
                events.extend(self._decoder.feed(byte, now))

        events.extend(self._decoder.tick())

        for event in events:
            self._handle(event)

    def _handle(self, event: Event) -> None:
        if event.kind is EventKind.RAW:
            return  # already logged once by the decoder

        # Layer switch: no MPD action, but announce it so the Stream Deck (and
        # anything else on the bus) can show which layer is live. On a headless
        # device this toast is the ONLY feedback that the mode changed.
        if event.kind is EventKind.LAYER:
            LOG.info("layer -> %d (%s)", event.layer, event.layer_label)
            self._bus.publish("layer", index=event.layer, label=event.layer_label)
            self._bus.publish("toast", text=event.layer_label[:16])
            return

        if not event.action or event.action == "noop":
            return

        magnitude = event.magnitude
        steps = self._config.tourbox.steps

        # A fast spin gets larger per-detent steps so a full volume sweep does
        # not need thirty turns, while a slow deliberate turn stays
        # fine-grained.
        #
        # The rate comes from the decoder, which measures it across the actual
        # burst. Do NOT try to infer it from magnitude and the coalesce window:
        # a single detent would then always compute as a fast spin, and every
        # click would move the volume by the large step.
        fast = False
        if event.kind is EventKind.ROTARY:
            fast = event.rate >= steps.fast_threshold
            self._ctx.volume_step = steps.volume_fast if fast else steps.volume
            self._ctx.seek_step = steps.seek_fast if fast else steps.seek
        else:
            self._ctx.volume_step = steps.volume
            self._ctx.seek_step = steps.seek

        LOG.debug(
            "%s %s -> %s (x%d, %.1f det/s%s%s, layer=%s)",
            event.kind.value,
            event.control,
            event.action,
            magnitude,
            event.rate,
            ", fast" if fast else "",
            ", shifted" if event.shifted else "",
            event.layer_label or event.layer,
        )
        dispatch(event.action, self._ctx, magnitude)

    # -- systemd -----------------------------------------------------------

    @staticmethod
    def _notify_systemd(message: str) -> None:
        """Minimal sd_notify so the unit can use Type=notify.

        Avoids a dependency on python-systemd for eight lines of socket code.
        """
        addr = os.environ.get("NOTIFY_SOCKET")
        if not addr:
            return
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        try:
            import socket as _socket

            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM) as sock:
                sock.connect(addr)
                sock.sendall(message.encode())
        except OSError as exc:
            LOG.debug("sd_notify failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TourBox -> MPD control daemon")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--keymap", type=Path, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging("DEBUG" if args.verbose else config.log_level)
    keymap = load_keymap(args.keymap)

    LOG.info(
        "loaded keymap: %d buttons, %d rotaries, modifier=%s",
        len(keymap.buttons),
        len(keymap.rotaries),
        keymap.modifier,
    )

    return TourBoxDaemon(config, keymap).run()


if __name__ == "__main__":
    sys.exit(main())
