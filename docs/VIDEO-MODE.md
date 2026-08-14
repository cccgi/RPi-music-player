# Video / karaoke mode — architecture plan

> **Status: implemented.** This was the design plan written before any of it
> existed; it is kept as-is below because the reasoning and the verified
> hardware findings (subfile demuxing, DRM output with no desktop, the
> IPC-only audio-add requirement) are still exactly correct and worth
> understanding before touching `player/video.py` or `player/skp.py`. What
> actually shipped follows this plan closely, with a few things worth
> knowing if you're comparing the two: the Stream Deck video page layout
> ended up mirroring the music page's grid cell-for-cell (Local/BT/AirPlay
> and Shutdown occupy the SAME keys in both modes, only their state changes)
> rather than the independent layout sketched in §6; a resume-from-skip
> feature remembers playback position on long videos; and `video-mpv.service`
> needed a `--vo=drm,null` fallback for the box to still play audio at all
> with no HDMI monitor connected (`--vo=drm` alone aborts the ENTIRE file
> load, audio included, if there's no valid DRM connector — a real, live-
> reproduced failure mode in the car, not a theoretical one). See
> [`DEPLOY.md`](DEPLOY.md) step 10 for the deploy steps and
> [`CONFIGURATION.md`](CONFIGURATION.md) for the `[video]` config reference.

Goal: play `.skp` karaoke files (video + switchable vocal/karaoke audio) to
an HDMI display, driven from the same TourBox + Stream Deck hardware as the
music player, with **no conversion pass** over the library — files are
demuxed and streamed on the fly, straight from wherever they live.

The `.skp` format itself is already fully solved (see the reverse-engineering
writeup this plan is based on): `["HomeKara" magic][header][JPEG
thumbnail][complete standalone .mkv, video-only][1+ complete standalone
.m4a files, one per audio track]`, all simply concatenated. Nothing here
re-derives that — it reuses it.

Three decisions were confirmed with you before writing this: the library
lives in a new `Video` folder at the same level as `Music`; playback is
on-the-fly (no `.mkv` pre-conversion); control is a dedicated Stream Deck
page.

---

## 1. What was actually verified on this Pi before trusting this plan

Rather than plan against the format guide's assumptions alone, the three
riskiest mechanisms were tested directly on the hardware tonight, with a
synthetic file built to mimic the real `.skp` structure (junk bytes, then a
real `.mkv`, then more junk, then a second real file — same shape as
`[header][thumbnail][video][audio]`):

| Mechanism | Result |
|---|---|
| Opening a **byte range** of a larger file as a standalone MKV, with zero copying | **Works.** `ffmpeg`'s `subfile` protocol, invoked as `lavf://subfile,,start,<N>,end,<N>,,:<path>`, correctly isolated a 27,650-byte slice out of a 77,516-byte file and mpv identified it as Matroska, `h264 320x240`, `aac` — no temp file written. |
| Rendering video **directly to the HDMI output with no desktop running** | **Works.** `mpv --vo=drm --gpu-context=drm` found the `vc4` driver, detected the connected `1920x1080@60Hz` display, and rendered via DRM atomic modeset — over plain SSH, no X11/Wayland session active. |
| **Dynamically attaching a second audio track** (the vocal/karaoke switch) at runtime | **Works, but only through the JSON IPC socket, not the CLI.** `mpv`'s `--audio-files=` command-line option splits its value on commas — which collides with the `subfile` protocol's own comma-delimited syntax and breaks it. The IPC `audio-add` command doesn't have this problem, since JSON arguments aren't passed through mpv's shell-style option tokenizer. **This is the one real gotcha found: the daemon must drive mpv exclusively over its IPC socket, never construct a static `--audio-files=subfile,...` command line.** |

`mpv 0.40.0` and `ffmpeg` are now installed on the device (`apt install mpv`
— it wasn't present before). Both are stock Debian Trixie packages, no
special build needed.

---

## 2. Storage

**The SD card cannot hold this library.** `df` shows 19GB free on the root
filesystem — a 56,353-file karaoke library is not going to fit regardless of
per-file size. `Video` needs to be a mount point for external storage (USB
drive, same pattern as any external drive), not a folder actually living on
`mmcblk0p2`.

Recommendation: mount it by **UUID** in `/etc/fstab`, the same lesson
already baked into this project elsewhere (never trust a `/dev/sdX` device
name across reboots/replugs) —

```fstab
UUID=xxxx-xxxx  /home/rpi/Video  exfat  defaults,nofail,uid=1000,gid=1000  0  2
```

`nofail` matters here specifically: if the drive isn't plugged in, the Pi
must still boot and play music normally — video mode should degrade to
"library not found," never take down the whole system.

---

## 3. Format parsing — `player/skp.py` (new)

Ports the **seek-based** scan logic from the reference Python tool (the
`scan_skp_fast`/`walk_mp4_boxes_seek` path, not the in-memory `parse_skp`
path) — files here can be large, and the Pi only has 2GB RAM, so nothing
should ever read a whole video into memory just to find its boundaries.

```python
def skp_offsets(path: str) -> SkpOffsets:
    """Returns byte ranges only — never reads track content into memory.

    SkpOffsets:
        video: (start, end)
        audio: list[(start, end)]   # 0, 1, 2, or more — same as the
                                     # reference tool, never assume exactly 2
    """
```

Directly reused, unchanged in logic (just ported from the uploaded
reference implementation):
- `read_vint_size` — EBML varint parsing for the MKV segment size
- `find_mkv_segment_end` — locates where the embedded video's own MKV
  Segment declares itself to end
- `find_ftyp_in_window` / `walk_mp4_boxes_seek` — locates each subsequent
  embedded MP4/M4A file by its `ftyp` box, using the **same fix already
  found the hard way** in the reference tool: a second `ftyp` box only ever
  means a new concatenated file has started, never a legitimate continuation
  of the current one
- The **search-window tolerance** for padding variance (0–16+ bytes seen
  between embedded files) — ported as-is, not re-derived

A `SkpOffsets` converts directly into `subfile://` URLs:

```python
def subfile_url(path: str, start: int, end: int) -> str:
    return f"lavf://subfile,,start,{start},end,{end},,:{path}"
```

---

## 4. Player engine — `video_daemon.py` (new), driving `mpv` over JSON IPC

**Why `mpv` and not MPD:** MPD is audio-only by design — it has no video
output path at all. `mpv` is the natural counterpart: scriptable over a
Unix-socket JSON IPC protocol (same idea as MPD's own protocol, different
wire format), hardware-accelerated decode via V3D/Hantro on the Pi 5, and
direct DRM output confirmed above.

**Lifecycle:** `mpv` runs as a persistent `systemd` service, like `mpd`
does — started once at boot with `--idle=yes`, holding the DRM output the
whole time (idle = black screen, not "off"), so entering video mode has
near-zero startup latency instead of a multi-second process spawn. It sits
completely idle and silent until a file is loaded.

```
[Service]
ExecStart=/usr/bin/mpv --idle=yes --input-ipc-server=/run/rpi-player/mpv.sock \
    --vo=drm --gpu-context=drm --hwdec=auto --keep-open=yes \
    --script-opts=osc-visibility=never
```

`video_daemon.py` is the only thing that talks to this socket. Its job:
- Resolve a chosen `.skp` file to a `SkpOffsets` (via `skp.py`)
- `loadfile <video subfile URL>` to start the video track
- `audio-add <audio subfile URL #1> select "Original (Vocal)"` for the
  first audio track, `audio-add <#2> "Karaoke (Backing Track)"` for the
  second (not selected) — **both over IPC, never the CLI flag**, per the
  finding above
- Default the active track to **vocal** (`aid` = the first `audio-add`),
  matching the "Original (Vocal)" convention already established in the
  reference tool and its `--iphone-default-track` default
- Expose `play_pause`, `next_track`, `prev_track`, `switch_track`,
  `seek(seconds)` methods that translate to the corresponding IPC commands
  (`cycle pause`, `set aid`, `seek`, etc.)
- Publish state (current file, elapsed/duration, which track is active) on
  the existing IPC bus (`ipc.py` / `bus.sock`), the same mechanism the
  TourBox daemon already uses to push toasts to the Stream Deck — no new
  transport needed, this one already exists and does exactly this job.

### The one real hazard: `hw:CARD=S3` is exclusive, and MPD holds it

`51-mpd-dac-ignore.conf` deliberately hides the Sound Blaster from PipeWire
so MPD can open it directly for bit-perfect output — which means `mpv`
**cannot** get to that device through PipeWire either, and can't share
`hw:` with MPD if MPD is still holding it open (which it does whenever the
`Local` output is *enabled*, even while paused — ALSA devices are
typically kept open across pause to avoid a click, not released).

The fix is an explicit handoff, owned by `video_daemon.py`, not a hope that
it works out:

1. **Entering video mode:** `mpc disable Local` (not just `stop`/`pause` —
   disabling is what actually makes MPD release the ALSA handle), *then*
   point `mpv`'s audio output at the same device
   (`--audio-device=alsa/hw:CARD=S3,DEV=0`, or `plughw:` if a video's
   sample rate needs resampling — `hw:` will hard-fail on a rate mismatch
   the way it does for MPD, so this needs the same care the HDMI `plughw:`
   lesson already taught).
2. **Leaving video mode:** unload the file (releases `mpv`'s hold on the
   device), then `mpc enable Local` to hand it back to MPD.

If the DAC route isn't the active one (say, Bluetooth is), there's no
conflict at all — `mpv` can just go through PipeWire normally like anything
else. The handoff logic only needs to trigger when `Local` is the active
route.

---

## 5. Library browsing — no MPD database involved

`.skp` files aren't music MPD can index, so the video library needs its own
scan, entirely separate from `browser.py`'s MPD-backed one — same visual
idea, different data source: walk `Video/`, split each filename on the
already-established `"Song - Singer.skp"` convention (reusing
`split_song_singer`, verbatim, from the reference tool) for display.

Given 56,353 files, this needs an index built once and cached (a flat list
in memory is ~a few MB even at that count — fine for 2GB RAM), refreshed on
a manual "rescan" action rather than watched live, since there's no
MPD-style `idle` event source for an arbitrary folder.

---

## 6. Control integration

**Mode switch lives on the Stream Deck**, as agreed — a `Video` key on the
main page switches to the new video page (part of the multi-page work
already pending); a `Music` key on the video page switches back. This is
also the natural place to trigger the MPD/mpv audio handoff from §4,
one-directional and explicit (not something either daemon has to infer).

**TourBox stays physically the same** while in video mode — same
buttons, different destination. Rather than duplicating TourBox's keymap,
the cleanest fit with what's already built: the `action` names dispatched
by TourBox (`next_track`, `prev_track`, `play_pause`, seek, `delete_current`
→ here, "skip song") stay identical; `actions.py` gains a thin mode check
that routes each one to either the MPD action (today) or the equivalent
`video_daemon` IPC call, based on which mode is currently active (tracked
via the same bus that already broadcasts route/toast state). This reuses
the layer system's muscle-memory principle — the buttons don't move,
what they're wired to does — without needing a second TourBox keymap file.

### Proposed video page layout (Stream Deck)

| Key(s) | Action |
|---|---|
| Browse rows (reuses the 4-key browse pattern from the music page) | Walk `Video/` folders and files |
| Play/Pause | `cycle pause` |
| Next / Prev | Next/previous file in the current folder |
| **Track** (new) | Toggle active audio track, shows "Vocal" / "Karaoke" |
| Progress key | Elapsed/duration, same rendering approach as the music progress key |
| **Music** | Leave video mode, hand the DAC back to MPD, return to the music page |

---

## 7. Open risks for Phase 1 to resolve with real `.skp` files

Nothing above was tested against an actual `.skp` file — only a synthetic
stand-in with the same byte layout, since the real library isn't on this Pi
yet. Specifically still unverified:

- **HEVC/4K hardware decode headroom.** The format guide notes some files
  are HEVC/4K. `--hwdec=auto` on the Pi 5's V3D/Hantro core should handle
  this, but wasn't tested — the synthetic clip was tiny H.264. Worth an
  early real-file test before assuming it holds for the whole library.
- **Real padding-variance and 0/1/3-track files**, exercising `skp.py`
  against the actual edge cases the reference tool's investigation found,
  not just the clean 2-track synthetic case here.
- **Actual `mpc disable`/`enable` timing** — how long MPD takes to release
  the ALSA handle in practice, so the Stream Deck's mode-switch key doesn't
  feel like it hung.

---

## 8. Suggested build order

1. `player/skp.py` — port the offset-finding logic, unit-test against a
   handful of real `.skp` files once the drive is mounted (no daemon yet)
2. `video_daemon.py` core — IPC-drive `mpv`, play a `.skp` file end to end
   from the command line/a debug script, confirm real-file video +
   both audio tracks + track switching, before touching any hardware UI
3. MPD/mpv audio handoff (§4) — the one piece with real failure modes,
   worth its own focused verification pass
4. Stream Deck video page (§6) — depends on the multi-page work (task #25),
   so this is also the natural point to finally build that
5. TourBox mode-routing (§6) — last, since it's the smallest piece once
   video_daemon's actions already exist and are proven from step 2

Steps 1–3 don't need any Stream Deck/TourBox work at all and can be fully
verified from the command line — recommend doing those first once the
drive with real `.skp` files is mounted.
