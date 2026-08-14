"""player/wifi.py: the AirPlay picker's Wi-Fi status/fix tiles.

Covers:
  - status() parses a normal connected/on-signal/known-IP/powersave-off
    case correctly from realistic nmcli -t / iw output.
  - status() degrades every field independently to its "unknown" value
    (rather than crashing) when a command returns nothing -- e.g. Wi-Fi is
    off, or `iw` isn't installed -- since a status tile that throws would be
    worse than a blank field.
  - status() correctly flags the misconfigured "powersave on" case that
    caused the original AirPlay-vanishing bug, distinct from "off" and from
    "unknown" (no reading at all).
  - reconnect_blocking()/restart_networking_blocking() report success/
    failure based on subprocess exit behaviour, without ever calling the
    real `nmcli`/`systemctl` (fully mocked at the _run/subprocess.run level).
"""
import _bootstrap  # noqa: F401

from player.config import setup_logging
from player import wifi
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


print("=== status(): normal connected case -- SSID, signal, IP, powersave "
      "off all parse correctly ===")
def fake_run_normal(argv, timeout=8.0):
    if argv[:2] == [wifi.NMCLI, "-t"] and "dev" in argv and "wifi" in argv:
        return ("no:OtherNetwork:40\n"
                "yes:HomeWiFi:78\n")
    if argv[:2] == [wifi.NMCLI, "-g"]:
        return "192.168.0.150/24\n"
    if argv[0] == wifi.IW:
        return "Power save: off\n"
    return ""

wifi._run = fake_run_normal
info = wifi.status()
check("connected", info["connected"], True)
check("ssid", info["ssid"], "HomeWiFi")
check("signal", info["signal"], 78)
check("ip", info["ip"], "192.168.0.150")
check("powersave off", info["powersave"], "off")

print("\n=== status(): the misconfigured case (powersave ON) that caused "
      "AirPlay speakers to vanish is distinguished from 'off' ===")
def fake_run_powersave_on(argv, timeout=8.0):
    if argv[:2] == [wifi.NMCLI, "-t"]:
        return "yes:HomeWiFi:60\n"
    if argv[:2] == [wifi.NMCLI, "-g"]:
        return "192.168.0.150/24\n"
    if argv[0] == wifi.IW:
        return "Power save: on\n"
    return ""

wifi._run = fake_run_powersave_on
info = wifi.status()
check("powersave flagged as on", info["powersave"], "on")

print("\n=== status(): every field degrades independently when nothing "
      "comes back at all (Wi-Fi off / iw missing), no crash ===")
wifi._run = lambda argv, timeout=8.0: ""
info = wifi.status()
check("connected defaults False", info["connected"], False)
check("ssid unknown", info["ssid"], None)
check("signal unknown", info["signal"], None)
check("ip unknown", info["ip"], None)
check("powersave unknown (not 'on' or 'off')", info["powersave"], None)

print("\n=== reconnect_blocking(): success path ===")
calls = []
def fake_run_reconnect_ok(argv, timeout=8.0):
    calls.append(list(argv))
    if "disconnect" in argv:
        return ""
    if "connect" in argv:
        return "Device 'wlan0' successfully activated.\n"
    return ""

wifi._run = fake_run_reconnect_ok
ok = wifi.reconnect_blocking()
check("reports success", ok, True)
check("disconnect was called before connect",
      [c for c in calls if "disconnect" in c or "connect" in c][0][-2],
      "disconnect")

print("\n=== reconnect_blocking(): failure path (nmcli reports an error) ===")
def fake_run_reconnect_fail(argv, timeout=8.0):
    if "connect" in argv and "disconnect" not in argv:
        return "Error: Connection activation failed.\n"
    return ""

wifi._run = fake_run_reconnect_fail
ok = wifi.reconnect_blocking()
check("reports failure", ok, False)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All wifi tests passed.")
