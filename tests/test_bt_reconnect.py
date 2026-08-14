"""bt._connect_with_retry / connect_blocking: retries a failed BR/EDR connect
instead of giving up after one attempt.

The bug this covers: a single `bluetoothctl connect` commonly fails with
br-connection-page-timeout even when the remote device never left range —
confirmed live, reconnecting a Bose SL III failed 3 times in a row before
succeeding on the 4th attempt, nothing else changing between tries. Before
this fix, every code path here (the Stream Deck's BT picker, the automatic
reconnect helper, bin/bt-connect) made exactly one attempt and reported
failure, which is why reconnecting "usually took 3-4 presses of the BT key"
— each press really only tried once.

Exercised against a fake `_bt()` (the bluetoothctl subprocess wrapper) and a
fake PipeWireControl, so this runs fast with no real Bluetooth hardware.
"""
import _bootstrap  # noqa: F401
import time

import player.bt as bt_mod

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeSink:
    pass


class FakePipeWireControl:
    """Sink appears immediately once instantiated -- connect_blocking creates
    this AFTER the connect succeeds, so "sink already there" is realistic."""
    def __init__(self, sink_present=True):
        self._present = sink_present

    def find_sink(self, needle):
        return FakeSink() if self._present else None


def with_fake_bt(script, real_fn):
    """Replace module-level _bt with one that pops canned responses for
    'connect' calls and delegates everything else to a no-op, tracking call
    count."""
    calls = {"connect": 0}

    def fake_bt(*args, timeout=20.0):
        if args and args[0] == "connect":
            calls["connect"] += 1
            return script.pop(0) if script else "Failed to connect: org.bluez.Error.Failed br-connection-page-timeout"
        return ""

    bt_mod._bt = fake_bt
    return calls


_real_bt = bt_mod._bt

print("=== _connect_with_retry: fails 3 times, succeeds on the 4th, same as observed live ===")
calls = with_fake_bt([
    "Failed to connect: org.bluez.Error.Failed br-connection-page-timeout",
    "Failed to connect: org.bluez.Error.Failed br-connection-page-timeout",
    "Failed to connect: org.bluez.Error.Failed br-connection-page-timeout",
    "Connection successful",
], _real_bt)
ok = bt_mod._connect_with_retry("AA:BB:CC:DD:EE:FF", attempts=5, retry_delay=0.01)
check("eventually succeeds", ok, True)
check("took exactly 4 attempts", calls["connect"], 4)
bt_mod._bt = _real_bt

print("\n=== _connect_with_retry: exhausts all attempts, reports failure ===")
calls = with_fake_bt([], _real_bt)  # every call falls through to the default failure
ok = bt_mod._connect_with_retry("AA:BB:CC:DD:EE:FF", attempts=3, retry_delay=0.01)
check("gives up after exhausting attempts", ok, False)
check("made exactly `attempts` tries, not more", calls["connect"], 3)
bt_mod._bt = _real_bt

print("\n=== _connect_with_retry: succeeds first try -> exactly one call, no wasted retries ===")
calls = with_fake_bt(["Connection successful"], _real_bt)
t0 = time.monotonic()
ok = bt_mod._connect_with_retry("AA:BB:CC:DD:EE:FF", attempts=5, retry_delay=0.01)
elapsed = time.monotonic() - t0
check("succeeds", ok, True)
check("exactly one call -- no retry needed", calls["connect"], 1)
check("no retry delay was waited", elapsed < 0.05, True)
bt_mod._bt = _real_bt

print("\n=== connect_blocking end-to-end: retry succeeds, then waits for the PipeWire sink ===")
calls = with_fake_bt([
    "Failed to connect: org.bluez.Error.Failed br-connection-page-timeout",
    "Connection successful",
], _real_bt)
import player.outputs as outputs_mod
_real_pwc = outputs_mod.PipeWireControl
outputs_mod.PipeWireControl = lambda: FakePipeWireControl(sink_present=True)
ok = bt_mod.connect_blocking("AA:BB:CC:DD:EE:FF", sink_wait_seconds=1.0)
check("connect_blocking succeeds after an internal retry", ok, True)
check("two connect attempts", calls["connect"], 2)
outputs_mod.PipeWireControl = _real_pwc
bt_mod._bt = _real_bt

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All bt-reconnect tests passed.")
