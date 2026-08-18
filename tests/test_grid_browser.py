"""Page-2 grid browsers (MusicGridBrowser / VideoGridBrowser) — the new
22-slot unified browser backing Music/Video "page 2" (see
layout.LAYOUT_BROWSE / LAYOUT_VIDEO_BROWSE and streamdeck_daemon.
_render_grid_key / _on_grid_key_press).

Covers, against fakes (no real MPD, no real Pi):
  - subfolders sort before files, case-insensitive, on both browsers.
  - pagination wraps at both ends (page_down past the last page wraps to
    the first; page_up before the first wraps to the last) once there are
    more than GRID_VISIBLE_SLOTS (21) entries.
  - entering a directory resets to page 0.
  - the ".." slot (browser.back()) is inert at the root and returns a name
    one level up otherwise.
  - goto_now_playing() jumps straight to the directory containing whatever
    is currently playing, or the root if nothing is.
  - MusicGridBrowser.enter() on a file plays it (queues the containing
    directory, same MPD call as LibraryBrowser) without navigating.
  - VideoGridBrowser.enter() on a file returns a ("play", path, toast)
    tuple instead of playing it directly (it has no VideoCommander of its
    own — see its docstring) and does NOT navigate.
"""
import _bootstrap  # noqa: F401
import os
import tempfile
from pathlib import Path

_bootstrap.require("mpd")
from player.config import setup_logging
from player.streamdeck.browser import GRID_VISIBLE_SLOTS, MusicGridBrowser, VideoGridBrowser
from player.video import VideoLibrary
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeMpd:
    """Minimal lsinfo-backed fake, same shape as LibraryBrowser's tests."""

    def __init__(self, tree):
        self.tree = tree  # path -> list of lsinfo-style dicts
        self.play_calls = []

    def lsinfo(self, path=""):
        return list(self.tree.get(path, []))

    def play_uri_from_directory(self, directory, uri):
        self.play_calls.append((directory, uri))


print("=== MusicGridBrowser: dirs before files, case-insensitive ===")
tree = {
    "": [
        {"file": "zzz.mp3"},
        {"directory": "Alpha"},
        {"file": "aaa.mp3"},
        {"directory": "beta"},
    ],
}
mpd = FakeMpd(tree)
mb = MusicGridBrowser(mpd)
names = [e.name for e in mb.visible() if e is not None]
check("dirs first, then case-insensitive files", names, ["Alpha", "beta", "aaa.mp3", "zzz.mp3"])

print("\n=== MusicGridBrowser: pagination wraps both directions ===")
big_tree = {"": [{"file": f"track{i:02d}.mp3"} for i in range(30)]}
mpd2 = FakeMpd(big_tree)
mb2 = MusicGridBrowser(mpd2)
check("first page starts at offset 0", mb2.offset, 0)
check("page count is 2 for 30 entries / 21 per page", mb2.page_count, 2)
mb2.page_down()
check("page_down advances to the second page", mb2.offset, GRID_VISIBLE_SLOTS)
mb2.page_down()
check("page_down past the last page wraps to the first", mb2.offset, 0)
mb2.page_up()
check("page_up before the first page wraps to the last", mb2.offset, GRID_VISIBLE_SLOTS)

small_tree = {"": [{"file": "only.mp3"}]}
mb3 = MusicGridBrowser(FakeMpd(small_tree))
mb3.page_down()
check("page_down is a no-op with <= 21 entries", mb3.offset, 0)

print("\n=== MusicGridBrowser: entering a directory resets to page 0 ===")
tree2 = {
    "": [{"directory": "Sub"}] + [{"file": f"t{i:02d}.mp3"} for i in range(25)],
    "Sub": [{"file": "inside.mp3"}],
}
mb4 = MusicGridBrowser(FakeMpd(tree2))
mb4.page_down()
check("moved off page 0 first", mb4.offset != 0, True)
# "Sub" is always sorted first (directories before files), so it's slot 0
# of page 0 -- go back to page 0 to find it, then enter it.
mb4.offset = 0
toast = mb4.enter(0)
check("entering the directory returns its name as the toast", toast, "Sub")
check("path descended into Sub", mb4.path, "Sub")
check("offset reset to 0 after descending", mb4.offset, 0)

print("\n=== MusicGridBrowser: '..' (back()) is inert at the root ===")
mb5 = MusicGridBrowser(FakeMpd({"": [{"directory": "X"}], "X": [{"file": "f.mp3"}]}))
check("at_root initially", mb5.at_root, True)
check("back() at root returns None", mb5.back(), None)
mb5.path = "X"
mb5.refresh()
toast = mb5.back()
check("back() from X returns 'Library' (root has no basename)", toast, "Library")
check("back() returned to the root", mb5.at_root, True)

print("\n=== MusicGridBrowser: entering a FILE plays it, does not navigate ===")
tree3 = {"Album": [{"file": "Album/one.mp3"}, {"file": "Album/two.mp3"}]}
mpd3 = FakeMpd(tree3)
mb6 = MusicGridBrowser(mpd3)
mb6.path = "Album"
mb6.refresh()
toast = mb6.enter(0)
check("path unchanged after playing a file", mb6.path, "Album")
check("MPD was told to play the containing directory", mpd3.play_calls,
      [("Album", "Album/one.mp3")])
check("toast is the file's name", toast, "one.mp3")

print("\n=== MusicGridBrowser: goto_now_playing() ===")
mb7 = MusicGridBrowser(FakeMpd({"": [{"directory": "Deep"}],
                                "Deep/Nested": [{"file": "Deep/Nested/song.mp3"}]}))
mb7.goto_now_playing("Deep/Nested/song.mp3")
check("jumped to the containing directory", mb7.path, "Deep/Nested")
mb7.goto_now_playing("")
check("nothing playing -> library root", mb7.path, "")


# ---------------------------------------------------------------------------
# VideoGridBrowser -- against a real temp directory tree + the real
# VideoLibrary (list_dir/current are exactly what's under test on the
# VideoLibrary side; there is no meaningful fake for "real directory
# listing").
# ---------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "Favorites").mkdir()
    (root / "Favorites" / "a.mp4").write_bytes(b"")
    (root / "zzz.mp4").write_bytes(b"")
    (root / "aaa.mp4").write_bytes(b"")
    (root / "ignored.txt").write_bytes(b"")  # not a video extension

    library = VideoLibrary(str(root))

    print("\n=== VideoGridBrowser: dirs before files, case-insensitive, "
          "non-video extensions excluded ===")
    vb = VideoGridBrowser(library)
    names = [(e.name, e.is_dir) for e in vb.visible() if e is not None]
    check("Favorites dir first, then files, ignored.txt excluded",
          names, [("Favorites", True), ("aaa.mp4", False), ("zzz.mp4", False)])

    print("\n=== VideoGridBrowser: entering a directory resets to page 0, "
          "descending updates path relative to the library root ===")
    toast = vb.enter(0)  # "Favorites"
    check("toast is the folder name", toast, "Favorites")
    check("path is root-relative", vb.path, "Favorites")
    check("offset reset to 0", vb.offset, 0)
    names_in_favorites = [e.name for e in vb.visible() if e is not None]
    check("now listing Favorites' contents", names_in_favorites, ["a.mp4"])

    print("\n=== VideoGridBrowser: '..' is inert at the root ===")
    vb2 = VideoGridBrowser(library)
    check("back() at root returns None", vb2.back(), None)
    vb2.path = "Favorites"
    vb2.refresh()
    toast = vb2.back()
    check("back() from Favorites returns 'Library'", toast, "Library")
    check("back() returned to the root", vb2.at_root, True)

    print("\n=== VideoGridBrowser: entering a FILE returns a ('play', path, "
          "toast) tuple instead of playing it directly ===")
    vb3 = VideoGridBrowser(library)
    # slot order: Favorites(dir), aaa.mp4, zzz.mp4 -- slot 1 is aaa.mp4.
    result = vb3.enter(1)
    check("result is a 'play' tuple", isinstance(result, tuple) and result[0] == "play", True)
    check("path unchanged (no navigation happened)", vb3.path, "")
    check("play tuple carries the full path", result[1], str(root / "aaa.mp4"))
    check("play tuple carries a toast", result[2], "aaa.mp4")

    print("\n=== VideoGridBrowser: goto_now_playing() ===")
    library.select_by_path(str(root / "Favorites" / "a.mp4"))
    vb4 = VideoGridBrowser(library)
    vb4.goto_now_playing()
    check("jumped to the playing file's directory", vb4.path, "Favorites")

    # An empty library (nothing ever indexed) -> current is None -> root.
    empty_root = tempfile.mkdtemp()
    empty_library = VideoLibrary(empty_root)
    vb5 = VideoGridBrowser(empty_library)
    vb5.goto_now_playing()
    check("nothing playing -> library root", vb5.path, "")

    print("\n=== VideoGridBrowser: pagination wraps with > 21 entries ===")
    many_root = tempfile.mkdtemp()
    for i in range(30):
        Path(many_root, f"v{i:02d}.mp4").write_bytes(b"")
    many_library = VideoLibrary(many_root)
    vb6 = VideoGridBrowser(many_library)
    check("page count is 2 for 30 entries / 21 per page", vb6.page_count, 2)
    vb6.page_down()
    check("page_down advances", vb6.offset, GRID_VISIBLE_SLOTS)
    vb6.page_down()
    check("page_down wraps past the last page", vb6.offset, 0)
    vb6.page_up()
    check("page_up wraps before the first page", vb6.offset, GRID_VISIBLE_SLOTS)

print("\n=== MusicGridBrowser: a whitespace-only Title tag falls back to the "
      "filename, not a blank name ===")
# Reproduced live against a real USB library: 31 .wav files carried a Title
# tag that was literally a single space (blank-but-not-empty -- truthy in
# Python), which used to win over the filename here and render as a
# music-note icon with NO visible label (render._wrap()'s word-splitting
# turns " " into zero words). A missing tag (None) must still fall back too.
tree_blank_title = {
    "": [
        {"file": "7A - 138 - track.wav", "title": " "},
        {"file": "8A - 140 - other.wav"},  # no title tag at all
    ],
}
mb_blank = MusicGridBrowser(FakeMpd(tree_blank_title))
names = [e.name for e in mb_blank.visible() if e is not None]
check("whitespace-only title falls back to filename", "7A - 138 - track.wav" in names, True)
check("missing title falls back to filename", "8A - 140 - other.wav" in names, True)
check("no blank-string names slipped through", any(n.strip() == "" for n in names), False)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All grid browser tests passed.")
