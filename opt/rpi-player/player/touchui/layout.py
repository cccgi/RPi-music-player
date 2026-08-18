"""Touch UI button geometry: the single source of truth shared by
render.py (drawing) and touch_daemon.py (hit-testing), so the two can
never drift apart — same rationale as player/streamdeck/layout.py for the
Stream Deck's 8x4 grid.

--------------------------------------------------------------------------
v3 redesign: control HIERARCHY, not just restyling
--------------------------------------------------------------------------
v2 gave every control the same pill treatment ("FAVORITE, PREVIOUS, PLAY,
NEXT, DELETE all look like variations of the same generic button" — the
explicit complaint that triggered this pass). v3 sizes each control by
its importance instead of using one shape for everything:

  PRIMARY   Play/Pause — PLAY_BTN[_V]. A big circle, substantially larger
            than everything else (100px vs. 52-95px), the only control
            that glows.
  SECONDARY Previous/Next — PREV_BTN[_V]/NEXT_BTN[_V]. Compact rounded
            rects with icon + text, neutral gray, no accent color.
  TERTIARY  Favorite, Scan (see below), Delete — CURATE_BTN[_V],
            SCAN_ICON[_V], DELETE_BTN[_V]. Small (52x52) icon-only
            squares, visually subordinate on purpose (per the design
            spec: "Do not allow DELETE to visually compete with PLAY").
            Karaoke/Vocal (video only) is the one tertiary control with a
            text label (VOCAL/KARAOKE toggle state has to be readable),
            so it's sized like a secondary control instead.

The transport row's x-positions below are hand-centered as a group per
mode (5 controls in music, 6 in video) — see the redesign notes inline
at each rect for the arithmetic, so a future resize has the reasoning
next to the numbers, not just the numbers.

NOTE ON NAMING: the design spec this round calls for an orange
"PLAYLIST" tertiary control in that position. This app has no playlist
browsing screen to put behind it (out of scope to build one now), so
that slot keeps doing what it already did in v2 — SCAN_ICON, a library
rescan (update_database / video_rescan) so files copied in over SMB get
picked up. It's positioned and colored per the spec's "Playlist" slot,
but kept honestly labeled "SCAN" rather than calling it "Playlist" when
tapping it doesn't open one.

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
ART_RECT = (20, 24, 168, 168)
# Small heart badge overlaid on the art's top-left corner — a second,
# larger tap target for curate/favorite than the tiny tertiary transport
# button, mirroring the mockup exactly (it shows a heart badge ON the
# art). Same action as CURATE_BTN below, just a second rect that reaches
# it — hit_test() doesn't care which rect fired.
ART_BADGE   = Button("curate_current",       (26, 30, 34, 34), "curate")
ART_BADGE_V = Button("video_curate_current", ART_BADGE.rect,   "curate", modes=("video",))

# -- top-right source/status cluster (compact — these are STATUS controls,
# not primary playback controls, per the design spec) -----------------------
MODE_PILL    = Button("_toggle_mode",    (204, 24, 108, 36), "MODE")
STORAGE_PILL = Button("_toggle_storage", (490, 24, 80, 32), "INT/USB")
BT_PILL      = Button("_open_bt",        (578, 24, 80, 32), "BT")
AIRPLAY_PILL = Button("_open_airplay",   (666, 24, 80, 32), "AirPlay")
# The 3-dot overflow icon in the top-right corner (spec item 8) is
# deliberately NOT a Button — there's no settings/overflow screen behind
# it yet, and inventing one wasn't asked for. render.py draws it as an
# inert decoration; wire it up here (a real rect + hit_test entry) the day
# there's an actual menu for it to open.
MENU_DOTS_RECT = (756, 24, 24, 32)

# Decorative spectrum visualizer (spec item 10) — NOT real-time audio
# analysis (no FFT/spectrum data is available to this daemon; see
# render.py's docstring for why a stylized deterministic pattern was used
# instead of faking live data). Non-interactive, no Button entry needed.
VISUALIZER_RECT = (556, 96, 228, 56)

# -- scrub bar ----------------------------------------------------------------
SCRUB_BAR = Button("_seek_absolute", (20, 226, 764, 14), "seek")

# -- bottom transport row: PRIMARY / SECONDARY / TERTIARY hierarchy ----------
# Common row centerline cy=300; each tier is a different height, all
# vertically centered on it (see the module docstring for the hierarchy
# rationale). Music: 6 controls (favorite/prev/play/next/scan/delete),
# horizontally centered as a group -> width 52+95+100+95+52+52=446 plus
# 5x14 gaps=70 -> 516 total, so it starts at 16+(768-516)/2=142 to land
# symmetric margins (142-16=126, 784-658=126).
CURATE_BTN = Button("curate_current",    (142, 274, 52, 52), "curate")   # tertiary
PREV_BTN   = Button("prev_track",        (208, 270, 95, 60), "prev")    # secondary
PLAY_BTN   = Button("toggle_pause",      (317, 250, 100, 100), "play/pause")  # primary
NEXT_BTN   = Button("next_track",        (431, 270, 95, 60), "next")    # secondary
SCAN_ICON  = Button("update_database",   (540, 274, 52, 52), "scan")    # tertiary ("Playlist" slot — see module docstring)
DELETE_BTN = Button("delete_current",    (606, 274, 52, 52), "delete")  # tertiary
# Video adds Karaoke/Vocal after Delete — needs a text label (state
# toggles between VOCAL/KARAOKE), so it's sized like a secondary control.
# 672 + 112 = 784, landing exactly on the right margin.
VOCAL_BTN  = Button("switch_track",      (672, 270, 112, 60), "vocal", modes=("video",))

# Video-mode equivalents of the five music actions above — same rect, same
# label, different actions.py entry point.
CURATE_BTN_V = Button("video_curate_current", CURATE_BTN.rect, "curate", modes=("video",))
PREV_BTN_V   = Button("video_prev_song",      PREV_BTN.rect,   "prev",   modes=("video",))
PLAY_BTN_V   = Button("video_play_pause",     PLAY_BTN.rect,   "play/pause", modes=("video",))
NEXT_BTN_V   = Button("video_next_song",      NEXT_BTN.rect,   "next",   modes=("video",))
SCAN_ICON_V  = Button("video_rescan",         SCAN_ICON.rect,  "scan",   modes=("video",))
DELETE_BTN_V = Button("video_delete_current", DELETE_BTN.rect, "delete", modes=("video",))

# -- volume row ----------------------------------------------------------------
VOLUME_BAR = Button("_volume_absolute", (76, 400, 636, 14), "volume")

# Whole screen. Plain tap (no direction ever locked in — see
# touch_daemon.py's _on_touch_up) toggles hide/show. A vertical drag
# adjusts volume, a horizontal drag scrubs — see touch_daemon.py's
# _handle_content_gesture(). Lowest priority in hit_test() — every real
# control rect is checked first.
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
_MUSIC_ONLY = (CURATE_BTN, PREV_BTN, PLAY_BTN, NEXT_BTN, SCAN_ICON, DELETE_BTN, ART_BADGE)
_VIDEO_ONLY = (CURATE_BTN_V, PREV_BTN_V, PLAY_BTN_V, NEXT_BTN_V, SCAN_ICON_V, DELETE_BTN_V,
               VOCAL_BTN, ART_BADGE_V)
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
