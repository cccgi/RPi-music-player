"""player/bt.py: the Stream Deck Bluetooth picker's "Reset" button
(list_paired_not_connected / reset_pairing_blocking /
reset_failed_pairings_blocking).

This is the offroad replacement for the SSH dance that was needed live to
unstick the Bose SLIII: `bluetoothctl remove <mac>` followed, only if that
alone didn't actually clear the device's info, by a `systemctl restart
bluetooth` fallback and a second `remove`. Everything here runs against a
fake in-memory bluetoothd (no real `bluetoothctl`/`systemctl` calls) so the
retry-vs-restart-fallback logic can be tested without a Pi.

Covers:
  - list_paired_not_connected() only surfaces paired devices that are NOT
    currently connected -- a working, connected device must never be
    touched by a "reset failed pairings" sweep.
  - reset_pairing_blocking(): the common case (remove clears it first try)
    never triggers the bluetooth service restart at all.
  - reset_pairing_blocking(): the stuck case (remove leaves a residual
    record, exactly like the live Bose SLIII incident) DOES trigger a
    restart, and only then actually clears.
  - reset_failed_pairings_blocking(): resets multiple stuck devices in one
    pass and returns an accurate count, restarting the service at most once
    for the whole batch rather than once per straggler.
  - reset_failed_pairings_blocking(): returns 0 and never touches the
    service at all when nothing is stuck.
"""
import _bootstrap  # noqa: F401

from player.config import setup_logging
from player import bt
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeBluetoothd:
    """Minimal stand-in for bluetoothd's view of paired devices, driven
    entirely through the same `bluetoothctl`-shaped text bt._bt() normally
    parses -- so the code under test never knows it isn't talking to the
    real thing."""

    def __init__(self):
        # mac -> {"name": str, "connected": bool, "stuck": bool}
        # "stuck" means: a `remove` call is silently ignored (mimics the
        # live BlueZ quirk of leaving a residual on-disk record) until
        # `restart_count` has been incremented at least once.
        self.devices: dict[str, dict] = {}
        self.restart_count = 0

    def add(self, mac, name, connected=False, stuck=False):
        self.devices[mac] = {"name": name, "connected": connected, "stuck": stuck}

    def bt(self, *args, timeout=20.0):
        if args[0] == "paired-devices":
            return "\n".join(f"Device {mac} {d['name']}" for mac, d in self.devices.items())
        if args[0] == "info":
            mac = args[1]
            d = self.devices.get(mac)
            if d is None:
                return ""  # bluetoothd has no record -- exactly what a clean remove looks like
            return (f"Device {mac} ({d['name']})\n"
                    f"\tPaired: yes\n"
                    f"\tConnected: {'yes' if d['connected'] else 'no'}\n")
        if args[0] == "remove":
            mac = args[1]
            d = self.devices.get(mac)
            if d is not None and (not d["stuck"] or self.restart_count > 0):
                del self.devices[mac]
            return ""
        return ""

    def restart(self):
        self.restart_count += 1
        # A restart is what actually clears every stuck record in real
        # bluetoothd -- mirror that by un-sticking everything left.
        for d in self.devices.values():
            d["stuck"] = False


print("=== list_paired_not_connected: only paired-but-disconnected devices, "
      "a connected device is left alone ===")
fake = FakeBluetoothd()
fake.add("AA:AA:AA:AA:AA:AA", "Working Headphones", connected=True)
fake.add("BB:BB:BB:BB:BB:BB", "Stuck Speaker", connected=False)
bt._bt = fake.bt
got = bt.list_paired_not_connected()
check("only the disconnected device is returned",
      got, [("BB:BB:BB:BB:BB:BB", "Stuck Speaker")])

print("\n=== reset_pairing_blocking: common case -- remove clears it on the "
      "first try, no service restart needed ===")
fake = FakeBluetoothd()
fake.add("CC:CC:CC:CC:CC:CC", "Easy Speaker", connected=False, stuck=False)
bt._bt = fake.bt
bt._restart_bluetooth_service = fake.restart
ok = bt.reset_pairing_blocking("CC:CC:CC:CC:CC:CC")
check("reset reports success", ok, True)
check("device is gone from bluetoothd", "CC:CC:CC:CC:CC:CC" in fake.devices, False)
check("no restart was needed for the easy case", fake.restart_count, 0)

print("\n=== reset_pairing_blocking: stuck case (the live Bose SLIII bug) -- "
      "plain remove leaves a residual record, so a restart is needed and "
      "used automatically ===")
fake = FakeBluetoothd()
fake.add("DD:DD:DD:DD:DD:DD", "Bose SLIII", connected=False, stuck=True)
bt._bt = fake.bt
bt._restart_bluetooth_service = fake.restart
ok = bt.reset_pairing_blocking("DD:DD:DD:DD:DD:DD")
check("reset still reports success after falling back to a restart", ok, True)
check("device is gone from bluetoothd", "DD:DD:DD:DD:DD:DD" in fake.devices, False)
check("exactly one restart was used", fake.restart_count, 1)

print("\n=== reset_failed_pairings_blocking: nothing stuck -> 0, and the "
      "service is never touched ===")
fake = FakeBluetoothd()
fake.add("EE:EE:EE:EE:EE:EE", "Connected Thing", connected=True)
bt._bt = fake.bt
bt._restart_bluetooth_service = fake.restart
count = bt.reset_failed_pairings_blocking()
check("nothing to reset", count, 0)
check("connected device untouched", "EE:EE:EE:EE:EE:EE" in fake.devices, True)
check("service never restarted", fake.restart_count, 0)

print("\n=== reset_failed_pairings_blocking: a batch of stuck devices is "
      "cleared in one pass, restarting the service at most once for the "
      "whole batch (not once per device) ===")
fake = FakeBluetoothd()
fake.add("F1:F1:F1:F1:F1:F1", "Stuck One", connected=False, stuck=True)
fake.add("F2:F2:F2:F2:F2:F2", "Stuck Two", connected=False, stuck=True)
fake.add("F3:F3:F3:F3:F3:F3", "Easy Three", connected=False, stuck=False)
fake.add("F4:F4:F4:F4:F4:F4", "Still Connected", connected=True, stuck=False)
bt._bt = fake.bt
bt._restart_bluetooth_service = fake.restart
count = bt.reset_failed_pairings_blocking()
check("all 3 disconnected devices got cleared", count, 3)
check("exactly one restart handled the whole stuck batch", fake.restart_count, 1)
check("the still-connected device was never even considered",
      "F4:F4:F4:F4:F4:F4" in fake.devices, True)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All bt reset tests passed.")
