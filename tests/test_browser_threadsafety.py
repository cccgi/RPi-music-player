"""LibraryBrowser concurrency: the actual bug behind "the song shown on
screen doesn't match what plays when I press it."

LibraryBrowser is genuinely touched from 3 threads in the real daemon: the
Stream Deck's key-callback thread (enter/back), the render/tick loop
(visible/page_index, several times a second), and the MPD idle-watcher
thread (refresh() on every "database" event -- which delete_current
triggers on every use). Before the fix, refresh() reassigned `path`,
`offset` and `entries` as three separate, unsynchronized steps across a
network round-trip -- a concurrent visible() (or a second refresh()) could
observe a torn mix of old/new state. This drives a slow FakeMpd (a real
delay on lsinfo(), like a real network round trip) with one thread hammering
refresh()+enter() while another hammers visible(), and asserts every single
observed snapshot is self-consistent: every returned Entry's directory
membership (by URI prefix) must be internally uniform, never a mix of two
different refreshes' worth of entries in the same visible() call.
"""
import _bootstrap  # noqa: F401
import random
import threading
import time
_bootstrap.require("mpd")
from player.config import setup_logging
from player.streamdeck.browser import LibraryBrowser
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


class SlowFakeMpd:
    """Two folders (A/, B/) with distinctly-prefixed filenames, and a real
    (small but non-zero) delay on lsinfo -- long enough to reliably land a
    concurrent read mid-refresh on a real interpreter, without making the
    test slow."""

    def __init__(self):
        self.folders = {
            "": [{"directory": "A"}, {"directory": "B"}],
            "A": [{"file": f"A/track{i}.mp3"} for i in range(4)],
            "B": [{"file": f"B/track{i}.mp3"} for i in range(4)],
        }

    def lsinfo(self, path=""):
        time.sleep(0.002)  # simulate a real network round trip
        return list(self.folders.get(path, []))

    def play_uri_from_directory(self, directory, uri):
        pass


print("=== concurrent refresh()/enter() vs visible() never tears ===")
mpd = SlowFakeMpd()
browser = LibraryBrowser(mpd)
browser.enter(0)  # descend into "A" so both A and B are reachable via back()+enter()

stop = threading.Event()
inconsistent = []

def navigator():
    paths = ["A", "B"]
    while not stop.is_set():
        target = random.choice(paths)
        browser.back()
        # enter() by slot -- slot 0 is "A", slot 1 is "B" from the root
        browser.enter(0 if target == "A" else 1)


def reader():
    while not stop.is_set():
        # Only file entries carry a folder-prefixed URI ("A/track0.mp3");
        # bare directory names at the root ("A", "B") are correctly shown
        # together and are not a sign of anything torn.
        window = [e for e in browser.visible() if e is not None and not e.is_dir]
        if window:
            prefixes = {e.uri.split("/")[0] for e in window}
            if len(prefixes) > 1:
                inconsistent.append(list(window))


threads = [threading.Thread(target=navigator, daemon=True) for _ in range(3)]
threads += [threading.Thread(target=reader, daemon=True) for _ in range(3)]
for t in threads:
    t.start()
time.sleep(1.0)
stop.set()
for t in threads:
    t.join(timeout=2)

check("every visible() snapshot was internally consistent (one folder's worth)",
      inconsistent, [])

print()
if fails:
    print(f"FAILURES: {fails}")
    if inconsistent:
        print(f"  (found {len(inconsistent)} torn snapshot(s), e.g. {inconsistent[0]})")
    raise SystemExit(1)
print("All browser thread-safety tests passed.")
