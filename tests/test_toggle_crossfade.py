"""toggle_crossfade: the TourBox Knob click's new job, replacing cycle_output.

Reads MPD's own `xfade` status field rather than tracking state locally, so
it stays correct across daemon restarts and other clients changing it too.
"""
import _bootstrap  # noqa: F401
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
    def __init__(self, xfade=0):
        self.xfade = xfade
        self.log = []

    def status(self):
        return {"xfade": str(self.xfade)}

    def set_crossfade(self, seconds):
        self.xfade = seconds
        self.log.append(f"crossfade {seconds}")


class NullBus:
    def publish(self, *a, **k):
        pass


print("=== starts at the configured default (10s) -> toggles to 5s ===")
m = FakeMpd(xfade=10)
ctx = ActionContext(mpd=m, router=None, bus=NullBus(),
                     crossfade_seconds=10, crossfade_seconds_alt=5)
toast = dispatch("toggle_crossfade", ctx, 1)
check("toast", toast, "Crossfade 5s")
check("mpd crossfade called with 5", m.log, ["crossfade 5"])

print("\n=== press again -> back to 10s ===")
toast = dispatch("toggle_crossfade", ctx, 1)
check("toast", toast, "Crossfade 10s")
check("mpd crossfade called with 10", m.log, ["crossfade 5", "crossfade 10"])

print("\n=== crossfade was 0 (disabled, e.g. after an mpd restart) -> treated as 'not 10', goes to 10 ===")
m2 = FakeMpd(xfade=0)
ctx2 = ActionContext(mpd=m2, router=None, bus=NullBus(),
                      crossfade_seconds=10, crossfade_seconds_alt=5)
toast = dispatch("toggle_crossfade", ctx2, 1)
check("toast", toast, "Crossfade 10s")

print("\n=== some other value entirely (another client set it) -> also just goes to 10 ===")
m3 = FakeMpd(xfade=20)
ctx3 = ActionContext(mpd=m3, router=None, bus=NullBus(),
                      crossfade_seconds=10, crossfade_seconds_alt=5)
toast = dispatch("toggle_crossfade", ctx3, 1)
check("toast", toast, "Crossfade 10s")

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All toggle_crossfade tests passed.")
