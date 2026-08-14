"""End-to-end: the REAL TourBoxDaemon against a pty, with a fake MPD."""
import _bootstrap  # noqa: F401
import sys, os, pty, time
_bootstrap.require("serial", "mpd")
from player.config import load_config, load_keymap, setup_logging
import player.tourbox_daemon as tbd
import player.actions as actions
setup_logging('ERROR')

cfg=load_config(); km=load_keymap()

# --- Fake MPD that records every command -----------------------------------
class FakeMpd:
    def __init__(self):
        self.log=[]; self.vol=50; self.state="pause"; self.elapsed=30.0
        self._outs=[{"outputid":"0","outputname":"Local","outputenabled":"1"},
                    {"outputid":"1","outputname":"Network","outputenabled":"0"},
                    {"outputid":"2","outputname":"HDMI","outputenabled":"0"}]
    def status(self): return {"state":self.state,"volume":str(self.vol),
                              "elapsed":str(self.elapsed),"duration":"240.0",
                              "random":"0","repeat":"0","song":"2"}
    def outputs(self): return self._outs
    def toggle_pause(self):
        self.state = "play" if self.state!="play" else "pause"; self.log.append("toggle_pause")
    def change_volume(self,d):
        self.vol=max(0,min(100,self.vol+d)); self.log.append(f"vol{d:+d}->{self.vol}"); return self.vol
    def set_volume(self,v): self.vol=v; self.log.append(f"setvol {v}")
    def seek_relative(self,s): self.elapsed=max(0,self.elapsed+s); self.log.append(f"seek{s:+d}")
    def next_track(self): self.log.append("next")
    def prev_track(self): self.log.append("prev")
    def seek_to_start(self): self.log.append("seek0")
    def toggle_random(self): self.log.append("random")
    def stats(self): return {"db_update": "1"}
    def current_song(self): return {"file": "B/track.flac", "id": "1", "title": "T"}
    def lsinfo(self, uri=""): return [{"directory": "A"}, {"directory": "B"}] if uri == "" else []
    def clear_queue(self): self.log.append("clear")
    def add(self, u): self.log.append(f"add {u}")
    def toggle_repeat(self): self.log.append("repeat")
    def toggle_single(self): self.log.append("single")
    def toggle_consume(self): self.log.append("consume")
    def toggle_mute(self): self.log.append("mute")
    def update_database(self): self.log.append("update")
    def play_position(self,p): self.log.append(f"play{p}")
    def enable_output(self,i): self.log.append(f"enable{i}")
    def disable_output(self,i): self.log.append(f"disable{i}")
    def close(self): pass
    def playlist_info(self): return [{"album":"X"},{"album":"X"},{"album":"Y"}]
    def stop(self): self.log.append("stop")

fake=FakeMpd()

# --- Virtual serial port ----------------------------------------------------
master, slave = pty.openpty()
slave_path = os.ttyname(slave)
print(f"virtual TourBox on {slave_path}")

# Point config at the pty and inject the fake MPD
import dataclasses
cfg = dataclasses.replace(cfg, tourbox=dataclasses.replace(cfg.tourbox,
        device=slave_path, fallback_globs=()))

daemon = tbd.TourBoxDaemon.__new__(tbd.TourBoxDaemon)
from player.outputs import OutputRouter, PipeWireControl
from player.ipc import NullBus
from player.tourbox.protocol import Decoder
daemon._config=cfg; daemon._keymap=km; daemon._running=True; daemon._serial=None
daemon._decoder=Decoder(km, cfg.tourbox.long_press_seconds, cfg.tourbox.rotary_coalesce_seconds)
daemon._mpd=fake
class NoPw(PipeWireControl):
    def _run(self,*a,timeout=5.0): return "Audio\n ├─ Sinks:\n │  *   52. Built-in\n"
daemon._router=OutputRouter(fake,cfg.routes,NoPw())
daemon._bus=NullBus()
from player import mode as _mode
daemon._video=None; daemon._video_library=None
daemon._mode_file="/tmp/rpi-player-test-mode"; daemon._last_mode=_mode.MUSIC
daemon._last_mode_check=0.0
daemon._repeat_last_fire={}
daemon._ctx=actions.ActionContext(mpd=fake,router=daemon._router,bus=daemon._bus,
    volume_step=cfg.tourbox.steps.volume, seek_step=cfg.tourbox.steps.seek,
    mode_file=daemon._mode_file,
    # Skip fade runs next/prev on a background thread with a multi-second
    # ramp — this suite asserts on fake.log immediately after send()'s short
    # settle window, so disable it here; the fade itself is covered by
    # test_skip_fade.py.
    skip_fade_seconds=0.0)

assert daemon._open_device(), "daemon failed to open the pty"
print("daemon opened the virtual port\n")

def send(data, settle=0.15):
    os.write(master, bytes(data))
    end=time.time()+settle
    while time.time()<end:
        daemon._pump()

fails=[]
def check(l,g,w):
    s="OK " if g==w else "FAIL"
    if g!=w: fails.append(l)
    print(f"  {s} {l}: got={g} want={w}")

tall=km.buttons['tall']; knob=km.rotaries['knob']
c1=km.buttons['c1']; scroll=km.rotaries['scroll']
dial=km.rotaries['dial']; side=km.buttons['side']; top=km.buttons['top']

print("--- play/pause via TALL button ---")
fake.log.clear()
send([tall.byte, tall.release_byte])
check("toggle_pause dispatched", fake.log, ["toggle_pause"])
check("state flipped", fake.state, "play")

print("\n--- volume: 5 slow detents (one command each) ---")
fake.log.clear(); fake.vol=50
for _ in range(5):
    os.write(master, bytes([knob.cw]))
    end=time.time()+0.10
    while time.time()<end: daemon._pump()
check("5 separate volume commands", len(fake.log), 5)
check("volume 50 -> 65 (5 x 3%)", fake.vol, 65)

print("\n--- volume: 12 detents in one fast burst (coalesced) ---")
fake.log.clear(); fake.vol=20
send([knob.cw]*12, settle=0.3)
check("coalesced into 1 command", len(fake.log), 1)
check("fast spin clamped, not slammed to 100", fake.vol, 20+25)
print(f"       (volume {20} -> {fake.vol}, log={fake.log})")

print("\n--- seek step comes from CONFIG, not a hardcoded default ---")
check("config says 15s", cfg.tourbox.steps.seek, 15)
fake.log.clear(); fake.elapsed=100.0
daemon._ctx.seek_step = cfg.tourbox.steps.seek
send([scroll.cw])
check("one detent seeks +15s", fake.log, ["seek+15"])

print("\n--- seek via SCROLL wheel ---")
fake.log.clear(); fake.elapsed=100.0
send([scroll.cw, scroll.cw])
check("seek forward once, magnitude 2", len(fake.log), 1)
check("seek applied", fake.log[0].startswith("seek+"), True)

print("\n--- track skip via DIAL ---")
fake.log.clear()
send([dial.cw]); send([dial.ccw])
check("next then prev", fake.log, ["next","prev"])

print("\n--- SHIFT: side+c1 -> the shift binding, not the press binding ---")
fake.log.clear()
send([side.byte, c1.byte, c1.release_byte, side.release_byte])
check("shifted c1 -> prev_album jumps in queue", [l for l in fake.log if l.startswith("play")] != [], True)

print("\n--- LAYER: top toggles, dispatches nothing itself ---")
fake.log.clear()
send([top.byte, top.release_byte])
check("top fires no MPD action", fake.log, [])
check("daemon moved to layer 1", daemon._decoder.layer, 1)
fake.log.clear()
send([c1.byte, c1.release_byte])
check("c1 on layer 1 -> prev_folder loads a folder",
      any(l.startswith("add ") for l in fake.log), True)
send([top.byte, top.release_byte])
check("toggled back to layer 0", daemon._decoder.layer, 0)

print("\n--- unplug mid-hold does not latch the modifier ---")
os.write(master, bytes([side.byte])); 
end=time.time()+0.1
while time.time()<end: daemon._pump()
check("modifier is down", daemon._decoder.modifier_active, True)
daemon._close_device()
check("close cleared modifier", daemon._decoder.modifier_active, False)

# reconnect
assert daemon._open_device()
fake.log.clear()
send([tall.byte, tall.release_byte])
check("after replug, tall still works", fake.log, ["toggle_pause"])


print("\n--- response-frame guard: real 26-byte unlock reply must NOT inject presses ---")
fake.log.clear(); daemon._decoder.reset()
FRAME = bytes.fromhex("07e05cd1c1b2ca5e9ed3ec26336cf70b00000000000202000000")
send(list(FRAME), settle=0.3)
check("frame produced no actions", fake.log, [])
check("no button left latched", daemon._decoder.modifier_active, False)

print("\n--- but a genuine fast spin of the SAME length still works ---")
fake.log.clear(); fake.vol=40
send([knob.cw]*26, settle=0.3)
check("large all-known burst decoded", len(fake.log), 1)

daemon._close_device()
os.close(master)
print()
if fails: print("FAILURES:",fails); sys.exit(1)
print("End-to-end daemon test passed.")
