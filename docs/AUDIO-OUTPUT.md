# Audio output on the Raspberry Pi 5

Measured on the target hardware. Two findings here cost real debugging time, so
both are recorded with the evidence.

---

## 1. The Pi 5 has no 3.5mm analog jack

```
$ cat /proc/asound/cards
 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1

$ tr -d '\0' < /proc/device-tree/model
Raspberry Pi 5 Model B Rev 1.1
```

There is no `bcm2835 Headphones` device, because there is no analog output
circuit on the board. Raspberry Pi dropped the 3.5mm TRRS jack when they moved
to the Pi 5 — the Pi 4, 3 and earlier all had one.

**`dtparam=audio=on` in `config.txt` does nothing on a Pi 5.** It is a leftover
from the default image and is often mistaken for a broken setting. Removing it
changes nothing; leaving it changes nothing.

So on a Pi 5 the only built-in audio path is HDMI. Analog requires added
hardware.

### Getting analog out

| Option | How it appears | Notes |
|---|---|---|
| **USB audio adapter / DAC** | New ALSA card, e.g. `hw:CARD=Device` | Cheapest and instant. Anything from a £8 dongle to a serious USB DAC. No config.txt changes, no soldering. Uses a USB port and its power budget. |
| **I2S DAC HAT** | New ALSA card via a `dtoverlay` | Best quality and the natural fit for a dedicated player. HiFiBerry DAC2 Pro, IQaudIO DAC+, Allo Boss. Sits on the GPIO header, leaves USB free, usually gives RCA *and* 3.5mm. Needs one line in `config.txt`. |
| **HDMI audio extractor** | Still `vc4hdmi` | Works, but adds a box and a power supply to a portable build. |
| **Bluetooth / AirPlay** | PipeWire sink | Already in the architecture; not a wired analog answer. |

For a portable audiophile player, a DAC HAT is the intended path — the `Local`
output in `mpd.conf` was written for exactly this, using a direct `hw:` device
so playback is bit-perfect.

Once the DAC is fitted:

```bash
aplay -l                                     # find its CARD= name
sudoedit /etc/mpd.conf                       # uncomment the Local block, set device
sudoedit /etc/wireplumber/wireplumber.conf.d/51-mpd-dac-ignore.conf
sudo systemctl restart mpd
```

Use the `hw:CARD=<name>` form, never `hw:1,0` — card numbers shuffle when USB
devices enumerate in a different order, names do not.

---

## 2. HDMI needs `plughw:`, not `hw:`

**Symptom:** the library indexes perfectly, every format is supported, and
nothing plays. MPD silently returns to `pause`. The only clue is in `mpc status`:

```
ERROR: Failed to open "HDMI" (alsa); Error opening ALSA device
"hw:CARD=vc4hdmi0,DEV=0"; Failed to configure format 24: Invalid argument
```

**Cause:** the vc4hdmi device accepts exactly one format.

```
$ aplay -D hw:CARD=vc4hdmi0,DEV=0 --dump-hw-params /dev/zero
Available formats:
- IEC958_SUBFRAME_LE
```

That is the raw S/PDIF-over-HDMI subframe encoding, not plain PCM. MPD hands a
bare `hw:` device S16_LE and the driver rejects it.

**Fix:** use `plughw:`, which inserts ALSA's plug layer to convert PCM into
IEC958 subframes.

```
audio_output {
    type    "alsa"
    name    "HDMI"
    device  "plughw:CARD=vc4hdmi0,DEV=0"    # plughw:, NOT hw:
    ...
}
```

Verified before and after:

```
hw:      aplay -D hw:CARD=vc4hdmi0,DEV=0 tone.wav  -> "Available formats: IEC958_SUBFRAME_LE"
plughw:  aplay -D plughw:CARD=vc4hdmi0,DEV=0 tone.wav -> plays
```

Losing bit-perfect on HDMI costs nothing — it is the convenience output. **Do
not copy `plughw:` onto a USB DAC or HAT**, where `hw:` is correct and is what
makes the wired path bit-perfect.

### Which HDMI port

`vc4hdmi0` is the port nearest the USB-C power connector. Check which one has a
display attached:

```bash
for d in /sys/class/drm/card*-HDMI-A-*/status; do echo "$d = $(cat $d)"; done
```

---

## 3. Format support — confirmed by playback, not just indexing

MPD 0.24.4 on Trixie, decoders compiled in:

```
[flac]     flac
[sndfile]  wav aiff aif au snd paf iff svx sf voc w64 pvf xi htk caf sd2
[dsf]      dsf          [dsdiff] dff
[ffmpeg]   alac m4a ape tak wv wma opus ... (very long list)
```

Actually played end to end on the device, not merely indexed:

| Format | Result | Reported audio |
|---|---|---|
| FLAC | plays | `44100:16:2`, 878 kbps |
| WAV | plays | `48000:16:2` |
| MP3 | plays | `44100:16:2`, 128 kbps |
| M4A | plays | `44100:f:2` (float) |

### Tag coverage varies by format, which affects the display

| ext | n | title | artist | album |
|---|---|---|---|---|
| mp3 | 36 | 35 | 34 | 28 |
| wav | 19 | 19 | **0** | 19 |
| flac | 10 | 10 | 10 | 10 |
| m4a | 3 | 3 | 3 | 0 |

**WAV files carry no artist tag.** MPD synthesises a title from the filename,
so the Stream Deck shows the track name but leaves the artist line blank. That
is correct behaviour, not a rendering fault — WAV has no standard tag chunk.
FLAC is fully tagged and displays completely.

If you want artists on WAV tracks, either convert to FLAC (lossless, same
audio, real tags) or accept filename-derived titles.

---

## 4. macOS junk from SMB copies

A Finder copy leaves `._*` AppleDouble stubs and `.DS_Store` behind. MPD
correctly ignores them, but they inflate the file count and confuse
`find`-based tooling — 26 were removed from this library during bring-up.

`system/samba/smb.conf` vetoes them on new copies:

```
veto files = /.DS_Store/._*/.Trashes/.Spotlight-V100/.fseventsd/Thumbs.db/desktop.ini/
delete veto files = yes
```

To clean an existing library:

```bash
find ~/Music \( -name '._*' -o -name '.DS_Store' \) -delete
mpc update
```

---

## 5. The Sound Blaster's hardware gain silently defaults to -20dB

**Symptom:** playback works, format and route are all correct, but volume is
noticeably lower than a normal PC output — enough that the car head unit has
to be turned up far higher than usual, which brings the noise floor and any
ground-loop hum up with it.

**Cause:** the Play! 3 has its own onboard "Speaker" attenuator, separate from
whatever MPD or PipeWire do:

```
$ amixer -c S3 sget Speaker
Front Left: Playback 48 [55%] [-20.00dB] [on]
```

`Local`'s mpd.conf block uses `mixer_type "software"` deliberately — MPD's
volume knob needs to behave the same way on every route (Local, Bluetooth,
AirPlay), and hardware mixer semantics differ per device. The consequence is
that MPD **never touches this control at all**. It sits wherever the DAC's
own power-on default leaves it — measured at -20dB, not 0dB — on every boot
and every USB unplug/replug.

-20dB is roughly a 10x cut in amplitude. That alone accounts for most of
"quieter than my PC" and directly explains needing more downstream gain (and
therefore more noise) to compensate.

**Fix:** pin it to 0dB and keep it there. `system/udev/99-soundblaster-gain.rules`
runs `opt/rpi-player/bin/soundblaster-gain` (`amixer -c S3 sset Speaker 100%`)
every time the card's control device appears, so it's re-pinned on every boot
and every replug without relying on ALSA state persistence (`alsactl store`
is keyed by card index, which isn't stable across enumeration order — the
udev rule matches the DAC's USB vendor:product id instead, `041e:324d`).

MPD's own software volume still does 100% of the normal volume-knob
attenuation on top of this fixed 0dB ceiling; this only removes the
pointless permanent pad in front of it.

### There is no Creative driver to install on Linux, and nothing is being missed

Checked directly: the Play! 3 is a plain USB Audio Class device — the kernel's
built-in `snd-usb-audio` driver is the only driver involved on Linux, same as
Windows once Creative's own driver replaces the generic one. Windows/macOS
users additionally get **Creative's SBX Pro Studio app** (dynamic loudness
EQ, bass boost, "Crystalizer", Smart Volume). That processing lives entirely
in Creative's desktop software — it is not firmware or DSP running inside the
Play! 3 hardware — so there was never a Linux driver capability being left on
the table. Nothing is missing; there was just nothing to install.

The equivalent on Linux, if wanted, is PipeWire's `filter-chain` module (or
the EasyEffects GUI built on it) — parametric EQ, a compressor, and loudness
plugins are all available. The catch: that only applies to audio routed
*through* PipeWire, and `Local` deliberately bypasses PipeWire for bit-perfect
`hw:` playback. Adding a loudness curve there would mean giving up the direct
path — worth doing only if the 0dB gain fix above still isn't loud/full
enough in the car. Not done here since it wasn't asked for and changes the
bit-perfect guarantee.

### The ground-loop hum is an electrical problem, not a software one

Audible hum that gets worse with the head unit volume up is the classic
signature of a **ground loop**: the Pi's power ground and the car's chassis
ground are both tied to the audio cable shield, and a small potential
difference between them rides along as 50/60-cycle-adjacent hum. No amount of
ALSA, MPD, or PipeWire configuration fixes this — it's a wiring/grounding
issue between two separately-grounded systems joined by a cable.

Two known-working, purely electrical fixes, cheapest first:

1. **In-line ground loop isolator** on the 3.5mm cable between the DAC and
   the AUX jack (a small transformer-isolated adapter, a few dollars). Breaks
   the DC path while passing audio.
2. **Power the Pi from a source not electrically tied to the car's chassis**
   — a USB battery bank instead of the car's own USB port — which removes one
   of the two ground paths causing the loop in the first place.

Try (1) first; it's cheaper and doesn't touch the power setup.

---

## 6. Bluetooth: two bugs that both look like "it just doesn't work"

Verified against a Bose QC45 (`C8:7B:23:4A:F7:60`) on the Pi 5's onboard radio.

### 5a. `bluez5.roles = [ a2dp_sink ]` breaks headphone output

This was in our own config, written with the intent "A2DP only, never HFP".
That is **not** what the setting means. The roles describe what the **local
machine** does:

| Role | Meaning |
|---|---|
| `a2dp_sink` | the Pi **receives** audio — a phone streams *to* the Pi |
| `a2dp_source` | the Pi **sends** audio — the Pi streams *to* headphones |

Restricting to `a2dp_sink` removed the ability to drive headphones entirely.

**The symptom is genuinely misleading.** Everything reports success:

```
$ bluetoothctl info C8:7B:23:4A:F7:60
    Paired: yes
    Trusted: yes
    Connected: yes
```

No error in `journalctl`, none in wireplumber's log. But no `bluez_output`
sink is ever created. The tell is in the card's profile list:

```
$ pw-cli enum-params <device-id> EnumProfile
    "off"            "Off"
    "audio-gateway"  "Audio Gateway (A2DP Source & HSP/HFP AG)"
```

Only `audio-gateway` — PipeWire is waiting for the headphones to stream *to*
the Pi. There is no `a2dp-sink` profile to select, so nothing can play.

**Fix:** leave `bluez5.roles` unset and use PipeWire's default, which enables
both directions. After removing it the same device offers:

```
    "off"                "Off"
    "a2dp-sink"          "High Fidelity Playback (A2DP Sink, codec SBC)"
    "a2dp-sink-sbc_xq"   "High Fidelity Playback (A2DP Sink, codec SBC-XQ)"
    "headset-head-unit"  "Headset Head Unit (HSP/HFP, codec CVSD)"
```

To prevent HFP degradation — the actual concern that motivated the bad setting
— use the control designed for it:

```
bluez5.autoswitch-profile = false
```

### 5b. MPD cannot reach PipeWire when it runs as the `mpd` user

```
Failed to open "Network" (pipewire); Failed to connect stream: Host is down
```

"Host is down" is misleading. PipeWire is a **per-user** service with its socket
at `/run/user/<uid>/pipewire-0`, owned by that user. Debian's `mpd.service` runs
as the system account `mpd`, which cannot see it. ALSA output keeps working, so
it looks like Bluetooth is broken rather than a permissions boundary.

**Fix:** run MPD as the desktop user — see
`system/systemd/mpd.service.d-override.conf`. Requires:

```bash
loginctl enable-linger rpi        # /run/user/1000 must exist before login
sudo chown -R rpi:rpi /var/lib/mpd
```

Two traps when doing this:

* `After=` / `Wants=` belong in `[Unit]`, not `[Service]`. systemd logs
  "Unknown key ... ignoring" and the dependency silently does not exist.
* `/run/mpd` was created owned by the *old* user; the stale socket then causes
  `Failed to bind to '/run/mpd/socket': Address already in use`. Re-declaring
  `RuntimeDirectory=mpd` in the override makes systemd recreate it correctly.

### 5c. Codec reality on Debian

```
$ ls /usr/lib/aarch64-linux-gnu/spa-0.2/bluez5/
libspa-codec-bluez5-aptx.so   libspa-codec-bluez5-ldac.so
libspa-codec-bluez5-lc3.so    libspa-codec-bluez5-opus.so
libspa-codec-bluez5-sbc.so    ...
```

**There is no `libspa-codec-bluez5-aac.so`** — Debian builds PipeWire without
the AAC encoder because fdk-aac is non-free. Bose headphones support only SBC
and AAC, so a QC45 on Debian will always land on SBC.

**SBC-XQ is the upgrade available.** It is a higher-bitpool SBC that most
listeners find close to AAC. It is not chosen automatically; select the profile:

```bash
wpctl set-profile <device-id> <a2dp-sink-sbc_xq index>
```

Confirm with `api.bluez5.codec` in `pw-dump` — it should read `sbc_xq`, not
`sbc`. WirePlumber remembers the choice per device in
`~/.local/state/wireplumber/`.

### 5d. `wpctl status` hides the node name — do not match against it

`wpctl status` prints the node **description**, never the node name:

```
node.name        bluez_output.C8_7B_23_4A_F7_60.1
node.description Bose QC45
```

Config patterns like `bluez_output` or `raop_sink` only ever appear in the
*name*, so any code that scrapes `wpctl status` and substring-matches will find
nothing, and every wireless route silently reports itself unavailable.
`player/outputs.py` reads `pw-dump` JSON instead and matches on node name,
description or API.
