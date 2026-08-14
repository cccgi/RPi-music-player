"""Pillow rendering for Stream Deck XL keycaps.

Design constraints that shape this module:

* The XL has 32 keys at 96x96 px. Redrawing all of them costs real CPU and, on
  a battery build, real power. So every render is cached by a content key and
  the daemon only pushes keys whose content actually changed.
* Text must stay legible at 96 px. That means aggressive truncation and
  measured-width fitting rather than hoping a fixed font size works.
* Fonts are loaded once. Constructing an ImageFont per render is a surprisingly
  large cost when it happens 32 times a second.
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LOG = logging.getLogger(__name__)

KEY_SIZE = (96, 96)

# Fallback search path if the configured font is missing. fonts-dejavu-core is
# a dependency in install.sh, so this should always resolve.
_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


@dataclass(frozen=True)
class Theme:
    bg: str = "#101014"
    fg: str = "#F2F2F7"
    muted: str = "#8E8E93"
    accent: str = "#0A84FF"
    active: str = "#30D158"
    warn: str = "#FF9F0A"
    font_regular: str = "/opt/rpi-player/assets/fonts/DejaVuSans.ttf"
    font_bold: str = "/opt/rpi-player/assets/fonts/DejaVuSans-Bold.ttf"


# Folder icon colours (browse_entry, requested specifically after every
# folder rendered in the same flat grey made a listing hard to tell apart at
# a glance). Deliberately distinct from theme.accent/active/warn, which
# already mean specific things elsewhere (selected route, playing/on,
# muted/off) — reusing one of those for an unrelated folder would read as a
# status indicator that isn't there.
_FOLDER_PALETTE = [
    "#FF6B6B",  # coral
    "#FFA94D",  # orange
    "#FFD43B",  # amber-yellow
    "#69DB7C",  # green
    "#3BC9DB",  # teal
    "#74C0FC",  # sky blue
    "#B197FC",  # violet
    "#F783AC",  # pink
]


def _folder_color(name: str) -> str:
    """Deterministic colour for a folder, keyed by its name.

    The SAME folder must always render the SAME colour — across pages
    (browse_entry and video_folder both call this), across renders, and
    across daemon restarts — or the colour-coding defeats its own purpose
    (quick "oh, that's the blue one" recognition). Uses zlib.crc32, not
    Python's built-in hash(): that's randomised per-process via
    PYTHONHASHSEED and would repaint every folder a different colour on
    every restart.
    """
    index = zlib.crc32(name.encode("utf-8")) % len(_FOLDER_PALETTE)
    return _FOLDER_PALETTE[index]


@lru_cache(maxsize=32)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    for candidate in (path, *_FONT_FALLBACKS):
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, ValueError):
            continue
    LOG.warning("no TrueType font found (tried %s); falling back to bitmap default", path)
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str,
                font: ImageFont.FreeTypeFont) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    max_width: int,
    start_size: int,
    min_size: int = 9,
) -> tuple[str, ImageFont.FreeTypeFont]:
    """Shrink the font until the text fits; ellipsise if even min_size is too big."""
    for size in range(start_size, min_size - 1, -1):
        font = _font(font_path, size)
        if _text_width(draw, text, font) <= max_width:
            return text, font

    font = _font(font_path, min_size)
    truncated = text
    while truncated and _text_width(draw, truncated + "…", font) > max_width:
        truncated = truncated[:-1]
    return (truncated + "…") if truncated else "", font


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Greedy word wrap with ellipsis on the final line."""
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if _text_width(draw, trial, font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines:
        last = lines[-1]
        # If there is more text than we can show, mark it.
        consumed = sum(len(line.split()) for line in lines)
        if consumed < len(words):
            while last and _text_width(draw, last + "…", font) > max_width:
                last = last[:-1]
            lines[-1] = last + "…"

    return lines[:max_lines]


class KeyRenderer:
    """Produces 96x96 PIL images for each kind of keycap."""

    def __init__(self, theme: Theme) -> None:
        self.theme = theme

    # -- primitives --------------------------------------------------------

    def _blank(self, bg: str | None = None) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        image = Image.new("RGB", KEY_SIZE, bg or self.theme.bg)
        return image, ImageDraw.Draw(image)

    def _rounded_bg(self, draw: ImageDraw.ImageDraw, colour: str,
                    inset: int = 4, radius: int = 12) -> None:
        draw.rounded_rectangle(
            [inset, inset, KEY_SIZE[0] - inset, KEY_SIZE[1] - inset],
            radius=radius,
            fill=colour,
        )

    # -- keycap types ------------------------------------------------------

    def blank(self) -> Image.Image:
        image, _ = self._blank()
        return image

    def label(
        self,
        text: str,
        *,
        sub: str = "",
        colour: str | None = None,
        bg: str | None = None,
        highlight: bool = False,
    ) -> Image.Image:
        """A general text key with an optional smaller second line."""
        image, draw = self._blank(bg)
        if highlight:
            self._rounded_bg(draw, self.theme.accent)

        fg = colour or self.theme.fg
        centre_y = 48 if not sub else 38

        # Try to fit on one line at a readable size first. If that would force
        # the font below ~14pt, wrap onto two lines instead — a wrapped 16pt
        # album name is far more legible than a single 9pt one.
        fitted, font = _fit_text(draw, text, self.theme.font_bold, 84, 26, min_size=14)
        if fitted.endswith("…"):
            wrap_font = _font(self.theme.font_bold, 16)
            lines = _wrap(draw, text, wrap_font, 88, 2)
            y = centre_y - (len(lines) - 1) * 9
            for line in lines:
                draw.text((48, y), line, font=wrap_font, fill=fg, anchor="mm")
                y += 18
        else:
            draw.text((48, centre_y), fitted, font=font, fill=fg, anchor="mm")

        if sub:
            sub_fitted, sub_font = _fit_text(draw, sub, self.theme.font_regular, 88, 15)
            draw.text((48, 68), sub_fitted, font=sub_font,
                      fill=self.theme.muted, anchor="mm")
        return image

    def glyph(
        self,
        symbol: str,
        *,
        caption: str = "",
        colour: str | None = None,
        highlight: bool = False,
    ) -> Image.Image:
        """A large symbol key (transport controls, drawn as vector shapes)."""
        image, draw = self._blank()
        if highlight:
            self._rounded_bg(draw, self.theme.accent)

        fg = colour or self.theme.fg
        cy = 44 if caption else 48

        _draw_symbol(draw, symbol, cy, fg)

        if caption:
            fitted, font = _fit_text(draw, caption, self.theme.font_regular, 88, 14)
            draw.text((48, 80), fitted, font=font, fill=self.theme.muted, anchor="mm")
        return image

    def now_playing(self, title: str, artist: str) -> Image.Image:
        """Multi-line track info, wrapped and sized to fit."""
        image, draw = self._blank()

        title_font = _font(self.theme.font_bold, 15)
        title_lines = _wrap(draw, title or "Nothing playing", title_font, 88, 3)

        y = 16
        for line in title_lines:
            draw.text((48, y), line, font=title_font, fill=self.theme.fg, anchor="mm")
            y += 17

        if artist:
            artist_font = _font(self.theme.font_regular, 13)
            artist_lines = _wrap(draw, artist, artist_font, 88, 2)
            y = max(y + 4, 62)
            for line in artist_lines:
                draw.text((48, y), line, font=artist_font,
                          fill=self.theme.muted, anchor="mm")
                y += 15

        return image

    def progress(self, elapsed: float, duration: float, state: str) -> Image.Image:
        """Elapsed / remaining with a progress bar."""
        image, draw = self._blank()

        draw.text((48, 24), _mmss(elapsed), font=_font(self.theme.font_bold, 22),
                  fill=self.theme.fg, anchor="mm")

        # Bar
        bar_x0, bar_x1, bar_y = 10, 86, 50
        draw.rounded_rectangle([bar_x0, bar_y, bar_x1, bar_y + 6], radius=3,
                               fill="#2C2C2E")
        if duration > 0:
            fraction = max(0.0, min(1.0, elapsed / duration))
            filled = bar_x0 + int((bar_x1 - bar_x0) * fraction)
            if filled > bar_x0:
                colour = self.theme.active if state == "play" else self.theme.muted
                draw.rounded_rectangle([bar_x0, bar_y, filled, bar_y + 6],
                                       radius=3, fill=colour)

        if duration > 0:
            draw.text((48, 74), f"-{_mmss(max(0.0, duration - elapsed))}",
                      font=_font(self.theme.font_regular, 15),
                      fill=self.theme.muted, anchor="mm")
        return image

    def volume(self, level: int | None, muted: bool = False) -> Image.Image:
        """Volume as a number plus a vertical-ish bar."""
        image, draw = self._blank()

        if level is None:
            draw.text((48, 40), "—", font=_font(self.theme.font_bold, 30),
                      fill=self.theme.muted, anchor="mm")
            draw.text((48, 72), "no mixer", font=_font(self.theme.font_regular, 12),
                      fill=self.theme.muted, anchor="mm")
            return image

        colour = self.theme.warn if muted or level == 0 else self.theme.fg
        number_font = _font(self.theme.font_bold, 32)
        percent_font = _font(self.theme.font_regular, 13)

        # Lay the number and the '%' out as one unit and centre the pair, so a
        # one-digit volume does not leave the percent sign stranded to the right.
        number = str(level)
        number_w = _text_width(draw, number, number_font)
        percent_w = _text_width(draw, "%", percent_font)
        total_w = number_w + 2 + percent_w
        left = 48 - total_w // 2

        draw.text((left, 34), number, font=number_font, fill=colour, anchor="lm")
        draw.text((left + number_w + 2, 42), "%", font=percent_font,
                  fill=self.theme.muted, anchor="lm")

        bar_x0, bar_x1, bar_y = 10, 86, 62
        draw.rounded_rectangle([bar_x0, bar_y, bar_x1, bar_y + 8], radius=4,
                               fill="#2C2C2E")
        filled = bar_x0 + int((bar_x1 - bar_x0) * (level / 100))
        if filled > bar_x0:
            draw.rounded_rectangle([bar_x0, bar_y, filled, bar_y + 8], radius=4,
                                   fill=self.theme.warn if muted else self.theme.accent)

        if muted:
            draw.text((48, 82), "MUTED", font=_font(self.theme.font_bold, 11),
                      fill=self.theme.warn, anchor="mm")
        return image

    def route(self, label: str, active: bool, available: bool) -> Image.Image:
        """Output destination key: active, available, or greyed out.

        Greying out unavailable routes matters — switching to an AirPlay sink
        that has not been discovered means playing to nothing, which is very
        confusing on a device with no other feedback.
        """
        image, draw = self._blank()

        if active:
            self._rounded_bg(draw, self.theme.accent)
            fg = "#FFFFFF"
            sub_fg = "#D8E8FF"
        elif available:
            fg = self.theme.fg
            sub_fg = self.theme.muted
        else:
            fg = "#48484A"
            sub_fg = "#3A3A3C"

        _draw_symbol(draw, _ROUTE_GLYPHS.get(label.lower(), "speaker"), 38, fg)

        fitted, font = _fit_text(draw, label, self.theme.font_bold, 88, 16)
        draw.text((48, 76), fitted, font=font, fill=sub_fg, anchor="mm")
        return image

    def browse_entry(
        self,
        name: str,
        *,
        is_dir: bool,
        is_video: bool = False,
        is_playing: bool = False,
        empty: bool = False,
    ) -> Image.Image:
        """One row of the library browser, or one of the video page's
        "up next" / subfolder-shortcut tiles (both reuse this method).

        Folders get a folder glyph, video files get a screen+play glyph,
        everything else (music tracks) gets a note glyph — ``is_video`` was
        added specifically because before it, every non-folder entry got
        the same note glyph regardless of the actual page, so the video
        page's own file listing looked identical to the music library's and
        gave no visual cue you were even looking at video files. The
        currently playing entry is tinted green so you can see where you
        are in a listing without cross-referencing the now-playing key.

        A folder's glyph is colour-coded by name (see _folder_color) rather
        than the flat grey every folder used to render in — makes adjacent
        folders in a listing (or the video page's subfolder-shortcut tiles)
        distinguishable at a glance instead of needing to read every label.
        Only the icon is tinted; the text stays at the normal legible fg
        colour, and a currently-playing entry still renders fully green
        exactly as before, since "this is what's playing" is a more
        important signal than which folder it's in.

        Layout: the icon fills the top 60% of the key (_ICON_TOP..
        _ICON_BOTTOM below) — previously it was a small ~20px-tall glyph
        squeezed into the top corner, cramped and hard to read at a
        glance. The name gets the bottom 40%, capped at 2 lines (a 3rd line
        never actually helped: at this key size a genuinely long name is
        already truncated with an ellipsis by then, so the icon giving up
        vertical space for a line that's just "…" was a bad trade).
        """
        image, draw = self._blank()
        if empty:
            return image

        if is_playing:
            self._rounded_bg(draw, "#14361F")   # subtle green wash
            fg = self.theme.active
            icon_fg = fg
        elif is_dir:
            fg = self.theme.fg
            icon_fg = _folder_color(name)
        else:
            fg = "#C7C7CC"
            icon_fg = fg

        # Icon zone: a landscape RECTANGLE (wider than tall), not a square --
        # a folder or a video frame naturally reads as a rectangle, and
        # forcing either into a square bounding box was itself the bug (a
        # separate issue from the earlier stretching one: "1:1" meant pixel
        # proportions inside each shape should be undistorted -- a circle
        # should draw as a circle, not a flattened/stretched oval -- NOT
        # that the overall icon silhouette must be square). Every shape
        # below is drawn with equal x/y scaling so nothing inside it is
        # non-uniformly squashed, while the bounding box itself stays a
        # natural ~5:3 rectangle.
        cx = 48
        icon_top, icon_bottom = 10, 48       # 38px tall
        icon_left, icon_right = 16, 80       # 64px wide

        if is_dir:
            # Folder: tab along the top-left edge, body filling the rest of
            # the rectangle.
            tab_h = 8
            body_top = icon_top + tab_h
            draw.polygon(
                [(icon_left, body_top), (icon_left, icon_top),
                 (icon_left + 20, icon_top), (icon_left + 28, body_top)],
                fill=icon_fg,
            )
            draw.rounded_rectangle(
                [icon_left, body_top, icon_right, icon_bottom], radius=4, fill=icon_fg,
            )
        elif is_video:
            # Screen/frame outline with a play triangle inside -- reads as
            # "video" at a glance and doesn't share a silhouette with the
            # music note glyph below, which was the whole complaint (video
            # files and music files looked identical).
            draw.rounded_rectangle(
                [icon_left, icon_top, icon_right, icon_bottom], radius=6,
                outline=icon_fg, width=3,
            )
            play_w = 14
            draw.polygon(
                [(cx - play_w // 2, icon_top + 8), (cx - play_w // 2, icon_bottom - 8),
                 (cx + play_w // 2, (icon_top + icon_bottom) // 2)],
                fill=icon_fg,
            )
        else:
            # Music note. Naturally a narrower, taller shape than a folder
            # or a video frame -- that's correct for a note, not a
            # regression of the same "forced square" bug, so it keeps its
            # own proportions rather than being stretched to match the
            # folder/video rectangle's width.
            note_left = cx - 14
            draw.ellipse([note_left, icon_bottom - 14, note_left + 14, icon_bottom],
                         fill=icon_fg)
            draw.rectangle([note_left + 11, icon_top, note_left + 14, icon_bottom - 9],
                           fill=icon_fg)
            draw.polygon(
                [(note_left + 11, icon_top), (note_left + 32, icon_top + 2),
                 (note_left + 32, icon_top + 11), (note_left + 11, icon_top + 13)],
                fill=icon_fg,
            )

        # Up to 2 lines of wrapped name in the bottom 40%.
        font = _font(self.theme.font_regular, 13)
        lines = _wrap(draw, name, font, 90, 2)
        y = 68 if len(lines) > 1 else 76
        for line in lines:
            draw.text((48, y), line, font=font, fill=fg, anchor="mm")
            y += 16
        return image

    def browse_nav(self, label: str, sub: str = "", enabled: bool = True) -> Image.Image:
        """Back / page keys for the browser."""
        image, draw = self._blank()
        fg = self.theme.fg if enabled else "#48484A"
        sub_fg = self.theme.muted if enabled else "#3A3A3C"

        cx, cy = 48, 36
        if label == "Back":
            draw.polygon([(cx + 10, cy - 16), (cx + 10, cy + 16), (cx - 14, cy)], fill=fg)
            draw.rectangle([cx - 20, cy - 16, cx - 16, cy + 16], fill=fg)
        else:  # page / more
            for i, dy in enumerate((-12, 2)):
                draw.polygon([(cx - 14, cy + dy), (cx + 14, cy + dy),
                              (cx, cy + dy + 12)], fill=fg)

        fitted, font = _fit_text(draw, label, self.theme.font_bold, 88, 16)
        draw.text((48, 68), fitted, font=font, fill=fg, anchor="mm")
        if sub:
            draw.text((48, 85), sub, font=_font(self.theme.font_regular, 11),
                      fill=sub_fg, anchor="mm")
        return image

    def toast(self, text: str) -> Image.Image:
        image, draw = self._blank()
        self._rounded_bg(draw, "#1C1C1E")
        fitted, font = _fit_text(draw, text, self.theme.font_bold, 84, 20)
        draw.text((48, 48), fitted, font=font, fill=self.theme.accent, anchor="mm")
        return image

    def status(self, label: str, on: bool) -> Image.Image:
        """A mode toggle: shuffle, repeat, consume."""
        image, draw = self._blank()
        fg = self.theme.active if on else "#5A5A5E"
        fitted, font = _fit_text(draw, label, self.theme.font_bold, 84, 19)
        draw.text((48, 42), fitted, font=font, fill=fg, anchor="mm")
        draw.text((48, 70), "ON" if on else "OFF",
                  font=_font(self.theme.font_regular, 13),
                  fill=fg if on else self.theme.muted, anchor="mm")
        return image


# ---------------------------------------------------------------------------
# Vector glyphs
# ---------------------------------------------------------------------------
# Drawn as shapes rather than shipped as PNGs: no asset pipeline, scales to any
# key size, and recolours for free.

_ROUTE_GLYPHS = {
    "local": "dac",
    "bt": "bluetooth",
    "bluetooth": "bluetooth",
    "airplay": "airplay",
    "hdmi": "hdmi",
}


def _draw_symbol(draw: ImageDraw.ImageDraw, symbol: str, cy: int, fill: str) -> None:
    cx = 48

    if symbol == "play":
        draw.polygon([(cx - 13, cy - 18), (cx - 13, cy + 18), (cx + 19, cy)], fill=fill)

    elif symbol == "pause":
        draw.rounded_rectangle([cx - 16, cy - 18, cx - 5, cy + 18], radius=2, fill=fill)
        draw.rounded_rectangle([cx + 5, cy - 18, cx + 16, cy + 18], radius=2, fill=fill)

    elif symbol == "stop":
        draw.rounded_rectangle([cx - 16, cy - 16, cx + 16, cy + 16], radius=3, fill=fill)

    elif symbol == "next":
        draw.polygon([(cx - 18, cy - 15), (cx - 18, cy + 15), (cx + 6, cy)], fill=fill)
        draw.rectangle([cx + 9, cy - 15, cx + 15, cy + 15], fill=fill)

    elif symbol == "prev":
        draw.polygon([(cx + 18, cy - 15), (cx + 18, cy + 15), (cx - 6, cy)], fill=fill)
        draw.rectangle([cx - 15, cy - 15, cx - 9, cy + 15], fill=fill)

    elif symbol == "ffwd":
        draw.polygon([(cx - 20, cy - 13), (cx - 20, cy + 13), (cx - 2, cy)], fill=fill)
        draw.polygon([(cx - 1, cy - 13), (cx - 1, cy + 13), (cx + 17, cy)], fill=fill)

    elif symbol == "rew":
        draw.polygon([(cx + 20, cy - 13), (cx + 20, cy + 13), (cx + 2, cy)], fill=fill)
        draw.polygon([(cx + 1, cy - 13), (cx + 1, cy + 13), (cx - 17, cy)], fill=fill)

    elif symbol == "ffwd3":
        # Three chevrons, not two ("ffwd" above) — the coarse-scrub keys
        # need to read as visibly DIFFERENT from the plain seek keys at a
        # glance, not just a different number in the caption underneath.
        for i, x0 in enumerate((-23, -8, 7)):
            draw.polygon([(cx + x0, cy - 12), (cx + x0, cy + 12), (cx + x0 + 14, cy)],
                        fill=fill)

    elif symbol == "rew3":
        for i, x0 in enumerate((23, 8, -7)):
            draw.polygon([(cx + x0, cy - 12), (cx + x0, cy + 12), (cx + x0 - 14, cy)],
                        fill=fill)

    elif symbol == "bluetooth":
        # The classic rune, drawn as two chevrons plus a vertical stem.
        top, bottom, mid = cy - 20, cy + 20, cy
        width = 4
        draw.line([(cx, top), (cx, bottom)], fill=fill, width=width)
        draw.line([(cx, top), (cx + 12, cy - 10), (cx - 12, cy + 10)],
                  fill=fill, width=width, joint="curve")
        draw.line([(cx, bottom), (cx + 12, cy + 10), (cx - 12, cy - 10)],
                  fill=fill, width=width, joint="curve")
        draw.line([(cx, mid), (cx, mid)], fill=fill, width=width)

    elif symbol == "airplay":
        # Three arcs over a triangle.
        for index, radius in enumerate((10, 18, 26)):
            box = [cx - radius, cy - radius - 4, cx + radius, cy + radius - 4]
            draw.arc(box, start=210, end=330, fill=fill, width=3 - (index == 2))
        draw.polygon([(cx - 12, cy + 20), (cx + 12, cy + 20), (cx, cy + 4)], fill=fill)

    elif symbol == "dac":
        # A little box with a jack.
        draw.rounded_rectangle([cx - 20, cy - 14, cx + 20, cy + 14], radius=4,
                               outline=fill, width=3)
        draw.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], outline=fill, width=3)

    elif symbol == "hdmi":
        draw.rounded_rectangle([cx - 22, cy - 10, cx + 22, cy + 10], radius=4,
                               outline=fill, width=3)
        draw.line([(cx - 14, cy + 10), (cx - 10, cy + 18)], fill=fill, width=3)
        draw.line([(cx + 14, cy + 10), (cx + 10, cy + 18)], fill=fill, width=3)

    elif symbol == "speaker":
        draw.polygon([(cx - 16, cy - 7), (cx - 6, cy - 7), (cx + 4, cy - 18),
                      (cx + 4, cy + 18), (cx - 6, cy + 7), (cx - 16, cy + 7)], fill=fill)

    elif symbol in ("vol_down", "vol_up"):
        # Speaker cone (shifted left of centre to leave room for the +/-)
        # plus a plain minus or plus — the standard system-volume visual
        # language. Deliberately NOT the rewind/fast-forward triangles
        # ("ffwd"/"rew" above): those read as seek/transport controls, which
        # is exactly what made the old volume keys confusing — they looked
        # like they scrubbed the track, not the level.
        sx = cx - 14
        draw.polygon([(sx - 14, cy - 7), (sx - 4, cy - 7), (sx + 6, cy - 18),
                      (sx + 6, cy + 18), (sx - 4, cy + 7), (sx - 14, cy + 7)], fill=fill)
        draw.line([(sx + 15, cy), (sx + 31, cy)], fill=fill, width=4)
        if symbol == "vol_up":
            draw.line([(sx + 23, cy - 8), (sx + 23, cy + 8)], fill=fill, width=4)

    elif symbol == "shuffle":
        draw.line([(cx - 20, cy - 10), (cx + 10, cy + 10)], fill=fill, width=4)
        draw.line([(cx - 20, cy + 10), (cx + 10, cy - 10)], fill=fill, width=4)
        draw.polygon([(cx + 8, cy - 18), (cx + 20, cy - 10), (cx + 8, cy - 2)], fill=fill)
        draw.polygon([(cx + 8, cy + 2), (cx + 20, cy + 10), (cx + 8, cy + 18)], fill=fill)

    elif symbol == "repeat":
        draw.arc([cx - 20, cy - 16, cx + 20, cy + 16], start=20, end=340,
                 fill=fill, width=4)
        draw.polygon([(cx + 12, cy - 20), (cx + 22, cy - 10), (cx + 10, cy - 4)],
                     fill=fill)

    elif symbol == "trash":
        # Lid
        draw.rectangle([cx - 18, cy - 16, cx + 18, cy - 11], fill=fill)
        draw.rectangle([cx - 7, cy - 21, cx + 7, cy - 16], fill=fill)
        # Body, tapered
        draw.polygon([(cx - 15, cy - 9), (cx + 15, cy - 9),
                      (cx + 11, cy + 19), (cx - 11, cy + 19)], fill=fill)
        # Ribs, punched out in the background colour
        for dx in (-5, 0, 5):
            draw.line([(cx + dx, cy - 4), (cx + dx, cy + 14)], fill="#101014", width=2)

    elif symbol == "power":
        draw.arc([cx - 17, cy - 17, cx + 17, cy + 17], start=300, end=240,
                 fill=fill, width=4)
        draw.line([(cx, cy - 22), (cx, cy - 2)], fill=fill, width=4)

    elif symbol in ("wifi", "wifi_off"):
        # Classic three-arc-over-a-dot glyph, same "arcs" language as
        # "airplay" above. A slash through it for the off state so the
        # difference reads even if the colour swap (see streamdeck_daemon's
        # wifi_toggle render branch) doesn't stand out on a given key.
        for index, radius in enumerate((8, 16, 24)):
            box = [cx - radius, cy - radius + 2, cx + radius, cy + radius + 2]
            draw.arc(box, start=215, end=325, fill=fill, width=4 - (index == 2))
        draw.ellipse([cx - 4, cy + 14, cx + 4, cy + 22], fill=fill)
        if symbol == "wifi_off":
            draw.line([(cx - 26, cy - 22), (cx + 26, cy + 26)], fill=fill, width=5)


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def ensure_fonts(theme: Theme) -> None:
    """Warn early if the configured fonts are missing.

    Better to say so at startup than to silently render everything in the
    default bitmap font and have the user wonder why it looks wrong.
    """
    for path in (theme.font_regular, theme.font_bold):
        if not Path(path).exists():
            LOG.warning(
                "font missing: %s (falling back). Fix with: "
                "sudo apt install fonts-dejavu-core", path
            )
