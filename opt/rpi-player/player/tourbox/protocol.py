"""TourBox byte-stream decoder.

The device is a USB CDC-ACM serial port emitting single bytes at 115200 8N1.
There is no HID report descriptor and no framing — just a byte stream:

    button press    -> byte B
    button release  -> byte B | 0x80
    rotary detent   -> one byte per click, distinct byte per direction

This module turns that stream into semantic :class:`Event` objects. It is
entirely data-driven from the keymap, holds no MPD knowledge, and does no I/O,
which makes it straightforward to unit-test against a recorded byte sequence.

Three timing behaviours matter and interact:

* **Long press** — a control with a ``long`` binding cannot fire its short
  action on the press edge; we must wait to see if the hold continues. That
  delay is real, which is why the hot path leaves ``long`` unset.
* **Double click** — a control with a ``double`` binding but NO ``press``
  binding costs nothing: a single press does nothing anyway, so the second
  press can fire immediately with no waiting. A control with BOTH pays a
  ``double_click_seconds`` delay on its single press.
* **Layers** — a designated toggle button cycles the active layer. Its own
  bindings are never dispatched; pressing it only switches layer.

:meth:`Decoder.tick` must be called regularly (the daemon does so on its select
timeout) so held buttons and pending double-clicks resolve without needing a
further byte to arrive.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from ..config import ButtonSpec, Keymap, RotarySpec

LOG = logging.getLogger(__name__)


class EventKind(Enum):
    PRESS = "press"          # short press, resolved
    LONG = "long"            # held past the threshold
    DOUBLE = "double"        # two presses within the double-click window
    ROTARY = "rotary"        # one or more detents
    LAYER = "layer"          # the layer-toggle button was pressed
    RAW = "raw"              # unrecognised byte (diagnostics only)


@dataclass
class Event:
    kind: EventKind
    control: str
    action: str = ""
    magnitude: int = 1        # detents for rotary, 1 for buttons
    direction: int = 0        # +1 cw / -1 ccw, 0 for buttons
    shifted: bool = False
    raw_byte: int | None = None
    rate: float = 0.0         # detents/second, measured across the burst
    layer: int = 0            # active layer index when the event resolved
    layer_label: str = ""


@dataclass
class _HeldButton:
    spec: ButtonSpec
    pressed_at: float
    long_fired: bool = False


@dataclass
class _PendingClick:
    """A press waiting to see whether a second press makes it a double."""

    spec: ButtonSpec
    at: float
    shifted: bool


@dataclass
class _RotaryAccumulator:
    """Coalesces rapid detents into one larger step."""

    spec: RotarySpec
    direction: int = 0
    count: int = 0
    first_at: float = 0.0
    last_at: float = 0.0
    shifted: bool = False

    def rate(self) -> float:
        """Detents per second measured across the burst.

        A single detent has no measurable span, so it reports 0.0 — which is
        correct: one isolated click is by definition not a fast spin. Getting
        this wrong (e.g. dividing the count by the coalesce window) makes every
        single detent look like a fast spin and the volume becomes unusable.
        """
        span = self.last_at - self.first_at
        if span <= 0 or self.count < 2:
            return 0.0
        return (self.count - 1) / span


class Decoder:
    """Stateful byte -> Event decoder with layer and double-click support."""

    def __init__(self, keymap: Keymap, long_press_seconds: float = 0.45,
                 rotary_coalesce_seconds: float = 0.06) -> None:
        self._keymap = keymap
        self._long_press = long_press_seconds
        self._coalesce = rotary_coalesce_seconds
        self._double_window = keymap.double_click_seconds

        self._held: dict[str, _HeldButton] = {}
        self._pending: dict[str, _RotaryAccumulator] = {}
        self._pending_click: dict[str, _PendingClick] = {}
        self._modifier_down = False
        # True once the modifier has actually been used to shift another
        # control while held. Lets release-without-use be told apart from a
        # real shift chord, so the modifier button can ALSO carry its own
        # `press` binding (a tap) without breaking its shift role (a hold).
        self._modifier_used = False
        self._modifier_name = keymap.modifier
        self._toggle_name = keymap.layer_toggle
        self._layer = 0
        self._unknown_bytes: set[int] = set()

    # -- layer -------------------------------------------------------------

    @property
    def layer(self) -> int:
        return self._layer

    @property
    def layer_label(self) -> str:
        return self._keymap.layer(self._layer).label

    def set_layer(self, index: int) -> None:
        if self._keymap.layers:
            self._layer = index % len(self._keymap.layers)

    def cycle_layer(self) -> int:
        """Advance to the next layer via Top — but only among the music
        layers (index 0/1, Track/Folder). Video mode (index 2, if present)
        is entered/left only from the Stream Deck's mode-switch key, never by
        cycling into it here — an accidental Top press mid-song should never
        silently swap the whole hardware into karaoke controls, and Top
        pressed while already in video mode should not silently drop back to
        music navigation either. See docs/VIDEO-MODE.md section 6.
        """
        if self._layer in (0, 1) and len(self._keymap.layers) >= 2:
            self.set_layer(1 - self._layer)
        return self._layer

    # -- binding lookup ----------------------------------------------------

    def _binding(self, kind: str, control: str) -> str:
        return self._keymap.resolve(self._layer, kind, control)

    def _has_long(self, control: str) -> bool:
        return bool(self._binding("long", control))

    def _has_double(self, control: str) -> bool:
        return bool(self._binding("double", control))

    def _has_press(self, control: str) -> bool:
        return bool(self._binding("press", control))

    def _resolve_button_action(self, control: str, long: bool) -> str:
        if long:
            return self._binding("long", control)
        if self._modifier_down and control != self._modifier_name:
            shifted = self._binding("shift", control)
            if shifted:
                return shifted
        return self._binding("press", control)

    def _resolve_rotary_action(self, spec: RotarySpec, direction: int) -> str:
        if self._modifier_down:
            shifted = spec.shift_cw if direction > 0 else spec.shift_ccw
            if shifted:
                return shifted
        return spec.action_cw if direction > 0 else spec.action_ccw

    @property
    def modifier_active(self) -> bool:
        return self._modifier_down

    # -- held-button introspection ------------------------------------------
    # For callers that want to auto-repeat an action while its control stays
    # physically held (see tourbox_daemon.py's volume auto-repeat) without
    # this class needing to know anything about what "repeatable" means.
    #
    # `_held` is maintained unconditionally by _feed_press/_feed_release
    # regardless of whether a release EVENT is ever emitted for the control
    # (a plain single-press control with no long/double binding emits
    # nothing on release — see _feed_release — but is still tracked here),
    # so this reflects real physical button state even for controls whose
    # own event stream goes silent after the initial press.

    def held_actions(self) -> dict[str, str]:
        """Control name -> its currently-bound press action, for every
        button physically held right now (respecting the active layer and
        shift state, the same resolution a fresh press would use)."""
        return {
            name: self._resolve_button_action(name, long=False)
            for name in self._held
            if name not in (self._modifier_name, self._toggle_name)
        }

    def held_since(self, control: str) -> float | None:
        held = self._held.get(control)
        return held.pressed_at if held else None

    def _event(self, kind: EventKind, control: str, action: str, **kw) -> Event:
        return Event(kind=kind, control=control, action=action,
                     layer=self._layer, layer_label=self.layer_label, **kw)

    # -- main entry points -------------------------------------------------

    def feed(self, byte: int, now: float | None = None) -> list[Event]:
        """Consume one byte; return any events it completed."""
        now = now if now is not None else time.monotonic()

        rotary = self._keymap.rotary_index.get(byte)
        if rotary is not None:
            spec, direction = rotary
            return self._feed_rotary(spec, direction, now)

        press_spec = self._keymap.press_index.get(byte)
        if press_spec is not None:
            return self._feed_press(press_spec, now)

        release_spec = self._keymap.release_index.get(byte)
        if release_spec is not None:
            return self._feed_release(release_spec, now)

        # Log each unknown byte once. A stream of these means the keymap does
        # not match this firmware — run bin/tourbox-capture.
        if byte not in self._unknown_bytes:
            self._unknown_bytes.add(byte)
            LOG.warning(
                "unmapped byte 0x%02X — run /opt/rpi-player/bin/tourbox-capture --learn",
                byte,
            )
        return [Event(kind=EventKind.RAW, control="?", raw_byte=byte)]

    def _feed_rotary(self, spec: RotarySpec, direction: int, now: float) -> list[Event]:
        if self._modifier_down:
            self._modifier_used = True
        acc = self._pending.get(spec.name)

        # Direction reversal flushes immediately: the user changed their mind
        # and waiting would feel unresponsive.
        if acc is not None and acc.direction != direction:
            events = self._flush_rotary(spec.name, now)
            acc = None
        else:
            events = []

        if acc is None:
            self._pending[spec.name] = _RotaryAccumulator(
                spec=spec, direction=direction, count=1,
                first_at=now, last_at=now, shifted=self._modifier_down,
            )
        else:
            acc.count += 1
            acc.last_at = now
        return events

    def _feed_press(self, spec: ButtonSpec, now: float) -> list[Event]:
        name = spec.name

        # --- modifier ------------------------------------------------------
        if name == self._modifier_name:
            self._modifier_down = True
            self._modifier_used = False
            self._held[name] = _HeldButton(spec=spec, pressed_at=now)
            return []

        # Any OTHER control processed while the modifier is held counts as
        # "used as shift" — even if that control has no `shift` binding and
        # falls through to its plain action, this was still a two-button
        # chord, not a lone tap of the modifier.
        if self._modifier_down:
            self._modifier_used = True

        # --- layer toggle ---------------------------------------------------
        # Switches layer and dispatches nothing else. Handled before any
        # binding lookup so it cannot be shadowed by a stray binding.
        if name == self._toggle_name:
            self._held[name] = _HeldButton(spec=spec, pressed_at=now)
            index = self.cycle_layer()
            LOG.debug("layer -> %d (%s)", index, self.layer_label)
            return [self._event(EventKind.LAYER, name, "")]

        self._held[name] = _HeldButton(spec=spec, pressed_at=now)

        # --- double click ---------------------------------------------------
        if self._has_double(name):
            pending = self._pending_click.pop(name, None)
            if pending is not None and now - pending.at <= self._double_window:
                # Second press inside the window: fire the double action.
                return [self._event(
                    EventKind.DOUBLE, name,
                    self._binding("double", name),
                    shifted=self._modifier_down,
                )]
            self._pending_click[name] = _PendingClick(
                spec=spec, at=now, shifted=self._modifier_down)
            # If the control has NO single-press binding, waiting costs
            # nothing and we simply emit nothing now.
            return []

        # --- ordinary press -------------------------------------------------
        # No long binding means no reason to wait: fire on the press edge for
        # minimum latency.
        if not self._has_long(name):
            return [self._event(
                EventKind.PRESS, name,
                self._resolve_button_action(name, long=False),
                shifted=self._modifier_down,
            )]
        return []

    def _feed_release(self, spec: ButtonSpec, now: float) -> list[Event]:
        name = spec.name
        held = self._held.pop(name, None)

        if name == self._modifier_name:
            self._modifier_down = False
            # Flush rotaries so a detent that arrived while shifted is not
            # re-evaluated unshifted after release.
            events = self._flush_all_rotaries(now)
            # A tap — held down, released, and never used to shift another
            # control in between — fires its own `press` binding (layer-
            # specific; e.g. Side has none in Track/Folder but does in Video).
            # A real shift chord (_modifier_used) never reaches this branch.
            if not self._modifier_used:
                action = self._binding("press", name)
                if action:
                    events.append(self._event(
                        EventKind.PRESS, name, action, shifted=False))
            self._modifier_used = False
            return events

        if name == self._toggle_name:
            return []

        if held is None:
            # Release without a matching press: usually a missed byte. Ignore.
            return []

        if self._has_double(name) or not self._has_long(name) or held.long_fired:
            return []

        return [self._event(
            EventKind.PRESS, name,
            self._resolve_button_action(name, long=False),
            shifted=self._modifier_down,
        )]

    def tick(self, now: float | None = None) -> list[Event]:
        """Emit time-driven events: long presses, double-click timeouts and
        coalesced rotary bursts.

        Call this on every loop iteration, including when no byte arrived.
        """
        now = now if now is not None else time.monotonic()
        events: list[Event] = []

        # Long presses.
        for held in self._held.values():
            name = held.spec.name
            if held.long_fired or not self._has_long(name) or self._has_double(name):
                continue
            if now - held.pressed_at >= self._long_press:
                held.long_fired = True
                events.append(self._event(
                    EventKind.LONG, name,
                    self._resolve_button_action(name, long=True),
                    shifted=self._modifier_down,
                ))

        # Double-click windows that expired -> it was a single press after all.
        for name in list(self._pending_click):
            pending = self._pending_click[name]
            if now - pending.at < self._double_window:
                continue
            self._pending_click.pop(name, None)
            action = self._binding("press", name)
            if action:
                events.append(self._event(
                    EventKind.PRESS, name, action, shifted=pending.shifted))

        # Rotary bursts that have gone quiet.
        for name in list(self._pending):
            acc = self._pending[name]
            if now - acc.last_at >= self._coalesce:
                events.extend(self._flush_rotary(name, now))

        return events

    def _flush_rotary(self, name: str, now: float) -> list[Event]:
        acc = self._pending.pop(name, None)
        if acc is None or acc.count == 0:
            return []
        return [self._event(
            EventKind.ROTARY, acc.spec.name,
            self._resolve_rotary_action(acc.spec, acc.direction),
            magnitude=acc.count, direction=acc.direction,
            shifted=acc.shifted, rate=acc.rate(),
        )]

    def _flush_all_rotaries(self, now: float) -> list[Event]:
        events: list[Event] = []
        for name in list(self._pending):
            events.extend(self._flush_rotary(name, now))
        return events

    def rotary_rate(self, name: str) -> float:
        acc = self._pending.get(name)
        return acc.rate() if acc else 0.0

    def knows(self, byte: int) -> bool:
        """True if this byte is a recognised input code."""
        return (
            byte in self._keymap.press_index
            or byte in self._keymap.release_index
            or byte in self._keymap.rotary_index
        )

    def all_known(self, data: bytes) -> bool:
        """True if every byte in ``data`` is a recognised input code.

        Used to tell a legitimate burst of input from a device status frame.
        A fast rotary spin arrives as many bytes in one read, but they are all
        valid codes; a status/unlock reply contains arbitrary payload bytes
        that are not in the keymap. Length alone cannot distinguish the two.
        """
        return all(self.knows(b) for b in data)

    def reset(self) -> None:
        """Clear transient state. Call after a device reconnect.

        Without this, a button held while the cable was pulled stays 'down'
        forever and the modifier gets stuck on — which presents as every
        control doing its shifted action.

        The LAYER is deliberately preserved: a cable hiccup should not silently
        move the user to a different set of bindings.
        """
        self._held.clear()
        self._pending.clear()
        self._pending_click.clear()
        self._modifier_down = False
