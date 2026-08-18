"""VideoLibrary.select_by_path() + VideoCommander.status()'s "path" field --
the two pieces streamdeck_daemon._collect_video_state uses to resync a
process's own VideoLibrary.current/.index against whatever mpv is ACTUALLY
playing, every render pass.

Background: the Stream Deck daemon and the TourBox daemon are separate
processes, each holding its own independent in-memory VideoLibrary. Auto-
advance (player/video_continuous.py) and TourBox-controller skips run in
the TourBox process and only ever update THAT process's copy -- reported
live as the Stream Deck's "now playing" tile and page-2 green highlight
staying frozen on a song several tracks back after auto-advance moved on.
mpv is the one thing both processes share, so _collect_video_state now
re-derives the correct entry from mpv's live loaded path every tick instead
of trusting this process's own last-known index. This test exercises the
underlying VideoLibrary/status() building blocks that fix relies on (not
the daemon class itself, which needs a live Stream Deck/MPD/config
environment to construct).
"""
import _bootstrap  # noqa: F401
import tempfile
from pathlib import Path

_bootstrap.require("mpd")
from player.config import setup_logging
from player.video import VideoLibrary
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


print("=== select_by_path() resyncs .current/.index to match an externally-"
      "advanced file, simulating a DIFFERENT process having moved mpv on ===")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        (root / name).write_bytes(b"")
    library = VideoLibrary(str(root))

    library.select(0)
    check("starts on the first entry (alphabetical)", library.current.path, str(root / "a.mkv"))

    # Simulate: some OTHER process's auto-advance told mpv to move on to
    # "c.mkv" two songs ago. THIS process's library was never told -- its
    # .current is still "a.mkv" until something resyncs it, exactly the bug
    # reported live ("stuck on a song... it has passed several songs").
    mpv_reported_path = str(root / "c.mkv")
    check("this process's cache is still stale before resync",
          library.current.path != mpv_reported_path, True)

    # This is the exact call _collect_video_state now makes every render
    # pass when mpv's live path disagrees with library.current.path.
    found = library.select_by_path(mpv_reported_path)
    check("select_by_path found and returned the matching entry",
          found.path if found else None, mpv_reported_path)
    check("library.current now matches mpv's real state",
          library.current.path, mpv_reported_path)
    check("library.index moved accordingly (used by the 'up next' window "
          "reset -- see streamdeck_daemon._video_window)",
          library.entries()[library.index].path, mpv_reported_path)

print("\n=== select_by_path(): a path mpv reports that isn't in this "
      "process's index (e.g. not yet rescanned) leaves .current untouched, "
      "not crashed ===")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "only.mkv").write_bytes(b"")
    library = VideoLibrary(str(root))
    library.select(0)
    before = library.current.path

    result = library.select_by_path(str(root / "not-indexed-yet.mkv"))
    check("select_by_path returns None for an unknown path", result, None)
    check("library.current left unchanged, not corrupted",
          library.current.path, before)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All video cross-process resync tests passed.")
