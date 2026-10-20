# pc_client/macro_engine.py
"""Shared macro recording + playback core. Consumed by both the
standalone CLI (`macro_tool.py`) and the UI's `MacroController`.

The engine owns:
- The event buffer and its `{time, device, key, event_type}` shape.
- The auto-repeat suppression rule (drop consecutive `down` events
  for the same keyboard key).
- The `double → down` event-type normalization.
- Playback routing: an Arduino backend with `play_timed_macro` may
  preload and schedule the whole event list on-device; every other
  backend keeps the host scheduler below. Both paths are cancellable
  via a `threading.Event` and share the safety sweep (release inputs
  this playback left held, plus rhythm keys + L/R mouse on exit).
- On-disk slot format `{name, events}` with bare-list legacy
  back-compat on read. Every slot write goes through
  `atomic_write_json` (temp file + fsync + `os.replace`) so a failed
  save can never leave a truncated / 0-byte slot behind.
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
- The slot-picker (slot range / bank keys + ESC + timer) — coupled to each tool's
  hotkey registry, kept duplicated in each tool by design.
- State machine (idle / recording / playing). Caller wraps the
  engine and tracks state however it likes.
"""
import json
import os
import tempfile
import time
from pathlib import Path

try:
    from system_setup import boost_timer, restore_timer
except Exception:                                   # pragma: no cover
    def boost_timer():
        return False

    def restore_timer():
        pass


# ---- atomic JSON persistence ----
#
# `os.replace` on Windows fails with PermissionError while any other
# handle has the destination open without FILE_SHARE_DELETE -- Python's
# own `open()` never passes that flag, so a concurrent reader (the UI
# drain reading slot names, an AV scanner) can make a single attempt
# fail. Retry briefly; total worst case ~0.2 s before giving up.
_REPLACE_ATTEMPTS = 10
_REPLACE_RETRY_S = 0.02


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _replace_with_retry(src, dst):
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_S)


def atomic_write_json(path, obj, **json_kwargs):
    """Write `obj` as JSON to `path` so readers only ever see the old
    file or the complete new one -- never a truncated / 0-byte file.

    Order matters:
    1. Serialize first (`json.dumps(obj, **json_kwargs)`), so a
       TypeError / ValueError from a bad object never touches disk.
    2. Write a uniquely-named temp file in the destination's own
       directory (same volume => the rename below is atomic on NTFS),
       then flush + `os.fsync`.
    3. `os.replace` the temp file onto `path` (retried briefly on
       Windows sharing violations, see `_replace_with_retry`).

    Creates the parent directory if it is missing. On any failure the
    temp file is removed and the exception re-raised; the destination
    keeps its previous contents. Returns None on success.
    """
    path = Path(path)
    text = json.dumps(obj, **json_kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        fh = os.fdopen(fd, 'w', encoding='utf-8')
    except BaseException:
        os.close(fd)
        _unlink_quietly(tmp_name)
        raise
    try:
        with fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_name, path)
    except BaseException:
        _unlink_quietly(tmp_name)
        raise


# ---- event validation (UI event-editor path) ----

# Buttons the `mouse` package (software backend + recorder) can press.
# The Arduino backend supports a subset (left/right/middle) and logs +
# skips the rest at playback time.
_MOUSE_BUTTONS = frozenset({'left', 'right', 'middle', 'x', 'x2'})


def _keyboard_package():
    """The `keyboard` package, or None when it can't be imported (e.g.
    a headless test box). Imported lazily so this module stays cheap
    to import and validation degrades to "accept as typed"."""
    try:
        import keyboard
    except Exception:
        return None
    return keyboard


def _canonical_key_name(key):
    """Resolve an editor-typed key name the same way the recorder and
    the software backend do. Returns `(canonical_name, None)` when the
    name is a single key the `keyboard` package can press, or
    `(None, reason)` when it can't.

    Why this matters: `keyboard.press('not_a_real_key')` raises AFTER
    setting the package-global replay flag, which silences every
    keyboard hotkey (including macro Stop) until restart. Canonicalizing
    (e.g. 'escape' -> 'esc', 'page_up' -> 'page up') also keeps the
    Arduino backend's key map, which only knows canonical names, in
    agreement with the software backend.

    If the `keyboard` package is unavailable or its name tables blow up
    for an unrelated reason, the name passes through unvalidated -- the
    backends already log + skip unknown keys, so dropping user data on
    a validator failure would be the worse outcome."""
    kb = _keyboard_package()
    if kb is None:
        return key, None
    try:
        canonical = str(kb.normalize_name(key)).lower()
        kb.key_to_scan_codes(canonical)
        # parse_hotkey is exactly what keyboard.press/release run, so a
        # name that passes here can't raise inside them.
        steps = kb.parse_hotkey(canonical)
    except ValueError:
        return None, 'unknown key'
    except Exception:
        return key, None
    if len(steps) != 1 or len(steps[0]) != 1:
        return None, 'key combination (use one event per key)'
    return canonical, None

# Playback timing notes -- measured on this repo, do not "simplify" back:
#
# * `threading.Event.wait()` is NOT usable as the timing primitive. It
#   resolves to WaitForSingleObject, which honours only the global OS
#   tick: ~5.5 ms overshoot per call at the Windows default, ~0.2 ms
#   with timeBeginPeriod(1). Worse, the boost stops being honoured
#   mid-run once Windows power-throttles the process (measured: first
#   ~6.5 s of an 11 s macro landed at +0.8 ms, everything after it
#   scattered to +14 ms). That is exactly the reported symptom -- the
#   same macro producing different timings run to run, cutting a
#   finisher on some runs and not others, because several margins in
#   the Mavuika combos are only 50-210 ms.
# * `time.sleep()` on Python 3.11+ Windows uses a high-resolution
#   waitable timer: ~0.3 ms overshoot with OR without the boost. So the
#   wait loop sleeps in short chunks and polls `stop_evt` between them
#   instead of waiting on it. Timing no longer depends on the OS tick.
# * The boost is still requested (cheap, ref-counted, helps the rest of
#   the process) but correctness no longer rests on it.
#
# Sleep until this close to the target, then busy-spin the remainder.
_SPIN_WINDOW = 0.002
# Longest single sleep chunk => worst-case stop latency. The app's
# overall stop-latency target is ~500 ms, so 5 ms is free.
_STOP_POLL = 0.005


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
        hand-edited slot can't crash playback. Keyboard key names must
        resolve to a single key in the `keyboard` package and are
        stored in its canonical form (see `_canonical_key_name`);
        mouse buttons must be one of left/right/middle/x/x2. Sorts by
        `time` so out-of-order edits don't jumble playback ordering.
        Returns the cleaned list, or None if the top-level payload
        wasn't a list. Marks the buffer dirty."""
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
            if device == 'keyboard':
                canonical, reason = _canonical_key_name(key)
                if canonical is None:
                    print(f"[macro] dropped event {i}: {reason} {key!r}")
                    continue
                key = canonical
            elif key not in _MOUSE_BUTTONS:
                print(f"[macro] dropped event {i}: unknown mouse button "
                      f"{key!r} (use left/right/middle/x/x2)")
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
        """Play the buffer through the best scheduler the controller
        exposes. Arduino firmware with the optional
        `play_timed_macro(events, stop_evt)` capability preloads and
        schedules the sequence on-device. If the method is absent or
        returns False (old firmware / unsupported event), this falls
        back to the existing host-side high-resolution scheduler.

        `stop_evt` is a `threading.Event` consulted while waiting — set
        it to cancel. On exit (normal, cancelled, or exception), the
        shared safety sweep releases any host-tracked inputs plus every
        key in `rhythm_keys` and left/right mouse so an interrupted
        macro doesn't leave anything stuck down in-game.

        Caller manages state-machine transitions (mark playing on
        entry, idle on return)."""
        boosted = False
        held_keys = set()
        held_mouse = set()
        cancelled = False
        try:
            timed_player = getattr(controller, 'play_timed_macro', None)
            if callable(timed_player):
                try:
                    handled = timed_player(
                        list(self._events), stop_evt=stop_evt)
                except Exception as exc:
                    # An optional acceleration must never make macro
                    # playback less compatible. The concrete Arduino
                    # adapter normally swallows transport failures, but
                    # third-party adapters may not.
                    print(f"[macro] timed backend failed; using host "
                          f"scheduler: {exc}")
                    handled = False
                if handled:
                    return

            boosted = boost_timer()
            # perf_counter, not time(): time() is wall-clock and also has
            # ~15.6 ms resolution on Windows, so the wait computation itself
            # was coarse. perf_counter is monotonic and high-resolution.
            start = time.perf_counter()
            for ev in self._events:
                if stop_evt is not None and stop_evt.is_set():
                    break
                target = start + ev.get('time', 0.0)
                # Sleep to just short of the target in short chunks,
                # then busy-spin the remainder. See the timing notes at
                # the top of the module for why this is not Event.wait.
                while True:
                    remaining = target - time.perf_counter()
                    if remaining <= 0:
                        break
                    if remaining > _SPIN_WINDOW:
                        time.sleep(min(remaining - _SPIN_WINDOW, _STOP_POLL))
                    if stop_evt is not None and stop_evt.is_set():
                        cancelled = True
                        break
                if cancelled:
                    break
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
            if boosted:
                restore_timer()
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
        slot numbers from paths.

        Failure contract (unchanged API, now safe on disk): returns
        None on success and RAISES on failure — OSError from the
        filesystem, TypeError / ValueError if an event isn't
        JSON-serializable. The write is atomic (`write_slot`), so on
        failure the previous slot file is left intact; callers must
        not `mark_saved` after an exception."""
        self.write_slot(path, self._events, name)

    @staticmethod
    def write_slot(path, events, name=''):
        """Atomically write `{name, events}` to `path` (see
        `atomic_write_json`). Single home for every slot write so
        save / rename / any future path can't reintroduce a
        truncate-then-write. Raises on failure; the destination keeps
        its previous contents."""
        payload = {'name': name or '', 'events': events}
        atomic_write_json(Path(path), payload, indent=2)

    @classmethod
    def rename_slot(cls, path, name):
        """Rewrite slot `path` with a new `name`, keeping its events
        (a legacy bare-list file is upgraded to `{name, events}`).
        Raises like `read()` (missing / malformed file) and
        `write_slot()` (write failure — old file intact)."""
        events, _old_name = cls.read(Path(path))
        cls.write_slot(path, events, name)

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
