"""Output router tests: wpctl parsing, two-layer switching, availability."""
import _bootstrap  # noqa: F401
import sys
_bootstrap.require("mpd")
from player.config import load_config, setup_logging
from player.outputs import OutputRouter, PipeWireControl, PwSink
setup_logging('ERROR')

cfg=load_config()
fails=[]
def check(l,g,w):
    s="OK " if g==w else "FAIL"
    if g!=w: fails.append(l)
    print(f"  {s} {l}: got={g} want={w}")

# --- Real-world wpctl status output, verbatim shape ---
WPCTL = """PipeWire 'pipewire-0' [1.2.7, pi@rpi5, cookie:1234]
 └─ Clients:
        32. WirePlumber                         [1.2.7]
        45. mpd                                 [1.2.7]

Audio
 ├─ Devices:
 │      50. Built-in Audio                      [alsa]
 │
 ├─ Sinks:
 │  *   52. Built-in Audio Digital Stereo (HDMI) [vol: 0.40]
 │      61. WH-1000XM4                          [vol: 1.00]
 │      74. Living Room HomePod                 [vol: 0.65]
 │
 ├─ Sink endpoints:
 │
 ├─ Sources:
 │      53. Built-in Audio Analog Stereo        [vol: 1.00]
 │
 └─ Streams:

Video
 ├─ Devices:
"""

class FakePw(PipeWireControl):
    def __init__(self, text): self._text=text; self.default_set=[]; self.volume_set=[]
    def _run(self,*a,timeout=5.0):
        if a[0]=="status": return self._text
        if a[0]=="set-default": self.default_set.append(a[1]); return ""
        if a[0]=="set-volume": self.volume_set.append((a[1], a[2])); return ""
        return ""

pw=FakePw(WPCTL)
sinks=pw.list_sinks()
print("--- wpctl parsing ---")
check("parsed 3 sinks", len(sinks), 3)
check("ids", [s.id for s in sinks], [52,61,74])
check("default is 52", [s.id for s in sinks if s.is_default], [52])
check("names clean of vol suffix", sinks[1].name, "WH-1000XM4")

# rename sinks to realistic node names for matching. The AirPlay pattern
# matches a SPECIFIC speaker's mDNS hostname (raop_sink.SoundTouch...), not
# a bare "raop_sink" substring -- config.toml deliberately narrowed this
# after discovering module-raop-discover creates one sink per _raop._tcp
# announcer on the LAN, which includes other people's laptops, not just
# real speakers. See config.toml's [[routes]] comment for the full story.
WPCTL2 = WPCTL.replace("WH-1000XM4","bluez_output.38_18_4C_2A_9B_01.1")\
              .replace("Living Room HomePod","raop_sink.SoundTouch-30.local.192.168.0.151.1024")
pw2=FakePw(WPCTL2)
print("\n--- substring sink matching ---")
check("bluez_output found", pw2.find_sink("bluez_output").id, 61)
check("raop_sink.SoundTouch found", pw2.find_sink("raop_sink.SoundTouch").id, 74)
check("missing returns None", pw2.find_sink("chromecast"), None)
check("empty needle returns None", pw2.find_sink(""), None)

# --- Router with fake MPD ---
class FakeMpd:
    def __init__(self): self.calls=[]; self._outs=[
        {"outputid":"0","outputname":"Local","outputenabled":"1"},
        {"outputid":"1","outputname":"Network","outputenabled":"0"},
        {"outputid":"2","outputname":"HDMI","outputenabled":"0"}]
    def outputs(self): return self._outs
    def enable_output(self,i):
        self.calls.append(("enable",i))
        for o in self._outs:
            if o["outputid"]==str(i): o["outputenabled"]="1"
    def disable_output(self,i):
        self.calls.append(("disable",i))
        for o in self._outs:
            if o["outputid"]==str(i): o["outputenabled"]="0"
    def status(self):
        # No real mixer in this fixture -- "-1" is MPD's own way of saying
        # "no volume support", which _clamp_airplay_volume already treats as
        # a no-op (see outputs.py). Keeps switch_to(airplay) exercisable here
        # without dragging a real volume model into these routing tests.
        return {"volume": "-1"}

m=FakeMpd(); router=OutputRouter(m, cfg.routes, pw2)
print("\n--- routing ---")
check("detects Local active", router.detect_current().id, "local")
avail=[r.id for r in router.available_routes()]
# airplay_arylic has no matching sink in this fixture (only one AirPlay
# speaker -- SoundTouch -- is present), so it's correctly NOT available.
check("bt+airplay(SoundTouch) available, airplay_arylic is not",
      avail, ["local","bt","airplay"])

m.calls.clear()
bt=cfg.route_by_id("bt")
check("switch to bt succeeds", router.switch_to(bt), True)
check("set default sink BEFORE mpd enable", pw2.default_set, ["61"])
check("enable happened before disable", m.calls[0][0], "enable")
check("Network(1) enabled, Local(0) disabled", m.calls, [("enable",1),("disable",0)])

print("\n--- fresh BT/AirPlay node volume forced to unity, not left at WirePlumber's low default ---")
# Reproduces the live bug: connecting to the Camry head unit for the first
# time left its brand-new bluez_output PipeWire node at WirePlumber's own
# (well under 100%) default volume, multiplying against MPD's software
# mixer -- quiet even with MPD at 100% and the car's own volume maxed.
# set_default() now always forces the node's own volume to unity right
# after making it the default, on every route-switch path.
check("switch_to(bt) also forced the node's own PipeWire volume to unity",
      pw2.volume_set, [("61", "1.00")])

pw2.volume_set.clear()
airplay=cfg.route_by_id("airplay")
check("switch to airplay succeeds", router.switch_to(airplay), True)
check("switch_to(airplay) forces its node's own volume to unity too "
      "(separate from MPD's own AIRPLAY_SAFE_VOLUME software clamp)",
      pw2.volume_set, [("74", "1.00")])

print("\n--- reassert_current still forces BT node volume even when already default ---")
# A trusted Bluetooth device can auto-(re)connect and make itself default
# entirely on its own, bypassing switch_to()/set_default() -- the exact
# scenario reassert_current()'s own docstring describes. That path used to
# skip the node-volume fix above entirely, since it only ran inside the
# "sink had drifted, call set_default()" branch -- this fixture has the
# bluez_output sink ALREADY marked default (the "*"), so that branch is
# never taken, and only the new elif is what could apply the fix.
WPCTL_BT_ALREADY_DEFAULT = """PipeWire 'pipewire-0' [1.2.7, pi@rpi5, cookie:1234]
 └─ Clients:
        32. WirePlumber                         [1.2.7]

Audio
 ├─ Devices:
 │      50. Built-in Audio                      [alsa]
 │
 ├─ Sinks:
 │      52. Built-in Audio Digital Stereo (HDMI) [vol: 0.40]
 │  *   61. bluez_output.38_18_4C_2A_9B_01.1     [vol: 0.35]
 │
 ├─ Sink endpoints:
 │
 ├─ Sources:
 │
 └─ Streams:

Video
 ├─ Devices:
"""
pw5=FakePw(WPCTL_BT_ALREADY_DEFAULT)
m5=FakeMpd(); m5.enable_output(1); m5.disable_output(0)  # Network on, Local off
r5=OutputRouter(m5, cfg.routes, pw5)
r5.mark_current("bt")
pw5.volume_set.clear()
pw5.default_set.clear()
r5.reassert_current()       # sink is ALREADY default -- no drift, no set_default() call
check("reassert_current does not call set_default when nothing drifted",
      pw5.default_set, [])
check("but still force-applies unity volume to the already-default BT sink",
      pw5.volume_set, [("61", "1.00")])

print("\n--- ambiguity: bt and airplay both map to 'Network' ---")
check("disambiguated by default sink -> bt", router.detect_current().id, "bt")

print("\n--- unavailable route is refused, not silently broken ---")
pw3=FakePw(WPCTL)  # generic names, no bluez/raop -- nothing wireless present
m2=FakeMpd(); r3=OutputRouter(m2, cfg.routes, pw3)
ap=cfg.route_by_id("airplay")
check("airplay unavailable", r3.is_available(ap), False)
check("switch_to refuses", r3.switch_to(ap), False)
check("no MPD calls made on refusal", m2.calls, [])
check("cycle skips both unavailable airplay routes",
      [x.id for x in r3.available_routes()], ["local"])

print("\n--- cycle wraps (local <-> bt, both airplay routes skipped) ---")
WPCTL4 = WPCTL.replace("WH-1000XM4","bluez_output.38_18_4C_2A_9B_01.1")
pw4=FakePw(WPCTL4)  # bluetooth present, no AirPlay speaker at all
m4=FakeMpd(); r4=OutputRouter(m4, cfg.routes, pw4)
r4.detect_current()
check("cycle local->bt", r4.cycle(1).id, "bt")
check("cycle bt->local (wrap, both airplay routes skipped)", r4.cycle(1).id, "local")
check("cycle back local->bt", r4.cycle(-1).id, "bt")

print("\n--- cycle() re-syncs from ground truth instead of trusting a stale cache ---")
# Reproduces the live bug: tourbox-player and streamdeck-player each run
# their OWN OutputRouter instance in separate processes. If the STREAM DECK
# switches the route (a route-key press, or an AirPlay/BT picker pick),
# tourbox-player's own router never hears about it -- its cached
# _current_id is left pointing at whatever IT last switched to (or
# detected at its own startup). A TourBox knob click (cycle_output) that
# trusted that stale cache would compute the wrong "next" route -- e.g.
# instantly cycling right back to the route that is already active, or
# skipping the one that should actually come next.
m6=FakeMpd(); r6=OutputRouter(m6, cfg.routes, pw4)
r6.detect_current()          # real state: Local active
r6.switch_to(cfg.route_by_id("bt"))
# Now simulate drift: something OUTSIDE this router instance moved MPD/
# PipeWire back to Local (e.g. the other daemon's own switch_to("local")),
# without ever telling this instance -- its cache still says "bt".
m6.enable_output(0); m6.disable_output(1)
pw4.default_set.clear()
check("cache is stale: still thinks bt is current", r6.current().id, "bt")
check("cycle() ignores the stale cache and advances from the REAL current "
      "route (local) instead of the cached one (bt) -- would otherwise "
      "cycle bt->airplay based on a route that is not actually playing",
      r6.cycle(1).id, "bt")

print()
if fails: print("FAILURES:",fails); sys.exit(1)
print("All router tests passed.")
