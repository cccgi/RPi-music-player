"""curate_current / video_curate_current: move the currently playing file
into a Curated/ subfolder inside its own directory, without touching
playback/queue state, with numeric-suffix collision handling.

No real MPD/mpv connection needed -- everything is a temp directory plus
fakes shaped like the real MpdCommander/VideoLibrary calls these actions
make.
"""
import _bootstrap  # noqa: F401
import tempfile
from pathlib import Path

_bootstrap.require("mpd")
from player.actions import ActionContext, DeleteSettings, dispatch
from player.config import setup_logging
from player.video import VideoLibrary
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class NullBus:
    def publish(self, *a, **k):
        pass


class FakeMpd:
    def __init__(self, uri):
        self._uri = uri
        self.calls = []

    def current_song(self):
        return {"file": self._uri} if self._uri else {}

    def _call(self, *args):
        self.calls.append(args)


print("=== curate_current: moves the file into Curated/ inside its own directory ===")
root = Path(tempfile.mkdtemp(prefix="curatetest"))
(root / "Album").mkdir()
song = root / "Album" / "track.flac"
song.write_text("audio")

mpd = FakeMpd("Album/track.flac")
ctx = ActionContext(mpd=mpd, router=None, bus=NullBus(),
                    delete=DeleteSettings(True, "queue", "", "", str(root)))
toast = dispatch("curate_current", ctx, 1)
check("toast says Curated", toast, "Curated")
check("original file gone from its old spot", song.exists(), False)
check("file landed in Curated/", (root / "Album" / "Curated" / "track.flac").exists(), True)
check("targeted update, not a full rescan", mpd.calls, [("update", "Album/Curated")])

print("\n=== curate_current: a name collision gets a numeric suffix, never overwrites ===")
(root / "Album2").mkdir()
(root / "Album2" / "Curated").mkdir()
(root / "Album2" / "Curated" / "dup.flac").write_text("existing curated file")
(root / "Album2" / "dup.flac").write_text("the one being curated now")
mpd2 = FakeMpd("Album2/dup.flac")
ctx2 = ActionContext(mpd=mpd2, router=None, bus=NullBus(),
                     delete=DeleteSettings(True, "queue", "", "", str(root)))
dispatch("curate_current", ctx2, 1)
check("original curated file untouched",
      (root / "Album2" / "Curated" / "dup.flac").read_text(), "existing curated file")
check("new file landed under a numeric suffix",
      (root / "Album2" / "Curated" / "dup (1).flac").read_text(), "the one being curated now")

print("\n=== curate_current: root-level file (no subdirectory) curates into root's own Curated/ ===")
(root / "top.flac").write_text("audio")
mpd3 = FakeMpd("top.flac")
ctx3 = ActionContext(mpd=mpd3, router=None, bus=NullBus(),
                     delete=DeleteSettings(True, "queue", "", "", str(root)))
dispatch("curate_current", ctx3, 1)
check("root file curated into the root Curated/", (root / "Curated" / "top.flac").exists(), True)
check("update issued for the root-level Curated/ dir", mpd3.calls, [("update", "Curated")])

print("\n=== curate_current: nothing playing -> a clear toast, no crash ===")
mpd4 = FakeMpd("")
ctx4 = ActionContext(mpd=mpd4, router=None, bus=NullBus(),
                     delete=DeleteSettings(True, "queue", "", "", str(root)))
check("nothing playing toast", dispatch("curate_current", ctx4, 1), "Nothing playing")


class FakeVideoLibrary:
    """Shaped like the real VideoLibrary just enough for video_curate_current:
    .current (a fake VideoEntry), .forget_position(), .rescan()."""

    def __init__(self, entry):
        self._entry = entry
        self.forgotten = []
        self.rescan_calls = 0

    @property
    def current(self):
        return self._entry

    def forget_position(self, path):
        self.forgotten.append(path)

    def rescan(self):
        self.rescan_calls += 1
        return 3


class FakeEntry:
    def __init__(self, path):
        self.path = path


print("\n=== video_curate_current: moves the video, forgets its old resume "
      "position, and rescans synchronously ===")
vroot = Path(tempfile.mkdtemp(prefix="videocuratetest"))
(vroot / "Karaoke").mkdir()
video_file = vroot / "Karaoke" / "song.mkv"
video_file.write_text("video")

lib = FakeVideoLibrary(FakeEntry(str(video_file)))
ctx5 = ActionContext(mpd=FakeMpd(""), router=None, bus=NullBus(), video_library=lib)
toast = dispatch("video_curate_current", ctx5, 1)
check("toast says Curated", toast, "Curated")
check("original video gone", video_file.exists(), False)
check("video landed in its own Curated/", (vroot / "Karaoke" / "Curated" / "song.mkv").exists(), True)
check("stale resume position forgotten by the OLD path", lib.forgotten, [str(video_file)])
check("synchronous rescan happened", lib.rescan_calls, 1)

print("\n=== video_curate_current: nothing playing -> a clear toast ===")
lib2 = FakeVideoLibrary(None)
ctx6 = ActionContext(mpd=FakeMpd(""), router=None, bus=NullBus(), video_library=lib2)
check("nothing playing toast", dispatch("video_curate_current", ctx6, 1), "Nothing playing")

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All curate tests passed.")
