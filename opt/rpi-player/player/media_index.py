"""Media ingestion / metadata index — Phase 1 (media discovery) of the
media-ingestion subsystem described in this round's spec.

--------------------------------------------------------------------------
WHERE THIS FITS (read this before wiring it into anything)
--------------------------------------------------------------------------
Two very different things already exist and neither is a real database:

  * MUSIC: MPD owns the "database" today. ``ctx.mpd.update_database()``
    (actions.py's "update_database" action, bound to the touch UI's Scan
    control) tells MPD's OWN internal library scanner to walk
    music_directory and re-read tags — MPD parses FLAC/WAV tags itself and
    serves them back via ``current_song()``/``status()``, which is exactly
    where touch_daemon.py's ``_collect_state`` gets title/artist/album
    today. There has never been a second, custom index for music; MPD's
    scan span is a black box we just trigger and query.
  * VIDEO: ``VideoLibrary`` (video.py) is an in-memory, non-persistent
    Python list rebuilt from scratch on every ``rescan()`` — it walks
    ``config.video.library_dir``, matches known extensions, and derives a
    display name by SPLITTING THE FILENAME (``split_song_singer``), not by
    reading any embedded metadata. No artwork, no persistence, no
    metadata-completeness tracking at all.

Neither gives the spec's ingestion pipeline (discover -> extract embedded
metadata -> extract artwork -> enrich online if possible -> track
PENDING_METADATA -> serve to the UI) anywhere to live. This module is a
NEW, separate, persistent index — SQLite, per spec section 13/18 — that
sits ALONGSIDE MPD and VideoLibrary rather than replacing either:

  SMB copy -> files exist on storage
      |
      v
  Scan pressed -> MediaIndex.scan() (THIS MODULE, Phase 1: discovery only)
      |              walks config.delete.music_dir + config.video.library_dir,
      |              finds new/changed/removed files by path+size+mtime,
      |              writes rows to media_index.sqlite3 with status=NEW
      |              -- does NOT touch tags/artwork yet, does NOT touch
      |              MPD's own update_database() or VideoLibrary.rescan()
      |              -- both keep running exactly as they do today.
      v
  (Phase 2, NOT YET BUILT) embedded-metadata extraction for NEW/MODIFIED
  rows -> title/artist/album/etc + embedded artwork, status -> either
  METADATA_COMPLETE or PENDING_METADATA
      |
      v
  (Phase 5/6/7, NOT YET BUILT) network check -> MusicBrainz/Picard-style
  enrichment for PENDING_METADATA rows only, retried with a backoff policy
  -- see spec section 7's "do not repeatedly attempt the same failed match
  on every scan"
      |
      v
  (Phase 9, NOT YET BUILT) touch UI reads FROM THIS INDEX for
  artwork/metadata display, instead of (or in addition to) MPD's own tags

--------------------------------------------------------------------------
WHY ONLY PHASE 1 THIS ROUND
--------------------------------------------------------------------------
The full spec (network detection, MusicBrainz/Picard integration,
confidence-scored matching, safe in-place artwork writing, a retry policy
for failed enrichment, wiring the touch UI's Scan button and status
readout to it) is a genuinely large, multi-part subsystem — the spec's own
"Phase 1..9, verify playback still works after every phase" structure
already asks for incremental delivery, not one big drop. This file
implements ONLY Phase 1 (file discovery + change detection against a real
SQLite index) and is NOT YET WIRED into actions.py's "update_database" /
"video_rescan" actions or the touch UI at all — it's a standalone module,
exercised by this file's own ``_selftest()`` against a scratch directory,
not by anything that touches live playback. Wiring it into the Scan
button, then building Phase 2 (embedded metadata + artwork extraction via
``mutagen``, not yet a dependency of this project) is the natural next
increment, once this phase has been reviewed/tested on the actual Pi.

--------------------------------------------------------------------------
CHANGE DETECTION
--------------------------------------------------------------------------
Per spec section 2: "path + size + modification time and, where useful, a
content hash. Do not reprocess unchanged files unnecessarily." This module
uses (size, mtime_ns) as the cheap first-pass fingerprint — matches spec's
primary signal, and is O(1) per file (a single stat()) rather than reading
and hashing every byte of every file on every scan, which would be far too
slow for a library scan on a Pi 5 2GB. A content hash is deliberately NOT
computed here (columns exist in the schema for one — see MediaFile.content_hash
— reserved for a future rename-detection pass, per spec section 2's
"renamed files if reasonably detectable", which needs a hash to recognize
the same content at a different path; out of scope for Phase 1).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger(__name__)

# Spec section 2: "Supported media currently includes: WAV, FLAC, MP4, SKP"
SUPPORTED_EXTENSIONS = frozenset({".wav", ".flac", ".mp4", ".skp"})

# Spec section 12: explicit database states. NEW rows start at "NEW";
# Phase 2 (not built yet) is what would move a row to METADATA_COMPLETE /
# PENDING_METADATA / METADATA_REVIEW / ERROR. "MODIFIED" is this module's
# own addition, distinct from "NEW", so a re-scan can tell "just showed up"
# apart from "existed before but changed on disk" for logging/UI purposes
# even though both currently reset status the same way (see _upsert).
STATUS_NEW = "NEW"
STATUS_MODIFIED = "MODIFIED"
STATUS_SCANNED = "SCANNED"
STATUS_METADATA_COMPLETE = "METADATA_COMPLETE"
STATUS_PENDING_METADATA = "PENDING_METADATA"
STATUS_METADATA_REVIEW = "METADATA_REVIEW"
STATUS_ERROR = "ERROR"
STATUS_REMOVED = "REMOVED"  # soft-delete: file no longer found on disk

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_files (
    path                TEXT PRIMARY KEY,
    filename            TEXT NOT NULL,
    media_type          TEXT NOT NULL,      -- file extension, lowercase, no dot
    duration            REAL,
    title               TEXT,
    artist              TEXT,
    album               TEXT,
    album_artist        TEXT,
    track               TEXT,
    disc                TEXT,
    year                TEXT,
    genre               TEXT,
    codec               TEXT,
    bit_depth           INTEGER,
    sample_rate         INTEGER,
    channels            INTEGER,
    artwork_ref         TEXT,               -- path into the artwork cache, or NULL
    metadata_status     TEXT NOT NULL,      -- one of the STATUS_* constants above
    last_scanned        REAL NOT NULL,      -- unix timestamp of the scan that touched this row
    file_mtime_ns       INTEGER NOT NULL,
    file_size           INTEGER NOT NULL,
    content_hash        TEXT,               -- reserved for future rename detection (see module docstring)
    musicbrainz_release_id TEXT,
    musicbrainz_track_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_files_status ON media_files(metadata_status);
"""


@dataclass
class ScanSummary:
    """Returned by MediaIndex.scan() -- shaped to feed spec section 11's
    "SCANNING / 342 files discovered / 17 new / ..." status line directly,
    once something in the UI actually reads it (not wired up yet — see
    module docstring).
    """
    discovered: int = 0
    new: int = 0
    modified: int = 0
    unchanged: int = 0
    removed: int = 0

    def as_message(self) -> str:
        parts = [f"{self.discovered} files discovered"]
        if self.new:
            parts.append(f"{self.new} new")
        if self.modified:
            parts.append(f"{self.modified} modified")
        if self.removed:
            parts.append(f"{self.removed} removed")
        return ", ".join(parts)


class MediaIndex:
    """SQLite-backed media file index. One connection, opened lazily,
    reused across calls — same rationale as MpdCommander's single
    connection: SQLite handles one local writer fine, and this avoids a
    connect/close per scan press.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- Phase 1: discovery + change detection --------------------------------

    def scan(self, roots: list[str]) -> ScanSummary:
        """Walk ``roots`` for supported media, diffing against the index.

        Deliberately does NOT extract embedded metadata or artwork (that's
        Phase 2 — see module docstring) and does NOT touch MPD's own
        database or VideoLibrary's in-memory list; this is purely "what
        files exist now, and which of them are new/changed/gone since the
        last scan".
        """
        import time
        now = time.time()
        summary = ScanSummary()
        seen_paths: set[str] = set()

        for root in roots:
            root_path = Path(root)
            if not root_path.is_dir():
                LOG.warning("media index scan root does not exist, skipping: %s", root)
                continue
            for entry in root_path.rglob("*"):
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                path_str = str(entry)
                seen_paths.add(path_str)
                summary.discovered += 1
                try:
                    st = entry.stat()
                except OSError:
                    LOG.debug("could not stat %s during scan", path_str, exc_info=True)
                    continue
                outcome = self._upsert(entry, st.st_size, st.st_mtime_ns, now)
                if outcome == STATUS_NEW:
                    summary.new += 1
                elif outcome == STATUS_MODIFIED:
                    summary.modified += 1
                else:
                    summary.unchanged += 1

        summary.removed = self._mark_missing_as_removed(seen_paths, now)
        self._conn.commit()
        LOG.info("media index scan: %s", summary.as_message())
        return summary

    def _upsert(self, entry: Path, size: int, mtime_ns: int, now: float) -> str:
        """Insert or update one file's row. Returns which of NEW/MODIFIED/
        SCANNED (unchanged) happened, for the caller's summary counts.
        """
        cur = self._conn.execute(
            "SELECT file_size, file_mtime_ns, metadata_status FROM media_files WHERE path = ?",
            (str(entry),),
        )
        row = cur.fetchone()
        media_type = entry.suffix.lower().lstrip(".")

        if row is None:
            self._conn.execute(
                """INSERT INTO media_files
                   (path, filename, media_type, metadata_status, last_scanned,
                    file_mtime_ns, file_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(entry), entry.name, media_type, STATUS_NEW, now, mtime_ns, size),
            )
            return STATUS_NEW

        unchanged = row["file_size"] == size and row["file_mtime_ns"] == mtime_ns
        if unchanged:
            self._conn.execute(
                "UPDATE media_files SET last_scanned = ? WHERE path = ?",
                (now, str(entry)),
            )
            return STATUS_SCANNED

        # Changed on disk since last scan -- reset to NEW so a future
        # Phase 2 metadata pass knows to re-extract it. A previously
        # REMOVED row that reappeared (e.g. a drive was unplugged and
        # reconnected) also lands here, which is the right outcome: treat
        # it like new content again rather than leaving it REMOVED.
        self._conn.execute(
            """UPDATE media_files
               SET file_size = ?, file_mtime_ns = ?, last_scanned = ?,
                   metadata_status = ?
               WHERE path = ?""",
            (size, mtime_ns, now, STATUS_NEW, str(entry)),
        )
        return STATUS_MODIFIED

    def _mark_missing_as_removed(self, seen_paths: set[str], now: float) -> int:
        """Soft-delete rows for files that were indexed before but weren't
        found in this scan (deleted, or on a currently-unmounted drive).
        Soft, not hard delete -- per spec section 12's spirit of explicit
        states rather than silently losing history; a file that reappears
        (see _upsert) simply flips back to NEW.
        """
        cur = self._conn.execute(
            "SELECT path FROM media_files WHERE metadata_status != ?",
            (STATUS_REMOVED,),
        )
        indexed_paths = {r["path"] for r in cur.fetchall()}
        missing = indexed_paths - seen_paths
        for path in missing:
            self._conn.execute(
                "UPDATE media_files SET metadata_status = ?, last_scanned = ? WHERE path = ?",
                (STATUS_REMOVED, now, path),
            )
        return len(missing)

    # -- read helpers (for Phase 2+ and eventual UI wiring) --------------------

    def pending_metadata(self) -> list[sqlite3.Row]:
        """Rows awaiting enrichment -- what a future Phase 7 rescan would
        iterate over once network connectivity is confirmed.
        """
        cur = self._conn.execute(
            "SELECT * FROM media_files WHERE metadata_status = ? ORDER BY last_scanned",
            (STATUS_PENDING_METADATA,),
        )
        return cur.fetchall()

    def counts_by_status(self) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT metadata_status, COUNT(*) AS n FROM media_files GROUP BY metadata_status"
        )
        return {r["metadata_status"]: r["n"] for r in cur.fetchall()}


def _selftest() -> None:
    """Standalone exercise against a scratch directory -- NOT hooked into
    any daemon or real media directory. Run directly:
        python3 -m player.media_index
    Proves the discovery/change-detection logic in isolation before it's
    wired into anything that touches live playback (see module docstring).
    """
    import shutil
    import tempfile
    import time

    logging.basicConfig(level=logging.INFO)
    tmp = tempfile.mkdtemp(prefix="media_index_selftest_")
    db_path = str(Path(tmp) / "media_index.sqlite3")
    music_dir = Path(tmp) / "Music"
    music_dir.mkdir()
    try:
        (music_dir / "track1.flac").write_bytes(b"fake flac data")
        (music_dir / "track2.wav").write_bytes(b"fake wav data")
        (music_dir / "notes.txt").write_text("should be ignored, not a supported type")

        idx = MediaIndex(db_path)
        summary1 = idx.scan([str(music_dir)])
        assert summary1.discovered == 2, summary1
        assert summary1.new == 2, summary1
        print("scan 1 (fresh):", summary1.as_message())

        summary2 = idx.scan([str(music_dir)])
        assert summary2.new == 0 and summary2.modified == 0, summary2
        assert summary2.unchanged == 2, summary2
        print("scan 2 (unchanged):", summary2.as_message())

        time.sleep(0.01)
        (music_dir / "track1.flac").write_bytes(b"fake flac data, but longer now")
        summary3 = idx.scan([str(music_dir)])
        assert summary3.modified == 1, summary3
        print("scan 3 (one file modified):", summary3.as_message())

        (music_dir / "track2.wav").unlink()
        summary4 = idx.scan([str(music_dir)])
        assert summary4.removed == 1, summary4
        print("scan 4 (one file removed):", summary4.as_message())
        print("counts by status:", idx.counts_by_status())
        idx.close()
        print("SELFTEST PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _selftest()
