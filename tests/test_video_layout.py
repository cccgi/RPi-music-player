"""LAYOUT_VIDEO sanity checks for the prev/next relocation + fast-scrub +
subfolder-access change.

Reproduces the exact request: Prev/Next moved outward one column each (from
(2,1)/(2,5) to (2,0)/(2,6)), their old spots now hold coarse 30s scrub keys
using seek_fast (double the plain 15s seek step), and the four previously
blank row-1 slots (1,4)-(1,7) now hold direct subfolder-access keys.
"""
import _bootstrap  # noqa: F401
from player.config import setup_logging
from player.streamdeck import layout
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


print("=== transport row: prev/next moved outward, fast-scrub fills old spots ===")
L = layout.LAYOUT_VIDEO

check("prev now at (2,0)", L[layout.index(2, 0)].action, "video_prev_song")
check("next now at (2,6)", L[layout.index(2, 6)].action, "video_next_song")

back_fast = L[layout.index(2, 1)]
check("(2,1) is the new coarse back-scrub", back_fast.action, "video_seek_back_fast")
check("coarse back-scrub uses the 3-chevron symbol", back_fast.params.get("symbol"), "rew3")
check("coarse back-scrub caption reflects seek_fast", back_fast.params.get("caption_fmt"), "-{seek_fast}s")

fwd_fast = L[layout.index(2, 5)]
check("(2,5) is the new coarse forward-scrub", fwd_fast.action, "video_seek_forward_fast")
check("coarse forward-scrub uses the 3-chevron symbol", fwd_fast.params.get("symbol"), "ffwd3")
check("coarse forward-scrub caption reflects seek_fast", fwd_fast.params.get("caption_fmt"), "+{seek_fast}s")

# Plain (15s) seek keys stay put — only prev/next moved.
check("plain seek-back still at (2,2)", L[layout.index(2, 2)].action, "video_seek_back")
check("plain seek-forward still at (2,4)", L[layout.index(2, 4)].action, "video_seek_forward")
check("playpause untouched at (2,3)", L[layout.index(2, 3)].action, "video_play_pause")

print("\n=== (2,7): Delete, the EXACT SAME cell as the music page's Delete key ===")
delete_key = L[layout.index(2, 7)]
check("delete key kind (shared render treatment with music's Delete)",
      delete_key.kind, "delete")
check("delete key action", delete_key.action, "video_delete_current")
check("music page's Delete really is at the same index",
      layout.LAYOUT[layout.index(2, 7)].kind, "delete")

print("\n=== (1,7): Wi-Fi toggle (moved off (2,7) to make room for Delete) ===")
wifi_key = L[layout.index(1, 7)]
check("wifi key kind", wifi_key.kind, "wifi_toggle")
check("wifi key action", wifi_key.action, "toggle_wifi")
check("Power is still at (3,7)", L[layout.index(3, 7)].action, "shutdown")

print("\n=== row 1, cols 4-5: direct subfolder access (2 slots, not 3) ===")
for col, slot in zip(range(4, 6), range(2)):
    kd = L[layout.index(1, col)]
    check(f"(1,{col}) is a video_folder key", kd.kind, "video_folder")
    check(f"(1,{col}) slot matches its position", kd.params.get("slot"), slot)

print("\n=== (1,6): live Pi power-draw tile (took the 3rd folder slot's cell) ===")
power_key = L[layout.index(1, 6)]
check("power-draw tile kind", power_key.kind, "power_draw")
check("power-draw tile has no action -- display only", power_key.action, "")

print("\n=== (0,7): screen on/off toggle, top-right of the Video page ===")
screen_key = L[layout.index(0, 7)]
check("screen toggle kind", screen_key.kind, "screen_toggle")
check("screen toggle action", screen_key.action, "toggle_screen")
check("music page's (0,7) is unaffected (still the plain bitrate/toast slot)",
      layout.LAYOUT[layout.index(0, 7)].kind, "bitrate")
check("video page has its OWN toast slot now, separate from the music page's",
      layout.TOAST_SLOT_VIDEO, layout.index(0, 6))
check("TOAST_SLOT_VIDEO != TOAST_SLOT (they used to be the same shared slot)",
      layout.TOAST_SLOT_VIDEO == layout.TOAST_SLOT, False)

print("\n=== no duplicate/overlapping key indices ===")
# Every dict key in LAYOUT_VIDEO is unique by construction (it's a dict), but
# guard against a copy/paste index() typo landing two defs on literally the
# same cell by re-deriving indices from the KeyDef set some other way isn't
# possible after the fact — instead just confirm the row-2 set is exactly
# the 8 cells we expect, nothing extra, nothing missing.
row2_cells = {k for k in L if k // layout.COLUMNS == 2}
check("row 2 has exactly the expected 8 populated cells (7 transport + Delete)",
      row2_cells, {layout.index(2, c) for c in (0, 1, 2, 3, 4, 5, 6, 7)})
row1_cells = {k for k in L if k // layout.COLUMNS == 1}
check("row 1 has exactly the expected 8 populated cells "
      "(progress/volume x3 + 2 folder slots + power-draw + Wi-Fi)",
      row1_cells, {layout.index(1, c) for c in (0, 1, 2, 3, 4, 5, 6, 7)})
row0_cells = {k for k in L if k // layout.COLUMNS == 0}
check("row 0 has exactly the expected 8 populated cells "
      "(4 up-next slots + now playing + track + queue position + screen toggle)",
      row0_cells, {layout.index(0, c) for c in (0, 1, 2, 3, 4, 5, 6, 7)})

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All video-layout tests passed.")
