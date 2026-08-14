"""Stream Deck XL key layout.

The XL is 8 columns x 4 rows = 32 keys, indexed left-to-right, top-to-bottom.

    ┌────┬────┬────┬────┬────┬────┬────┬────┐
  0 │  0 │  1 │  2 │  3 │  4 │  5 │  6 │  7 │   now playing + art
    ├────┼────┼────┼────┼────┼────┼────┼────┤
  1 │  8 │  9 │ 10 │ 11 │ 12 │ 13 │ 14 │ 15 │   progress, volume, modes
    ├────┼────┼────┼────┼────┼────┼────┼────┤
  2 │ 16 │ 17 │ 18 │ 19 │ 20 │ 21 │ 22 │ 23 │   transport
    ├────┼────┼────┼────┼────┼────┼────┼────┤
  3 │ 24 │ 25 │ 26 │ 27 │ 28 │ 29 │ 30 │ 31 │   output routes + power
    └────┴────┴────┴────┴────┴────┴────┴────┘

Each key is described by a :class:`KeyDef` naming a render *kind* (what to draw)
and an *action* (what to dispatch on press). Keeping layout as data means the
daemon's render loop is a simple lookup and rearranging the panel is a one-file
change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COLUMNS = 8
ROWS = 4
KEY_COUNT = COLUMNS * ROWS


def index(row: int, col: int) -> int:
    return row * COLUMNS + col


@dataclass(frozen=True)
class KeyDef:
    kind: str                       # render kind, see daemon._render_key
    action: str = ""                # action name dispatched on press
    label: str = ""
    params: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Row 0 — now playing
# ---------------------------------------------------------------------------
# Cover art is tiled across keys 0-3 as a 4x1 strip. The Stream Deck has no
# concept of a spanning key, so we slice one image into four and push each
# slice separately.

LAYOUT: dict[int, KeyDef] = {
    # Keys 0-3: LIBRARY BROWSER, four visible rows.
    #
    # These were reserved for album art, which is still unimplemented. Rather
    # than leave a quarter of the panel dark, they carry the folder/track
    # listing. Navigation uses the two spare keys on row 3 (Back and Page), so
    # nothing existing had to be displaced.
    index(0, 0): KeyDef("browse_entry", params={"slot": 0}),
    index(0, 1): KeyDef("browse_entry", params={"slot": 1}),
    index(0, 2): KeyDef("browse_entry", params={"slot": 2}),
    index(0, 3): KeyDef("browse_entry", params={"slot": 3}),
    # One authoritative now-playing key, then distinct metadata beside it.
    index(0, 4): KeyDef("now_playing_title", action="toggle_pause"),
    # Was a plain (inert) album-name display — no album art, so there was
    # nothing to press and nothing useful to show. Repurposed: jump the
    # browser straight to the library root in one press, so a folder several
    # levels deep is one tap away from a fresh top-level listing to descend
    # from again, rather than repeated presses of Back.
    index(0, 5): KeyDef("browse_root", label="Library"),
    index(0, 6): KeyDef("queue_position"),
    index(0, 7): KeyDef("bitrate"),          # also the toast slot

    # -----------------------------------------------------------------------
    # Row 1 — telemetry and modes
    # -----------------------------------------------------------------------
    index(1, 0): KeyDef("progress", action="seek_to_start"),
    index(1, 1): KeyDef("volume", action="toggle_mute"),
    index(1, 2): KeyDef("glyph", action="volume_down", label="Vol -",
                        params={"symbol": "vol_down", "caption": "Vol −"}),
    index(1, 3): KeyDef("glyph", action="volume_up", label="Vol +",
                        params={"symbol": "vol_up", "caption": "Vol +"}),
    index(1, 4): KeyDef("mode_random", action="toggle_random"),
    index(1, 5): KeyDef("mode_repeat", action="toggle_repeat"),
    index(1, 6): KeyDef("mode_single", action="toggle_single"),
    index(1, 7): KeyDef("mode_consume", action="toggle_consume"),

    # -----------------------------------------------------------------------
    # Row 2 — transport
    # -----------------------------------------------------------------------
    index(2, 0): KeyDef("glyph", action="prev_album",
                        params={"symbol": "rew", "caption": "Album"}),
    index(2, 1): KeyDef("glyph", action="prev_track", params={"symbol": "prev"}),
    index(2, 2): KeyDef("glyph", action="seek_back",
                        params={"symbol": "rew", "caption_fmt": "-{seek}s"}),
    index(2, 3): KeyDef("playpause", action="toggle_pause"),
    index(2, 4): KeyDef("glyph", action="seek_forward",
                        params={"symbol": "ffwd", "caption_fmt": "+{seek}s"}),
    index(2, 5): KeyDef("glyph", action="next_track", params={"symbol": "next"}),
    index(2, 6): KeyDef("glyph", action="next_album",
                        params={"symbol": "ffwd", "caption": "Album"}),
    # Key 23 was Stop, replaced with DELETE at the user's request.
    #
    # Note the neighbourhood: 21 is next-track and 22 is next-album, both of
    # which get hit constantly while listening. An irreversible action sits one
    # key away from them. Configure [delete] confirm/mode in config.toml if that
    # ever bites.
    index(2, 7): KeyDef("delete", action="delete_current"),

    # -----------------------------------------------------------------------
    # Row 3 — output routing, library, power
    # -----------------------------------------------------------------------
    # Route keys are assigned dynamically from config [[routes]] at startup;
    # see daemon._bind_route_keys(). Slots 24-26 are reserved for them —
    # only 3 now. Slot 27 was HDMI: dropped ("I do not play music audio over
    # HDMI at this time") and reused below as the video-mode entry point,
    # which fits the same "route-ish, bottom row" mental model the other
    # three keys already have. A 4th configured route ([[routes]]
    # id="airplay_arylic") still exists and still works — it just doesn't
    # get a dedicated key; see config.toml for how to reach it via the
    # TourBox's cycle_output instead.
    index(3, 3): KeyDef("video_mode_toggle", action="enter_video_mode", label="Video"),
    # Browser navigation, on the two keys that were previously blank.
    index(3, 4): KeyDef("browse_back"),
    index(3, 5): KeyDef("label", action="update_database", label="Scan",
                        params={"sub": "library"}),
    index(3, 6): KeyDef("browse_page"),
    index(3, 7): KeyDef("power", action="shutdown", label="Power"),
}

# Slots the daemon fills with one route key each, in config order. Only 3
# now — see the comment above index(3, 3) in LAYOUT for where the 4th went.
ROUTE_SLOTS = [index(3, 0), index(3, 1), index(3, 2)]

# Where a transient toast is shown. Overlays whatever normally lives here for
# a couple of seconds, then reverts.
TOAST_SLOT = index(0, 7)

# The video page's own toast slot -- separate from TOAST_SLOT because (0,7)
# on the video page is now the screen on/off key (see LAYOUT_VIDEO), not a
# spare info tile. Overlays "Up next" queue position instead, mirroring how
# TOAST_SLOT overlays the (normally low-stakes) bitrate tile on the music
# page.
TOAST_SLOT_VIDEO = index(0, 6)


def key_def(key: int) -> KeyDef | None:
    return LAYOUT.get(key)


# ---------------------------------------------------------------------------
# Video / karaoke page
# ---------------------------------------------------------------------------
# Deliberately not a full library browser yet (that's the browse_entry/
# browse_back/browse_page machinery above, which is MPD-database-backed and
# doesn't apply here — .skp files aren't in MPD's database at all). This is
# the MVP from docs/VIDEO-MODE.md section 8: enough to actually play through
# a library — prev/next song, play/pause, seek, the track switch that was
# asked for specifically — with paged search/browse as a follow-up.
#
# Reuses existing render kinds wherever the state shape lines up (see
# streamdeck_daemon._collect_video_state), so only two kinds are new:
# "audio_track" (the vocal/karaoke switch) and "video_back" (return to music).


# Deliberately mirrors LAYOUT's grid cell-for-cell wherever a video
# equivalent exists, rather than packing keys wherever there was room —
# requested specifically after the two pages drifted apart enough to be
# disorienting to switch between: Local/BT/AirPlay, the Music/Video toggle,
# and Shutdown all live at the EXACT SAME index() on both pages, so muscle
# memory built on one page carries straight over to the other. Cells with no
# sensible video equivalent (album, shuffle/repeat/single/consume)
# are simply left blank rather than filled with something arbitrary just to
# avoid empty space. Scan (3,5) and Delete (2,7) DO have video equivalents —
# see video_rescan and video_delete_current below — since nothing watches
# the Video folder for changes the way MPD's database watches Music, so a
# dropped-in file is otherwise invisible until the daemon restarts, and
# unwanted videos need the same on-device cleanup music files already have.
LAYOUT_VIDEO: dict[int, KeyDef] = {
    # Row 0 — mirrors LAYOUT's browser strip (0,0-3) + now playing (0,4-7).
    # "Up next" sits where the library browser sits on the music page — same
    # idea, browsing a list — handled like browse_entry: special-cased in
    # streamdeck_daemon._on_key rather than the shared action table, since
    # which library entry a slot means depends on where playback is.
    index(0, 0): KeyDef("video_entry", params={"slot": 0}),
    index(0, 1): KeyDef("video_entry", params={"slot": 1}),
    index(0, 2): KeyDef("video_entry", params={"slot": 2}),
    index(0, 3): KeyDef("video_entry", params={"slot": 3}),
    index(0, 4): KeyDef("now_playing_title", action="video_play_pause"),
    index(0, 5): KeyDef("audio_track", action="switch_track", label="Track"),
    index(0, 6): KeyDef("queue_position"),
    # Top-right: turn the Stream Deck's own backlight off to save power on
    # the road, at the user's specific request for "top right tile". This
    # only touches deck.set_brightness() -- the USB HID connection stays up
    # the whole time, so every key keeps responding to presses with the
    # screen dark; see streamdeck_daemon._on_key's wake-on-any-key handling
    # (the same mechanism the existing dim/blank-after-idle feature already
    # uses) for how the first press after blanking just wakes the panel
    # instead of also firing that key's normal action. Used TOAST_SLOT_VIDEO
    # instead of TOAST_SLOT for the video page now that this cell has a
    # permanent job.
    index(0, 7): KeyDef("screen_toggle", action="toggle_screen", label="Screen"),

    # Row 1 — mirrors progress/volume. mpv has its own softvol, entirely
    # separate from MPD's (paused/idle the whole time video plays) — there
    # was previously no way to change video volume from the panel at all.
    index(1, 0): KeyDef("progress"),
    index(1, 1): KeyDef("volume", action="video_toggle_mute"),
    index(1, 2): KeyDef("glyph", action="video_volume_down", label="Vol -",
                        params={"symbol": "vol_down", "caption": "Vol −"}),
    index(1, 3): KeyDef("glyph", action="video_volume_up", label="Vol +",
                        params={"symbol": "vol_up", "caption": "Vol +"}),
    # (1,4-5): direct subfolder access — jump straight to the first video in
    # one of the library's top-level subfolders (e.g. "Favorites", "Remix").
    # No music-page equivalent to mirror (the browser strip already covers
    # that job there), so these slots — otherwise blank, mirroring
    # shuffle/repeat/single/consume which don't apply to video — get their
    # own kind instead. Populated dynamically from
    # VideoLibrary.top_folders(); see streamdeck_daemon._on_video_folder_press.
    # Only 2 slots now, not 3 — slot 2's cell (1,6) was given to the power-
    # draw tile below (it was rendering as an empty/unused tile for most
    # libraries, which only have a couple of top-level folders anyway, so
    # losing it costs little). (1,7) is Wi-Fi (see below), freeing (2,7) for
    # Delete at the same cell as the music page's.
    index(1, 4): KeyDef("video_folder", params={"slot": 0}),
    index(1, 5): KeyDef("video_folder", params={"slot": 1}),
    # Live Pi board power draw (5V-equivalent mA, read from the Pi 5's PMIC
    # -- see player/power.py). Display-only, no action: there is nothing to
    # press here, it's a gauge. Does NOT include the Stream Deck's own
    # draw -- there is no power-monitoring hardware anywhere in this
    # build's USB chain to measure that, so rather than show a fabricated
    # number this only ever reports the Pi's own board draw. Refreshed on
    # the same cadence as everything else on this page (every tick_seconds
    # while something is playing, every 5s otherwise — see
    # streamdeck_daemon._tick).
    index(1, 6): KeyDef("power_draw"),
    # Wi-Fi off/on, a battery saver for on-the-road use (turn Wi-Fi off to
    # save power while driving, stay off across Video<->Music switches, come
    # back on automatically only at the next reboot — see actions.py's
    # toggle_wifi and system/systemd/rpi-player-wifi-on.service). Originally
    # placed at (2,7) directly above Power; moved here to make room for
    # Delete at (2,7), matching the music page's delete key position exactly
    # as requested. No music-page equivalent by design: this button only
    # exists where "on the road" actually applies.
    index(1, 7): KeyDef("wifi_toggle", action="toggle_wifi", label="Wifi"),

    # Row 2 — mirrors the transport belt; playpause lands on the EXACT same
    # cell (2,3) as the music page's. Prev/next moved outward by one column
    # each (from 2,1/2,5 to 2,0/2,6) to make room for a coarser scrub pair
    # in their old spots — the plain seek keys (2,2)/(2,4) step
    # [tourbox.steps] seek (15s default); these step seek_fast (30s
    # default, exactly double) for covering more ground in a long video.
    index(2, 0): KeyDef("glyph", action="video_prev_song", params={"symbol": "prev"}),
    index(2, 1): KeyDef("glyph", action="video_seek_back_fast",
                        params={"symbol": "rew3", "caption_fmt": "-{seek_fast}s"}),
    index(2, 2): KeyDef("glyph", action="video_seek_back",
                        params={"symbol": "rew", "caption": "Seek"}),
    index(2, 3): KeyDef("playpause", action="video_play_pause"),
    index(2, 4): KeyDef("glyph", action="video_seek_forward",
                        params={"symbol": "ffwd", "caption": "Seek"}),
    index(2, 5): KeyDef("glyph", action="video_seek_forward_fast",
                        params={"symbol": "ffwd3", "caption_fmt": "+{seek_fast}s"}),
    index(2, 6): KeyDef("glyph", action="video_next_song", params={"symbol": "next"}),
    # (2,7) — the EXACT SAME cell as LAYOUT's Delete key — deletes/trashes
    # the currently playing video, using the same ctx.delete settings
    # (mode/trash_dir/log_path/confirm) as music's delete_current, one
    # config for both. Uses the same "delete" render kind, so it looks
    # and confirms identically (see streamdeck_daemon._handle_delete_key).
    index(2, 7): KeyDef("delete", action="video_delete_current"),

    # Row 3 — routes, mode toggle, browse nav, power: all at the SAME cells
    # as the music page's row 3.
    #
    # Same audio routes as the music page (Local needs the ALSA hw: handoff
    # described in actions.py's video_route_local — PipeWire never sees that
    # card at all; BT/AirPlay are a plain PipeWire default-sink switch, same
    # as the music page's route keys). Pressing BT or AirPlay opens a picker
    # (streamdeck_daemon._enter_bt_picker / _enter_airplay_picker) instead
    # of dispatching these actions directly — see the interception in
    # _on_key.
    index(3, 0): KeyDef("video_route", action="video_route_local",
                        params={"route_id": "local"}),
    index(3, 1): KeyDef("video_route", action="video_route_bt",
                        params={"route_id": "bt"}),
    index(3, 2): KeyDef("video_route", action="video_route_airplay",
                        params={"route_id": "airplay"}),
    # Same physical key as the music page's Video toggle (3,3) — only the
    # label and action change with the page, never the position.
    index(3, 3): KeyDef("video_back", action="exit_video_mode", label="Music"),
    index(3, 4): KeyDef("glyph", action="video_browse_prev",
                        params={"symbol": "rew", "caption": "Songs"}),
    index(3, 5): KeyDef("label", action="video_rescan", label="Scan",
                        params={"sub": "video"}),
    index(3, 6): KeyDef("glyph", action="video_browse_next",
                        params={"symbol": "ffwd", "caption": "Songs"}),
    # Same key, same action name, as the music page's Shutdown (3,7) — the
    # two-step confirm logic in streamdeck_daemon._on_key is shared across
    # both pages, not reimplemented here.
    index(3, 7): KeyDef("power", action="shutdown", label="Power"),
}


def video_key_def(key: int) -> KeyDef | None:
    return LAYOUT_VIDEO.get(key)


# ---------------------------------------------------------------------------
# AirPlay picker — transient overlay page
# ---------------------------------------------------------------------------
# Not a real "mode" (nothing is written to player/mode.py's sentinel file;
# the TourBox has no screen to show a list on, so this is purely a Stream
# Deck concern). Opened by pressing the AirPlay route key on either the
# music or video page; closes back to whichever page opened it.
#
# Slots are populated dynamically from whatever `raop_sink` nodes PipeWire
# currently sees — see streamdeck_daemon._render_airplay_picker — so, unlike
# the 3 fixed route keys, a speaker with no [[routes]] entry at all still
# shows up here.

LAYOUT_AIRPLAY_PICKER: dict[int, KeyDef] = {
    index(0, 0): KeyDef("airplay_entry", params={"slot": 0}),
    index(0, 1): KeyDef("airplay_entry", params={"slot": 1}),
    index(0, 2): KeyDef("airplay_entry", params={"slot": 2}),
    index(0, 3): KeyDef("airplay_entry", params={"slot": 3}),
    index(0, 4): KeyDef("airplay_entry", params={"slot": 4}),
    index(0, 5): KeyDef("airplay_entry", params={"slot": 5}),

    # Wi-Fi status + fix buttons -- added after repeated "iffy Wi-Fi" /
    # "AirPlay speakers vanish" incidents that always turned out to be
    # something on the Wi-Fi side (power-save sleeping the radio between
    # beacons and dropping mDNS multicast, or a stale/duplicate DHCP lease)
    # rather than anything AirPlay-specific -- this is the page you're
    # already on when that symptom shows up, and previously fixing it
    # required a LAN cable + SSH. See player/wifi.py.
    index(1, 0): KeyDef("wifi_status", params={}),
    index(1, 1): KeyDef("wifi_ip", params={}),

    index(3, 5): KeyDef("wifi_reconnect", action="wifi_reconnect", label="Reconnect"),
    index(3, 6): KeyDef("wifi_fix", action="wifi_restart_networking", label="Fix Wifi"),

    index(3, 7): KeyDef("video_back", action="close_airplay_picker", label="Back"),
}


def airplay_picker_key_def(key: int) -> KeyDef | None:
    return LAYOUT_AIRPLAY_PICKER.get(key)


# ---------------------------------------------------------------------------
# Bluetooth picker — same idea as the AirPlay picker, but the candidate list
# comes from bluetoothd (known devices immediately, a scan filling in
# anything new a couple of seconds later) instead of PipeWire.
# ---------------------------------------------------------------------------

LAYOUT_BT_PICKER: dict[int, KeyDef] = {
    index(0, 0): KeyDef("bt_entry", params={"slot": 0}),
    index(0, 1): KeyDef("bt_entry", params={"slot": 1}),
    index(0, 2): KeyDef("bt_entry", params={"slot": 2}),
    index(0, 3): KeyDef("bt_entry", params={"slot": 3}),
    index(0, 4): KeyDef("bt_entry", params={"slot": 4}),
    index(0, 5): KeyDef("bt_entry", params={"slot": 5}),

    # Clears any paired-but-disconnected device's stuck bond (bt.py's
    # reset_failed_pairings_async) so a device stuck failing to reconnect
    # can be re-paired from scratch without needing SSH on the road -- see
    # the "Bose SLIII shows up but still can't pair/connect" incident this
    # was added for: `bluetoothctl remove` alone sometimes leaves a stale
    # on-disk record that makes a fresh `pair` fail with `AlreadyExists`.
    index(3, 6): KeyDef("bt_reset", action="reset_bt_pairings", label="Reset"),

    index(3, 7): KeyDef("video_back", action="close_bt_picker", label="Back"),
}


def bt_picker_key_def(key: int) -> KeyDef | None:
    return LAYOUT_BT_PICKER.get(key)
