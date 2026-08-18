"""Touch UI overlay renderer: current playback state -> a BGRA bitmap mpv
composites over whatever it's currently showing (idle/black in music mode,
a real frame in video mode) via IPC ``overlay-add``.

--------------------------------------------------------------------------
v3: control HIERARCHY + restrained "premium hardware" palette
--------------------------------------------------------------------------
v2 gave every control the same bright filled-pill treatment ("looks like a
basic prototype... every control looks like a variation of the same
generic pill button" — the explicit complaint that triggered this pass).
v3 follows a detailed design spec built around a supplied reference
mockup, with three real differences from v2:

  1. SIZE hierarchy: Play/Pause is a large, clearly dominant circle;
     Previous/Next are mid-sized pills; Favorite/Scan/Delete are small
     icon-only squares (see touchui/layout.py's module docstring for the
     exact rects and reasoning — this file just draws what that file
     positions).
  2. COLOR hierarchy: color is used SPARINGLY. Most surfaces are dark
     neutral gray (Theme.surface/elevated); only a handful of elements
     ever get an accent color (the mode pill, an actively-connected route
     pill, Play, Favorite, the Scan/"Playlist"-slot button, Karaoke, and
     Delete ONLY while actually being held down — see
     touch_daemon.py's ``_pressed_action`` and the module docstring in
     layout.py for why Delete stays neutral until touched).
  3. Glow is reserved for Play alone, and kept subtle (a single soft
     halo, not the punchier multi-layer version from the previous pass).

VIDEO mode stays translucent — the ORIGINAL request that predates this
design spec ("I need this UI to be wireframe... so I can see through the
video being played") still applies there, since the actual video is
playing full-screen underneath the overlay in that mode; the supplied
spec doesn't address that constraint (it's a generic media-player brief,
not aware of the mpv-overlay architecture this app is built on), so this
file keeps making that same explicit trade-off: MUSIC mode is fully
opaque (nothing plays behind it — mpv just holds a muted idle-black clip,
so there's nothing to preserve visibility of), VIDEO mode stays
translucent. Every draw call takes an ``opaque`` bool for this reason.

No real album art / video-frame thumbnail is fetched (needs MPD's
`albumart`/`readpicture` binary protocol, or an ffmpeg frame grab for
video, with its own caching — scoped out, flagged as a follow-up). The
"visualizer" is likewise NOT real spectrum analysis — this daemon has no
access to decoded audio samples, only MPD/mpv's transport status — so
rather than fake a live FFT it draws a stylized, deterministic bar
pattern derived from elapsed playback time, purely decorative, and never
claims to represent the actual audio.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import layout

LOG = logging.getLogger(__name__)

_FALLBACK_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FALLBACK_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@dataclass
class Theme:
    # Backdrop (music mode only — see module docstring): nearly-black,
    # subtle gradient from bg to bg_elevated.
    bg: str = "#080C12"
    surface: str = "#101620"
    elevated: str = "#161D28"
    border: str = "#2A3440"
    fg: str = "#F5F7FA"
    fg_secondary: str = "#AEB7C4"
    muted: str = "#707B89"
    accent: str = "#2684FF"         # music accent (blue)
    accent_video: str = "#A855F7"   # video accent (purple)
    active: str = "#22C55E"         # connected/active route (green)
    favorite: str = "#FF4D7D"
    scan: str = "#FF9F1C"           # "Playlist" slot color — see layout.py
    danger: str = "#FF4D4D"         # Delete, only while armed/held
    font_regular: str = _FALLBACK_REGULAR
    font_bold: str = _FALLBACK_BOLD


@dataclass
class OverlayState:
    mode: str = "music"                 # "music" | "video"
    title: str = ""
    artist: str = ""                    # or a fixed sub-label in video mode
    meta_line: str = ""                 # music: "Album (Year)"; video: unused
    tags: list = field(default_factory=list)  # format chips, e.g. ["FLAC", "24-bit", "96 kHz", "2ch"]
    elapsed: float = 0.0
    duration: float = 0.0
    volume: int = 0
    playing: bool = False
    storage: str = "INT"                # "INT" | "USB"
    route_icon: str = ""                # "bluetooth" | "airplay" | "local" | ""
    bt_available: bool = False
    airplay_available: bool = False
    curated: bool = False
    vocal_active: bool = True           # video mode only: True=Vocal, False=Karaoke
    overlay_visible: bool = True        # whole overlay hidden -> render fully transparent
    pressed_action: str = ""            # action name of the currently-held-down button, if any


def _hex_rgba(h: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


class Renderer:
    """Holds loaded fonts across calls — avoids re-hitting disk every frame."""

    def __init__(self, theme: Theme) -> None:
        self._theme = theme
        self._font_title = self._load(theme.font_bold, 28)
        self._font_sub = self._load(theme.font_regular, 17)
        self._font_meta = self._load(theme.font_regular, 14)
        self._font_pill = self._load(theme.font_bold, 14)
        self._font_tag = self._load(theme.font_bold, 12)
        self._font_time = self._load(theme.font_regular, 13)

    def _load(self, path: str, size: int) -> ImageFont.FreeTypeFont:
        for candidate in (path, _FALLBACK_REGULAR):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        LOG.warning("no usable font found (tried %s, %s) — falling back to PIL default",
                    path, _FALLBACK_REGULAR)
        return ImageFont.load_default()

    # -- public --------------------------------------------------------------

    def render(self, state: OverlayState, rotate_180: bool) -> bytes:
        """Return raw BGRA bytes, W*H*4, ready for mpv's overlay-add."""
        img = Image.new("RGBA", (layout.W, layout.H), (0, 0, 0, 0))
        if state.overlay_visible:
            opaque = state.mode == "music"
            accent = self._theme.accent_video if state.mode == "video" else self._theme.accent
            if opaque:
                self._draw_backdrop(img)
            draw = ImageDraw.Draw(img)
            self._draw_art_panel(draw, state, accent, opaque)
            self._draw_header(draw, state, accent, opaque)
            self._draw_visualizer(draw, state, accent)
            self._draw_scrub(draw, state, accent, opaque)
            self._draw_transport(img, draw, state, accent, opaque)
            self._draw_volume(draw, state, accent, opaque)
        if rotate_180:
            img = img.transpose(Image.ROTATE_180)
        # mpv's overlay-add "bgra" format wants byte order B,G,R,A per
        # pixel. Pillow's raw encoder can do this conversion directly
        # without a manual channel-swap loop.
        return img.tobytes("raw", "BGRA")

    # -- shared drawing helpers ------------------------------------------------

    def _draw_backdrop(self, img: Image.Image) -> None:
        """Subtle vertical gradient backdrop — music mode only (see module
        docstring). Pillow has no built-in RGBA gradient fill; one
        horizontal line per row is trivial at 480 rows and this only runs
        on frames that actually change (roughly once a second, or during
        a drag), nowhere near a hot path.
        """
        top = _hex_rgba(self._theme.bg)
        bottom = _hex_rgba(self._theme.elevated)
        draw = ImageDraw.Draw(img)
        h = layout.H
        for y in range(h):
            f = y / max(1, h - 1)
            row = tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(4))
            draw.line([(0, y), (layout.W, y)], fill=row)

    def _glow(self, img: Image.Image, cx: float, cy: float, r: float, color: tuple, blur: int = 10) -> None:
        """ONE soft blurred halo — reserved for Play alone. Kept subtle
        per the design spec ("glow must be subtle... only important
        active controls should glow"): a single blur layer extending
        roughly 10-20px past the button, not a multi-layer neon effect.
        """
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
        img.alpha_composite(layer)

    def _shadowed_text(self, draw, pos, text, font, fill):
        x, y = pos
        draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, 170))
        draw.text((x, y), text, font=font, fill=fill)

    def _secondary_pill(self, draw, rect, icon_fn, text, accent=None, active=False,
                         muted=False, opaque=True) -> None:
        """Mid-tier control (Previous/Next/Karaoke): a rounded rect with
        icon + text. Neutral dark-gray surface by default — only Karaoke
        (an ``accent`` is passed) or an ``active`` toggle state gets any
        color at all, per the spec's "everything else = neutral gray".
        """
        t = self._theme
        x, y, w, h = rect
        base_alpha = 235 if opaque else 60
        border_alpha = 200 if opaque else 90
        if muted:
            border = _hex_rgba(t.muted, border_alpha * 0.6)
            fill = _hex_rgba(t.surface, base_alpha * 0.7)
            fg = _hex_rgba(t.muted, 210)
        elif active and accent:
            border = _hex_rgba(accent, 255)
            fill = _hex_rgba(accent, 70 if opaque else 50)
            fg = _hex_rgba(t.fg)
        else:
            border = _hex_rgba(t.border, border_alpha)
            fill = _hex_rgba(t.elevated, base_alpha)
            fg = _hex_rgba(t.fg_secondary, 235)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=12, outline=border, width=2, fill=fill)

        cy = y + h / 2
        icon_s = h * 0.24
        pad = 14
        if icon_fn is not None:
            icon_cx = x + pad + icon_s * 0.5
            icon_fn(draw, icon_cx, cy, icon_s, fg)
            text_x = icon_cx + icon_s * 1.3
        else:
            text_x = x + pad
        if text:
            draw.text((text_x, cy), text, font=self._font_pill, fill=fg, anchor="lm")

    def _tertiary_icon_btn(self, draw, rect, icon_fn, accent=None, active=False,
                            armed=False, opaque=True) -> None:
        """Smallest tier (Favorite/Scan/Delete): an icon-only rounded
        square, visually subordinate on purpose — "do not allow DELETE to
        visually compete with PLAY". Neutral by default; ``accent`` colors
        it only when ``active``/``armed`` (Delete's red only shows up
        while actually held down, not at rest).
        """
        t = self._theme
        x, y, w, h = rect
        base_alpha = 235 if opaque else 60
        if armed:
            border = _hex_rgba(t.danger, 255)
            fill = _hex_rgba(t.danger, 90 if opaque else 70)
            fg = _hex_rgba(t.fg)
        elif active and accent:
            border = _hex_rgba(accent, 230)
            fill = _hex_rgba(accent, 55 if opaque else 45)
            fg = _hex_rgba(accent, 255) if not opaque else _hex_rgba(t.fg)
        else:
            border = _hex_rgba(t.border, 200 if opaque else 90)
            fill = _hex_rgba(t.elevated, base_alpha)
            fg = _hex_rgba(t.fg_secondary, 230)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=12, outline=border, width=2, fill=fill)
        icon_fn(draw, x + w / 2, y + h / 2, h * 0.26, fg)

    # -- album art panel ---------------------------------------------------------

    def _draw_art_panel(self, draw, state: OverlayState, accent: str, opaque: bool) -> None:
        """No real album art / video thumbnail fetch yet (see module
        docstring) — a themed rounded panel with a music-note or film-reel
        icon stands in for it, plus the heart badge the mockup overlays on
        the art's corner (also a real, tappable curate button — see
        layout.ART_BADGE).
        """
        t = self._theme
        x, y, w, h = layout.ART_RECT
        panel_alpha = 255 if opaque else 200
        draw.rounded_rectangle([x, y, x + w, y + h], radius=16,
                                fill=_hex_rgba(t.elevated, panel_alpha),
                                outline=_hex_rgba(t.border, 220), width=2)
        cx, cy = x + w / 2, y + h / 2
        icon_alpha = 220 if opaque else 190
        if state.mode == "video":
            _icon_film(draw, cx, cy, w * 0.2, _hex_rgba(accent, icon_alpha))
        else:
            _icon_music_note(draw, cx, cy, w * 0.2, _hex_rgba(accent, icon_alpha))

        # heart badge, top-left corner of the art
        bx, by, bw, bh = layout.ART_BADGE.rect
        badge_fill = _hex_rgba(t.favorite, 235) if state.curated else _hex_rgba(t.bg, 215)
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=9, fill=badge_fill,
                                outline=_hex_rgba(t.favorite, 235), width=2)
        _heart_glyph(draw, bx + bw / 2, by + bh / 2, bw * 0.3, _hex_rgba(t.fg), filled=state.curated)

    # -- header: mode/source pills, title, tags --------------------------------

    def _draw_header(self, draw, state: OverlayState, accent: str, opaque: bool) -> None:
        t = self._theme

        mode_icon = _icon_film if state.mode == "video" else _icon_music_note
        mode_label = "VIDEO" if state.mode == "video" else "MUSIC"
        x, y, w, h = layout.MODE_PILL.rect
        draw.rounded_rectangle([x, y, x + w, y + h], radius=h / 2,
                                outline=_hex_rgba(accent, 255), width=2,
                                fill=_hex_rgba(accent, 210 if opaque else 55))
        icon_cx = x + 16 + h * 0.13
        mode_icon(draw, icon_cx, y + h / 2, h * 0.26, _hex_rgba(t.fg))
        draw.text((icon_cx + h * 0.4, y + h / 2), mode_label, font=self._font_pill,
                  fill=_hex_rgba(t.fg), anchor="lm")

        # source/status cluster — compact, mostly neutral; color appears
        # ONLY on an actively-connected route (spec: "do not make every
        # source button brightly colored simultaneously").
        self._source_pill(draw, layout.STORAGE_PILL.rect, _icon_drive, state.storage, opaque=opaque)
        self._source_pill(draw, layout.BT_PILL.rect, _icon_bluetooth, "BT", opaque=opaque,
                           active=state.route_icon == "bluetooth",
                           muted=not state.bt_available and state.route_icon != "bluetooth")
        self._source_pill(draw, layout.AIRPLAY_PILL.rect, _icon_airplay, "AP", opaque=opaque,
                           active=state.route_icon == "airplay",
                           muted=not state.airplay_available and state.route_icon != "airplay")
        _menu_dots(draw, layout.MENU_DOTS_RECT, _hex_rgba(t.muted, 200))

        title_x = layout.MODE_PILL.rect[0]
        title = state.title or ("Nothing playing" if state.mode == "music" else "No video loaded")
        self._shadowed_text(draw, (title_x, 74), title, self._font_title, _hex_rgba(t.fg))
        if state.artist:
            self._shadowed_text(draw, (title_x, 110), state.artist, self._font_sub,
                                 _hex_rgba(t.fg_secondary, 235))
        if state.meta_line:
            self._shadowed_text(draw, (title_x, 134), state.meta_line, self._font_meta,
                                 _hex_rgba(t.muted, 235))

        # format-tag chips (FLAC/24-bit/96kHz/2ch, or 1920x1080/H.264/16:9/29.97fps)
        tx = title_x
        ty = 162
        for tag in state.tags:
            tw = draw.textlength(tag, font=self._font_tag) + 16
            draw.rounded_rectangle([tx, ty, tx + tw, ty + 22], radius=6,
                                    fill=_hex_rgba(t.elevated, 220 if opaque else 60),
                                    outline=_hex_rgba(t.border, 220), width=1)
            draw.text((tx + 8, ty + 11), tag, font=self._font_tag, fill=_hex_rgba(t.fg_secondary, 235),
                      anchor="lm")
            tx += tw + 8

    def _source_pill(self, draw, rect, icon_fn, text, opaque, active=False, muted=False) -> None:
        t = self._theme
        x, y, w, h = rect
        if active:
            border = _hex_rgba(t.active, 255)
            fill = _hex_rgba(t.active, 60 if opaque else 45)
            fg = _hex_rgba(t.fg)
        elif muted:
            border = _hex_rgba(t.border, 140)
            fill = _hex_rgba(t.surface, 150 if opaque else 40)
            fg = _hex_rgba(t.muted, 200)
        else:
            border = _hex_rgba(t.border, 220)
            fill = _hex_rgba(t.elevated, 220 if opaque else 55)
            fg = _hex_rgba(t.fg_secondary, 230)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, outline=border, width=2, fill=fill)
        icon_s = h * 0.28
        icon_cx = x + 12 + icon_s * 0.5
        icon_fn(draw, icon_cx, y + h / 2, icon_s, fg)
        draw.text((icon_cx + icon_s * 1.15, y + h / 2), text, font=self._font_tag, fill=fg, anchor="lm")

    # -- decorative "spectrum" strip -----------------------------------------------

    def _draw_visualizer(self, draw, state: OverlayState, accent: str) -> None:
        """Stylized, deterministic bar pattern — NOT real audio analysis
        (see module docstring). Derived from elapsed playback time so it
        visibly shifts while something is playing and sits flat/still
        when nothing is, without ever being presented as a real spectrum.
        """
        t = self._theme
        x, y, w, h = layout.VISUALIZER_RECT
        n_bars = 24
        bar_w = w / n_bars * 0.6
        gap = w / n_bars
        moving = state.mode == "music" and state.playing or state.mode == "video" and state.playing
        phase = state.elapsed if moving else 0.0
        for i in range(n_bars):
            # Smooth, non-repeating-looking pseudo-pattern from a couple
            # of out-of-phase sine waves — deliberately NOT random per
            # frame (that would just look like flicker/noise).
            v = (math.sin(phase * 2.2 + i * 0.9) * 0.5
                 + math.sin(phase * 1.3 + i * 0.4) * 0.3
                 + 0.5)
            v = max(0.08, min(1.0, v))
            bh = h * v
            bx = x + i * gap
            draw.rounded_rectangle([bx, y + h - bh, bx + bar_w, y + h], radius=bar_w / 2,
                                    fill=_hex_rgba(accent, 190))

    # -- scrub bar ---------------------------------------------------------------

    def _draw_scrub(self, draw, state: OverlayState, accent: str, opaque: bool) -> None:
        t = self._theme
        sx, sy, sw, sh = layout.SCRUB_BAR.rect
        track_y = sy + sh / 2
        self._shadowed_text(draw, (sx, sy - 20), _fmt_time(state.elapsed), self._font_time,
                             _hex_rgba(t.fg_secondary, 220))
        dur_txt = _fmt_time(state.duration)
        dw = draw.textlength(dur_txt, font=self._font_time)
        self._shadowed_text(draw, (sx + sw - dw, sy - 20), dur_txt, self._font_time,
                             _hex_rgba(t.fg_secondary, 220))

        draw.line([(sx, track_y), (sx + sw, track_y)], fill=_hex_rgba(t.border, 220), width=3)
        frac = 0.0
        if state.duration > 0:
            frac = max(0.0, min(1.0, state.elapsed / state.duration))
        fill_x = sx + sw * frac
        if frac > 0:
            draw.line([(sx, track_y), (fill_x, track_y)], fill=_hex_rgba(accent, 255), width=3)
        draw.ellipse([fill_x - 6, track_y - 6, fill_x + 6, track_y + 6],
                     fill=_hex_rgba(t.fg), outline=_hex_rgba(accent, 255), width=2)

    # -- transport row -------------------------------------------------------------

    def _draw_transport(self, img, draw, state: OverlayState, accent: str, opaque: bool) -> None:
        """NOTE: curate/prev/play/next/delete each have TWO Button entries
        sharing an action name in some cases (the transport-row control
        here, plus ART_BADGE on the album art for curate specifically) —
        do NOT look these up via a by_action={b.action: b for b in
        buttons} dict the way the header does for BT/AP. Two rects with
        the same action collide in that dict (whichever is last in the
        tuple wins) — reference layout.CURATE_BTN[_V] etc. directly.
        """
        t = self._theme
        video = state.mode == "video"
        curate_rect = layout.CURATE_BTN_V.rect if video else layout.CURATE_BTN.rect
        prev_rect = layout.PREV_BTN_V.rect if video else layout.PREV_BTN.rect
        play_rect = layout.PLAY_BTN_V.rect if video else layout.PLAY_BTN.rect
        next_rect = layout.NEXT_BTN_V.rect if video else layout.NEXT_BTN.rect
        scan_rect = layout.SCAN_ICON_V.rect if video else layout.SCAN_ICON.rect
        delete_rect = layout.DELETE_BTN_V.rect if video else layout.DELETE_BTN.rect
        delete_action = "video_delete_current" if video else "delete_current"

        # tertiary: icon-only, subordinate
        self._tertiary_icon_btn(draw, curate_rect,
                                 lambda d, cx, cy, s, fg: _heart_glyph(d, cx, cy, s, fg, filled=state.curated),
                                 accent=t.favorite, active=state.curated, opaque=opaque)
        self._tertiary_icon_btn(draw, scan_rect, _scan_glyph, accent=t.scan, active=True, opaque=opaque)
        self._tertiary_icon_btn(draw, delete_rect, _icon_trash, armed=state.pressed_action == delete_action,
                                 opaque=opaque)

        # secondary: icon + text, neutral
        self._secondary_pill(draw, prev_rect,
                              lambda d, cx, cy, s, fg: _skip_glyph(d, cx, cy, s, fg, direction=-1),
                              "PREV", opaque=opaque)
        self._secondary_pill(draw, next_rect,
                              lambda d, cx, cy, s, fg: _skip_glyph(d, cx, cy, s, fg, direction=1),
                              "NEXT", opaque=opaque)
        if video:
            label = "VOCAL" if state.vocal_active else "KARAOKE"
            self._secondary_pill(draw, layout.VOCAL_BTN.rect, _icon_mic, label,
                                  accent=t.accent_video, active=True, opaque=opaque)

        # primary: Play/Pause — the one dominant, glowing control
        x, y, w, h = play_rect
        cx, cy = x + w / 2, y + h / 2
        r = max(w, h) / 2
        self._glow(img, cx, cy, r * 1.25, _hex_rgba(accent, 130 if opaque else 90), blur=10)
        draw.ellipse([x, y, x + w, y + h], fill=_hex_rgba(accent, 235 if opaque else 90),
                     outline=_hex_rgba(accent, 255), width=2)
        s = h * 0.24
        if state.playing:
            bar_w = s * 0.55
            draw.rounded_rectangle([cx - s * 0.8, cy - s, cx - s * 0.8 + bar_w, cy + s],
                                    radius=bar_w * 0.3, fill=_hex_rgba(t.fg))
            draw.rounded_rectangle([cx + s * 0.25, cy - s, cx + s * 0.25 + bar_w, cy + s],
                                    radius=bar_w * 0.3, fill=_hex_rgba(t.fg))
        else:
            draw.polygon([(cx - s * 0.55, cy - s), (cx - s * 0.55, cy + s), (cx + s * 0.95, cy)],
                         fill=_hex_rgba(t.fg))

    # -- volume row ----------------------------------------------------------------

    def _draw_volume(self, draw, state: OverlayState, accent: str, opaque: bool) -> None:
        t = self._theme
        vx, vy, vw, vh = layout.VOLUME_BAR.rect
        track_y = vy + vh / 2
        _icon_speaker(draw, vx - 30, track_y, vh * 0.9, _hex_rgba(t.fg_secondary, 220),
                      muted=state.volume <= 0)

        draw.line([(vx, track_y), (vx + vw, track_y)], fill=_hex_rgba(t.border, 220), width=3)
        vfrac = max(0.0, min(1.0, state.volume / 100.0))
        vfill_x = vx + vw * vfrac
        if vfrac > 0:
            draw.line([(vx, track_y), (vfill_x, track_y)], fill=_hex_rgba(accent, 255), width=3)
        draw.ellipse([vfill_x - 6, track_y - 6, vfill_x + 6, track_y + 6],
                     fill=_hex_rgba(t.fg), outline=_hex_rgba(accent, 255), width=2)
        pct = f"{state.volume}%"
        self._shadowed_text(draw, (vx + vw + 12, track_y - 8), pct, self._font_time,
                             _hex_rgba(t.fg_secondary, 220))


# -- standalone vector icon helpers (module-level: no per-button state needed) --

def _heart_glyph(draw, cx, cy, s, fg, filled: bool) -> None:
    """A real heart outline (parametric heart curve). Filled only when
    curated=True — a stronger, more universally understood "liked" signal
    than an outline heart.
    """
    n = 48
    raw = []
    for i in range(n):
        t = 2 * math.pi * i / n
        hx = 16 * math.sin(t) ** 3
        hy = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        raw.append((hx, -hy))
    xs = [p[0] for p in raw]
    ys = [p[1] for p in raw]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    target = s * 2.0
    scale = target / max(maxx - minx, maxy - miny)
    ox = cx - (minx + maxx) / 2 * scale
    oy = cy - (miny + maxy) / 2 * scale
    pts = [(ox + px * scale, oy + py * scale) for px, py in raw]
    draw.polygon(pts, outline=fg, width=2, fill=fg if filled else None)


def _skip_glyph(draw, cx, cy, s, fg, direction: int) -> None:
    """Standard "skip to next/previous" icon: two triangles + an end bar
    (⏭ / ⏮), built symmetrically from one direction sign.
    """
    tri_h = s
    tri_w = s * 0.62
    gap = tri_w * 0.18
    x0 = cx - direction * (tri_w + gap / 2)
    x1 = x0 + direction * tri_w
    draw.polygon([(x0, cy - tri_h), (x0, cy + tri_h), (x1, cy)], fill=fg)
    x2 = x1 + direction * gap
    x3 = x2 + direction * tri_w
    draw.polygon([(x2, cy - tri_h), (x2, cy + tri_h), (x3, cy)], fill=fg)
    bar_x = x3 + direction * (gap * 0.5)
    draw.line([(bar_x, cy - tri_h), (bar_x, cy + tri_h)], fill=fg, width=3)


def _scan_glyph(draw, cx, cy, s, fg) -> None:
    """Refresh/rescan icon: a near-complete circular arc with a single
    arrowhead — the standard "re-scan / reload" visual metaphor.
    """
    r = s * 0.85
    bbox = [cx - r, cy - r, cx + r, cy + r]
    draw.arc(bbox, start=20, end=330, fill=fg, width=2)
    ang = math.radians(20)
    tipx = cx + r * math.cos(ang)
    tipy = cy + r * math.sin(ang)
    ah = s * 0.38
    draw.polygon([
        (tipx + ah * 0.55, tipy - ah * 0.15),
        (tipx - ah * 0.55, tipy + ah * 0.25),
        (tipx + ah * 0.05, tipy + ah * 0.75),
    ], fill=fg)


def _icon_trash(draw, cx, cy, s, fg) -> None:
    """Trash-can: lid line + handle + body outline with two vertical
    "ridge" lines.
    """
    body_w, body_h = s * 1.3, s * 1.4
    bx0, by0 = cx - body_w / 2, cy - body_h / 2 + s * 0.25
    bx1, by1 = cx + body_w / 2, cy + body_h / 2
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=3, outline=fg, width=2)
    draw.line([(bx0 - 3, by0), (bx1 + 3, by0)], fill=fg, width=2)
    draw.line([(cx - body_w * 0.2, by0), (cx - body_w * 0.15, by0 - 5)], fill=fg, width=2)
    draw.line([(cx + body_w * 0.2, by0), (cx + body_w * 0.15, by0 - 5)], fill=fg, width=2)
    draw.line([(cx - body_w * 0.18, by0 + 5), (cx - body_w * 0.18, by1 - 5)], fill=fg, width=2)
    draw.line([(cx + body_w * 0.18, by0 + 5), (cx + body_w * 0.18, by1 - 5)], fill=fg, width=2)


def _icon_music_note(draw, cx, cy, s, fg) -> None:
    stem_x = cx + s * 0.35
    draw.line([(stem_x, cy - s * 1.1), (stem_x, cy + s * 0.55)], fill=fg, width=3)
    draw.line([(stem_x, cy - s * 1.1), (stem_x + s * 0.7, cy - s * 0.85)], fill=fg, width=3)
    draw.ellipse([stem_x - s * 0.55, cy + s * 0.15, stem_x - s * 0.05, cy + s * 0.65],
                 outline=fg, width=2)


def _icon_film(draw, cx, cy, s, fg) -> None:
    x0, y0, x1, y1 = cx - s, cy - s * 0.7, cx + s, cy + s * 0.7
    draw.rounded_rectangle([x0, y0, x1, y1], radius=4, outline=fg, width=2)
    hole_r = s * 0.14
    for hx in (x0 + s * 0.35, x1 - s * 0.35):
        for hy in (y0 + s * 0.28, y1 - s * 0.28):
            draw.ellipse([hx - hole_r, hy - hole_r, hx + hole_r, hy + hole_r], outline=fg, width=1)
    draw.polygon([(cx - s * 0.25, cy - s * 0.32), (cx - s * 0.25, cy + s * 0.32), (cx + s * 0.35, cy)],
                 fill=fg)


def _icon_drive(draw, cx, cy, s, fg) -> None:
    x0, y0, x1, y1 = cx - s * 0.9, cy - s * 0.6, cx + s * 0.9, cy + s * 0.6
    draw.rounded_rectangle([x0, y0, x1, y1], radius=3, outline=fg, width=2)
    draw.ellipse([x1 - s * 0.42, cy - s * 0.12, x1 - s * 0.18, cy + s * 0.12], outline=fg, width=1)


def _icon_bluetooth(draw, cx, cy, s, fg) -> None:
    x0, x1 = cx - s * 0.35, cx + s * 0.35
    y0, y1 = cy - s, cy + s
    draw.line([(cx, y0), (cx, y1)], fill=fg, width=2)
    draw.line([(cx, y0), (x1, cy - s * 0.5), (x0, cy + s * 0.5), (cx, y1)], fill=fg, width=2, joint="curve")
    draw.line([(cx, y0), (x1, cy + s * 0.5), (x0, cy - s * 0.5), (cx, y1)], fill=fg, width=2, joint="curve")


def _icon_airplay(draw, cx, cy, s, fg) -> None:
    for r in (s * 0.95, s * 0.6):
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=215, end=325, fill=fg, width=2)
    tri = s * 0.32
    draw.polygon([(cx - tri, cy + s * 0.35), (cx + tri, cy + s * 0.35), (cx, cy + s * 0.9)], fill=fg)


def _icon_mic(draw, cx, cy, s, fg) -> None:
    draw.rounded_rectangle([cx - s * 0.32, cy - s, cx + s * 0.32, cy + s * 0.25],
                            radius=s * 0.3, outline=fg, width=2)
    draw.arc([cx - s * 0.65, cy - s * 0.5, cx + s * 0.65, cy + s * 0.65], start=20, end=160, fill=fg, width=2)
    draw.line([(cx, cy + s * 0.65), (cx, cy + s)], fill=fg, width=2)
    draw.line([(cx - s * 0.35, cy + s), (cx + s * 0.35, cy + s)], fill=fg, width=2)


def _icon_speaker(draw, cx, cy, s, fg, muted: bool) -> None:
    box_w, box_h = s * 0.5, s * 0.7
    draw.polygon([(cx - s, cy - box_h * 0.35), (cx - s + box_w * 0.5, cy - box_h * 0.35),
                  (cx - s + box_w, cy - box_h), (cx - s + box_w, cy + box_h),
                  (cx - s + box_w * 0.5, cy + box_h * 0.35), (cx - s, cy + box_h * 0.35)], fill=fg)
    if muted:
        x0 = cx - s * 0.05
        draw.line([(x0, cy - s * 0.4), (x0 + s * 0.55, cy + s * 0.4)], fill=fg, width=2)
        draw.line([(x0, cy + s * 0.4), (x0 + s * 0.55, cy - s * 0.4)], fill=fg, width=2)
    else:
        for r in (s * 0.5, s * 0.85):
            draw.arc([cx - s + box_w + r * 0.3, cy - r, cx - s + box_w + r * 0.3 + r * 2, cy + r],
                      start=-45, end=45, fill=fg, width=2)


def _menu_dots(draw, rect, fg) -> None:
    """Decorative vertical 3-dot overflow icon — see layout.py's
    MENU_DOTS_RECT comment for why it's inert (no menu behind it yet).
    """
    x, y, w, h = rect
    cx = x + w / 2
    r = 2.2
    for i, f in enumerate((0.28, 0.5, 0.72)):
        cy = y + h * f
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
