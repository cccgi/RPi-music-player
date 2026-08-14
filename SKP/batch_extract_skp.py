#!/usr/bin/env python3
"""
batch_extract_skp.py -- reconstruct HomeKara .skp files (and standard DVD
.vob rips) into playable .mkv files with karaoke/backing audio first,
original vocal second.

.skp format (fully reverse-engineered):
    [8 bytes]  "HomeKara" magic
    [24 bytes] header fields (offset 0x18 = end offset of JPEG thumbnail)
    [1 JPEG]   thumbnail image
    [1 MKV]    video-only Matroska file (H.264 or HEVC)
    [1+ M4A]   one or more complete, independently concatenated M4A files
               (0, 1, 2, or more -- auto-detected)

.vob format: standard DVD MPEG-PS container, already has 2 real audio
streams -- no custom parsing needed, just probed and remuxed directly.

Usage:
    # Process a whole folder
    python3 batch_extract_skp.py <input_folder> <output_folder> [options]

    # Process a single file
    python3 batch_extract_skp.py "/path/to/one/song.skp" <output_folder> [options]

    # Fuzzy-match a CSV list of (song, singer) against a search root and
    # process only the matches found
    python3 batch_extract_skp.py "<search_root>" <output_folder> \\
        --csv-list songs.csv [options]

Options:
    --swap              Swap track order for .skp files (use if audio
                         file #1 turns out to be vocal instead of backing
                         -- confirm from a manual test first!)
    --vob-swap          Same, but for .vob DVD rips (independent flag --
                         DVD stream order may differ from .skp convention;
                         unconfirmed until tested on a real sample)
    --limit N           Only process the first N files found (for testing)
    --overwrite         Reprocess files even if output already exists
                         (default: skip existing outputs, resumable)
    --delete-source     Delete the original file after successful,
                         verified extraction (use once you trust the
                         pipeline -- saves needing 2x storage)
    --dry-run           Do everything except delete source files, even
                         if --delete-source is passed (safety check mode)
    --csv-list PATH     CSV with columns: song,singer (singer optional/
                         can be blank per row). Fuzzy-matches each row
                         against every .skp/.vob file found under the
                         input_folder path (used as search root in this
                         mode), tolerating typos and missing accents.
    --match-threshold F Minimum fuzzy match score (0-1) to auto-process
                         a CSV row's best match. Default 0.72. Matches
                         below this are reported but not processed.

Requires ffmpeg/ffprobe on PATH.
"""

import sys
import os
import struct
import subprocess
import csv
import time
import argparse
import tempfile
import shutil
import difflib
import unicodedata
import re
import datetime
import calendar


# ---------- low-level format parsing (same logic validated earlier) ----------

def read_vint_size(data, pos):
    first = data[pos]
    if first == 0:
        raise ValueError("invalid EBML vint (leading byte is 0)")
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            raise ValueError("invalid EBML vint (too long)")
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, length


def find_mkv_segment_end(data, start):
    pos = start
    if data[pos:pos+4] != b"\x1a\x45\xdf\xa3":
        raise ValueError(f"No EBML header magic at offset {pos}")
    pos += 4
    ebml_size, n = read_vint_size(data, pos)
    pos += n + ebml_size

    if data[pos:pos+4] != b"\x18\x53\x80\x67":
        raise ValueError(f"No Segment element at offset {pos}")
    pos += 4
    seg_size, n2 = read_vint_size(data, pos)
    pos += n2
    return pos + seg_size  # segment content end


def find_ftyp_validated(data, near_offset, slack=2048):
    search_start = max(0, near_offset - slack)
    search_end = min(len(data), near_offset + slack)
    region = data[search_start:search_end]
    idx = 0
    while True:
        rel = region.find(b"ftyp", idx)
        if rel == -1:
            return None
        abs_idx = search_start + rel
        box_start = abs_idx - 4
        if box_start >= 0:
            box_size = int.from_bytes(data[box_start:box_start+4], "big")
            if 8 <= box_size <= 64:
                return box_start
        idx = rel + 1


def parse_mp4_end(data, start):
    """Walk top-level MP4 boxes from `start`, return offset where this
    container's own declared content ends. Critically: a 'ftyp' box
    should only ever legitimately appear as the FIRST box of a container.
    If we encounter a second one mid-walk, that means a new concatenated
    file has begun right there -- stop instead of walking through it."""
    pos = start
    n = len(data)
    known_types = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"udta", b"wide", b"pnot"}
    while pos + 8 <= n:
        size = int.from_bytes(data[pos:pos+4], "big")
        box_type = data[pos+4:pos+8]
        if box_type == b"ftyp" and pos != start:
            break
        if box_type not in known_types and not box_type.isalnum():
            break
        if size == 1:
            if pos + 16 > n:
                break
            box_size = int.from_bytes(data[pos+8:pos+16], "big")
        elif size == 0:
            box_size = n - pos
        else:
            box_size = size
        if box_size <= 0:
            break
        pos += box_size
    return pos


def parse_skp(path):
    """Returns dict with video_bytes and a LIST of audio track byte
    blobs (however many are actually embedded -- 0, 1, 2, or more)."""
    with open(path, "rb") as f:
        data = f.read()

    total_size = len(data)
    on_disk_size = os.path.getsize(path)
    if total_size < 32 or total_size != on_disk_size:
        raise ValueError(
            f"File could not be read properly: expected {on_disk_size} bytes "
            f"on disk, only read {total_size} bytes. This usually means the "
            f"file is corrupted/incomplete, or (if on a cloud-sync drive like "
            f"OneDrive/Dropbox) not fully downloaded yet -- try opening it "
            f"once in a media player to force it to sync, then re-run.")

    header_end = struct.unpack_from("<I", data, 0x18)[0]

    seg_end = find_mkv_segment_end(data, header_end)
    first_audio_start = find_ftyp_validated(data, seg_end)
    if first_audio_start is None:
        video_bytes = data[header_end:]
        return {
            "total_size": total_size, "video_bytes": video_bytes,
            "audio_tracks": [], "unaccounted_bytes": 0, "trak_count_single": None,
        }

    video_bytes = data[header_end:first_audio_start]

    audio_tracks = []
    cursor = first_audio_start
    while True:
        end = parse_mp4_end(data, cursor)
        audio_tracks.append(data[cursor:end])
        leftover = total_size - end
        if leftover <= 100:  # trivial padding -- genuinely done
            unaccounted = leftover
            break
        next_start = find_ftyp_validated(data, end, slack=2048)
        if next_start is None:
            unaccounted = leftover  # leftover data we can't identify as another file
            break
        cursor = next_start

    trak_count_single = None
    if len(audio_tracks) == 1:
        trak_count_single = count_trak_atoms(audio_tracks[0], 0, len(audio_tracks[0]))

    return {
        "total_size": total_size,
        "video_bytes": video_bytes,
        "audio_tracks": audio_tracks,
        "unaccounted_bytes": unaccounted,
        "trak_count_single": trak_count_single,
    }



def find_ftyp_in_window(window, window_abs_start):
    idx = 0
    while True:
        rel = window.find(b"ftyp", idx)
        if rel == -1:
            return None
        abs_idx = window_abs_start + rel
        box_start = abs_idx - 4
        if box_start >= window_abs_start:
            box_size_bytes = window[rel-4:rel] if rel >= 4 else None
            if box_size_bytes and len(box_size_bytes) == 4:
                box_size = int.from_bytes(box_size_bytes, "big")
                if 8 <= box_size <= 64:
                    return box_start
        idx = rel + 1
    return None


def walk_mp4_boxes_seek(f, start, total_size):
    """Same box-walking logic as parse_mp4_end, but reads only small
    headers via seeks instead of loading whole box contents -- fast
    and memory-light for scanning thousands of large files. Stops at
    a second 'ftyp' box (signals a new concatenated file has begun)."""
    pos = start
    known_types = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"udta", b"wide", b"pnot"}
    while pos + 8 <= total_size:
        f.seek(pos)
        head = f.read(16)
        if len(head) < 8:
            break
        size = int.from_bytes(head[0:4], "big")
        box_type = head[4:8]
        if box_type == b"ftyp" and pos != start:
            break
        if box_type not in known_types and not box_type.isalnum():
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


def scan_skp_fast(path):
    """Read-only, memory-light scan: determine track count and byte
    accounting for a .skp file without extracting anything."""
    total_size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(32)
        if len(head) < 32:
            raise ValueError(
                f"File could not be read properly: expected at least 32 header "
                f"bytes, only read {len(head)} (file reports {total_size} bytes "
                f"on disk). Likely corrupted/incomplete, or an un-synced cloud "
                f"placeholder file.")
        if head[:8] != b"HomeKara":
            raise ValueError("Missing HomeKara magic header")
        header_end = struct.unpack_from("<I", head, 0x18)[0]

        f.seek(header_end)
        chunk = f.read(64)
        if chunk[0:4] != b"\x1a\x45\xdf\xa3":
            raise ValueError("No EBML header magic at expected offset")
        pos = 4
        ebml_size, n = read_vint_size(chunk, pos)
        pos += n
        ebml_content_end_abs = header_end + pos + ebml_size

        f.seek(ebml_content_end_abs)
        seg_chunk = f.read(32)
        if seg_chunk[0:4] != b"\x18\x53\x80\x67":
            raise ValueError("No Segment element at expected offset")
        pos2 = 4
        seg_size, n2 = read_vint_size(seg_chunk, pos2)
        pos2 += n2
        segment_content_end_abs = ebml_content_end_abs + pos2 + seg_size

        slack = 1024
        window_start = max(0, segment_content_end_abs - slack)
        f.seek(window_start)
        window = f.read(slack * 2)
        first_audio_start = find_ftyp_in_window(window, window_start)
        if first_audio_start is None:
            return {"status": "OK", "total_size": total_size, "track_count": 0,
                    "unaccounted_bytes": total_size - segment_content_end_abs,
                    "video_bytes_approx": segment_content_end_abs - header_end,
                    "trak_count_single": None}

        track_count = 0
        cursor = first_audio_start
        last_end = None
        while True:
            end = walk_mp4_boxes_seek(f, cursor, total_size)
            track_count += 1
            last_end = end
            leftover = total_size - end
            if leftover <= 100:
                unaccounted = leftover
                break
            slack2 = 2048
            f.seek(end)
            window2 = f.read(slack2 * 2)
            next_start = find_ftyp_in_window(window2, end)
            if next_start is None:
                unaccounted = leftover
                break
            cursor = next_start

        trak_count_single = None
        if track_count == 1:
            f.seek(first_audio_start)
            audio1_data = f.read(last_end - first_audio_start)
            trak_count_single = count_trak_atoms(audio1_data, 0, len(audio1_data))

    return {
        "status": "OK",
        "total_size": total_size,
        "track_count": track_count,
        "unaccounted_bytes": unaccounted,
        "video_bytes_approx": segment_content_end_abs - header_end,
        "trak_count_single": trak_count_single,
    }


def count_trak_atoms(data, start, end):
    """Cheap heuristic: count validated 'trak' box occurrences within a
    byte range, to detect a single MP4 file that internally contains
    multiple tracks in one moov (as opposed to two concatenated files)."""
    count = 0
    idx = start
    region = data[start:end]
    pos = 0
    while True:
        rel = region.find(b"trak", pos)
        if rel == -1:
            break
        abs_box_start = start + rel - 4
        if abs_box_start >= start:
            box_size = int.from_bytes(data[abs_box_start:abs_box_start+4], "big")
            if 8 <= box_size <= (end - abs_box_start):
                count += 1
        pos = rel + 1
    return count


# ---------- fuzzy filename matching (CSV-list mode) ----------

def normalize_for_match(s):
    """Lowercase, strip accents, collapse to alphanumeric+space, for
    tolerant fuzzy comparison (handles typos and missing diacritics)."""
    if s is None:
        return ""
    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def split_song_singer(basename):
    """Filenames follow 'song name - singer name.ext'. Split on the
    FIRST ' - ' occurrence."""
    if " - " in basename:
        song, singer = basename.split(" - ", 1)
        return song.strip(), singer.strip()
    return basename.strip(), ""


def build_media_index(search_root):
    """Recursively index every .skp/.vob file under search_root once,
    so we don't re-walk the filesystem per CSV row. Also builds an exact
    filename lookup dict for direct (non-fuzzy) matches."""
    index = []
    exact_by_filename = {}
    for root, _, files in os.walk(search_root):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in (".skp", ".vob"):
                path = os.path.join(root, fn)
                base = os.path.splitext(fn)[0]
                song_part, singer_part = split_song_singer(base)
                index.append({
                    "path": path,
                    "song_norm": normalize_for_match(song_part),
                    "singer_norm": normalize_for_match(singer_part),
                    "ext": ext,
                })
                exact_by_filename.setdefault(fn.lower(), []).append(path)
    return index, exact_by_filename


def find_exact_filename_match(raw_query, exact_by_filename):
    """If raw_query is already an exact (or case-insensitive-exact)
    filename, look it up directly -- no fuzzy scoring needed or wanted,
    since an exact filename match is unambiguous. Returns (path, ambiguous)
    or (None, False) if not found."""
    key = raw_query.strip().lower()
    matches = exact_by_filename.get(key)
    if not matches:
        return None, False
    if len(matches) > 1:
        return matches[0], True  # multiple files share this exact name -- flagged, first used
    return matches[0], False


def find_best_match(song_query, singer_query, index):
    song_q = normalize_for_match(song_query)
    singer_q = normalize_for_match(singer_query) if singer_query else ""

    best = None
    best_score = -1.0
    for entry in index:
        song_score = difflib.SequenceMatcher(None, song_q, entry["song_norm"]).ratio()
        if singer_q and entry["singer_norm"]:
            # Both sides have singer info -- use the real combined score.
            # (Do NOT fall back to song-only here: many songs have several
            # different singers' versions with an identical title, which
            # would make song_score alone tie at 1.0 across all of them --
            # singer comparison is exactly what breaks that tie correctly.)
            singer_score = difflib.SequenceMatcher(None, singer_q, entry["singer_norm"]).ratio()
            score = 0.6 * song_score + 0.4 * singer_score
        else:
            # No singer info on one side or the other -- song-only is the
            # best we can do.
            score = song_score
        if score > best_score:
            best_score = score
            best = entry

    return best, best_score


# ---------- .vob (standard DVD rip) handling ----------

def process_vob(vob_path, out_path, vob_swap, container="mkv", default_track="vocal"):
    """VOB is a standard MPEG-PS container with 2 real audio streams
    already -- no custom byte parsing needed, just probe and remux."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type",
         "-of", "csv=p=0", vob_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if probe.returncode != 0:
        raise RuntimeError(f"ffprobe failed on VOB: {probe.stderr[-500:]}")

    audio_indices = []
    video_index = None
    for line in probe.stdout.strip().splitlines():
        parts = line.split(",")
        if len(parts) != 2:
            continue
        idx, codec_type = parts
        if codec_type == "video" and video_index is None:
            video_index = idx
        elif codec_type == "audio":
            audio_indices.append(idx)

    if video_index is None:
        raise RuntimeError("No video stream found in VOB")

    track_count = len(audio_indices)
    cmd = ["ffmpeg", "-y", "-i", vob_path, "-map", f"0:{video_index}"]

    titles = []
    if track_count == 2:
        # Default: keep native stream order (stream 0 = vocal, stream 1 =
        # backing, matching the .skp convention now that it's also
        # native-order-by-default). --vob-swap reorders to karaoke-first
        # if a real sample turns out reversed once tested.
        if not vob_swap:
            order_indices = [audio_indices[0], audio_indices[1]]
            titles = ["Original (Vocal)", "Karaoke (Backing Track)"]
        else:
            order_indices = [audio_indices[1], audio_indices[0]]
            titles = ["Karaoke (Backing Track)", "Original (Vocal)"]
        for ai in order_indices:
            cmd += ["-map", f"0:{ai}"]
        for i, title in enumerate(titles):
            cmd += [f"-metadata:s:a:{i}", f"title={title}"]
    else:
        for ai in audio_indices:
            cmd += ["-map", f"0:{ai}"]

    cmd += ["-c", "copy"]

    if container == "mp4":
        cmd += ["-movflags", "+faststart"]
        for i, title in enumerate(titles):
            is_default_track = (
                (default_track == "karaoke" and "Karaoke" in title) or
                (default_track == "vocal" and "Vocal" in title)
            )
            cmd += [f"-disposition:a:{i}", "default" if is_default_track else "0"]

    cmd += [out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg VOB remux failed: {proc.stderr[-800:]}")

    preserve_timestamps(vob_path, out_path)

    return {"track_count": track_count, "output_size": os.path.getsize(out_path),
            "unaccounted_bytes": 0, "total_size": os.path.getsize(vob_path),
            "trak_count_single": None}


# ---------- per-file processing ----------

def build_mux_command(inputs_with_titles, video_tmp, out_path, container, default_track):
    """inputs_with_titles: list of (audio_file_path, title_string) in the
    order they'll appear as audio streams 0,1,2...
    container: 'mkv' or 'mp4'
    default_track: 'karaoke' or 'vocal' -- only relevant for mp4, controls
    which stream gets the disposition flag Photos/AVPlayer will honor.
    """
    cmd = ["ffmpeg", "-y", "-i", video_tmp]
    for path, _ in inputs_with_titles:
        cmd += ["-i", path]
    cmd += ["-map", "0:v"]
    for i in range(len(inputs_with_titles)):
        cmd += ["-map", f"{i+1}:a"]
    cmd += ["-c", "copy"]
    for i, (_, title) in enumerate(inputs_with_titles):
        cmd += [f"-metadata:s:a:{i}", f"title={title}"]

    if container == "mp4":
        cmd += ["-movflags", "+faststart"]
        # Set disposition: exactly one track marked default (what Photos
        # will play), others explicitly non-default (still present and
        # selectable in apps like VLC-iOS/Infuse that expose track switching)
        for i, (_, title) in enumerate(inputs_with_titles):
            is_default_track = (
                (default_track == "karaoke" and "Karaoke" in title) or
                (default_track == "vocal" and "Vocal" in title)
            )
            cmd += [f"-disposition:a:{i}", "default" if is_default_track else "0"]

    cmd += [out_path]
    return cmd


def process_file(path, out_path, swap, vob_swap, tmpdir, container="mkv", default_track="vocal"):
    """Dispatch based on file extension -- .skp uses our custom parser,
    .vob uses standard ffprobe/ffmpeg since it's already a normal DVD
    container with real audio tracks."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".vob":
        return process_vob(path, out_path, vob_swap, container, default_track)
    return process_skp(path, out_path, swap, tmpdir, container, default_track)


def process_skp(skp_path, out_path, swap, tmpdir, container="mkv", default_track="vocal"):
    parsed = parse_skp(skp_path)

    video_tmp = os.path.join(tmpdir, "video.mkv")
    with open(video_tmp, "wb") as f:
        f.write(parsed["video_bytes"])

    audio_tracks = parsed["audio_tracks"]
    track_count = len(audio_tracks)

    audio_paths = []
    for i, blob in enumerate(audio_tracks):
        p = os.path.join(tmpdir, f"audio{i+1}.m4a")
        with open(p, "wb") as f:
            f.write(blob)
        audio_paths.append(p)

    if track_count == 2:
        # Confirmed via listening test: audio file #1 (first embedded) = vocal,
        # audio file #2 (second embedded) = karaoke/backing track.
        # Default: keep this exact native order (matches what the original
        # SKPlayer Android app expects by stream index).
        # --swap reorders to karaoke-first/vocal-second instead (was the old
        # default, useful for VLC/casual listening where karaoke-first is
        # more intuitive -- use swap_audio_tracks.py to convert existing
        # files between the two conventions instead of re-extracting).
        if not swap:
            order = [(audio_paths[0], "Original (Vocal)"),
                     (audio_paths[1], "Karaoke (Backing Track)")]
        else:
            order = [(audio_paths[1], "Karaoke (Backing Track)"),
                     (audio_paths[0], "Original (Vocal)")]
    elif track_count == 1:
        order = [(audio_paths[0], "Audio")]
    elif track_count == 0:
        order = []
    else:
        # 3+ tracks found -- unexpected/unconfirmed scenario. Preserve
        # original file order and label generically rather than guessing
        # at semantics; flagged in the report for manual review.
        order = [(p, f"Audio Track {i+1}") for i, p in enumerate(audio_paths)]

    cmd = build_mux_command(order, video_tmp, out_path, container, default_track)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg remux failed: {proc.stderr[-800:]}")

    preserve_timestamps(skp_path, out_path)

    return {
        "track_count": track_count,
        "unaccounted_bytes": parsed["unaccounted_bytes"],
        "total_size": parsed["total_size"],
        "output_size": os.path.getsize(out_path),
        "trak_count_single": parsed.get("trak_count_single"),
    }


def preserve_timestamps(src_path, dest_path):
    """Copy modification/access time from src to dest, and on Windows also
    copy the creation time -- so date-based filtering (--date-source mtime
    or ctime) and general file history stay accurate on converted output,
    not just the original .skp/.vob files. Best-effort: never raises."""
    try:
        st = os.stat(src_path)
        os.utime(dest_path, (st.st_atime, st.st_mtime))
    except OSError:
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            def to_filetime(unix_time):
                # Windows FILETIME: 100-ns intervals since 1601-01-01
                return int((unix_time * 10_000_000) + 116444736000000000)

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

            def make_filetime(val):
                return FILETIME(val & 0xFFFFFFFF, (val >> 32) & 0xFFFFFFFF)

            GENERIC_WRITE = 0x40000000
            FILE_SHARE_WRITE = 0x00000002
            OPEN_EXISTING = 3
            FILE_ATTRIBUTE_NORMAL = 0x80

            handle = ctypes.windll.kernel32.CreateFileW(
                dest_path, GENERIC_WRITE, FILE_SHARE_WRITE, None,
                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
            )
            if handle and handle != -1:
                creation_ft = make_filetime(to_filetime(st.st_ctime))  # st_ctime IS creation time on Windows
                atime_ft = make_filetime(to_filetime(st.st_atime))
                mtime_ft = make_filetime(to_filetime(st.st_mtime))
                ctypes.windll.kernel32.SetFileTime(
                    handle, ctypes.byref(creation_ft), ctypes.byref(atime_ft), ctypes.byref(mtime_ft)
                )
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass  # best-effort only -- mtime/atime above are already preserved regardless

    return True


def check_disk_space(output_folder, needed_bytes, on_low_space, poll_interval=30, safety_factor=1.15):
    """Returns True if it's OK to proceed, False if the caller should stop
    (only happens in 'exit' mode). In 'pause' mode, blocks and polls until
    enough space is free, then returns True -- so the run can continue
    unattended once the issue is resolved."""
    required = int(needed_bytes * safety_factor)
    while True:
        free = shutil.disk_usage(output_folder).free
        if free >= required:
            return True
        msg = (f"\n  LOW DISK SPACE on output destination: "
               f"{free/1e9:.2f} GB free, need ~{required/1e9:.2f} GB for this file.")
        if on_low_space == "exit":
            print(msg)
            print("  Stopping here. Free up space and re-run the same command --")
            print("  already-completed files are skipped automatically (resumable).")
            return False
        else:  # pause
            print(msg)
            print(f"  Waiting ({poll_interval}s poll) -- free up space on the "
                  f"destination drive and this will continue automatically.")
            time.sleep(poll_interval)


def parse_date_range(month_arg, date_from_arg, date_to_arg):
    """Returns (date_from, date_to) as date objects (inclusive), or (None, None)
    if no date filtering was requested."""
    if month_arg:
        year, mon = (int(x) for x in month_arg.split("-"))
        date_from = datetime.date(year, mon, 1)
        last_day = calendar.monthrange(year, mon)[1]
        date_to = datetime.date(year, mon, last_day)
        return date_from, date_to
    if date_from_arg or date_to_arg:
        date_from = datetime.date.fromisoformat(date_from_arg) if date_from_arg else datetime.date.min
        date_to = datetime.date.fromisoformat(date_to_arg) if date_to_arg else datetime.date.max
        return date_from, date_to
    return None, None


FOLDER_DATE_PATTERN = re.compile(r"(\d{2})-(\d{4})")  # matches "06-2025" style folder names


def get_folder_name_date(path):
    """Search path components for an 'MM-YYYY' folder name (the convention
    already used in this library, e.g. 'NhacMoi/06-2025/'). Returns a date
    (first of that month) or None if no such component is found."""
    for part in os.path.normpath(path).split(os.sep):
        m = FOLDER_DATE_PATTERN.fullmatch(part)
        if m:
            mon, year = int(m.group(1)), int(m.group(2))
            if 1 <= mon <= 12:
                return datetime.date(year, mon, 1)
    return None


def file_matches_date_range(path, date_from, date_to, date_source):
    if date_source == "folder-name":
        d = get_folder_name_date(path)
        if d is None:
            return False  # no MM-YYYY folder found in path -- can't confirm, exclude rather than guess
        return date_from <= d <= date_to
    else:
        ts = os.path.getmtime(path) if date_source == "mtime" else os.path.getctime(path)
        d = datetime.date.fromtimestamp(ts)
        return date_from <= d <= date_to


def gather_files_for_normal_mode(input_path, limit, date_from=None, date_to=None, date_source="mtime"):
    """Single file, or recursive folder walk for .skp/.vob files, optionally
    filtered to a date range (by file mtime/ctime, or by an 'MM-YYYY'
    folder-name component already used in this library's structure)."""
    if os.path.isfile(input_path):
        return [input_path]
    files = []
    for root, _, fns in os.walk(input_path):
        for fn in fns:
            if fn.lower().endswith((".skp", ".vob")):
                files.append(os.path.join(root, fn))
    files.sort()

    if date_from is not None:
        before_count = len(files)
        files = [f for f in files if file_matches_date_range(f, date_from, date_to, date_source)]
        print(f"Date filter ({date_source}, {date_from} to {date_to}): "
              f"{len(files)}/{before_count} files matched.")

    if limit:
        files = files[:limit]
    return files


def run_extraction(files, base_dir, output_folder, args):
    """Shared extraction loop used by normal mode and CSV mode.
    base_dir is used to compute relative output paths (preserving
    folder structure); pass os.path.dirname(f) per-file for flat CSV mode."""
    os.makedirs(output_folder, exist_ok=True)
    report_path = os.path.join(output_folder, "_batch_report.csv")

    if args.scan_only:
        print("Running in --scan-only mode: no media files will be written.\n")
        results = []
        for i, path in enumerate(files, 1):
            rel = os.path.relpath(path, base_dir)
            row = {"file": rel}
            try:
                if path.lower().endswith(".vob"):
                    row["status"] = "skipped_scan_not_supported_for_vob"
                else:
                    stats = scan_skp_fast(path)
                    row.update(stats)
            except Exception as e:
                row["status"] = f"FAILED: {e}"
            results.append(row)
            if i % 25 == 0 or i == len(files):
                print(f"[{i}/{len(files)}] scanned...")
                fieldnames = sorted({k for r in results for k in r.keys()})
                with open(report_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(results)
        from collections import Counter
        track_counts = Counter(r.get("track_count") for r in results if "track_count" in r)
        failed = sum(1 for r in results if str(r.get("status", "")).startswith("FAILED"))
        print(f"\nScan complete. Report: {report_path}")
        print(f"Track count distribution: {dict(track_counts)}")
        print(f"Failed to parse: {failed}")
        return results

    results = []
    for i, path in enumerate(files, 1):
        rel = os.path.relpath(path, base_dir)
        rel_out = os.path.splitext(rel)[0] + f".{args.container}"
        out_path = os.path.join(output_folder, rel_out)

        print(f"[{i}/{len(files)}] {rel}")

        if os.path.exists(out_path) and not args.overwrite:
            print("    -> skipped (already exists)")
            results.append({"file": rel, "status": "skipped_existing"})
            continue

        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        needed_estimate = os.path.getsize(path)  # output is always <= input size
        if not check_disk_space(output_folder, needed_estimate, args.on_low_space):
            print(f"\nStopped at file {i}/{len(files)} due to low disk space. "
                  f"Report so far written to {os.path.join(output_folder, '_batch_report.csv')}")
            break

        row = {"file": rel}
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                stats = process_file(path, out_path, args.swap, args.vob_swap, tmpdir,
                                      args.container, args.iphone_default_track)
                row.update(stats)
                tc = stats["track_count"]
                if tc == 2:
                    row["status"] = "OK"
                elif tc > 2:
                    row["status"] = f"OK_but_UNUSUAL_{tc}_audio_tracks_review_needed"
                else:
                    row["status"] = f"OK_but_only_{tc}_audio_tracks"
                print(f"    -> {row['status']} "
                      f"(tracks={stats['track_count']}, "
                      f"unaccounted_bytes={stats.get('unaccounted_bytes')})")

                if args.delete_source and not args.dry_run:
                    os.remove(path)
                    print("    -> source deleted")
                elif args.delete_source and args.dry_run:
                    print("    -> (dry-run: would delete source)")

            except Exception as e:
                row["status"] = f"FAILED: {e}"
                print(f"    -> FAILED: {e}")
                if os.path.exists(out_path):
                    os.remove(out_path)

        results.append(row)
        fieldnames = sorted({k for r in results for k in r.keys()})
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

    ok = sum(1 for r in results if str(r["status"]).startswith("OK"))
    failed = sum(1 for r in results if str(r["status"]).startswith("FAILED"))
    skipped = sum(1 for r in results if r["status"] == "skipped_existing")
    print(f"\nDone. {ok} OK, {failed} failed, {skipped} skipped. Report: {report_path}")
    return results


def looks_like_filename_or_combined(song):
    """Heuristic: does this string look like a whole filename or a
    'Song - Singer' combined string, rather than a bare song title?"""
    lower = song.lower()
    has_known_ext = lower.endswith((".skp", ".vob", ".mkv", ".mp4"))
    has_separator = " - " in song
    return has_known_ext or has_separator


def split_query_row(song, singer):
    """If singer is blank and the song field looks like a full filename or
    a combined 'Song - Singer[.ext]' string, split it the same way real
    library filenames are split (strip known extension, then split on the
    first ' - '). This handles input lists that are just plain filenames
    per line rather than proper song,singer CSV columns -- otherwise the
    singer name (and file extension) end up glued onto the song query,
    dragging every match score down even when the right file is found."""
    if singer:
        return song, singer  # singer already provided -- nothing to fix
    if not looks_like_filename_or_combined(song):
        return song, singer  # bare title, no separator -- nothing to split

    base = song
    for ext in (".skp", ".vob", ".mkv", ".mp4"):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    split_song, split_singer = split_song_singer(base)
    if split_singer:
        return split_song, split_singer
    return song, singer


def run_csv_mode(search_root, output_folder, args):
    print(f"Indexing all .skp/.vob files under: {search_root}\n(this may take a moment for a large library)")
    index, exact_by_filename = build_media_index(search_root)
    print(f"Indexed {len(index)} candidate files.\n")

    with open(args.csv_list, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if r and r[0].strip()]

    # tolerate an optional header row
    if rows and rows[0][0].strip().lower() in ("song", "song name", "title"):
        rows = rows[1:]

    match_results = []
    to_process = []
    for row in rows:
        raw_song = row[0].strip() if len(row) > 0 else ""
        raw_singer = row[1].strip() if len(row) > 1 else ""

        # Try an exact filename match first -- if this row is already a
        # correct, complete filename (as in a plain list exported from the
        # library itself), this is unambiguous and skips fuzzy scoring
        # entirely, avoiding any risk of a near-miss picking the wrong file.
        exact_path = None
        ambiguous = False
        if not raw_singer and looks_like_filename_or_combined(raw_song):
            exact_path, ambiguous = find_exact_filename_match(raw_song, exact_by_filename)

        if exact_path:
            entry = {
                "requested_song": raw_song, "requested_singer": raw_singer,
                "raw_input": "",
                "matched_file": os.path.relpath(exact_path, search_root),
                "score": 1.0,
                "match_status": "exact_filename_match" if not ambiguous else
                                "exact_filename_match_AMBIGUOUS_multiple_files",
            }
            to_process.append(exact_path)
        else:
            song, singer = split_query_row(raw_song, raw_singer)
            best, score = find_best_match(song, singer, index)
            entry = {
                "requested_song": song, "requested_singer": singer,
                "raw_input": raw_song if song != raw_song or singer != raw_singer else "",
                "matched_file": os.path.relpath(best["path"], search_root) if best else None,
                "score": round(score, 3) if best else 0,
            }
            if best and score >= args.match_threshold:
                entry["match_status"] = "will_process"
                to_process.append(best["path"])
            elif best:
                entry["match_status"] = "low_confidence_skipped"
            else:
                entry["match_status"] = "no_candidates_found"

        match_results.append(entry)
        print(f"  '{entry['requested_song']}' / '{entry['requested_singer']}' -> "
              f"{entry['match_status']} (score={entry['score']}) {entry['matched_file'] or ''}")

    os.makedirs(output_folder, exist_ok=True)
    match_report_path = os.path.join(output_folder, "_csv_match_report.csv")
    with open(match_report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["raw_input", "requested_song", "requested_singer",
                                                "matched_file", "score", "match_status"])
        writer.writeheader()
        writer.writerows(match_results)
    print(f"\nMatch report written: {match_report_path}")
    print(f"{len(to_process)}/{len(rows)} rows matched above threshold and will be processed.\n")

    if to_process:
        run_extraction(to_process, search_root, output_folder, args)


def main():
    ap = argparse.ArgumentParser(description="Reconstruct .skp/.vob karaoke files into playable .mkv files")
    ap.add_argument("input_folder", help="A folder to walk, a single file to process, "
                                          "or (with --csv-list) the search root to match against")
    ap.add_argument("output_folder")
    ap.add_argument("--swap", action="store_true",
                     help="Swap which embedded audio file is labeled karaoke vs vocal (.skp files)")
    ap.add_argument("--vob-swap", action="store_true",
                     help="Same, but for .vob DVD rips (independent of --swap)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--delete-source", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scan-only", action="store_true",
                     help="Only scan and report track counts/byte accounting to CSV -- "
                          "does NOT extract, mux, or write any media files.")
    ap.add_argument("--csv-list", default=None,
                     help="CSV file with columns: song,singer. Fuzzy-matches each row "
                          "against every .skp/.vob file under input_folder.")
    ap.add_argument("--match-threshold", type=float, default=0.72,
                     help="Minimum fuzzy match score (0-1) to auto-process a CSV row (default 0.72)")
    ap.add_argument("--container", choices=["mkv", "mp4"], default="mkv",
                     help="Output container. 'mp4' is iPhone/Photos-app compatible "
                          "(both tracks embedded for apps like VLC-iOS/Infuse that "
                          "support track switching; one marked default for Photos, "
                          "which has no track-switching UI at all). Default: mkv.")
    ap.add_argument("--iphone-default-track", choices=["vocal", "karaoke"], default="vocal",
                     help="Which track plays by default in Photos/AVPlayer when "
                          "--container mp4 is used (default: vocal)")
    ap.add_argument("--on-low-space", choices=["exit", "pause"], default="exit",
                     help="What to do when the output drive runs low on space: "
                          "'exit' stops cleanly (just re-run the same command "
                          "later -- completed files are skipped automatically); "
                          "'pause' waits and polls, resuming automatically once "
                          "space is freed up, no need to re-run anything.")
    ap.add_argument("--month", default=None,
                     help="Only process files dated within this calendar month, "
                          "format YYYY-MM (e.g. 2026-01 for January 2026). "
                          "Shorthand for --date-from/--date-to covering the whole month.")
    ap.add_argument("--date-from", default=None,
                     help="Only process files dated on/after this date, format YYYY-MM-DD")
    ap.add_argument("--date-to", default=None,
                     help="Only process files dated on/before this date, format YYYY-MM-DD")
    ap.add_argument("--date-source", choices=["mtime", "ctime", "folder-name"], default="mtime",
                     help="What to check the date against: 'mtime' (file modification time, "
                          "default), 'ctime' (file creation time on Windows), or 'folder-name' "
                          "(parse an 'MM-YYYY' folder name in the path, matching this library's "
                          "existing monthly folder convention -- more reliable if files have "
                          "been copied/backed up in ways that could alter timestamps)")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH.")
        sys.exit(1)

    if args.csv_list:
        run_csv_mode(args.input_folder, args.output_folder, args)
        return

    date_from, date_to = parse_date_range(args.month, args.date_from, args.date_to)
    files = gather_files_for_normal_mode(args.input_folder, args.limit, date_from, date_to, args.date_source)
    base_dir = args.input_folder if os.path.isdir(args.input_folder) else os.path.dirname(args.input_folder)
    print(f"Found {len(files)} file(s) to process.\n")
    run_extraction(files, base_dir, args.output_folder, args)


if __name__ == "__main__":
    main()
