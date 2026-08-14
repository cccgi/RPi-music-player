# RPi Portable Music Player

A dedicated, headless, portable music player on Raspberry Pi 5, controlled by a
TourBox and displayed on a Stream Deck XL.

- **OS** — Raspberry Pi OS Lite 64-bit (Debian Trixie)
- **Engine** — MPD, with PipeWire as the wireless output router
- **Control** — TourBox Neo over USB CDC-ACM serial
- **Display** — Stream Deck XL via HID
- **Sharing** — Samba + Avahi, `smb://rpi-audio.local`

Start with [`docs/DEPLOY.md`](docs/DEPLOY.md) for a single, start-to-finish
runbook from a vanilla Raspberry Pi OS Lite flash to a fully working device.
[`docs/ROADMAP.md`](docs/ROADMAP.md) covers the same ground phase-by-phase
with more of the reasoning behind each gate, and
[`ARCHITECTURE.md`](ARCHITECTURE.md) explains why this stack was chosen over
DietPi/moOde/Volumio. [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) is the
full `config.toml`/`keymap.toml` reference.

Also built on top of the core music player: **video/karaoke mode** for
`.skp` files with switchable vocal/instrumental audio tracks (see
[`docs/VIDEO-MODE.md`](docs/VIDEO-MODE.md)), MPD crossfade with a software
volume-duck fallback for manual skips, and a Delete key with queue/trash/
permanent modes and an audit log for library curation (see
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)).

---

## Two things that shape the whole design

**1. The TourBox is not a USB HID device.** It enumerates as a CDC-ACM serial
port (`/dev/ttyACM0`) streaming single bytes at 115200 baud. There is no HID
report descriptor. Buttons emit a byte on press and `byte | 0x80` on release;
rotaries emit one byte per detent.

**2. Every existing TourBox Linux driver is the wrong shape for a headless
player.** They all synthesise keystrokes through `uinput`, which requires a
graphical session with a focused window to receive them. There is no focus here,
so the keystroke goes nowhere. This project decodes the protocol and issues MPD
commands directly — which also means the controller works identically over SSH,
on the console, and with nothing plugged into HDMI.

---

## Architecture

```
  TourBox (CDC-ACM serial)                Stream Deck XL (HID)
          │                                        │
          │ raw bytes @115200                      │ hidapi
          ▼                                        ▼
  ┌───────────────────┐                  ┌────────────────────┐
  │ tourbox_daemon.py │                  │ streamdeck_daemon  │
  │  protocol.Decoder │                  │  render (Pillow)   │
  │  press/release,   │                  │  lazy per-key      │
  │  long-press,      │                  │  redraw            │
  │  modifier, rotary │                  │                    │
  │  coalescing       │                  │                    │
  └─────────┬─────────┘                  └────────┬───────────┘
            │                                     │
            │   ┌─────────────────────────────────┤
            │   │  IPC bus (optional, NDJSON)     │
            │   │  toasts, optimistic route hints │
            │   └─────────────────────────────────┘
            │                                     │
            ▼                                     ▼
       ┌─────────────────────────────────────────────────┐
       │           player/actions.py                     │
       │  ONE symbolic action table for both surfaces    │
       └───────────────────────┬─────────────────────────┘
                               │
              ┌────────────────┴──────────────────┐
              ▼                                   ▼
      commands (python-mpd2)              idle subscription
              │                          (push, not polling)
              ▼                                   │
       ┌──────────────────────────────────────────▼──────┐
       │                    MPD                          │
       │   [0] "Local"    -> ALSA hw:  (bit-perfect)     │
       │   [1] "Network"  -> PipeWire                    │
       │   [2] "HDMI"     -> ALSA vc4hdmi                │
       └──────────────────────┬──────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              BlueZ A2DP           RAOP sink
           (BT headphones)      (AirPlay speakers)
```

### Output routing is two layers, not one

The thing that makes this build tractable:

> **MPD has two outputs, not three.** Bluetooth and AirPlay are *both* PipeWire
> sinks behind the single `Network` output. Choosing between them is not an MPD
> operation at all — it is `wpctl set-default`.

So switching route is up to two steps, and `player/outputs.py` owns both:

1. **MPD layer** — `enableoutput <target>`, then `disableoutput` the rest.
   Enable-before-disable, so there is never a window with zero outputs enabled
   (which makes MPD stop).
2. **PipeWire layer** — `wpctl set-default <sink>`, done *before* the MPD enable.
   The other order gives you a beat of audio out of the previous device.

Routes whose sink does not exist are reported unavailable, render greyed out, and
are skipped by the cycle action — switching to an undiscovered AirPlay sink means
playing to nothing.

---

## Layout

```
├── ARCHITECTURE.md              # distro/engine evaluation and rationale
├── README.md
├── install.sh                   # idempotent deploy; preserves your configs
├── docs/
│   ├── DEPLOY.md                 # single start-to-finish deploy runbook
│   ├── CONFIGURATION.md          # config.toml / keymap.toml reference
│   ├── ROADMAP.md                # phase-by-phase build with gates
│   ├── RECOVERY.md               # fixing a bad deploy
│   ├── AUDIO-OUTPUT.md           # DAC/HDMI/Bluetooth findings, measured
│   ├── TOURBOX-NOTES.md          # protocol notes, measured on hardware
│   ├── VIDEO-MODE.md             # karaoke/.skp mode architecture
│   └── CARPLAY.md                # CarPlay/Carlinkit integration notes
├── tests/
│   ├── run-all.sh                # offline suite: no hardware, no MPD, no Pi
│   ├── diagnose-deploy.sh        # run on the Pi when a daemon won't start
│   ├── _bootstrap.py             # shared sys.path / dependency guard
│   └── test_*.py                 # decoder, router, render, e2e, browser
│                                  # thread-safety, continuous playback,
│                                  # skip-fade, crossfade, delete, video resume
├── system/                       # everything that lands outside /opt
│   ├── mpd.conf                  # two-layer output model (Local/Network/HDMI)
│   ├── mpd.conf.hdmi-only        # bring-up config before a DAC is fitted
│   ├── udev/99-tourbox.rules     # ModemManager exclusion + stable symlink
│   ├── udev/99-streamdeck.rules
│   ├── udev/99-soundblaster-gain.rules  # pins DAC hardware gain to 0dB
│   ├── systemd/tourbox-player.service
│   ├── systemd/streamdeck-player.service
│   ├── systemd/video-mpv.service         # persistent idle mpv for video mode
│   ├── systemd/shutdown-button.service
│   ├── systemd/mpd.service.d-override.conf  # runs MPD as your user, not `mpd`
│   ├── polkit/49-rpi-player-shutdown.rules
│   ├── samba/smb.conf            # macOS + Windows friendly
│   ├── samba/avahi-smb.service   # _device-info._tcp for a proper Finder icon
│   └── pipewire/
│       ├── 51-mpd-dac-ignore.conf   # stop WirePlumber stealing the DAC
│       └── 10-raop-discover.conf    # enables AirPlay speaker discovery
└── opt/rpi-player/               # deployed to /opt/rpi-player
    ├── requirements.txt
    ├── bin/
    │   ├── tourbox-capture       # byte sniffer + guided keymap learner
    │   ├── rpi-player-doctor     # checks every layer, says what is wrong
    │   ├── shutdown-button       # GPIO clean shutdown
    │   ├── bt-connect            # interactive Bluetooth pair/trust/connect
    │   ├── soundblaster-gain     # pins DAC hardware gain, run by udev
    │   └── streamdeck-preview / streamdeck-blank
    ├── etc/
    │   ├── config.toml           # single source of truth
    │   └── keymap.toml           # byte -> action map
    └── player/
        ├── config.py             # TOML -> dataclasses
        ├── mpdbus.py             # commander + idle watcher, auto-reconnect
        ├── outputs.py            # two-layer output routing
        ├── actions.py            # shared action table (both daemons)
        ├── continuous.py         # auto-advance to the next folder
        ├── bt.py                 # Bluetooth reconnect + picker discovery
        ├── video.py               # mpv IPC client + .skp library scanner
        ├── skp.py                # .skp byte-offset parsing (no copying)
        ├── mode.py                # cross-process music/video mode sentinel
        ├── ipc.py                # optional NDJSON bus
        ├── tourbox_daemon.py
        ├── tourbox/protocol.py   # pure decoder, no I/O, unit-testable
        ├── streamdeck_daemon.py
        └── streamdeck/
            ├── layout.py          # 8x4 key map, data-driven, music + video pages
            ├── render.py          # Pillow keycaps, vector glyphs
            └── browser.py         # library browser (thread-safe)
```

---

## Quick start

```bash
git clone <repo> ~/rpi-music-player
cd ~/rpi-music-player
sudo ./install.sh
```

Then several things need your attention before starting — the installer
prints them, and [`docs/DEPLOY.md`](docs/DEPLOY.md) walks through each one
with a verification step:

1. **MPD output device** — `aplay -l`, then set `device` in `/etc/mpd.conf.rpi-player`
2. **WirePlumber DAC exclusion** — or MPD and PipeWire fight over the card
3. **MPD running as your user, not the system `mpd` account** — required for
   Bluetooth/AirPlay to work at all; `install.sh` installs the override, but
   it needs `sudo systemctl restart mpd` to take effect
4. **TourBox byte codes** — the shipped keymap is *unverified*: 

```bash
sudo systemctl stop tourbox-player
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture \
    --learn --out /tmp/keymap.toml
sudo cp /tmp/keymap.toml /opt/rpi-player/etc/keymap.toml
```

Then:

```bash
sudo systemctl start tourbox-player streamdeck-player
/opt/rpi-player/bin/rpi-player-doctor
```

---

## Testing and troubleshooting

```bash
./tests/run-all.sh                  # offline suite; no hardware, MPD, or Pi
```

It auto-selects an interpreter that has the dependencies (the deployed venv if
present) and reports what is missing rather than dying on `ModuleNotFoundError`.
These are plain scripts, **not** `unittest` TestCases — `unittest discover` will
import the same module name from two sys.path roots and fail.

On the Pi, when a daemon will not start:

```bash
bash ~/rpi-music-player/tests/diagnose-deploy.sh   # what is actually deployed
/opt/rpi-player/bin/rpi-player-doctor              # runtime health
```

If a `.py` file under `/opt/rpi-player/player/` turns out to be a shell script,
the deployed tree is not this repository — see
[`docs/RECOVERY.md`](docs/RECOVERY.md).

---

## Design notes

**Why two daemons.** Stream Deck rendering is 32 Pillow composites — bursty CPU.
Control latency from knob to audio must never queue behind a redraw, so the
processes are separate and the renderer runs at `Nice=5` / `IOSchedulingClass=idle`
while the TourBox daemon runs at `Nice=-5`. Unplugging the Stream Deck also must
not stop playback.

**Why `idle`, not polling.** MPD's `idle` command blocks until a subsystem
changes and then names it. The renderer wakes only on real events, and each key
is content-hashed so identical redraws are skipped. The difference between this
and a naive 1 Hz full repaint is roughly the difference between idling at 0% CPU
and burning a core — which is battery life on a portable device.

**Why the daemons hold separate MPD connections.** `idle` blocks its socket.
Issuing a command on that same socket means sending `noidle` and carefully
re-entering idle, a well-known source of dropped events. MPD handles concurrent
clients cheaply.

**Why rotary coalescing.** A fast knob spin emits 40+ bytes/sec. Sending 40
`setvol` commands is wasteful and feels laggy because MPD processes them
serially. Bursts are coalesced into one command with a summed magnitude, and the
total is clamped (`max_volume_jump`) so a hard flick does not slam the volume to
100 on headphones.

**Why long-press costs latency.** A control with a `long` action cannot fire its
short action on the press edge — we must wait to see whether you keep holding.
That is why the hot path (play/pause, volume) deliberately leaves `long` unset
and fires immediately.

---

## Deliberate omissions

- **Album art** is a Phase 4 stretch goal. MPD's `readpicture` returns binary
  over the protocol in chunks; the layout reserves keys 0–3 for it and currently
  falls back to the title block.
- ~~**Library browsing** on the Stream Deck is stubbed.~~ Implemented —
  `player/streamdeck/browser.py` is a real folder/file browser with paging,
  breadcrumbs, and thread-safe state (guarded with an `RLock` after a live
  race was found where the displayed and playing song diverged). See
  [`CONFIGURATION.md`](docs/CONFIGURATION.md) and `DEPLOY.md` step 10.
- **AirPlay 2** is not covered by PipeWire's RAOP module. If your speakers need
  it, add OwnTone — it speaks the MPD protocol, so it is a config change rather
  than a code change. See the AirPlay section of `docs/ROADMAP.md`.
