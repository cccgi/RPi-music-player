"""render.py: browse_entry()'s icon system -- per-folder colour-coding, the
distinct video-file glyph, and the enlarged top-60%-of-the-key icon layout.

Covers:
  - _folder_color() is deterministic -- same name always gives the same
    colour, across repeated calls AND across separate lru_cache-free
    invocations, since this must stay stable across daemon restarts (see
    the function's own docstring for why zlib.crc32 is used instead of
    Python's built-in hash()).
  - Different folder names generally land on different colours (not a hard
    guarantee for every possible name given a fixed small palette, but a
    representative real-world set of folder names should spread out, not
    collapse onto one colour).
  - browse_entry() actually uses a per-name colour for a folder's icon
    (not the old flat grey/fg), leaves file rows and the playing-highlight
    case untouched, and returns a genuinely blank image for empty=True.
  - is_video=True renders a visibly different icon than a plain (music)
    file with the exact same name -- the whole point of adding it was that
    video and music entries used to be indistinguishable.
  - The icon zone stays within the top 60% of the 96px key (y < ~58) and
    doesn't bleed into the bottom text zone, for folders, video files, AND
    music files alike.
"""
import _bootstrap  # noqa: F401
_bootstrap.require("PIL")

from player.config import setup_logging
from player.streamdeck.render import KeyRenderer, Theme, _folder_color, _FOLDER_PALETTE
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    # got/want can be raw image bytes in this file -- never repr() those,
    # only ever pass in plain bools/small values/short strings to check().
    print(f"  {'OK ' if ok else 'FAIL'} {label}: {'match' if ok else 'MISMATCH'}")


print("=== _folder_color: deterministic across repeated calls ===")
check("same name -> same colour, twice", _folder_color("Favorites"), _folder_color("Favorites"))
check("colour comes from the palette", _folder_color("Favorites") in _FOLDER_PALETTE, True)

print("\n=== _folder_color: a representative set of real folder names spreads "
      "across more than one colour (not all collapsing onto one) ===")
names = ["Favorites", "Remix", "Karaoke", "New", "2024", "Live", "Covers", "Old"]
colours = {_folder_color(n) for n in names}
check("more than one distinct colour used across 8 different folder names",
      len(colours) > 1, True)

print("\n=== browse_entry: folder icon uses a per-name colour, not flat grey ===")
theme = Theme(font_regular="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              font_bold="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
renderer = KeyRenderer(theme)

img_a = renderer.browse_entry("Favorites", is_dir=True)
img_b = renderer.browse_entry("Remix", is_dir=True)
check("two different folder names render two different images "
      "(icon colour differs)", img_a.tobytes() == img_b.tobytes(), False)

same_a = renderer.browse_entry("Favorites", is_dir=True)
check("the SAME folder name renders identically every time",
      img_a.tobytes() == same_a.tobytes(), True)

print("\n=== browse_entry: files and the playing-highlight case are untouched ===")
file_img = renderer.browse_entry("some_song.mp3", is_dir=False)
check("a file entry still renders (no crash, real image)",
      file_img.size, (96, 96))

playing_img = renderer.browse_entry("Favorites", is_dir=True, is_playing=True)
not_playing_img = renderer.browse_entry("Favorites", is_dir=True, is_playing=False)
check("the SAME folder playing vs not-playing render differently "
      "(green highlight still wins over folder colour)",
      playing_img.tobytes() == not_playing_img.tobytes(), False)

print("\n=== browse_entry: empty=True is still just a blank frame ===")
empty_img = renderer.browse_entry("", is_dir=True, empty=True)
blank_img = renderer.blank()
check("empty entry matches a plain blank key",
      empty_img.tobytes() == blank_img.tobytes(), True)

print("\n=== browse_entry: is_video gets its own glyph, distinct from a "
      "plain music file with the same name (this was the actual bug "
      "report -- video and audio entries looked identical) ===")
music_img = renderer.browse_entry("Same Name", is_dir=False, is_video=False)
video_img = renderer.browse_entry("Same Name", is_dir=False, is_video=True)
check("video icon differs from the music note icon for the identical name",
      music_img.tobytes() == video_img.tobytes(), False)

same_video_a = renderer.browse_entry("Same Name", is_dir=False, is_video=True)
check("the video icon itself is still deterministic for the same name",
      video_img.tobytes() == same_video_a.tobytes(), True)

print("\n=== browse_entry: folder and video icons are landscape rectangles "
      "(wider than tall), not squares -- squares were themselves the bug ===")


def icon_bbox(image, y_max=58):
    """Bounding box of non-background pixels within the icon zone only
    (y < y_max), ignoring the text band below it."""
    bg = image.getpixel((0, 0))
    xs, ys = [], []
    for y in range(0, y_max):
        for x in range(0, 96):
            if image.getpixel((x, y)) != bg:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)  # (width, height)


folder_w, folder_h = icon_bbox(renderer.browse_entry("X", is_dir=True))
check("folder icon is wider than it is tall", folder_w > folder_h, True)

video_w, video_h = icon_bbox(renderer.browse_entry("X", is_dir=False, is_video=True))
check("video icon is wider than it is tall", video_w > video_h, True)

print("\n=== browse_entry: icon zone stays within the top 60% of the key "
      "(y < 58), for every icon kind ===")


def icon_pixels_below(image, y_cutoff):
    """True if any non-background pixel appears at or below y_cutoff,
    ignoring the very bottom text band (y >= 60) entirely -- we only care
    that the ICON itself (not the text) respects the 60% boundary, and
    text legitimately starts around y=68."""
    bg = image.getpixel((0, 0))
    for y in range(y_cutoff, 60):
        for x in range(0, 96):
            if image.getpixel((x, y)) != bg:
                return True
    return False


folder_img = renderer.browse_entry("X", is_dir=True)
check("folder icon does not bleed past y=58 into the text zone",
      icon_pixels_below(folder_img, 58), False)

video_only_img = renderer.browse_entry("X", is_dir=False, is_video=True)
check("video icon does not bleed past y=58 into the text zone",
      icon_pixels_below(video_only_img, 58), False)

music_only_img = renderer.browse_entry("X", is_dir=False, is_video=False)
check("music note icon does not bleed past y=58 into the text zone",
      icon_pixels_below(music_only_img, 58), False)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All render icon tests passed.")
