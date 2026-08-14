"""Video page's Wi-Fi off/on toggle (battery saver for on-the-road use).

Covers:
  - query_wifi_enabled() parses `nmcli radio wifi` correctly, and defaults
    to True (on) on any failure -- matching what the boot-time
    rpi-player-wifi-on.service enforces anyway, so a query failure never
    falsely shows the key as already off.
  - toggle_wifi flips ctx.wifi_enabled (and the state used to render the
    key) only when the nmcli command actually succeeds.
  - the on/off state is plain ActionContext state, untouched by
    enter_video_mode/exit_video_mode -- so it stays exactly as the user left
    it across any number of Video<->Music switches, only resetting at the
    next process restart (a real reboot's actual reset is
    rpi-player-wifi-on.service's job, not something this process does).
"""
import _bootstrap  # noqa: F401
_bootstrap.require("mpd")
from types import SimpleNamespace

from player.config import setup_logging
from player import actions
from player.actions import ActionContext, dispatch, query_wifi_enabled
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_ctx(wifi_enabled=True):
    return ActionContext(
        mpd=None, router=None, bus=SimpleNamespace(publish=lambda *a, **k: None),
        wifi_enabled=wifi_enabled,
    )


print("=== query_wifi_enabled ===")

calls = []
def fake_run_enabled(args, **kwargs):
    calls.append(args)
    return FakeCompleted(returncode=0, stdout="enabled\n")
actions.subprocess.run = fake_run_enabled
check("radio reported 'enabled' -> True", query_wifi_enabled(), True)
check("queried with the right nmcli subcommand", calls[-1][-2:], ["radio", "wifi"])

actions.subprocess.run = lambda args, **kw: FakeCompleted(returncode=0, stdout="disabled\n")
check("radio reported 'disabled' -> False", query_wifi_enabled(), False)

actions.subprocess.run = lambda args, **kw: FakeCompleted(returncode=1, stdout="")
check("nmcli exit != 0 -> defaults to True (assume on)", query_wifi_enabled(), True)

def raise_oserror(args, **kw):
    raise OSError("nmcli not found")
actions.subprocess.run = raise_oserror
check("nmcli missing entirely -> defaults to True", query_wifi_enabled(), True)


print("\n=== toggle_wifi ===")

calls = []
def fake_run_ok(args, **kwargs):
    calls.append(args)
    return FakeCompleted(returncode=0)
actions.subprocess.run = fake_run_ok

ctx = make_ctx(wifi_enabled=True)
toast = dispatch("toggle_wifi", ctx, 1)
check("turning off from 'on' issues 'nmcli radio wifi off'", calls[-1][-3:], ["radio", "wifi", "off"])
check("ctx.wifi_enabled flips to False", ctx.wifi_enabled, False)
check("toast reflects the new state", toast, "Wifi Off")

toast2 = dispatch("toggle_wifi", ctx, 1)
check("turning back on issues 'nmcli radio wifi on'", calls[-1][-3:], ["radio", "wifi", "on"])
check("ctx.wifi_enabled flips back to True", ctx.wifi_enabled, True)
check("toast reflects the new state", toast2, "Wifi On")

print("\n=== toggle_wifi: a failed nmcli call must not lie about the state ===")

ctx2 = make_ctx(wifi_enabled=True)
actions.subprocess.run = lambda args, **kw: FakeCompleted(returncode=1, stderr="not authorized")
toast3 = dispatch("toggle_wifi", ctx2, 1)
check("failed command -> wifi_enabled unchanged", ctx2.wifi_enabled, True)
check("failed command -> failure toast, not a false 'Wifi Off'", toast3, "Wifi failed")

ctx3 = make_ctx(wifi_enabled=True)
def raise_timeout(args, **kw):
    import subprocess as sp
    raise sp.TimeoutExpired(cmd=args, timeout=10)
actions.subprocess.run = raise_timeout
toast4 = dispatch("toggle_wifi", ctx3, 1)
check("nmcli timeout -> wifi_enabled unchanged", ctx3.wifi_enabled, True)
check("nmcli timeout -> failure toast", toast4, "Wifi failed")

print("\n=== survives Video<->Music mode switches (not touched by either action) ===")

actions.subprocess.run = fake_run_ok
ctx4 = make_ctx(wifi_enabled=True)
dispatch("toggle_wifi", ctx4, 1)   # user turns it off before a drive
check("wifi now off", ctx4.wifi_enabled, False)
dispatch("enter_video_mode", ctx4, 1)   # video=None -> returns early, but must not touch wifi
check("entering video mode (even a no-op one) leaves wifi_enabled alone", ctx4.wifi_enabled, False)
dispatch("exit_video_mode", ctx4, 1)
check("exiting video mode leaves wifi_enabled alone", ctx4.wifi_enabled, False)
dispatch("enter_video_mode", ctx4, 1)
check("back into video mode again -- still off", ctx4.wifi_enabled, False)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All wifi-toggle tests passed.")
