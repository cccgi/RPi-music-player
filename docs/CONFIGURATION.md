# Configuration reference

Two files, both under `/opt/rpi-player/etc/`, both preserved across
`install.sh` re-runs:

- **`config.toml`** — everything except the TourBox's byte→action mapping.
  Single source of truth for both daemons.
- **`keymap.toml`** — TourBox byte codes and the action each control/layer
  fires. See [`TOURBOX-NOTES.md`](TOURBOX-NOTES.md) for the protocol and why
  the shipped codes need re-learning on your specific unit.

Both files are heavily commented in place — this document is a map of what's
where and why, not a duplicate of those comments. Reload after any edit:

```bash
sudo systemctl restart tourbox-player streamdeck-player
```

---

## `config.toml` sections

| Section | Controls |
|---|---|
| `[mpd]` | Connection to MPD — host/port/timeout. TCP on loopback, deliberately not a Unix socket (see the in-file comment on why the socket path proved unreliable under systemd's `RuntimeDirectory`). |
| `[ipc]` | The optional NDJSON toast/status bus between daemons. Both daemons run fine if this socket is absent — a nicety, not a dependency. |
| `[log]` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `[tourbox]` | Device path, baud, the unlock handshake toggle (leave `send_unlock = false` unless you have an Elite), long-press timing, rotary coalescing. |
| `[tourbox.steps]` | Volume/seek step sizes, fast-spin thresholds, and the safety clamps (`max_volume_jump`, `max_seek_jump`) that stop a hard knob flick slamming the volume to 0/100. |
| `[streamdeck]` | Brightness, dim/blank timers, `keep_awake_while_playing` (never fully blank mid-album — playback alone isn't "interaction"). |
| `[streamdeck.theme]` | Colours and font paths. Font paths fall back to the `fonts-dejavu-core` system paths if a custom override is missing, so a bad path degrades to plain text rather than PIL's unreadable bitmap default. |
| `[[routes]]` | Output routing — see below, it's the one section worth understanding in full before editing. |
| `[power]` | GPIO shutdown button pin/hold time. `enabled = false` until you actually wire a button. |
| `[delete]` | The Delete key's behaviour — see below. |
| `[playback]` | Continuous folder playback, crossfade, skip-fade, the startup output safety net — see below. |
| `[video]` | Karaoke/`.skp` mode — library path, mpv IPC socket, timeout, the ALSA device string used when video's audio route is `Local`. |

---

## Output routing (`[[routes]]`)

The one thing worth internalizing: **MPD has two outputs, not three or four.**
Bluetooth and AirPlay are *both* PipeWire sinks behind the single `Network`
MPD output — picking between them is `wpctl set-default`, not an MPD-level
choice at all. `player/outputs.py` (`OutputRouter`) owns this two-layer
switch. Each `[[routes]]` entry is:

```toml
[[routes]]
id = "airplay"              # internal id — matched by default_route_id, etc.
label = "AirPlay"            # Stream Deck caption
icon = "airplay"             # icon key the renderer understands
mpd_output = "Network"       # which MPD audio_output block this enables
pw_sink = "raop_sink.Foo"    # substring match against `wpctl status` — "" for Local
bt_mac = "AA:BB:CC:DD:EE:FF" # optional — lets the BT key attempt an active reconnect
```

`pw_sink` is matched as a substring against PipeWire's node **name** (not the
human-readable description `wpctl status` prints — see
[`AUDIO-OUTPUT.md`](AUDIO-OUTPUT.md) section 6d for why matching against the
description finds nothing). Be as specific as your actual hardware needs —
`"raop_sink"` alone matches *every* AirPlay receiver on the LAN, including
other people's laptops with receiving enabled.

A route with no matching sink renders greyed out and is skipped by the
TourBox's cycle action — switching to an undiscovered sink would mean playing
to nothing, which is worse than just not offering it.

Only 3 routes get a dedicated Stream Deck key (the 4th slot is the Video mode
toggle). Any additional routes (see `airplay_arylic` in the shipped config)
are still reachable via the TourBox's `cycle_output`/`cycle_output_back`
actions, which walk the *entire* `[[routes]]` list regardless of what has a
panel key.

---

## Delete key (`[delete]`)

```toml
[delete]
enabled = true
mode = "trash"          # "queue" | "trash" | "permanent"
confirm = "none"
trash_dir = "/home/pi/Music-trash"
log_path  = "/home/pi/deleted-tracks.log"
music_dir = "/home/pi/Music"
```

| Mode | Effect |
|---|---|
| `queue` | Drops the track from the play queue. File untouched. |
| `trash` | Moves the file into `trash_dir`, preserving its library-relative path. Recoverable until you empty the trash yourself. |
| `permanent` | Unlinks the file. **Not recoverable.** |

Every mode logs to `log_path` with a timestamp — this is the only record of
what a permanent delete removed, and worth checking after your first few
presses regardless of mode. `music_dir` is a hard safety rail: whatever URI
MPD reports, the resolved path is checked to be *inside* `music_dir` before
anything happens to it — a malformed or crafted URI containing `../` cannot
escape the library, no matter what mode is configured.

Ordering matters internally and is worth knowing if you're reading the logs:
the daemon advances to the next track *before* touching the file, so playback
never continues trying to read something mid-delete.

---

## Playback behaviour (`[playback]`)

```toml
[playback]
auto_advance_folders = true
force_consume_off    = true
crossfade_seconds     = 10
crossfade_seconds_alt = 5
skip_fade_seconds     = 1.2
default_route_id      = "local"
```

- **`auto_advance_folders`** — when the queue runs dry, load the next library
  folder automatically rather than sitting silent with no screen to explain
  why. Guards itself against false triggers: any deliberate queue reload
  elsewhere (a folder jump, a browser press, delete's own advance) passes
  through a real, momentary "queue is empty" state that looks identical to a
  genuine end-of-queue from one status snapshot alone — the watcher
  debounces and re-checks before ever acting, specifically to avoid
  clobbering a queue someone else just loaded.
- **`force_consume_off`** — MPD's `consume` and `single` modes are each one
  Stream Deck press away and both silently break headless playback in ways
  that look like controller bugs (queue draining early, "previous" appearing
  to just restart the track). Forced off at startup and again whenever they
  come back on.
- **`crossfade_seconds`** — MPD's own built-in crossfade, applied via the
  `crossfade` command at daemon startup (MPD's crossfade state resets to 0 on
  every `mpd.service` restart, so this is re-applied every time the daemon
  starts, not just once at install). **Only covers automatic transitions** —
  a song ending and the queue moving on by itself. MPD has no ability to
  crossfade a manual skip at all; this is a long-standing, still-open MPD
  limitation, not something fixable from the client side.
- **`crossfade_seconds_alt`** — the TourBox Knob click toggles crossfade
  between this value and `crossfade_seconds` above (its old job, cycling
  output routes, moved to Shift+Knob click).
- **`skip_fade_seconds`** — since MPD can't crossfade a manual skip, this
  instead ducks the volume down, changes track, and ramps back up — not a
  true overlap, but it takes the jolt out of a hard instant cut. Split evenly
  between the ramp-down and ramp-up halves. `0` restores the old instant-cut
  behaviour. Runs on a background thread so a rapid double-skip lands as two
  real skips in order, not one lost press.
- **`default_route_id`** — MPD persists its enabled/disabled output state
  across restarts in its `state_file`. If that file is stale or blank (found
  live: a stray `mpd.service` restart left every output disabled, and
  playback silently accepted `play` commands and did nothing), the daemon
  force-enables this route at startup rather than leaving the box silent
  with no explanation anywhere except `mpc status`.

---

## Video mode (`[video]`)

```toml
[video]
enabled = true
library_dir = "/home/pi/Video"
mpv_socket = "/run/rpi-player-video/mpv.sock"
timeout = 5.0
auto_advance = true
extensions = [".skp", ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm"]
local_audio_device = "hw:CARD=S3,DEV=0"
```

`library_dir` is scanned recursively (subfolders included) for any file
matching `extensions` (see [`VIDEO-MODE.md`](VIDEO-MODE.md) for the `.skp`
format) — not MPD-backed, since none of these are something MPD can index.
The library is not `.skp`-only: a mix of `.skp` karaoke files and plain
video is expected side by side. `.skp` is the one extension needing special
handling at load time — `player/video.py`'s `load_and_play` picks
`VideoCommander.load_skp` (subfile demux, separate audio-track attach) for
`.skp` entries and plain `VideoCommander.load_plain` (an ordinary `loadfile`
— the container already carries its own audio track) for everything else.
`local_audio_device` must match the
same ALSA `hw:CARD=...` string used in `mpd.conf`'s `Local` output — video
mode's audio handoff to the DAC bypasses PipeWire the same way MPD's own
bit-perfect path does, and needs the exact device string to hand off to
correctly.

**`auto_advance`** — Video mode's equivalent of `[playback].auto_advance_folders`:
load the next `.skp` automatically when one plays to the end, wrapping back
to the first entry after the last, so video mode plays straight through the
whole library the same way Music mode does. Implemented by
`player/video_continuous.py` (`VideoContinuous`), which runs on its own
background thread in the TourBox daemon — same "must work headless, with no
Stream Deck attached" reasoning as `ContinuousPlayback`. Unlike MPD, mpv has
no blocking `idle` command to wait on, so this watcher polls mpv's
`eof-reached` property once a second rather than waiting on a push event; it
only acts while Video mode is the active mode, so it never swaps out a video
someone paused to go do something in Music mode.

Video mode's output route selection is independent of but mirrors music
mode's `[[routes]]` — same three destinations (Local/BT/AirPlay), same
Stream Deck key *positions*, tracked separately because video is a different
player process (`mpv`, not MPD) with its own softvol mixer. Switching between
Music and Video mode keeps whatever output route was already selected —
deliberately, so you never lose your Bluetooth/AirPlay connection by
switching modes.

---

## `keymap.toml` structure

```toml
modifier = "side"                          # held button that shifts every other control
layer_toggle = "top"                       # cycles between layers
layer_order = ["track", "folder", "video"] # video is entered/left via Stream Deck only

[buttons.tall]
byte = 0x00                                 # VERIFIED on hardware -- yours may differ

[layers.track.press]
tall = "toggle_pause"
...

[layers.track.shift]                        # held modifier + control
tall = "stop"
...

[layers.track.double]                       # double-click within double_click_seconds
tour = "delete_current"
```

Every action name here must exist in `player/actions.py`'s registry
(`known_actions()`); an unknown name is logged once and does nothing rather
than crashing the daemon — a typo degrades one button, not the whole
controller.

**The shipped byte codes are almost certainly wrong for your unit** — see
[`TOURBOX-NOTES.md`](TOURBOX-NOTES.md) for why (multiple firmware revisions,
nine of twelve guessed values matched a NEO tested here, three didn't, and
the three that didn't collided with other controls' codes and produced
silently wrong actions). Always run `tourbox-capture --learn` before trusting
the controller — see [`DEPLOY.md`](DEPLOY.md) step 8.
