"""VideoContinuous._tick: the video-mode auto-advance watcher.

Video mode should play straight through the whole .skp library the same way
Music mode plays straight through a folder and then the next one -- this is
what was missing before (a video would finish and just sit there paused at
the end with nothing telling anyone why).

Exercised directly against fakes, same style as test_continuous.py, so this
runs fast and deterministically with no real mpv/MPD involved.
"""
import _bootstrap  # noqa: F401
import tempfile
from pathlib import Path

from player import mode as _mode
from player.actions import ActionContext
from player.video_continuous import VideoContinuous

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeBus:
    def publish(self, *a, **kw):
        pass


class FakeVideo:
    def __init__(self):
        self._eof = False
        self.log = []

    def eof_reached(self):
        return self._eof

    def status(self):
        return {"loaded": True, "elapsed": 0.0, "duration": 0.0}


class FakeEntry:
    def __init__(self, path, song="Song"):
        self.path = path
        self.song = song
        self.singer = ""


class FakeVideoLibrary:
    def __init__(self, paths):
        self._paths = paths
        self._i = 0
        self.log = []

    @property
    def current(self):
        return FakeEntry(self._paths[self._i]) if self._paths else None

    def next(self):
        self._i = (self._i + 1) % len(self._paths)
        self.log.append(f"advance -> {self._paths[self._i]}")
        return self.current

    def remember_position(self, *a, **kw):
        pass

    def take_resume_position(self, path):
        return None


def make_watcher(video, ctx, mode_file):
    vc = VideoContinuous.__new__(VideoContinuous)
    vc._ctx = ctx
    vc._video = video
    vc._mode_file = mode_file
    vc._enabled = True
    vc._last_eof = False
    return vc


def patch_load_and_play(monkeypatch_target):
    """actions._video_advance calls load_and_play(video, entry, resume=...),
    which would try to actually parse a .skp file. Stub it so the watcher
    test only exercises the advance/dispatch wiring, not the parser."""
    import player.actions as actions_mod
    calls = []
    def fake_load_and_play(video, entry, resume=None):
        calls.append(entry.path)
    actions_mod.load_and_play = fake_load_and_play
    return calls


with tempfile.TemporaryDirectory() as tmp:
    mode_path = str(Path(tmp) / "mode")

    print("=== not in video mode: eof is ignored entirely ===")
    _mode.write_mode(mode_path, _mode.MUSIC)
    video = FakeVideo()
    video._eof = True
    library = FakeVideoLibrary(["/v/a.skp", "/v/b.skp"])
    ctx = ActionContext(mpd=None, router=None, bus=FakeBus(),
                        video=video, video_library=library)
    calls = patch_load_and_play(None)
    watcher = make_watcher(video, ctx, mode_path)
    watcher._tick()
    check("no advance while in music mode", library.log, [])

    print("\n=== in video mode, eof true -> advances exactly once ===")
    _mode.write_mode(mode_path, _mode.VIDEO)
    video2 = FakeVideo()
    video2._eof = True
    library2 = FakeVideoLibrary(["/v/a.skp", "/v/b.skp"])
    ctx2 = ActionContext(mpd=None, router=None, bus=FakeBus(),
                         video=video2, video_library=library2)
    calls2 = patch_load_and_play(None)
    watcher2 = make_watcher(video2, ctx2, mode_path)
    watcher2._tick()
    check("advanced once", library2.log, ["advance -> /v/b.skp"])
    check("loaded the new entry", calls2, ["/v/b.skp"])

    # eof-reached stays TRUE in mpv until the next loadfile actually lands --
    # ticking again with the same (stale) eof state must NOT advance a
    # second time, or every poll after a finish would fire another skip.
    watcher2._tick()
    check("second tick with stale eof=true does not advance again",
          library2.log, ["advance -> /v/b.skp"])

    print("\n=== eof clears (new file actually loaded) -> armed for the next finish ===")
    video2._eof = False
    watcher2._tick()
    video2._eof = True
    watcher2._tick()
    check("re-armed after eof cleared, advances again",
          library2.log, ["advance -> /v/b.skp", "advance -> /v/a.skp"])

    print("\n=== wraps from the last entry back to the first ===")
    _mode.write_mode(mode_path, _mode.VIDEO)
    video3 = FakeVideo()
    video3._eof = True
    library3 = FakeVideoLibrary(["/v/only-one.skp"])
    ctx3 = ActionContext(mpd=None, router=None, bus=FakeBus(),
                         video=video3, video_library=library3)
    calls3 = patch_load_and_play(None)
    watcher3 = make_watcher(video3, ctx3, mode_path)
    watcher3._tick()
    check("wraps to itself with a single-entry library",
          library3.log, ["advance -> /v/only-one.skp"])

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All video-continuous tests passed.")
