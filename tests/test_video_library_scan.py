"""VideoLibrary.rescan: recurses into subfolders, same as Music mode's
library, and indexes any of DEFAULT_VIDEO_EXTENSIONS, not just .skp.

Two real bugs reproduced here:

1. rescan() was a flat os.listdir() — any .skp organized into a subfolder
   (e.g. "By Artist/Queen/...") was silently invisible to the video page no
   matter how many times Scan was pressed, with no error anywhere to
   explain why.
2. rescan() only ever matched ".skp" — reported live as "27 videos across
   two subfolders, only 16 detected": the library actually held 16 .skp
   files plus 11 plain .mkv files, and every .mkv was silently skipped.
   The library is not .skp-only; a mix of .skp karaoke files and plain
   video is expected side by side.

Exercises the real filesystem scan against a small tree, not a fake, since
the whole point of both fixes is walking real directories.
"""
import _bootstrap  # noqa: F401
import os
import tempfile
from pathlib import Path

from player.video import VideoLibrary, DEFAULT_VIDEO_EXTENSIONS

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    # Flat file at the root.
    (root / "Solo Song - Nobody.skp").write_bytes(b"")
    # One level deep.
    (root / "By Artist" / "Queen").mkdir(parents=True)
    (root / "By Artist" / "Queen" / "Bohemian Rhapsody - Queen.skp").write_bytes(b"")
    # Two levels deep.
    (root / "By Artist" / "ABBA" / "Live").mkdir(parents=True)
    (root / "By Artist" / "ABBA" / "Live" / "Dancing Queen - ABBA.skp").write_bytes(b"")
    # Non-.skp files anywhere in the tree must be ignored.
    (root / "By Artist" / "Queen" / "cover.jpg").write_bytes(b"")
    (root / "readme.txt").write_bytes(b"")

    print("=== rescan walks subfolders at every depth ===")
    lib = VideoLibrary(str(root))
    count = lib.rescan()
    check("found all 3 .skp files across 3 depths", count, 3)

    songs = sorted(e.song for e in lib.entries())
    check("song titles parsed from every depth", songs,
          sorted(["Bohemian Rhapsody", "Dancing Queen", "Solo Song"]))

    singers = {e.song: e.singer for e in lib.entries()}
    check("singer parsed correctly regardless of depth", singers,
          {"Bohemian Rhapsody": "Queen", "Dancing Queen": "ABBA", "Solo Song": "Nobody"})

    non_skp = [e for e in lib.entries() if not e.path.lower().endswith(".skp")]
    check("non-.skp files (cover.jpg, readme.txt) excluded", non_skp, [])

    print("\n=== rescan is deterministic: same tree, same order every time ===")
    order1 = [e.path for e in lib.entries()]
    lib.rescan()
    order2 = [e.path for e in lib.entries()]
    check("order stable across repeated rescans", order1, order2)

    print("\n=== adding a file in a NEW subfolder is picked up on rescan ===")
    (root / "By Artist" / "Bowie").mkdir(parents=True)
    (root / "By Artist" / "Bowie" / "Heroes - David Bowie.skp").write_bytes(b"")
    count2 = lib.rescan()
    check("new deep file counted", count2, 4)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    print("\n=== mixed .skp + .mkv library: reproduces the live '16 of 27' bug ===")
    (root / "Favorites").mkdir()
    (root / "Remix").mkdir()
    for i in range(16):
        (root / "Favorites" / f"Karaoke {i} - Someone.skp").write_bytes(b"")
    for i in range(11):
        (root / "Remix" / f"Video {i} - Someone.mkv").write_bytes(b"")

    lib2 = VideoLibrary(str(root))
    count = lib2.rescan()
    check("all 27 files found (16 .skp + 11 .mkv), not just the .skp ones", count, 27)
    exts = {os.path.splitext(e.path)[1].lower() for e in lib2.entries()}
    check("both extensions represented", exts, {".skp", ".mkv"})

    print("\n=== case-insensitive extension matching ===")
    (root / "Favorites" / "Upper.SKP").write_bytes(b"")
    (root / "Remix" / "Upper.MKV").write_bytes(b"")
    count3 = lib2.rescan()
    check("uppercase extensions also matched", count3, 29)

    print("\n=== extensions= is configurable, e.g. to exclude .mkv ===")
    lib3 = VideoLibrary(str(root), extensions=(".skp",))
    count4 = lib3.rescan()
    check("only .skp counted when extensions is restricted", count4, 17)  # 16 + Upper.SKP

    print("\n=== a format outside the configured set is not indexed at all ===")
    (root / "Favorites" / "Not A Video.mp3").write_bytes(b"")
    lib4 = VideoLibrary(str(root), extensions=DEFAULT_VIDEO_EXTENSIONS)
    count5 = lib4.rescan()
    check(".mp3 not in DEFAULT_VIDEO_EXTENSIONS -> not indexed", count5, 29)

    print("\n=== top_folders / first_index_in_folder: direct subfolder access ===")
    check("top-level subfolders listed, sorted", lib2.top_folders(),
          ["Favorites", "Remix"])
    fav_idx = lib2.first_index_in_folder("Favorites")
    check("first Favorites index points into Favorites",
          lib2.entries()[fav_idx].path.split(os.sep)[-2], "Favorites")
    remix_idx = lib2.first_index_in_folder("Remix")
    check("first Remix index points into Remix",
          lib2.entries()[remix_idx].path.split(os.sep)[-2], "Remix")
    check("unknown folder name -> None", lib2.first_index_in_folder("Nope"), None)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    print("\n=== a video sitting directly at the root has no folder to show ===")
    (root / "Solo - Nobody.skp").write_bytes(b"")
    lib5 = VideoLibrary(str(root))
    lib5.rescan()
    check("root-level file contributes no folder name", lib5.top_folders(), [])

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All video-library-scan tests passed.")
