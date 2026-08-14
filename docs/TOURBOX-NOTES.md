# TourBox NEO — protocol notes from hardware

Everything here was measured on a real device, not taken from documentation.
Where a claim is unverified it says so.

**Unit under test:** TourBox NEO, USB ID `cafe:4001`, on Raspberry Pi 5 /
Debian Trixie, connected through a powered USB 2.0 hub.

---

## USB identity

```
Bus 001 Device 003: ID cafe:4001 TourBox Tech Inc. TourBox NEO
  manufacturer  TourBox Tech Inc.
  product       TourBox NEO
  serial        TourBox
  driver        cdc_acm   ->  /dev/ttyACM0
```

**The vendor ID is literally `0xcafe`.** That is not a placeholder — it is the
default ID from the TinyUSB stack, shipped unchanged. This matters because the
two main community drivers hardcode different IDs:

| Source | NEO ID it expects | Matches this unit? |
|---|---|---|
| `AndyCappDev/tuxbox` | `2e3c:5740` (Artery AT32 generic CDC) | **No** |
| Various forks | `2e3c:5740` | **No** |
| This unit | `cafe:4001` | — |

So this is a firmware variant neither driver has direct evidence for. Our udev
rule matches all three IDs plus a `ATTRS{product}=="TourBox*"` fallback.

---

## Wire format

115200 8N1, one byte per event. No framing, no checksums.

```
button press    ->  byte B
button release  ->  byte B | 0x80        # confirmed: 0x00 press -> 0x80 release
rotary step     ->  one byte per step, distinct byte per direction
```

Typical button hold measured at 0.12–0.25 s between press and release bytes.

---

## Verified button codes

All twelve captured from the device and cross-checked against
`bloodywing/tourboxneo`'s independently derived table. They agree on every
button. Tall was captured in two separate runs as an alignment check.

| Control | Press | Release | Agrees with tourboxneo |
|---|---|---|---|
| tall | `0x00` | `0x80` | yes |
| side | `0x01` | `0x81` | yes |
| top | `0x02` | `0x82` | yes |
| short | `0x03` | `0x83` | yes |
| scroll_click | `0x0A` | `0x8A` | **not in its table** |
| dpad_up | `0x10` | `0x90` | yes |
| dpad_down | `0x11` | `0x91` | yes |
| dpad_left | `0x12` | `0x92` | yes |
| dpad_right | `0x13` | `0x93` | yes |
| tour | `0x2A` | `0xAA` | yes |
| knob_click | `0x37` | `0xB7` | yes (`KNOB_DOWN`) |
| dial_click | `0x38` | `0xB8` | yes (`DIAL_DOWN`) |

### Three codes the guessed keymap got wrong

Worth recording because the failure mode is silent:

| Control | Guessed | Actual | Effect of the guess |
|---|---|---|---|
| knob_click | `0x0A` | `0x37` | `0x0A` is really *scroll_click*, so pressing scroll fired the knob's action |
| scroll_click | `0x0B` | `0x0A` | — |
| dial_click | `0x0C` | `0x38` | `0x0C` unused, dial press did nothing |

Nine of twelve guesses were right, which is exactly what makes this dangerous:
enough works that you assume the mapping is fine, and the three that collide
produce "random wrong actions" with nothing useful in the log.

---

## OPEN ISSUE: rotaries emit nothing

**On this unit the knob, scroll wheel and dial produce zero bytes.**

Two independent capture runs, each recording full slow rotations of all three
controls:

| Run | Handshake sent | Buttons captured | Rotary bytes |
|---|---|---|---|
| 1 | none | 12/12 clean | **0** |
| 2 | unlock `5500078894001afe` | Tall control byte clean | **0** |

Run 2 included a deliberate control: pressing Tall produced `0x00`/`0x80`
correctly in the same session in which full rotations produced nothing. So the
capture path was working and the silence is real, not a tooling artifact.

### What was ruled out

- **Not the recorder.** Buttons captured perfectly in both runs.
- **Not a missing handshake.** `bloodywing/tourboxneo` — a working NEO driver —
  opens the port with `serial.Serial(path, timeout=2)` and does nothing else.
  No init, no unlock. Yet it documents rotary codes, so rotaries are expected
  to report with no handshake at all.
- **Not the Elite unlock.** Sending `5500078894001afe` gets a valid 26-byte
  reply (`07e05cd1c1b2ca5e9ed3ec26336cf70b00000000000202000000`), so the device
  understands the command — but it does not enable rotary reporting.
- **Not detent confusion.** The NEO's knob and dial have no tactile detents;
  the encoders still emit per step. Full rotations were performed.

### Still open

- Whether the rotary encoders on this specific unit are functional at all.
  **Decisive test: connect to a Mac/Windows machine with TourBox Console and
  check whether the knob, scroll and dial register there.** That separates
  hardware fault from protocol difference in one step.
- Whether `cafe:4001` firmware uses a different enable sequence.

### Expected codes if they ever appear

From `tourboxneo`, retained in `keymap.toml` and clearly marked unverified. The
daemon logs any unmapped byte once with a pointer to the capture tool, so if the
real codes differ you will see them.

| Control | CW / up | CCW / down |
|---|---|---|
| knob | `0x44` | `0x04` |
| scroll | `0x49` | `0x09` |
| dial | `0x0F` | `0x4F` |

### Consequence for the build

The action layout in `keymap.toml` was rearranged so **nothing essential is
behind a rotary**. Volume moved to the D-pad, seek onto shift+D-pad. The player
is fully operable from buttons alone. If rotaries come alive, move volume back
to `[rotary.knob]`.

---

## The response-frame hazard

`tuxbox` documents this and it is worth restating, because any driver that
sends the unlock will hit it:

> A response frame's trailing run of `0x00` bytes maps to *tall*, so the driver
> injects a stream of key presses with no matching releases and leaves a
> modifier stuck down.

The observed 26-byte reply ends in exactly that run of `0x00`. Since `0x00` is
the Tall press code, feeding the frame to a decoder produces phantom presses
with no releases — and if one lands on the modifier, every later control
silently fires its *shifted* action.

**Our guard** (`tourbox_daemon._pump`) drops a read only when it is **both**
large (>= 8 bytes) **and** contains bytes that are not valid input codes.
Length alone is insufficient: a fast rotary spin legitimately delivers a dozen
or more bytes in one read. Content is what separates them — real input is
entirely made of keymap codes, a status frame is not.

There is a regression test for this in `tests/test_e2e.py` using the real
26-byte frame captured from this device.

Because the NEO needs no handshake and its reply is a hazard, `send_unlock`
defaults to **false** in `config.toml`. Set it true only for an Elite.

---

## Reproducing the capture

```bash
sudo systemctl stop tourbox-player

# raw byte dump with timestamps
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture --sniff

# guided keymap generation with collision detection
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture \
    --learn --out /tmp/keymap.toml
```

If you background a recorder over SSH, use `systemd-run --user` rather than
`nohup ... &` — the latter gets killed when the SSH session closes, which
silently produced an empty capture during this bring-up.
