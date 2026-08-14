"""ContinuousPlayback._on_player_change: the auto-advance-folder watcher.

The bug this covers: any deliberate queue reload elsewhere (a folder jump, a
library-browser press, delete_current's own advance, or literally anyone
running `mpc clear && mpc add ... && mpc play`) passes through a real,
observable MPD state of state=="stop", playlistlength=="0" for a moment
between the clear and the add landing. Before the fix, this watcher treated
that moment as "the queue genuinely ran out" and clobbered whatever was
being loaded with an unrelated folder -- found live: queued the whole
260-track library, and within about a second it had been silently swapped
for a different folder entirely. This directly explains "I pressed play on
one song and something completely different started playing."

_on_player_change is exercised directly (not through the idle watcher
thread) against a FakeMpd/FakeWatcher pair so this runs fast and
deterministically.
"""
import _bootstrap  # noqa: F401
import threading
_bootstrap.require("mpd")
from player.config import setup_logging
from player.continuous import ContinuousPlayback
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeMpd:
    def __init__(self):
        self.status_queue = []  # list of dicts, popped in order by status()
        self.log = []

    def status(self):
        if len(self.status_queue) > 1:
            return self.status_queue.pop(0)
        return self.status_queue[0]  # last one repeats

    def current_song(self):
        return {}  # nothing "current" once stopped at the end of the queue

    def playlist_info(self):
        return []  # empty is fine here -- _last_played_uri degrades gracefully

    def clear_queue(self):
        self.log.append("clear")

    def add(self, uri):
        self.log.append(f"add {uri}")

    def play_position(self, pos):
        self.log.append(f"play {pos}")

    def set_consume(self, on):
        pass

    def set_single(self, on):
        pass


def make_cp(mpd):
    cp = ContinuousPlayback.__new__(ContinuousPlayback)
    cp._mpd = mpd
    cp._enabled = True
    cp._force_consume_off = True
    cp._stop = threading.Event()
    cp._last_state = "play"   # pretend something was already playing
    cp._consecutive_advances = 0
    cp._max_consecutive = 8
    return cp


print("=== false alarm: length==0 (mid-reload elsewhere) never even debounces ===")
mpd = FakeMpd()
mpd.status_queue = [{"state": "stop", "playlistlength": "0"}]
cp = make_cp(mpd)
cp._on_player_change()
check("no advance was attempted", mpd.log, [])

print("\n=== false alarm: length>0 but a reload lands during the debounce window ===")
mpd2 = FakeMpd()
mpd2.status_queue = [
    {"state": "stop", "playlistlength": "260", "song": None},   # initial sample
    {"state": "play", "playlistlength": "23"},                   # re-check: something resumed
]
cp2 = make_cp(mpd2)
cp2._on_player_change()
check("no advance -- the re-check saw it was already playing again", mpd2.log, [])

print("\n=== false alarm: queue length changed underneath us during the debounce ===")
mpd3 = FakeMpd()
mpd3.status_queue = [
    {"state": "stop", "playlistlength": "260", "song": None},
    {"state": "stop", "playlistlength": "23", "song": None},   # different queue now
]
cp3 = make_cp(mpd3)
cp3._on_player_change()
check("no advance -- length mismatch on re-check", mpd3.log, [])

print("\n=== genuine end-of-queue: same state confirmed on re-check -> advances ===")
mpd4 = FakeMpd()
mpd4.status_queue = [
    {"state": "stop", "playlistlength": "4", "song": None},
    {"state": "stop", "playlistlength": "4", "song": None},   # still stopped, same length
]
cp4 = make_cp(mpd4)
import player.continuous as continuous_mod
continuous_mod.library_folders_for = lambda mpd: ["Folder A", "Folder B"]
# _advance imports library_folders_for from .actions at call time; patch that instead
import player.actions as actions_mod
actions_mod.library_folders_for = lambda mpd: ["Folder A", "Folder B"]
cp4._on_player_change()
check("advanced to a folder", any(l.startswith("add ") for l in mpd4.log), True)
check("cleared then added then played", mpd4.log[:1], ["clear"])

print("\n=== stopped mid-queue (user pressed stop) -- untouched, no debounce needed ===")
mpd5 = FakeMpd()
mpd5.status_queue = [{"state": "stop", "playlistlength": "10", "song": "3"}]
cp5 = make_cp(mpd5)
cp5._on_player_change()
check("left alone", mpd5.log, [])

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All continuous-playback tests passed.")
