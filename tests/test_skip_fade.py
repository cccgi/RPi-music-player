"""Skip-fade tests: the software volume duck around a manual next/previous.

True crossfade on a manual skip is impossible via MPD (see actions.py's
_skip_with_fade docstring) -- this covers the duck/restore approximation
instead: ramps down, changes track, ramps back up, and that a second skip
landing mid-fade cancels the first fade's ramp-up instead of the two
fighting over the volume level.
"""
import _bootstrap  # noqa: F401
import time
_bootstrap.require("mpd")
from player.actions import ActionContext, dispatch
from player.config import setup_logging
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeMpd:
    def __init__(self, volume=80):
        self.vol = volume
        self.log = []

    def status(self):
        return {"volume": str(self.vol)}

    def set_volume(self, v):
        self.vol = v
        self.log.append(f"vol {v}")

    def next_track(self):
        self.log.append("next")

    def prev_track(self):
        self.log.append("prev")


class NullBus:
    def publish(self, *a, **k):
        pass


print("=== single skip: ramps down, changes track, ramps back up ===")
m = FakeMpd(volume=80)
ctx = ActionContext(mpd=m, router=None, bus=NullBus(), skip_fade_seconds=0.2)
dispatch("next_track", ctx, 1)
time.sleep(0.6)  # comfortably longer than the 0.2s fade
check("track changed exactly once", m.log.count("next"), 1)
check("ended back at the original volume", m.vol, 80)
check("volume dipped below baseline at some point", any(
    entry.startswith("vol ") and int(entry.split()[1]) < 80 for entry in m.log), True)
check("last log entry restores baseline", m.log[-1], "vol 80")

print("\n=== skip_fade_seconds=0 -- instant, synchronous, no thread ===")
m2 = FakeMpd(volume=50)
ctx2 = ActionContext(mpd=m2, router=None, bus=NullBus(), skip_fade_seconds=0)
dispatch("next_track", ctx2, 1)
check("next_track happened synchronously", m2.log, ["next"])

print("\n=== no mixer available (volume=-1) -- falls back to instant skip ===")
class NoMixerMpd(FakeMpd):
    def status(self):
        return {"volume": "-1"}
m3 = NoMixerMpd()
ctx3 = ActionContext(mpd=m3, router=None, bus=NullBus(), skip_fade_seconds=1.0)
dispatch("next_track", ctx3, 1)
check("skipped instantly with no mixer to duck", m3.log, ["next"])

print("\n=== a second skip mid-fade cancels the first's ramp-up ===")
# skip_fade_seconds is the TOTAL down+up time, split evenly between the two
# phases -- so with 0.3s total, the ramp-down alone is ~0.15s.
m4 = FakeMpd(volume=90)
ctx4 = ActionContext(mpd=m4, router=None, bus=NullBus(), skip_fade_seconds=0.3)
dispatch("next_track", ctx4, 1)
time.sleep(0.08)  # land inside the first fade's ~0.15s ramp-down
dispatch("next_track", ctx4, 1)
time.sleep(0.9)   # comfortably longer than the second fade's full 0.3s down+up cycle
check("both skips landed", m4.log.count("next"), 2)
check("ended back at the original volume (only once)", m4.vol, 90)
check("volume only restored to baseline exactly once", m4.log.count("vol 90"), 1)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All skip-fade tests passed.")
