# Deploy: vanilla Raspberry Pi OS Lite → fully working player

One document, start to finish. Each section ends with a **check** — do not move
on until it passes; this build has several subsystems that fail in ways that
look identical from the outside (silence with no error), and the only defence
is verifying one layer before stacking the next on top of it.

For *why* things are built this way, see [`ARCHITECTURE.md`](../ARCHITECTURE.md).
For the deep-dive on any one subsystem, see the other files in `docs/` —
this document links to them at the relevant step instead of repeating them.

---

## 0. What you need

**Hardware:**
- Raspberry Pi 5 (this was built and verified against a Pi 5; earlier Pis
  likely work but are untested — note the Pi 5 has **no 3.5mm jack**, see
  [`AUDIO-OUTPUT.md`](AUDIO-OUTPUT.md))
- A USB DAC or I2S DAC HAT if you want wired audio (recommended — HDMI is a
  fallback, not the design target)
- TourBox Neo (or Elite/Elite Plus) — USB
- Stream Deck XL — USB (optional; the player is fully operable without it)
- A USB Bluetooth dongle if you want Bluetooth headphones — **do not rely on
  the Pi's onboard radio**, it shares silicon with Wi-Fi and the combination
  is a well-documented source of A2DP stuttering
- NVMe boot storage strongly preferred over SD — faster, and dramatically
  more resistant to the corruption an abrupt power-off causes, which on a
  portable device is the single most likely way this build dies

**On your laptop:**
- Raspberry Pi Imager
- This repository, cloned locally
- `ssh`/`scp`/`rsync` — no keyboard/monitor is ever attached to the Pi in
  this guide

---

## 1. Flash and first boot

Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**. In the gear/advanced
menu:

- Set hostname (this guide uses `rpi-audio`)
- Enable SSH, authorize your public key (not a password)
- Set the username — **this guide uses `pi`**; if you pick something else,
  substitute it everywhere below and pass it to `install.sh` implicitly via
  `SUDO_USER` (see step 3)
- Configure Wi-Fi if you're not wiring Ethernet for setup

Boot it, then:

```bash
ssh pi@rpi-audio.local
sudo apt update && sudo apt full-upgrade -y
sudo raspi-config nonint do_boot_behaviour B2     # console autologin, no desktop
```

**Boot-time optimisation** (optional but recommended for "music playing fast,
not fully booted fast" — local playback needs no network):

```bash
sudo rpi-eeprom-config --edit                      # NVMe: set BOOT_ORDER=0xf416
sudo systemctl disable systemd-networkd-wait-online.service
sudo systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
```

**Check:** `ssh pi@rpi-audio.local` works with no password prompt.

---

## 2. Deploy the repository

```bash
git clone <your-repo-url> ~/rpi-music-player
cd ~/rpi-music-player
sudo ./install.sh
```

`install.sh` is idempotent — safe to re-run after a `git pull`. It preserves
your edited `etc/config.toml` and `etc/keymap.toml` (the learned TourBox byte
codes especially — a clobber there means redoing the capture in step 8). It
also **refuses to deploy a broken tree**: every module is checked for being
real Python of a plausible size before anything is copied, and re-verified by
actually importing after the venv is built. If it dies partway through with a
specific warning, that warning is the actual problem — see
[`RECOVERY.md`](RECOVERY.md) if the failure mode looks like a shell script
masquerading as a `.py` file.

What it does, in order: installs system packages (MPD, PipeWire, BlueZ, mpv/
ffmpeg for video mode, Samba, Avahi, Python build deps), copies the tree to
`/opt/rpi-player`, builds a venv, installs udev rules (TourBox, Stream Deck,
the Sound Blaster gain fix if relevant), installs the polkit shutdown rule,
adds your user to the groups it needs, enables lingering for your user
(**required** — PipeWire's per-user socket doesn't exist until this is on;
see step 5), installs the MPD-runs-as-your-user override (**required** for
Bluetooth/AirPlay — see step 5), writes `/etc/mpd.conf.rpi-player` (not yet
active), installs the WirePlumber DAC-exclusion template and the AirPlay
discovery module, configures Samba, and installs+enables the systemd units.

```bash
sudo smbpasswd -a pi        # Samba needs its own password, separate from SSH
sudo reboot                 # picks up new group memberships + lingering
```

**Check:** after reboot, `ssh pi@rpi-audio.local` still works, and:

```bash
loginctl show-user pi | grep Linger        # -> Linger=yes
systemctl --user status pipewire            # -> active, even with no one "logged in"
```

---

## 3. Music library over the network

From macOS Finder: <kbd>⌘K</kbd> → `smb://rpi-audio.local`, or look in the
sidebar (Avahi gives it a proper icon). From Windows: `\\RPI-AUDIO\Music`.

Drag your library in, then:

```bash
ls -la ~/Music/          # confirm it landed owned by pi, not root
```

**Check:** the share is writable from your laptop, and files you drop in show
up owned by `pi:pi` on the Pi.

---

## 4. MPD and your audio output

```bash
aplay -l                                    # find your DAC's CARD= name
sudoedit /etc/mpd.conf.rpi-player           # set device "hw:CARD=<name>,DEV=0"
sudo cp /etc/mpd.conf.rpi-player /etc/mpd.conf
sudo systemctl restart mpd
mpc update && mpc stats
```

Use `hw:CARD=<name>`, never `hw:1,0` — card **numbers** shuffle when USB
devices enumerate in a different order; names do not.

**No DAC yet?** Use `system/mpd.conf.hdmi-only` as your starting
`/etc/mpd.conf` instead — HDMI-only, `Local` marked unavailable until you add
one. Remember `plughw:`, not `hw:`, for HDMI (see
[`AUDIO-OUTPUT.md`](AUDIO-OUTPUT.md) section 2 for why a bare `hw:` device on
HDMI fails with a cryptic ALSA format error).

### Stop WirePlumber stealing the DAC

**The step people skip and then lose a day to.** WirePlumber claims every
ALSA card it finds. Once it holds your DAC, MPD cannot open `hw:` exclusively
— "Device or resource busy", or worse, silent resampling that defeats the
entire point of a bit-perfect path.

```bash
wpctl status                                # find the card's device.name
sudoedit /etc/wireplumber/wireplumber.conf.d/51-mpd-dac-ignore.conf
# edit the device.name glob to match YOUR card (installed with a Sound
# Blaster Play! 3 pattern as an example — it will not match your hardware)
systemctl --user restart wireplumber
```

**Check:**

```bash
wpctl status | grep -i <your DAC name>      # must print NOTHING
mpc play
cat /proc/asound/card*/pcm0p/sub0/hw_params # 'rate:' must match the source file
```

If a 96 kHz FLAC reports 44100 in `hw_params`, something is resampling —
re-check the exclusion and `auto_resample "no"` in `mpd.conf`.

---

## 5. MPD needs to run as your user, not the system `mpd` account

**This is the step that is easy to miss entirely, because local (wired DAC)
playback works completely fine without it — the failure is silent and
specific to Bluetooth and AirPlay.**

Debian's stock `mpd.service` runs as the system account `mpd`. PipeWire is a
*per-user* service with its socket at `/run/user/<uid>/pipewire-0`, owned by
and visible only to that user. The `mpd` system account cannot see it, so
switching to Bluetooth or AirPlay fails with:

```
Failed to open "Network" (pipewire); Failed to connect stream: Host is down
```

"Host is down" is misleading — nothing is down; MPD simply cannot see the
socket. The wired path keeps working, which makes this look like a Bluetooth
problem rather than a permissions boundary.

`install.sh` already installed the fix
(`system/systemd/mpd.service.d-override.conf`, which runs MPD as your user
instead) and did the two things it needs:

```bash
loginctl enable-linger pi           # already done in step 2
sudo chown -R pi:pi /var/lib/mpd    # already done by install.sh
```

Apply it:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mpd
systemctl status mpd | head -5      # confirm it's running as pi, not mpd
```

**Check:** `ps -eo user,cmd | grep '[m]pd'` shows your user, not `mpd`.

---

## 6. Bluetooth headphones

Fit a **USB Bluetooth dongle**. Then, if not already done:

```bash
# Disable the onboard radio so BlueZ uses the dongle instead
echo "dtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

```bash
bluetoothctl
  power on
  agent on
  default-agent
  scan on
  pair    <MAC>
  trust   <MAC>        # trust is what makes it reconnect automatically
  connect <MAC>
  quit

wpctl status | grep bluez        # a bluez_output sink should appear
```

Set `config.toml`'s `[[routes]]` `bt_mac` to match your headphones' MAC — this
is what lets the Stream Deck's BT key attempt an active reconnect instead of
just reporting "Unavailable" when they're merely off or paired to a phone.

Prefer 5 GHz Wi-Fi to keep Wi-Fi traffic out of the 2.4 GHz band Bluetooth
uses. See [`AUDIO-OUTPUT.md`](AUDIO-OUTPUT.md) section 6 for two more subtle
Bluetooth bugs (a `bluez5.roles` misconfiguration that silently kills output
entirely, and Debian shipping PipeWire without AAC) — both are already
avoided in the installed WirePlumber config, but worth reading if BT behaves
strangely on hardware other than what this was built against.

**Check:** `mpc enable 2 && mpc disable 1` (Network on, Local off) moves audio
to the headphones with no restart.

---

## 7. AirPlay speakers

Two independent things both have to be true, or the AirPlay route silently
has nothing to switch to:

**a. PipeWire has to be told to discover AirPlay receivers at all.**
`install.sh` already installed `system/pipewire/10-raop-discover.conf` — the
module doesn't ship enabled by default, and without it PipeWire never creates
`raop_sink` nodes regardless of what speakers are on the network.

```bash
systemctl --user restart pipewire pipewire-pulse wireplumber
wpctl status | grep -i raop         # one line per AirPlay speaker found
avahi-browse -rt _raop._tcp         # confirms what's actually on the LAN
```

**b. `config.toml`'s route entries have to match YOUR speaker, specifically.**
The generic `pw_sink = "raop_sink"` substring matches *every* `_raop._tcp`
announcer on the LAN — which on most networks includes other people's laptops
with AirPlay receiving enabled, not just real speakers. Match on your
speaker's actual mDNS hostname instead:

```toml
[[routes]]
id = "airplay"
label = "AirPlay"
icon = "airplay"
mpd_output = "Network"
pw_sink = "raop_sink.YourSpeakerName"   # from wpctl status / avahi-browse above
```

**AirPlay 2 note:** PipeWire's RAOP module is effectively AirPlay 1 — no
metadata, and a documented latency-desync bug that causes dropouts on some
receivers after ~25s. Fine for AirPort Express, most Sonos, older gear. If
your speakers are HomePod/HomePod mini/Apple TV 4K (AirPlay 2 only), you need
OwnTone as a second engine — see `docs/ROADMAP.md`'s AirPlay section. Do not
add it speculatively; prove you need it first.

**Check:** switching the panel/TourBox to the AirPlay route actually starts
audio on the physical speaker, at a sane volume (see the note on
`AIRPLAY_SAFE_VOLUME` in `docs/CONFIGURATION.md` — a freshly-woken AirPlay
speaker plays instantly at whatever software volume was last set, with no
ramp).

---

## 8. TourBox controller

```bash
ls -l /dev/tourbox                  # the udev rule's stable symlink
sudo fuser -v /dev/tourbox          # should show nothing before the daemon starts
```

If something else holds the port, it's almost certainly ModemManager probing
the CDC-ACM port with AT commands (`install.sh` purges it and installs a udev
rule as a backstop). Symptom: the device works for a few presses, then drops
with *"device reports readiness to read but returned no data."*

**Do not skip learning the real byte codes.** The shipped `keymap.toml`
contains community-sourced values from a different unit's firmware revision.

```bash
sudo systemctl stop tourbox-player

# Raw byte stream first — confirms the port works
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture --sniff

# Guided walkthrough, with byte-collision detection
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture \
    --learn --out /tmp/keymap.toml

diff /opt/rpi-player/etc/keymap.toml /tmp/keymap.toml
sudo cp /tmp/keymap.toml /opt/rpi-player/etc/keymap.toml
sudo systemctl start tourbox-player
```

The tool's **byte collision** report is the single most valuable thing it
prints — a collision means the daemon will fire the wrong action for a
control, and nothing in the logs will explain why. See
[`TOURBOX-NOTES.md`](TOURBOX-NOTES.md) for the full protocol writeup,
including the response-frame hazard the daemon guards against and why the
NEO's rotary encoders may report nothing at all on some units.

**Check:** the knob changes volume, Tall toggles play/pause, and both still
work after `sudo systemctl restart mpd` and after unplugging/replugging the
TourBox — all with no screen attached.

---

## 9. Stream Deck XL

```bash
lsusb | grep 0fd9                   # Elgato vendor ID
sudo systemctl start streamdeck-player
journalctl -u streamdeck-player -f
```

A permissions error here means `99-streamdeck.rules` is missing or your user
isn't in `plugdev` (needs a re-login/reboot to take effect after
`install.sh`'s `usermod`).

Check the panel reflects reality: play something and watch title/elapsed/
progress update; turn the TourBox knob and watch the volume key follow; press
a route key and watch it highlight. Unavailable routes render greyed out —
if AirPlay is grey, no sink has been discovered yet (step 7).

Tune `[streamdeck]` in `config.toml` for battery: `brightness`,
`dim_after_seconds`, `blank_after_seconds` — the panel is the largest
discretionary power draw in the build.

**Check:** keycaps track playback state within ~100ms of an MPD change, key
presses control playback, and unplugging the Stream Deck neither interrupts
audio nor breaks the TourBox (they're deliberately separate processes — see
`ARCHITECTURE.md`).

---

## 10. Video / karaoke mode (`.skp` files)

Optional. `install.sh` already installed and enabled `mpv`, `ffmpeg`, and
`video-mpv.service` (a persistent, idle `mpv` instance holding the DRM/HDMI
output, the same "always resident" pattern MPD itself uses).

```bash
sudo systemctl start video-mpv
systemctl status video-mpv
```

Drop `.skp` files into `/home/pi/Video` (or point `config.toml`'s `[video]
library_dir` elsewhere — a mounted external drive is strongly recommended for
a large library; see [`VIDEO-MODE.md`](VIDEO-MODE.md) section 2 on why the SD/
NVMe root filesystem alone won't hold one).

Switch into video mode from the Stream Deck's Video key. The video page
mirrors the music page's layout — Local/BT/AirPlay and Shutdown stay in the
same physical positions in both modes; only Music/Video state changes.

**No monitor attached is fine.** `video-mpv.service`'s `--vo=drm,null`
fallback means video mode still plays audio-only with no HDMI display
connected — see [`VIDEO-MODE.md`](VIDEO-MODE.md) for what this device is
actually for (karaoke video with switchable vocal/instrumental audio tracks,
byte-range-demuxed on the fly with no conversion pass) and its architecture.

**Check:** playing a `.skp` file shows video (if a monitor is attached) or
audio-only (if not), Side taps switch between vocal and karaoke audio tracks,
and switching back to Music mode leaves the output route exactly where it
was.

---

## 11. Delete key, crossfade, and other playback behaviour

All in `[delete]` and `[playback]` in `config.toml` — see
[`CONFIGURATION.md`](CONFIGURATION.md) for the full reference. The defaults
worth knowing before you start using the Delete key on real files:

```toml
[delete]
mode = "trash"      # moves the file to trash_dir, recoverable — NOT "permanent"
trash_dir = "/home/pi/Music-trash"
log_path  = "/home/pi/deleted-tracks.log"
```

`install.sh` already created `Music-trash`. Every deletion is logged
regardless of mode, so `deleted-tracks.log` is always your audit trail —
worth checking after your first few presses of the key to confirm it's doing
what you expect before trusting it on a library you've spent time curating.

**Check:** delete a throwaway test file via the Stream Deck's delete key,
confirm it moved to `Music-trash` (not gone entirely, if you're on `trash`
mode) and appears in `deleted-tracks.log`.

---

## 12. Power management and graceful shutdown

Fit this before the device goes anywhere it can lose power ungracefully. A
portable player gets powered off by disconnecting a battery, and an abrupt
cut during a filesystem write is the most likely way this build becomes
unbootable.

### Shutdown button

Wire a momentary button between **GPIO 3 (physical pin 5)** and **GND
(physical pin 6)** — pin 3 is deliberate, it doubles as the firmware WAKE pin,
so the same button also powers the board back on from halt.

```bash
sudoedit /opt/rpi-player/etc/config.toml     # [power] enabled = true
sudo systemctl enable --now shutdown-button

# Test without actually powering off
sudo /opt/rpi-player/venv/bin/python /opt/rpi-player/bin/shutdown-button --dry-run
```

The Stream Deck's own shutdown key uses a two-step press-to-confirm instead
of a hardware button, and needs the polkit rule `install.sh` already
installed (`system/polkit/49-rpi-player-shutdown.rules`) — without it,
`systemctl poweroff` run by an unprivileged service account fails silently
with "Interactive authentication required," and the confirm sequence just
goes back to idle with no feedback.

### Read-only root (recommended)

Removes the power-loss failure mode entirely rather than mitigating it:

```bash
sudo raspi-config      # Performance Options -> Overlay File System -> enable
```

With the overlay active, changes don't persist — disable it temporarily to
edit config or add music another way, or keep using Samba (which writes to
`/home/pi/Music`, ideally a separate writable partition so the library stays
writable while root stays protected).

**Check:** holding the button cleanly powers down and pressing it again
powers back up; the device survives ten power cycles with no filesystem
errors (`sudo dmesg | grep -i ext4`).

---

## 13. Final verification

```bash
/opt/rpi-player/bin/rpi-player-doctor
./tests/run-all.sh          # from your laptop, or on the Pi with the venv
```

`rpi-player-doctor` checks every layer end-to-end and says specifically
what's wrong, rather than "something is broken." `run-all.sh` is the offline
suite — protocol decoding, output routing, the library browser's thread
safety, continuous-playback's false-advance guards, skip-fade, crossfade
toggling, video resume — no hardware, MPD, or Pi required, safe to run before
ever touching the device.

A fully healthy boot's `journalctl -u tourbox-player -n 20` looks like:

```
tourbox daemon started
loaded keymap: 14 buttons, 3 rotaries, modifier=side
crossfade: 10s (automatic transitions only)
current output route: Local
TourBox connected on /dev/tourbox @ 115200 baud
```

---

## If something goes wrong

- **A `.py` file turns out to be a shell script, or a service crash-loops
  right after deploy** → [`RECOVERY.md`](RECOVERY.md).
- **Audio silently doesn't play, `mpc status` says "All audio outputs are
  disabled"** → MPD's `state_file` can persist a stale all-disabled state
  across a restart. `config.toml`'s `[playback] default_route_id` now
  force-enables a default route at daemon startup if this happens, but if
  you hit it manually: `mpc enable 1` (or whichever output you want).
- **Anything Bluetooth/AirPlay/audio-format specific** →
  [`AUDIO-OUTPUT.md`](AUDIO-OUTPUT.md).
- **TourBox behaving randomly, or a control doing nothing** →
  [`TOURBOX-NOTES.md`](TOURBOX-NOTES.md) — almost always an unlearned/
  colliding byte code, see step 8.
- **Full config reference** → [`CONFIGURATION.md`](CONFIGURATION.md).
