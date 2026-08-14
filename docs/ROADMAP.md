# Execution Roadmap

Five phases, each with an explicit **gate** — a thing that must work before you
move on. The gates matter more than the steps. This build has several
subsystems that fail in ways that look identical from the outside, and the only
defence is refusing to stack an unverified layer on another.

Run `/opt/rpi-player/bin/rpi-player-doctor` at the end of every phase.

---

## Phase 1 — Base OS, Samba, udev

### 1.1 Flash and first boot

Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**. In the gear menu set
hostname `rpi-audio`, enable SSH with your public key, set the username (this
guide assumes `pi`), and configure Wi-Fi. Doing it here avoids ever needing a
keyboard on the device.

Strongly prefer **NVMe over SD**. It is faster to boot and dramatically more
resistant to the corruption that abrupt power-off causes — which, on a portable
device, is the single most likely way this build dies.

```bash
ssh pi@rpi-audio.local
sudo apt update && sudo apt full-upgrade -y
sudo raspi-config nonint do_boot_behaviour B2   # console autologin
```

### 1.2 Boot-time optimisation

The goal is *music playing* fast, not *fully booted* fast. Local playback needs
no network, so let audio start while Wi-Fi is still negotiating.

```bash
# NVMe first in the boot order so the bootloader stops probing slower devices
sudo rpi-eeprom-config --edit          # BOOT_ORDER=0xf416

# Do not block boot waiting for the network
sudo systemctl disable systemd-networkd-wait-online.service
sudo systemctl disable NetworkManager-wait-online.service 2>/dev/null || true

# Find what is actually slow
systemd-analyze blame | head -20
systemd-analyze critical-chain
```

Record your baseline `systemd-analyze time` now so you can tell later whether a
change helped.

### 1.3 Deploy

```bash
git clone <your-repo> ~/rpi-music-player
cd ~/rpi-music-player
sudo ./install.sh
sudo smbpasswd -a pi        # Samba needs its own password
sudo reboot                 # picks up the new group memberships
```

### 1.4 Verify sharing

From macOS Finder: <kbd>⌘K</kbd> → `smb://rpi-audio.local` — or just look in the
sidebar, where the Avahi `_device-info._tcp` record should give it a proper icon
rather than a generic placeholder. From Windows: `\\RPI-AUDIO\Music`.

Drag an album in and confirm it lands with the right ownership:

```bash
ls -la ~/Music/
```

> **Gate 1:** SSH works, the share is writable from your laptop, and
> `ls /dev/tourbox` resolves when the TourBox is plugged in.

---

## Phase 2 — MPD, PipeWire, Bluetooth

### 2.1 Point MPD at your DAC

```bash
aplay -l                                    # find the card
sudoedit /etc/mpd.conf.rpi-player           # set device "hw:CARD=<name>,DEV=0"
sudo cp /etc/mpd.conf.rpi-player /etc/mpd.conf
sudo systemctl restart mpd
mpc update && mpc stats
```

Use the `hw:CARD=<name>` form, not `hw:1,0`. Card numbers shuffle when USB
devices enumerate in a different order; names do not.

### 2.2 Stop WirePlumber stealing the DAC

**This is the step people skip and then spend a day debugging.** WirePlumber
claims every ALSA card it finds. Once it holds your DAC, MPD cannot open `hw:`
exclusively — you get "Device or resource busy", or worse, silent resampling
that quietly defeats the whole point of a bit-perfect path.

```bash
wpctl status                    # find the card's device.name
sudoedit /etc/wireplumber/wireplumber.conf.d/51-mpd-dac-ignore.conf
systemctl --user restart wireplumber
```

Verify the exclusion took:

```bash
wpctl status | grep -i dac      # should now show NOTHING
aplay -D hw:CARD=<name>,DEV=0 /usr/share/sounds/alsa/Front_Center.wav
```

### 2.3 Confirm bit-perfect

```bash
mpc play
cat /proc/asound/card*/pcm0p/sub0/hw_params
```

The `rate:` line must match the source file. If a 96 kHz FLAC reports 44100,
something is resampling — check `auto_resample "no"` in mpd.conf and re-verify
the WirePlumber exclusion.

### 2.4 Bluetooth headphones

Fit a **USB Bluetooth dongle**. The Pi's onboard Wi-Fi and Bluetooth share
silicon, and this build wants both simultaneously — that combination is a
well-documented source of A2DP stuttering on every Pi generation.

```bash
# Disable the onboard radio so BlueZ uses the dongle
echo "dtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
sudo reboot

bluetoothctl
  power on
  agent on
  default-agent
  scan on
  pair    <MAC>
  trust   <MAC>        # 'trust' is what makes it reconnect automatically
  connect <MAC>
  quit

wpctl status | grep bluez        # a bluez_output sink should appear
```

Prefer a 5 GHz Wi-Fi network to move Wi-Fi traffic out of the 2.4 GHz band
Bluetooth occupies.

> **Gate 2:** hi-res plays bit-perfect to the wired DAC, and
> `mpc enable 2 && mpc disable 1` moves audio to the Bluetooth headphones
> without a restart.

---

## Phase 3 — TourBox

### 3.1 Confirm the port is yours

```bash
ls -l /dev/tourbox
sudo fuser -v /dev/tourbox      # should show nothing before the daemon starts
```

If something else holds it, that is almost certainly ModemManager probing the
CDC-ACM port with AT commands. `install.sh` purges it and installs a udev rule
setting `ID_MM_DEVICE_IGNORE`; if you see the symptom anyway (device works for a
few presses, then drops with *"device reports readiness to read but returned no
data"*), that is the cause.

### 3.2 Learn the real byte codes

**Do not skip this.** The shipped `keymap.toml` contains community-sourced
candidates. TourBox has shipped multiple firmware revisions and there is no
guarantee they match your unit.

```bash
sudo systemctl stop tourbox-player

# First look at the raw stream — confirms the port works and shows the
# press/release convention (release = press | 0x80)
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture --sniff

# Then the guided walkthrough
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture \
    --learn --out /tmp/keymap.toml

diff /opt/rpi-player/etc/keymap.toml /tmp/keymap.toml
sudo cp /tmp/keymap.toml /opt/rpi-player/etc/keymap.toml
```

The tool reports **byte collisions** — two controls emitting the same code. That
report is the single most valuable output here; a collision means the daemon
will fire the wrong action and no amount of staring at logs will explain why.

### 3.3 Run it

```bash
sudo systemctl start tourbox-player
journalctl -u tourbox-player -f
```

Turn each control and watch the debug log resolve it to an action. Unmapped
bytes are logged once each with a pointer back to the capture tool.

> **Gate 3:** the knob changes volume, the tall button toggles play/pause, and
> both still work after `sudo systemctl restart mpd` and after unplugging and
> replugging the TourBox. All with no screen attached.

---

## Phase 4 — Stream Deck XL

```bash
lsusb | grep 0fd9                # Elgato vendor ID
sudo systemctl start streamdeck-player
journalctl -u streamdeck-player -f
```

A permissions error here means `99-streamdeck.rules` is missing or the service
user is not in `plugdev`. Group changes need a re-login to take effect.

Check the panel reflects reality: play something and watch the title, elapsed
time and progress bar update; turn the TourBox knob and watch the volume key
follow; press a route key and watch it highlight.

Note that unavailable routes render greyed out. If AirPlay is grey, no RAOP sink
has been discovered yet — that is Phase 6, and the greying is working as
intended. Switching to an output with no sink behind it would mean playing to
nothing, which is baffling on a device with no other feedback.

Tune for battery in `config.toml`: `brightness`, `dim_after_seconds`,
`blank_after_seconds`. The panel is the largest discretionary power draw in the
build.

> **Gate 4:** keycaps track playback state within ~100 ms of an MPD change, key
> presses control playback, and unplugging the Stream Deck does not interrupt
> audio or break the TourBox.

---

## Phase 5 — Power management and graceful shutdown

Fit this before you take the device anywhere. A portable player gets powered off
by disconnecting a battery, and an abrupt cut during a filesystem write is how
you come back to an unbootable device.

### 5.1 Shutdown button

Wire a momentary button between **GPIO 3 (physical pin 5)** and **GND (physical
pin 6)**. Pin 3 is chosen deliberately: it doubles as the firmware WAKE pin, so
the same button also powers the board back on from halt. No other GPIO gives you
both.

```bash
sudoedit /opt/rpi-player/etc/config.toml     # [power] enabled = true
sudo systemctl enable --now shutdown-button

# Test without actually powering off
sudo /opt/rpi-player/venv/bin/python /opt/rpi-player/bin/shutdown-button --dry-run
```

`hold_seconds` gives press-and-hold semantics so a knock in a bag does not shut
the device down mid-album.

### 5.2 Read-only root (recommended)

Removes the failure mode entirely rather than mitigating it.

```bash
sudo raspi-config      # Performance Options -> Overlay File System -> enable
```

Remember that with the overlay active, changes do not persist — disable it
temporarily when you edit config or add music. Since music arrives over Samba
into `/home/pi/Music`, mount that as a separate writable partition so the
library stays writable while root stays protected.

### 5.3 Power budget

The Pi 5 needs a 5 A-capable supply. Under battery, consider a UPS HAT with
capacity reporting so the daemon can trigger a clean shutdown on low battery
rather than browning out. Capping clocks in `config.txt` meaningfully extends
runtime; a music player is nowhere near CPU-bound.

> **Gate 5:** holding the button cleanly powers down; pressing it again powers
> back up; the device survives ten power cycles without filesystem errors
> (`sudo dmesg | grep -i ext4`).

---

## Verification

Offline test suite — no hardware, no MPD, no Pi required. Run it on your laptop
before deploying:

```bash
./tests/run-all.sh
```

Covers the protocol decoder (press/release, long-press deferral, modifier
handling, rotary coalescing, direction reversal, unplug state reset), the output
router (`wpctl` parsing, two-layer switching, ordering, unavailable-route
refusal), and an end-to-end run of the real daemon against a virtual serial port
with a fake MPD.

On the device:

```bash
/opt/rpi-player/bin/rpi-player-doctor
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| TourBox works briefly then drops | ModemManager probing the port | `sudo fuser -v /dev/tourbox`; purge ModemManager or install the udev rule |
| MPD: "Device or resource busy" | WirePlumber holds the DAC | Edit `51-mpd-dac-ignore.conf`, restart wireplumber |
| Wrong action on every control | Stuck modifier | Restart the daemon; the decoder resets held state on reconnect |
| Random wrong actions | Unverified keymap, byte collision | `tourbox-capture --learn` and read the collision report |
| Volume slams to 0/100 | `max_volume_jump` too high | Lower it in `[tourbox.steps]` |
| Stream Deck permission denied | Missing udev rule or `plugdev` | Install `99-streamdeck.rules`; re-login after `usermod` |
| BT audio stutters | Onboard radio contending with Wi-Fi | USB BT dongle + `dtoverlay=disable-bt`; prefer 5 GHz Wi-Fi |
| BT sounds like a telephone | Fell back to HSP/HFP | Confirm `bluez5.roles = [ a2dp_sink ]` in the WirePlumber config |
| AirPlay route greyed out | No RAOP sink discovered | Check `module-raop-discover`; confirm same subnet |
| AirPlay drops after ~25 s | Known PipeWire RAOP latency bug | This is the AirPlay 1 limitation — see below |
| Hi-res plays at 44.1 kHz | Something is resampling | Check `hw_params`, `auto_resample "no"`, WirePlumber exclusion |

### On AirPlay

PipeWire's RAOP module is effectively **AirPlay 1**: no metadata forwarding, and
documented latency-desync bugs that cause broken-pipe dropouts on some
receivers. It is fine for AirPort Express, most Sonos, and older third-party
gear.

If your speakers are **AirPlay 2** (HomePod, HomePod mini, Apple TV 4K), add
**OwnTone** as a second engine for that path. It does proper AirPlay 2 with
pairing and multiroom sync — and conveniently also speaks the MPD protocol, so
`player/` drives it by changing one socket address in `config.toml`, with no
code changes.

Do not add OwnTone speculatively. Prove your speakers need it first.

**Shairport-Sync is not the answer here.** It is an AirPlay *receiver* — it makes
the Pi into a speaker your phone can stream *to*. Useful, and worth adding if you
want that, but it cannot send audio out to AirPlay speakers.
