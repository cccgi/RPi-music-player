"""Touch UI button geometry: the single source of truth shared by
render.py (drawing) and touch_daemon.py (hit-testing), so the two can
never drift apart — same rationale as player/streamdeck/layout.py for the
Stream Deck's 8x4 grid.

Mirrors the wireframe mockup reviewed and approved before this was built
(see the design doc/artifact "rpi-touch-wireframe").

--------------------------------------------------------------------------
Coordinate space and the 180-degree rotation contract
--------------------------------------------------------------------------
Every rectangle below is in LOGICAL space: an 800x480 canvas, NOT
pre-rotated, drawn exactly as the wireframe was designed. Rotation (see
TouchConfig.rotate_180 — needed because this Pi's DSI driver does not
honor the standard KMS rotate property, confirmed live) is applied ONCE,
at the boundary, in both directions, and nowhere else:

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

from dataclasses import dataclass, field

W, H = 800, 480

TOPBAR_H = 56
BOTTOMBAR_Y = 340
BOTTOMBAR_H = H - BOTTOMBAR_Y   # 140


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


# -- top bar ----------------------------------------------------------------
MODE_PILL    = Button("_toggle_mode",    (8, 9, 118, 38), "MODE")
STORAGE_PILL = Button("_toggle_storage", (612, 9, 70, 38), "INT/USB")
BT_ICON      = Button("_open_bt",        (694, 9, 38, 38), "BT")
AIRPLAY_ICON = Button("_open_airplay",   (744, 9, 38, 38), "AirPlay")

# -- bottom control zone ------------------------------------------------------
SCRUB_BAR   = Button("_seek_absolute",    (18, 344, 764, 24), "seek")
CURATE_BTN  = Button("curate_current",    (40, 388, 56, 56), "curate")
PREV_BTN    = Button("prev_track",        (120, 388, 56, 56), "prev")
PLAY_BTN    = Button("toggle_pause",      (322, 384, 74, 74), "play/pause")
NEXT_BTN    = Button("next_track",        (450, 388, 56, 56), "next")
DELETE_BTN  = Button("delete_current",    (530, 388, 56, 56), "delete")
VOCAL_BTN   = Button("switch_track",      (610, 388, 120, 56), "vocal", modes=("video",))
VOLUME_BAR  = Button("_volume_absolute",  (40, 460, 720, 20), "volume")

# Video-mode equivalents of the two music actions above — same rect, same
# label, different actions.py entry point (see actions.py's
# video_curate_current / video_delete_current / video_play_pause /
# video_next_song / video_prev_song).
CURATE_BTN_V = Button("video_curate_current", CURATE_BTN.rect, "curate", modes=("video",))
PREV_BTN_V   = Button("video_prev_song",      PREV_BTN.rect,   "prev",   modes=("video",))
PLAY_BTN_V   = Button("video_play_pause",     PLAY_BTN.rect,   "play/pause", modes=("video",))
NEXT_BTN_V   = Button("video_next_song",      NEXT_BTN.rect,   "next",   modes=("video",))
DELETE_BTN_V = Button("video_delete_current", DELETE_BTN.rect, "delete", modes=("video",))

# Whole middle band: tap to hide/show the overlay so art/video isn't
# obstructed. Lowest priority in hit_test() — every other button rect is
# checked first.
CONTENT_AREA = Button("_toggle_overlay", (0, TOPBAR_H, W, BOTTOMBAR_Y - TOPBAR_H), "content")

# Buttons that exist in BOTH modes but dispatch a different action name per
# mode (curate/prev/play/next/delete). Everything else is mode-agnostic.
_MUSIC_ONLY = (CURATE_BTN, PREV_BTN, PLAY_BTN, NEXT_BTN, DELETE_BTN)
_VIDEO_ONLY = (CURATE_BTN_V, PREV_BTN_V, PLAY_BTN_V, NEXT_BTN_V, DELETE_BTN_V, VOCAL_BTN)
_SHARED = (MODE_PILL, STORAGE_PILL, BT_ICON, AIRPLAY_ICON, SCRUB_BAR, VOLUME_BAR)

ALL_BUTTONS: tuple[Button, ...] = _SHARED + _MUSIC_ONLY + _VIDEO_ONLY + (CONTENT_AREA,)


def buttons_for_mode(mode: str) -> list[Button]:
    """Buttons actually visible/tappable in ``mode`` ('music' or 'video')."""
    if mode == "video":
        return list(_SHARED) + list(_VIDEO_ONLY)
    return list(_SHARED) + list(_MUSIC_ONLY)


def hit_test(x: int, y: int, mode: str) -> Button | None:
    """Which button (if any) logical point (x, y) falls inside.

    CONTENT_AREA is checked last on purpose — every real control sits
    inside the top/bottom bars, which are visually stacked ON TOP of the
    content band in the overlay, so a real control's rect must win before
    the whole-band catch-all gets a chance.
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
