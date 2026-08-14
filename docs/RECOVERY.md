# Recovery: "SyntaxError: invalid syntax" on `export PYTHONPATH`

## What happened

```
File "/opt/rpi-player/bin/tourbox-daemon", line 2
    export PYTHONPATH="/opt/rpi-player"
           ^^^^^^^^^^
SyntaxError: invalid syntax
```

One or more files in `/opt/rpi-player/player/` are **shell scripts with a `.py`
extension**:

```bash
$ head -n1 /opt/rpi-player/player/tourbox_daemon.py
#!/usr/bin/env bash
```

systemd runs it through `python`, Python reaches `export PYTHONPATH=...` on
line 2, and reports a syntax error. Nothing is wrong with the Python code.

Symlinking `bin/tourbox-daemon` to `player/tourbox_daemon.py` cannot help,
because the symlink target is itself the stub.

> **Diagnose per file, not per tree.** When this happened on 2026-08-08 the
> obvious conclusion — "the whole deployment is a different scaffold" — was
> **wrong**. Every module was correct except exactly two, which had been
> overwritten half an hour after the rest landed. Judging the tree by its two
> broken files would have thrown away a working install. See the field notes at
> the end of this document.

## Confirming the diagnosis

Check each file individually — size *and* shebang:

```bash
for f in /opt/rpi-player/player/*.py /opt/rpi-player/player/*/*.py; do
  printf "%8s  %-46s | %s\n" "$(stat -c%s "$f")" "${f#/opt/rpi-player/}" "$(head -n1 "$f")"
done
```

Real modules are 5–20 kB and start with `#!/usr/bin/env python3` or a docstring.
A stub is ~120 B and starts with `#!/usr/bin/env bash`.

Other signals worth checking:

| Signal | Broken | Correct |
|---|---|---|
| Module in `ExecStart` | `-m player.tourbox` | `-m player.tourbox_daemon` |
| `config.toml` routes | 0 | 4 (local, bt, airplay, hdmi) |
| `keymap.toml` buttons | 0 | 12 buttons + 3 rotaries |
| `ls /opt` | `opt  rpi-player` | `rpi-player` |

`player.tourbox` and `player.streamdeck` are **packages** here (`protocol.py`,
`render.py`, `layout.py` live inside them). They are not entry points, so
`-m player.tourbox` could never have worked.

Run the full diagnostic, which does all of the above in one pass:

```bash
bash ~/rpi-music-player/tests/diagnose-deploy.sh
```

## Fast path: only the daemon files are stubs

If the diagnostic shows everything healthy except one or two modules, you do not
need the full rebuild below. `/opt/rpi-player` is owned by your user, so no sudo
is required:

```bash
# from your laptop
cd ~/Projects/RPi-Music-player
for f in player/tourbox_daemon.py player/streamdeck_daemon.py; do
  scp "opt/rpi-player/$f" rpi@rpi-audio.local:/opt/rpi-player/$f
done
ssh rpi@rpi-audio.local 'sudo systemctl restart tourbox-player streamdeck-player'
```

---

## Recovery

### 1. Stop and disable the services

```bash
sudo systemctl stop    tourbox-player streamdeck-player
sudo systemctl disable tourbox-player streamdeck-player
sudo rm -f /etc/systemd/system/tourbox-player.service \
           /etc/systemd/system/streamdeck-player.service \
           /etc/systemd/system/shutdown-button.service
sudo systemctl daemon-reload
```

### 2. Remove the bad trees

Back up the keymap first **only if** you already ran `tourbox-capture --learn`
and it contains real byte codes. If it shows 0 buttons, there is nothing worth
keeping.

```bash
cp /opt/rpi-player/etc/keymap.toml ~/keymap.toml.bak 2>/dev/null || true

sudo rm -rf /opt/rpi-player
sudo rm -rf /opt/opt            # the nested copy from 'cp -r opt /opt/'
rm -rf ~/uploaded_opt           # stale transfer, if you no longer need it
```

The venv is rebuilt by the installer, so removing the whole tree is safe.

### 3. Get a clean copy of the source onto the Pi

From your **laptop**, not the Pi. `rsync --delete` guarantees no leftovers from
the previous transfer:

```bash
rsync -av --delete \
    --exclude '.git' --exclude '__pycache__' --exclude 'venv' \
    ~/Projects/RPi-Music-player/  rpi@rpi-audio.local:~/rpi-music-player/
```

Then on the Pi, confirm you have the real thing before installing:

```bash
cd ~/rpi-music-player
head -n1 opt/rpi-player/player/tourbox_daemon.py   # -> #!/usr/bin/env python3
ls docs/                                            # -> ROADMAP.md RECOVERY.md
wc -l opt/rpi-player/player/*.py                    # each should be 100+ lines
```

If `head` still prints `bash`, the source on your laptop is wrong too — you are
copying from the wrong directory.

### 4. Reinstall

```bash
cd ~/rpi-music-player
sudo ./install.sh
```

The installer now refuses to deploy a broken tree. It checks, before copying:

- every module is real Python, not a shell script
- every module meets a minimum plausible size
- every module passes `py_compile`
- `config.toml` has `[[routes]]` and `keymap.toml` has `[buttons]`/`[rotary]`

and after building the venv:

- every module imports cleanly
- `python -m player.tourbox_daemon --help` and the Stream Deck equivalent both run
- the deployed config parses to a non-empty route and button set

Any failure aborts with a specific message instead of installing something that
crash-loops.

### 5. Verify

```bash
sudo systemctl status tourbox-player streamdeck-player
journalctl -u tourbox-player -n 30 --no-pager
/opt/rpi-player/bin/rpi-player-doctor
```

A healthy start looks like:

```
tourbox daemon started
loaded keymap: 12 buttons, 3 rotaries, modifier=side
current output route: Local
```

---

## Running the tests on the Pi

Your earlier attempts failed for two separate reasons, both now fixed.

**System Python has no Pillow.** `./tests/run-all.sh` now auto-selects an
interpreter that does — `/opt/rpi-player/venv/bin/python` when it exists — and
tells you what is missing rather than dying on `ModuleNotFoundError`.

```bash
cd ~/rpi-music-player
./tests/run-all.sh
```

Override the interpreter explicitly if needed:

```bash
PYTHON=/opt/rpi-player/venv/bin/python ./tests/run-all.sh
```

**Do not use `unittest discover`.** These are plain scripts, not `TestCase`
classes. That is what produced:

```
ImportError: 'test_render' module incorrectly imported from
'/home/rpi/rpi-music-player/opt/rpi-player'. Expected '.../tests'.
```

`unittest` walked into `opt/rpi-player/`, found the old copies of the test files
that had been left there, and imported the same module name from two different
roots. Use `run-all.sh`.

To run the suite against the **deployed** copy rather than the working copy:

```bash
RPI_PLAYER_TEST_ROOT=/opt/rpi-player ./tests/run-all.sh
```

---

## Preventing a repeat

- **Deploy only with `install.sh`.** Do not `cp -r` into `/opt` by hand — that
  is what produced `/opt/opt`, and it bypasses every integrity check.
- **Transfer with `rsync --delete`**, not drag-and-drop over SMB. SMB copies can
  silently truncate and will not remove files the source no longer has.
- **Run `tests/diagnose-deploy.sh`** whenever a daemon will not start. It reports
  file types, module importability, config contents, and unit `ExecStart` lines
  in one pass.

## Next step once it starts

The shipped `keymap.toml` byte codes are unverified community values. Before
trusting the controller:

```bash
sudo systemctl stop tourbox-player
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture --sniff
/opt/rpi-player/venv/bin/python /opt/rpi-player/bin/tourbox-capture \
    --learn --out /tmp/keymap.toml
diff /opt/rpi-player/etc/keymap.toml /tmp/keymap.toml
sudo cp /tmp/keymap.toml /opt/rpi-player/etc/keymap.toml
sudo systemctl start tourbox-player
```

See `docs/ROADMAP.md` Phase 3.

---

## Field notes from the 2026-08-08 bring-up

Verified directly on the device (Pi 5, Trixie, Python 3.13, python-mpd2 3.1.1).

### What was actually broken

The tree was **not** a foreign scaffold — that first diagnosis was wrong. Every
real module was present and correct. Exactly **two files** had been overwritten
with three-line bash wrappers, at 13:45, half an hour after everything else
landed at 13:09:

```
   116 B  player/tourbox_daemon.py     #!/usr/bin/env bash
   119 B  player/streamdeck_daemon.py  #!/usr/bin/env bash
```

Everything else — `config.py`, `mpdbus.py`, `outputs.py`, `actions.py`,
`protocol.py`, `render.py`, both TOMLs — was intact and the right size. The
lesson: check file *sizes and shebangs* per file before concluding anything
about the tree as a whole.

Also found: `/opt/opt/rpi-player` (a symlink loop back to `/opt/rpi-player`),
`bin/` missing every script, `/etc/mpd.conf.rpi-player` at **0 bytes** because
the installer's `sed` had no source to read, and `/home/rpi/Music` containing
only macOS `._*` AppleDouble junk from an SMB copy.

### Bugs this exposed in the repo (all now fixed)

| Bug | Symptom | Fix |
|---|---|---|
| `StartLimitIntervalSec` in `[Service]` | `Unknown key ... ignoring`; restart limiting silently stayed at default | moved to `[Unit]` |
| Multi-line `python -c` in `ExecStopPost` | `IndentationError` on every stop — systemd's line continuation keeps leading whitespace | replaced with `bin/streamdeck-blank` |
| Font paths under `/opt/rpi-player/assets/fonts/` | dir was never populated; renderer fell back to the unreadable PIL bitmap font | point at `fonts-dejavu-core` system paths |
| `MpdWatcher.stop()` called `noidle()` | `NotImplementedError: Abstract MPDClientBase does not implement noidle` | shut the socket down instead |

### python-mpd2 3.1.1 — measured, not assumed

Three ways to interrupt a blocked `idle()` were tested on the device:

| Approach | Result |
|---|---|
| `client.noidle()` from another thread | `NotImplementedError`. idle/noidle is not thread-safe. |
| `client.idletimeout = N` | Raises `TimeoutError` on schedule, but leaves the connection **permanently broken** — `OSError: cannot read from timed out object`. Every poll would force a reconnect. |
| `client._sock.shutdown(SHUT_RDWR)` | Unblocks immediately, raises `ConnectionError`, which the existing handler already catches. **This is what we use.** |

Also note: `send_idle` / `fetch_idle` **do not exist** in 3.1.1, despite
appearing in older documentation. Only blocking `idle()`, `noidle()`, and
`fileno()` are available.

Confirmed working end-to-end afterwards: `mpc volume` produced
`mpd idle: mixer`, `mpc random on` produced `mpd idle: options`.
