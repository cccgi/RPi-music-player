"""cycle_repeat_mode / cycle_shuffle_mode -- the 3-state Repeat (Off/Folder/
Song) and Shuffle (Off/Folder/All) buttons that replaced the old plain
on/off toggle_repeat/toggle_random bindings (and the separate Single
button, folded into Repeat) on the Stream Deck's page 1.

No real MPD connection -- a small fake tracking repeat/single/random state
and recording clear/add/play calls, shaped just enough to exercise the
cycle logic and the "Shuffle: All" queue-replacement path.
"""
import _bootstrap  # noqa: F401

_bootstrap.require("mpd")
from player.actions import ActionContext, dispatch
from player import actions
from player.config import setup_logging
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
    def __init__(self):
        self.repeat = False
        self.single = False
        self.random = False
        self.calls = []

    def status(self):
        return {
            "repeat": "1" if self.repeat else "0",
            "single": "1" if self.single else "0",
            "random": "1" if self.random else "0",
        }

    def set_repeat(self, on):
        self.calls.append(("set_repeat", on))
        self.repeat = on

    def set_single(self, on):
        self.calls.append(("set_single", on))
        self.single = on

    def set_random(self, on):
        self.calls.append(("set_random", on))
        self.random = on

    def replace_queue_with_library(self):
        self.calls.append(("replace_queue_with_library",))


print("=== cycle_repeat_mode: Off -> Folder -> Song -> Off ===")
mpd = FakeMpd()
ctx = ActionContext(mpd=mpd, router=None, bus=NullBus())

toast = dispatch("cycle_repeat_mode", ctx, 1)
check("Off -> Folder toast", toast, "Repeat: Folder")
check("repeat on, single off", (mpd.repeat, mpd.single), (True, False))

toast = dispatch("cycle_repeat_mode", ctx, 1)
check("Folder -> Song toast", toast, "Repeat: Song")
check("repeat on, single on", (mpd.repeat, mpd.single), (True, True))

toast = dispatch("cycle_repeat_mode", ctx, 1)
check("Song -> Off toast", toast, "Repeat: Off")
check("repeat off, single off", (mpd.repeat, mpd.single), (False, False))

print("\n=== cycle_repeat_mode: an odd single-without-repeat state (MPD "
      "allows it, this app never produces it) is treated as Off, first "
      "press goes to Folder ===")
mpd2 = FakeMpd()
mpd2.single = True  # single=1, repeat=0 -- the odd state
ctx2 = ActionContext(mpd=mpd2, router=None, bus=NullBus())
toast = dispatch("cycle_repeat_mode", ctx2, 1)
check("odd state -> Folder", toast, "Repeat: Folder")
check("single forced off, repeat on", (mpd2.repeat, mpd2.single), (True, False))

print("\n=== cycle_shuffle_mode: Off -> Folder -> All -> Off ===")
mpd3 = FakeMpd()
actions._MUSIC_LINK = actions._MUSIC_LINK  # no-op, just documenting reliance
ctx3 = ActionContext(mpd=mpd3, router=None, bus=NullBus())
check("shuffle_all_active starts False", ctx3.shuffle_all_active, False)

toast = dispatch("cycle_shuffle_mode", ctx3, 1)
check("Off -> Folder toast", toast, "Shuffle: Folder")
check("random on", mpd3.random, True)
check("queue was NOT replaced for Folder",
      any(c[0] == "replace_queue_with_library" for c in mpd3.calls), False)
check("shuffle_all_active still False", ctx3.shuffle_all_active, False)

toast = dispatch("cycle_shuffle_mode", ctx3, 1)
check("Folder -> All toast mentions the source", toast.startswith("Shuffle: All"), True)
check("queue WAS replaced for All",
      any(c[0] == "replace_queue_with_library" for c in mpd3.calls), True)
check("shuffle_all_active now True", ctx3.shuffle_all_active, True)
check("random still on", mpd3.random, True)

toast = dispatch("cycle_shuffle_mode", ctx3, 1)
check("All -> Off toast", toast, "Shuffle: Off")
check("random off", mpd3.random, False)
check("shuffle_all_active reset to False", ctx3.shuffle_all_active, False)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All mode-cycle tests passed.")
