"""TourBoxDaemon._check_repeats: volume-control auto-repeat while held.

Holding a volume control should keep stepping the volume until it's
released, not just fire once on the press edge -- this is the TourBox side
of that fix (see streamdeck_daemon.py's REPEAT_ACTIONS/_start_repeat for the
Stream Deck side, not covered here since importing that module needs the
`StreamDeck` hardware package, which this offline suite does not require).

_check_repeats is exercised directly against a fake Decoder (held_actions()/
held_since()) and a fake MPD, with time.monotonic() patched to a controllable
fake clock, so this runs fast and deterministically with no real serial
device or MPD involved.
"""
import _bootstrap  # noqa: F401
_bootstrap.require("serial")
from player.actions import ActionContext
from player.config import setup_logging
import player.tourbox_daemon as tourbox_daemon_mod
from player.tourbox_daemon import TourBoxDaemon, _REPEAT_START_DELAY, _REPEAT_INTERVAL
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t
    def __call__(self):
        return self.t


class FakeMpd:
    def __init__(self, volume=50):
        self.volume = volume
        self.log = []

    def status(self):
        return {"volume": str(self.volume)}

    def change_volume(self, delta):
        self.volume = max(0, min(100, self.volume + delta))
        self.log.append(f"vol {self.volume}")
        return self.volume


class NullBus:
    def publish(self, *a, **kw):
        pass


class FakeDecoder:
    """Stands in for Decoder.held_actions()/held_since() -- the daemon only
    ever calls these two methods on it."""
    def __init__(self):
        self._held = {}   # name -> (action, pressed_at)

    def press(self, name, action, at):
        self._held[name] = (action, at)

    def release(self, name):
        self._held.pop(name, None)

    def held_actions(self):
        return {name: action for name, (action, _) in self._held.items()}

    def held_since(self, name):
        entry = self._held.get(name)
        return entry[1] if entry else None


def make_daemon(mpd, decoder):
    d = TourBoxDaemon.__new__(TourBoxDaemon)
    d._decoder = decoder
    d._repeat_last_fire = {}
    d._ctx = ActionContext(mpd=mpd, router=None, bus=NullBus(),
                           volume_step=3, max_volume_jump=25)
    return d


clock = FakeClock()
_orig_monotonic = tourbox_daemon_mod.time.monotonic
tourbox_daemon_mod.time.monotonic = clock

try:
    print("=== held less than REPEAT_START_DELAY: no repeat fires yet ===")
    mpd = FakeMpd(volume=50)
    dec = FakeDecoder()
    daemon = make_daemon(mpd, dec)
    clock.t = 0.0
    dec.press("dpad_up", "volume_up", at=0.0)
    daemon._check_repeats()
    clock.t = _REPEAT_START_DELAY - 0.05
    daemon._check_repeats()
    check("no repeat before the grace period elapses", mpd.log, [])

    print("\n=== held past REPEAT_START_DELAY: first repeat fires ===")
    clock.t = _REPEAT_START_DELAY + 0.01
    daemon._check_repeats()
    check("exactly one repeat step fired", len(mpd.log), 1)

    print("\n=== held further: fires again after each REPEAT_INTERVAL, not before ===")
    clock.t += _REPEAT_INTERVAL / 2
    daemon._check_repeats()
    check("too soon -- still just one step", len(mpd.log), 1)
    clock.t += _REPEAT_INTERVAL
    daemon._check_repeats()
    check("interval elapsed -- second step fired", len(mpd.log), 2)

    print("\n=== released: repeating stops, no more steps regardless of time passing ===")
    dec.release("dpad_up")
    daemon._check_repeats()
    clock.t += 10.0
    daemon._check_repeats()
    check("no further steps after release", len(mpd.log), 2)
    check("bookkeeping cleared on release", daemon._repeat_last_fire, {})

    print("\n=== volume already at the bound: repeating keeps calling but value stays clamped ===")
    mpd2 = FakeMpd(volume=100)
    dec2 = FakeDecoder()
    daemon2 = make_daemon(mpd2, dec2)
    clock.t = 0.0
    dec2.press("dpad_up", "volume_up", at=0.0)
    clock.t = _REPEAT_START_DELAY + _REPEAT_INTERVAL * 3
    for _ in range(3):
        daemon2._check_repeats()
        clock.t += _REPEAT_INTERVAL
    check("volume never exceeds 100", mpd2.volume, 100)

    print("\n=== non-repeatable action held (e.g. prev_track) never repeats ===")
    mpd3 = FakeMpd()
    dec3 = FakeDecoder()
    daemon3 = make_daemon(mpd3, dec3)
    clock.t = 0.0
    dec3.press("c1", "prev_track", at=0.0)
    clock.t = _REPEAT_START_DELAY + _REPEAT_INTERVAL * 5
    daemon3._check_repeats()
    check("non-volume action is never auto-repeated", mpd3.log, [])
finally:
    tourbox_daemon_mod.time.monotonic = _orig_monotonic

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All volume-repeat tests passed.")
