"""Resume-position tests for long .skp videos skipped mid-play.

Covers VideoLibrary.remember_position/take_resume_position directly (no mpv
needed) plus load_and_play's resume seek, using a fake VideoCommander.
"""
import _bootstrap  # noqa: F401
_bootstrap.require("mpd")
from player.config import setup_logging
from player.video import VideoLibrary, load_and_play
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


print("=== remember_position / take_resume_position ===")
lib = VideoLibrary.__new__(VideoLibrary)  # skip the real filesystem scan
lib._positions = {}

lib.remember_position("song.skp", elapsed=90.0, duration=240.0)
check("mid-play position saved", lib._positions.get("song.skp"), 90.0)
check("take returns it once", lib.take_resume_position("song.skp"), 90.0)
check("and only once", lib.take_resume_position("song.skp"), None)

print("\n=== too early to bother resuming ===")
lib.remember_position("song.skp", elapsed=2.0, duration=240.0)
check("a few seconds in is not saved", lib.take_resume_position("song.skp"), None)

print("\n=== near the end counts as finished, not mid-play ===")
lib.remember_position("song.skp", elapsed=235.0, duration=240.0)
check("last few seconds is not saved", lib.take_resume_position("song.skp"), None)

print("\n=== a later, earlier skip clears an existing saved position ===")
lib.remember_position("song.skp", elapsed=100.0, duration=240.0)
lib.remember_position("song.skp", elapsed=239.0, duration=240.0)  # picked back up, watched to the end
check("finishing it clears the earlier resume point", lib.take_resume_position("song.skp"), None)


print("\n=== load_and_play seeks to the resume position once loaded ===")
class FakeOffsets:
    path = "fake.skp"
    video = (0, 100)
    audio = ()

class FakeCommander:
    def __init__(self):
        self.log = []
    def load_skp(self, offsets, start_track=0):
        self.log.append("load")
    def load_plain(self, path):
        self.log.append(f"load_plain {path}")
    def seek_absolute(self, seconds):
        self.log.append(f"seek {seconds}")

import player.video as video_mod
_orig_parse = video_mod.parse_skp
video_mod.parse_skp = lambda path: FakeOffsets()
try:
    class FakeEntry:
        path = "fake.skp"
        song = "Song"
        singer = "Singer"

    cmd = FakeCommander()
    load_and_play(cmd, FakeEntry(), resume=42.0)
    check("loads then seeks to the resume position", cmd.log, ["load", "seek 42.0"])

    cmd2 = FakeCommander()
    load_and_play(cmd2, FakeEntry(), resume=None)
    check("no resume -> no seek call", cmd2.log, ["load"])

    cmd3 = FakeCommander()
    load_and_play(cmd3, FakeEntry(), resume=0.0)
    check("resume=0.0 is falsy -> no seek call (fresh load already starts at 0)", cmd3.log, ["load"])
finally:
    video_mod.parse_skp = _orig_parse

print("\n=== load_and_play: a plain video file (not .skp) uses load_plain, no subfile parse ===")
class FakePlainEntry:
    path = "fake.mkv"
    song = "Song"
    singer = "Singer"

def _fail_if_parse_skp_called(path):
    raise AssertionError(f"parse_skp should never be called for a non-.skp entry: {path}")

video_mod.parse_skp = _fail_if_parse_skp_called
try:
    cmd4 = FakeCommander()
    offsets = load_and_play(cmd4, FakePlainEntry(), resume=None)
    check("load_plain called with the file's own path", cmd4.log, ["load_plain fake.mkv"])
    check("no SkpOffsets returned for a plain file", offsets, None)

    cmd5 = FakeCommander()
    load_and_play(cmd5, FakePlainEntry(), resume=17.5)
    check("resume still seeks for a plain file", cmd5.log, ["load_plain fake.mkv", "seek 17.5"])
finally:
    video_mod.parse_skp = _orig_parse

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All video-resume tests passed.")
