"""Touch UI button geometry: the single source of truth shared by
render.py (drawing) and touch_daemon.py (hit-testing), so the two can
never drift apart — same rationale as player/streamdeck/layout.py for the
Stream Deck's 8x4 grid.

--------------------------------------------------------------------------
v5: full recomposition -- a deliberate grid, not stacked widgets
--------------------------------------------------------------------------
v3/v4 fixed CONTROL HIERARCHY (different shapes per importance tier) and
STRUCTURE (Scan away from Delete, volume off the bottom bar), but a real
hardware photo after those landed showed the underlying problem was never
actually fixed: everything was still anchored to the top of the screen,
leaving roughly a third of the 480px canvas as dead space below the
transport row, while the header felt cramped. v5 is a genuine recompose,
not another round of nudging existing rects:

    y=20            HEADER        mode pill (left), volume readout +
                                   source pills + scan + menu (right)
    y=72-240        CONTENT       168px art panel (left) + title/artist/
                                   album/tags (center) + a small 16-bar
                                   visualizer (upper-right)
    y=254-284       PROGRESS      scrub bar + time labels
    y=314-414       TRANSPORT     favorite / prev / play / next / delete
    y=414-480       (66px of deliberate bottom margin)

Every zone's height was chosen so the WHOLE canvas is used on purpose --
the old layout only used roughly the top 340px and called the remaining
140px "done"; here the gaps between zones (14-30px) and the 66px bottom
margin are close in scale to each other, which is what makes empty space
read as "designed" rather than "leftover after moving things around."

Two other concrete v5 changes:

  * VOLUME_COMPACT_RECT got real room (170x38, up from 228x20) instead of
    cramming a speaker icon + bars + percentage into a 20px-tall sliver.
  * VOLUME_HUD_RECT moved into the CONTENT zone (y=90-180) instead of
    dead center of the screen, specifically so it can never overlap the
    transport row -- a real bug in v4 (the HUD sat directly over
    Play/Prev/Next during a swipe).

--------------------------------------------------------------------------
v4: Scan out of the transport row, volume off the bottom bar
--------------------------------------------------------------------------
  1. SCAN/RESCAN MOVED OUT OF THE TRANSPORT ROW into the header's status
     cluster as a small circular utility icon (SCAN_UTIL/SCAN_UTIL_V) --
     specifically so it's never adjacent to DELETE ("a dangerous UI
     collision"). Still no real playlist/queue screen behind an orange
     "Playlist" slot -- out of scope, documented rather than faked.
  2. VOLUME BAR REMOVED FROM THE BOTTOM OF THE SCREEN. Swipe up/down
     (touch_daemon.py's _handle_content_gesture) is the only volume
     input now; VOLUME_COMPACT_RECT/VOLUME_HUD_RECT are read-only
     displays, not Buttons.

--------------------------------------------------------------------------
v3: control HIERARCHY, not just restyling
--------------------------------------------------------------------------
Controls are sized by importance, not one shape for everything:

  PRIMARY   Play/Pause -- PLAY_BTN[_V]. A large circle, the only control
            that glows (kept subtle per this round's feedback -- v4/v3
            both leaned too hard on glow).
  SECONDARY Previous/Next -- PREV_BTN[_V]/NEXT_BTN[_V]. Compact rounded
            rects with icon + text, neutral gray.
  TERTIARY  Favorite, Delete -- CURATE_BTN[_V], DELETE_BTN[_V]. Small
            icon-only squares, visually subordinate on purpose. Karaoke/
            Vocal (video only) needs a text label, so it's sized like a
            secondary control instead.

The transport row's x-positions are hand-centered as a group per mode (5
controls in music, 6 in video) -- see the inline comments at each rect for
the arithmetic.

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


# -- HEADER zone: y 20-58 (button HIT rects are 44 tall, y20-64 -- see note) --
# Header pills are visually drawn within the 38px-tall header band, but
# each Button's rect (the tap-hit area) is grown to 44px tall, the touch
# target minimum this spec calls for ("touch targets >=44px even if
# visuals are minimal") -- there's an 8px gap before CONTENT starts at
# y=72, so this costs nothing layout-wise. Verified via the touch-target
# check alongside the collision/bounds checks (all in the sandbox's
# preview harness) -- see the delivery notes for the one deliberate
# exception (SCRUB_BAR, a thin scrub/slider, kept below 44px tall on
# purpose; see its own comment).
MODE_PILL = Button("_toggle_mode", (20, 20, 112, 44), "MODE")

# Compact, ALWAYS-VISIBLE volume readout (speaker icon + tick bars +
# percentage) -- read-only, not a Button (no touch-target requirement
# applies). Sits in the gap between the mode pill and the source-pill
# cluster, with real room this round (170x38 vs v4's cramped 228x20) per
# the spec: "give it enough breathing room."
VOLUME_COMPACT_RECT = (242, 20, 170, 38)

# Right-aligned status/utility cluster: STORAGE(84) + BT(84) + AP(84) +
# SCAN(44) + DOTS(24) = 320, plus 4x10 gaps = 40 -> 360 total, ending at
# x=788. Hit-rect heights are 44 (see MODE_PILL's comment above); visual
# drawing still uses the 38px header band.
STORAGE_PILL = Button("_toggle_storage", (428, 20, 84, 44), "INT/USB")
BT_PILL      = Button("_open_bt",        (522, 20, 84, 44), "BT")
AIRPLAY_PILL = Button("_open_airplay",   (616, 20, 84, 44), "AirPlay")
# Library rescan (update_database / video_rescan) -- a small circular
# utility icon, deliberately far from DELETE (see module docstring).
# Widened from 36 to 44 for the same touch-target reason as the pills
# above; still comfortably short of MENU_DOTS_RECT at x=756 (2px gap).
SCAN_UTIL   = Button("update_database", (710, 20, 44, 44), "scan")
SCAN_UTIL_V = Button("video_rescan",    SCAN_UTIL.rect,    "scan", modes=("video",))
# The 3-dot overflow icon is deliberately NOT a Button -- no settings
# screen exists behind it yet. render.py draws it as an inert decoration.
MENU_DOTS_RECT = (756, 20, 24, 38)

# -- CONTENT zone: y 72-240 ---------------------------------------------------
ART_RECT = (20, 72, 168, 168)
# Small heart badge overlaid on the art's top-left corner -- a second,
# larger tap target for curate/favorite than the tiny tertiary transport
# button. Same action as CURATE_BTN, just a second rect that reaches it.
# 44x44 (touch-target minimum) fits comfortably inside the 168x168 art
# panel without reaching its opposite edges.
ART_BADGE   = Button("curate_current",       (26, 78, 44, 44), "curate")
ART_BADGE_V = Button("video_curate_current", ART_BADGE.rect,   "curate", modes=("video",))

# Title/artist/album/tags column starts here (art ends at x=188, +20 gap).
INFO_COLUMN_X = 208

# Real audio-reactive visualizer (MUSIC ONLY -- see audio_visualizer.py
# and render.py's module docstring for where the data comes from and why
# video never shows it). Small and upper-right of the content zone, per
# spec: "occupy a relatively small area... complement the artwork rather
# than compete with it." 16 bars, not 24+.
VISUALIZER_RECT = (568, 72, 212, 52)
VISUALIZER_BAR_COUNT = 16

# Transient volume HUD -- appears only while a volume swipe is live or has
# just ended (~1.5s hold, see touch_daemon._volume_hud_until), then
# disappears. Positioned INSIDE the content zone (never over the
# transport row) -- v4 got this wrong (HUD sat on top of Play/Prev/Next
# during a swipe); this rect physically cannot reach y=314+ where
# transport starts.
#
# Width widened from v5's first pass (200) to 360: a preview render with
# a real-length title ("Bohemian Rhapsody") showed the old narrower card
# clipping the title text at both edges -- letters peeking out to the
# left and right of the HUD card instead of the card cleanly covering it
# -- which read as broken, not intentional. 360 covers the full
# _ellipsize()'d title width (text_max_w = VISUALIZER_RECT[0] -
# INFO_COLUMN_X - 16 = 344px, see render.py's _draw_header) with margin
# to spare, while staying clear of both ART_RECT (ends x=188) and
# VISUALIZER_RECT (starts x=568).
VOLUME_HUD_RECT = (200, 90, 360, 90)

# -- PROGRESS zone: y 254-284 -------------------------------------------------
SCRUB_BAR = Button("_seek_absolute", (20, 270, 764, 14), "seek")

# -- TRANSPORT zone: y 314-414, hierarchy per module docstring ---------------
# Common centerline cy=364. Music: 5 controls (favorite/prev/play/next/
# delete) -> width 58+104+100+104+58=424, plus 4x16 gaps=64 -> 488 total,
# starts at (800-488)/2=156 (symmetric 156px margins).
CURATE_BTN = Button("curate_current",    (156, 335, 58, 58), "curate")   # tertiary
PREV_BTN   = Button("prev_track",        (230, 331, 104, 66), "prev")   # secondary
PLAY_BTN   = Button("toggle_pause",      (350, 314, 100, 100), "play/pause")  # primary
NEXT_BTN   = Button("next_track",        (466, 331, 104, 66), "next")   # secondary
DELETE_BTN = Button("delete_current",    (586, 335, 58, 58), "delete")  # tertiary
# Video adds Karaoke/Vocal after Delete -- needs a text label, sized like
# a secondary control. 6 controls -> 488+16+124=628 total, starts at
# (800-628)/2=86 (symmetric 86px margins).
VOCAL_BTN = Button("switch_track", (590, 331, 124, 66), "vocal", modes=("video",))

# Video-mode equivalents of the five music actions above -- same
# arithmetic, shifted as a group by -70px (156->86), same label, different
# actions.py entry point.
CURATE_BTN_V = Button("video_curate_current", (86, 335, 58, 58),   "curate", modes=("video",))
PREV_BTN_V   = Button("video_prev_song",      (160, 331, 104, 66), "prev",   modes=("video",))
PLAY_BTN_V   = Button("video_play_pause",     (280, 314, 100, 100), "play/pause", modes=("video",))
NEXT_BTN_V   = Button("video_next_song",      (396, 331, 104, 66), "next",   modes=("video",))
DELETE_BTN_V = Button("video_delete_current", (516, 335, 58, 58),  "delete", modes=("video",))

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
# update_database (the music action) instead of video_rescan. Two Buttons
# sharing a rect must never share an action name AND both be reachable in
# the same mode's hit_test list.
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
