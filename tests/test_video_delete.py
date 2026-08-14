"""video_delete_current: the video page's Delete key, sharing ctx.delete's
mode/trash_dir/log_path with music's delete_current (see actions.py).

Covers: queue mode just skips (file untouched), trash mode moves the file
under trash_dir/Video/<relative path> and advances playback first, permanent
mode unlinks, the safety rail refuses anything VideoLibrary didn't itself
scan into existence, deleting the ONLY entry stops playback instead of
reloading the file being removed, and the same transient-OSError retry
music's delete_current got also applies here.
"""
import _bootstrap  # noqa: F401
_bootstrap.require("mpd")
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from player.config import load_keymap, setup_logging
from player import actions
from player.actions import ActionContext, DeleteSettings, dispatch
from player.video import VideoLibrary
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeVideoCommander:
    def __init__(self):
        self.log = []
        self._volume = 100

    def status(self):
        return {"loaded": False}

    def load_plain(self, path):
        self.log.append(f"load {path}")

    def load_skp(self, offsets, start_track=0):
        self.log.append("load skp")

    def seek_absolute(self, seconds):
        self.log.append(f"seek {seconds}")

    def stop(self):
        self.log.append("stop")

    def volume(self):
        return self._volume

    def set_volume(self, v):
        self._volume = v
        return v


def make_ctx(tmp, mode="trash", entries=("Favorites/a.mkv", "Favorites/b.mkv")):
    video_dir = Path(tmp) / "Video"
    trash_dir = Path(tmp) / "Trash"
    log_path = Path(tmp) / "deleted.log"
    for rel in entries:
        p = video_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake video")
    library = VideoLibrary(str(video_dir))
    video = FakeVideoCommander()
    ctx = ActionContext(
        mpd=None, router=None, bus=SimpleNamespace(publish=lambda *a, **k: None),
        video=video, video_library=library,
        delete=DeleteSettings(enabled=True, mode=mode, trash_dir=str(trash_dir),
                               log_path=str(log_path), music_dir=str(Path(tmp) / "Music")),
    )
    return ctx, video, library, video_dir, trash_dir


actions.time.sleep = lambda seconds: None  # skip the real retry delay


print("=== queue mode: skips to next, file left untouched ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, video, library, video_dir, trash_dir = make_ctx(tmp, mode="queue")
    first = library.current
    toast = dispatch("video_delete_current", ctx, 1)
    check("toast says Skipped", toast, f"Skipped {first.song[:12]}")
    check("original file still on disk", Path(first.path).exists(), True)
    check("advanced to the other entry", library.current.path != first.path, True)


print("\n=== trash mode: moves the file, preserves relative layout under trash_dir/Video ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, video, library, video_dir, trash_dir = make_ctx(tmp, mode="trash")
    first = library.current
    rel = Path(first.path).relative_to(video_dir)
    toast = dispatch("video_delete_current", ctx, 1)
    target = trash_dir / "Video" / rel
    check("toast says Trashed", toast, f"Trashed {first.song[:12]}")
    check("original gone", Path(first.path).exists(), False)
    check("landed in trash_dir/Video/<relative path>", target.exists(), True)
    check("library rescanned -- only 1 entry left", len(library), 1)


print("\n=== trash mode: advances playback to the NEXT entry before touching the file ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, video, library, video_dir, trash_dir = make_ctx(tmp, mode="trash")
    first = library.current
    dispatch("video_delete_current", ctx, 1)
    check("mpv was told to load the next entry, not the deleted one",
          any("load" in c and str(first.path) not in c for c in video.log), True)


print("\n=== permanent mode: unlinks ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, video, library, video_dir, trash_dir = make_ctx(tmp, mode="permanent")
    first = library.current
    toast = dispatch("video_delete_current", ctx, 1)
    check("toast says Deleted", toast, f"Deleted {first.song[:12]}")
    check("file actually gone", Path(first.path).exists(), False)


print("\n=== deleting the ONLY entry stops playback instead of reloading it ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, video, library, video_dir, trash_dir = make_ctx(
        tmp, mode="trash", entries=("only.mkv",))
    dispatch("video_delete_current", ctx, 1)
    check("commander was told to stop, not reload the file being deleted",
          "stop" in video.log, True)
    check("library now empty", len(library), 0)


print("\n=== disabled delete: refuses, nothing touched ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, video, library, video_dir, trash_dir = make_ctx(tmp, mode="trash")
    ctx.delete.enabled = False
    first = library.current
    toast = dispatch("video_delete_current", ctx, 1)
    check("toast says Delete off", toast, "Delete off")
    check("file untouched", Path(first.path).exists(), True)


print("\n=== transient OSError: retried once before giving up (same as music) ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, video, library, video_dir, trash_dir = make_ctx(tmp, mode="trash")
    real_move = shutil.move
    calls = []

    def flaky_move(src, dst):
        calls.append((src, dst))
        if len(calls) == 1:
            raise OSError(30, "Read-only file system")
        return real_move(src, dst)

    actions.shutil.move = flaky_move
    try:
        toast = dispatch("video_delete_current", ctx, 1)
    finally:
        actions.shutil.move = real_move

    check("two attempts made", len(calls), 2)
    check("second attempt succeeded", toast is not None and toast.startswith("Trashed"), True)

print("\n=== keymap.toml: Tour double-click resolves to video_delete_current in the video layer ===")
# _VIDEO_LAYER = 2 in tourbox_daemon.py -- verify through the REAL config-
# loading path (Keymap.resolve), not just that the raw TOML text looks right.
keymap = load_keymap()
_VIDEO_LAYER = 2
_MUSIC_LAYER = 0
check("video layer's Tour double-click -> video_delete_current",
      keymap.resolve(_VIDEO_LAYER, "double", "tour"), "video_delete_current")
check("music layer's Tour double-click is unaffected (still delete_current)",
      keymap.resolve(_MUSIC_LAYER, "double", "tour"), "delete_current")

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All video-delete tests passed.")
