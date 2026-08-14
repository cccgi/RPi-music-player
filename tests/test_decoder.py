"""Decoder tests: press/release, long-press, modifier, rotary, LAYERS, DOUBLE-CLICK."""
import _bootstrap  # noqa: F401  (must be first: sets sys.path)
import sys
from player.config import load_config, load_keymap, setup_logging, Layer, Keymap, ButtonSpec
from player.tourbox.protocol import Decoder, EventKind
setup_logging('WARNING')

cfg = load_config(); km = load_keymap()
print(f"config: {len(cfg.routes)} routes, modifier={km.modifier}")
print(f"keymap: {len(km.buttons)} buttons, layers={[l.name for l in km.layers]}, toggle={km.layer_toggle}")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got} want={want}")

def mk(long_press=0.45, coalesce=0.06, keymap=None):
    return Decoder(keymap or km, long_press_seconds=long_press,
                   rotary_coalesce_seconds=coalesce)

B = km.buttons
tall, side, top, tour = B['tall'], B['side'], B['top'], B['c1']
c1, c2 = B['c1'], B['c2']
left, right, up = B['dpad_left'], B['dpad_right'], B['dpad_up']
tour = B['tour']

print("\n--- T1: instant button fires on the press edge ---")
d = mk()
ev = d.feed(tall.byte, now=0.0)
check("tall -> its layer-0 press action",
      [(e.kind.value, e.action) for e in ev],
      [("press", km.resolve(0, "press", "tall"))])
check("release emits nothing", d.feed(tall.release_byte, now=0.1), [])

print("\n--- T2: modifier / shift ---")
d = mk()
d.feed(side.byte, now=1.0)
check("modifier active", d.modifier_active, True)
ev = d.feed(c1.byte, now=1.1)
check("c1 shifted", [e.action for e in ev], [km.resolve(0, "shift", "c1")])
d.feed(c1.release_byte, now=1.2); d.feed(side.release_byte, now=1.3)
check("modifier released", d.modifier_active, False)
ev = d.feed(c1.byte, now=1.4)
check("c1 unshifted", [e.action for e in ev], [km.resolve(0, "press", "c1")])

print("\n--- T3: LAYER toggle changes what the other buttons do ---")
d = mk()
check("starts on layer 0", d.layer, 0)
ev = d.feed(top.byte, now=2.0)
check("top emits a LAYER event", [e.kind for e in ev], [EventKind.LAYER])
check("top dispatches no action", [e.action for e in ev], [""])
check("now on layer 1", d.layer, 1)
check("layer label", d.layer_label, km.layers[1].label)
d.feed(top.release_byte, now=2.1)

ev = d.feed(c1.byte, now=2.2)
check("c1 on layer 1 -> prev_folder", [e.action for e in ev], ["prev_folder"])
d.feed(c1.release_byte, now=2.3)
ev = d.feed(right.byte, now=2.4)
check("right on layer 1 -> coarse scrub", [e.action for e in ev], ["seek_forward_fast"])
d.feed(right.release_byte, now=2.5)

print("\n--- T4: unlisted controls FALL BACK to layer 0 ---")
ev = d.feed(up.byte, now=2.6)
check("up still volume_up on layer 1", [e.action for e in ev], ["volume_up"])
d.feed(up.release_byte, now=2.7)
ev = d.feed(tall.byte, now=2.8)
check("tall still play/pause on layer 1", [e.action for e in ev], ["toggle_pause"])
d.feed(tall.release_byte, now=2.9)

print("\n--- T5: toggle wraps back to layer 0 ---")
d.feed(top.byte, now=3.0); d.feed(top.release_byte, now=3.1)
check("wrapped to layer 0", d.layer, 0)
ev = d.feed(c1.byte, now=3.2)
check("c1 back to prev_track", [e.action for e in ev], ["prev_track"])

print("\n--- T6: DOUBLE-CLICK on Tour ---")
d = mk()
ev = d.feed(tour.byte, now=4.0)
check("single press emits nothing yet", ev, [])
d.feed(tour.release_byte, now=4.05)
ev = d.feed(tour.byte, now=4.2)          # within the 0.35s window
check("second press fires delete", [(e.kind.value, e.action) for e in ev],
      [("double", "delete_current")])
d.feed(tour.release_byte, now=4.25)

print("\n--- T7: a LONE press on Tour does nothing at all ---")
d = mk()
d.feed(tour.byte, now=5.0); d.feed(tour.release_byte, now=5.05)
ev = d.tick(now=5.5)                      # window expired
check("no action after the window (Tour has no single-press binding)",
      [e.action for e in ev], [])

print("\n--- T8: two SLOW presses are not a double ---")
d = mk()
d.feed(tour.byte, now=6.0); d.feed(tour.release_byte, now=6.05)
d.tick(now=6.5)
ev = d.feed(tour.byte, now=6.6)
check("second press starts a NEW window, does not fire", ev, [])

print("\n--- T9: rotary coalescing still works ---")
d = mk()
knob = km.rotaries['knob']
for i in range(10):
    assert d.feed(knob.cw, now=7.0 + i*0.005) == []
ev = d.tick(now=7.0+0.005*9+0.07)
check("one coalesced event", len(ev), 1)
check("magnitude 10", ev[0].magnitude, 10)

print("\n--- T10: reset clears held state but PRESERVES the layer ---")
d = mk()
d.feed(top.byte, now=8.0); d.feed(top.release_byte, now=8.05)
check("on layer 1", d.layer, 1)
d.feed(side.byte, now=8.1)
check("modifier down", d.modifier_active, True)
d.reset()
check("reset cleared modifier", d.modifier_active, False)
check("reset KEPT the layer", d.layer, 1)

print("\n--- T11: modifier TAP fires its own press binding (video layer only) ---")
d = mk()
d.set_layer(2)  # video layer: side has a press binding (switch_track)
check("on video layer", d.layer, 2)
d.feed(side.byte, now=9.0)
ev = d.feed(side.release_byte, now=9.05)   # released without touching anything else
check("lone tap fires switch_track", [e.action for e in ev], ["switch_track"])

print("\n--- T12: modifier HOLD-and-use still behaves as shift, even on video layer ---")
d = mk()
d.set_layer(2)
d.feed(side.byte, now=10.0)
ev = d.feed(c1.byte, now=10.1)  # c1's shift binding, or its plain press if none
check("c1 resolved while shifted",
      [e.action for e in ev], [km.resolve(2, "shift", "c1") or km.resolve(2, "press", "c1")])
d.feed(c1.release_byte, now=10.2)
ev = d.feed(side.release_byte, now=10.3)
check("release after real use does NOT also fire switch_track", [e.action for e in ev], [])

print("\n--- T13: Top does not cycle INTO the video layer, nor out of it ---")
d = mk()
check("starts on layer 0", d.layer, 0)
d.feed(top.byte, now=11.0); d.feed(top.release_byte, now=11.05)
d.feed(top.byte, now=11.1); d.feed(top.release_byte, now=11.15)
check("two Top presses stay within track<->folder (0/1), never reach 2",
      d.layer, 0)
d.set_layer(2)
d.feed(top.byte, now=11.2); d.feed(top.release_byte, now=11.25)
check("Top pressed while on video layer does not move off it",
      d.layer, 2)

print("\n--- T14: held_actions()/held_since() reflect real physical state ---")
# dpad_up has no long/double binding, so its release emits nothing at all
# (see T1-style plain presses) -- held_actions()/held_since() are what
# volume auto-repeat relies on instead, since there is no release EVENT to
# key off of for a control like this.
d = mk()
check("nothing held yet", d.held_actions(), {})
d.feed(up.byte, now=12.0)
check("dpad_up now held, resolved to its press action",
      d.held_actions(), {"dpad_up": km.resolve(0, "press", "dpad_up")})
check("held_since reports the press time", d.held_since("dpad_up"), 12.0)
check("held_since is None for anything not held", d.held_since("dpad_down"), None)
d.feed(up.release_byte, now=12.5)
check("released -> no longer held", d.held_actions(), {})
check("held_since None after release", d.held_since("dpad_up"), None)

print("\n--- T15: the modifier and layer-toggle are excluded from held_actions() ---")
# Neither has a meaningful "repeat" action, and the modifier's OWN tap
# binding is a distinct thing from "what should repeat while held".
d = mk()
d.feed(side.byte, now=13.0)   # modifier held
check("modifier held but excluded from held_actions()", d.held_actions(), {})
d.feed(side.release_byte, now=13.1)
d.feed(top.byte, now=13.2)    # layer toggle held
check("layer toggle held but excluded from held_actions()", d.held_actions(), {})
d.feed(top.release_byte, now=13.3)

print()
if fails: print("FAILURES:", fails); sys.exit(1)
print("All decoder tests passed.")
