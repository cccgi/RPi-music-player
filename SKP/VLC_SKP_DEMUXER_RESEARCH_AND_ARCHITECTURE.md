# Native VLC `.skp` Playback — Research Findings & Architecture Plan

## Executive Summary

Building native `.skp` playback support into VLC is feasible **without forking
or rebuilding VLC itself**. VLC has a well-established out-of-tree plugin
system: a standalone shared library (`.dll` on Windows) compiled against
VLC's public SDK, dropped into VLC's existing plugins folder, auto-discovered
at startup. This is confirmed directly by VLC's own project lead (Jean-Baptiste
Kempf) on the official forum and by current VideoLAN Wiki documentation.

Since we already have a complete, byte-exact reverse-engineered `.skp` format
specification (see `SKP_FORMAT_AND_RECONSTRUCTION_GUIDE.md`), the plugin's
job is narrower than "write a media parser from scratch": locate the
embedded video/audio byte ranges, and **reuse VLC's own existing, mature
MKV and AAC/M4A demuxers** for the actual container/codec parsing, rather
than reimplementing that logic. This document lays out what's been confirmed
about VLC's plugin architecture, two candidate technical approaches for the
actual demuxing mechanism, and a phased plan from initial toolchain
validation through to a working, packaged plugin.

---

## Part 1: Research Findings

### 1.1 Plugin architecture (confirmed)

- VLC plugins are shared libraries linked against `libvlccore`, exporting a
  version-suffixed entry point (`vlc_entry__VERSION`). This means **a
  plugin's binary compatibility is tied to a specific VLC major
  version/ABI** -- a plugin built against the VLC 3.x SDK works with VLC 3.x
  installations, not automatically with 4.x (still rolling out as of this
  writing). Recommendation: target VLC 3.x first, since it's the current
  stable/widely-deployed line.
- Out-of-tree compilation is officially supported and documented: VideoLAN
  Wiki's **"OutOfTreeCompile"** and **"Hacker Guide/How To Write a Module"**
  pages describe the exact process, including on Windows (headers and
  import libraries are inside the `sdk/` folder of the official
  `vlc-*-win*.7z` release archive).
- Real, current (2026) precedent exists for exactly this kind of project:
  - `github.com/amCap1712/vlc-listenbrainz-plugin` -- a working out-of-tree
    plugin with a complete Windows build recipe using MSYS2 + mingw-w64.
  - `github.com/InterDigitalInc/VTMDecoder_VLCPlugin` -- a real custom
    demux+decoder plugin pair shipped for VLC on Windows, proving this
    general "new container format" scenario has working prior art.

### 1.2 The demux module API (confirmed, from VideoLAN's official Hacker
Guide and VLC's public header `include/vlc_demux.h`)

A demux module implements:
- `pf_demux` -- pulls data from the input `stream_t` (the raw byte stream),
  returns 0 for EOF, positive for success, negative for failure
- `pf_control` -- handles queries (seeking, duration, etc.)
- Creates and manages elementary stream track objects (`es_out_id_t`) via
  the module's `es_out_t`, pushing decoded-container (but not
  decoded-codec) data blocks to each track for VLC's decoder stage to consume

This is exactly the mechanism needed to expose "1 video track + 2 audio
tracks" to VLC's UI and track-switching menu -- precisely matching what our
`ffmpeg`-based reconstruction currently produces as a static file, just
generated live.

### 1.3 Two candidate architectures for the actual sub-stream handling

This is the one area where Phase 1 research below needs to confirm the
cleanest path -- both are real, existing VLC mechanisms, but which is more
robust/maintainable for our specific case needs validation against VLC's
current source before committing.

**Architecture A: Aggregator demux module (child-demuxer composition)**

Our plugin registers as a standard `demux` capability module. Its `Open()`:
1. Validates the `"HomeKara"` magic header and parses our known header
   fields (offset `0x18`, etc. -- logic already fully written and validated
   in `batch_extract_skp.py`'s `parse_skp()`/`scan_skp_fast()`).
2. Creates 2-3 **byte-range-clipped sub-streams** of the underlying
   `stream_t` (one for the embedded MKV's byte range, one each for the
   embedded M4A audio files' byte ranges).
3. Hands each sub-stream to VLC's own internal demux-selection logic to
   spawn a **child demuxer instance** per sub-stream (VLC's own MKV demuxer
   for the video range, VLC's own MP4/M4A demuxer for each audio range) --
   avoiding any reimplementation of container/codec parsing.
4. On each `Demux()` call, our module round-robins pulling packets from the
   child demuxers and forwards their elementary streams into **our own**
   `es_out_t`, presenting one unified program with 1 video ES + N audio ES
   tracks to VLC's playback pipeline.

`src/input/demux.c` in VLC's own source tree is the concrete file to study
for exactly how VLC's core creates/dispatches demuxer instances -- this is
the first deep-research task in Phase 1.

**Architecture B: `stream_extractor` sub-item exposure**

VLC's public ABI (confirmed present in the real `vlc-3.0.23` exported
symbol table: `vlc_stream_extractor_Attach`, `vlc_stream_extractor_CreateMRL`)
includes a dedicated module capability for exposing sub-items *within* a
container as independently addressable streams -- this is the actual
mechanism VLC uses today to let you open a specific file inside a `.zip`
or a track inside a disc image. Under this approach, our plugin would:
1. Register as a `stream_extractor`, recognizing `.skp` files
2. Expose the embedded video and each audio track as separate virtual
   sub-items (e.g. derived MRLs like `skp://path/to/song.skp/video`,
   `.../audio1`, `.../audio2`)
3. Let VLC's *existing*, completely unmodified demux auto-detection open
   each sub-item normally

The open question for Phase 1: `stream_extractor` is designed around
*archive-like* browsing (picking one sub-item to open), not natively around
"open all three simultaneously as one synchronized multi-track playback
session." Combining three independently-opened sub-items into one playback
session with track-switching would likely need an additional mechanism
(possibly modeled on VLC's `--input-slave` multi-file-as-one-session
feature) layered on top. This may end up more complex than Architecture A
despite feeling more "idiomatic" -- needs a real prototype to know for sure.

**Recommendation:** Prototype Architecture A first. It's the more
traditional, better-documented demux pattern, and the "aggregate multiple
child demuxers into one program" need is a known, established pattern in
VLC's own codebase (used for things like podcast enclosures and certain
playlist-driven multi-part media). Architecture B stays as a documented
alternative if A hits an unexpected wall.

---

## Part 2: Phased Implementation Plan

### Phase 0 -- Toolchain validation (no `.skp` logic yet)
**Goal:** confirm the out-of-tree plugin build/deploy pipeline works end to
end in your actual environment before writing any format-specific code.
- Set up MSYS2 + mingw-w64 toolchain (following the `vlc-listenbrainz-plugin`
  Windows build recipe as a template)
- Download the VLC SDK (`vlc-*-win64.7z`, matching whatever VLC version
  you'll run day-to-day)
- Build VLC's own minimal "empty module" example, install it into VLC's
  plugins folder, confirm VLC's plugin cache picks it up (`vlc -vvv` should
  log the module being loaded)
- **Exit criterion:** a trivial no-op plugin loads successfully in your real
  VLC installation

### Phase 1 -- Deep API research and a probe-only plugin
**Goal:** resolve the Architecture A vs. B question with actual current
VLC source, and get a plugin that *recognizes* `.skp` files without playing
them yet.
- Read `src/input/demux.c` and a representative existing demuxer (e.g.
  `modules/demux/mp4/mp4.c` or a simpler one) in the current VLC source tree
  to confirm the exact child-demuxer spawning API and its stability/version
  history
- Implement `Open()` probe logic: read first 32 bytes, check `"HomeKara"`
  magic, parse header field at offset `0x18` -- port directly from the
  already-validated Python reference implementation
- Register the module with an appropriate capability score so VLC tries it
  for `.skp` files
- **Exit criterion:** opening a `.skp` file in VLC shows your plugin's log
  output confirming successful format detection, even though playback
  doesn't work yet

### Phase 2 -- Single-track MVP (video only)
**Goal:** prove the child-demuxer/sub-stream mechanism actually works for
one track before adding complexity.
- Implement the byte-range-clipped sub-stream wrapper for the embedded MKV
  region (reusing the EBML segment-size parsing logic already written and
  tested in Python)
- Spawn a child demuxer for that sub-stream, forward its video ES into our
  own `es_out_t`
- **Exit criterion:** a `.skp` file plays back its video (silently) directly
  in VLC, no conversion step involved

### Phase 3 -- Multi-track audio, full feature parity
**Goal:** match what the current `ffmpeg`-based reconstruction already
delivers, but live.
- Extend the header parser to locate all embedded audio tracks (reusing the
  already-solved "stop at the second `ftyp` box" boundary-detection logic --
  this is the exact bug we found and fixed in the Python tooling; the C
  port needs the same fix from day one, not rediscovered the hard way)
- Spawn a child demuxer per audio track, expose each as a separate audio ES
  -- VLC's native Audio Track menu should then show both, exactly like the
  `.mkv` files produced by the existing toolkit
- Handle the variable-track-count cases already known from the Python
  implementation: 0, 1, 2, or (unconfirmed but handled) 3+ tracks
- **Exit criterion:** a `.skp` file plays in VLC with working video, and the
  Audio Track menu correctly lists and switches between vocal/karaoke exactly
  as the converted `.mkv` files do today

### Phase 4 -- Robustness and edge cases
- Padding variance before each embedded file's header (already handled in
  Python via a search window, not a fixed offset -- port the same tolerance)
- Files with only 1 audio track, or 0
- Seeking support (`pf_control` -- seeking within an aggregated multi-child-demuxer
  stream is a real complexity point worth explicit test coverage)
- Graceful failure on a corrupted/truncated `.skp` file (matching the
  clear-error-message philosophy already used in the Python tooling, rather
  than a hard crash)

### Phase 5 -- Packaging, testing at scale, distribution
- Build a headless smoke-test harness: `vlc --intf dummy --play-and-exit
  <file>` scripted across a sample of real `.skp` files from the library,
  checking for clean exit / no fatal errors
- Validate against a meaningful sample size before trusting the whole
  56,353-file library (mirrors the "test small, then scale" approach used
  throughout the conversion toolkit)
- Document installation (where to drop the `.dll`, any VLC preference
  settings needed) as a short user-facing README, separate from this
  development-focused document

---

## Part 3: Proposed Source Tree / Build Scaffold

```
vlc-skp-demux/
├── src/
│   ├── skp_demux.c        # main demux module: Open/Close/Demux/Control
│   ├── skp_header.c       # port of parse_skp()'s header/offset logic
│   ├── skp_header.h
│   ├── skp_substream.c    # byte-range-clipped stream_t wrapper
│   └── skp_substream.h
├── Makefile                # out-of-tree build, following the
│                            # vlc-listenbrainz-plugin / OutOfTreeCompile pattern
├── test/
│   ├── smoke_test.py       # scripted headless VLC playback test harness
│   └── sample_files/       # small set of representative .skp test files
└── README.md                # end-user install instructions
```

**Build system**: follow the confirmed out-of-tree pattern (`pkg-config
--cflags vlc-plugin`, linking against the SDK's import libraries) -- the
`vlc-listenbrainz-plugin` repo's Windows/MSYS2 Makefile is a directly
reusable template for the build rules themselves.

---

## Part 4: Testing & Validation Strategy

The existing Python reference implementation (`batch_extract_skp.py`'s
parsing functions) is a **validated, byte-exact ground-truth oracle** --
every header offset, boundary-detection rule, and edge case it handles was
confirmed empirically against real files during the original investigation.
The C port should be tested by cross-checking its parsed offsets against
the Python implementation's output for the same sample files, not
re-derived from scratch -- this avoids re-discovering already-solved bugs
(like the "walks through a second file's boundary" bug fixed earlier).

Recommended test corpus: reuse the same small (~10-50 file) sample set
already used to validate the Python toolkit, covering H.264/1080p,
HEVC/4K, 0/1/2-track files, and the two known padding-variance cases.

---

## Part 5: Risk Register / Open Questions for Phase 1

| Question | Why it matters | How to resolve |
|---|---|---|
| Is Architecture A's child-demuxer spawning API stable/public across VLC 3.x point releases? | Determines long-term maintenance burden | Read `src/input/demux.c` + changelog history in Phase 1 |
| Does seeking work cleanly across 3 aggregated child demuxers? | Core UX expectation (scrubbing the timeline) | Explicit test in Phase 4, may need custom `pf_control` logic to keep children in sync |
| VLC 3.x vs 4.x targeting | Affects which SDK/ABI to build against | Decide based on which VLC version you'll actually run day to day |
| Windows-only, or also macOS/Linux? | Affects toolchain scope | Given current usage is Windows-primary, scope Phase 0-4 to Windows first; the format-parsing logic itself is portable C, so cross-platform later is mostly a build-system exercise |

---

## Part 6: Effort Assessment

Given the format is fully solved and VLC's own codec/container support is
being reused rather than reimplemented, this is a genuinely scoped project
-- realistically a few hundred to low-thousand lines of C for the plugin
itself, concentrated mostly in Phases 1-3. The riskiest unknown is the
exact child-demuxer aggregation mechanics (Architecture A's core), which is
why Phase 1 is scoped specifically to resolve that before any format-parsing
code is written -- cheaper to discover an architectural dead-end early than
after Phase 3.

## Recommended Immediate Next Step

Start with **Phase 0** (toolchain validation) -- it's low-effort, has a
clear pass/fail exit criterion, and de-risks everything downstream by
confirming the build/deploy pipeline works in your actual environment
before any real engineering investment begins.
