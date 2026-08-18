"""Touch UI button geometry: the single source of truth shared by
render.py (drawing) and touch_daemon.py (hit-testing), so the two can
never drift apart — same rationale as player/streamdeck/layout.py for the
Stream Deck's 8x4 grid.

--------------------------------------------------------------------------
v2 redesign (this file's second layout, replacing the first outline-only
wireframe)
--------------------------------------------------------------------------
The first wireframe pass (pure outline, fully transparent interiors) was
explicitly rejected as "hideous" in favor of a supplied mockup: filled
rounded-pill buttons, colored glowing borders, an album-art panel, and
format-tag chips. This file's rects were redrawn from scratch to match
that mockup's structure (art panel top-left, mode+output pills top-right,
title/subtitle/tags in a middle column, scrub bar, a transport row of
pill buttons with a bigger circular Play in the middle, a volume bar).
See render.py's module docstring for the one deliberate compromise versus
the mockup: panel fills are translucent, not fully opaque, so video mode
still shows the playing video faintly through the UI (the ORIGINAL
explicit ask — "so I can see through the video being played" — still
applies in video mode; a fully opaque card would defeat it).

--------------------------------------------------------------------------
Coordinate space and the 180-degree rotation contract
--------------------------------------------------------------------------
Every rectangle below is in LOGICAL space: an 800x480 canvas, NOT
pre-rotated. Rotation (TouchConfig.rotate_180 — this Pi's DSI driver does
not honor the standard KMS rotate property, confirmed live) is applied
ONCE, at the boundary, in both directions, and nowhere else:

  * render.py rotates the FINISHED 800x480 canvas 180 degrees before
    handing raw BGRA bytes to mpv.
  * touch_daemon.py rotates a raw touch event's (x, y) 180 degrees BEFORE
    calling hit_test() against the rectangles below.

Every button rect, every draw call, every hit-test in this module and in
render.py stays in plain, unrotated logical coordinates. If a control ever
looks mirrored or hit-tests the wrong thing, the bug is in one of those
two boundary transforms, not in this file.
"""

from __future__ import annotations

from dataclasses import dataclass

W, H = 800, 480


@dataclass(frozen=True)
class Button:
    # Action name. Either a real player.actions action name (dispatched
    # through the shared action table, exactly like TourBox/Stream Deck),
    # or a "_"-prefixed pseudo-action touch_daemon handles itself (mode
    # switch, route-picker open, absolute seek/volume drag, overlay
    # show/hide) because it has no TourBox/Stream Deck equivalent.
    action: str
    rect: tuple[int, int, int, int]                    # x, y, w, h
    label: str = ""
    modes: tuple[str, ...] = ("music", "video")         # which mode(s) show it


# -- album art panel (top-left) ----------------------------------------------
ART_RECT = (20, 26, 168, 168)
# Small heart badge overlaid on the art's top-left corner — a second,
# larger tap target for curate/favorite than the bottom transport pill,
# mirroring the mockup exactly (it shows a heart badge ON the art, not
# just in the transport row). Same action as CURATE_BTN below, just a
# second rect that reaches it — hit_test() doesn't care which rect fired.
ART_BADGE   = Button("curate_current",       (26, 32, 34, 34), "curate")
ART_BADGE_V = Button("video_curate_current", ART_BADGE.rect,   "curate", modes=("video",))

# -- top-right icon/pill cluster ---------------------------------------------
MODE_PILL    = Button("_toggle_mode",    (204, 26, 108, 36), "MODE")
SCAN_ICON    = Button("update_database", (472, 26, 36, 36), "scan")
STORAGE_PILL = Button("_toggle_storage", (516, 26, 84, 36), "INT/USB")
BT_PILL      = Button("_open_bt",        (608, 26, 84, 36), "BT")
AIRPLAY_PILL = Button("_open_airplay",   (700, 26, 84, 36), "AirPlay")
SCAN_ICON_V  = Button("video_rescan", SCAN_ICON.rect, "scan", modes=("video",))

# -- scrub bar ----------------------------------------------------------------
SCRUB_BAR = Button("_seek_absolute", (20, 232, 764, 16), "seek")

# -- bottom transport row -----------------------------------------------------
# Centered as a group (not left-anchored) — four pills + a bigger circular
# Play in music mode, the same plus a Karaoke/Vocal pill on the end in
# video mode (fits the 800px canvas exactly with room to spare, see the
# arithmetic in the redesign notes above).
CURATE_BTN = Button("curate_current",    (151, 262, 140, 56), "curate")
PREV_BTN   = Button("prev_track",        (307, 262, 110, 56), "prev")
PLAY_BTN   = Button("toggle_pause",      (433, 254, 100, 72), "play/pause")
NEXT_BTN   = Button("next_track",        (549, 262, 100, 56), "next")
VOCAL_BTN  = Button("switch_track",      (665, 262, 119, 56), "vocal", modes=("video",))

# Video-mode equivalents of the four music actions above — same rect, same
# label, different actions.py entry point.
CURATE_BTN_V = Button("video_curate_current", CURATE_BTN.rect, "curate", modes=("video",))
PREV_BTN_V   = Button("video_prev_song",      PREV_BTN.rect,   "prev",   modes=("video",))
PLAY_BTN_V   = Button("video_play_pause",     PLAY_BTN.rect,   "play/pause", modes=("video",))
NEXT_BTN_V   = Button("video_next_song",      NEXT_BTN.rect,   "next",   modes=("video",))

# -- volume row ----------------------------------------------------------------
VOLUME_BAR = Button("_volume_absolute", (76, 346, 636, 20), "volume")

# Whole middle band below the header, above the transport row — plain tap
# (no direction ever locked in — see touch_daemon.py's _on_touch_up)
# toggles hide/show. A vertical drag adjusts volume, a horizontal drag
# scrubs — see touch_daemon.py's _handle_content_gesture(). Lowest
# priority in hit_test() — every real control rect is checked first.
CONTENT_AREA = Button("_toggle_overlay", (0, 0, W, H), "content")

# Buttons that exist in BOTH modes but dispatch a different action name per
# mode. Everything else is mode-agnostic.
#
# NOTE: SCAN_ICON/SCAN_ICON_V (like CURATE_BTN/_V etc.) share a rect but
# NOT an action — SCAN_ICON must stay out of _SHARED, or hit_test() in
# video mode would match it (earlier in the concatenated list) before ever
# reaching SCAN_ICON_V at the same coordinates, silently calling
# update_database (the music action) instead of video_rescan. Found this
# exact class of bug once already in this file (ART_BADGE vs. CURATE_BTN
# colliding in render.py's by-action dict) — same root cause, different
# spot: two Buttons sharing a rect must never share an action name AND
# both be reachable in the same mode's hit_test list.
_MUSIC_ONLY = (CURATE_BTN, PREV_BTN, PLAY_BTN, NEXT_BTN, ART_BADGE, SCAN_ICON)
_VIDEO_ONLY = (CURATE_BTN_V, PREV_BTN_V, PLAY_BTN_V, NEXT_BTN_V, VOCAL_BTN,
               SCAN_ICON_V, ART_BADGE_V)
_SHARED = (MODE_PILL, STORAGE_PILL, BT_PILL, AIRPLAY_PILL, SCRUB_BAR, VOLUME_BAR)

ALL_BUTTONS: tuple[Button, ...] = _SHARED + _MUSIC_ONLY + _VIDEO_ONLY + (CONTENT_AREA,)


def buttons_for_mode(mode: str) -> list[Button]:
    """Buttons actually visible/tappable in ``mode`` ('music' or 'video')."""
    if mode == "video":
        return [b for b in _SHARED if "video" in b.modes] + list(_VIDEO_ONLY)
    return [b for b in _SHARED if "music" in b.modes] + list(_MUSIC_ONLY)


def hit_test(x: int, y: int, mode: str) -> Button | None:
    """Which button (if any) logical point (x, y) falls inside.

    CONTENT_AREA is checked last on purpose — every real control sits ON
    TOP of it visually, so a real control's rect must win before the
    whole-screen catch-all gets a chance.
    """
    for b in buttons_for_mode(mode):
        bx, by, bw, bh = b.rect
        if bx <= x < bx + bw and by <= y < by + bh:
            return b
    bx, by, bw, bh = CONTENT_AREA.rect
    if bx <= x < bx + bw and by <= y < by + bh:
        return CONTENT_AREA
    return None


def rotate_point_180(x: int, y: int) -> tuple[int, int]:
    """Flip a raw touch coordinate 180 degrees within the WxH canvas.

    See this module's docstring — the ONE place this transform should
    happen on the input side. touch_daemon.py calls this on every raw
    touch sample before anything else touches the coordinate.
    """
    return (W - 1 - x, H - 1 - y)
