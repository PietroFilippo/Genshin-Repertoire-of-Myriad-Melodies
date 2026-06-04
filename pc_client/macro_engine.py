# pc_client/macro_engine.py
"""Shared macro recording + playback core. Consumed by both the
standalone CLI (`macro_tool.py`) and the UI's `MacroController`.

The engine owns:
- The event buffer and its `{time, device, key, event_type}` shape.
- The auto-repeat suppression rule (drop consecutive `down` events
  for the same keyboard key).
- The `double → down` event-type normalization.
- Playback timing (sleep to each event's target time, cancellable
  via a `threading.Event`) and the safety sweep (release inputs
  this playback left held, plus rhythm keys + L/R mouse on exit).
- On-disk slot format `{name, events}` with bare-list legacy
  back-compat on read.
- `loaded_slot` / `dirty` bookkeeping so callers (UI) can show the
  "slot N loaded" indicator and the unsaved-changes marker.

The engine does NOT own:
- Keyboard / mouse hook installation. Both consumers install their
  own hooks with their own lifecycle (CLI: install once at startup;
  UI: install per record session) and feed filtered events in via
  `record_keyboard(...)` / `record_mouse(...)`.
- Foreground / window gating (UI-only — caller checks before
  forwarding).
- Excluded-hotkey filtering (caller's concern — which keys are
  bound to macro hotkeys differs per tool).
- The slot-picker (1-9 + ESC + 4s timer) — coupled to each tool's
  hotkey registry, kept duplicated in each tool by design.
- State machine (idle / recording / playing). Caller wraps the
  engine and tracks state however it likes.
"""
import json
import time


class MacroEngine:
    """Macro event buffer + playback. Thread-safety is the caller's
    responsibility — neither field accesses nor method calls are
    locked. The UI's `MacroController` wraps the engine in its own
    lock; the CLI single-threads everything except the playback
    worker (which writes nothing engine-side after start)."""

    def __init__(self):
        self._events = []
        self._start_ts = 0.0
        self._recording = False
        self._loaded_slot = None
        self._dirty = False

    # ---- introspection ----

    @property
    def events(self):
        """Direct reference to the buffer. Callers that need to hand
        the list across a thread boundary should copy first."""
        return self._events

    def event_count(self):
        return len(self._events)

    def is_recording(self):
        return self._recording

    def loaded_slot(self):
        return self._loaded_slot

    def is_dirty(self):
        return self._dirty

    # ---- buffer lifecycle ----

    def begin_record(self):
        """Start a recording session: clear the buffer, reset
        timestamp origin, detach from any loaded slot. Caller
        installs hooks before calling and uninstalls after
        `end_record()` — engine never touches `keyboard.hook`."""
        self._events = []
        self._loaded_slot = None
        self._dirty = False
        self._start_ts = time.time()
        self._recording = True

    def end_record(self):
        """Stop a recording session. Buffer is marked dirty if it
        contains anything so the UI shows the unsaved-new state."""
        self._recording = False
        self._dirty = bool(self._events)

    def clear(self):
        self._events = []
        self._loaded_slot = None
        self._dirty = False
        self._recording = False

    # ---- record-time event ingestion ----

    def record_keyboard(self, name, event_type):
        """Append a keyboard event if currently recording. Silently
        drops events while not recording so the caller can wire its
        hook unconditionally. Auto-repeat suppression: a `down` whose
        previous event for the same key was also `down` is dropped
        (otherwise OS auto-repeat balloons the buffer)."""
        if not self._recording:
            return
        name = (name or '').lower()
        if not name:
            return
        if event_type == 'double':
            event_type = 'down'
        if event_type == 'down' and self._last_keyboard_was_down(name):
            return
        self._events.append({
            'time': time.time() - self._start_ts,
            'device': 'keyboard',
            'key': name,
            'event_type': event_type,
        })

    def record_mouse(self, button, event_type):
        """Append a mouse-button event if currently recording.
        `double` is normalized to `down` so playback uses a plain
        click (the controller has no double-click primitive)."""
        if not self._recording:
            return
        button = (button or '').lower()
        if not button:
            return
        if event_type == 'double':
            event_type = 'down'
        self._events.append({
            'time': time.time() - self._start_ts,
            'device': 'mouse',
            'key': button,
            'event_type': event_type,
        })

    def _last_keyboard_was_down(self, name):
        for prior in reversed(self._events):
            if prior['device'] != 'keyboard' or prior['key'] != name:
                continue
            return prior['event_type'] == 'down'
        return False

    # ---- bulk editing (UI event-editor path) ----

    def replace_events(self, events):
        """Replace the buffer with a JS-edited list. Validates each
        entry — bad fields drop the event with a log line so a
        hand-edited slot can't crash playback. Sorts by `time` so
        out-of-order edits don't jumble playback ordering. Returns
        the cleaned list, or None if the top-level payload wasn't
        a list. Marks the buffer dirty."""
        if not isinstance(events, list):
            return None
        cleaned = []
        for i, ev in enumerate(events):
            if not isinstance(ev, dict):
                print(f"[macro] dropped event {i}: not an object")
                continue
            try:
                t = float(ev.get('time', 0.0))
            except (TypeError, ValueError):
                print(f"[macro] dropped event {i}: bad time")
                continue
            if t < 0:
                t = 0.0
            device = (ev.get('device') or '').lower()
            if device not in ('keyboard', 'mouse'):
                print(f"[macro] dropped event {i}: bad device {device!r}")
                continue
            key = str(ev.get('key') or '').strip().lower()
            if not key:
                print(f"[macro] dropped event {i}: empty key")
                continue
            etype = (ev.get('event_type') or '').lower()
            if etype not in ('down', 'up', 'double'):
                print(f"[macro] dropped event {i}: bad event_type {etype!r}")
                continue
            cleaned.append({
                'time': t,
                'device': device,
                'key': key,
                'event_type': etype,
            })
        cleaned.sort(key=lambda e: e['time'])
        self._events = cleaned
        self._dirty = True
        return cleaned

    # ---- playback ----

    def play(self, controller, stop_evt=None, rhythm_keys=()):
        """Iterate the buffer, sleep to each event's target time,
        dispatch to `controller`. `stop_evt` is a `threading.Event`
        consulted between events and during the sleep — set it to
        cancel. On exit (normal, cancelled, or exception) releases
        every key in `rhythm_keys` plus left/right mouse so an
        interrupted macro doesn't leave anything stuck down in-game.

        Caller manages state-machine transitions (mark playing on
        entry, idle on return)."""
        start = time.time()
        held_keys = set()
        held_mouse = set()
        try:
            for ev in self._events:
                if stop_evt is not None and stop_evt.is_set():
                    break
                target = start + ev.get('time', 0.0)
                wait = target - time.time()
                if wait > 0:
                    if stop_evt is not None:
                        if stop_evt.wait(wait):
                            break
                    else:
                        time.sleep(wait)
                device = ev.get('device', 'keyboard')
                etype = ev.get('event_type')
                key = ev.get('key', '')
                if device == 'keyboard':
                    if etype == 'down':
                        controller.key_down(key)
                        held_keys.add(key)
                    elif etype == 'up':
                        controller.key_up(key)
                        held_keys.discard(key)
                elif device == 'mouse':
                    if etype in ('down', 'double'):
                        controller.mouse_down(key)
                        held_mouse.add(key)
                    elif etype == 'up':
                        controller.mouse_up(key)
                        held_mouse.discard(key)
        finally:
            for k in list(held_keys):
                controller.key_up(k)
            for b in list(held_mouse):
                controller.mouse_up(b)
            for k in rhythm_keys:
                controller.key_up(k)
            controller.mouse_up('left')
            controller.mouse_up('right')

    @staticmethod
    def play_events(events, controller, stop_evt=None, rhythm_keys=()):
        """Play an event list without adopting it as this engine's buffer."""
        engine = MacroEngine()
        engine._events = list(events)
        engine.play(controller, stop_evt=stop_evt, rhythm_keys=rhythm_keys)

    # ---- slot I/O ----

    def save(self, path, name=''):
        """Write the buffer to `path` as `{name, events}`. Creates
        parent directories as needed. Caller is responsible for
        associating the path with a slot tag (via `mark_saved`) if
        it cares about loaded-slot tracking — engine doesn't infer
        slot numbers from paths."""
        payload = {'name': name or '', 'events': self._events}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)

    @staticmethod
    def read(path):
        """Read a slot file from `path`. Supports two on-disk shapes:
        legacy bare list `[events]` (no name) or new dict
        `{name, events}`. Returns `(events, name)`. Raises
        `ValueError` on malformed payload (top-level shape wrong or
        `events` field not a list); raises whatever `json` /
        `open` raise on I/O failure. Static so callers can read a
        slot's metadata (e.g. names for the slot grid) without
        touching the engine's buffer."""
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, ''
        if isinstance(data, dict):
            events = data.get('events', [])
            if not isinstance(events, list):
                raise ValueError("malformed events field")
            name = (data.get('name') or '').strip()
            return events, name
        raise ValueError("malformed slot payload")

    def load(self, path, slot=None):
        """Read `path` and adopt the events into the buffer. Marks
        the buffer not-dirty (matches disk). If `slot` is provided,
        records it as the loaded slot so the UI's indicator stays
        coherent. Returns the loaded `name` string (empty for
        legacy bare-list files). Propagates exceptions from
        `read()` — caller decides whether to log + recover or
        propagate to UI."""
        events, name = self.read(path)
        self._events = events
        self._loaded_slot = slot
        self._dirty = False
        return name

    # ---- slot bookkeeping ----

    def mark_saved(self, slot):
        """Buffer now matches `slot` on disk. Clears dirty, sets
        loaded_slot. Called by the caller after a successful
        `save()`."""
        self._loaded_slot = slot
        self._dirty = False

    def mark_dirty(self):
        """Mark the buffer as differing from its loaded slot. The
        engine sets this itself on `replace_events()` and at the
        end of `end_record()`; this method exists for callers that
        mutate the buffer through other paths."""
        self._dirty = True

    def detach_slot(self, slot):
        """If `slot` is the currently-loaded slot, clear the loaded
        indicator. Used when a slot file is deleted — the buffer
        contents still exist in memory but no longer correspond to
        any saved slot on disk."""
        if self._loaded_slot == slot:
            self._loaded_slot = None
