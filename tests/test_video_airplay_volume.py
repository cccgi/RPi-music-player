"""Reproduces the live bug: "video volume resets to 25% every song skip
while AirPlaying".

_clamp_video_volume_if_airplay used to unconditionally clamp mpv's volume
down to AIRPLAY_SAFE_VOLUME (25) any time it was found above that ceiling —
called after EVERY song skip (_video_advance). That meant a deliberate
video_volume_up past 25% got wiped back down on the very next skip, with no
way to actually keep the volume above the safety ceiling while AirPlaying.

The fix tracks ctx.video_last_set_volume (what WE last intentionally set)
and only reclamps when mpv's live volume has drifted away from that on its
own (e.g. mpv restarting and reverting to its 100% default) — a deliberate
raise via video_volume_up/down is remembered and left alone across skips.
"""
import _bootstrap  # noqa: F401
_bootstrap.require("mpd")
from types import SimpleNamespace

from player.config import setup_logging
from player.actions import (
    ActionContext, dispatch, _clamp_video_volume_if_airplay, AIRPLAY_SAFE_VOLUME,
)
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class FakeVideoCommander:
    def __init__(self, initial=100):
        self._volume = initial

    def volume(self):
        return self._volume

    def set_volume(self, value):
        self._volume = max(0, min(100, value))
        return self._volume

    def change_volume(self, delta):
        return self.set_volume(self._volume + delta)

    def status(self):
        return {"loaded": False}  # skip _capture_video_position's remember step

    def load_plain(self, path):
        pass


class FakeRouter:
    def __init__(self, icon="airplay"):
        self._route = SimpleNamespace(icon=icon)

    def current(self):
        return self._route

    def reassert_current(self):
        pass


class FakeEntry:
    path = "song.mkv"
    song = "Song"
    singer = ""


class FakeLibrary:
    def __init__(self):
        self.current = FakeEntry()

    def next(self):
        return FakeEntry()

    def prev(self):
        return FakeEntry()

    def take_resume_position(self, path):
        return None


def make_ctx(icon="airplay", initial_volume=100):
    return ActionContext(
        mpd=None, router=FakeRouter(icon), bus=SimpleNamespace(publish=lambda *a, **k: None),
        video=FakeVideoCommander(initial_volume), video_library=FakeLibrary(),
    )


print("=== _clamp_video_volume_if_airplay: direct unit tests ===")

ctx = make_ctx(initial_volume=100)
_clamp_video_volume_if_airplay(ctx)
check("fresh clamp on 100% -> ceiling", ctx.video.volume(), AIRPLAY_SAFE_VOLUME)
check("baseline recorded after clamp", ctx.video_last_set_volume, AIRPLAY_SAFE_VOLUME)

ctx2 = make_ctx(initial_volume=100)
_clamp_video_volume_if_airplay(ctx2)  # baseline: 25
ctx2.video.set_volume(50)             # simulate a deliberate raise past the ceiling
ctx2.video_last_set_volume = 50       # (what video_volume_up would record)
_clamp_video_volume_if_airplay(ctx2)  # a subsequent skip's clamp call
check("deliberate raise past ceiling survives a clamp call", ctx2.video.volume(), 50)

ctx3 = make_ctx(initial_volume=100)
_clamp_video_volume_if_airplay(ctx3)     # baseline: 25
ctx3.video.set_volume(50)                # simulate a deliberate raise
ctx3.video_last_set_volume = 50
ctx3.video._volume = 100                 # mpv silently reverted (e.g. restart), no record updated
_clamp_video_volume_if_airplay(ctx3)
check("undocumented drift back to 100 IS reclamped", ctx3.video.volume(), AIRPLAY_SAFE_VOLUME)

ctx4 = make_ctx(icon="bt", initial_volume=100)
_clamp_video_volume_if_airplay(ctx4)
check("non-AirPlay route -> never clamped", ctx4.video.volume(), 100)


print("\n=== end-to-end: video_volume_up survives repeated video_next_song ===")

ctx5 = make_ctx(initial_volume=100)
dispatch("video_next_song", ctx5, 1)  # first skip while at mpv's raw default
check("first skip clamps the untouched 100% default", ctx5.video.volume(), AIRPLAY_SAFE_VOLUME)

dispatch("video_volume_up", ctx5, 1)  # user deliberately raises past the ceiling
raised = ctx5.video.volume()
check("volume_up actually moved it above the ceiling", raised > AIRPLAY_SAFE_VOLUME, True)

for _ in range(5):
    dispatch("video_next_song", ctx5, 1)
check("volume stays exactly where the user set it across 5 more skips",
      ctx5.video.volume(), raised)

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All video-airplay-volume tests passed.")
