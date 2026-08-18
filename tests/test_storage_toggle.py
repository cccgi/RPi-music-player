"""toggle_storage_source / video_toggle_storage_source: pure symlink
manipulation between an "-internal" directory and a mounted USB drive's
Audio/Video subfolder. MPD's music_directory and config.toml's
video.library_dir never move -- only the symlink TARGET does.

Monkeypatches the module-level path constants (actions._MUSIC_LINK etc.) to
point into a temp directory instead of the real /home/rpi/... paths, per
the task's explicit instruction -- this never touches the real filesystem
outside of a tempfile.TemporaryDirectory.
"""
import _bootstrap  # noqa: F401
import os
import tempfile
from pathlib import Path

_bootstrap.require("mpd")
from player import actions
from player.actions import ActionContext, dispatch
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
    def __init__(self, state="stop"):
        self._state = state
        self.calls = []

    def status(self):
        return {"state": self._state}

    def stop(self):
        self.calls.append("stop")
        self._state = "stop"

    def update_database(self):
        self.calls.append("update")


def _make_music_layout(tmp):
    """internal dir (with content) + link pointing at it, USB dir absent."""
    internal = Path(tmp) / "Music-internal"
    internal.mkdir()
    (internal / "song.flac").write_text("x")
    link = Path(tmp) / "Music"
    os.symlink(internal, link)
    usb = Path(tmp) / "usb-storage" / "Audio"
    return link, internal, usb


print("=== toggle_storage_source: internal -> USB (mounted), fire-and-forget scan ===")
with tempfile.TemporaryDirectory() as tmp:
    link, internal, usb = _make_music_layout(tmp)
    usb.mkdir(parents=True)
    (usb / "usb-song.flac").write_text("y")

    actions._MUSIC_LINK = link
    actions._MUSIC_INTERNAL = internal
    actions._MUSIC_USB = usb

    check("label starts as Internal", actions.music_storage_label(), "Internal")

    mpd = FakeMpd(state="play")
    ctx = ActionContext(mpd=mpd, router=None, bus=NullBus())
    toast = dispatch("toggle_storage_source", ctx, 1)

    check("toast says USB", toast, "USB")
    check("playback was stopped first (was playing)", "stop" in mpd.calls, True)
    # mpd update() is fire-and-forget at the MPD PROTOCOL level -- MPD acks
    # immediately and scans in ITS OWN background thread, so triggering it
    # does not make this action (or the button press) slow. Skipping it
    # entirely (an earlier iteration of this button) left MPD's database
    # permanently stale relative to the new source -- reported live as a
    # stale folder listing that never went away. See toggle_storage_source's
    # docstring for the full story.
    check("mpd database update WAS triggered", "update" in mpd.calls, True)
    check("symlink now points at the USB dir", os.readlink(link), str(usb))
    check("label now reads USB", actions.music_storage_label(), "USB")

    print("\n=== toggle_storage_source: USB -> internal (toggle back) ===")
    mpd2 = FakeMpd(state="stop")
    ctx2 = ActionContext(mpd=mpd2, router=None, bus=NullBus())
    toast2 = dispatch("toggle_storage_source", ctx2, 1)
    check("toast says Internal", toast2, "Internal")
    check("not playing -> stop() never called, update still triggered",
          mpd2.calls, ["update"])
    check("symlink back on the internal dir", os.readlink(link), str(internal))

print("\n=== toggle_storage_source: USB not mounted -> refuses, symlink untouched ===")
with tempfile.TemporaryDirectory() as tmp:
    link, internal, usb = _make_music_layout(tmp)
    # usb intentionally never created -- simulates "drive unplugged".
    actions._MUSIC_LINK = link
    actions._MUSIC_INTERNAL = internal
    actions._MUSIC_USB = usb

    mpd = FakeMpd(state="stop")
    ctx = ActionContext(mpd=mpd, router=None, bus=NullBus())
    toast = dispatch("toggle_storage_source", ctx, 1)
    check("clear failure toast", toast, "USB not mounted")
    check("symlink was never touched", os.readlink(link), str(internal))
    check("no database update was triggered on failure", mpd.calls, [])
    check("label still reads Internal", actions.music_storage_label(), "Internal")


class FakeVideoLibrary:
    def __init__(self):
        self.rescan_calls = 0

    def rescan(self):
        self.rescan_calls += 1
        return 7


print("\n=== video_toggle_storage_source: internal -> USB, synchronous rescan "
      "(cheap even for a real library -- see the action's docstring) ===")
with tempfile.TemporaryDirectory() as tmp:
    internal = Path(tmp) / "Video-internal"
    internal.mkdir()
    link = Path(tmp) / "Video"
    os.symlink(internal, link)
    usb = Path(tmp) / "usb-storage" / "Video"
    usb.mkdir(parents=True)

    actions._VIDEO_LINK = link
    actions._VIDEO_INTERNAL = internal
    actions._VIDEO_USB = usb

    lib = FakeVideoLibrary()
    ctx = ActionContext(mpd=FakeMpd(), router=None, bus=NullBus(), video_library=lib)
    toast = dispatch("video_toggle_storage_source", ctx, 1)
    check("toast mentions USB and the new count", toast, "USB 7")
    check("rescan happened synchronously", lib.rescan_calls, 1)
    check("symlink swapped to USB", os.readlink(link), str(usb))
    check("label reads USB", actions.video_storage_label(), "USB")

print("\n=== video_toggle_storage_source: USB not mounted -> refuses ===")
with tempfile.TemporaryDirectory() as tmp:
    internal = Path(tmp) / "Video-internal"
    internal.mkdir()
    link = Path(tmp) / "Video"
    os.symlink(internal, link)
    usb = Path(tmp) / "usb-storage" / "Video"  # never created

    actions._VIDEO_LINK = link
    actions._VIDEO_INTERNAL = internal
    actions._VIDEO_USB = usb

    lib = FakeVideoLibrary()
    ctx = ActionContext(mpd=FakeMpd(), router=None, bus=NullBus(), video_library=lib)
    toast = dispatch("video_toggle_storage_source", ctx, 1)
    check("clear failure toast", toast, "USB not mounted")
    check("rescan never happened on failure", lib.rescan_calls, 0)
    check("symlink untouched", os.readlink(link), str(internal))

print("\n=== set_storage_internal/set_storage_usb: explicit-set is idempotent, "
      "unlike toggle -- pressing USB while already on USB stays on USB "
      "(a plain toggle would flip back to Internal) ===")
with tempfile.TemporaryDirectory() as tmp:
    link, internal, usb = _make_music_layout(tmp)
    usb.mkdir(parents=True)
    (usb / "usb-song.flac").write_text("y")
    actions._MUSIC_LINK = link
    actions._MUSIC_INTERNAL = internal
    actions._MUSIC_USB = usb

    mpd = FakeMpd(state="stop")
    ctx = ActionContext(mpd=mpd, router=None, bus=NullBus())
    check("set_storage_usb switches to USB",
          dispatch("set_storage_usb", ctx, 1), "USB")
    check("pressing USB again stays on USB (idempotent, not a flip)",
          dispatch("set_storage_usb", ctx, 1), "USB")
    check("symlink still points at USB", os.readlink(link), str(usb))
    check("set_storage_internal switches back",
          dispatch("set_storage_internal", ctx, 1), "Internal")
    check("symlink back on internal", os.readlink(link), str(internal))

print("\n=== set_storage_usb: USB not mounted -> refuses, symlink untouched ===")
with tempfile.TemporaryDirectory() as tmp:
    link, internal, usb = _make_music_layout(tmp)
    # usb intentionally never created
    actions._MUSIC_LINK = link
    actions._MUSIC_INTERNAL = internal
    actions._MUSIC_USB = usb

    ctx = ActionContext(mpd=FakeMpd(state="stop"), router=None, bus=NullBus())
    toast = dispatch("set_storage_usb", ctx, 1)
    check("clear failure toast", toast, "USB not mounted")
    check("symlink untouched", os.readlink(link), str(internal))
    check("music_usb_available reports False", actions.music_usb_available(), False)

print("\n=== video_set_storage_internal/video_set_storage_usb: same "
      "idempotent-set behaviour, synchronous rescan ===")
with tempfile.TemporaryDirectory() as tmp:
    internal = Path(tmp) / "Video-internal"
    internal.mkdir()
    link = Path(tmp) / "Video"
    os.symlink(internal, link)
    usb = Path(tmp) / "usb-storage" / "Video"
    usb.mkdir(parents=True)
    actions._VIDEO_LINK = link
    actions._VIDEO_INTERNAL = internal
    actions._VIDEO_USB = usb
    # video_usb_available() now also requires the mountpoint itself to be a
    # real mount (usb_mounted()), not just that the Video subfolder exists --
    # see actions.usb_mounted()'s docstring. A plain temp directory is never
    # a real mount, so point USB_MOUNT_POINT somewhere usb_mounted() will
    # legitimately report True: the process's own root filesystem mount.
    actions.USB_MOUNT_POINT = Path("/")

    lib = FakeVideoLibrary()
    ctx = ActionContext(mpd=FakeMpd(), router=None, bus=NullBus(), video_library=lib)
    check("video_set_storage_usb switches to USB",
          dispatch("video_set_storage_usb", ctx, 1), "USB 7")
    check("pressing USB again stays on USB",
          dispatch("video_set_storage_usb", ctx, 1), "USB 7")
    check("symlink still on USB", os.readlink(link), str(usb))
    check("video_usb_available reports True", actions.video_usb_available(), True)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All storage-toggle tests passed.")
