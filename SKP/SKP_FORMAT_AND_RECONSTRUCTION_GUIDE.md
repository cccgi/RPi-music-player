# The HomeKara `.skp` Format — Full Findings and Reconstruction Guide

## Background

The karaoke box (X96Max Plus running Droidlogic/Amlogic firmware, using the
"SKPlayer"/HomeKara app) stores its ~56,000-song library as proprietary
`.skp` files. The original hardware is dead and the manufacturer is out of
business, so there was no way to keep using this library except by figuring
out the file format ourselves and converting it into standard, universally
playable video files.

This document records exactly what the format turned out to be, how that was
verified, and full instructions for using the toolkit that resulted.

---

## Part 1: How the format was actually solved

### The investigation, in order

1. **Screen mirroring showed black video.** scrcpy/ADB screen capture only
   ever showed UI/subtitles, never the actual video — traced to the Amlogic
   SoC decoding video to a hardware overlay plane that sits *below*
   Android's normal screen-compositing layer, invisible to any
   software-capture API (scrcpy, `screenrecord`, MediaProjection all have
   this same blind spot). This ruled out screen-capture entirely as a way
   to get at the content and shifted the approach to direct file extraction.

2. **Located the actual files.** The 56,353 `.skp` files live on an internal
   18TB HDD, alongside unrelated personal backup data (photos, DJ library,
   etc. on a *different* drive that had briefly confused an early
   investigation — worth flagging as a lesson: always confirm you're
   looking at the real target data, not an unrelated backup).

3. **Hex-dumped a sample file.** First 8 bytes: literal ASCII `"HomeKara"`
   — a real, custom container format, not a known standard. Bytes
   immediately after decoded as a **plain, unencrypted JPEG thumbnail**
   (confirmed via `ffd8 ffe0 ... JFIF` signature) — ruling out DRM/encryption
   as a concern immediately (real encryption would show high-entropy noise
   from byte 0; this showed readable structure instead).

4. **Found the key header field.** A 4-byte little-endian integer at file
   offset `0x18` turned out to be the exact byte offset where the thumbnail
   ends and the next section begins — confirmed by the fact that
   `header_value == 32 (header size) + actual_thumbnail_size` to the byte,
   across every sample file tested.

5. **The next section is a complete, standard Matroska (MKV) file.**
   Verified with `ffprobe`/`mkvinfo` — video track only (H.264 or HEVC
   depending on file), no audio. This was initially mistaken for "this
   format has no audio at all," until:

6. **Realized the true structure: whole files are just concatenated.**
   After the video MKV's own declared EBML/Segment size ends, there's
   **another complete, independent, standard file appended immediately
   after** — an `.m4a` (AAC) audio file, byte-for-byte identical (confirmed
   via SHA-1 hash match) to audio a user had separately extracted with
   another tool and knew to be correct. The container is, in essence:
   `[header][thumbnail][video.mkv][audio1.m4a]`.

7. **Found the second audio track the same way.** Initial testing seemed to
   show only one audio track — until direct listening tests (user-driven,
   correctly overriding an initial incorrect assumption from this
   investigation) proved conclusively that two genuinely different audio
   masters exist per song (original vocal + a separately produced backing
   track, sometimes at a different pitch — provably *not* derivable from
   one track via any real-time signal processing, since pitch differences
   require distinct source recordings). The second audio file is simply a
   **third concatenated file**, appended right after the first `.m4a`,
   detected by finding a second `ftyp` (MP4) box signature.

8. **Fixed a parser bug that mattered enormously at scale.** An early
   version of the parser correctly found the first embedded MP4 file but
   then kept "walking" straight through its box structure and into the
   *second* file's boxes without noticing a boundary — because a valid
   `ftyp`/`moov`/`mdat` sequence looks the same whether it's a continuation
   of one file or the start of a new one. The fix: **a `ftyp` box is only
   ever legitimate as the very first box of a file** — encountering a second
   one mid-walk is the signal that a new concatenated file has begun. This
   generalizes correctly to any number of embedded tracks (0, 1, 2, or more),
   not just exactly two.

### Final, confirmed `.skp` format

```
[8 bytes]   "HomeKara" magic
[24 bytes]  header fields (offset 0x18 = end offset of the JPEG thumbnail)
[N bytes]   1 JPEG thumbnail image
[N bytes]   1 complete, standard .mkv file -- video only (H.264 or HEVC)
[N bytes]   1 complete, standard .m4a file -- audio track #1
[N bytes]   1 complete, standard .m4a file -- audio track #2 (if present)
```

No encryption, no proprietary codecs, no interleaving — just a small custom
header followed by ordinary files glued together back-to-back. Every byte
in every tested file is accounted for by this structure exactly (verified by
summing header + video + audio1 + audio2 sizes against total file size).

### Known variation between files
- Some files have 0, 1, or 2 embedded audio tracks (not all songs shipped
  with a backing track). The toolkit auto-detects this per file.
- Padding before each embedded file's `ftyp`/EBML header varies slightly
  between files (0–16+ bytes seen) — the toolkit searches a window rather
  than assuming a fixed offset.
- **Not yet confirmed in any real sample:** a hypothetical variant where two
  tracks live inside one shared MP4 `moov` (rather than as two separate
  concatenated files). The toolkit flags this possibility for review
  (`trak_count_single` in scan reports) but hasn't encountered it in practice.
- `.vob` files (older DVD-era rips, also present in the library) are a
  completely different, ordinary MPEG-PS container with two real audio
  streams already — no custom parsing needed for those, just standard
  probing and remuxing.

---

## Part 2: The toolkit

All scripts below are attached to this message. They share consistent
conventions: resumable (safe to stop/restart, skips completed files),
disk-space aware (won't silently fail mid-run when a drive fills up), and
UTF-8 safe (handles Vietnamese filenames/metadata correctly on Windows).

### `batch_extract_skp.py` — the main tool
Reconstructs `.skp` (and `.vob`) files into a single playable `.mkv` or
`.mp4` per song, with both audio tracks preserved and correctly labeled.

**Single file:**
```bash
python3 batch_extract_skp.py "/path/to/song.skp" /path/to/output
```

**Whole folder (recursive):**
```bash
python3 batch_extract_skp.py /path/to/skp_folder /path/to/output
```

**CSV fuzzy-match mode** (given a list of song/singer names, possibly with
typos or missing accents, finds and processes only the matching files out of
the whole library):
```bash
python3 batch_extract_skp.py /path/to/skp_folder /path/to/output --csv-list songs.csv
```
CSV format: two columns, `song,singer` (header row optional, singer can be
blank per row).

**Scan-only mode** (fast, read-light, no media files written — just reports
track counts and flags anomalies, recommended as a first pass over the whole
library):
```bash
python3 batch_extract_skp.py /path/to/skp_folder /path/to/scan_output --scan-only
```

**Key options:**

| Flag | Purpose |
|---|---|
| `--container {mkv,mp4}` | Output format. `mp4` for iPhone/Photos compatibility (one track marked default for Photos, both present for apps like VLC-iOS that support track switching) |
| `--iphone-default-track {vocal,karaoke}` | Which track Photos plays by default (mp4 only) |
| `--swap` | Reorder to karaoke-first/vocal-second instead of the native SKP order (default keeps native order, which matches what the original SKPlayer app expects) |
| `--delete-source` | Delete each original `.skp` only after its replacement is verified — lets you reclaim disk space as you go without needing double the storage |
| `--on-low-space {exit,pause}` | What happens if the output drive runs low mid-batch: `exit` stops cleanly (just re-run later), `pause` waits and auto-resumes once space frees up |
| `--limit N` | Only process the first N files — always test with this before a full run |

### `mkv_to_mp4.py` — convert already-reconstructed files to Apple-compatible MP4
For when you've already built `.mkv` files and want iPhone/Photos-compatible
copies without redoing the whole extraction:
```bash
python3 mkv_to_mp4.py /path/to/mkv_folder /path/to/output_folder
```

### `swap_audio_tracks.py` — flip track order on already-built files
Useful because the original SKPlayer Android app reads tracks by raw stream
index (not by title), so files built for VLC convenience may need reordering
to play correctly back on the original app:
```bash
python3 swap_audio_tracks.py /path/to/folder --in-place
```

### `generate_ai_karaoke.py` — regenerate a better karaoke track with AI
For songs where the embedded backing track is lower quality than the vocal
track — uses AI vocal separation (`audio-separator`, the same underlying
models as UVR5) to generate a fresh, high-quality instrumental from the clean
vocal track:
```bash
python3 generate_ai_karaoke.py /path/to/mkv_folder /path/to/output --model MODEL_FILENAME
```
CUDA-accelerated by default (`--use-cuda`) for Nvidia GPU setups.

### `upload_to_youtube.py` — bulk upload to a private/unlisted playlist
Automatically reorders to karaoke-first before uploading (since YouTube only
keeps one audio track), for private on-the-go access from a phone:
```bash
python3 upload_to_youtube.py /path/to/mkv_folder --playlist-id PLxxxxxxxxxxxx
```
Requires one-time Google Cloud OAuth setup (see the script's own docstring)
and is subject to YouTube's API quota (~6 uploads/day by default).

### `setup_windows.ps1` — one-command Windows setup
Installs everything needed and writes out all the scripts above into a
project folder:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_windows.ps1
```

---

## Recommended workflow for the full library

1. **Scan first**, no files written, to see the overall shape of the library:
   ```bash
   python3 batch_extract_skp.py /path/to/library /path/to/scan_report --scan-only
   ```
2. **Test on a small batch** (20–50 files) with your intended final settings,
   and actually verify a few outputs play correctly in your target
   player(s) before committing further.
3. **Run the full batch**, with `--delete-source` if reclaiming space as you
   go, and `--on-low-space pause` for long unattended runs.
4. **Optional follow-up passes**: `mkv_to_mp4.py` for iPhone copies,
   `generate_ai_karaoke.py` for songs with weak backing tracks,
   `upload_to_youtube.py` for a private cloud backup/phone-access copy.

---

## Verification note

Every claim about the format in this document was confirmed empirically
during the investigation — not assumed: exact byte-offset math checked
against real file sizes, SHA-1 hash comparison against independently
extracted reference audio, and direct listening tests on the reconstructed
output (video + both audio tracks, confirmed playing correctly and
switchable in VLC). The toolkit's parsing logic was iterated on specifically
in response to real edge cases found this way (the second-file boundary bug,
padding variance, files with 0/1/2 tracks), rather than written once and
assumed correct.
