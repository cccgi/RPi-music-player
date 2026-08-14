"""`.skp` (HomeKara karaoke container) byte-offset parsing.

Ported from the reference reverse-engineering toolkit (`batch_extract_skp.py`),
seek-based variant only (`scan_skp_fast`'s approach, not the in-memory
`parse_skp` one) — files here can be large (hundreds of MB to several GB) and
the Pi has 2GB of RAM, so nothing here ever reads a whole track into memory.

Confirmed format (see docs/VIDEO-MODE.md and the uploaded format guide):

    [8 bytes]   "HomeKara" magic
    [24 bytes]  header fields (offset 0x18 = end offset of the JPEG thumbnail)
    [N bytes]   1 JPEG thumbnail
    [N bytes]   1 complete, standalone .mkv file -- video only (H.264/HEVC)
    [N bytes]   0, 1, 2 (or more) complete, standalone .m4a files, concatenated

This module only ever returns byte RANGES. Turning a range into something
mpv can open is `subfile_url()` -- verified directly against real `.skp`
files on this hardware: `lavf://subfile,,start,<N>,end,<N>,,:<path>` opens
that range as if it were its own file, with zero copying.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

MAGIC = b"HomeKara"
_EBML_HEADER = b"\x1a\x45\xdf\xa3"
_SEGMENT = b"\x18\x53\x80\x67"
_KNOWN_MP4_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"udta", b"wide", b"pnot"}


class SkpParseError(ValueError):
    """Raised for anything that doesn't look like a valid .skp file.

    Deliberately a ValueError subclass with a message aimed at a human
    debugging a bad file, not a stack trace — matches the reference tool's
    philosophy of clear, actionable parse errors over a hard crash.
    """


@dataclass(frozen=True)
class SkpOffsets:
    path: str
    total_size: int
    video: tuple[int, int]           # (start, end), video-only MKV
    audio: tuple[tuple[int, int], ...]  # 0, 1, 2, or more (start, end) pairs
    unaccounted_bytes: int


def _read_vint_size(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    if first == 0:
        raise SkpParseError("invalid EBML vint (leading byte is 0)")
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            raise SkpParseError("invalid EBML vint (too long)")
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, length


def _find_ftyp_in_window(window: bytes, window_abs_start: int) -> int | None:
    idx = 0
    while True:
        rel = window.find(b"ftyp", idx)
        if rel == -1:
            return None
        if rel >= 4:
            box_size = int.from_bytes(window[rel - 4:rel], "big")
            if 8 <= box_size <= 64:
                return window_abs_start + rel - 4
        idx = rel + 1


def _walk_mp4_boxes(handle, start: int, total_size: int) -> int:
    """Walk top-level MP4 boxes from `start`, return where this file ends.

    A `ftyp` box is only ever legitimate as the FIRST box of a file — a
    second one mid-walk means a new concatenated file has begun right there.
    This is the exact boundary bug the reference tool found and fixed; ported
    unchanged rather than re-derived.
    """
    pos = start
    while pos + 8 <= total_size:
        handle.seek(pos)
        head = handle.read(16)
        if len(head) < 8:
            break
        size = int.from_bytes(head[0:4], "big")
        box_type = head[4:8]
        if box_type == b"ftyp" and pos != start:
            break
        if box_type not in _KNOWN_MP4_BOXES and not box_type.isalnum():
            break
        if size == 1:
            if len(head) < 16:
                break
            box_size = int.from_bytes(head[8:16], "big")
        elif size == 0:
            box_size = total_size - pos
        else:
            box_size = size
        if box_size <= 0:
            break
        pos += box_size
    return pos


def parse_skp(path: str) -> SkpOffsets:
    """Read-only, memory-light: only headers/box tables are ever read.

    Raises SkpParseError with a human-readable reason on anything unexpected
    (missing magic, truncated file, malformed EBML/MP4 structure) rather than
    an opaque exception from deep inside the byte walk.
    """
    total_size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(32)
        if len(head) < 32:
            raise SkpParseError(
                f"{path}: only read {len(head)} header bytes (file reports "
                f"{total_size} on disk) — truncated, corrupted, or an "
                f"un-synced network/cloud placeholder file"
            )
        if head[:8] != MAGIC:
            raise SkpParseError(f"{path}: missing 'HomeKara' magic header")
        header_end = struct.unpack_from("<I", head, 0x18)[0]

        f.seek(header_end)
        chunk = f.read(64)
        if chunk[0:4] != _EBML_HEADER:
            raise SkpParseError(
                f"{path}: no EBML header at offset {header_end} "
                f"(computed from the file's own header field at 0x18)"
            )
        pos = 4
        ebml_size, n = _read_vint_size(chunk, pos)
        pos += n
        ebml_content_end = header_end + pos + ebml_size

        f.seek(ebml_content_end)
        seg_chunk = f.read(32)
        if seg_chunk[0:4] != _SEGMENT:
            raise SkpParseError(f"{path}: no MKV Segment element at offset {ebml_content_end}")
        pos2 = 4
        seg_size, n2 = _read_vint_size(seg_chunk, pos2)
        pos2 += n2
        video_end = ebml_content_end + pos2 + seg_size

        # Padding before the first embedded audio file's `ftyp` varies (0-16+
        # bytes seen in the reference investigation) — search a window rather
        # than assume a fixed offset.
        slack = 1024
        window_start = max(0, video_end - slack)
        f.seek(window_start)
        window = f.read(slack * 2)
        first_audio_start = _find_ftyp_in_window(window, window_start)

        if first_audio_start is None:
            return SkpOffsets(
                path=path, total_size=total_size,
                video=(header_end, video_end), audio=(),
                unaccounted_bytes=total_size - video_end,
            )

        tracks: list[tuple[int, int]] = []
        cursor = first_audio_start
        unaccounted = 0
        while True:
            end = _walk_mp4_boxes(f, cursor, total_size)
            tracks.append((cursor, end))
            leftover = total_size - end
            if leftover <= 100:  # trivial trailing padding — genuinely done
                unaccounted = leftover
                break
            f.seek(end)
            window2 = f.read(4096)
            next_start = _find_ftyp_in_window(window2, end)
            if next_start is None:
                unaccounted = leftover
                break
            cursor = next_start

    return SkpOffsets(
        path=path, total_size=total_size,
        video=(header_end, video_end), audio=tuple(tracks),
        unaccounted_bytes=unaccounted,
    )


def subfile_url(path: str, start: int, end: int) -> str:
    """A byte range of `path` as an mpv-openable URL, zero-copy.

    MUST be handed to mpv over its JSON IPC socket (`loadfile`, `audio-add`),
    never as a CLI flag value: `--audio-files=` splits on commas, which
    collides with this protocol's own comma-delimited syntax and silently
    mis-parses the URL. Confirmed on real hardware — see docs/VIDEO-MODE.md
    section 1.
    """
    return f"lavf://subfile,,start,{start},end,{end},,:{path}"


# Track title convention, matching the reference tool exactly: track 1 =
# vocal, track 2 = backing/karaoke (confirmed there via a direct listening
# test on the native embedded order — not re-derived here).
TRACK_TITLES = ("Original (Vocal)", "Karaoke (Backing Track)")


def track_title(index: int) -> str:
    if 0 <= index < len(TRACK_TITLES):
        return TRACK_TITLES[index]
    return f"Audio Track {index + 1}"
