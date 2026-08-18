"""usb_mounted() / music_usb_available() / video_usb_available(): the
mount-aware availability checks that the eject-detection watcher
(streamdeck_daemon._watch_usb) relies on to tell "drive physically present"
apart from "the /mnt/usb-storage directory merely exists" (which stays true
even after a real unplug, since the mountpoint itself lives on the internal
SD card, not the USB drive).

Monkeypatches actions.USB_MOUNT_POINT/_MUSIC_USB/_VIDEO_USB into a temp
directory -- os.path.ismount() is real, called against real temp
directories/mountpoint-shaped paths, not mocked, so this exercises the
actual logic rather than a stand-in for it.
"""
import _bootstrap  # noqa: F401
import os
import tempfile
from pathlib import Path

_bootstrap.require("mpd")
from player import actions
from player.config import setup_logging
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


print("=== usb_mounted(): a plain directory is never a mountpoint ===")
with tempfile.TemporaryDirectory() as tmp:
    fake_mount = Path(tmp) / "usb-storage"
    fake_mount.mkdir()
    actions.USB_MOUNT_POINT = fake_mount
    check("plain directory is not a mount", actions.usb_mounted(), False)

print("\n=== usb_mounted(): a nonexistent path is not a mount (no crash) ===")
with tempfile.TemporaryDirectory() as tmp:
    actions.USB_MOUNT_POINT = Path(tmp) / "does-not-exist"
    check("nonexistent path is not a mount", actions.usb_mounted(), False)

print("\n=== music_usb_available()/video_usb_available(): False when the "
      "mountpoint itself isn't a real mount, even if the Audio/Video "
      "subfolder directories happen to exist on the underlying disk "
      "(the exact 'drive unplugged but directory shell still stat()s' case) ===")
with tempfile.TemporaryDirectory() as tmp:
    fake_mount = Path(tmp) / "usb-storage"
    audio = fake_mount / "Audio"
    video = fake_mount / "Video"
    audio.mkdir(parents=True)
    video.mkdir(parents=True)

    actions.USB_MOUNT_POINT = fake_mount
    actions._MUSIC_USB = audio
    actions._VIDEO_USB = video

    check("music unavailable: mountpoint isn't a real mount",
          actions.music_usb_available(), False)
    check("video unavailable: mountpoint isn't a real mount",
          actions.video_usb_available(), False)

print("\n=== usb_mounted(): OSError from os.path.ismount is treated as "
      "'not mounted', not raised (the yanked-mid-stat case) ===")
class _BoomPath:
    def __fspath__(self):
        raise OSError("simulated I/O error")

_real_ismount = os.path.ismount
def _boom_ismount(path):
    raise OSError("simulated I/O error")
os.path.ismount = _boom_ismount
try:
    actions.USB_MOUNT_POINT = Path("/mnt/usb-storage")
    check("OSError from ismount is swallowed as False", actions.usb_mounted(), False)
finally:
    os.path.ismount = _real_ismount

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All USB mount-detection tests passed.")
