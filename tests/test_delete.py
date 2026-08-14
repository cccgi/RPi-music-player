"""Delete action tests. The path guard is the one that really matters."""
import _bootstrap  # noqa: F401
import os, sys, tempfile, shutil
from pathlib import Path
_bootstrap.require("mpd")
from player.actions import ActionContext, DeleteSettings, dispatch, _resolve_under_music_dir
from player.config import setup_logging
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")

root = Path(tempfile.mkdtemp(prefix="musictest"))
(root / "Album").mkdir()
song = root / "Album" / "track.flac"; song.write_text("audio")
outside = Path(tempfile.mkdtemp(prefix="outside")) / "secret.txt"
outside.write_text("do not delete me")

print("=== path guard: the difference between deleting a song and deleting anything ===")
check("normal URI resolves", _resolve_under_music_dir("Album/track.flac", str(root)), song.resolve())
check("../ escape REFUSED", _resolve_under_music_dir("../outside/secret.txt", str(root)), None)
check("deep ../ escape REFUSED", _resolve_under_music_dir("Album/../../etc/passwd", str(root)), None)
check("absolute path REFUSED", _resolve_under_music_dir("/etc/passwd", str(root)), None)
check("empty URI refused", _resolve_under_music_dir("", str(root)), None)
check("missing file refused", _resolve_under_music_dir("Album/nope.flac", str(root)), None)
check("escape target still exists", outside.exists(), True)

class FakeMpd:
    def __init__(self, uri): self._uri=uri; self.log=[]
    def current_song(self): return {"file": self._uri, "id": "7", "title": "T"}
    def status(self): return {"state": "play"}
    def next_track(self): self.log.append("next")
    def delete_id(self, i): self.log.append(f"deleteid {i}")
    def update_database(self): self.log.append("update")
    def _call(self, *a): self.log.append(" ".join(str(x) for x in a))

logf = root / "del.log"   # inside the per-run temp dir, not shared /tmp

print("\n=== mode=queue leaves the file alone ===")
m = FakeMpd("Album/track.flac")
ctx = ActionContext(mpd=m, router=None, bus=type("N",(),{"publish":lambda *a,**k:None})(),
    delete=DeleteSettings(True, "queue", "", str(logf), str(root)))
dispatch("delete_current", ctx, 1)
check("file untouched", song.exists(), True)
check("removed from queue", "deleteid 7" in m.log, True)

print("\n=== mode=trash moves, does not destroy ===")
trash = root.parent / "trash"
m = FakeMpd("Album/track.flac")
ctx = ActionContext(mpd=m, router=None, bus=type("N",(),{"publish":lambda *a,**k:None})(),
    delete=DeleteSettings(True, "trash", str(trash), str(logf), str(root)))
dispatch("delete_current", ctx, 1)
check("gone from library", song.exists(), False)
check("recoverable in trash", (trash / "Album" / "track.flac").exists(), True)

print("\n=== mode=permanent really does remove it ===")
song.parent.mkdir(parents=True, exist_ok=True); song.write_text("audio again")
m = FakeMpd("Album/track.flac")
ctx = ActionContext(mpd=m, router=None, bus=type("N",(),{"publish":lambda *a,**k:None})(),
    delete=DeleteSettings(True, "permanent", "", str(logf), str(root)))
dispatch("delete_current", ctx, 1)
check("file destroyed", song.exists(), False)
check("advanced before deleting", m.log[0], "next")

print("\n=== a malicious URI cannot destroy anything ===")
m = FakeMpd("../outside/secret.txt")
ctx = ActionContext(mpd=m, router=None, bus=type("N",(),{"publish":lambda *a,**k:None})(),
    delete=DeleteSettings(True, "permanent", "", str(logf), str(root)))
res = dispatch("delete_current", ctx, 1)
check("blocked", res, "Blocked")
check("outside file survives", outside.exists(), True)
check("no MPD calls made", m.log, [])

print("\n=== every deletion is logged ===")
lines = logf.read_text().strip().splitlines()
check("3 log entries", len(lines), 3)
print("      " + "\n      ".join(lines))

print("\n=== disabled means disabled ===")
m = FakeMpd("Album/x.flac")
ctx = ActionContext(mpd=m, router=None, bus=type("N",(),{"publish":lambda *a,**k:None})(),
    delete=DeleteSettings(False, "permanent", "", "", str(root)))
check("no-op when disabled", dispatch("delete_current", ctx, 1), "Delete off")

shutil.rmtree(root, ignore_errors=True); shutil.rmtree(outside.parent, ignore_errors=True)
print()
if fails: print("FAILURES:", fails); sys.exit(1)
print("All delete tests passed.")
