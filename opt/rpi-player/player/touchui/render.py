"""Touch UI overlay renderer: current playback state -> a BGRA bitmap mpv
composites over whatever it's currently showing (idle/black in music mode,
a real frame in video mode) via IPC ``overlay-add``.

--------------------------------------------------------------------------
v2 redesign
--------------------------------------------------------------------------
The first pass here was a pure outline wireframe (fully transparent
interiors, no fills at all). That was explicitly rejected as "hideous" in
favor of a supplied mockup: filled rounded pill buttons with colored
borders, a soft glow on the Play button, an album-art panel with a heart
badge, and format-tag chips (FLAC/24-bit/96kHz, or resolution/codec/fps).
This file was rewritten to match that mockup's visual language.

One deliberate compromise versus the mockup, kept from the original,
still-valid request ("I need this UI to be wireframe... so I can see
through the video being played"): every panel/pill fill here is
TRANSLUCENT (low alpha), not fully opaque like the mockup's dark cards.
In music mode there's nothing playing behind the UI to see through
anyway (mpv just holds an idle black clip), so this barely matters there
— but in video/karaoke mode the actual video is still playing full-screen
underneath, and a fully opaque card would silently defeat the whole
reason this got built as an mpv overlay instead of a normal touchscreen
UI toolkit. Translucent fills keep the mockup's polished look while
keeping that promise.

No real album art or video-frame thumbnail is fetched here (that needs an
MPD `albumart`/`readpicture` binary-protocol fetch — or an ffmpeg frame
grab for video — with its own caching; scoped out of this pass, flagged
as a natural follow-up). The art panel instead shows a themed placeholder
icon (music note / film reel).
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
    bg: str = "#0A0E16"
    panel: str = "#12161F"
    fg: str = "#F2F2F7"
    muted: str = "#8E8E99"
    accent: str = "#3D8BFD"       # music mode accent (blue)
    accent_video: str = "#A855F7"  # video mode accent (purple)
    active: str = "#22C58B"       # connected/active route (green)
    warn: str = "#FF9F0A"
    favorite: str = "#FF5C7A"
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


def _hex_rgba(h: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


class Renderer:
    """Holds loaded fonts across calls — avoids re-hitting disk every frame."""

    def __init__(self, theme: Theme) -> None:
        self._theme = theme
        self._font_title = self._load(theme.font_bold, 25)
        self._font_sub = self._load(theme.font_regular, 16)
        self._font_meta = self._load(theme.font_regular, 13)
        self._font_pill = self._load(theme.font_bold, 15)
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
            accent = self._theme.accent_video if state.mode == "video" else self._theme.accent
            draw = ImageDraw.Draw(img)
            self._draw_art_panel(img, draw, state, accent)
            self._draw_header(draw, state, accent)
            self._draw_scrub(draw, state, accent)
            self._draw_transport(img, draw, state, accent)
            self._draw_volume(draw, state)
        if rotate_180:
            img = img.transpose(Image.ROTATE_180)
        # mpv's overlay-add "bgra" format wants byte order B,G,R,A per
        # pixel. Pillow's raw encoder can do this conversion directly
        # without a manual channel-swap loop.
        return img.tobytes("raw", "BGRA")

    # -- shared drawing helpers ------------------------------------------------

    def _glow(self, img: Image.Image, cx: float, cy: float, r: float, color: tuple, blur: int = 12) -> None:
        """Soft blurred halo behind an element — the one place a real
        Gaussian blur is worth the per-frame cost, since it's what makes
        the Play button read as "glowing" like the mockup instead of just
        outlined. Composited onto ``img`` directly, so call this BEFORE
        drawing the crisp shape on top of it.
        """
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
        img.alpha_composite(layer)

    def _shadowed_text(self, draw, pos, text, font, fill):
        x, y = pos
        draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, 170))
        draw.text((x, y), text, font=font, fill=fill)

    def _pill_button(self, draw, rect, icon_fn, text, active=False, accent=None,
                      muted=False) -> None:
        """A filled, rounded pill: translucent background + colored border
        when active/accented, icon on the left, label text after it. This
        is the workhorse shape for every transport/route control in the
        v2 design — see this module's docstring for why the fill stays
        translucent rather than fully opaque like the mockup.
        """
        t = self._theme
        x, y, w, h = rect
        color = accent or t.fg
        if muted:
            border = _hex_rgba(t.muted, 90)
            fill = _hex_rgba(t.muted, 22)
            fg = _hex_rgba(t.muted, 180)
        elif active:
            border = _hex_rgba(color, 235)
            fill = _hex_rgba(color, 60)
            fg = _hex_rgba(t.fg)
        else:
            border = _hex_rgba(t.fg, 90)
            fill = _hex_rgba(t.fg, 20)
            fg = _hex_rgba(t.fg, 220)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, outline=border, width=2, fill=fill)

        cy = y + h / 2
        icon_s = h * 0.28
        pad = h * 0.32
        if icon_fn is not None:
            icon_cx = x + pad + icon_s * 0.5
            icon_fn(draw, icon_cx, cy, icon_s, fg)
            text_x = icon_cx + icon_s * 1.15
        else:
            text_x = x + pad
        if text:
            draw.text((text_x, cy), text, font=self._font_pill, fill=fg, anchor="lm")

    # -- album art panel ---------------------------------------------------------

    def _draw_art_panel(self, img, draw, state: OverlayState, accent: str) -> None:
        """No real album art / video thumbnail fetch yet (see module
        docstring) — a themed rounded panel with a music-note or film-reel
        icon stands in for it, plus the heart badge the mockup overlays on
        the art's corner (also a real, tappable curate button — see
        layout.ART_BADGE).
        """
        t = self._theme
        x, y, w, h = layout.ART_RECT
        draw.rounded_rectangle([x, y, x + w, y + h], radius=18,
                                fill=_hex_rgba(t.panel, 210), outline=_hex_rgba(accent, 140), width=2)
        cx, cy = x + w / 2, y + h / 2
        if state.mode == "video":
            _icon_film(draw, cx, cy, w * 0.22, _hex_rgba(accent, 200))
        else:
            _icon_music_note(draw, cx, cy, w * 0.22, _hex_rgba(accent, 200))

        # heart badge, top-left corner of the art
        bx, by, bw, bh = layout.ART_BADGE.rect
        badge_fill = _hex_rgba(t.favorite, 210) if state.curated else _hex_rgba(t.bg, 190)
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=9, fill=badge_fill,
                                outline=_hex_rgba(t.favorite, 220), width=2)
        _heart_glyph(draw, bx + bw / 2, by + bh / 2, bw * 0.3, _hex_rgba(t.fg), filled=state.curated)

    # -- header: mode/output pills, title, tags --------------------------------

    def _draw_header(self, draw, state: OverlayState, accent: str) -> None:
        t = self._theme

        mode_icon = _icon_film if state.mode == "video" else _icon_music_note
        mode_label = "VIDEO" if state.mode == "video" else "MUSIC"
        self._pill_button(draw, layout.MODE_PILL.rect, mode_icon, mode_label,
                           active=True, accent=accent)

        buttons = layout.buttons_for_mode(state.mode)
        by_action = {b.action: b for b in buttons}
        scan_b = by_action.get("update_database") or by_action.get("video_rescan")
        if scan_b:
            sx, sy, sw, sh = scan_b.rect
            draw.rounded_rectangle([sx, sy, sx + sw, sy + sh], radius=10,
                                    fill=_hex_rgba(t.fg, 18), outline=_hex_rgba(t.fg, 90), width=2)
            _scan_glyph(draw, sx + sw / 2, sy + sh / 2, sw * 0.32, _hex_rgba(t.fg, 220))

        self._pill_button(draw, layout.STORAGE_PILL.rect, _icon_drive, state.storage,
                           active=False)
        self._pill_button(draw, layout.BT_PILL.rect, _icon_bluetooth, "BT",
                           active=state.route_icon == "bluetooth", accent=t.active,
                           muted=not state.bt_available and state.route_icon != "bluetooth")
        self._pill_button(draw, layout.AIRPLAY_PILL.rect, _icon_airplay, "AP",
                           active=state.route_icon == "airplay", accent=t.active,
                           muted=not state.airplay_available and state.route_icon != "airplay")

        title_x = layout.MODE_PILL.rect[0]
        title = state.title or ("Nothing playing" if state.mode == "music" else "No video loaded")
        self._shadowed_text(draw, (title_x, 68), title, self._font_title, _hex_rgba(t.fg))
        if state.artist:
            self._shadowed_text(draw, (title_x, 102), state.artist, self._font_sub,
                                 _hex_rgba(t.fg, 210))
        if state.meta_line:
            self._shadowed_text(draw, (title_x, 124), state.meta_line, self._font_meta,
                                 _hex_rgba(t.muted, 220))

        # format-tag chips (FLAC/24-bit/96kHz/2ch, or 1920x1080/H.264/16:9/29.97fps)
        tx = title_x
        ty = 150
        for tag in state.tags:
            tw = draw.textlength(tag, font=self._font_tag) + 16
            draw.rounded_rectangle([tx, ty, tx + tw, ty + 22], radius=6,
                                    fill=_hex_rgba(t.fg, 16), outline=_hex_rgba(t.fg, 80), width=1)
            draw.text((tx + 8, ty + 11), tag, font=self._font_tag, fill=_hex_rgba(t.fg, 210), anchor="lm")
            tx += tw + 8

    # -- scrub bar ---------------------------------------------------------------

    def _draw_scrub(self, draw, state: OverlayState, accent: str) -> None:
        t = self._theme
        sx, sy, sw, sh = layout.SCRUB_BAR.rect
        track_y = sy + sh / 2
        self._shadowed_text(draw, (sx, sy - 20), _fmt_time(state.elapsed), self._font_time,
                             _hex_rgba(t.fg, 210))
        dur_txt = _fmt_time(state.duration)
        dw = draw.textlength(dur_txt, font=self._font_time)
        self._shadowed_text(draw, (sx + sw - dw, sy - 20), dur_txt, self._font_time,
                             _hex_rgba(t.fg, 210))

        draw.line([(sx, track_y), (sx + sw, track_y)], fill=_hex_rgba(t.fg, 60), width=4)
        frac = 0.0
        if state.duration > 0:
            frac = max(0.0, min(1.0, state.elapsed / state.duration))
        fill_x = sx + sw * frac
        if frac > 0:
            draw.line([(sx, track_y), (fill_x, track_y)], fill=_hex_rgba(accent, 235), width=4)
        draw.ellipse([fill_x - 7, track_y - 7, fill_x + 7, track_y + 7],
                     fill=_hex_rgba(t.fg), outline=_hex_rgba(accent, 235), width=2)

    # -- transport row -------------------------------------------------------------

    def _draw_transport(self, img, draw, state: OverlayState, accent: str) -> None:
        """NOTE: curate/prev/play/next each have TWO Button entries sharing
        the same action name (the transport-row pill here, plus ART_BADGE
        on the album art for curate specifically) — do NOT look these up
        via a by_action={b.action: b for b in buttons} dict the way the
        header does for BT/AP/scan. Two rects with the same action collide
        in that dict (whichever is last in the tuple wins), which is
        exactly the bug that first shipped here: the transport row's
        FAVORITE pill silently rendered at the tiny art-badge rect
        instead. Reference layout.CURATE_BTN[_V] etc. directly instead.
        """
        t = self._theme
        video = state.mode == "video"
        curate_rect = layout.CURATE_BTN_V.rect if video else layout.CURATE_BTN.rect
        prev_rect = layout.PREV_BTN_V.rect if video else layout.PREV_BTN.rect
        play_rect = layout.PLAY_BTN_V.rect if video else layout.PLAY_BTN.rect
        next_rect = layout.NEXT_BTN_V.rect if video else layout.NEXT_BTN.rect

        self._pill_button(draw, curate_rect,
                           lambda d, cx, cy, s, fg: _heart_glyph(d, cx, cy, s, fg, filled=state.curated),
                           "FAVORITE", active=state.curated, accent=t.favorite)
        self._pill_button(draw, prev_rect,
                           lambda d, cx, cy, s, fg: _skip_glyph(d, cx, cy, s, fg, direction=-1),
                           "PREVIOUS")
        self._pill_button(draw, next_rect,
                           lambda d, cx, cy, s, fg: _skip_glyph(d, cx, cy, s, fg, direction=1),
                           "NEXT")
        if video:
            vb = layout.VOCAL_BTN.rect
            label = "VOCAL" if state.vocal_active else "KARAOKE"
            # No icon here (icon_fn=None) — this pill's rect is narrower
            # than the others and "KARAOKE" already nearly fills it; adding
            # a mic icon pushed the text into the pill's rounded edge.
            self._pill_button(draw, vb, None, label, active=state.vocal_active,
                               accent=t.accent_video)

        x, y, w, h = play_rect
        cx, cy = x + w / 2, y + h / 2
        r = max(w, h) / 2
        self._glow(img, cx, cy, r * 1.35, _hex_rgba(accent, 110))
        draw.ellipse([x, y, x + w, y + h], fill=_hex_rgba(accent, 90),
                     outline=_hex_rgba(accent, 240), width=3)
        s = h * 0.26
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

    def _draw_volume(self, draw, state: OverlayState) -> None:
        t = self._theme
        vx, vy, vw, vh = layout.VOLUME_BAR.rect
        track_y = vy + vh / 2
        _icon_speaker(draw, vx - 30, track_y, vh * 0.7, _hex_rgba(t.fg, 210), muted=state.volume <= 0)

        draw.line([(vx, track_y), (vx + vw, track_y)], fill=_hex_rgba(t.fg, 60), width=4)
        vfrac = max(0.0, min(1.0, state.volume / 100.0))
        vfill_x = vx + vw * vfrac
        if vfrac > 0:
            draw.line([(vx, track_y), (vfill_x, track_y)], fill=_hex_rgba(t.fg, 220), width=4)
        draw.ellipse([vfill_x - 7, track_y - 7, vfill_x + 7, track_y + 7],
                     fill=_hex_rgba(t.fg), outline=_hex_rgba(t.fg, 235), width=2)
        pct = f"{state.volume}%"
        self._shadowed_text(draw, (vx + vw + 12, track_y - 8), pct, self._font_time,
                             _hex_rgba(t.fg, 220))


# -- standalone vector icon helpers (module-level: no per-button state needed) --

def _heart_glyph(draw, cx, cy, s, fg, filled: bool) -> None:
    """A real heart outline (parametric heart curve). Filled only when
    curated=True — a stronger, more universally understood "liked" signal
    than an outline heart, and small relative to whatever it sits on.
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
    target = s * 2.1
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
    y0, y1, ym = cy - s, cy + s, cy
    draw.line([(cx, y0), (cx, y1)], fill=fg, width=2)
    draw.line([(cx, y0), (x1, cy - s * 0.5), (x0, cy + s * 0.5), (cx, y1)], fill=fg, width=2, joint="curve")
    draw.line([(cx, y0), (x1, cy + s * 0.5), (x0, cy - s * 0.5), (cx, y1)], fill=fg, width=2, joint="curve")


def _icon_airplay(draw, cx, cy, s, fg) -> None:
    for i, r in enumerate((s * 0.95, s * 0.6)):
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
        for i, r in enumerate((s * 0.5, s * 0.85)):
            draw.arc([cx - s + box_w + r * 0.3, cy - r, cx - s + box_w + r * 0.3 + r * 2, cy + r],
                      start=-45, end=45, fill=fg, width=2)


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
