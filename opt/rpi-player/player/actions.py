"""Central action dispatch table.

Both input daemons resolve a symbolic action name (``"volume_up"``) to a
callable here. Keeping one table means the TourBox keymap and the Stream Deck
layout cannot drift apart, and adding a verb is a one-line change that both
surfaces pick up.

Actions receive an :class:`ActionContext` and an integer ``magnitude`` (rotary
detent count, or 1 for a button). They return an optional short string for the
on-screen toast.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .mpdbus import MpdCommander
from .outputs import AIRPLAY_SAFE_VOLUME, OutputRouter
from . import mode as _mode
from .skp import SkpParseError, track_title
from .video import VideoCommander, VideoError, VideoLibrary, load_and_play

LOG = logging.getLogger(__name__)


@dataclass
class DeleteSettings:
    """Mirror of config.DeleteConfig, passed into ActionContext."""

    enabled: bool = True
    mode: str = "queue"
    trash_dir: str = ""
    log_path: str = ""
    music_dir: str = ""


@dataclass
class ActionContext:
    mpd: MpdCommander
    router: OutputRouter
    bus: Any                      # BusServer | NullBus
    volume_step: int = 3
    seek_step: int = 15
    # Safety clamps on a single coalesced rotary burst. Without these, a hard
    # flick of the knob (which can register 40+ detents) multiplies by the
    # fast-spin step and slams the volume to 0 or 100 — startling on
    # headphones, and the main reason accelerated encoders feel uncontrollable.
    max_volume_jump: int = 25
    max_seek_jump: int = 120
    # Coarse scrub, used by the *_fast seek actions. On this build it is
    # bound to shift+button because the TourBox rotaries do not report.
    seek_step_fast: int = 60
    # Total ramp-down + ramp-up time for the software volume duck around a
    # manual skip — see _skip_with_fade. 0 disables it (instant cut).
    skip_fade_seconds: float = 1.2
    # The two values toggle_crossfade cycles between (TourBox Knob click).
    crossfade_seconds: int = 10
    crossfade_seconds_alt: int = 5
    delete: Any = None            # DeleteSettings | None
    video: Any = None             # VideoCommander | None
    video_library: Any = None     # VideoLibrary | None
    mode_file: str = "/run/rpi-player/mode"
    video_local_device: str = "hw:CARD=S3,DEV=0"
    # Mutated at runtime by video_route_local/exit_video_mode — tracks
    # whether WE were the one who disabled MPD's Local output, so exiting
    # video mode restores it only when that's actually true. Same pattern as
    # volume_step/seek_step above, which the TourBox daemon already mutates
    # per-event.
    video_local_was_enabled: bool = False
    # What WE last intentionally set mpv's softvol to (video_volume_up/down,
    # video_toggle_mute, or the AirPlay safety clamp itself) — lets
    # _clamp_video_volume_if_airplay tell "user deliberately raised it past
    # the ceiling" apart from "mpv's volume reverted on its own" instead of
    # blindly re-clamping on every call. None until the first such action.
    video_last_set_volume: int | None = None
    # Cached, not re-queried every render — see query_wifi_enabled()/
    # toggle_wifi. Only this process's Stream Deck daemon ever mutates it
    # (the Wi-Fi toggle key lives on the Video page only), so a single
    # cached bool per ActionContext is enough; no cross-process sync needed.
    wifi_enabled: bool = True
    # Whether Shuffle is currently in its "All" state (queue replaced with
    # the ENTIRE current-source library, not just the current folder) — see
    # cycle_shuffle_mode. MPD itself only has one boolean (`random`), no
    # concept of "shuffle scope", so this distinguishes "Folder" from "All"
    # the same way video_local_was_enabled tracks state MPD has no field
    # for. Mutated at runtime by cycle_shuffle_mode only.
    shuffle_all_active: bool = False


ActionFn = Callable[[ActionContext, int], str | None]

_REGISTRY: dict[str, ActionFn] = {}


def action(name: str) -> Callable[[ActionFn], ActionFn]:
    def decorator(fn: ActionFn) -> ActionFn:
        _REGISTRY[name] = fn
        return fn

    return decorator


def dispatch(name: str, ctx: ActionContext, magnitude: int = 1) -> str | None:
    """Run an action by name. Unknown names are logged, never raised.

    A typo in keymap.toml should degrade one button, not crash the daemon on a
    device with no screen attached.
    """
    fn = _REGISTRY.get(name)
    if fn is None:
        if name and name != "noop":
            LOG.warning("unknown action %r (check keymap.toml)", name)
        return None
    try:
        toast = fn(ctx, magnitude)
    except Exception:  # noqa: BLE001 - a bad action must not kill the daemon
        LOG.exception("action %r raised", name)
        return None

    if toast:
        ctx.bus.publish("toast", text=toast)
    return toast


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@action("noop")
def _noop(ctx: ActionContext, magnitude: int) -> str | None:
    return None


@action("toggle_pause")
def _toggle_pause(ctx: ActionContext, magnitude: int) -> str | None:
    # About to go from paused/stopped -> playing: this is the moment MPD
    # will actually open a PipeWire stream and lock onto whatever the
    # default sink is right now. Make sure that is still the route the
    # panel claims is selected — see OutputRouter.reassert_current().
    if ctx.router is not None and ctx.mpd.status().get("state") != "play":
        ctx.router.reassert_current()
    ctx.mpd.toggle_pause()
    return "Play" if ctx.mpd.status().get("state") == "play" else "Pause"


@action("stop")
def _stop(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.stop()
    return "Stop"


@action("next_track")
def _next_track(ctx: ActionContext, magnitude: int) -> str | None:
    _skip_with_fade(ctx, +1, max(1, magnitude))
    return None


@action("prev_track")
def _prev_track(ctx: ActionContext, magnitude: int) -> str | None:
    """Go to the PREVIOUS track, not restart the current one.

    Many players restart the current track if you are more than a few seconds
    in. That is deliberately NOT done here — on a handheld with no screen,
    "previous" that sometimes restarts and sometimes goes back is impossible to
    predict, and there is a separate control (dial click) for restarting.

    If this ever appears to just restart the track, the cause is almost
    certainly MPD's `consume` mode: it removes played tracks from the queue, so
    there is no previous entry to move to. ContinuousPlayback forces it off.
    """
    _skip_with_fade(ctx, -1, max(1, magnitude))
    return None


# ---------------------------------------------------------------------------
# Skip fade — a quick volume duck around a manual next/previous.
#
# MPD's own `crossfade` (see PlaybackConfig.crossfade_seconds, applied at
# daemon startup) only ever overlaps AUTOMATIC transitions — a song ending
# and the queue moving on by itself. MPD cannot crossfade a manual skip at
# all; that is a genuine, still-open MPD limitation (playing two songs at
# once needs a second decoder MPD's protocol has no concept of), not
# something this codebase can work around by calling MPD differently. What
# we CAN do is duck the volume down, change tracks at the bottom, and ramp
# back up — not a real overlap, but it takes the jolt out of a hard instant
# cut, asked for specifically after "audio crossfade" turned out to mean
# "the change shouldn't feel abrupt" as much as it meant true mixing.
#
# Runs on a background thread so a TourBox/Stream Deck skip press never
# blocks the input loop for the ~1-2s a fade takes. A generation counter
# lets a second skip landing mid-fade cancel the first fade's ramp-up
# cleanly instead of the two fights over the volume level — the newer
# press's ramp-down just continues from wherever the first one left off,
# and only the LAST press's ramp-up actually restores full volume.
# ---------------------------------------------------------------------------

_FADE_STEPS = 12
_fade_lock = threading.Lock()
_fade_generation = 0
_fade_baseline_volume: int | None = None


def _skip_with_fade(ctx: ActionContext, direction: int, count: int) -> None:
    global _fade_generation, _fade_baseline_volume

    duration = ctx.skip_fade_seconds
    if duration <= 0:
        for _ in range(count):
            ctx.mpd.next_track() if direction > 0 else ctx.mpd.prev_track()
        return

    with _fade_lock:
        _fade_generation += 1
        my_gen = _fade_generation
        if _fade_baseline_volume is None:
            raw = ctx.mpd.status().get("volume")
            try:
                baseline = int(raw) if raw not in (None, "-1") else None
            except ValueError:
                baseline = None
            _fade_baseline_volume = baseline
        baseline = _fade_baseline_volume

    if not baseline:  # no mixer, or already silent — nothing to duck
        for _ in range(count):
            ctx.mpd.next_track() if direction > 0 else ctx.mpd.prev_track()
        return

    threading.Thread(
        target=_run_skip_fade, args=(ctx, direction, count, baseline, my_gen, duration),
        daemon=True, name="skip-fade",
    ).start()


def _run_skip_fade(ctx: ActionContext, direction: int, count: int,
                    baseline: int, my_gen: int, duration: float) -> None:
    """Ramp down, change track(s), ramp back up.

    The one thing that must NEVER happen is losing a skip: if a second press
    lands mid-ramp and supersedes this generation, this thread cuts its own
    ramp-DOWN short (skipping straight to the track change) rather than
    bailing out before calling next/prev — otherwise a quick double-tap would
    silently eat the first press. Only the ramp-UP is skippable outright: an
    older, superseded generation has no business restoring the volume once a
    newer skip is already under way, since that newer skip's own ramp-up
    will do it. This is what keeps N rapid presses landing as N real skips,
    each in order, with only ever one final volume restore at the end.
    """
    global _fade_generation, _fade_baseline_volume
    # `duration` is the TOTAL time (see PlaybackConfig.skip_fade_seconds) —
    # split evenly between the ramp-down and ramp-up halves.
    step_sleep = (duration / 2) / _FADE_STEPS

    for i in range(_FADE_STEPS - 1, -1, -1):
        if _fade_generation != my_gen:
            break  # superseded — stop animating, but still make the skip happen below
        ctx.mpd.set_volume(int(baseline * i / _FADE_STEPS))
        time.sleep(step_sleep)

    for _ in range(count):
        ctx.mpd.next_track() if direction > 0 else ctx.mpd.prev_track()

    # Stops one step short of `baseline` — the explicit set below is the
    # sole "reached full volume" event, so the log/UI only ever sees the
    # exact baseline value asserted once, not once from this loop's last
    # step and again from the explicit set right after it.
    for i in range(1, _FADE_STEPS):
        if _fade_generation != my_gen:
            return  # a newer skip is now in charge of ramping back up
        ctx.mpd.set_volume(int(baseline * i / _FADE_STEPS))
        time.sleep(step_sleep)

    with _fade_lock:
        if _fade_generation == my_gen:
            ctx.mpd.set_volume(baseline)
            _fade_baseline_volume = None


def _seek_delta(ctx: ActionContext, magnitude: int) -> int:
    return min(ctx.seek_step * max(1, magnitude), ctx.max_seek_jump)


@action("seek_forward")
def _seek_forward(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.seek_relative(_seek_delta(ctx, magnitude))
    return None


@action("seek_back")
def _seek_back(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.seek_relative(-_seek_delta(ctx, magnitude))
    return None


def _seek_delta_fast(ctx: ActionContext, magnitude: int) -> int:
    return min(ctx.seek_step_fast * max(1, magnitude), ctx.max_seek_jump)


@action("seek_forward_fast")
def _seek_forward_fast(ctx: ActionContext, magnitude: int) -> str | None:
    """Coarse scrub forward.

    Exists because the TourBox NEO's rotaries emit nothing on this unit, so the
    "two scrub speeds" the rotaries would have provided are delivered by
    plain vs shifted button presses instead.
    """
    ctx.mpd.seek_relative(_seek_delta_fast(ctx, magnitude))
    return f"+{ctx.seek_step_fast}s"


@action("seek_back_fast")
def _seek_back_fast(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.seek_relative(-_seek_delta_fast(ctx, magnitude))
    return f"-{ctx.seek_step_fast}s"


@action("seek_to_start")
def _seek_to_start(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.seek_to_start()
    return "Restart"


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def _volume_delta(ctx: ActionContext, magnitude: int) -> int:
    return min(ctx.volume_step * max(1, magnitude), ctx.max_volume_jump)


@action("volume_up")
def _volume_up(ctx: ActionContext, magnitude: int) -> str | None:
    new = ctx.mpd.change_volume(_volume_delta(ctx, magnitude))
    return f"Vol {new}%" if new is not None else "No mixer"


@action("volume_down")
def _volume_down(ctx: ActionContext, magnitude: int) -> str | None:
    new = ctx.mpd.change_volume(-_volume_delta(ctx, magnitude))
    return f"Vol {new}%" if new is not None else "No mixer"


# Mute is implemented in software because MPD has no mute concept: we stash the
# pre-mute level and restore it. Module-level state is fine — a single daemon
# owns the controller.
_pre_mute_volume: int | None = None


@action("toggle_mute")
def _toggle_mute(ctx: ActionContext, magnitude: int) -> str | None:
    global _pre_mute_volume
    status = ctx.mpd.status()
    raw = status.get("volume")
    if raw is None or raw == "-1":
        return "No mixer"
    current = int(raw)

    if current > 0:
        _pre_mute_volume = current
        ctx.mpd.set_volume(0)
        return "Muted"

    ctx.mpd.set_volume(_pre_mute_volume or 30)
    _pre_mute_volume = None
    return "Unmuted"


# ---------------------------------------------------------------------------
# Album navigation
# ---------------------------------------------------------------------------


def _jump_album(ctx: ActionContext, direction: int) -> str | None:
    """Move to the first track of the adjacent album in the queue.

    MPD has no native "next album", so we walk the queue comparing the Album
    tag. Falls back to plain next/prev when tags are missing.
    """
    playlist = ctx.mpd.playlist_info()
    status = ctx.mpd.status()
    if not playlist or "song" not in status:
        return None

    try:
        position = int(status["song"])
    except (TypeError, ValueError):
        return None

    current_album = playlist[position].get("album", "")
    index = position

    # Walk until the album tag changes.
    while 0 <= index < len(playlist):
        index += direction
        if not (0 <= index < len(playlist)):
            break
        if playlist[index].get("album", "") != current_album:
            break
    else:
        return None

    if not (0 <= index < len(playlist)):
        return None

    # Going backwards lands on the LAST track of the previous album; rewind to
    # its first track so "previous album" starts at the beginning.
    if direction < 0:
        target_album = playlist[index].get("album", "")
        while index > 0 and playlist[index - 1].get("album", "") == target_album:
            index -= 1

    ctx.mpd.play_position(index)
    return playlist[index].get("album", "Album")


@action("next_album")
def _next_album(ctx: ActionContext, magnitude: int) -> str | None:
    return _jump_album(ctx, +1)


@action("prev_album")
def _prev_album(ctx: ActionContext, magnitude: int) -> str | None:
    return _jump_album(ctx, -1)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


@action("toggle_random")
def _toggle_random(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.toggle_random()
    return f"Shuffle {'on' if ctx.mpd.status().get('random') == '1' else 'off'}"


@action("toggle_repeat")
def _toggle_repeat(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.toggle_repeat()
    return f"Repeat {'on' if ctx.mpd.status().get('repeat') == '1' else 'off'}"


@action("toggle_single")
def _toggle_single(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.toggle_single()
    return f"Single {'on' if ctx.mpd.status().get('single') == '1' else 'off'}"


@action("toggle_consume")
def _toggle_consume(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.toggle_consume()
    return f"Consume {'on' if ctx.mpd.status().get('consume') == '1' else 'off'}"


@action("cycle_repeat_mode")
def _cycle_repeat_mode(ctx: ActionContext, magnitude: int) -> str | None:
    """Replaces the old separate Repeat/Single buttons with one 3-state
    cycle: Off -> Folder -> Song -> Off. "Single" (repeat only the current
    track) was its own button before this — asked to be removed and folded
    into Repeat instead, since it's really just a stronger repeat mode, not
    an independent concept.

    "Folder" reuses MPD's plain repeat=1/single=0 as-is: this player's own
    browsing convention already replaces the queue with the CONTAINING
    FOLDER whenever a track is picked (see LibraryBrowser.enter() /
    play_uri_from_directory) or with the entire library when Shuffle's
    "All" mode is active (see cycle_shuffle_mode) — so "repeat whatever's
    in the queue" already means "repeat the folder" under normal use,
    with no new mechanism needed. "Song" is MPD's repeat=1/single=1 combo
    (loops just the current track forever). State is read live from MPD's
    own repeat/single flags, not stored separately, so this can never drift
    from what MPD is actually doing.
    """
    status = ctx.mpd.status()
    repeat = status.get("repeat") == "1"
    single = status.get("single") == "1"

    if not repeat:
        # Off (or the single-without-repeat state MPD allows but this app
        # never produces) -> Folder.
        ctx.mpd.set_repeat(True)
        ctx.mpd.set_single(False)
        return "Repeat: Folder"
    if not single:
        # Folder -> Song.
        ctx.mpd.set_single(True)
        return "Repeat: Song"
    # Song -> Off.
    ctx.mpd.set_repeat(False)
    ctx.mpd.set_single(False)
    return "Repeat: Off"


@action("cycle_shuffle_mode")
def _cycle_shuffle_mode(ctx: ActionContext, magnitude: int) -> str | None:
    """Replaces the old plain on/off Shuffle toggle with a 3-state cycle:
    Off -> Folder -> All -> Off.

    "Folder" just enables MPD's `random` flag on whatever's already queued
    (the current folder, per this player's normal browsing convention —
    see cycle_repeat_mode's docstring for the same reasoning). "All"
    replaces the queue with the ENTIRE current-source library (Internal or
    USB, whichever the storage-source toggle currently has active —
    music_storage_label() reads that live) and shuffles across all of it.

    MPD has no native concept of "shuffle scope" (just one boolean,
    `random`), so which of "Folder"/"All" is active is tracked on
    ``ctx.shuffle_all_active`` rather than derived from MPD state — same
    pattern as video_local_was_enabled tracking state MPD itself has no
    field for.
    """
    status = ctx.mpd.status()
    random_on = status.get("random") == "1"

    if not random_on:
        # Off -> Folder: enable random on the current (folder-scoped) queue.
        ctx.mpd.set_random(True)
        ctx.shuffle_all_active = False
        return "Shuffle: Folder"
    if not ctx.shuffle_all_active:
        # Folder -> All: load the entire current-source library, then shuffle.
        source = music_storage_label()
        ctx.mpd.replace_queue_with_library()
        ctx.mpd.set_random(True)
        ctx.shuffle_all_active = True
        return f"Shuffle: All ({source})"
    # All -> Off.
    ctx.mpd.set_random(False)
    ctx.shuffle_all_active = False
    return "Shuffle: Off"


# ---------------------------------------------------------------------------
# Crossfade
# ---------------------------------------------------------------------------


@action("toggle_crossfade")
def _toggle_crossfade(ctx: ActionContext, magnitude: int) -> str | None:
    """Toggle MPD's automatic-transition crossfade between two configured
    values (config.toml [playback] crossfade_seconds / crossfade_seconds_alt).

    Bound to the TourBox's Knob click, which used to cycle output routes —
    rebound here specifically, with shift+Knob click (cycle_output_back)
    left in place so route cycling is still one press away, just shifted.

    Reads MPD's own `xfade` status field rather than tracking state locally,
    so this stays correct even if crossfade was last changed some other way
    (a client other than this daemon, or a fresh `mpd.service` restart
    resetting it to 0) — whatever MPD reports right now is what gets
    toggled away from.
    """
    raw = ctx.mpd.status().get("xfade")
    try:
        current = int(raw) if raw is not None else 0
    except ValueError:
        current = 0
    new = ctx.crossfade_seconds_alt if current == ctx.crossfade_seconds else ctx.crossfade_seconds
    ctx.mpd.set_crossfade(new)
    return f"Crossfade {new}s"


# ---------------------------------------------------------------------------
# Curate — move the currently playing file into a "Curated" subfolder inside
# its own containing directory, one tap, without touching playback/queue
# state at all. Safe to do live: shutil.move on the SAME filesystem does not
# invalidate an already-open file descriptor on Linux, so whatever is
# currently playing keeps playing uninterrupted through the move.
# ---------------------------------------------------------------------------


def _try_remount_usb_rw() -> bool:
    """If the USB drive has been auto-remounted read-only by the exFAT
    kernel driver (errno 30 EROFS), call the wrapper script installed at
    /usr/local/sbin/usb-remount-rw (granted passwordless sudo via
    /etc/sudoers.d/50-rpi-usb-remount) to flip it back to rw.

    Returns True if the remount succeeded or wasn't needed, False on failure.
    Called as a recovery step before retrying a failed delete/curate.
    """
    try:
        result = subprocess.run(
            ["sudo", "/usr/local/sbin/usb-remount-rw"],
            timeout=5, capture_output=True,
        )
        if result.returncode == 0:
            LOG.warning("USB remounted rw after EROFS; retrying operation")
            return True
        LOG.error("usb-remount-rw failed (rc=%d): %s",
                  result.returncode, result.stderr.decode().strip())
        return False
    except Exception as exc:
        LOG.error("usb-remount-rw exception: %s", exc)
        return False


def _unique_target(dest_dir: Path, name: str) -> Path:
    """``dest_dir / name``, or a numeric-suffixed variant if that already
    exists — never overwrites, never raises on a collision."""
    target = dest_dir / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


@action("curate_current")
def _curate_current(ctx: ActionContext, magnitude: int) -> str | None:
    """Move MPD's currently playing file into a Curated/ subfolder inside
    its own containing directory, then trigger a targeted (not full-library)
    ``mpd update`` so the browser/library reflects the move.

    Deliberately does not stop playback, touch the queue, or wait for the
    database update — see the module comment above for why the move itself
    is safe to do live, and update_database's existing fire-and-forget style
    for why the ``update`` call doesn't need to be awaited either.
    """
    song = ctx.mpd.current_song()
    uri = song.get("file", "")
    if not uri:
        return "Nothing playing"

    music_dir = (getattr(ctx.delete, "music_dir", "") if ctx.delete else "") or "/home/rpi/Music"
    root = Path(music_dir)
    rel_dir = str(Path(uri).parent)
    if rel_dir in (".", "/"):
        rel_dir = ""

    src = root / uri
    if not src.is_file():
        return "File missing"

    dest_dir = (root / rel_dir / "Curated") if rel_dir else (root / "Curated")
    for attempt in (1, 2):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = _unique_target(dest_dir, src.name)
            shutil.move(str(src), str(target))
            break
        except OSError as exc:
            if exc.errno == 30 and attempt == 1:  # EROFS — try remounting rw
                if not _try_remount_usb_rw():
                    LOG.error("curate failed for %s: %s", src, exc)
                    return "Curate failed"
                time.sleep(0.3)
                continue
            LOG.error("curate failed for %s: %s", src, exc)
            return "Curate failed"

    update_dir = f"{rel_dir}/Curated" if rel_dir else "Curated"
    ctx.mpd._call("update", update_dir)
    LOG.info("curated: %s -> %s", src, target)
    return "Curated"


@action("video_curate_current")
def _video_curate_current(ctx: ActionContext, magnitude: int) -> str | None:
    """Video's equivalent of curate_current — moves the currently playing
    VideoEntry into a Curated/ subfolder inside its own directory, then
    a synchronous rescan (matching video_rescan's existing style, unlike
    music's fire-and-forget ``mpd update``) so the library reflects the
    move immediately. Clears the stale resume-position bookkeeping keyed
    by the OLD path so it doesn't linger pointing at nothing.
    """
    if ctx.video_library is None:
        return None
    entry = ctx.video_library.current
    if entry is None:
        return "Nothing playing"

    src = Path(entry.path)
    if not src.is_file():
        return "File missing"

    dest_dir = src.parent / "Curated"
    for attempt in (1, 2):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = _unique_target(dest_dir, src.name)
            shutil.move(str(src), str(target))
            break
        except OSError as exc:
            if exc.errno == 30 and attempt == 1:
                if not _try_remount_usb_rw():
                    LOG.error("video curate failed for %s: %s", src, exc)
                    return "Curate failed"
                time.sleep(0.3)
                continue
            LOG.error("video curate failed for %s: %s", src, exc)
            return "Curate failed"

    ctx.video_library.forget_position(str(src))
    ctx.video_library.rescan()
    LOG.info("curated video: %s -> %s", src, target)
    return "Curated"


def _curate_to_folder(ctx: ActionContext, folder_name: str) -> str | None:
    """Shared implementation for curate_to_favorites and any future
    named-folder curate variants.  Moves the current track into
    ``<containing_dir>/<folder_name>/`` rather than ``Curated/``."""
    song = ctx.mpd.current_song()
    uri = song.get("file", "")
    if not uri:
        return "Nothing playing"

    music_dir = (getattr(ctx.delete, "music_dir", "") if ctx.delete else "") or "/home/rpi/Music"
    root = Path(music_dir)
    rel_dir = str(Path(uri).parent)
    if rel_dir in (".", "/"):
        rel_dir = ""

    src = root / uri
    if not src.is_file():
        return "File missing"

    dest_dir = (root / rel_dir / folder_name) if rel_dir else (root / folder_name)
    for attempt in (1, 2):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = _unique_target(dest_dir, src.name)
            shutil.move(str(src), str(target))
            break
        except OSError as exc:
            if exc.errno == 30 and attempt == 1:
                if not _try_remount_usb_rw():
                    LOG.error("curate_to_folder(%s) failed for %s: %s", folder_name, src, exc)
                    return "Curate failed"
                time.sleep(0.3)
                continue
            LOG.error("curate_to_folder(%s) failed for %s: %s", folder_name, src, exc)
            return "Curate failed"

    update_dir = f"{rel_dir}/{folder_name}" if rel_dir else folder_name
    ctx.mpd._call("update", update_dir)
    LOG.info("curated to %s: %s -> %s", folder_name, src, target)
    return f"→ {folder_name}"


@action("curate_to_favorites")
def _curate_to_favorites(ctx: ActionContext, magnitude: int) -> str | None:
    """Move the current music track into a Favorites/ subfolder alongside it."""
    return _curate_to_folder(ctx, "Favorites")


@action("video_curate_to_favorites")
def _video_curate_to_favorites(ctx: ActionContext, magnitude: int) -> str | None:
    """Move the current video into a Favorites/ subfolder alongside it."""
    if ctx.video_library is None:
        return None
    entry = ctx.video_library.current
    if entry is None:
        return "Nothing playing"

    src = Path(entry.path)
    if not src.is_file():
        return "File missing"

    dest_dir = src.parent / "Favorites"
    for attempt in (1, 2):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = _unique_target(dest_dir, src.name)
            shutil.move(str(src), str(target))
            break
        except OSError as exc:
            if exc.errno == 30 and attempt == 1:
                if not _try_remount_usb_rw():
                    LOG.error("video curate_to_favorites failed for %s: %s", src, exc)
                    return "Curate failed"
                time.sleep(0.3)
                continue
            LOG.error("video curate_to_favorites failed for %s: %s", src, exc)
            return "Curate failed"

    ctx.video_library.forget_position(str(src))
    ctx.video_library.rescan()
    LOG.info("curated video to Favorites: %s -> %s", src, target)
    return "→ Favorites"


# ---------------------------------------------------------------------------
# Storage source toggle — internal microSD vs external USB drive.
#
# /home/rpi/Music and /home/rpi/Video are (or will shortly be, per the
# infrastructure set up separately/outside this codebase — no /etc/fstab,
# no SSH, no mount handling here) symlinks, each pointing at either the
# original "-internal" directory or a mounted USB drive's Audio/Video
# subfolder. MPD's music_directory config and config.toml's video.
# library_dir both stay fixed at the symlink path FOREVER — only the
# symlink TARGET ever changes. That makes this pure filesystem symlink
# manipulation: no new root/polkit permission (the symlinks live under
# /home/rpi, already owned by the `rpi` service user), no mpd.conf edit, no
# service restart.
# ---------------------------------------------------------------------------

_MUSIC_LINK = Path("/home/rpi/Music")
_MUSIC_INTERNAL = Path("/home/rpi/Music-internal")
_MUSIC_USB = Path("/mnt/usb-storage/Audio")

_VIDEO_LINK = Path("/home/rpi/Video")
_VIDEO_INTERNAL = Path("/home/rpi/Video-internal")
_VIDEO_USB = Path("/mnt/usb-storage/Video")

USB_MOUNT_POINT = Path("/mnt/usb-storage")


def usb_mounted() -> bool:
    """Whether the USB drive's mountpoint is currently a REAL, live mount —
    as opposed to just "the /mnt/usb-storage directory exists", which stays
    true even after the drive is physically removed (the mountpoint itself
    lives on the internal SD card/eMMC, not the USB drive). Used by the
    eject-detection watcher (streamdeck_daemon._watch_usb) to tell "drive
    present" apart from "drive gone but nothing has noticed yet".

    Wrapped in try/except: a mount whose underlying block device just
    vanished (a yank, not a clean unmount) can make stat() calls return I/O
    errors rather than cleanly reporting "not a mountpoint" — either case
    means "treat it as gone".
    """
    try:
        return os.path.ismount(USB_MOUNT_POINT)
    except OSError:
        return False


def _current_symlink_target(link: Path) -> Path | None:
    """The symlink's target, resolved to an absolute path — cheap (a single
    ``os.readlink``), so callers can read this live on every render instead
    of caching daemon-side state that could drift out of sync with the
    filesystem (same pattern as power_draw's read_pi_current_ma(), called
    fresh on every render pass rather than cached).
    """
    try:
        raw_target = os.readlink(link)
    except OSError:
        return None
    target = Path(raw_target)
    if not target.is_absolute():
        target = (link.parent / target).resolve()
    return target


def _set_symlink(link: Path, target: Path) -> bool:
    """Point ``link`` at ``target`` explicitly. Idempotent — a no-op success
    if it's already pointed there. Refuses to touch the symlink at all if
    ``target`` doesn't exist (e.g. the USB drive isn't mounted) — a
    partially-applied swap pointing at nothing would be worse than just
    declining the press.
    """
    if _current_symlink_target(link) == target:
        return True
    if not target.is_dir():
        return False
    try:
        if link.is_symlink() or link.exists():
            os.unlink(link)
        os.symlink(target, link)
    except OSError as exc:
        LOG.error("storage source set failed for %s -> %s: %s", link, target, exc)
        return False
    return True


def _swap_symlink(link: Path, internal: Path, usb: Path) -> tuple[bool, str]:
    """Toggle ``link`` between ``internal`` and ``usb``. Returns
    ``(success, label_or_failure_toast)``."""
    going_to_usb = _current_symlink_target(link) != usb
    new_target = usb if going_to_usb else internal
    if not _set_symlink(link, new_target):
        return False, "USB not mounted" if going_to_usb else "Internal missing"
    return True, ("USB" if going_to_usb else "Internal")


def music_storage_label() -> str:
    """"USB" or "Internal", derived live from the symlink target — used by
    the Stream Deck tile's ``sub`` label on every render (see
    streamdeck_daemon's "label" render branch for toggle_storage_source),
    never cached, so it can never drift from what the filesystem actually
    says.
    """
    return "USB" if _current_symlink_target(_MUSIC_LINK) == _MUSIC_USB else "Internal"


def video_storage_label() -> str:
    return "USB" if _current_symlink_target(_VIDEO_LINK) == _VIDEO_USB else "Internal"


def music_usb_available() -> bool:
    """Whether the USB drive is actually mounted right now — used to grey
    out the page-2 "USB" quick-select button when it isn't, same as a route
    key's own available/unavailable rendering. Checks the real mountpoint
    (usb_mounted()) first, not just whether the Audio subfolder happens to
    stat() successfully — see usb_mounted()'s docstring for why that
    distinction matters right after a physical unplug."""
    if not usb_mounted():
        return False
    try:
        return _MUSIC_USB.is_dir()
    except OSError:
        return False


def video_usb_available() -> bool:
    if not usb_mounted():
        return False
    try:
        return _VIDEO_USB.is_dir()
    except OSError:
        return False


@action("toggle_storage_source")
def _toggle_storage_source(ctx: ActionContext, magnitude: int) -> str | None:
    """Swap /home/rpi/Music between its internal directory and a mounted
    USB drive. Stops playback first — the currently open file's containing
    directory is about to vanish (from the listener's perspective; MPD's
    library_dir path itself never changes) mid-track otherwise — then swaps
    the symlink and triggers an ``mpd update``.

    That update() call is fire-and-forget at the MPD PROTOCOL level: MPD
    acknowledges the command immediately and does the actual (possibly
    60-90+ second, for a large library) file walk in ITS OWN background
    thread — this call, and therefore this whole action/button-press,
    returns instantly either way. The "Scanning..." label that used to sit
    on this button (removed) was what made it FEEL stuck, not the button
    itself ever blocking. Without triggering update() at all, the button
    stayed instant but MPD's database went permanently stale relative to
    whichever source is actually linked — reported live as "the folder
    __Alex Do from USB stays on row 1 after switching to Internal", i.e.
    the browser kept showing the last-indexed (wrong) content forever, not
    just slowly. Now: instant symlink flip AND instant button response,
    with the browser catching up on its own the moment MPD's background
    scan finishes — see streamdeck_daemon._watch_mpd's existing "database"
    idle-event handler, already wired to refresh both the page-1 and
    page-2 browsers with no polling needed.
    """
    if ctx.mpd.status().get("state") == "play":
        ctx.mpd.stop()
    ok, label = _swap_symlink(_MUSIC_LINK, _MUSIC_INTERNAL, _MUSIC_USB)
    if not ok:
        return label
    ctx.mpd.update_database()
    return label


@action("video_toggle_storage_source")
def _video_toggle_storage_source(ctx: ActionContext, magnitude: int) -> str | None:
    """Video's equivalent of toggle_storage_source — swaps /home/rpi/Video,
    then a synchronous rescan. Unlike MPD's database, VideoLibrary.rescan()
    is a plain directory listing (no tag-parsing per file at this scale)
    and has consistently measured near-instant even on real use (e.g. "36
    file(s)" logged with no perceptible delay) — so, unlike the music side,
    there is no slow-background-job problem to work around here; doing it
    synchronously is what makes the "up next" list on page 1 actually match
    the new source right away, addressing the same "stale after switching"
    complaint as the music fix above.
    """
    ok, label = _swap_symlink(_VIDEO_LINK, _VIDEO_INTERNAL, _VIDEO_USB)
    if not ok:
        return label
    if ctx.video_library is not None:
        count = ctx.video_library.rescan()
        return f"{label} {count}"
    return label


# ---------------------------------------------------------------------------
# Page-2 direct storage-source select — the two "Internal"/"USB" quick-jump
# keys on the browse grid's bottom row (layout.LAYOUT_BROWSE/
# LAYOUT_VIDEO_BROWSE index(3,1)/index(3,2)). Unlike the toggle above these
# set an EXPLICIT target rather than flipping whatever's currently active —
# pressing "USB" always means USB, even if you're already on USB (a no-op
# in that case, not a flip back to Internal), which is what makes them safe
# to press without first checking the current state.
# ---------------------------------------------------------------------------


def _apply_music_storage_set(ctx: ActionContext, target: str) -> str | None:
    dest = _MUSIC_INTERNAL if target == "internal" else _MUSIC_USB
    if ctx.mpd.status().get("state") == "play":
        ctx.mpd.stop()
    if not _set_symlink(_MUSIC_LINK, dest):
        return "USB not mounted" if target == "usb" else "Internal missing"
    ctx.mpd.update_database()
    return "USB" if target == "usb" else "Internal"


@action("set_storage_internal")
def _set_storage_internal(ctx: ActionContext, magnitude: int) -> str | None:
    return _apply_music_storage_set(ctx, "internal")


@action("set_storage_usb")
def _set_storage_usb(ctx: ActionContext, magnitude: int) -> str | None:
    return _apply_music_storage_set(ctx, "usb")


def _apply_video_storage_set(ctx: ActionContext, target: str) -> str | None:
    dest = _VIDEO_INTERNAL if target == "internal" else _VIDEO_USB
    if not _set_symlink(_VIDEO_LINK, dest):
        return "USB not mounted" if target == "usb" else "Internal missing"
    label = "USB" if target == "usb" else "Internal"
    if ctx.video_library is not None:
        count = ctx.video_library.rescan()
        return f"{label} {count}"
    return label


@action("video_set_storage_internal")
def _video_set_storage_internal(ctx: ActionContext, magnitude: int) -> str | None:
    return _apply_video_storage_set(ctx, "internal")


@action("video_set_storage_usb")
def _video_set_storage_usb(ctx: ActionContext, magnitude: int) -> str | None:
    return _apply_video_storage_set(ctx, "usb")


# ---------------------------------------------------------------------------
# Output routing
# ---------------------------------------------------------------------------


@action("route_to_local")
def _route_to_local(ctx: ActionContext, magnitude: int) -> str | None:
    """Switch MPD's own output to Local/USB DAC — the music page's
    equivalent of video_route_local, but far simpler: MPD already owns this
    device directly (no ALSA hand-off dance the way mpv needs), so this is
    just ``OutputRouter.switch_to`` guarded the same way every other
    route-switch call site in this codebase already guards it (see
    _video_route_local / the route-key press handling in
    streamdeck_daemon._on_key) — unavailable is reported as a toast, never
    silently ignored.
    """
    if ctx.router is None:
        return None
    route = next((r for r in ctx.router.routes if r.id == "local"), None)
    if route is None or not ctx.router.is_available(route):
        return "DAC unavailable"
    return route.label if ctx.router.switch_to(route) else "Unavailable"


@action("cycle_output")
def _cycle_output(ctx: ActionContext, magnitude: int) -> str | None:
    route = ctx.router.cycle(+1)
    if route is None:
        return "No outputs"
    ctx.bus.publish("route", id=route.id, label=route.label)
    return route.label


@action("cycle_output_back")
def _cycle_output_back(ctx: ActionContext, magnitude: int) -> str | None:
    route = ctx.router.cycle(-1)
    if route is None:
        return "No outputs"
    ctx.bus.publish("route", id=route.id, label=route.label)
    return route.label


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


@action("update_database")
def _update_database(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.mpd.update_database()
    return "Scanning…"


# Library browse actions are stubs: the queue-based model above covers the
# common case, and a full browser needs a screen. Wire these up in Phase 4
# once the Stream Deck can render a list.
@action("library_up")
def _library_up(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.bus.publish("library", direction="up", count=magnitude)
    return None


@action("library_down")
def _library_down(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.bus.publish("library", direction="down", count=magnitude)
    return None


@action("library_select")
def _library_select(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.bus.publish("library", direction="select")
    return None


@action("library_back")
def _library_back(ctx: ActionContext, magnitude: int) -> str | None:
    ctx.bus.publish("library", direction="back")
    return None


# ---------------------------------------------------------------------------
# Video / karaoke (.skp) mode
# ---------------------------------------------------------------------------
# These action NAMES are shared by the Stream Deck's video page and the
# TourBox's "video" layer — one dispatch table for both, same as every other
# action here. Only Side's role (switch_track) is layer-specific on the
# TourBox side, and that's a keymap/protocol matter, not an actions.py one.


def _capture_video_position(ctx: ActionContext) -> None:
    """Remember where the CURRENTLY loaded video is, right before something
    is about to replace it with a fresh `loadfile` (a skip, or a reload of
    the same entry when re-entering video mode after a trip to audio mode).

    mpv's `--keep-open=yes` means a video stays loaded (just paused) even
    while music mode is active on top of it — so this has to run at the top
    of every path that eventually calls `load_and_play` again, not just the
    skip actions, or resuming after Music -> Video would silently restart
    the same video from 0 instead of picking up where Music mode found it.
    """
    if ctx.video is None or ctx.video_library is None:
        return
    status = ctx.video.status()
    if not status.get("loaded"):
        return
    entry = ctx.video_library.current
    if entry is None:
        return
    ctx.video_library.remember_position(entry.path, status.get("elapsed", 0.0),
                                         status.get("duration", 0.0))


@action("enter_video_mode")
def _enter_video_mode(ctx: ActionContext, magnitude: int) -> str | None:
    """Pause MPD, load the current (or first) library entry, and tell every
    daemon on the bus to switch to video controls.

    Deliberately does NOT touch ALSA/PipeWire routing: mpv's audio currently
    goes out through whatever PipeWire's default sink is (HDMI, in the usual
    "video to a TV" case), completely independent of MPD's `Local` DAC route
    — there is no conflict to resolve for that case. If karaoke audio through
    the standalone DAC is ever wanted instead, see docs/VIDEO-MODE.md
    section 4 for the `mpc disable Local` handoff that would need adding here.
    """
    _capture_video_position(ctx)

    if ctx.mpd.status().get("state") == "play":
        ctx.mpd.toggle_pause()

    _mode.write_mode(ctx.mode_file, _mode.VIDEO)
    ctx.bus.publish("mode", value="video")

    if ctx.video is None or ctx.video_library is None:
        return "No video"

    # Default video's audio to whatever route music was already on — asked
    # for specifically: the selected output must stay locked in switching
    # back and forth between audio and video, not reset to something else.
    # Local is the one route PipeWire never sees at all (see
    # video_route_local): matching it needs the same explicit ALSA handoff a
    # press of the Local key on the video page would do. BT/AirPlay just
    # need mpv pointed at "auto" again — NOT left alone, because a previous
    # video session could have left it on the direct-ALSA string from a
    # Local session, which would otherwise silently keep mpv on the DAC even
    # though the panel (and MPD) had since moved to BT/AirPlay.
    if ctx.router is not None:
        current_route = ctx.router.detect_current()
        if current_route is not None and current_route.id == "local":
            _video_route_local(ctx, magnitude)
        elif ctx.video is not None:
            ctx.video.set_audio_device("auto")
            ctx.video_local_was_enabled = False

    entry = ctx.video_library.current or ctx.video_library.select(0)
    if entry is None:
        return "No videos found"

    if ctx.router is not None:
        ctx.router.reassert_current()
    try:
        resume = ctx.video_library.take_resume_position(entry.path)
        load_and_play(ctx.video, entry, resume=resume)
    except (SkpParseError, VideoError) as exc:
        LOG.error("video load failed for %s: %s", entry.path, exc)
        return "Load failed"
    _clamp_video_volume_if_airplay(ctx)

    return entry.song[:16]


@action("exit_video_mode")
def _exit_video_mode(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is not None:
        ctx.video.pause()
        ctx.video.set_audio_device("auto")
    # Only restore Local if VIDEO mode was the thing that disabled it. If the
    # user switched video's audio to BT/AirPlay before leaving, that's
    # already MPD's active route too (switch_to() affects both) — leave it.
    if ctx.video_local_was_enabled and ctx.router is not None:
        route = next((r for r in ctx.router.routes if r.id == "local"), None)
        if route is not None:
            ctx.router.switch_to(route)
        ctx.video_local_was_enabled = False
    _mode.write_mode(ctx.mode_file, _mode.MUSIC)
    ctx.bus.publish("mode", value="music")
    return "Music"


# ---------------------------------------------------------------------------
# Video audio routing — same 3 routes as music (config.toml [[routes]]),
# selected independently since video is a separate player process (mpv, not
# MPD). BT/AirPlay are a plain PipeWire default-sink switch; Local is not,
# because that card is deliberately hidden from PipeWire entirely (see
# 51-mpd-dac-ignore.conf) so MPD can open it bit-perfect. mpv has to reach it
# the same way MPD does: direct ALSA hw:, with MPD's own Local output
# disabled first so the two are never fighting over one exclusive device.
# ---------------------------------------------------------------------------


@action("video_route_local")
def _video_route_local(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is None or ctx.router is None:
        return None
    route = next((r for r in ctx.router.routes if r.id == "local"), None)
    if route is None or not ctx.router.is_available(route):
        return "DAC unavailable"

    for entry in ctx.mpd.outputs():
        if entry.get("outputname") == route.mpd_output:
            if entry.get("outputenabled") == "1":
                ctx.video_local_was_enabled = True
                ctx.mpd.disable_output(int(entry["outputid"]))
            break

    ctx.video.set_audio_device(f"alsa/{ctx.video_local_device}")
    ctx.video.reload_audio()
    ctx.router.mark_current("local")
    return "USB DAC"


def _clamp_video_volume_if_airplay(ctx: ActionContext) -> None:
    """Re-apply the AirPlay safety ceiling ONLY when mpv's volume has
    drifted away from what we last intentionally set it to.

    Route-switch time isn't the only moment mpv's volume can end up back at
    a dangerous 100% while AirPlay is selected — mpv resets its own runtime
    volume to its default on every process restart, and it restarts
    whenever its DRM video output fails to initialize (e.g. no HDMI monitor
    plugged in — see video-mpv.service's --vo=drm,null comment). Without
    this, a crash-looping mpv would silently undo the clamp applied at
    switch time, and the NEXT thing that actually got audio out was back at
    100%. Call this after every load_and_play(), not just after a route
    switch.

    Deliberately NOT a blind "clamp to 25 whenever it's currently above 25"
    — this runs after every song skip (_video_advance), so that used to wipe
    out a deliberate video_volume_up above the ceiling right back down to
    25% on the very next skip. Reported live as "resets to 25% every song
    skip while AirPlaying". Comparing the live volume against
    ``ctx.video_last_set_volume`` (updated by every action that deliberately
    changes mpv's volume — see video_volume_up/down, video_toggle_mute,
    and this function itself) tells "the user turned it up on purpose"
    apart from "mpv's volume reverted on its own without our knowledge" —
    only the latter gets reclamped.
    """
    if ctx.video is None or ctx.router is None:
        return
    route = ctx.router.current()
    if route is None or route.icon != "airplay":
        return
    current = ctx.video.volume()
    if current != ctx.video_last_set_volume and current > AIRPLAY_SAFE_VOLUME:
        ctx.video.set_volume(AIRPLAY_SAFE_VOLUME)
        current = AIRPLAY_SAFE_VOLUME
    ctx.video_last_set_volume = current


def _video_finish_route_switch(ctx: ActionContext, route) -> bool:
    if not ctx.router.switch_to(route):
        return False
    if ctx.video is not None:
        ctx.video.set_audio_device("auto")
        ctx.video.reload_audio()
        # Router.switch_to() already clamped MPD's mixer for AirPlay; mpv's
        # softvol is a completely separate mixer and needs the same ceiling
        # — see AIRPLAY_SAFE_VOLUME's comment (outputs.py). A freshly-woken
        # speaker plays whatever mpv's volume already was, instantly, with
        # no ramp.
        if route.icon == "airplay" and ctx.video.volume() > AIRPLAY_SAFE_VOLUME:
            ctx.video.set_volume(AIRPLAY_SAFE_VOLUME)
        # Keep the drift-detection baseline in sync — see
        # ActionContext.video_last_set_volume / _clamp_video_volume_if_airplay.
        # Without this, the very next song skip would see mpv's volume still
        # sitting above the ceiling (if it was already there before this
        # switch) with no matching "last set" record, and reclamp again —
        # harmless here since it's already at/under the ceiling, but this
        # keeps every call site consistent about who owns the value.
        ctx.video_last_set_volume = ctx.video.volume()
    ctx.video_local_was_enabled = False
    return True


def _reconnect_bt_then(ctx: ActionContext, route, finish: Callable[[], bool]) -> None:
    """Background thread body: reconnect a paired-but-off Bluetooth device,
    then run ``finish()`` (whatever completes the route switch for this
    context) if that worked. Posts the final result as a toast — this is the
    only way to surface it, since the button press that triggered this
    already returned "Connecting..." synchronously.
    """
    from .bt import connect_blocking

    ok = connect_blocking(route.bt_mac)
    if ok:
        ok = finish()
    ctx.bus.publish("toast", text=(route.label if ok else "BT failed"))


def _video_switch_route(ctx: ActionContext, route_id: str) -> str | None:
    if ctx.video is None or ctx.router is None:
        return None
    route = next((r for r in ctx.router.routes if r.id == route_id), None)
    if route is None:
        return "No route"

    if ctx.router.is_available(route):
        return route.label if _video_finish_route_switch(ctx, route) else "Unavailable"

    if route.bt_mac:
        threading.Thread(
            target=_reconnect_bt_then,
            args=(ctx, route, lambda: _video_finish_route_switch(ctx, route)),
            daemon=True, name="bt-connect-video",
        ).start()
        return "Connecting..."

    return "Unavailable"


@action("video_route_bt")
def _video_route_bt(ctx: ActionContext, magnitude: int) -> str | None:
    return _video_switch_route(ctx, "bt")


@action("video_route_airplay")
def _video_route_airplay(ctx: ActionContext, magnitude: int) -> str | None:
    return _video_switch_route(ctx, "airplay")


@action("video_volume_up")
def _video_volume_up(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is None:
        return None
    new = ctx.video.change_volume(_volume_delta(ctx, magnitude))
    # Deliberate, past the AirPlay ceiling if the user wants — record it so
    # the next song skip's safety clamp recognizes this as intentional and
    # leaves it alone. See ActionContext.video_last_set_volume.
    ctx.video_last_set_volume = new
    return f"Vol {new}%"


@action("video_volume_down")
def _video_volume_down(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is None:
        return None
    new = ctx.video.change_volume(-_volume_delta(ctx, magnitude))
    ctx.video_last_set_volume = new
    return f"Vol {new}%"


# Same stash-and-restore mute as MPD's toggle_mute above, kept as a separate
# module-level variable since it is a genuinely different mixer (mpv's, not
# MPD's) — muting one must never touch the other's remembered level.
_pre_mute_video_volume: int | None = None


@action("video_toggle_mute")
def _video_toggle_mute(ctx: ActionContext, magnitude: int) -> str | None:
    global _pre_mute_video_volume
    if ctx.video is None:
        return None
    current = ctx.video.volume()
    if current > 0:
        _pre_mute_video_volume = current
        ctx.video.set_volume(0)
        ctx.video_last_set_volume = 0
        return "Muted"
    restored = _pre_mute_video_volume or 30
    ctx.video.set_volume(restored)
    ctx.video_last_set_volume = restored
    _pre_mute_video_volume = None
    return "Unmuted"


@action("video_play_pause")
def _video_play_pause(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is None:
        return None
    # Same drift guard as toggle_pause — mpv's audio-device="auto" means it
    # is PipeWire's default-sink follower too, with the same "only resolved
    # when the stream (re)opens" caveat.
    if ctx.router is not None and bool(ctx.video.get("pause", True)):
        ctx.router.reassert_current()
    ctx.video.play_pause()
    return None


@action("switch_track")
def _switch_track(ctx: ActionContext, magnitude: int) -> str | None:
    """Toggle between the embedded vocal and karaoke audio tracks.

    Requested specifically as: a Stream Deck video-page key, and the
    TourBox's Side button while in the video layer (a tap, not a hold — Side
    keeps its normal shift-modifier role everywhere else, including within
    the video layer, via Decoder's tap-vs-hold handling).
    """
    if ctx.video is None:
        return None
    new_id = ctx.video.switch_track()
    return track_title(new_id - 1)[:16]  # mpv track ids are 1-based


def _video_advance(ctx: ActionContext, step: int) -> str | None:
    if ctx.video is None or ctx.video_library is None:
        return None
    _capture_video_position(ctx)
    if ctx.router is not None:
        ctx.router.reassert_current()
    entry = ctx.video_library.next() if step > 0 else ctx.video_library.prev()
    if entry is None:
        return "No videos found"
    try:
        resume = ctx.video_library.take_resume_position(entry.path)
        load_and_play(ctx.video, entry, resume=resume)
    except (SkpParseError, VideoError) as exc:
        LOG.error("video load failed for %s: %s", entry.path, exc)
        return "Load failed"
    _clamp_video_volume_if_airplay(ctx)
    return entry.song[:16]


@action("video_next_song")
def _video_next_song(ctx: ActionContext, magnitude: int) -> str | None:
    return _video_advance(ctx, +1)


@action("video_prev_song")
def _video_prev_song(ctx: ActionContext, magnitude: int) -> str | None:
    return _video_advance(ctx, -1)


@action("video_rescan")
def _video_rescan(ctx: ActionContext, magnitude: int) -> str | None:
    """Re-scan the .skp library folder for new/removed files.

    Video mode's equivalent of the music page's "Scan" key (update_database)
    — needed because, unlike MPD's library, nothing watches the Video folder
    for changes: dropping a new .skp in over SFTP/USB is otherwise invisible
    until the daemon restarts. Synchronous and cheap (a flat os.listdir, no
    tag reading), so unlike MPD's async "Scanning…" this can report the
    actual count immediately rather than a fire-and-forget toast.
    """
    if ctx.video_library is None:
        return None
    count = ctx.video_library.rescan()
    return f"{count} video{'s' if count != 1 else ''}"


@action("video_reinit")
def _video_reinit(ctx: ActionContext, magnitude: int) -> str | None:
    """Restart the mpv video engine (video-mpv.service) to force a fresh
    HDMI/DRM output probe.

    video-mpv.service starts with ``--vo=drm,null`` (see that unit file's
    own comment): if no HDMI monitor is connected at BOOT, mpv's DRM output
    fails to initialize and falls back to the no-op "null" video output —
    permanently, for the life of that mpv process. Plugging a monitor in
    later does nothing; mpv never re-probes DRM on its own; the box keeps
    playing audio with no video output until something forces a fresh
    start. Reported live as exactly that. A full process restart (not just
    an mpv IPC property poke) is what's needed — the vo fallback decision
    happens once, at process startup, and `--vo=drm,null` gives no runtime
    "retry drm" affordance to hook into over IPC.

    Needs org.freedesktop.systemd1.manage-units authorization for
    video-mpv.service specifically — see
    system/polkit/53-rpi-player-video-mpv.rules. Without that rule this
    just fails (non-zero exit, caught below), same as every other
    ``systemctl`` action in this codebase without its matching rule.

    Remembers whatever was loaded and its playback position beforehand
    (same take_resume_position/load_and_play pair enter_video_mode uses)
    and resumes it once the fresh mpv process's IPC socket is back up —
    otherwise this would silently leave video mode sitting on a blank,
    idle mpv with nothing loaded, which defeats the point of a "fix my
    video output" button.

    Also explicitly restores whatever mpv's volume was set to right before
    the restart. A freshly-started mpv process resets its own softvol to
    its default (100%) — reported live as this button silently blasting
    the volume to max even though the video itself came back fine. Every
    other place this codebase deliberately changes mpv's volume records it
    on ``ctx.video_last_set_volume`` first (see video_volume_up/down,
    video_toggle_mute) so a later drift-check can tell "we did this on
    purpose" apart from "mpv's volume reverted on its own" — this restore
    is exactly that same "mpv's volume reverted on its own" case, just
    triggered by a full process restart instead of a crash-loop.
    """
    if ctx.video is None or ctx.video_library is None:
        return None

    saved_volume = ctx.video.volume()
    _capture_video_position(ctx)
    entry = ctx.video_library.current
    resume = ctx.video_library.take_resume_position(entry.path) if entry else None

    try:
        proc = subprocess.run(
            ["systemctl", "restart", "video-mpv.service"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.error("systemctl restart video-mpv.service failed: %s", exc)
        return "Reinit failed"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        LOG.error("systemctl restart video-mpv.service exited %d: %s",
                  proc.returncode, detail[-1] if detail else "no output")
        return "Reinit failed"

    # Wait for the FRESH mpv process's IPC socket to actually accept
    # connections and answer before trying to reload anything into it —
    # command()'s own one-retry-on-a-dead-socket logic covers a socket that
    # merely hiccuped, not "the whole process is still mid-restart".
    deadline = time.monotonic() + 8.0
    ready = False
    while time.monotonic() < deadline:
        if ctx.video.get("idle-active", None) is not None:
            ready = True
            break
        time.sleep(0.3)
    if not ready:
        return "Reinit: no reply"

    ctx.video.set_volume(saved_volume)
    ctx.video_last_set_volume = saved_volume

    if entry is None:
        return "HDMI reinit"
    try:
        load_and_play(ctx.video, entry, resume=resume)
    except (SkpParseError, VideoError) as exc:
        LOG.error("video reinit: reload failed for %s: %s", entry.path, exc)
        return "Reinit OK, load failed"
    # A fresh load_and_play can itself reset mpv's softvol, same as the
    # process restart did -- reassert it once more after, not just before.
    ctx.video.set_volume(saved_volume)
    ctx.video_last_set_volume = saved_volume
    _clamp_video_volume_if_airplay(ctx)
    return "HDMI reinit"


@action("video_delete_current")
def _video_delete_current(ctx: ActionContext, magnitude: int) -> str | None:
    """Delete/trash/skip the CURRENTLY PLAYING video — the video-page
    equivalent of delete_current, sharing the exact same ctx.delete settings
    (mode/trash_dir/log_path/enabled/confirm) so one config controls both;
    see LAYOUT_VIDEO's (2,7), the same cell as the music page's Delete key.

    Unlike music, there is no MPD queue to remove from — VideoLibrary is
    just an in-memory list — so "queue" mode here means "skip past it
    without touching the file", the same least-destructive spirit as
    music's "remove from queue, file untouched".

    Ordering mirrors _delete_current exactly: advance playback to the NEXT
    video first, THEN touch the old file, so mpv is never asked to keep
    playing something being moved/unlinked out from under it.
    """
    settings = ctx.delete
    if settings is None or not getattr(settings, "enabled", False):
        return "Delete off"
    if ctx.video is None or ctx.video_library is None:
        return None

    entry = ctx.video_library.current
    if entry is None:
        return "Nothing playing"

    label = entry.song
    mode = getattr(settings, "mode", "queue")
    old_path = Path(entry.path)

    # Validate before changing any state — same hard safety rail as music's
    # _resolve_under_music_dir, checked directly against VideoLibrary's own
    # root (see VideoLibrary.root's docstring for why this is
    # defense-in-depth rather than the primary guard here).
    if mode in ("trash", "permanent"):
        try:
            old_path.resolve().relative_to(Path(ctx.video_library.root).resolve())
        except ValueError:
            LOG.error("REFUSING to touch %s — outside the video library %s",
                      old_path, ctx.video_library.root)
            return "Blocked"
        if not old_path.is_file():
            return "Blocked"

    # Advance playback along BEFORE touching the file — mirrors music's
    # "move playback along first" ordering.
    if ctx.router is not None:
        ctx.router.reassert_current()
    next_entry = ctx.video_library.next()
    if next_entry is not None and next_entry.path != str(old_path):
        try:
            resume = ctx.video_library.take_resume_position(next_entry.path)
            load_and_play(ctx.video, next_entry, resume=resume)
            _clamp_video_volume_if_airplay(ctx)
        except (SkpParseError, VideoError) as exc:
            LOG.error("video advance-before-delete failed for %s: %s",
                      next_entry.path, exc)
    else:
        # Only entry in the library (next() wrapped back to itself) —
        # nothing to advance to. Stop playback so mpv isn't left holding a
        # handle to a file about to disappear.
        ctx.video.stop()

    if mode == "queue":
        _log_deletion(settings, old_path.name, None, "queue")
        LOG.info("skipped without deleting: %s", old_path)
        return f"Skipped {label[:12]}"

    # Same transient-failure retry as music's _delete_current — see that
    # function's comment for the live "[Errno 30] Read-only file system"
    # incident this defends against.
    last_exc: OSError | None = None
    result = ""
    for attempt in (1, 2):
        try:
            if mode == "trash":
                trash_root = Path(getattr(settings, "trash_dir", "") or "")
                if not trash_root:
                    return "No trash dir"
                rel = old_path.resolve().relative_to(Path(ctx.video_library.root).resolve())
                # Namespaced under "Video/" so a video and a music file that
                # happen to share the same relative path never collide in
                # one shared trash_dir.
                target = trash_root / "Video" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(
                        f"{target.stem}.{int(datetime.now().timestamp())}{target.suffix}")
                shutil.move(str(old_path), str(target))
                _log_deletion(settings, str(rel), target, "trash")
                LOG.warning("moved video to trash: %s -> %s", old_path, target)
                result = f"Trashed {label[:12]}"
            else:
                old_path.unlink()
                _log_deletion(settings, old_path.name, old_path, "permanent")
                LOG.warning("PERMANENTLY DELETED video: %s", old_path)
                result = f"Deleted {label[:12]}"
            last_exc = None
            break
        except OSError as exc:
            last_exc = exc
            LOG.warning("video delete attempt %d/2 failed for %s: %s",
                        attempt, old_path, exc)
            if attempt == 1:
                if exc.errno == 30:  # EROFS — USB went read-only; try to recover
                    _try_remount_usb_rw()
                time.sleep(0.6)

    if last_exc is not None:
        LOG.error("video delete failed for %s after retry: %s", old_path, last_exc)
        return "Delete failed"

    ctx.video_library.rescan()
    return result


@action("video_seek_forward")
def _video_seek_forward(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is not None:
        ctx.video.seek(_seek_delta(ctx, magnitude))
    return None


@action("video_seek_back")
def _video_seek_back(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is not None:
        ctx.video.seek(-_seek_delta(ctx, magnitude))
    return None


@action("video_seek_forward_fast")
def _video_seek_forward_fast(ctx: ActionContext, magnitude: int) -> str | None:
    """Coarse scrub forward — the video-page equivalent of seek_forward_fast.
    Reuses the same ``ctx.seek_step_fast`` (config.toml's [tourbox.steps]
    seek_fast, 30s by default — double the plain 15s seek step) rather than
    a separate video-only setting, so the two scrub speeds stay in the same
    ratio on both pages.
    """
    if ctx.video is not None:
        ctx.video.seek(_seek_delta_fast(ctx, magnitude))
    return f"+{ctx.seek_step_fast}s"


@action("video_seek_back_fast")
def _video_seek_back_fast(ctx: ActionContext, magnitude: int) -> str | None:
    if ctx.video is not None:
        ctx.video.seek(-_seek_delta_fast(ctx, magnitude))
    return f"-{ctx.seek_step_fast}s"


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


@action("shutdown")
def _shutdown(ctx: ActionContext, magnitude: int) -> str | None:
    """Clean shutdown.

    Stop playback first so the DAC is released and any network sink gets a
    proper teardown, then hand off to systemd. See docs/ROADMAP.md Phase 5 for
    why an abrupt power cut is the most likely way this build dies.

    ``systemctl poweroff`` talks to systemd-logind over D-Bus and is subject to
    polkit authorization. Without a rule granting this to the service account,
    it fails with "Interactive authentication required" — and because the old
    code used ``check=False`` and only logged inside the ``except`` block, a
    non-zero exit here was swallowed *silently*: no journal line, no toast, the
    Stream Deck just fell back to idle as if nothing happened. See
    system/polkit/49-rpi-player-shutdown.rules for the authorization fix; this
    still checks and reports failure so a *future* permissions regression is
    visible instead of silent.
    """
    LOG.warning("shutdown requested")
    ctx.bus.publish("shutdown")
    ctx.mpd.stop()
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "poweroff"],
            timeout=5, check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            LOG.error(
                "poweroff failed (exit %d): %s",
                result.returncode, detail[-1] if detail else "no output",
            )
            return "Shutdown failed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.error("poweroff failed: %s", exc)
        return "Shutdown failed"
    return "Shutting down"


# ---------------------------------------------------------------------------
# Wi-Fi radio toggle — Video page only, battery saver for on-the-road use.
# NetworkManager owns networking on this build (see docs/DEPLOY.md), so this
# is plain `nmcli radio wifi`, not a raw rfkill/ip link toggle that could
# drift out of sync with what NetworkManager itself thinks the radio state
# is. Deliberately independent of video/music mode — nothing about entering
# or exiting video mode touches networking, so "off" naturally survives
# switching back and forth exactly as asked. Only comes back on at the next
# boot, forced by system/systemd/rpi-player-wifi-on.service (NetworkManager
# otherwise persists WirelessEnabled across reboots on its own, which would
# make "off" stick forever instead of being a per-drive choice).
# ---------------------------------------------------------------------------

_NMCLI = shutil.which("nmcli") or "/usr/bin/nmcli"


def query_wifi_enabled() -> bool:
    """Ask NetworkManager whether the Wi-Fi radio is currently on.

    Called once at daemon startup (streamdeck_daemon.py) to seed
    ActionContext.wifi_enabled correctly, rather than assuming it's on —
    the previous session could have left it off if the daemon restarted
    without an intervening reboot. Defaults to True (on) if nmcli is
    missing or the query fails, matching what the boot-time unit enforces
    anyway, so a query failure never falsely renders the key as already off.
    """
    try:
        result = subprocess.run(
            [_NMCLI, "radio", "wifi"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.debug("nmcli radio wifi query failed: %s", exc)
        return True
    if result.returncode != 0:
        LOG.debug("nmcli radio wifi query exited %d", result.returncode)
        return True
    return result.stdout.strip() == "enabled"


@action("toggle_wifi")
def _toggle_wifi(ctx: ActionContext, magnitude: int) -> str | None:
    """Turn the Wi-Fi radio off to save battery on the road, or back on.

    Runs as the unprivileged `rpi` service user; system/polkit/50-rpi-
    player-wifi.rules grants exactly the one NetworkManager D-Bus action
    (org.freedesktop.NetworkManager.enable-disable-wifi) this needs, without
    a password — same pattern as `shutdown`'s polkit rule, see that
    action's docstring. Without it this would fail the same silent way
    poweroff used to: a non-zero exit, nothing else.

    Only flips ctx.wifi_enabled (and so what the panel shows) when the
    command actually succeeded — a failed nmcli call must not leave the key
    lying about the real radio state.
    """
    target_state = "off" if ctx.wifi_enabled else "on"
    try:
        result = subprocess.run(
            [_NMCLI, "radio", "wifi", target_state],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.error("nmcli radio wifi %s failed: %s", target_state, exc)
        return "Wifi failed"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        LOG.error("nmcli radio wifi %s failed (exit %d): %s",
                  target_state, result.returncode, detail[-1] if detail else "no output")
        return "Wifi failed"
    ctx.wifi_enabled = not ctx.wifi_enabled
    return "Wifi On" if ctx.wifi_enabled else "Wifi Off"


def known_actions() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Delete — DESTRUCTIVE
# ---------------------------------------------------------------------------
# Behaviour is driven by ctx.delete (a DeleteSettings). The mode decides how
# far it goes; the path guard below is NOT optional in any mode.


def _resolve_under_music_dir(uri: str, music_dir: str) -> Path | None:
    """Resolve an MPD URI to an absolute path, refusing to escape the library.

    MPD URIs are relative to music_directory, but a malformed or crafted URI
    containing '..' would otherwise resolve anywhere the daemon can write. This
    is the difference between "deletes a song" and "deletes an arbitrary file",
    so it is enforced regardless of configuration.
    """
    if not uri or not music_dir:
        return None
    root = Path(music_dir).resolve()
    candidate = (root / uri).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        LOG.error("REFUSING to touch %s — outside the music directory %s",
                  candidate, root)
        return None
    if not candidate.is_file():
        LOG.warning("not a file (already gone?): %s", candidate)
        return None
    return candidate


def _log_deletion(settings: Any, uri: str, path: Path | None, mode: str) -> None:
    """Append a record of what was removed.

    The only trace left after a permanent delete. Costs nothing at press time
    and turns "what did I just lose?" into an answerable question.
    """
    if not getattr(settings, "log_path", ""):
        return
    try:
        line = (f"{datetime.now().isoformat(timespec='seconds')}\t{mode}\t"
                f"{uri}\t{path or ''}\n")
        with open(settings.log_path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        LOG.warning("could not write the deletion log: %s", exc)


@action("delete_current")
def _delete_current(ctx: ActionContext, magnitude: int) -> str | None:
    """Remove the currently playing track.

    How far this goes depends on ctx.delete.mode:
        queue      -> drop from the play queue; file untouched
        trash      -> move the file into trash_dir; recoverable
        permanent  -> unlink the file; NOT recoverable

    Ordering matters. We advance to the next track BEFORE touching the file, so
    playback moves on cleanly rather than continuing to read a file that is
    being removed underneath it.
    """
    settings = ctx.delete
    if settings is None or not getattr(settings, "enabled", False):
        return "Delete off"

    song = ctx.mpd.current_song()
    uri = song.get("file", "")
    song_id = song.get("id")
    if not uri:
        return "Nothing playing"

    label = song.get("title") or Path(uri).name
    mode = getattr(settings, "mode", "queue")

    # Resolve and validate before changing any state.
    path = None
    if mode in ("trash", "permanent"):
        path = _resolve_under_music_dir(uri, getattr(settings, "music_dir", ""))
        if path is None:
            return "Blocked"

    # Move playback along first.
    status = ctx.mpd.status()
    if status.get("state") in ("play", "pause"):
        ctx.mpd.next_track()

    if song_id:
        ctx.mpd.delete_id(song_id)

    if mode == "queue":
        _log_deletion(settings, uri, None, "queue")
        LOG.info("removed from queue: %s", uri)
        return f"Dequeued {label[:12]}"

    # One retry after a short pause before giving up. Found live: two real
    # delete attempts minutes apart both failed with
    # "[Errno 30] Read-only file system: '/home/rpi/Music-trash'" even
    # though the root filesystem was rw both immediately before and after —
    # no matching kernel remount-ro event in the journal either, so this
    # reads as a genuinely transient hiccup (this whole build runs off a
    # microSD card on a car's 12V->5V supply — vcgencmd reported a history
    # of throttling/soft-temp-limit events — rather than a real, persistent
    # permission or mount problem; ls -l on both the music directory and the
    # trash directory show the service account owns them outright). A
    # momentary blip like that is exactly what a short retry is for; a
    # genuinely broken mount would just fail again and still surface
    # "Delete failed" same as before.
    last_exc: OSError | None = None
    result = ""
    for attempt in (1, 2):
        try:
            if mode == "trash":
                trash_root = Path(getattr(settings, "trash_dir", "") or "")
                if not trash_root:
                    return "No trash dir"
                # Preserve the library-relative layout so the file is findable.
                target = trash_root / uri
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(
                        f"{target.stem}.{int(datetime.now().timestamp())}{target.suffix}")
                shutil.move(str(path), str(target))
                _log_deletion(settings, uri, target, "trash")
                LOG.warning("moved to trash: %s -> %s", uri, target)
                result = f"Trashed {label[:12]}"
            else:
                path.unlink()
                _log_deletion(settings, uri, path, "permanent")
                LOG.warning("PERMANENTLY DELETED: %s", path)
                result = f"Deleted {label[:12]}"
            last_exc = None
            break
        except OSError as exc:
            last_exc = exc
            LOG.warning("delete attempt %d/2 failed for %s: %s", attempt, path, exc)
            if attempt == 1:
                if exc.errno == 30:  # EROFS — USB went read-only; try to recover
                    _try_remount_usb_rw()
                time.sleep(0.6)

    if last_exc is not None:
        LOG.error("delete failed for %s after retry: %s", path, last_exc)
        return "Delete failed"

    # Tell MPD the file is gone so the browser and database stay honest.
    parent = str(Path(uri).parent)
    ctx.mpd.update_database() if parent in ("", ".") else ctx.mpd._call("update", parent)
    return result


# ---------------------------------------------------------------------------
# Folder navigation
# ---------------------------------------------------------------------------
# MPD has no "next folder" command. We enumerate the library's directory tree,
# locate the directory of the currently playing file, step to the adjacent one
# and load it. Sorted order so "next" is stable and predictable.

_folder_cache: list[str] = []
_folder_cache_key: str = ""


def library_folders_for(mpd) -> list[str]:
    """Public helper so ContinuousPlayback can reuse the cached folder list."""
    class _Shim:
        pass
    shim = _Shim(); shim.mpd = mpd
    return _library_folders(shim)


def _library_folders(ctx) -> list[str]:
    """All directories in the library, sorted, cached against the DB version.

    Cached because walking the tree costs an lsinfo per directory; invalidated
    on MPD's db_update so a rescan is picked up without a restart.
    """
    global _folder_cache, _folder_cache_key
    key = str(ctx.mpd.stats().get("db_update", ""))
    if key and key == _folder_cache_key and _folder_cache:
        return _folder_cache

    folders: list[str] = []
    stack = [""]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        for entry in ctx.mpd.lsinfo(current):
            directory = entry.get("directory")
            if directory and directory not in seen:
                seen.add(directory)
                folders.append(directory)
                stack.append(directory)

    folders.sort(key=str.lower)
    _folder_cache, _folder_cache_key = folders, key
    return folders


def _jump_folder(ctx: ActionContext, direction: int) -> str | None:
    """Load the adjacent library folder and start playing it."""
    folders = _library_folders(ctx)
    if not folders:
        return "No folders"

    current_uri = ctx.mpd.current_song().get("file", "")
    current_dir = str(Path(current_uri).parent) if current_uri else ""
    if current_dir in (".", "/"):
        current_dir = ""

    if current_dir in folders:
        index = folders.index(current_dir) + direction
    else:
        # Not inside a known folder (empty queue, or playing from the root):
        # start at either end depending on which way we were asked to go.
        index = 0 if direction > 0 else len(folders) - 1

    index %= len(folders)          # wrap, so the control never dead-ends
    target = folders[index]

    ctx.mpd.clear_queue()
    ctx.mpd.add(target)
    ctx.mpd.play_position(0)
    LOG.info("folder -> %s", target)
    return Path(target).name[:16] or target[:16]


@action("next_folder")
def _next_folder(ctx: ActionContext, magnitude: int) -> str | None:
    return _jump_folder(ctx, +1)


@action("prev_folder")
def _prev_folder(ctx: ActionContext, magnitude: int) -> str | None:
    return _jump_folder(ctx, -1)
