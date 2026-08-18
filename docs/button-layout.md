# Button Layout Reference

## Stream Deck XL

The Stream Deck XL is **8 columns × 4 rows = 32 keys**, numbered left-to-right, top-to-bottom.

```
Col:  0        1        2        3        4        5        6        7
    ┌────────┬────────┬────────┬────────┬────────┬────────┬────────┬────────┐
R0  │  0     │  1     │  2     │  3     │  4     │  5     │  6     │  7     │
    ├────────┼────────┼────────┼────────┼────────┼────────┼────────┼────────┤
R1  │  8     │  9     │ 10     │ 11     │ 12     │ 13     │ 14     │ 15     │
    ├────────┼────────┼────────┼────────┼────────┼────────┼────────┼────────┤
R2  │ 16     │ 17     │ 18     │ 19     │ 20     │ 21     │ 22     │ 23     │
    ├────────┼────────┼────────┼────────┼────────┼────────┼────────┼────────┤
R3  │ 24     │ 25     │ 26     │ 27     │ 28     │ 29     │ 30     │ 31     │
    └────────┴────────┴────────┴────────┴────────┴────────┴────────┴────────┘
```

---

### Music Page 1

```
Col:  0           1           2           3           4           5           6           7
    ┌───────────┬───────────┬───────────┬───────────┬───────────┬───────────┬───────────┬───────────┐
R0  │ Browser   │ Browser   │ Browser   │ Browser   │ Now       │ Library   │ Volume    │ Bitrate / │
    │ slot 0    │ slot 1    │ slot 2    │ slot 3    │ Playing   │ Root      │ bar/Mute  │ Toast     │
    ├───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┤
R1  │ Progress  │ Curate ♥  │ Vol −     │ Vol +     │ +30s >>>  │ Shuffle   │ Repeat    │ Consume   │
    │ (tap=     │           │           │           │           │ (Off/     │ (Off/     │           │
    │ restart)  │           │           │           │           │ Folder/   │ Folder/   │           │
    │           │           │           │           │           │ All)      │ Song)     │           │
    ├───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┤
R2  │ Prev      │ Prev      │ Seek −    │ Play /    │ Seek +    │ Next      │ Next      │ Delete 🗑  │
    │ Album     │ Track     │ 15s       │ Pause     │ 15s       │ Track     │ Album     │           │
    ├───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┤
R3  │ Page 2 ▶  │ Route 1   │ Route 2   │ Video     │ Browser   │ Storage   │ Browser   │ Power     │
    │           │ (BT)      │ (AirPlay) │ Mode      │ Back ←    │ (Int/USB) │ Page ▶    │           │
    └───────────┴───────────┴───────────┴───────────┴───────────┴───────────┴───────────┴───────────┘
```

**Row 0 — Now Playing strip**
| Key | Label | Action |
|-----|-------|--------|
| 0–3 | Library browser (4 rows) | Tap to navigate/play |
| 4 | Now Playing title | Tap = Play/Pause |
| 5 | Library Root | Jump browser to root |
| 6 | Volume bar | Tap = Mute/unmute |
| 7 | Bitrate / Toast | Display only (also toast overlay) |

**Row 1 — Modes & telemetry**
| Key | Label | Action |
|-----|-------|--------|
| 8 | Progress bar | Tap = Restart track |
| 9 | Curate ♥ | Move current file → Curated/ subfolder |
| 10 | Vol − | Volume down |
| 11 | Vol + | Volume up |
| 12 | +30s | Fast-forward 30 s |
| 13 | Shuffle | Cycle: Off → Folder → All → Off |
| 14 | Repeat | Cycle: Off → Folder → Song → Off |
| 15 | Consume | Toggle consume mode |

**Row 2 — Transport**
| Key | Label | Action |
|-----|-------|--------|
| 16 | ⏮ Album | Previous album |
| 17 | ⏮ Track | Previous track |
| 18 | ◀◀ −15s | Seek back 15 s |
| 19 | ▶/⏸ | Play / Pause |
| 20 | ▶▶ +15s | Seek forward 15 s |
| 21 | ⏭ Track | Next track |
| 22 | ⏭ Album | Next album |
| 23 | Delete 🗑 | Delete current (2-press confirm) |

**Row 3 — Routing & navigation**
| Key | Label | Action |
|-----|-------|--------|
| 24 | Next Page ▶ | Open Page 2 browser |
| 25 | BT | Bluetooth picker |
| 26 | AirPlay | AirPlay picker |
| 27 | Video | Switch to Video mode |
| 28 | Back ← | Browser: go up one folder |
| 29 | Storage | Toggle Internal ↔ USB |
| 30 | Page ▶ | Browser: next page |
| 31 | Power | Shutdown (2-press confirm) |

---

### Music Page 2 — Full Browser Grid

```
Col:  0      1      2      3      4      5      6      7      8 slots (cols 0–7)
    ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
R0  │ ..   │ [1]  │ [2]  │ [3]  │ [4]  │ [5]  │ [6]  │ [7]  │  ← 8 content slots
    ├──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤
R1  │ [8]  │ [9]  │[10]  │[11]  │[12]  │[13]  │[14]  │[15]  │  ← 8 content slots
    ├──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤
R2  │ ◀◀   │Curate│ [16] │ [17] │ [18] │ [19] │Delete│  ▶▶  │
    │ Page │  ♥   │      │      │      │      │  🗑   │ Page │
    ├──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤
R3  │Prev  │ Int  │ USB  │ ▶/⏸  │+15s  │ Next │ Scan │Local │
    │ Page │      │      │      │      │      │      │      │
    └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
```

Slot `..` (key 0) = go up one folder. Slots 1–19 = folder/file entries (folders listed first). Row 2 col 0/7 = page back/forward. Bottom row provides quick transport without leaving the browser.

| Key (R3) | Label | Action |
|----------|-------|--------|
| 24 | Prev Page | Close Page 2, return to Page 1 |
| 25 | Internal | Switch to internal storage & go to root |
| 26 | USB | Switch to USB storage & go to root |
| 27 | ▶/⏸ | Play / Pause |
| 28 | +15s | Seek forward 15 s |
| 29 | Next | Next track |
| 30 | Scan | Rescan MPD library |
| 31 | Local | Route audio to local/USB-DAC |

---

### Video Page 1

```
Col:  0           1           2           3           4           5           6           7
    ┌───────────┬───────────┬───────────┬───────────┬───────────┬───────────┬───────────┬───────────┐
R0  │ Up Next   │ Up Next   │ Up Next   │ Up Next   │ Now       │ Vocal /   │ Volume    │ Screen    │
    │ slot 0    │ slot 1    │ slot 2    │ slot 3    │ Playing   │ Sing      │ bar/Mute  │ On/Off    │
    │ (or browse│           │           │           │           │ (track)   │           │           │
    │ contents) │           │           │           │           │           │           │           │
    ├───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┤
R1  │ Progress  │ Curate ♥  │ Vol −     │ Vol +     │ Folder 0  │ Folder 1  │ Power     │ Wi-Fi     │
    │           │           │           │           │ (or ←Back)│           │ Draw      │ Toggle    │
    ├───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┤
R2  │ ⏮ Prev   │ ◀◀◀ −30s  │ ◀◀ Seek   │ ▶/⏸       │ ▶▶ Seek   │ ▶▶▶ +30s  │ ⏭ Next   │ Delete 🗑  │
    │           │           │           │           │           │           │           │           │
    ├───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┼───────────┤
R3  │ Page 2 ▶  │ BT        │ AirPlay   │ Music     │ ⏮ Songs   │ Storage   │ ⏭ Songs   │ Power     │
    └───────────┴───────────┴───────────┴───────────┴───────────┴───────────┴───────────┴───────────┘
```

**Row 0**
| Key | Label | Action / Notes |
|-----|-------|----------------|
| 0–3 | Up Next / Browse | In normal mode: next 4 videos in library. In folder-browse mode: folder contents (dirs first, then files). Tap a folder = navigate in; tap a file = play & return to normal mode |
| 4 | Now Playing | Tap = Play/Pause |
| 5 | Vocal / Sing | Toggle between original (Vocal) and karaoke (Sing) audio track |
| 6 | Volume bar | Tap = Mute/unmute |
| 7 | Screen | Turn Stream Deck backlight off (any key press wakes it) |

**Row 1**
| Key | Label | Action / Notes |
|-----|-------|----------------|
| 8 | Progress | Display only |
| 9 | Curate ♥ | Move current video → Curated/ subfolder |
| 10 | Vol − | Video volume down |
| 11 | Vol + | Video volume up |
| 12 | Folder slot 0 | At root: tap top-level folder to browse into it. When browsing: tap "← Back" to go up |
| 13 | Folder slot 1 | At root: second top-level folder shortcut. When browsing: blank |
| 14 | Power Draw | Pi board current draw — display only |
| 15 | Wi-Fi | Toggle Wi-Fi on/off |

**Row 2 — Transport**
| Key | Label | Action |
|-----|-------|--------|
| 16 | ⏮ | Previous video |
| 17 | ◀◀◀ −30s | Seek back 30 s |
| 18 | ◀◀ Seek | Seek back 15 s |
| 19 | ▶/⏸ | Play / Pause |
| 20 | ▶▶ Seek | Seek forward 15 s |
| 21 | ▶▶▶ +30s | Seek forward 30 s |
| 22 | ⏭ | Next video |
| 23 | Delete 🗑 | Delete current video (2-press confirm) |

**Row 3**
| Key | Label | Action |
|-----|-------|--------|
| 24 | Next Page ▶ | Open Video Page 2 browser |
| 25 | BT | Bluetooth picker |
| 26 | AirPlay | AirPlay picker |
| 27 | Music | Return to Music mode |
| 28 | ⏮ Songs | Scroll Up Next window backward |
| 29 | Storage | Toggle Internal ↔ USB video library |
| 30 | ⏭ Songs | Scroll Up Next window forward |
| 31 | Power | Shutdown (2-press confirm) |

---

### Video Page 2 — Full Browser Grid

Same geometry as Music Page 2 with video-specific bottom row:

| Key (R3) | Label | Action |
|----------|-------|--------|
| 24 | Prev Page | Close Page 2, return to Video Page 1 |
| 25 | Internal | Switch to internal video storage & go to root |
| 26 | USB | Switch to USB video storage & go to root |
| 27 | ▶/⏸ | Video Play / Pause |
| 28 | +15s | Video seek forward 15 s |
| 29 | Next | Next video |
| 30 | Scan | Rescan video library |
| 31 | Local | Route audio to local output |

Row 2 also has a unique **HDMI** reinit button at col 5 (key 21): restarts mpv's video output — use when HDMI monitor is plugged in after boot.

---

### AirPlay Picker

Keys 0–5 (row 0): available AirPlay sinks (dynamic).  
Key 29: Reconnect Wi-Fi.  
Key 30: Fix Wi-Fi (restart networking).  
Key 31: Back.  
Keys 8–9: Wi-Fi status / IP address display.

---

### Bluetooth Picker

Keys 0–5 (row 0): known/discovered Bluetooth devices (dynamic).  
Key 30: Reset stuck pairings.  
Key 31: Back.

---

## TourBox NEO

The TourBox has physical buttons (no screen). **Top** button cycles layers. **Side** button is a shift modifier.

### Physical Controls

| Control | Type | Notes |
|---------|------|-------|
| Tall | Button | Large tall button |
| Short | Button | Short button |
| Top | Button | Layer cycle (Track → Folder → back to Track). Video layer only entered via Stream Deck |
| Side | Button | Shift modifier (hold + another button for shifted action) |
| Tour | Button | Double-click only — single press does nothing |
| C1 | Button | Left side button |
| C2 | Button | Right side button |
| D-pad Up | Button | |
| D-pad Down | Button | |
| D-pad Left | Button | |
| D-pad Right | Button | |
| Knob click | Button | Encoder push (rotation broken on this unit) |
| Dial click | Button | Encoder push (rotation broken on this unit) |
| Scroll click | Button | Encoder push (rotation broken on this unit) |

> **Note:** All three rotary encoders (Knob, Scroll, Dial) emit no bytes on rotation on this unit — only their push-clicks work. This is a firmware issue, not a wiring fault.

---

### Layer 1 — Track (default)

| Control | Press | Shift + Press |
|---------|-------|---------------|
| Tall | Play / Pause | Stop |
| Short | Next Track | Mute toggle |
| C1 | Previous Track | Previous Album |
| C2 | Next Track | Next Album |
| D-pad Up | Volume Up | Volume Up |
| D-pad Down | Volume Down | Volume Down |
| D-pad Left | Seek −15 s | Seek −60 s |
| D-pad Right | Seek +15 s | Seek +60 s |
| Tour (double) | Delete current | — |
| Knob click | Toggle crossfade (10 s / 5 s) | Cycle output route (back) |
| Dial click | Restart track (seek to start) | — |
| Scroll click | Toggle shuffle | Toggle repeat |

---

### Layer 2 — Folder

Only the controls that differ from Layer 1 are listed. All others fall back to Layer 1.

| Control | Press | Shift + Press |
|---------|-------|---------------|
| C1 | Previous Folder | Previous Album |
| C2 | Next Folder | Next Album |
| D-pad Left | Seek −30 s | — |
| D-pad Right | Seek +30 s | — |

---

### Layer 3 — Video

Entered only via the Stream Deck's **Video** key (not by cycling Top). All shifted controls are nooped to prevent accidentally sending MPD commands while video is playing.

| Control | Press |
|---------|-------|
| Side | Switch track (Vocal ↔ Sing/Karaoke) |
| Tall | Video Play / Pause |
| Short | Next Video |
| C1 | Previous Video |
| C2 | Next Video |
| D-pad Left | Video seek back 15 s |
| D-pad Right | Video seek forward 15 s |
| D-pad Up | Video volume up |
| D-pad Down | Video volume down |
| Tour (double) | Delete current video |
| Knob click | No-op |
| Dial click | No-op |
| Scroll click | No-op |

All Shift combos in Video layer: **no-op** (prevents music controls firing while video is on screen).
