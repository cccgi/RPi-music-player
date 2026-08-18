"""Configuration loading for the RPi player daemons.

Reads ``etc/config.toml`` and ``etc/keymap.toml`` into plain dataclasses so the
rest of the codebase never touches raw dicts or worries about missing keys.

Python 3.11+ ships ``tomllib`` in the stdlib, which is what Trixie provides,
so there is no external TOML dependency.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

BASE_DIR = Path(os.environ.get("RPI_PLAYER_HOME", "/opt/rpi-player"))
CONFIG_PATH = Path(os.environ.get("RPI_PLAYER_CONFIG", BASE_DIR / "etc/config.toml"))
KEYMAP_PATH = Path(os.environ.get("RPI_PLAYER_KEYMAP", BASE_DIR / "etc/keymap.toml"))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MpdConfig:
    host: str = "/run/mpd/socket"
    port: int = 6600
    timeout: float = 10.0


@dataclass(frozen=True)
class IpcConfig:
    socket: str = "/run/rpi-player/bus.sock"
    enabled: bool = True


@dataclass(frozen=True)
class RotarySteps:
    volume: int = 3
    volume_fast: int = 10
    seek: int = 15
    seek_fast: int = 60
    fast_threshold: float = 8.0
    max_volume_jump: int = 25
    max_seek_jump: int = 120


@dataclass(frozen=True)
class TourBoxConfig:
    device: str = "/dev/tourbox"
    baud: int = 115200
    fallback_globs: tuple[str, ...] = ("/dev/ttyACM*",)
    reconnect_delay: float = 2.0
    long_press_seconds: float = 0.45
    rotary_coalesce_seconds: float = 0.06
    # Elite/Elite Plus need an unlock handshake before reporting. The NEO does
    # not, and its reply frame is a hazard (0x00 padding = Tall button), so
    # this defaults off.
    send_unlock: bool = False
    steps: RotarySteps = field(default_factory=RotarySteps)


@dataclass(frozen=True)
class Theme:
    bg: str = "#101014"
    fg: str = "#F2F2F7"
    muted: str = "#8E8E93"
    accent: str = "#0A84FF"
    active: str = "#30D158"
    warn: str = "#FF9F0A"
    font_regular: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_bold: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@dataclass(frozen=True)
class StreamDeckConfig:
    brightness: int = 60
    dim_after_seconds: int = 120
    dim_brightness: int = 15
    blank_after_seconds: int = 900
    # Never fully blank while music is playing. A dark panel mid-album
    # reads as a crashed device.
    keep_awake_while_playing: bool = True
    reconnect_delay: float = 3.0
    tick_seconds: float = 1.0
    theme: Theme = field(default_factory=Theme)


@dataclass(frozen=True)
class Route:
    """One entry in the output-routing cycle.

    ``mpd_output`` names an ``audio_output`` block in mpd.conf.
    ``pw_sink`` is a substring matched against PipeWire node names; empty means
    this route does not involve PipeWire at all.
    """

    id: str
    label: str
    icon: str = ""
    mpd_output: str = ""
    pw_sink: str = ""
    # Only meaningful for a Bluetooth route: the paired device's MAC. Lets a
    # press on an "unavailable" BT key actively reconnect (see player/bt.py)
    # instead of just reporting the obvious — the headset is almost always
    # paired already, just switched off or attached to something else.
    bt_mac: str = ""


@dataclass(frozen=True)
class DeleteConfig:
    """Settings for the destructive delete action.

    ``mode``:
        "queue"     remove from the play queue only; the file is untouched
        "trash"     move the file into ``trash_dir``; recoverable
        "permanent" unlink the file; NOT recoverable

    ``confirm``:
        "none"      act on the first press
        "two_step"  first press arms (key turns red), second within
                    ``confirm_seconds`` commits
        "long_press" hold ``confirm_seconds`` to commit
    """

    enabled: bool = True
    mode: str = "permanent"
    confirm: str = "none"
    confirm_seconds: float = 3.0
    trash_dir: str = "/home/rpi/Music-trash"
    # Every deletion is appended here with a timestamp. Costs nothing at press
    # time and is the only record you will have of what a stray tap removed.
    log_path: str = "/home/rpi/deleted-tracks.log"
    # Hard safety rail, NOT configurable by accident: refuse to touch anything
    # outside the music directory even if MPD reports a strange URI.
    music_dir: str = "/home/rpi/Music"


@dataclass(frozen=True)
class PlaybackConfig:
    """Headless continuous-playback behaviour."""

    # Load the next library folder when the queue runs dry.
    auto_advance_folders: bool = True
    # Force MPD's consume and single modes OFF. Both silently break headless
    # playback: consume drains the queue (playback stops early AND 'previous'
    # has nothing to go back to), single stops after every track.
    force_consume_off: bool = True
    # Seconds of overlap MPD cross-mixes between the end of one song and the
    # start of the next, applied via MPD's own `crossfade` command. Only
    # covers AUTOMATIC transitions (a song ending and the queue advancing on
    # its own) — MPD has no ability to crossfade a manual `next`/`previous`,
    # a long-standing, still-open MPD limitation (github.com/
    # MusicPlayerDaemon/MPD/discussions/1849), not something fixable from the
    # client side. 0 disables it (MPD's own default).
    crossfade_seconds: int = 10
    # The TourBox's Knob click toggles crossfade between this value and
    # crossfade_seconds above, rather than cycling output routes (its old
    # binding — see keymap.toml's [layers.track.press], shift+Knob click
    # still does that via cycle_output_back).
    crossfade_seconds_alt: int = 5
    # Manual skip gets a quick software fade-out/fade-in instead — see
    # actions.py's `_fade_and_advance`. This is a volume duck around an
    # instant track change, not a true overlap (MPD can only play one stream
    # at a time), but it smooths the jolt of a hard cut. Total time in
    # seconds for the ramp down (skip happens at the bottom, then it ramps
    # back up over the same duration).
    skip_fade_seconds: float = 1.2
    # Route to force on at daemon startup if MPD comes up with NO output
    # enabled at all. MPD persists enabled/disabled per output across
    # restarts in its state_file — found live: after an mpd.service restart
    # unrelated to this codebase, MPD came back up with Local, Network AND
    # HDMI all disabled (a stale/blank state_file), and nothing here ever
    # force-enables one, so the box sat there silently accepting `play`
    # commands and doing nothing — "audio won't even play", no error toast,
    # no log warning a driver would ever see. Must match a [[routes]] id.
    default_route_id: str = "local"


@dataclass(frozen=True)
class VideoConfig:
    """Video/karaoke (.skp) mode."""

    enabled: bool = True
    library_dir: str = "/home/rpi/Video"
    mpv_socket: str = "/run/rpi-player-video/mpv.sock"
    timeout: float = 5.0
    # Auto-advance to the next .skp when one finishes, mirroring
    # [playback].auto_advance_folders in Music mode — see
    # player/video_continuous.py. Wraps to the first entry after the last.
    auto_advance: bool = True
    # File extensions the library scan indexes, case-insensitive. Not just
    # .skp — a mixed library of .skp karaoke files alongside plain video is
    # expected. See video.DEFAULT_VIDEO_EXTENSIONS for the load-time
    # distinction (only .skp needs the subfile demux).
    extensions: tuple[str, ...] = (".skp", ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm")
    # Cross-process music/video mode flag — see player/mode.py.
    mode_file: str = "/run/rpi-player/mode"
    # Same ALSA device string as mpd.conf's Local output block. Used to hand
    # the DAC to mpv directly when video mode routes audio to Local — that
    # card is deliberately invisible to PipeWire (51-mpd-dac-ignore.conf), so
    # this is the only way anything but MPD itself can reach it.
    local_audio_device: str = "hw:CARD=S3,DEV=0"


@dataclass(frozen=True)
class PowerConfig:
    enabled: bool = False
    gpio_pin: int = 3
    hold_seconds: float = 2.0
    confirm_with_streamdeck: bool = True


@dataclass(frozen=True)
class TouchConfig:
    """rpi-touch profile only — the 7in DSI touchscreen overlay UI.

    See player/touch_daemon.py and player/touchui/ (layout.py, render.py).
    Absent/false on the original rpi-audio profile's config.toml.
    """

    enabled: bool = False
    # Explicit /dev/input/eventN path, or "" to auto-detect (see
    # touch_daemon.find_touch_device).
    device: str = ""
    # This Pi 5's DSI bridge driver does not honor the KMS rotate connector
    # property (confirmed live — see touchui/layout.py's docstring), so
    # rotation is done entirely in software: render.py rotates the finished
    # overlay canvas, touch_daemon.py rotates raw touch coordinates before
    # hit-testing.
    rotate_180: bool = True
    # Inside video-mpv.service's OWN RuntimeDirectory (rpi-player-video),
    # deliberately not the shared /run/rpi-player dir — see
    # touch-ui-player.service's comment on the multi-owner RuntimeDirectory
    # teardown race this project already hit once.
    overlay_path: str = "/run/rpi-player-video/overlay.bgra"
    # mpv's overlay-add ids are NOT arbitrary — they index a small fixed-size
    # internal slot array (found live: id 90210, picked purely to "obviously
    # not collide with anything", was rejected outright with "overlay-add:
    # invalid id 90210"). This project only ever needs exactly one overlay,
    # so 0 is deliberately boring and always in range.
    overlay_id: int = 0
    tick_seconds: float = 1.0
    # A muted, looping, near-zero-size black clip touch_daemon loads into
    # mpv at startup, purely to force mpv to actually claim the DSI panel's
    # DRM output. Found live on real hardware: mpv's --idle=yes with NOTHING
    # ever loaded does not perform a DRM modeset at all on this Pi/driver
    # combination — the text console keeps the screen indefinitely, and
    # overlay-add has nothing to composite onto, both completely silently.
    # Loading and looping this clip is what actually gives touch_daemon a
    # video plane to draw the wireframe on top of during music mode; real
    # video/karaoke playback (video mode) simply replaces it via the normal
    # load_and_play path, same as switching between two real videos.
    idle_clip_path: str = "/opt/rpi-player/assets/idle-black.mp4"


@dataclass(frozen=True)
class Config:
    mpd: MpdConfig
    ipc: IpcConfig
    tourbox: TourBoxConfig
    streamdeck: StreamDeckConfig
    routes: tuple[Route, ...]
    power: PowerConfig
    delete: DeleteConfig
    playback: PlaybackConfig
    video: VideoConfig
    touch: TouchConfig
    log_level: str = "INFO"

    def route_by_id(self, route_id: str) -> Route | None:
        for route in self.routes:
            if route.id == route_id:
                return route
        return None


# ---------------------------------------------------------------------------
# Keymap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ButtonSpec:
    """A physical button and its byte code.

    Deliberately carries NO actions. Byte codes are hardware facts that never
    change; actions depend on the active layer. Keeping them apart means adding
    a layer cannot corrupt a verified byte map.
    """

    name: str
    byte: int

    @property
    def release_byte(self) -> int:
        """Release is the press byte with the high bit set."""
        return self.byte | 0x80


@dataclass(frozen=True)
class Layer:
    """One set of bindings, selected by the layer-toggle button.

    ``press`` / ``shift`` / ``double`` / ``long`` map control NAME -> action.
    A control absent from a layer falls back to the base layer, so an alternate
    layer only has to declare what it actually changes.
    """

    name: str
    label: str = ""
    press: dict[str, str] = field(default_factory=dict)
    shift: dict[str, str] = field(default_factory=dict)
    double: dict[str, str] = field(default_factory=dict)
    long: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RotarySpec:
    name: str
    cw: int
    ccw: int
    action_cw: str = "noop"
    action_ccw: str = "noop"
    shift_cw: str = ""
    shift_ccw: str = ""


@dataclass(frozen=True)
class Keymap:
    modifier: str
    buttons: dict[str, ButtonSpec]
    rotaries: dict[str, RotarySpec]

    # Reverse lookup tables, built once at load time.
    press_index: dict[int, ButtonSpec]
    release_index: dict[int, ButtonSpec]
    rotary_index: dict[int, tuple[RotarySpec, int]]  # byte -> (spec, direction)

    layers: tuple[Layer, ...] = ()
    # Control name that cycles layers. Its own bindings are never dispatched.
    layer_toggle: str = ""
    double_click_seconds: float = 0.35

    def modifier_spec(self) -> ButtonSpec | None:
        return self.buttons.get(self.modifier)

    def layer(self, index: int) -> Layer:
        if not self.layers:
            return Layer(name="default")
        return self.layers[index % len(self.layers)]

    def resolve(self, index: int, kind: str, control: str) -> str:
        """Action for ``control`` in layer ``index``, falling back to layer 0.

        The fallback is what lets an alternate layer declare only the handful
        of controls it changes instead of restating the whole map.
        """
        table = getattr(self.layer(index), kind, {}) or {}
        if control in table:
            return table[control]
        if index % max(len(self.layers), 1) != 0 and self.layers:
            return (getattr(self.layers[0], kind, {}) or {}).get(control, "")
        return ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        LOG.error("config file not found: %s", path)
        raise
    except tomllib.TOMLDecodeError as exc:
        LOG.error("malformed TOML in %s: %s", path, exc)
        raise


def load_config(path: Path | None = None) -> Config:
    raw = _read_toml(path or CONFIG_PATH)

    mpd_raw = raw.get("mpd", {})
    mpd = MpdConfig(
        host=mpd_raw.get("host", "/run/mpd/socket"),
        port=int(mpd_raw.get("port", 6600)),
        timeout=float(mpd_raw.get("timeout", 10.0)),
    )

    ipc_raw = raw.get("ipc", {})
    ipc = IpcConfig(
        socket=ipc_raw.get("socket", "/run/rpi-player/bus.sock"),
        enabled=bool(ipc_raw.get("enabled", True)),
    )

    tb_raw = raw.get("tourbox", {})
    steps_raw = tb_raw.get("steps", {})
    tourbox = TourBoxConfig(
        device=tb_raw.get("device", "/dev/tourbox"),
        baud=int(tb_raw.get("baud", 115200)),
        fallback_globs=tuple(tb_raw.get("fallback_globs", ["/dev/ttyACM*"])),
        reconnect_delay=float(tb_raw.get("reconnect_delay", 2.0)),
        long_press_seconds=float(tb_raw.get("long_press_seconds", 0.45)),
        rotary_coalesce_seconds=float(tb_raw.get("rotary_coalesce_seconds", 0.06)),
        send_unlock=bool(tb_raw.get("send_unlock", False)),
        steps=RotarySteps(
            volume=int(steps_raw.get("volume", 3)),
            volume_fast=int(steps_raw.get("volume_fast", 10)),
            seek=int(steps_raw.get("seek", 15)),
            seek_fast=int(steps_raw.get("seek_fast", 60)),
            fast_threshold=float(steps_raw.get("fast_threshold", 8.0)),
            max_volume_jump=int(steps_raw.get("max_volume_jump", 25)),
            max_seek_jump=int(steps_raw.get("max_seek_jump", 120)),
        ),
    )

    sd_raw = raw.get("streamdeck", {})
    theme_raw = sd_raw.get("theme", {})
    streamdeck = StreamDeckConfig(
        brightness=int(sd_raw.get("brightness", 60)),
        dim_after_seconds=int(sd_raw.get("dim_after_seconds", 120)),
        dim_brightness=int(sd_raw.get("dim_brightness", 15)),
        blank_after_seconds=int(sd_raw.get("blank_after_seconds", 900)),
        keep_awake_while_playing=bool(sd_raw.get("keep_awake_while_playing", True)),
        reconnect_delay=float(sd_raw.get("reconnect_delay", 3.0)),
        tick_seconds=float(sd_raw.get("tick_seconds", 1.0)),
        theme=Theme(
            bg=theme_raw.get("bg", "#101014"),
            fg=theme_raw.get("fg", "#F2F2F7"),
            muted=theme_raw.get("muted", "#8E8E93"),
            accent=theme_raw.get("accent", "#0A84FF"),
            active=theme_raw.get("active", "#30D158"),
            warn=theme_raw.get("warn", "#FF9F0A"),
            font_regular=theme_raw.get(
                "font_regular", "/opt/rpi-player/assets/fonts/DejaVuSans.ttf"
            ),
            font_bold=theme_raw.get(
                "font_bold", "/opt/rpi-player/assets/fonts/DejaVuSans-Bold.ttf"
            ),
        ),
    )

    routes = tuple(
        Route(
            id=item["id"],
            label=item.get("label", item["id"]),
            icon=item.get("icon", ""),
            mpd_output=item.get("mpd_output", ""),
            pw_sink=item.get("pw_sink", ""),
            bt_mac=item.get("bt_mac", ""),
        )
        for item in raw.get("routes", [])
    )
    if not routes:
        LOG.warning("no [[routes]] configured; output switching disabled")

    pb_raw = raw.get("playback", {})
    playback = PlaybackConfig(
        auto_advance_folders=bool(pb_raw.get("auto_advance_folders", True)),
        force_consume_off=bool(pb_raw.get("force_consume_off", True)),
        crossfade_seconds=int(pb_raw.get("crossfade_seconds", 10)),
        crossfade_seconds_alt=int(pb_raw.get("crossfade_seconds_alt", 5)),
        skip_fade_seconds=float(pb_raw.get("skip_fade_seconds", 1.2)),
        default_route_id=str(pb_raw.get("default_route_id", "local")),
    )

    del_raw = raw.get("delete", {})
    delete = DeleteConfig(
        enabled=bool(del_raw.get("enabled", True)),
        mode=str(del_raw.get("mode", "permanent")).lower(),
        confirm=str(del_raw.get("confirm", "none")).lower(),
        confirm_seconds=float(del_raw.get("confirm_seconds", 3.0)),
        trash_dir=del_raw.get("trash_dir", "/home/rpi/Music-trash"),
        log_path=del_raw.get("log_path", "/home/rpi/deleted-tracks.log"),
        music_dir=del_raw.get("music_dir", "/home/rpi/Music"),
    )
    if delete.mode not in ("queue", "trash", "permanent"):
        LOG.warning("unknown delete.mode %r; falling back to 'queue' (safe)", delete.mode)
        delete = replace(delete, mode="queue")

    power_raw = raw.get("power", {})
    power = PowerConfig(
        enabled=bool(power_raw.get("enabled", False)),
        gpio_pin=int(power_raw.get("gpio_pin", 3)),
        hold_seconds=float(power_raw.get("hold_seconds", 2.0)),
        confirm_with_streamdeck=bool(power_raw.get("confirm_with_streamdeck", True)),
    )

    video_raw = raw.get("video", {})
    video = VideoConfig(
        enabled=bool(video_raw.get("enabled", True)),
        library_dir=video_raw.get("library_dir", "/home/rpi/Video"),
        mpv_socket=video_raw.get("mpv_socket", "/run/rpi-player-video/mpv.sock"),
        timeout=float(video_raw.get("timeout", 5.0)),
        auto_advance=bool(video_raw.get("auto_advance", True)),
        extensions=tuple(
            e if e.startswith(".") else f".{e}"
            for e in video_raw.get(
                "extensions",
                [".skp", ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm"],
            )
        ),
        mode_file=video_raw.get("mode_file", "/run/rpi-player/mode"),
        local_audio_device=video_raw.get("local_audio_device", "hw:CARD=S3,DEV=0"),
    )

    touch_raw = raw.get("touch", {})
    touch = TouchConfig(
        enabled=bool(touch_raw.get("enabled", False)),
        device=touch_raw.get("device", ""),
        rotate_180=bool(touch_raw.get("rotate_180", True)),
        overlay_path=touch_raw.get("overlay_path", "/run/rpi-player-video/overlay.bgra"),
        overlay_id=int(touch_raw.get("overlay_id", 0)),
        tick_seconds=float(touch_raw.get("tick_seconds", 1.0)),
        idle_clip_path=touch_raw.get("idle_clip_path", "/opt/rpi-player/assets/idle-black.mp4"),
    )

    return Config(
        mpd=mpd,
        ipc=ipc,
        tourbox=tourbox,
        streamdeck=streamdeck,
        routes=routes,
        power=power,
        delete=delete,
        playback=playback,
        video=video,
        touch=touch,
        log_level=raw.get("log", {}).get("level", "INFO"),
    )


def load_keymap(path: Path | None = None) -> Keymap:
    raw = _read_toml(path or KEYMAP_PATH)

    # Buttons carry ONLY their byte. Actions live in layers.
    buttons: dict[str, ButtonSpec] = {}
    for name, item in raw.get("buttons", {}).items():
        buttons[name] = ButtonSpec(name=name, byte=int(item["byte"]))

    rotaries: dict[str, RotarySpec] = {}
    for name, item in raw.get("rotary", {}).items():
        rotaries[name] = RotarySpec(
            name=name,
            cw=int(item["cw"]),
            ccw=int(item["ccw"]),
            action_cw=item.get("action_cw", "noop"),
            action_ccw=item.get("action_ccw", "noop"),
            shift_cw=item.get("shift_cw", ""),
            shift_ccw=item.get("shift_ccw", ""),
        )

    # Layers, in declared order. The first is the base layer and is what every
    # other layer falls back to for controls it does not override.
    layers: list[Layer] = []
    raw_layers = raw.get("layers", {})
    order = raw.get("layer_order") or list(raw_layers)
    for name in order:
        item = raw_layers.get(name, {})
        layers.append(Layer(
            name=name,
            label=item.get("label", name),
            press=dict(item.get("press", {})),
            shift=dict(item.get("shift", {})),
            double=dict(item.get("double", {})),
            long=dict(item.get("long", {})),
        ))
    if not layers:
        LOG.warning("keymap declares no [layers]; every control will be inert")
        layers = [Layer(name="default")]

    press_index: dict[int, ButtonSpec] = {}
    release_index: dict[int, ButtonSpec] = {}
    for spec in buttons.values():
        if spec.byte in press_index:
            LOG.warning(
                "duplicate press byte 0x%02X: %s and %s — run tourbox-capture",
                spec.byte, press_index[spec.byte].name, spec.name,
            )
        press_index[spec.byte] = spec
        release_index[spec.release_byte] = spec

    rotary_index: dict[int, tuple[RotarySpec, int]] = {}
    for spec in rotaries.values():
        rotary_index[spec.cw] = (spec, +1)
        rotary_index[spec.ccw] = (spec, -1)

    for byte in set(rotary_index) & set(press_index):
        LOG.warning(
            "byte 0x%02X is claimed by both a rotary and button %r — "
            "your keymap is almost certainly wrong; run tourbox-capture",
            byte, press_index[byte].name,
        )

    # Catch bindings that name a control which does not exist. Silent typos
    # here produce a dead button with no error at runtime.
    known = set(buttons) | set(rotaries)
    for layer in layers:
        for kind in ("press", "shift", "double", "long"):
            for control in getattr(layer, kind):
                if control not in known:
                    LOG.warning(
                        "layer %r binds unknown control %r (%s)",
                        layer.name, control, kind,
                    )

    toggle = raw.get("layer_toggle", "")
    if toggle and toggle not in buttons:
        LOG.warning("layer_toggle %r is not a known button", toggle)

    return Keymap(
        modifier=raw.get("modifier", "side"),
        buttons=buttons,
        rotaries=rotaries,
        press_index=press_index,
        release_index=release_index,
        rotary_index=rotary_index,
        layers=tuple(layers),
        layer_toggle=toggle,
        double_click_seconds=float(raw.get("double_click_seconds", 0.35)),
    )


def setup_logging(level: str = "INFO") -> None:
    """Log to stdout; systemd captures it into the journal with correct levels."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(levelname)-8s %(name)-22s %(message)s",
    )
