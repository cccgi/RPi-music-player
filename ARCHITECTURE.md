# Portable RPi Music Player — OS & Audio Engine Architecture

**Target hardware:** Raspberry Pi 5 · TourBox Neo · Stream Deck XL
**Date:** 2026-08-06
**Status:** Recommendation — pre-build

---

## 0. Executive summary

**Recommendation: Raspberry Pi OS Lite 64-bit (Trixie) + MPD as the playback engine + PipeWire as the output router, with OwnTone added conditionally as an AirPlay 2 bridge.**

Two findings from research materially change the design and should be read before anything else:

1. **The TourBox Neo is not a USB HID device.** It enumerates as a USB CDC-ACM serial port (`/dev/ttyACM0`) and speaks a proprietary byte protocol. There is no HID report descriptor to parse. This is *good* news — reading it is `pyserial` plus a byte-code table, not HID wrangling — but it invalidates the "intercept raw USB HID reports" plan and introduces a different failure mode (ModemManager stealing the port).

2. **Every existing TourBox Linux driver is the wrong shape for this project.** They all translate device input into synthetic keystrokes via `uinput`, which requires a graphical session with a focused window. On a headless player there is no focus, so a keystroke is delivered nowhere. The correct design reads the serial protocol directly and issues MPD commands. Existing drivers are valuable as a **protocol reference**, not as a runtime dependency.

The distro decision follows from a single constraint: **`mpd.conf` is the file that defines output routing, and output routing is the heart of this build.** Both moOde and Volumio generate `mpd.conf` from their own web UI and database, and will overwrite hand edits. That alone disqualifies them.

---

## 1. Candidate evaluation

### 1.1 Comparison matrix

| Criterion | Pi OS Lite + MPD | DietPi + MPD | moOde Audio | Volumio 3 |
|---|---|---|---|---|
| Base | Debian Trixie, official | Debian, custom tooling | Pi OS Lite Trixie + patches | Debian + custom image |
| Idle RAM (fresh) | ~130 MiB | ~64 MiB | ~250 MiB | ~300 MiB |
| Boot to audio (Pi 5, NVMe) | **6–9 s tuned** | 5–8 s | 20–30 s | 25–40 s |
| Root filesystem | Normal read-write | Normal read-write | Normal read-write | **SquashFS + overlay** |
| Owns `mpd.conf` | **No — you do** | No | **Yes, regenerates** | **Yes, regenerates** |
| Custom systemd units | Unrestricted | Unrestricted | Works, survives most updates | Fragile across OTA |
| udev rules persist | Yes | Yes | Yes | Overlay conflicts on OTA |
| Python venv / pip | Native | Native | Native | Discouraged; Node-centric |
| Bluetooth A2DP source | Add PipeWire/BlueZ | Add PipeWire/BlueZ | Built-in (its own scripts) | Built-in (plugin) |
| AirPlay **transmit** | Add OwnTone / RAOP | Add OwnTone / RAOP | Not a core feature | Paid tier historically |
| Samba | `apt install samba` | One-click | Built-in | Built-in |
| mDNS / Avahi | `apt install avahi` | One-click | Built-in | Built-in |
| Web GUI fighting you | **None** | Minimal | Yes — PHP UI owns state | Yes — Node UI owns state |
| Docs / community for debugging | Best in class | Good | Good, audio-focused | Good, but closed internals |

### 1.2 Option 1 — Raspberry Pi OS Lite + MPD ✅ **Recommended**

**Why it wins.** It is the only candidate where *you* own `mpd.conf`, `/etc/udev/rules.d/`, the systemd unit graph, and the boot sequence. Every other option layers an opinionated configuration manager over exactly the subsystems this project needs to control directly.

- **Boot.** Untuned, expect ~20 s on SD. Tuned on Pi 5 + NVMe, `systemd-analyze` lands around 3 s kernel + 3 s userspace. The dominant remaining cost is bootloader firmware probing, fixed by putting NVMe first in `BOOT_ORDER` so the bootloader stops probing slower devices. Crucially, MPD and local file playback have **no network dependency** — order the unit graph so audio starts before Wi-Fi, Avahi, Samba and OwnTone, and music is playing while the network is still negotiating.
- **Extensibility.** A Python venv, three systemd units, two udev rules. No abstraction layer to reverse-engineer.
- **Cost.** You assemble Bluetooth, AirPlay and Samba yourself. That is roughly a day of work and it is the price of not fighting a web GUI for the life of the project.

### 1.3 Option 2 — DietPi + MPD/PipeWire

Genuinely excellent and a close second. ~64 MiB idle vs ~130 MiB is a real 2× saving — **on a Pi Zero.** On a Pi 5 with 4–8 GB it is noise, and the boot-time advantage largely evaporates once you add PipeWire, BlueZ, Avahi and Samba to both.

What you pay for it: `dietpi-software` is a second opinionated configuration layer that regenerates config it installed, and DietPi's package choices occasionally lag Debian. You are trading Debian's enormous debugging corpus for RAM you do not need. **Reconsider this if you later port to a Pi Zero 2 W.**

### 1.4 Option 3 — moOde Audio

The best of the audiophile distros for this purpose, and the closest call. It is built on Pi OS Lite Trixie, is GPL-3, gives real SSH access, and its custom kernel/driver patches for USB DACs are genuinely valuable.

**It still loses on one decisive point, and the specifics are worse than expected.** moOde's PHP web UI is the source of truth for MPD configuration: it regenerates `mpd.conf` in response to a range of events, **and in doing so disables all but a single MPD output.**

That is not a minor inconvenience — it is a direct contradiction of this build's core mechanism. The whole design rests on three outputs defined simultaneously and switched at runtime by the TourBox. moOde will actively tear that down every time it regenerates.

There is a partial escape hatch: moOde merges `/etc/mpd.custom.conf` into `/etc/mpd.moode.conf` via `mpdconfmerge.py`. So custom stanzas *can* survive. But the output-disabling behaviour is separate from the merge, and you would be building your most critical subsystem on top of a mechanism explicitly designed to overrule it.

Secondary issues: ~250 MiB idle and 20–30 s boot are both dominated by services (Apache/PHP, its own worker daemons) that a headless dedicated player will never use.

### 1.5 Option 4 — Volumio 3 ❌ **Disqualified**

Three independent blockers:

1. **SquashFS + overlay root.** Volumio's own developer documentation states that editing a file residing on the base rootfs mirrors it to the overlay, and on subsequent OTA updates *the overlay copy supersedes the new base file*. Their explicit guidance is to never overwrite files under `/volumio`. That is a hostile environment for a build defined by system-level customization.
2. **Plugin system as the sanctioned extension path.** Extensions are meant to be Node.js plugins from the Volumio store. Your project is Python daemons reading a serial port. You would be swimming upstream permanently.
3. **Commercial gating.** Historically several relevant capabilities sit behind MyVolumio subscription tiers. A dedicated offline device should not have a business model in its audio path.

---

## 2. Audio engine: why MPD, and where it needs help

### 2.1 MPD is the right primary engine

| Requirement | MPD |
|---|---|
| Hi-res local playback | Best in class. Direct `hw:` ALSA, bit-perfect, no resampling, gapless, DSD |
| Control API | Simple line protocol over TCP/Unix socket; `python-mpd2` is mature |
| **Push state updates** | **`idle` command** — blocks until a subsystem changes, then names it |
| Runtime output switching | `enableoutput` / `disableoutput` by index, live |
| Footprint | ~15 MiB, single C daemon |
| Bluetooth output | Indirect, via PipeWire |
| **AirPlay output** | **None. This is the gap.** |

The `idle` command is the single most important feature for this build. Subscribe to `player`, `mixer`, `playlist` and `output`, and the Stream Deck renderer wakes only when something actually changed — no polling loop, no wasted CPU, sub-100 ms keycap updates. Cancel with `noidle`. `python-mpd2` exposes this as `send_idle()` / `fetch_idle()`, which composes cleanly with `select()`.

### 2.2 The AirPlay gap, honestly assessed

**PipeWire's `module-raop-sink`** creates AirPlay sinks automatically from Zeroconf via `module-raop-discover`. It works, but the limitations are real:

- It is fundamentally **RAOP / AirPlay 1**. Codec is PCM; AirPlay 2 negotiation and pairing are not properly implemented.
- **No metadata forwarding** — no track title or cover art to the speaker.
- Documented latency-desync bugs causing broken-pipe dropouts after ~25 s on some receivers.

**OwnTone** (formerly forked-daapd) is the mature answer: real AirPlay 1 **and 2**, synchronized multiroom, proper pairing for HomePods, a JSON API with WebSocket push, and — the detail that makes it fit here — **it also speaks the MPD protocol.**

### 2.3 The decision that makes the dual-engine design cheap

Because OwnTone implements an MPD-protocol server, your control daemon needs **exactly one client abstraction**. Switching the active engine is a change of socket address, not a change of code. Two engines, one control surface.

**Decision rule:**

| Your AirPlay speakers | Do this |
|---|---|
| AirPort Express, most Sonos, older third-party (AirPlay 1) | **MPD + PipeWire RAOP only.** Skip OwnTone entirely. |
| HomePod / HomePod mini, Apple TV 4K, AirPlay 2 gear | **Add OwnTone** as a second engine for the AirPlay path. |

Do not add OwnTone speculatively. Prove your speakers need it in Phase 6.

---

## 3. Recommended architecture

### 3.1 Stack

```
┌──────────────────────────────────────────────────────────────┐
│  CONTROL / PRESENTATION            (Python, systemd, venv)   │
│                                                              │
│   rpmp-tourbox.service   rpmp-streamdeck.service   rpmp-tui  │
│   /dev/ttyACM0 serial    hidapi + PIL render       tty1 TUI  │
│          │                      │  ▲                    ▲    │
│          │  intents             │  │ state              │    │
│          ▼                      ▼  │                    │    │
│   ┌──────────────────────────────────────────────────┐  │    │
│   │  rpmp-core.service                               │──┘    │
│   │  · single MPD-protocol client (idle subscriber)  │       │
│   │  · authoritative state cache                     │       │
│   │  · output-route policy engine                    │       │
│   │  · Unix socket: NDJSON req/resp + pub/sub bus    │       │
│   └──────────────────────────────────────────────────┘       │
└───────────────────────────│──────────────────────────────────┘
                            │ MPD protocol (TCP 6600 / 3689)
┌───────────────────────────▼──────────────────────────────────┐
│  ENGINE                                                      │
│    mpd.service ──────────────┐      owntone.service (opt.)   │
└──────────────────────────────│───────────────────│───────────┘
                               │                   │
┌──────────────────────────────▼───────────────────▼───────────┐
│  OUTPUT                                                      │
│   [0] alsa hw:  → USB DAC / HDMI     (bit-perfect, wired)    │
│   [1] pipewire  → BlueZ A2DP         (Bluetooth headphones)  │
│   [2] pipewire  → RAOP sink          (AirPlay 1)             │
│       owntone   → AirPlay 2          (HomePod etc.)          │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 Why four processes instead of one

- **Latency isolation.** Stream Deck XL rendering is 32 PIL image composites — CPU-bursty. Control latency from knob to audio must never queue behind a redraw. Separate processes, separate scheduling.
- **Independent failure.** Unplugging the Stream Deck must not stop playback or kill TourBox control. Each unit gets `Restart=on-failure` and fails alone.
- **Independent development.** Restart the renderer while music keeps playing.
- **One MPD connection.** Only `rpmp-core` holds the `idle` subscription. Multiple `idle` clients against MPD is a classic source of missed events.

### 3.3 IPC: Unix domain socket, newline-delimited JSON

Chosen over D-Bus (heavy, awkward from systemd system units) and MQTT (needs a broker for what is a single-host problem). NDJSON over `AF_UNIX` has zero dependencies, supports systemd socket activation, and is debuggable with `socat - UNIX-CONNECT:/run/rpmp/bus.sock` — which matters a great deal at 1 a.m. with a controller that won't respond.

### 3.4 Output routing is a first-class concept

The policy engine in `rpmp-core` owns route selection. MPD's `enableoutput`/`disableoutput` make it atomic:

```
route: WIRED     → enable 0, disable 1,2      (bit-perfect, hw: exclusive)
route: BT        → enable 1, disable 0,2      (PipeWire → A2DP)
route: AIRPLAY   → enable 2, disable 0,1      (or hand off to OwnTone)
```

**Gotcha — WirePlumber will claim your DAC.** If PipeWire owns the USB DAC's ALSA device, MPD cannot open `hw:` exclusively and the bit-perfect wired path fails with a busy device. Fix with a WirePlumber rule marking that specific node as ignored, so the DAC belongs to MPD and PipeWire handles only Bluetooth and RAOP.

---

## 4. TourBox Neo integration

### 4.1 Device facts

| Property | Value |
|---|---|
| Transport | USB CDC-ACM serial |
| Device node | `/dev/ttyACM0` (scan `/dev/ttyACM*` — do not hardcode) |
| Internal name | `TBG_H` |
| Bluetooth | **Not supported on Neo.** USB only. |
| Haptics | **None on Neo.** Elite/Elite Plus only. |
| Group needed | `dialout` |

### 4.2 The two failure modes to design against

**ModemManager steals the port.** NetworkManager pulls in ModemManager on most Debian systems, which probes CDC-ACM devices with AT commands. Symptom: the device connects, then drops after a few button presses with `device reports readiness to read but returned no data`. Fix with a udev rule setting `ID_MM_DEVICE_IGNORE="1"` for the TourBox VID/PID. Diagnose with `sudo fuser -v /dev/ttyACM0`.

On a dedicated player the cleaner move is to not install ModemManager at all — but ship the udev rule anyway so a future `apt install` cannot break the device.

**Node enumeration order.** If you ever add a second serial device, `/dev/ttyACM0` may not be the TourBox. Write a udev rule creating a stable `/dev/tourbox` symlink by VID/PID, and have the daemon probe for a valid TourBox response rather than trusting the path.

### 4.3 Protocol acquisition — build a capture tool first

Byte codes vary across TourBox firmware revisions. **Phase 3 starts with a 30-line capture script, not with the driver.** Open the port, dump every byte with a timestamp, press each control in a known order, and generate your own mapping table. Cross-check against the community drivers; do not simply copy their tables.

Reference implementations (protocol sources, not dependencies):

- `AndyCappDev/tuxbox` — most actively maintained, all models, Python, MIT. **Best reference.**
- `raleighlittles/Tourbox_Neo_Linux_Driver` — C++, Neo-specific, archived June 2025. Documents the Neo byte map in `docs/`.
- `bloodywing/tourboxneo` — Python, on PyPI, unmaintained, author describes it as hacked together in a day.

### 4.4 Suggested control mapping

Long-press and modifier combos matter, because a music player needs more verbs than the Neo has buttons. Note the timing trade-off: supporting long-press on a control adds a detection delay to its short-press. Keep the hot path — play/pause, volume — free of combos.

| Control | Short | Long / modified |
|---|---|---|
| Big knob | Volume ± | Output route cycle |
| Scroll wheel | Seek within track | Jump 10 tracks |
| Dial | Next/prev track | Next/prev album |
| D-pad | Browse library | — |
| Tall button | Play / pause | Stop |
| Side button | Modifier | — |
| Top button | Toggle shuffle | Toggle repeat |

---

## 5. Stream Deck XL integration

`python-elgato-streamdeck` (`pip install streamdeck`) is mature, supports the XL explicitly, and is verified working on Raspberry Pi. It talks HID via `hidapi` — genuinely a HID device, unlike the TourBox.

- Requires a udev rule granting non-root access to the Elgato hidraw node, plus `libhidapi-libusb0`.
- **Render lazily.** Only redraw keys whose content changed. Pre-render static keycaps at startup and cache glyphs; full 32-key redraws on every `idle` event will waste power on a battery build.
- **Suggested layout:** rows 1–2 as now-playing (album art tiled across 4 keys, title/artist rendered as text), row 3 as transport and route selection, row 4 as playlist/favourite shortcuts.
- **Optimistic UI.** AirPlay adds ~2 s of buffering latency. Update the keycap the instant the TourBox intent arrives, then reconcile against the authoritative state from MPD `idle`. Without this, the controls feel broken on the AirPlay route.

---

## 6. Supporting services

### 6.1 Samba + mDNS for macOS/Windows discovery

`apt install samba avahi-daemon`. Two details that make the difference between "works" and "appears properly in Finder":

- In the share definition, `vfs objects = catia fruit streams_xattr` for macOS resource-fork and xattr compatibility.
- Publish an Avahi service advertising `_smb._tcp` **and** `_device-info._tcp` with a model string, so the Pi appears in the Finder sidebar with a sensible icon rather than as a bare hostname.

Point the share at the same directory MPD indexes, and have `rpmp-core` trigger an MPD `update` on a debounce after writes settle.

### 6.2 Bluetooth — plan for a USB dongle

The Pi's onboard Wi-Fi and Bluetooth share a combined chipset, and simultaneous use is a well-documented source of A2DP stuttering across every Pi generation. Since this build wants Wi-Fi (AirPlay, Samba) *and* Bluetooth (headphones) at the same time, **budget for a USB Bluetooth 5.x dongle from the start.** Disable the onboard radio with `dtoverlay=disable-bt`. Prefer 5 GHz Wi-Fi to move the Wi-Fi traffic out of the 2.4 GHz band Bluetooth occupies.

Use PipeWire rather than BlueALSA — it is the default stack on current Pi OS, handles A2DP natively, and gives lower latency. Configure `bluetooth.autoswitch-to-headset-profile = false`, or the stack may drop your headphones to 8 kHz HSP the moment anything looks like a microphone.

### 6.3 Optional HDMI display

Do **not** install X11 or Wayland for this. On Pi OS Lite the console is already on HDMI. Run a Rich/Textual TUI on `tty1` via a `getty@tty1` override, subscribed to the same `rpmp-core` bus as the Stream Deck. Zero graphical stack, hotplug-safe, and it costs nothing when no monitor is attached. Set `hdmi_force_hotplug` if you want reliable output when plugging in a monitor after boot.

---

## 7. Risk register

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | ModemManager claims `/dev/ttyACM0` | Controller dies after seconds | udev `ID_MM_DEVICE_IGNORE`; don't install ModemManager |
| 2 | WirePlumber holds the DAC | Bit-perfect wired path fails, device busy | WirePlumber rule to ignore the DAC node |
| 3 | BT/Wi-Fi coexistence | A2DP stuttering | USB BT dongle; `dtoverlay=disable-bt`; 5 GHz Wi-Fi |
| 4 | PipeWire RAOP unreliable with AirPlay 2 | Dropouts after ~25 s, no metadata | Prove speakers in Phase 6; add OwnTone if AirPlay 2 |
| 5 | TourBox firmware byte-code drift | Wrong mappings | Capture-first workflow; ship a `--learn` mode |
| 6 | **Abrupt power-off corrupts filesystem** | **Portable device, unbootable player** | NVMe over SD; read-only root + overlayfs; GPIO or long-press clean shutdown |
| 7 | Pi 5 power draw on battery | Short runtime, brownout under load | 5 A-capable supply; UPS HAT; consider undervolting/capping clocks |
| 8 | AirPlay buffering latency | Controls feel unresponsive | Optimistic UI, reconcile on `idle` |
| 9 | Stream Deck redraw contention | Laggy knob response | Separate process; lazy per-key redraw |
| 10 | Debian upgrade changes PipeWire defaults | Silent routing regression | Pin config in `/etc/`, not user dirs; smoke-test script per route |

Risk 6 deserves emphasis. This is a portable device that will be powered off by disconnecting a battery, and that is the single most likely way this build dies in the field. Design the clean-shutdown path in Phase 1, not Phase 8.

---

## 8. Implementation phases

**Phase 0 — Bench.** Pi 5 + NVMe, Pi OS Lite 64-bit Trixie, SSH keys, git repo, Python venv at `/opt/rpmp`.

**Phase 1 — Base system.** Boot optimization (`BOOT_ORDER` NVMe-first, disable `systemd-networkd-wait-online`, audit `systemd-analyze blame`). Samba + Avahi. **Clean-shutdown path.** Baseline `systemd-analyze` number recorded.

**Phase 2 — Audio engine.** MPD from Debian, hand-written `mpd.conf` with all three output stanzas. Verify bit-perfect wired playback. Verify `enableoutput`/`disableoutput` switching by hand over `nc localhost 6600`. *Gate: local hi-res plays, outputs switch live.*

**Phase 3 — TourBox.** Capture tool → byte map → udev rules → `rpmp-core` + `rpmp-tourbox`. *Gate: knob changes volume with no screen attached.*

**Phase 4 — Stream Deck.** `rpmp-streamdeck` subscribed to the core bus. Lazy rendering, optimistic UI. *Gate: keycaps track playback state within 100 ms.*

**Phase 5 — Bluetooth.** PipeWire + BlueZ, USB dongle, pairing persistence across reboot, route switching from the TourBox. *Gate: reconnects to known headphones automatically at boot.*

**Phase 6 — AirPlay.** Test PipeWire RAOP against your actual speakers first. Add OwnTone only if RAOP fails. *Gate: 30+ minutes of uninterrupted AirPlay playback.*

**Phase 7 — HDMI TUI.** Textual UI on tty1, hotplug tested.

**Phase 8 — Hardening.** Read-only root + overlayfs, systemd watchdog, per-route smoke-test script, documented recovery procedure.

---

## Sources

- [AndyCappDev/tuxbox — Linux driver for all TourBox models](https://github.com/AndyCappDev/tuxbox)
- [raleighlittles/Tourbox_Neo_Linux_Driver](https://github.com/raleighlittles/Tourbox_Neo_Linux_Driver)
- [bloodywing/tourboxneo](https://github.com/bloodywing/tourboxneo)
- [python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck) · [docs](https://python-elgato-streamdeck.readthedocs.io/)
- [MPD protocol documentation (`idle`)](https://mpd.readthedocs.io/en/latest/protocol.html) · [MPD plugin reference](https://mpd.readthedocs.io/en/stable/plugins.html)
- [python-mpd2 — idle and command batching](https://python-mpd2.readthedocs.io/en/latest/topics/commands.html)
- [PipeWire — AirPlay (RAOP) sink module](https://docs.pipewire.org/page_module_raop_sink.html) · [RAOP Discover](https://docs.pipewire.org/page_module_raop_discover.html)
- [OwnTone — features and JSON API](https://owntone.github.io/owntone-server/) · [OwnTone AirPlay outputs](https://owntone.github.io/owntone-server/audio-outputs/airplay/)
- [Volumio — File System Architecture (SquashFS/overlay caveats)](https://developers.volumio.com/Architecture/filesystem-architecture)
- [Volumio — Plugin system overview](https://developers.volumio.com/plugins/plugins-overview)
- [moOde image builder (pi-gen, Pi OS Lite base)](https://github.com/moode-player/imgbuild) · [moOde releases](https://github.com/moode-player/moode/releases)
- [moOde regenerates `mpd.conf` and disables all but one output](https://github.com/antiprism/mpd_oled/blob/master/doc/install_moode6_source.md) · [moOde forum: "mpd.conf overwritten"](https://moodeaudio.org/forum/showthread.php?tid=873)
- [DietPi — comparison to other Debian-based distributions](https://dietpi.com/blog/?p=888)
- [Using a Raspberry Pi as a Bluetooth speaker with PipeWire — Collabora](https://www.collabora.com/news-and-blog/blog/2022/09/02/using-a-raspberry-pi-as-a-bluetooth-speaker-with-pipewire-wireplumber/)
- [nicokaiser/rpi-audio-receiver — BT/Wi-Fi coexistence notes](https://github.com/nicokaiser/rpi-audio-receiver)
- [Raspberry Pi forums — A2DP stuttering and onboard radio limitations](https://forums.raspberrypi.com/viewtopic.php?t=217886)
- [Raspberry Pi boot time optimization with systemd](https://ohyaan.github.io/tips/raspberry_pi_boot_time_optimization__complete_performance_guide/)
- [Kevin Boone — bit-perfect audio playback with ALSA](https://kevinboone.me/alsa_bitperfect.html)
