"""Write macOS Finder color tags to internal-storage files via xattr.

macOS reads two xattrs for color tags on SMB shares backed by Samba with
the fruit + streams_xattr VFS modules (already configured for the internal
[Music] share):

  user.com.apple.metadata:_kMDItemUserTags  — binary plist: ["Green\\n2"]
  user.com.apple.FinderInfo                  — 32-byte struct, color in byte 9

This only works on the internal storage (ext4, which supports xattrs).
exFAT (USB) silently returns False — no crash, just no tag.

Usage:
    from player.mac_tags import set_tag_for_uri
    set_tag_for_uri(Path("/home/rpi/Music-internal"), "FolderA/Song.mp3", "green")
    set_tag_for_uri(Path("/home/rpi/Music-internal"), "FolderA/Song.mp3", None)  # clear
"""
from __future__ import annotations

import logging
import os
import plistlib
from pathlib import Path

LOG = logging.getLogger(__name__)

# color name → (macOS tag label, FinderInfo color index)
# Indices: 0=none, 1=grey, 2=green, 3=purple, 4=blue, 5=yellow, 6=red, 7=orange
_COLORS: dict[str, tuple[str, int]] = {
    "green":  ("Green",  2),
    "yellow": ("Yellow", 5),
    "purple": ("Purple", 3),
    "blue":   ("Blue",   4),
}

_XATTR_TAGS   = "user.com.apple.metadata:_kMDItemUserTags"
_XATTR_FINDER = "user.com.apple.FinderInfo"


def set_tag(path: str | Path, color: str | None) -> bool:
    """Set or clear a macOS color tag xattr on *path*.

    Returns True on success.  Returns False silently if the filesystem does
    not support xattrs (e.g. exFAT USB drive) — no exception is raised.
    """
    p = str(path)
    if color is None:
        # Clear both xattrs; ignore "not set" errors.
        for attr in (_XATTR_TAGS, _XATTR_FINDER):
            try:
                os.removexattr(p, attr)
            except OSError:
                pass
        return True

    entry = _COLORS.get(color)
    if entry is None:
        LOG.warning("mac_tags: unknown color %r — ignoring", color)
        return False

    label, idx = entry
    plist_data = plistlib.dumps([f"{label}\n{idx}"], fmt=plistlib.FMT_BINARY)
    finder_data = bytearray(32)
    finder_data[9] = (idx & 0x07) << 1   # color bits sit in bits 1-3 of byte 9

    try:
        os.setxattr(p, _XATTR_TAGS,   plist_data)
        os.setxattr(p, _XATTR_FINDER, bytes(finder_data))
        LOG.debug("mac_tags: set %s on %s", color, p)
        return True
    except OSError as exc:
        LOG.debug("mac_tags: setxattr skipped for %s: %s", p, exc)
        return False


def set_tag_for_uri(music_root: Path, uri: str, color: str | None) -> bool:
    """Resolve *uri* relative to *music_root* and set its color tag.

    Safe to call for USB URIs — the setxattr will fail silently on exFAT.
    Returns True if the file exists and the xattr write succeeded.
    """
    abs_path = music_root / uri
    if not abs_path.is_file():
        LOG.debug("mac_tags: file not found: %s", abs_path)
        return False
    return set_tag(abs_path, color)


def sync_yellow(music_root: Path, listened: dict[str, float],
                threshold: float = 30.0) -> int:
    """Bulk-set yellow tags on all internal-storage songs that have been
    listened to for ≥ *threshold* seconds but are not curated (green).

    Curated songs already carry a green tag — we leave those alone.
    Returns the number of files successfully tagged.
    """
    tagged = 0
    for uri, secs in listened.items():
        if secs < threshold:
            continue
        # Curated songs live under a "Favorites" path component — leave green.
        if "Favorites" in Path(uri).parts:
            continue
        if set_tag_for_uri(music_root, uri, "yellow"):
            tagged += 1
    if tagged:
        LOG.info("mac_tags: applied yellow tag to %d file(s)", tagged)
    return tagged
