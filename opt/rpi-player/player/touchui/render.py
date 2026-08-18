"""Touch UI overlay renderer: current playback state -> a BGRA bitmap mpv
composites over whatever it's currently showing (idle/black in music mode,
a real frame in video mode) via IPC ``overlay-add``.

Mirrors player/streamdeck/render.py's approach (Pillow, config.toml's
[streamdeck.theme] colors, DejaVu fonts install.sh guarantees) rather than
inventing a second visual language for the same project.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import layout

LOG = logging.getLogger(__name__)

_FALLBACK_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FALLBACK_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@dataclass
class Theme:
    bg: str = "#101014"
    fg: str = "#F2F2F7"
    muted: str = "#8E8E93"
    accent: str = "#0A84FF"
    active: str = "#30D158"
    warn: str = "#FF9F0A"
    font_regular: str = _FALLBACK_REGULAR
    font_bold: str = _FALLBACK_BOLD


@dataclass
class OverlayState:
    mode: str = "music"                 # "music" | "video"
    title: str = ""
    artist: str = ""                    # or a fixed sub-label in video mode
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
        self._font_title = self._load(theme.font_bold, 20)
        self._font_sub = self._load(theme.font_regular, 14)
        self._font_pill = self._load(theme.font_bold, 15)
        self._font_time = self._load(theme.font_regular, 13)
        self._font_icon = self._load(theme.font_bold, 20)
        self._font_big = self._load(theme.font_regular, 26)

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
            draw = ImageDraw.Draw(img)
            self._draw_topbar(draw, state)
            self._draw_bottombar(draw, state)
        if rotate_180:
            img = img.transpose(Image.ROTATE_180)
        # mpv's overlay-add "bgra" format wants byte order B,G,R,A per
        # pixel. Pillow's raw encoder can do this conversion directly
        # without a manual channel-swap loop.
        return img.tobytes("raw", "BGRA")

    # -- top bar ---------------------------------------------------------------
    #
    # Everything below is deliberately drawn as OUTLINE-ONLY shapes with a
    # transparent interior — no filled bars, pills, or button backgrounds —
    # so the video/art playing underneath stays visible through the whole
    # control surface, not just in the gaps between controls. "Active" state
    # (current mode, current route, playing, curated, vocal/karaoke) is
    # conveyed by a brighter/colored OUTLINE and text color, never a filled
    # block. Requested explicitly after the first working render looked too
    # much like a solid HUD panel sitting on top of the video.

    def _pill(self, draw: ImageDraw.ImageDraw, rect, text, font, fg, active=False,
               accent=None):
        t = self._theme
        x, y, w, h = rect
        outline = _hex_rgba(accent or t.accent) if active else _hex_rgba(t.fg, 140)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, outline=outline, width=2)
        tw = draw.textlength(text, font=font)
        draw.text((x + (w - tw) / 2, y + h / 2), text, font=font, fill=fg, anchor="lm")

    def _draw_topbar(self, draw: ImageDraw.ImageDraw, state: OverlayState) -> None:
        t = self._theme

        mode_label = "MUSIC" if state.mode == "music" else "VIDEO"
        self._pill(draw, MODE_RECT, mode_label, self._font_pill, _hex_rgba(t.fg), active=True)

        # now playing strip — text only, no backing panel. A thin drop
        # shadow (offset dark copy under the light text) keeps it legible
        # over bright video content without needing an opaque strip.
        title = state.title or ("Nothing playing" if state.mode == "music" else "No video loaded")
        self._shadowed_text(draw, (136, 14), title, self._font_title, _hex_rgba(t.fg))
        if state.artist:
            self._shadowed_text(draw, (136, 36), state.artist, self._font_sub, _hex_rgba(t.fg, 220))

        # storage pill
        self._pill(draw, STORAGE_RECT, state.storage, self._font_pill, _hex_rgba(t.fg))

        # route icons — drawn as text abbreviations, not emoji/dingbats.
        # DejaVu (what install.sh guarantees on the Pi) does not reliably
        # cover the Bluetooth/AirPlay/media-control Unicode blocks — found
        # live in a local render preview: those glyphs came back as blank
        # tofu boxes even though plain ASCII and ♡/♥ (Latin-1 range)
        # rendered fine. Every icon in this file is vector-drawn or plain
        # text for exactly that reason — no font-coverage gamble anywhere.
        self._route_icon(draw, BT_RECT, "BT", state.bt_available,
                          state.route_icon == "bluetooth")
        self._route_icon(draw, AIRPLAY_RECT, "AP", state.airplay_available,
                          state.route_icon == "airplay")

    def _shadowed_text(self, draw, pos, text, font, fill):
        x, y = pos
        draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, 160))
        draw.text((x, y), text, font=font, fill=fill)

    def _route_icon(self, draw, rect, label, available, is_active):
        t = self._theme
        x, y, w, h = rect
        if is_active:
            outline, fg = _hex_rgba(t.active), _hex_rgba(t.active)
        elif available:
            outline, fg = _hex_rgba(t.fg, 140), _hex_rgba(t.fg)
        else:
            outline, fg = _hex_rgba(t.muted, 90), _hex_rgba(t.muted, 140)
        draw.ellipse([x, y, x + w, y + h], outline=outline, width=2)
        tw = draw.textlength(label, font=self._font_time)
        draw.text((x + w / 2 - tw / 2, y + h / 2), label, font=self._font_time, fill=fg, anchor="lm")

    # -- bottom bar --------------------------------------------------------------

    def _draw_bottombar(self, draw: ImageDraw.ImageDraw, state: OverlayState) -> None:
        t = self._theme

        # scrub bar — a thin outline track (not a filled block) with a
        # solid PROGRESS line inside it. The progress line is intentionally
        # still a thin fill (a few px tall, not a panel) since a track with
        # no visible fill at all gives no sense of playback position.
        sx, sy, sw, sh = layout.SCRUB_BAR.rect
        draw.rounded_rectangle([sx, sy + sh // 2 - 2, sx + sw, sy + sh // 2 + 3],
                                radius=3, outline=_hex_rgba(t.fg, 140), width=1)
        frac = 0.0
        if state.duration > 0:
            frac = max(0.0, min(1.0, state.elapsed / state.duration))
        fill_w = int(sw * frac)
        if fill_w > 2:
            draw.rounded_rectangle([sx, sy + sh // 2 - 2, sx + fill_w, sy + sh // 2 + 3],
                                    radius=3, fill=_hex_rgba(t.accent))
        draw.ellipse([sx + fill_w - 6, sy + sh // 2 - 6, sx + fill_w + 6, sy + sh // 2 + 6],
                     outline=_hex_rgba(t.fg), width=2)
        self._shadowed_text(draw, (sx, sy - 16), _fmt_time(state.elapsed), self._font_time,
                             _hex_rgba(t.fg, 220))
        dur_txt = _fmt_time(state.duration)
        dw = draw.textlength(dur_txt, font=self._font_time)
        self._shadowed_text(draw, (sx + sw - dw, sy - 16), dur_txt, self._font_time,
                             _hex_rgba(t.fg, 220))

        buttons = layout.buttons_for_mode(state.mode)
        by_action = {b.action: b for b in buttons}

        curate = by_action.get("curate_current") or by_action.get("video_curate_current")
        prev_b = by_action.get("prev_track") or by_action.get("video_prev_song")
        play_b = by_action.get("toggle_pause") or by_action.get("video_play_pause")
        next_b = by_action.get("next_track") or by_action.get("video_next_song")
        delete_b = by_action.get("delete_current") or by_action.get("video_delete_current")

        if curate:
            self._round_btn(draw, curate.rect, glyph="curate", curated=state.curated,
                             accent_color="#FF6482" if state.curated else None)
        if prev_b:
            self._round_btn(draw, prev_b.rect, glyph="prev")
        if play_b:
            self._round_btn(draw, play_b.rect, glyph="pause" if state.playing else "play",
                             accent_color=t.accent if state.playing else None, big=True)
        if next_b:
            self._round_btn(draw, next_b.rect, glyph="next")
        if delete_b:
            self._round_btn(draw, delete_b.rect, glyph="delete", accent_hover=t.warn)

        if state.mode == "video":
            vb = layout.VOCAL_BTN.rect
            label = "VOCAL" if state.vocal_active else "KARAOKE"
            self._pill(draw, vb, label, self._font_pill, _hex_rgba(t.fg),
                       active=state.vocal_active)

        # volume row — "VOL" text label instead of a speaker glyph (see the
        # font-coverage note in _draw_topbar; same policy applies here). The
        # track is an outline only; the fill is a thin progress line, same
        # treatment as the scrub bar above.
        vx, vy, vw, vh = layout.VOLUME_BAR.rect
        self._shadowed_text(draw, (vx - 30, vy + vh / 2 - 6), "VOL", self._font_time,
                             _hex_rgba(t.fg, 220))
        draw.rounded_rectangle([vx, vy + vh // 2 - 2, vx + vw, vy + vh // 2 + 3],
                                radius=3, outline=_hex_rgba(t.fg, 140), width=1)
        vfrac = max(0.0, min(1.0, state.volume / 100.0))
        vfill = int(vw * vfrac)
        if vfill > 2:
            draw.rounded_rectangle([vx, vy + vh // 2 - 2, vx + vfill, vy + vh // 2 + 3],
                                    radius=3, fill=_hex_rgba(t.fg, 220))
        draw.ellipse([vx + vfill - 6, vy + vh // 2 - 6, vx + vfill + 6, vy + vh // 2 + 6],
                     outline=_hex_rgba(t.fg), width=2)
        pct = f"{state.volume}%"
        self._shadowed_text(draw, (vx + vw + 8, vy + vh / 2 - 6), pct, self._font_time,
                             _hex_rgba(t.fg, 220))

    def _round_btn(self, draw, rect, glyph, accent_color=None, accent_hover=None, big=False,
                    curated=False):
        """Draw a round transport button as an OUTLINE with a VECTOR-drawn
        glyph — no filled background, so video shows through the whole
        button, not just around it.

        No text/emoji glyph is used for the icon itself — see the
        font-coverage note in _draw_topbar. Play/pause/prev/next/delete/
        curate are all drawn as simple outlined polygons/lines instead,
        which render identically regardless of what's installed on the Pi.
        ``accent_color`` tints the outline+icon for an "active" state (e.g.
        playing, curated) instead of filling the button — that's the
        wireframe look requested after the first working render looked too
        much like a solid HUD panel. ``accent_hover`` is a softer per-button
        tint (e.g. delete's warn color) applied even when not "active".
        """
        t = self._theme
        x, y, w, h = rect
        color = accent_color or accent_hover or t.fg
        fg = _hex_rgba(color) if isinstance(color, str) else color
        width = 3 if accent_color else 2
        draw.rounded_rectangle([x, y, x + w, y + h], radius=w // 3, outline=fg, width=width)

        cx, cy = x + w / 2, y + h / 2
        s = (h * (0.42 if big else 0.34))  # icon half-size, scales with button

        if glyph == "play":
            draw.polygon([(cx - s * 0.6, cy - s), (cx - s * 0.6, cy + s), (cx + s, cy)],
                         outline=fg, width=2)
        elif glyph == "pause":
            bar_w = s * 0.45
            draw.rectangle([cx - s * 0.8, cy - s, cx - s * 0.8 + bar_w, cy + s],
                           outline=fg, width=2)
            draw.rectangle([cx + s * 0.35, cy - s, cx + s * 0.35 + bar_w, cy + s],
                           outline=fg, width=2)
        elif glyph == "prev":
            draw.polygon([(cx + s * 0.5, cy - s), (cx + s * 0.5, cy + s), (cx - s * 0.3, cy)],
                         outline=fg, width=2)
            draw.line([(cx - s, cy - s), (cx - s, cy + s)], fill=fg, width=2)
        elif glyph == "next":
            draw.polygon([(cx - s * 0.5, cy - s), (cx - s * 0.5, cy + s), (cx + s * 0.3, cy)],
                         outline=fg, width=2)
            draw.line([(cx + s, cy - s), (cx + s, cy + s)], fill=fg, width=2)
        elif glyph == "delete":
            # simple trash-can: lid line + body rectangle outline
            body_w, body_h = s * 1.3, s * 1.4
            bx0, by0 = cx - body_w / 2, cy - body_h / 2 + s * 0.25
            bx1, by1 = cx + body_w / 2, cy + body_h / 2
            draw.rectangle([bx0, by0, bx1, by1], outline=fg, width=2)
            draw.line([(bx0 - 3, by0), (bx1 + 3, by0)], fill=fg, width=2)
            draw.line([(cx - body_w * 0.2, by0), (cx - body_w * 0.15, by0 - 5)], fill=fg, width=2)
            draw.line([(cx + body_w * 0.2, by0), (cx + body_w * 0.15, by0 - 5)], fill=fg, width=2)
        elif glyph == "curate":
            # heart: two circles + a triangle, filled only when curated —
            # the one shape that keeps a small solid fill on purpose, since
            # a "liked" indicator reading as a filled heart is a much
            # stronger, more universally understood signal than an outline
            # heart would be, and it is tiny relative to the whole button.
            r = s * 0.5
            draw.ellipse([cx - r * 1.5, cy - r * 0.7, cx - r * 0.1, cy + r * 0.9],
                         outline=fg, width=2, fill=fg if curated else None)
            draw.ellipse([cx + r * 0.1, cy - r * 0.7, cx + r * 1.5, cy + r * 0.9],
                         outline=fg, width=2, fill=fg if curated else None)
            draw.polygon([(cx - r * 1.3, cy + r * 0.3), (cx + r * 1.3, cy + r * 0.3), (cx, cy + r * 1.7)],
                         outline=fg, width=2, fill=fg if curated else None)


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# Rect aliases kept local to this module's draw calls above, sourced from
# layout.py so drawing and hit-testing can never disagree.
MODE_RECT = layout.MODE_PILL.rect
STORAGE_RECT = layout.STORAGE_PILL.rect
BT_RECT = layout.BT_ICON.rect
AIRPLAY_RECT = layout.AIRPLAY_ICON.rect
