"""RPi portable music player — control daemons.

Modules:
    config            TOML loading into dataclasses
    mpdbus            resilient MPD command + idle-watcher connections
    outputs           two-layer routing (MPD output + PipeWire default sink)
    actions           shared symbolic action table for both input surfaces
    ipc               optional NDJSON broadcast bus
    tourbox_daemon    TourBox CDC-ACM serial -> MPD
    streamdeck_daemon Stream Deck XL render + input -> MPD
"""

__version__ = "0.1.0"
