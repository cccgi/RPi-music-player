"""Touch UI button geometry: the single source of truth shared by
render.py (drawing) and touch_daemon.py (hit-testing), so the two can
never drift apart — same rationale as player/streamdeck/layout.py for the
Stream Deck's 8x4 grid.

--------------------------------------------------------------------------
v4: Scan out of the transport row, volume off the bottom bar
--------------------------------------------------------------------------
Two structural changes on top of v3's size hierarchy, both from this
round's feedback on a real hardware photo:

  1. SCAN/RESCAN MOVED OUT OF THE TRANSPORT ROW. v3 put it where the
     design spec's orange "Playlist" slot sits, right next to DELETE —
     flagged explicitly this round as "a dangerous UI collision: do not
     place [REFRESH] [DELETE] next to each other." It's now a small
     circular utility icon in the header's status cluster (SCAN_UTIL /
     SCAN_UTIL_V), spatially far from Delete and grouped with the other
     status/utility controls (source pills, overflow menu) it actually
     belongs with conceptually — it affects the LIBRARY, not the
     currently-playing track.

     This still leaves no real playlist/queue screen behind an orange
     "Playlist" button (out of scope to build one now — same call as
     v3, restated: no feature invented just to fill a slot). The
     transport row simply has one fewer tertiary control than the
     reference mockup's PLAYLIST+DELETE pair; flagged as a documented
     follow-up rather than faked.

  2. VOLUME BAR REMOVED FROM THE BOTTOM OF THE SCREEN. v2/v3 both had a
     full-width draggable VOLUME_BAR along the bottom edge — explicitly
     called out this round as something to remove: "the old large
     volume bar at the bottom MUST BE REMOVED... instead display a
     compact volume indicator in the top/status area" plus swipe
     up/down (already implemented — see touch_daemon.py's
     _handle_content_gesture) as the only way to actually change it.
     VOLUME_COMPACT_RECT (persistent, tiny, header area) and
     VOLUME_HUD_RECT (transient, appears only while a volume swipe is
     live or just ended, then auto-hides — see render.py's
     _draw_volume_hud and touch_daemon's _volume_hud_until) replace it.
     Neither is a Button: both are read-only displays, not drag
     targets — the swipe-anywhere gesture is now the only volume input.

--------------------------------------------------------------------------
v3: control HIERARCHY, not just restyling
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
  TERTIARY  Favorite, Delete — CURATE_BTN[_V], DELETE_BTN[_V]. Small
            (52x52) icon-only squares, visually subordinate on purpose
            (per the design spec: "Do not allow DELETE to visually
            compete with PLAY"). Karaoke/Vocal (video only) is the one
            tertiary control with a text label (VOCAL/KARAOKE toggle
            state has to be readable), so it's sized like a secondary
            control instead.

The transport row's x-positions below are hand-centered as a group per
mode (5 controls in music, 6 in video) — see the inline comments at each
rect for the arithmetic, so a future resize has the reasoning next to the
numbers, not just the numbers.

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
    # switch, route-picker open, absolute seek drag, overlay show/hide)
    # because it has no TourBox/Stream Deck equivalent.
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

# -- top-right source/status/utility cluster (compact — these are STATUS
# controls, not primary playback controls, per the design spec) -------------
# Right-aligned as a group: STORAGE(80) + BT(80) + AP(80) + SCAN(32) +
# DOTS(24) = 296, plus 4x8 gaps = 32 -> 328 total, ending at x=780 (20px
# right margin, matching ART_RECT's 20px left margin) -> starts at 452.
MODE_PILL    = Button("_toggle_mode",    (204, 24, 108, 36), "MODE")
STORAGE_PILL = Button("_toggle_storage", (452, 24, 80, 32), "INT/USB")
BT_PILL      = Button("_open_bt",        (540, 24, 80, 32), "BT")
AIRPLAY_PILL = Button("_open_airplay",   (628, 24, 80, 32), "AirPlay")
# Library rescan (update_database / video_rescan) — moved here in v4,
# OUT of the transport row, specifically so it's nowhere near DELETE (see
# module docstring, item 1: "do not place [REFRESH] [DELETE] next to
# each other"). A small circular utility icon grouped with the other
# status controls it conceptually belongs with.
SCAN_UTIL   = Button("update_database", (716, 24, 32, 32), "scan")
SCAN_UTIL_V = Button("video_rescan",    SCAN_UTIL.rect,    "scan", modes=("video",))
# The 3-dot overflow icon in the top-right corner (spec item 8) is
# deliberately NOT a Button — there's no settings/overflow screen behind
# it yet, and inventing one wasn't asked for. render.py draws it as an
# inert decoration; wire it up here (a real rect + hit_test entry) the day
# there's an actual menu for it to open.
MENU_DOTS_RECT = (756, 24, 24, 32)

# Compact, ALWAYS-VISIBLE volume readout (v4: replaces the old bottom
# VOLUME_BAR) — speaker icon + a handful of tick bars + percentage text,
# sitting in the header band just above the visualizer. Read-only: not a
# Button, nothing to hit-test — see module docstring, item 2.
VOLUME_COMPACT_RECT = (556, 72, 228, 20)

# Decorative spectrum visualizer (spec item 10) — NOT real-time audio
# analysis (no FFT/spectrum data is available to this daemon; see
# render.py's docstring for why a stylized deterministic pattern was used
# instead of faking live data). MUSIC MODE ONLY (spec: video must not show
# it — video prioritizes the video image itself). Non-interactive, no
# Button entry needed.
VISUALIZER_RECT = (556, 96, 228, 56)

# Transient volume HUD (v4) — a centered card that appears only while a
# volume swipe is live or has just ended, then auto-hides after ~1.5s
# (touch_daemon's _volume_hud_until / OverlayState.volume_hud_visible).
# Sized/centered independent of whatever else is on screen since it draws
# on top of everything, including a fully-hidden overlay.
VOLUME_HUD_RECT = (300, 195, 200, 90)

# -- scrub bar ----------------------------------------------------------------
SCRUB_BAR = Button("_seek_absolute", (20, 226, 764, 14), "seek")

# -- bottom transport row: PRIMARY / SECONDARY / TERTIARY hierarchy ----------
# Common row centerline cy=300; each tier is a different height, all
# vertically centered on it (see the module docstring for the hierarchy
# rationale).
#
# Music: 5 controls (favorite/prev/play/next/delete) since v4 moved Scan
# out -> width 52+95+100+95+52=394, plus 4x14 gaps=56 -> 450 total, so it
# starts at (800-450)/2=175 (175px margin each side, symmetric).
CURATE_BTN = Button("curate_current",    (175, 274, 52, 52), "curate")   # tertiary
PREV_BTN   = Button("prev_track",        (241, 270, 95, 60), "prev")    # secondary
PLAY_BTN   = Button("toggle_pause",      (350, 250, 100, 100), "play/pause")  # primary
NEXT_BTN   = Button("next_track",        (464, 270, 95, 60), "next")    # secondary
DELETE_BTN = Button("delete_current",    (573, 274, 52, 52), "delete")  # tertiary
# Video adds Karaoke/Vocal after Delete — needs a text label (state
# toggles between VOCAL/KARAOKE), so it's sized like a secondary control.
# 6 controls -> width 394+112=506, plus 5x14 gaps=70 -> 576 total, starts
# at (800-576)/2=112 (112px margin each side, symmetric).
VOCAL_BTN  = Button("switch_track",      (576, 270, 112, 60), "vocal", modes=("video",))

# Video-mode equivalents of the five music actions above — same rect
# ARITHMETIC as music (shifted as a group from 175-start to 112-start,
# i.e. -63px each), same label, different actions.py entry point.
CURATE_BTN_V = Button("video_curate_current", (112, 274, 52, 52),  "curate", modes=("video",))
PREV_BTN_V   = Button("video_prev_song",      (178, 270, 95, 60),  "prev",   modes=("video",))
PLAY_BTN_V   = Button("video_play_pause",     (287, 250, 100, 100), "play/pause", modes=("video",))
NEXT_BTN_V   = Button("video_next_song",      (401, 270, 95, 60),  "next",   modes=("video",))
DELETE_BTN_V = Button("video_delete_current", (510, 274, 52, 52),  "delete", modes=("video",))

# Whole screen. Plain tap (no direction ever locked in — see
# touch_daemon.py's _on_touch_up) toggles hide/show. A vertical drag
# adjusts volume, a horizontal drag scrubs — see touch_daemon.py's
# _handle_content_gesture(). Lowest priority in hit_test() — every real
# control rect is checked first.
CONTENT_AREA = Button("_toggle_overlay", (0, 0, W, H), "content")

# Buttons that exist in BOTH modes but dispatch a different action name per
# mode. Everything else is mode-agnostic.
#
# NOTE: SCAN_UTIL/SCAN_UTIL_V (like CURATE_BTN/_V etc.) share a rect but
# NOT an action — SCAN_UTIL must stay out of _SHARED, or hit_test() in
# video mode would match it (earlier in the concatenated list) before ever
# reaching SCAN_UTIL_V at the same coordinates, silently calling
# update_database (the music action) instead of video_rescan. Found this
# exact class of bug once already in this file (ART_BADGE vs. CURATE_BTN
# colliding in render.py's by-action dict) — same root cause, different
# spot: two Buttons sharing a rect must never share an action name AND
# both be reachable in the same mode's hit_test list.
_MUSIC_ONLY = (CURATE_BTN, PREV_BTN, PLAY_BTN, NEXT_BTN, DELETE_BTN, ART_BADGE, SCAN_UTIL)
_VIDEO_ONLY = (CURATE_BTN_V, PREV_BTN_V, PLAY_BTN_V, NEXT_BTN_V, DELETE_BTN_V,
               VOCAL_BTN, ART_BADGE_V, SCAN_UTIL_V)
_SHARED = (MODE_PILL, STORAGE_PILL, BT_PILL, AIRPLAY_PILL, SCRUB_BAR)

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
