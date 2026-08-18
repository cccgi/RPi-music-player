"""Two-layer output routing: MPD audio_output + PipeWire default sink.

The mental model that makes this build tractable:

    MPD has TWO outputs, not three.

        [0] "Local"    -> ALSA hw:, the wired DAC. Bit-perfect, exclusive.
        [1] "Network"  -> PipeWire.

    Bluetooth and AirPlay are BOTH PipeWire sinks behind that single "Network"
    output. Choosing between them is not an MPD operation at all — it is
    ``wpctl set-default``.

So switching route is up to two steps:

    1. MPD layer:      enableoutput <target>, disableoutput <everything else>
    2. PipeWire layer: wpctl set-default <sink id>   (only when relevant)

Getting this wrong is the most common way this kind of build breaks: people
define three MPD outputs, discover Bluetooth and AirPlay both fight over the
same PipeWire node, and never work out why the wrong speaker is playing.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from .config import Route
from .mpdbus import MpdCommander

LOG = logging.getLogger(__name__)

WPCTL = shutil.which("wpctl") or "/usr/bin/wpctl"
PW_DUMP = shutil.which("pw-dump") or "/usr/bin/pw-dump"

# A freshly-woken AirPlay speaker plays whatever level MPD's software mixer
# happens to already be at, with no volume ramp — if that mixer was last set
# to 100% (e.g. from a bit-perfect Local session that never needed to be
# quiet), the speaker's first sound is instantly at its own max hardware
# volume. Reported live: this blasted a speaker at full volume at 2:30am.
# Applied whenever a route/sink switch lands on AirPlay — never lowers an
# already-quiet volume, only ever pulls down from something dangerously
# loud.
AIRPLAY_SAFE_VOLUME = 25

# Fallback parser for a wpctl status line: "  │  *   47. Some Sink  [vol: 1.00]"
_WPCTL_NODE_RE = re.compile(r"(?P<default>\*)?\s*(?P<id>\d+)\.\s+(?P<name>.+?)\s*(?:\[|$)")


@dataclass(frozen=True)
class PwSink:
    """A PipeWire audio sink.

    The distinction between ``name`` and ``description`` matters and was the
    source of a real bug:

        node.name        bluez_output.C8_7B_23_4A_F7_60.1
        node.description Bose QC45

    ``wpctl status`` prints only the DESCRIPTION. Matching config patterns like
    ``bluez_output`` or ``raop_sink`` against scraped wpctl output therefore
    never matched anything, and every wireless route silently reported itself
    as unavailable. We read ``pw-dump`` instead and match against the node
    name, the description, or the API.
    """

    id: int
    name: str                    # node.name — stable, contains bluez_output/raop_sink
    description: str = ""        # node.description — human label
    api: str = ""                # "bluez5", "alsa", ...
    factory: str = ""            # e.g. api.bluez5.a2dp.sink
    is_default: bool = False

    @property
    def is_a2dp(self) -> bool:
        return "a2dp" in self.factory.lower()

    def matches(self, needle: str) -> bool:
        n = needle.lower()
        return (n in self.name.lower()
                or n in self.description.lower()
                or n == self.api.lower())


class PipeWireControl:
    """Thin wrapper over ``wpctl``.

    We shell out rather than binding libpipewire because the surface we need is
    tiny (list sinks, set default) and ``wpctl`` is stable, always present with
    PipeWire, and trivially debuggable by hand when something misbehaves.
    """

    def __init__(self, binary: str = WPCTL) -> None:
        self._binary = binary

    def available(self) -> bool:
        return shutil.which(self._binary) is not None or bool(self._binary)

    def _run(self, *args: str, timeout: float = 5.0) -> str | None:
        try:
            proc = subprocess.run(
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("wpctl %s failed: %s", " ".join(args), exc)
            return None
        if proc.returncode != 0:
            LOG.warning("wpctl %s exited %d: %s", " ".join(args), proc.returncode,
                        proc.stderr.strip())
            return None
        return proc.stdout

    def list_sinks(self) -> list[PwSink]:
        """Enumerate audio sinks, preferring structured data from pw-dump."""
        sinks = self._list_sinks_pwdump()
        if sinks:
            return sinks
        LOG.debug("pw-dump unavailable; falling back to scraping wpctl status")
        return self._list_sinks_wpctl()

    def _list_sinks_pwdump(self) -> list[PwSink]:
        """Read sinks from ``pw-dump`` JSON.

        Gives node.name, node.description and the factory (which tells us
        whether a Bluetooth link negotiated A2DP or fell back to HFP), none of
        which are available from wpctl's rendered output.
        """
        try:
            proc = subprocess.run([PW_DUMP], capture_output=True, text=True,
                                  timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.debug("pw-dump failed: %s", exc)
            return []
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        try:
            objects = json.loads(proc.stdout)
        except ValueError as exc:
            LOG.debug("pw-dump returned invalid JSON: %s", exc)
            return []

        # The default sink lives in a Metadata object, not on the node itself.
        default_name = ""
        for obj in objects:
            if obj.get("type") != "PipeWire:Interface:Metadata":
                continue
            for item in obj.get("metadata", []) or []:
                if item.get("key") == "default.audio.sink":
                    value = item.get("value")
                    if isinstance(value, dict):
                        default_name = value.get("name", "")
                    elif isinstance(value, str):
                        try:
                            default_name = json.loads(value).get("name", "")
                        except ValueError:
                            default_name = value

        sinks: list[PwSink] = []
        for obj in objects:
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("media.class") != "Audio/Sink":
                continue
            name = props.get("node.name", "")
            sinks.append(PwSink(
                id=int(obj.get("id", -1)),
                name=name,
                description=props.get("node.description", ""),
                api=props.get("device.api", ""),
                factory=props.get("factory.name", ""),
                is_default=bool(default_name) and name == default_name,
            ))
        return sinks

    def _list_sinks_wpctl(self) -> list[PwSink]:
        """Legacy fallback: scrape the Sinks block from ``wpctl status``.

        Only the description is recoverable this way, so substring matching on
        node names will not work. Kept for systems without pw-dump.
        """
        out = self._run("status")
        if not out:
            return []

        sinks: list[PwSink] = []
        in_audio = False
        in_sinks = False

        for line in out.splitlines():
            stripped = line.strip()

            if stripped.startswith("Audio"):
                in_audio = True
                continue
            if in_audio and (stripped.startswith("Video") or stripped.startswith("Settings")):
                break
            if not in_audio:
                continue

            if "Sinks:" in stripped:
                in_sinks = True
                continue
            # Any other section header ends the sinks block.
            if in_sinks and stripped.endswith(":") and "Sinks" not in stripped:
                in_sinks = False
                continue
            if not in_sinks:
                continue

            # Strip the box-drawing gutter wpctl draws.
            cleaned = stripped.lstrip("│├└─ ").strip()
            if not cleaned:
                continue
            match = _WPCTL_NODE_RE.match(cleaned)
            if not match:
                continue
            label = match.group("name").strip()
            sinks.append(
                PwSink(
                    id=int(match.group("id")),
                    name=label,
                    description=label,
                    is_default=bool(match.group("default")),
                )
            )

        return sinks

    def find_sink(self, needle: str) -> PwSink | None:
        """Find a sink by node name, description or API.

        Config uses short patterns like ``bluez_output`` or ``raop_sink``, which
        appear in node.name but never in the human description, so matching has
        to consider both. ``bluez5`` also works as an API-level match if you
        would rather not depend on the node naming scheme at all.
        """
        if not needle:
            return None
        for sink in self.list_sinks():
            if sink.matches(needle):
                return sink
        return None

    def set_default(self, sink: PwSink) -> bool:
        LOG.info("PipeWire default sink -> [%d] %s", sink.id, sink.name)
        if self._run("set-default", str(sink.id)) is None:
            return False
        # Force the node's OWN volume to unity gain every time it becomes
        # the default — see set_volume()'s docstring for why. Best-effort:
        # the route switch itself already succeeded even if this part fails.
        self.set_volume(sink.id, 1.0)
        return True

    def set_volume(self, sink_id: int, level: float) -> bool:
        """Force a PipeWire sink node's OWN volume to ``level`` (0.0-1.0).

        This build treats software mixers (MPD's, or mpv's for video) as the
        SINGLE point of volume control — every PipeWire sink node should
        just pass audio through at unity gain, never attenuating on its own.
        WirePlumber does not honor that by default: a freshly-created device
        node — most commonly a `bluez_output` the first time a Bluetooth
        device connects (each new connection gets a brand new node, not a
        reused one) — gets WirePlumber's own default node volume, well under
        100%, entirely independent of whatever MPD/mpv's mixer already says.
        That silently multiplies against the software volume: reported live
        as a car head unit sounding very quiet on first connect even with
        both MPD's volume and the car's own volume maxed — nothing in this
        codebase had ever touched the PipeWire NODE's volume before, only
        MPD's/mpv's software mixers (see AIRPLAY_SAFE_VOLUME).

        Called from set_default() so every route-switch path (music BT,
        video BT, AirPlay, either picker) gets this for free at exactly the
        moment the sink becomes active, with no need to remember it at every
        call site individually.
        """
        return self._run("set-volume", str(sink_id), f"{level:.2f}") is not None


class OutputRouter:
    """Owns route selection across the MPD and PipeWire layers."""

    def __init__(
        self,
        mpd: MpdCommander,
        routes: tuple[Route, ...],
        pw: PipeWireControl | None = None,
    ) -> None:
        self._mpd = mpd
        self._routes = routes
        self._pw = pw or PipeWireControl()
        self._current_id: str | None = None
        self._warned_outputs: set[str] = set()

    # -- introspection -----------------------------------------------------

    @property
    def routes(self) -> tuple[Route, ...]:
        return self._routes

    @property
    def pw(self) -> PipeWireControl:
        """Exposed for callers that need to enumerate sinks directly — the
        AirPlay picker, specifically, which lists EVERY discovered raop_sink
        on the LAN, not just the ones with a matching [[routes]] entry."""
        return self._pw

    def _mpd_output_index(self, name: str) -> int | None:
        for entry in self._mpd.outputs():
            if entry.get("outputname") == name:
                try:
                    return int(entry["outputid"])
                except (KeyError, ValueError):
                    return None
        # Warn once per name. This fires on every availability check — several
        # times a second while rendering — and a route that is legitimately
        # absent (the DAC has not arrived yet) would otherwise flood the log.
        if name not in self._warned_outputs:
            self._warned_outputs.add(name)
            LOG.warning("no MPD output named %r — check mpd.conf against config.toml", name)
        return None

    def detect_current(self) -> Route | None:
        """Work out which route is live by inspecting MPD and PipeWire.

        Called at startup so the UI reflects reality rather than assuming the
        first route. If several routes map to the same MPD output (Bluetooth
        and AirPlay both map to "Network"), the PipeWire default sink breaks
        the tie.
        """
        enabled = {
            entry.get("outputname")
            for entry in self._mpd.outputs()
            if entry.get("outputenabled") == "1"
        }
        if not enabled:
            self._current_id = None
            return None

        candidates = [r for r in self._routes if r.mpd_output in enabled]
        if not candidates:
            self._current_id = None
            return None

        if len(candidates) == 1:
            self._current_id = candidates[0].id
            return candidates[0]

        # Ambiguous: disambiguate on the PipeWire default sink.
        default = next((s for s in self._pw.list_sinks() if s.is_default), None)
        if default is not None:
            for route in candidates:
                if route.pw_sink and default.matches(route.pw_sink):
                    self._current_id = route.id
                    return route

        self._current_id = candidates[0].id
        return candidates[0]

    def current(self) -> Route | None:
        if self._current_id is None:
            return self.detect_current()
        return next((r for r in self._routes if r.id == self._current_id), None)

    def mark_current(self, route_id: str) -> None:
        """Tell the router a route became active by a means other than
        ``switch_to`` — specifically, video mode's Local route, which
        disables MPD's output directly and points mpv at the ALSA device
        itself rather than going through this class's own switching path
        (there's no PipeWire step to perform for it at all). Without this,
        the Stream Deck's video-route keys would show the wrong one
        highlighted after switching to Local from video mode.
        """
        self._current_id = route_id

    def is_available(self, route: Route) -> bool:
        """Can we actually switch to this route right now?

        A route needing a PipeWire sink is unavailable when no matching sink
        exists — e.g. AirPlay before any speaker has been discovered, or
        Bluetooth with no headphones connected. The UI greys these out and the
        cycle action skips them, which is much better than silently switching
        to a dead output and playing to nothing.
        """
        if not route.pw_sink:
            return self._mpd_output_index(route.mpd_output) is not None
        return self._pw.find_sink(route.pw_sink) is not None

    def available_routes(self) -> list[Route]:
        return [r for r in self._routes if self.is_available(r)]

    # -- switching ---------------------------------------------------------

    def switch_to(self, route: Route) -> bool:
        """Activate ``route``. Returns True on success.

        Order matters: set the PipeWire default sink BEFORE enabling the MPD
        output. If you enable MPD's PipeWire output first, it opens a stream
        against whatever the current default is and you get a beat of audio out
        of the wrong device.
        """
        LOG.info("switching output route -> %s", route.id)

        if route.pw_sink:
            sink = self._pw.find_sink(route.pw_sink)
            if sink is None:
                LOG.warning(
                    "route %r wants PipeWire sink matching %r but none is present",
                    route.id,
                    route.pw_sink,
                )
                return False
            if not sink.is_default and not self._pw.set_default(sink):
                return False

        target_index = self._mpd_output_index(route.mpd_output)
        if target_index is None:
            return False

        # Enable target first, then disable the others. Doing it in this order
        # avoids a window where zero outputs are enabled, which makes MPD stop.
        self._mpd.enable_output(target_index)
        for entry in self._mpd.outputs():
            try:
                index = int(entry["outputid"])
            except (KeyError, ValueError):
                continue
            if index != target_index and entry.get("outputenabled") == "1":
                self._mpd.disable_output(index)

        self._current_id = route.id
        if route.icon == "airplay":
            self._clamp_airplay_volume()
        return True

    def _clamp_airplay_volume(self) -> None:
        """Pull MPD's software volume down to a safe ceiling on AirPlay.
        See AIRPLAY_SAFE_VOLUME's comment for why this exists."""
        raw = self._mpd.status().get("volume")
        if raw is None or raw == "-1":
            return
        try:
            current = int(raw)
        except ValueError:
            return
        if current > AIRPLAY_SAFE_VOLUME:
            LOG.info("clamping volume %d%% -> %d%% for AirPlay", current, AIRPLAY_SAFE_VOLUME)
            self._mpd.set_volume(AIRPLAY_SAFE_VOLUME)

    def _sink_is_airplay(self, sink: PwSink) -> bool:
        """True if ``sink`` is (or looks like) an AirPlay/RAOP destination.

        Checked two ways: against every configured ``[[routes]]`` entry with
        ``icon == "airplay"`` (the normal case), OR generically against the
        ``raop_sink`` substring PipeWire's RAOP module always puts in
        node.name (covers an AirPlay picker selection with no matching
        ``[[routes]]`` entry at all — see ``switch_to_sink()``).
        """
        for route in self._routes:
            if route.icon == "airplay" and route.pw_sink and sink.matches(route.pw_sink):
                return True
        return sink.matches("raop_sink")

    def airplay_is_active(self) -> bool:
        """Is the PipeWire default sink an AirPlay destination RIGHT NOW?

        Checked against live PipeWire truth, not this router's cached
        ``_current_id`` — see ``enforce_volume_safety()`` for why that
        distinction is the whole point.
        """
        default = next((s for s in self._pw.list_sinks() if s.is_default), None)
        return default is not None and self._sink_is_airplay(default)

    def enforce_volume_safety(self) -> bool:
        """Actively guard against an AirPlay sink ever playing at full
        volume, regardless of how it became the PipeWire default.

        ``switch_to()`` / ``switch_to_sink()`` / ``reassert_current()`` all
        clamp MPD's volume — but only at the moment THIS process explicitly
        changes the route, or right before playback (re)starts. None of
        those cover the case that actually caused a real blast in the
        field: WirePlumber can silently repoint PipeWire's default sink to
        AirPlay entirely on its own — most commonly when a Bluetooth device
        drops out (powered off, walked out of range) and its bluez_output
        node disappears mid-song, and WirePlumber's own session-manager
        policy picks the next available sink as the new default with zero
        involvement from this codebase. MPD just keeps streaming to
        whatever is now default, at whatever software volume it already had
        (frequently 100% — a bit-perfect Local session, or a value simply
        never lowered because nothing here ever ran) — with no route
        switch, no reassert, nothing to trigger the existing clamps.

        Meant to be polled on a short timer (see ``OutputSafetyWatcher``),
        not just at explicit switch points, so "an AirPlay speaker never
        blasts at full volume" holds even when the reroute happens entirely
        outside this codebase's control, mid-playback. Returns whether
        AirPlay is (still) the active destination, so a caller that also
        owns a second mixer (mpv, in video mode) knows whether it needs to
        clamp that one too.
        """
        active = self.airplay_is_active()
        if active:
            self._clamp_airplay_volume()
        return active

    def switch_to_sink(self, sink: PwSink) -> bool:
        """Activate an arbitrary PipeWire sink directly, bypassing the
        [[routes]] table entirely.

        The AirPlay picker lists every ``raop_sink`` PipeWire currently sees,
        which can include speakers nobody has added a ``[[routes]]`` entry
        for — the whole point of a picker over a fixed 3-key list. Same
        ordering rule as ``switch_to()``: PipeWire default set before MPD's
        output is enabled, so nothing gets a beat of audio out of the wrong
        device.
        """
        LOG.info("switching output -> PipeWire sink [%d] %s", sink.id, sink.name)

        if not sink.is_default and not self._pw.set_default(sink):
            return False

        target_index = self._mpd_output_index("Network")
        if target_index is None:
            return False

        self._mpd.enable_output(target_index)
        for entry in self._mpd.outputs():
            try:
                index = int(entry["outputid"])
            except (KeyError, ValueError):
                continue
            if index != target_index and entry.get("outputenabled") == "1":
                self._mpd.disable_output(index)

        # If this sink happens to match a configured route, report that one —
        # otherwise there is nothing in self._routes to name it, so leave the
        # cache clear and let detect_current() fall back to PipeWire truth.
        matched = next((r for r in self._routes if r.pw_sink and sink.matches(r.pw_sink)), None)
        self._current_id = matched.id if matched else None
        # Every sink reachable through switch_to_sink() is an AirPlay pick —
        # this is the AirPlay picker's own switching path (see
        # streamdeck_daemon._on_airplay_entry_press); the same safety ceiling
        # applies whether or not the sink happens to also match a configured
        # [[routes]] entry.
        self._clamp_airplay_volume()
        return True

    def reassert_current(self) -> None:
        """Re-apply the selected route's PipeWire default sink.

        WirePlumber can silently reassign the default sink out from under
        us — most commonly, a Bluetooth device auto-connecting (or simply
        reconnecting on its own, since it is trusted) makes itself default
        the instant it appears, regardless of what was explicitly chosen on
        the Stream Deck. A PipeWire stream only picks up "default sink" at
        the moment it is CREATED, and MPD does not actually open one while
        paused/stopped — found live: switched the route to AirPlay, nothing
        was playing yet, a Bluetooth reconnect happened in between and
        quietly took over as default, and the next `mpc play` opened MPD's
        stream against the headset instead — the panel still said
        "AirPlay", the SoundTouch speaker never got a real RAOP session and
        never woke up, and audio played to Bluetooth instead with no error
        anywhere.

        Call this immediately before playback actually (re)starts — see
        actions.py's ``toggle_pause`` / ``video_play_pause`` — so what
        plays always matches what the panel claims is selected, regardless
        of what drifted underneath in the meantime.

        Also re-applies the AirPlay volume ceiling. ``_clamp_airplay_volume``
        was originally only called from ``switch_to``/``switch_to_sink``,
        which covers an explicit route change but misses a real case that
        still caused a 100%-volume blast: MPD persists its volume and
        enabled-output state across restarts, so if the daemon (re)starts
        with AirPlay already the active route — a crash, a reboot, an
        `mpd`/service restart — nothing ever "switches" to AirPlay, and the
        clamp that only lived in ``switch_to`` never ran. `reassert_current`
        already runs at exactly the moment that matters (right before
        playback actually starts), so it is the right central place for this
        too, rather than needing every call site to remember both calls.
        """
        route = self.current()
        if route is None:
            return
        if route.icon == "airplay":
            self._clamp_airplay_volume()
        if not route.pw_sink:
            return
        sink = self._pw.find_sink(route.pw_sink)
        if sink is None:
            return
        if not sink.is_default:
            LOG.info("re-asserting route %r's sink — it had drifted", route.id)
            self._pw.set_default(sink)  # also forces unity node volume, see set_volume()
        elif route.icon == "bluetooth":
            # A Bluetooth device that auto-connects/reconnects on its own
            # (trusted devices do) can make itself default WITHOUT ever
            # going through switch_to()/set_default() — see this method's
            # own docstring. That's exactly the path that skipped the
            # node-volume fix above: sink.is_default is already True, so the
            # branch that calls set_default() (and so set_volume()) never
            # runs. A fresh node still gets WirePlumber's own low default
            # volume regardless of how it became default, so force it here
            # too on every reassert, not just a drifted one.
            self._pw.set_volume(sink.id, 1.0)

    def cycle(self, step: int = 1) -> Route | None:
        """Advance to the next available route, skipping unavailable ones.

        Re-derives "current" from MPD/PipeWire truth (``detect_current()``)
        rather than trusting this instance's cached ``_current_id``. Each
        daemon (tourbox-player, streamdeck-player) runs its OWN OutputRouter
        in its own process — the TourBox's knob click cycles through THIS
        router's idea of "current", which can be stale if the route was last
        changed by the OTHER daemon (a Stream Deck route-key press, or an
        AirPlay/BT picker selection) and this process never heard about it.
        Cycling from a stale position picks the wrong next route — e.g.
        immediately switching right back to the route that is already
        playing, or skipping over the one actually meant to come next. This
        is only paid on an actual knob click, not on every render tick, so
        the extra wpctl/pw-dump round trip is cheap where it matters.
        """
        usable = self.available_routes()
        if not usable:
            LOG.warning("no output routes are currently available")
            return None

        current = self.detect_current()
        if current is None or current.id not in {r.id for r in usable}:
            target = usable[0]
        else:
            index = next(i for i, r in enumerate(usable) if r.id == current.id)
            target = usable[(index + step) % len(usable)]

        if target.id == (current.id if current else None):
            return current
        return target if self.switch_to(target) else current


class OutputSafetyWatcher:
    """Polls ``OutputRouter.enforce_volume_safety()`` on a short timer.

    Exists because the failure mode it guards against — WirePlumber
    silently repointing PipeWire's default sink to AirPlay when a Bluetooth
    device drops out mid-song — produces no MPD idle event and passes
    through no explicit route-switch call anywhere in this codebase. There
    is nothing to hang the check off of; the only way to catch it is to
    keep checking. Runs on its own daemon thread so it works whether or not
    a screen is attached, same rationale as ``ContinuousPlayback``.

    ``interval_seconds`` is the worst-case exposure window: a drift that
    happens right after one tick is caught by the next. Kept short — this
    is a safety net, not a UI refresh — and cheap: one ``pw-dump`` call per
    tick, which is what ``is_available()``/rendering already pay elsewhere.
    """

    def __init__(
        self,
        router: OutputRouter,
        interval_seconds: float = 1.5,
        on_tick: Callable[[bool], None] | None = None,
    ) -> None:
        self._router = router
        self._interval = interval_seconds
        self._on_tick = on_tick
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="output-safety",
        )
        self._thread.start()
        LOG.info("output safety watchdog started (every %.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                active = self._router.enforce_volume_safety()
                if self._on_tick is not None:
                    self._on_tick(active)
            except Exception:  # noqa: BLE001 - never kill the watchdog thread
                LOG.exception("output safety watchdog error")
