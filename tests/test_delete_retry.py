"""delete_current retries once on a transient OSError before giving up.

Reproduces the live bug: two real Stream Deck delete presses both failed
with "[Errno 30] Read-only file system: '/home/rpi/Music-trash'" minutes
apart, even though the filesystem was writable immediately before and after
each attempt, with no matching kernel remount-ro event in the journal --
consistent with a momentary hiccup (this build runs off a microSD card on a
car's power supply) rather than a real, persistent permission/mount
problem. A short retry turns that kind of blip into a silent success
instead of a lost "Delete failed" with no recourse but pressing again and
hoping.

Uses a real temp directory for music_dir/trash_dir (the code under test does
real Path.is_file()/mkdir/shutil.move calls) and monkeypatches
actions.shutil.move to simulate the transient failure without needing an
actual broken mount.
"""
import _bootstrap  # noqa: F401
_bootstrap.require("mpd")
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from player.config import setup_logging
from player import actions
from player.actions import ActionContext, DeleteSettings, dispatch
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeMpd:
    def __init__(self, uri):
        self._uri = uri
        self.calls = []

    def current_song(self):
        return {"file": self._uri, "id": "1"}

    def status(self):
        return {"state": "play"}

    def next_track(self):
        self.calls.append("next")

    def delete_id(self, song_id):
        self.calls.append(("delete_id", song_id))

    def update_database(self):
        self.calls.append("update_database")

    def _call(self, *args):
        self.calls.append(("call", args))


def make_ctx(tmp, mode="trash"):
    music_dir = Path(tmp) / "Music"
    trash_dir = Path(tmp) / "Trash"
    log_path = Path(tmp) / "deleted.log"
    (music_dir / "Test").mkdir(parents=True)
    song = music_dir / "Test" / "song.mp3"
    song.write_bytes(b"fake audio")
    uri = "Test/song.mp3"
    mpd = FakeMpd(uri)
    ctx = ActionContext(
        mpd=mpd, router=None, bus=SimpleNamespace(publish=lambda *a, **k: None),
        delete=DeleteSettings(enabled=True, mode=mode, trash_dir=str(trash_dir),
                               log_path=str(log_path), music_dir=str(music_dir)),
    )
    return ctx, mpd, song, trash_dir / uri


# Skip the real 0.6s retry delay so the test suite stays fast.
actions.time.sleep = lambda seconds: None

# actions.shutil IS the same module object as this file's own `shutil`
# import (Python modules are singletons in sys.modules) -- monkeypatching
# `actions.shutil.move` mutates the one global `shutil.move` everywhere,
# this test file's own references included. Save the TRUE original once,
# up front, and always restore to THIS saved reference, never to a live
# `shutil.move`/`Path.unlink` lookup -- once any block below has patched
# it, that live lookup no longer means "the original".
_REAL_MOVE = shutil.move
_REAL_UNLINK = Path.unlink


print("=== transient OSError on first attempt: retried, second succeeds ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, mpd, song, target = make_ctx(tmp)
    calls = []

    def flaky_move(src, dst):
        calls.append((src, dst))
        if len(calls) == 1:
            raise OSError(30, "Read-only file system")
        return _REAL_MOVE(src, dst)

    actions.shutil.move = flaky_move
    try:
        toast = dispatch("delete_current", ctx, 1)
    finally:
        actions.shutil.move = _REAL_MOVE

    check("two attempts were made", len(calls), 2)
    check("second attempt succeeded -> normal toast", toast, "Trashed song.mp3")
    check("file actually landed in trash on the retry", target.exists(), True)
    check("original location is empty", song.exists(), False)


print("\n=== both attempts fail: reports failure, does not crash/hang ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, mpd, song, target = make_ctx(tmp)
    calls = []

    def always_fails(src, dst):
        calls.append((src, dst))
        raise OSError(30, "Read-only file system")

    actions.shutil.move = always_fails
    try:
        toast = dispatch("delete_current", ctx, 1)
    finally:
        actions.shutil.move = _REAL_MOVE

    check("exactly two attempts, then gives up", len(calls), 2)
    check("failure toast surfaced, not silently swallowed", toast, "Delete failed")
    check("file untouched -- never partially lost", song.exists(), True)


print("\n=== success on the first try: no retry, no behaviour change ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, mpd, song, target = make_ctx(tmp)
    calls = []

    def counting_move(src, dst):
        calls.append((src, dst))
        return _REAL_MOVE(src, dst)

    actions.shutil.move = counting_move
    try:
        toast = dispatch("delete_current", ctx, 1)
    finally:
        actions.shutil.move = _REAL_MOVE

    check("only one attempt needed", len(calls), 1)
    check("normal success toast", toast, "Trashed song.mp3")
    check("db update still happened", "update_database" in mpd.calls or
          any(c == ("call", ("update", "Test")) for c in mpd.calls), True)


print("\n=== permanent mode: same retry behaviour, unlink instead of move ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx, mpd, song, target = make_ctx(tmp, mode="permanent")
    calls = []

    def flaky_unlink(self):
        calls.append(self)
        if len(calls) == 1:
            raise OSError(30, "Read-only file system")
        return _REAL_UNLINK(self)

    Path.unlink = flaky_unlink
    try:
        toast = dispatch("delete_current", ctx, 1)
    finally:
        Path.unlink = _REAL_UNLINK

    check("two attempts for permanent delete too", len(calls), 2)
    check("second attempt succeeded", toast, "Deleted song.mp3")
    check("file actually gone", song.exists(), False)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All delete-retry tests passed.")
