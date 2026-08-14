# Getting the Pi's audio into a 2019 Toyota Camry LE

**Short answer: don't chase CarPlay. Use the AUX jack.**

Your Sound Blaster Play! 3 has a 3.5mm output, the Camry LE has a 3.5mm AUX
input, and that path keeps the entire build intact — MPD, the TourBox, the
Stream Deck, bit-perfect decoding — with a £5 cable and no new code.

The rest of this document is the evidence for why the CarPlay routes are dead
ends, so the decision doesn't have to be re-litigated later.

---

## 1. Can the Pi emulate a CarPlay device over USB? No.

Not "difficult" — **structurally blocked**.

CarPlay is an MFi (Made for iPhone) application. Every CarPlay session is gated
behind Apple's authentication scheme:

- Video and control streams use **MFi-SAP** (Secure Association Protocol),
  encrypting the link with AES-128 in counter mode.
- Authentication depends on an **Apple Authentication Coprocessor** — a
  physical chip, sold only to licensed MFi manufacturers, connected over I²C.
- When a device connects, the other end issues a **cryptographic challenge**.
  The coprocessor signs it with a per-chip certificate that verifies against
  Apple's public keys. Without a genuine chip the handshake cannot complete.

There is no software workaround. The private key lives in tamper-resistant
silicon; that is the entire point of the design. You would need to join Apple's
MFi programme as a manufacturer and buy the coprocessor — not available to
individuals, and the licence covers building *accessories*, not impersonating
an iPhone.

**Direction matters, and it's the opposite of what you'd want.** In a CarPlay
session the head unit is the accessory and the iPhone is the source. To feed
the Camry you'd have to convince it your Pi *is an iPhone* — the hardest
possible side of the protocol to fake.

### Why the open-source projects don't help

Searching this turns up promising-looking projects. They all solve the mirror
image of your problem:

| Project | What it does | Useful here? |
|---|---|---|
| OpenAuto / Crankshaft | Pi becomes an **Android Auto head unit** — receives from a phone | No — wrong protocol, wrong direction |
| `node-carplay`, `react-carplay`, `pycarplay` | Pi becomes a **CarPlay screen**, using a Carlinkit CPC200-**CCPA** dongle as the CarPlay host | No — the Pi is the *display*, still needs a real iPhone as source |
| [LIVI](https://github.com/f-io/LIVI) (formerly `pi-carplay`) | The most actively maintained one of these. Pi becomes a **standalone CarPlay/Android Auto head unit** with hardware-accelerated GStreamer video, using a Carlinkit dongle for the MFi handshake | No — same direction as the others; see the addendum below |

Every one of them puts the Pi on the receiving end. None makes it a source.

---

## 2. Would the Carlinkit CPC200-U2W Plus help? No — it moves the barrier, it doesn't remove it.

The U2W is a **wired-to-wireless CarPlay converter**. Its job:

```
iPhone  --(Bluetooth pairing, then 5GHz WiFi)-->  CPC200-U2W  --(USB)-->  car head unit
```

It plugs into the car's USB port and presents itself to the head unit as a
wired CarPlay device. It then advertises Bluetooth, an **iPhone** pairs to it,
it hands the phone WiFi credentials, drops the Bluetooth link, and carries
CarPlay over WiFi from that point on.

The dongle contains the MFi silicon for the *head-unit side*. On the *phone
side* it expects a genuine iPhone completing a genuine CarPlay handshake. Your
Pi would have to impersonate an iPhone to the dongle — **exactly the same MFi
wall, one hop further away**, now with an extra £70 box in the chain.

Buying it for this purpose would not work.

> Note the model letters. **CPC200-U2W** = phone → car (what you looked at).
> **CPC200-CCPA** = the one the `node-carplay` projects use to turn a Pi into a
> CarPlay screen. Neither turns a Pi into a CarPlay *source*.

---

## 3. USB mass-storage gadget — possible, but it defeats the build

The Pi 5 *can* run USB gadget mode and appear to the Camry as a USB stick. The
head unit would read and play the files itself. It's clever, and it's wrong
for this project.

**Technical constraints:**

- Gadget mode works **only on the USB-C port** on a Pi 5, and that port is
  **also the power input**. In gadget mode plugged into a car USB port you'd be
  asking a ~500 mA socket to run a board that wants a 5 A supply. You'd have to
  power the Pi via the GPIO 5 V rail instead — doable, but now it's a wiring
  project.
- The USB-C port carries **USB 2.0 data only** on the Pi 5 (the USB 3 pins
  aren't connected).

**But the real objection is architectural.** In this mode the head unit does
the decoding, which means:

- **The head unit controls playback**, not you. MPD, the TourBox layers, the
  Stream Deck, the folder auto-advance — all bypassed. You'd be steering with
  the Camry's own clunky USB browser.
- **Format support collapses to the head unit's list** — MP3/WMA/AAC. Your
  FLAC and WAV library would not play.
- **No bit-perfect anything.** You'd have built a very expensive USB stick.

This is a file-transfer trick, not an audio path.

---

## 4. What actually works

### Recommended: AUX from the Sound Blaster

```
MPD --hw:CARD=S3 (bit-perfect)--> Sound Blaster Play! 3 --3.5mm--> Camry AUX
```

The 2019 Camry LE's Entune 3.0 includes an **auxiliary audio jack** alongside
the USB 2.0 port. Toyota even list the part separately (`8553206010`), and
retrofit interfaces exist for later model years that dropped it.

Why this is the right answer:

- **Best quality of any option here.** The digital chain stays bit-perfect all
  the way to the DAC; only then does it become analog line level. No Bluetooth
  codec, no re-encode, no head-unit transcoding.
- **Everything you've built keeps working.** MPD, TourBox layers, folder
  auto-advance, the Stream Deck, delete, output routing.
- **Zero new code.** The `Local` route already drives this exact DAC.
- **Cost: one 3.5mm cable.**

> **Verify your specific car.** AUX availability varied by trim and region, and
> Toyota dropped it on some later Camrys. Check for a 3.5mm socket near the USB
> port before buying anything.

### Fallback: Bluetooth A2DP

Already built and proven — the same code path that drives your QC45. Pair the
Pi to the Camry and select the `BT` route.

- **Wireless**, no cables at all.
- **Lossy** — SBC, or AAC if the car negotiates it. Fine in a car, where road
  noise dominates well before codec artefacts do.
- Uses the Pi's onboard radio, which shares silicon with Wi-Fi. In the car
  that's less of a problem since you probably aren't on Wi-Fi.

---

## 5. Recommendation

| Option | Verdict |
|---|---|
| CarPlay emulation over USB | **No.** MFi auth coprocessor; no software path exists. |
| Carlinkit CPC200-U2W Plus | **No.** Wrong direction; same MFi wall plus £70. |
| USB mass-storage gadget | **No.** Power conflict, and it bypasses the entire player. |
| **AUX via Sound Blaster** | **Yes — do this.** Best quality, zero new code, one cable. |
| Bluetooth A2DP | **Yes — as the wireless fallback.** Already working. |

**Nothing needs to be architected.** Both viable paths are already implemented
and tested; they are `Local` and `BT` on the existing output router.

---

## 6. Optional polish: a "car mode"

If you want the in-car experience smoother, none of it requires new protocols —
just small additions to what exists:

1. **Auto-select the output on boot.** Add a config key for a preferred startup
   route so it comes up on `Local` (AUX) without touching anything.
2. **Auto-resume.** `restore_paused "yes"` is already set in `mpd.conf`; pair it
   with a startup "press play" so music begins when the car powers the Pi.
3. **Power sequencing.** A car USB port cuts power with the ignition. That is
   an abrupt power loss on every trip — exactly the filesystem-corruption risk
   flagged in `ARCHITECTURE.md` risk #6. For in-car use, either fit a UPS HAT
   with a clean-shutdown trigger, or switch the root filesystem to read-only
   with overlayfs.

Point 3 is the one that matters. Everything else is convenience; that one is
the difference between a player that lasts and one that eventually won't boot.

---

## 7. Addendum: reviewing a "USB gadget mode + LIVI" blueprint

A second-opinion writeup proposed emulating CarPlay by putting the Pi 5's
USB-C port into gadget mode (`dwc2`, `dr_mode=peripheral`) so it enumerates
*as* a device to the Camry, then running [LIVI](https://github.com/f-io/LIVI)
(a real, actively-maintained project — formerly `pi-carplay`) with a Carlinkit
dongle to handle the actual CarPlay session. Verified LIVI's own project
description directly, since this is the load-bearing claim:

> "Apple CarPlay (wired & wireless) on Linux **requires an MFi authentication
> coprocessor**."

That's LIVI's own documentation confirming section 1's conclusion, not
contradicting it. What LIVI actually is: the Carlinkit dongle owns the MFi
handshake with the iPhone and hands LIVI **already-decoded** video frames and
audio over the dongle's own (non-CarPlay) USB protocol. LIVI's GStreamer
pipeline renders those frames to **its own local screen and speakers** — it
*is* the head unit. It does not — and structurally cannot — take that decoded
media and re-encode it back into a genuine CarPlay session for a *second*,
unrelated head unit to accept. The Camry's Entune unit would still need to
complete its own MFi handshake with whatever's plugged into its USB port, and
a Pi gadget-mode device — with or without LIVI running — has no MFi
coprocessor of its own to complete that handshake. Relocating the coprocessor
into a dongle upstream of the Pi doesn't give the Pi one downstream of it.

In short: LIVI + a Carlinkit dongle is a real way to make the **Pi replace**
the Camry's head unit entirely (own screen, own speakers, no Entune
involved). It is not a way to make the Pi a source that *feeds* the existing
Entune unit — which was the actual goal. The two proposals in that writeup —
"bridge into the Camry's USB port" and "run LIVI" — solve different problems;
combining their names doesn't combine their function.

The rest of that writeup's blueprint (Bluetooth broadcasting as an "iAP2
accessory discovery target" that performs a credential exchange with no
coprocessor involved) does not correspond to any capability in BlueZ or any
verifiable open-source project — it reads as a plausible-sounding
architecture rather than a built, working one. Worth being skeptical of
CarPlay "blueprints" generally: MFi is the single fact that determines
whether any approach can work, and it's also the fact that's cheapest to
gloss over convincingly.

**This doesn't change section 5's recommendation.**
