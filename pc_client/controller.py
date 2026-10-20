# pc_client/controller.py
import ctypes
import ctypes.wintypes
import math
import serial
from serial.tools import list_ports
import time
import threading
import sys
from config import SERIAL_PORT, BAUD_RATE

# Known USB VIDs for Arduino-compatible ATmega32u4 boards capable of HID.
# Match priority is list order: genuine Arduino first, then Sparkfun Pro
# Micro, then CH340-based clones (which advertise a generic Qinheng VID).
_KNOWN_VIDS = [
    0x2341,  # Arduino LLC (Leonardo, Micro)
    0x2A03,  # Arduino SRL (post-split clone of 0x2341)
    0x1B4F,  # Sparkfun Pro Micro
    0x1A86,  # QinHeng CH340 — common on cheap clones
    0x10C4,  # Silicon Labs CP210x — some clones
]

# Description-substring fallback when VID isn't recognized. Match is
# case-insensitive on the port's `description` (e.g. "Arduino Leonardo
# (COM7)" on Windows).
_DESCRIPTION_HINTS = ('arduino', 'leonardo', 'pro micro')


def _auto_detect_port():
    """Find the first plausible Arduino COM port. Returns device name
    (e.g. 'COM7') or None if no candidate found. Prefers VID match; falls
    back to description-substring match."""
    ports = list(list_ports.comports())
    # VID match — strongest signal.
    for vid in _KNOWN_VIDS:
        for p in ports:
            if p.vid == vid:
                print(f"Auto-detected Arduino on {p.device} "
                      f"(VID=0x{p.vid:04X}, '{p.description}')")
                return p.device
    # Description fallback for boards with unknown VIDs.
    for p in ports:
        desc = (p.description or '').lower()
        if any(hint in desc for hint in _DESCRIPTION_HINTS):
            print(f"Auto-detected Arduino on {p.device} "
                  f"(description match: '{p.description}')")
            return p.device
    if ports:
        print("No Arduino-like port matched. Available ports:")
        for p in ports:
            vid = f"0x{p.vid:04X}" if p.vid is not None else '?'
            print(f"  {p.device}  VID={vid}  '{p.description}'")
    else:
        print("No serial ports found at all.")
    return None

# Arduino Keyboard.h modifiers + non-printing keys. Names are the
# `keyboard` package's canonical names (what the recorder stores and
# what MacroEngine.replace_events canonicalizes editor input to).
# Keyboard.press() treats 128-135 as modifier bits and any code >= 136
# as raw HID usage (code - 136), so e.g. KEY_F1 0xC2 -> usage 0x3A (F1).
# The firmware forwards K:<code> and timed E:<code> unchanged to
# Keyboard.press, so the realtime path and the scheduled-macro compiler
# share this one table via _key_code(). Names missing here are logged
# and skipped by key_down/key_up (and force the host scheduler for the
# timed path) rather than sent as a wrong key.
KEY_MAP = {
    'ctrl': 128, 'left ctrl': 128, 'right ctrl': 128,
    'shift': 129, 'left shift': 129, 'right shift': 129,
    'alt': 130, 'left alt': 130, 'right alt': 130,
    'windows': 131, 'left windows': 131,                  # KEY_LEFT_GUI
    'right windows': 135,                                 # KEY_RIGHT_GUI
    'up': 218, 'down': 217, 'left': 216, 'right': 215,
    'backspace': 178, 'tab': 179, 'enter': 176, 'esc': 177,
    'space': 32,
    'insert': 0xD1, 'home': 0xD2, 'page up': 0xD3,
    'delete': 0xD4, 'end': 0xD5, 'page down': 0xD6,
    'caps lock': 0xC1,
    'print screen': 0xCE, 'scroll lock': 0xCF, 'pause': 0xD0,
    'num lock': 0xDB, 'menu': 0xED,
    # KEY_F1..KEY_F12 = 0xC2..0xCD; KEY_F13..KEY_F24 = 0xF0..0xFB.
    **{f'f{n}': 0xC2 + n - 1 for n in range(1, 13)},
    **{f'f{n}': 0xF0 + n - 13 for n in range(13, 25)},
}

# Arduino Mouse.h only reports three buttons (MOUSE_LEFT/RIGHT/MIDDLE).
# Side buttons (`x` / `x2`) can't be represented: they are logged and
# skipped, never remapped to a left click.
MOUSE_MAP = {
    'left': 1, 'right': 2, 'middle': 4
}

# Optional scheduled-macro protocol (firmware v2+). The ordinary K/M/P
# commands remain unchanged for rhythm + album mode; only MacroEngine probes
# and uses this extension. A packed event is represented on the wire as a
# text line but stored by the Leonardo as {uint32 milliseconds, uint8 op,
# uint8 code} (6 bytes). Upload happens before playback, so wire efficiency
# does not affect the combo's timing. Firmware v3 adds a per-event lateness
# trace emitted only after playback has ended; v2 remains playback-compatible.
_TIMED_PROTOCOL_MIN_VERSION = 2
_TIMED_PROTOCOL_VERSION = 3
_TIMED_KEY_DOWN = 1
_TIMED_KEY_UP = 2
_TIMED_MOUSE_DOWN = 3
_TIMED_MOUSE_UP = 4
_TIMED_MAX_DURATION_MS = 30 * 60 * 1000

# Community frame analysis places a Mavuika Flamestrider CA hit around 555 ms
# after its attack-down. This is diagnostic only: it never changes playback.
_MAVUIKA_CA_HIT_MS = 555


def _key_code(key):
    """Keyboard.h code for `key`, or 0 when the Arduino can't type it.
    Single characters go through Keyboard.press's US-ASCII map, so only
    7-bit ASCII is accepted — a code >= 136 would be read as a raw HID
    usage (e.g. 'ç' = 231 -> keypad 7), i.e. silently the wrong key."""
    k = str(key or '').lower()
    if len(k) == 1:
        code = ord(k)
        return code if 0 < code < 128 else 0
    return KEY_MAP.get(k, 0)


def _crc16_update(crc, value):
    """CRC-16/CCITT-FALSE byte update, mirrored in the sketch."""
    crc ^= (value & 0xFF) << 8
    for _ in range(8):
        if crc & 0x8000:
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF
    return crc


def _compile_timed_macro_events(events):
    """Convert MacroEngine events to the firmware's compact shape.

    Returns ``(packed_events, crc16)`` where every packed event is
    ``(at_ms, op, code)``. Raises ValueError when an event cannot be
    represented. The caller treats that as a compatibility fallback to the
    normal host scheduler, so adding the optional protocol never removes a
    macro feature.
    """
    packed = []
    crc = 0xFFFF
    for index, event in enumerate(events):
        try:
            seconds = float(event.get('time', 0.0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"event {index} has a bad timestamp") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"event {index} has a bad timestamp")
        at_ms = int(seconds * 1000.0 + 0.5)
        if at_ms > _TIMED_MAX_DURATION_MS:
            raise ValueError(f"event {index} exceeds the 30-minute limit")

        device = str(event.get('device', 'keyboard')).lower()
        event_type = str(event.get('event_type', '')).lower()
        key = str(event.get('key', '')).lower()
        if device == 'keyboard':
            # An editor-entered keyboard `double` is a press, exactly like
            # a mouse `double` below and like MacroEngine's host scheduler.
            code = _key_code(key)
            if (not code or code > 255
                    or event_type not in ('down', 'up', 'double')):
                raise ValueError(f"event {index} is not firmware-compatible")
            op = (_TIMED_KEY_UP if event_type == 'up'
                  else _TIMED_KEY_DOWN)
        elif device == 'mouse':
            # x / x2 have no MOUSE_MAP entry -> rejected here, so the
            # host scheduler runs the macro and mouse_down/up log + skip
            # them instead of the board pressing some other button.
            code = MOUSE_MAP.get(key, 0)
            if not code or event_type not in ('down', 'up', 'double'):
                raise ValueError(f"event {index} is not firmware-compatible")
            op = (_TIMED_MOUSE_UP if event_type == 'up'
                  else _TIMED_MOUSE_DOWN)
        else:
            raise ValueError(f"event {index} is not firmware-compatible")

        packed.append((at_ms, op, code))
        for shift in (0, 8, 16, 24):
            crc = _crc16_update(crc, at_ms >> shift)
        crc = _crc16_update(crc, op)
        crc = _crc16_update(crc, code)
    return packed, crc


def _parse_event_lateness_trace(line, expected_count):
    """Parse firmware-v3 ``T:<count>:<late_us>...`` telemetry."""
    parts = str(line or '').split(':')
    if len(parts) < 2 or parts[0] != 'T':
        return None
    try:
        reported_count = int(parts[1])
        values = [int(value) for value in parts[2:]]
    except ValueError:
        return None
    if (reported_count != expected_count or len(values) != expected_count
            or any(value < 0 or value > 0xFFFF for value in values)):
        return None
    return values


def _is_event(event, device, key, event_type):
    return (
        str(event.get('device', '')).lower() == device
        and str(event.get('key', '')).lower() == key
        and str(event.get('event_type', '')).lower() == event_type
    )


def _event_ms(event):
    return int(float(event.get('time', 0.0)) * 1000.0 + 0.5)


def _build_cdc_cycle_diagnostics(events, event_late_us=None):
    """Find Mavuika double-CDC pairs and calculate their timing margins.

    The detector is intentionally narrow: a CDC is an LMB-down followed by a
    left-Shift-down within 250 ms, and a pair is two such cancels whose dash
    inputs are 300-550 ms apart. It therefore ignores hands-free double taps.
    Returned event indices map directly to firmware-v3 lateness entries.
    """
    events = list(events)
    if event_late_us is not None and len(event_late_us) != len(events):
        event_late_us = None

    attack_downs = [
        index for index, event in enumerate(events)
        if _is_event(event, 'mouse', 'left', 'down')
    ]
    cancels = []
    for dash_index, dash_event in enumerate(events):
        if not _is_event(dash_event, 'keyboard', 'left shift', 'down'):
            continue
        dash_ms = _event_ms(dash_event)
        candidates = [
            index for index in attack_downs
            if index < dash_index
            and 0 <= dash_ms - _event_ms(events[index]) <= 250
        ]
        if not candidates:
            continue
        attack_index = candidates[-1]

        next_attack_down = next(
            (index for index in attack_downs if index > attack_index),
            len(events),
        )
        attack_up_index = next(
            (
                index for index in range(dash_index + 1, next_attack_down)
                if _is_event(events[index], 'mouse', 'left', 'up')
            ),
            None,
        )
        dash_up_index = next(
            (
                index for index in range(dash_index + 1, len(events))
                if _is_event(
                    events[index], 'keyboard', 'left shift', 'up'
                )
            ),
            None,
        )
        if attack_up_index is None or dash_up_index is None:
            continue
        cancels.append({
            'attack': attack_index,
            'dash': dash_index,
            'dash_up': dash_up_index,
            'attack_up': attack_up_index,
        })

    q_ms = next(
        (
            _event_ms(event) for event in events
            if _is_event(event, 'keyboard', 'q', 'down')
        ),
        None,
    )
    hold_e = None
    for down_index, event in enumerate(events):
        if not _is_event(event, 'keyboard', 'e', 'down'):
            continue
        down_ms = _event_ms(event)
        if q_ms is not None and down_ms <= q_ms:
            continue
        up_index = next(
            (
                index for index in range(down_index + 1, len(events))
                if _is_event(events[index], 'keyboard', 'e', 'up')
            ),
            None,
        )
        if up_index is not None and _event_ms(events[up_index]) - down_ms >= 250:
            hold_e = (down_ms, _event_ms(events[up_index]))
            break

    cycles = []
    pre_he_number = 0
    post_he_number = 0
    previous_pair_d2_ms = None
    index = 0
    while index + 1 < len(cancels):
        first = cancels[index]
        second = cancels[index + 1]
        d1_ms = _event_ms(events[first['dash']])
        d2_ms = _event_ms(events[second['dash']])
        dash_gap_ms = d2_ms - d1_ms
        if not 300 <= dash_gap_ms <= 550:
            index += 1
            continue

        c1_ms = _event_ms(events[first['attack']])
        c2_ms = _event_ms(events[second['attack']])
        c1_up_ms = _event_ms(events[first['attack_up']])
        c2_up_ms = _event_ms(events[second['attack_up']])
        if q_ms is not None and d2_ms < q_ms:
            label = 'opener'
        elif hold_e is not None and d2_ms < hold_e[0]:
            pre_he_number += 1
            label = f'pre-hE {pre_he_number}'
        elif hold_e is not None and d1_ms > hold_e[1]:
            post_he_number += 1
            label = f'post-hE {post_he_number}'
        else:
            label = f'cycle {len(cycles) + 1}'

        actual_c1_to_d2_ms = None
        late = None
        if event_late_us is not None:
            late = {
                'c1': event_late_us[first['attack']],
                'd1': event_late_us[first['dash']],
                'c1_up': event_late_us[first['attack_up']],
                'c2': event_late_us[second['attack']],
                'd2': event_late_us[second['dash']],
                'c2_up': event_late_us[second['attack_up']],
            }
            actual_c1_to_d2_ms = (
                d2_ms - c1_ms + (late['d2'] - late['c1']) / 1000.0
            )

        cycles.append({
            'label': label,
            'c1_ms': c1_ms,
            'd1_ms': d1_ms,
            'c1_up_ms': c1_up_ms,
            'c2_ms': c2_ms,
            'd2_ms': d2_ms,
            'c2_up_ms': c2_up_ms,
            'c1_to_d1_ms': d1_ms - c1_ms,
            'c1_hold_ms': c1_up_ms - c1_ms,
            'c2_to_d2_ms': d2_ms - c2_ms,
            'c2_hold_ms': c2_up_ms - c2_ms,
            'd1_to_d2_ms': dash_gap_ms,
            'entry_from_previous_d2_ms': (
                None if previous_pair_d2_ms is None
                else d1_ms - previous_pair_d2_ms
            ),
            'c1_to_d2_ms': d2_ms - c1_ms,
            'estimated_hit_clearance_ms': (
                d2_ms - c1_ms - _MAVUIKA_CA_HIT_MS
            ),
            'actual_c1_to_d2_ms': actual_c1_to_d2_ms,
            'late_us': late,
        })
        previous_pair_d2_ms = d2_ms
        index += 2
    return cycles


def _log_cdc_cycle_diagnostics(events, event_late_us=None):
    cycles = _build_cdc_cycle_diagnostics(events, event_late_us)
    if not cycles:
        return
    if event_late_us is None:
        print("[macro-cdc] per-event board timing unavailable; firmware v3 "
              "is required (playback timing is unchanged)")
    for cycle in cycles:
        print(
            f"[macro-cdc] {cycle['label']} plan: "
            f"C1/D1/C1up/C2/D2/C2up="
            f"{cycle['c1_ms']}/{cycle['d1_ms']}/{cycle['c1_up_ms']}/"
            f"{cycle['c2_ms']}/{cycle['d2_ms']}/{cycle['c2_up_ms']}ms; "
            f"C1-D1={cycle['c1_to_d1_ms']}ms, "
            f"C1hold={cycle['c1_hold_ms']}ms, "
            f"D1-D2={cycle['d1_to_d2_ms']}ms, "
            f"C1-D2={cycle['c1_to_d2_ms']}ms, "
            f"est_hit_margin={cycle['estimated_hit_clearance_ms']:+d}ms"
            + (
                "" if cycle['entry_from_previous_d2_ms'] is None
                else f", prevD2-D1={cycle['entry_from_previous_d2_ms']}ms"
            )
        )
        if cycle['late_us'] is not None:
            late = cycle['late_us']
            actual_margin = (
                cycle['actual_c1_to_d2_ms'] - _MAVUIKA_CA_HIT_MS
            )
            print(
                f"[macro-cdc] {cycle['label']} board_late_us: "
                f"C1={late['c1']}, D1={late['d1']}, "
                f"C1up={late['c1_up']}, C2={late['c2']}, "
                f"D2={late['d2']}, C2up={late['c2_up']}; "
                f"actual_C1-D2={cycle['actual_c1_to_d2_ms']:.3f}ms, "
                f"actual_hit_margin={actual_margin:+.3f}ms"
            )


class ArduinoConnectionError(RuntimeError):
    """The Arduino link could not be established. `str(exc)` is a
    user-facing message (no board found / port couldn't be opened /
    connection cancelled) suitable for showing as-is in the UI."""


# Consecutive write timeouts tolerated before the link is declared dead.
# A single timeout can be transient (the sketch briefly not draining its
# CDC buffer); several in a row with no successful write in between mean
# the board has hung or vanished, and each costs a 0.5 s stall.
_MAX_CONSECUTIVE_WRITE_TIMEOUTS = 3


def _open_failure_message(port, exc):
    reason = str(exc) or exc.__class__.__name__
    # pyserial already prefixes "could not open port 'COM7': " — drop it
    # so the message doesn't name the port twice.
    prefix = f"could not open port {port!r}: "
    if reason.startswith(prefix):
        reason = reason[len(prefix):]
    message = f"Couldn't open {port}: {reason}"
    if isinstance(exc, PermissionError) or 'denied' in reason.lower():
        message += (" (is the Arduino IDE serial monitor or another app "
                    "using the port?)")
    return message


class ArduinoHIDController:
    # Link-health state. Class-level defaults (all immutable) so test
    # doubles built with `__new__` behave like a freshly opened link.
    # `_link_failed` latches True on the first hard serial error (or on
    # close()) and never resets: a Windows COM handle doesn't come back
    # after the board is unplugged, even if it re-enumerates on the same
    # port.
    #
    # Both fields are only ever changed while holding `_write_lock`, in
    # the same critical section as the port I/O they describe. The timeout
    # run must follow the order writes actually hit the port: counting
    # after releasing the lock let six detector threads interleave
    # (timeout -> another thread's success -> timeout -> timeout read as
    # 3 in a row), latching a live link and swallowing the key-ups that
    # would have released held keys. Logging happens after release.
    _link_failed = False
    _write_timeouts = 0

    def __init__(self, cancel_evt=None):
        """Open the serial link to the Leonardo.

        Raises `ArduinoConnectionError` (user-facing message) when no
        board is found, the port can't be opened, or `cancel_evt` (an
        optional `threading.Event`) is set before / during the 2 s
        post-open settle wait — in which case the port is closed again
        before raising. Never calls sys.exit: UI worker threads catch
        the exception and surface the message."""
        # Resolve port: explicit config value wins; None/empty triggers
        # auto-detection. Auto-detect failure raises — no port = no bot.
        if SERIAL_PORT:
            self.port = SERIAL_PORT
        else:
            detected = _auto_detect_port()
            if not detected:
                raise ArduinoConnectionError(
                    "No Arduino found — plug in the Leonardo or switch "
                    "Input to Software")
            self.port = detected
        self.baud = BAUD_RATE
        self.ser = None
        # Serialize all writes from worker threads. Concurrent writes to the
        # same Windows COM port cause race conditions and Write timeouts.
        self._write_lock = threading.Lock()
        # Only one scheduled macro may own the upload/response channel.
        # BotController and MacroController already exclude each other, but
        # this keeps the adapter safe for direct/CLI consumers as well.
        self._macro_lock = threading.Lock()
        self._rx_buffer = bytearray()
        self._pending_response_lines = []
        self._timed_macro_supported = None
        self._timed_macro_protocol_version = 0
        self._timed_macro_max_events = 0
        self._loaded_timed_signature = None
        self._unsupported_warned = set()

        if cancel_evt is not None and cancel_evt.is_set():
            raise ArduinoConnectionError("Arduino connection cancelled")
        print(f"Connecting to Arduino on {self.port} at {self.baud} baud")
        try:
            # Explicit write_timeout so a stalled write fails fast instead of
            # blocking other commands behind it for unbounded time.
            self.ser = serial.Serial(self.port, self.baud,
                                     timeout=1, write_timeout=0.5)
        except Exception as e:
            raise ArduinoConnectionError(
                _open_failure_message(self.port, e)) from e
        # Allow arduino leonardo time to reset and initialize serial.
        # Cancellable when the caller passes an event (UI Stop during the
        # first connection) — a bare sleep pinned Stop for the full 2 s.
        if cancel_evt is not None:
            cancelled = cancel_evt.wait(2.0)
        else:
            time.sleep(2)
            cancelled = False
        if cancelled:
            self._close_port_quietly()
            raise ArduinoConnectionError("Arduino connection cancelled")
        print("Connected successfully")

    # ---- link health ----

    @property
    def healthy(self):
        """True while the serial link is usable: the port opened, is
        still open, and no read/write has hit a hard serial error.

        Latches False (see `_link_failed`) after the first hard
        SerialException / OSError on a read or write, after
        `_MAX_CONSECUTIVE_WRITE_TIMEOUTS` write timeouts in a row, or
        once `close()` has run. The owner should then close() this
        instance and build a new one. Reading the property also does a
        cheap non-blocking probe (`in_waiting` -> ClearCommError) so a
        board unplugged while idle is noticed before the next run
        starts; the probe is skipped if a writer currently holds the
        lock (that writer will notice the failure itself)."""
        if self._link_failed:
            return False
        ser = getattr(self, 'ser', None)
        try:
            if ser is None or not ser.is_open:
                return False
        except Exception:
            return False
        lock = getattr(self, '_write_lock', None)
        if lock is not None and lock.acquire(blocking=False):
            failure = None
            latched = False
            try:
                if not self._link_failed:
                    try:
                        ser.in_waiting
                    except (serial.SerialException, OSError) as exc:
                        failure = exc
                        latched = self._latch_link_failed_locked()
            finally:
                lock.release()
            if latched:
                self._log_link_failure('port check', failure)
        return not self._link_failed

    def _latch_link_failed_locked(self):
        """Latch the link as dead. Caller MUST hold `_write_lock`.
        Returns True only for the call that flipped the latch, so exactly
        one caller logs it (via `_log_link_failure`, after releasing the
        lock). Every later write is skipped (see `_write_line`) instead
        of re-failing — and re-logging — through the dead handle on
        every keystroke."""
        if self._link_failed:
            return False
        self._link_failed = True
        return True

    @staticmethod
    def _log_link_failure(context, exc):
        print(f"[serial] {context} failed: {exc}")
        print("[serial] Arduino disconnected — stop and press Start again "
              "to reconnect")

    def _close_port_quietly(self):
        """Close the port without raising. Returns True if `close()`
        completed, False if there was no port or it raised."""
        ser = getattr(self, 'ser', None)
        if ser is None:
            return False
        try:
            ser.close()
            return True
        except Exception as e:
            print(f"[serial] close failed: {e}")
            return False

    def _write_line(self, cmd):
        """Write one protocol line. Returns write duration in seconds or
        None on failure. Serial write completion is useful transport
        telemetry, but scheduled playback never treats it as the event's
        execution time — the Leonardo owns that clock."""
        if self._link_failed:
            return None
        ser = self.ser
        if not ser or not ser.is_open:
            return None
        payload = f"{cmd}\n".encode('utf-8')
        timeout_exc = hard_exc = None
        latched = False
        started = time.perf_counter()
        with self._write_lock:
            # Re-check under the lock: another writer may have latched
            # the link while this one was queued behind it.
            if self._link_failed:
                return None
            try:
                ser.write(payload)
            except serial.SerialTimeoutException as e:
                # Don't let a transient write failure crash a worker thread.
                # A run of them (no success in between) means the board is
                # gone or hung — latch the link dead instead of stalling
                # 0.5 s per write forever.
                timeout_exc = e
                self._write_timeouts += 1
                timeouts = self._write_timeouts
                if timeouts >= _MAX_CONSECUTIVE_WRITE_TIMEOUTS:
                    latched = self._latch_link_failed_locked()
            except (serial.SerialException, OSError) as e:
                # Hard failure (unplugged, handle invalid): never transient.
                hard_exc = e
                latched = self._latch_link_failed_locked()
            else:
                if self._write_timeouts:
                    self._write_timeouts = 0
                return time.perf_counter() - started
        # Failure path only — log outside the lock so other writers aren't
        # held up behind console / UI-queue output.
        if timeout_exc is not None:
            print(f"[serial] write failed for {cmd!r}: {timeout_exc}")
            if latched:
                self._log_link_failure(f"{timeouts} consecutive writes",
                                       timeout_exc)
        elif latched:
            self._log_link_failure(f"write {cmd!r}", hard_exc)
        return None

    def _send_command(self, cmd):
        """Internal method to write a legacy K/M/P command."""
        self._write_line(cmd)

    def _read_response_lines(self):
        """Read complete firmware response lines without blocking."""
        if self._link_failed:
            return []
        ser = self.ser
        if not ser or not ser.is_open:
            return []
        failure = None
        latched = False
        with self._write_lock:
            if self._link_failed:
                return []
            try:
                waiting = ser.in_waiting
                chunk = ser.read(waiting) if waiting else b''
            except (serial.SerialException, OSError) as exc:
                failure = exc
                latched = self._latch_link_failed_locked()
        if failure is not None:
            if latched:
                self._log_link_failure('read', failure)
            return []
        if chunk:
            self._rx_buffer.extend(chunk)
        lines = self._pending_response_lines
        self._pending_response_lines = []
        while b'\n' in self._rx_buffer:
            raw, _, remainder = self._rx_buffer.partition(b'\n')
            self._rx_buffer = bytearray(remainder)
            line = raw.rstrip(b'\r').decode('ascii', errors='replace')
            if line:
                lines.append(line)
        return lines

    def _wait_for_response(self, prefixes, timeout, stop_evt=None):
        """Wait for a response whose text starts with one of `prefixes`.
        Unrelated diagnostic lines are logged and skipped."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            lines = self._read_response_lines()
            for index, line in enumerate(lines):
                if any(line.startswith(prefix) for prefix in prefixes):
                    # Preserve responses that shared the same USB packet but
                    # followed the one this waiter wanted (notably S:OK + D
                    # for a zero-duration diagnostic macro).
                    if index + 1 < len(lines):
                        self._pending_response_lines = (
                            lines[index + 1:] + self._pending_response_lines)
                    return line
                print(f"[serial-timed] firmware: {line}")
            if stop_evt is not None and stop_evt.is_set():
                return None
            time.sleep(0.002)
        return None

    def _probe_timed_macro_support(self):
        """Probe once per connection. Old sketches silently ignore `V`,
        after which MacroEngine keeps using its host scheduler."""
        if self._timed_macro_supported is not None:
            return self._timed_macro_supported
        self._rx_buffer.clear()
        self._pending_response_lines = []
        try:
            with self._write_lock:
                self.ser.reset_input_buffer()
        except (serial.SerialException, OSError):
            pass
        if self._write_line('V') is None:
            self._timed_macro_supported = False
            return False
        line = self._wait_for_response(('V:',), 0.30)
        if line:
            parts = line.split(':')
            try:
                version = int(parts[1])
                feature = parts[2]
                max_events = int(parts[3])
            except (IndexError, TypeError, ValueError):
                version = 0
                feature = ''
                max_events = 0
            if (_TIMED_PROTOCOL_MIN_VERSION <= version
                    <= _TIMED_PROTOCOL_VERSION and feature == 'TM'
                    and max_events > 0):
                self._timed_macro_supported = True
                self._timed_macro_protocol_version = version
                self._timed_macro_max_events = max_events
                print(f"[serial-timed] firmware v{version}, capacity "
                      f"{max_events} events")
                return True
        self._timed_macro_supported = False
        print("[serial-timed] firmware has no scheduled-macro support; "
              "using host scheduler")
        return False

    def _upload_timed_macro(self, packed, crc, stop_evt):
        """Upload and commit one checked event buffer."""
        started = time.perf_counter()
        write_times = []
        elapsed = self._write_line(f"L:{len(packed)}:{crc}")
        if elapsed is None:
            return False
        write_times.append(elapsed)
        ready = self._wait_for_response(('L:OK', 'X:'), 0.75, stop_evt)
        if ready != 'L:OK':
            print(f"[serial-timed] load rejected: {ready or 'timeout'}")
            return False
        for at_ms, op, code in packed:
            if stop_evt is not None and stop_evt.is_set():
                return False
            elapsed = self._write_line(f"E:{at_ms}:{op}:{code}")
            if elapsed is None:
                return False
            write_times.append(elapsed)
        elapsed = self._write_line('C')
        if elapsed is None:
            return False
        write_times.append(elapsed)
        response = self._wait_for_response(('R:', 'X:'), 3.0, stop_evt)
        expected = f"R:{len(packed)}:{crc}"
        if response != expected:
            print(f"[serial-timed] commit failed: "
                  f"{response or 'timeout'} (expected {expected})")
            return False
        total_ms = (time.perf_counter() - started) * 1000.0
        max_write_ms = max(write_times, default=0.0) * 1000.0
        print(f"[serial-timed] uploaded {len(packed)} events in "
              f"{total_ms:.1f} ms (slowest write {max_write_ms:.2f} ms, "
              f"crc {crc})")
        return True

    def _abort_timed_macro(self):
        if self._write_line('A') is None:
            return
        self._wait_for_response(('A:OK',), 0.25)

    def play_timed_macro(self, events, stop_evt=None):
        """Optional MacroEngine acceleration implemented by firmware v2.

        Returns True once this adapter has taken ownership of playback,
        including cancellation or a failure after START. Returns False only
        when it is safe for MacroEngine to replay the sequence with its host
        scheduler (old firmware, unsupported event, or pre-start upload
        failure).
        """
        with self._macro_lock:
            if not self._probe_timed_macro_support():
                return False
            try:
                packed, crc = _compile_timed_macro_events(events)
            except ValueError as exc:
                print(f"[serial-timed] {exc}; using host scheduler")
                return False
            if len(packed) > self._timed_macro_max_events:
                print(f"[serial-timed] macro has {len(packed)} events but "
                      f"firmware holds {self._timed_macro_max_events}; using "
                      "host scheduler")
                return False
            if not packed:
                return True
            if stop_evt is not None and stop_evt.is_set():
                return True

            signature = (len(packed), crc)
            if signature != self._loaded_timed_signature:
                # Forget the old buffer BEFORE the first replacement byte
                # goes out. A replacement that fails after L: (or whose
                # R: ack is lost to a timeout/stop) may leave the board
                # holding nothing, a partial load, or even the NEW buffer
                # committed. Keeping the old signature would let the next
                # request for the old macro skip its upload and START
                # whatever the board holds. The signature is only set
                # again once the commit R:<count>:<crc> ack is verified.
                self._loaded_timed_signature = None
                if not self._upload_timed_macro(packed, crc, stop_evt):
                    return False
                self._loaded_timed_signature = signature

            # From the instant S is written, falling back would risk playing
            # the macro twice. Every exit after this point therefore returns
            # True and issues ABORT on uncertainty.
            if self._write_line('S') is None:
                self._abort_timed_macro()
                return True
            response = self._wait_for_response(('S:OK', 'X:'), 0.50,
                                               stop_evt)
            if response != 'S:OK':
                self._abort_timed_macro()
                if response == 'X:NOLOAD':
                    self._loaded_timed_signature = None
                print(f"[serial-timed] start failed: "
                      f"{response or 'timeout'}")
                return True

            duration_s = packed[-1][0] / 1000.0
            deadline = time.perf_counter() + duration_s + 2.0
            event_late_us = None
            while time.perf_counter() < deadline:
                if stop_evt is not None and stop_evt.is_set():
                    self._abort_timed_macro()
                    print("[serial-timed] playback aborted")
                    return True
                for line in self._read_response_lines():
                    if line.startswith('T:'):
                        parsed = _parse_event_lateness_trace(
                            line,
                            len(packed),
                        )
                        if parsed is None:
                            print("[serial-timed] malformed per-event "
                                  f"telemetry: {line}")
                        else:
                            event_late_us = parsed
                        continue
                    if line.startswith('D:'):
                        parts = line.split(':')
                        if len(parts) == 7:
                            print("[serial-timed] done: "
                                  f"events={parts[1]}, max_late={parts[2]}us, "
                                  f"avg_late={parts[3]}us, >1ms={parts[4]}, "
                                  f">5ms={parts[5]}, runtime={parts[6]}ms")
                        else:
                            print(f"[serial-timed] done: {line}")
                        _log_cdc_cycle_diagnostics(events, event_late_us)
                        return True
                    if line.startswith('X:'):
                        print(f"[serial-timed] firmware error: {line}")
                        self._abort_timed_macro()
                        return True
                    print(f"[serial-timed] firmware: {line}")
                time.sleep(0.002)

            print("[serial-timed] completion acknowledgement timed out; "
                  "aborting for safety")
            self._abort_timed_macro()
            return True

    def _get_key_code(self, key):
        return _key_code(key)

    def _warn_unsupported(self, kind, name):
        """Log an input the board can't produce, once per name per
        connection, so a macro that repeats it doesn't flood the log."""
        warned = self.__dict__.setdefault('_unsupported_warned', set())
        tag = (kind, str(name))          # str(): never raise on odd input
        if tag in warned:
            return
        warned.add(tag)
        print(f"[serial] {kind} {name!r} isn't supported by the Arduino "
              "backend")

    def key_down(self, key):
        """Press a key down"""
        code = self._get_key_code(key)
        if code:
            self._send_command(f"K:{code}:DOWN")
        else:
            self._warn_unsupported('key', key)

    def key_up(self, key):
        """Release a key"""
        code = self._get_key_code(key)
        if code:
            self._send_command(f"K:{code}:UP")
        else:
            self._warn_unsupported('key', key)

    def mouse_down(self, button):
        """Press a mouse button. Unsupported buttons (x / x2) are logged
        and skipped — never remapped to a left click."""
        name = str(button or '').lower()
        code = MOUSE_MAP.get(name, 0)
        if code:
            self._send_command(f"M:{code}:DOWN")
        else:
            self._warn_unsupported('mouse button', name)

    def mouse_up(self, button):
        """Release a mouse button (unsupported buttons: see mouse_down)."""
        name = str(button or '').lower()
        code = MOUSE_MAP.get(name, 0)
        if code:
            self._send_command(f"M:{code}:UP")
        else:
            self._warn_unsupported('mouse button', name)

    def mouse_move(self, dx, dy):
        """Move the mouse pointer by (dx, dy) pixels via HID. Genshin's
        anti-cheat ignores SetCursorPos / synthetic SendInput, so use the
        Arduino's hardware-style relative move. The sketch chunks deltas
        larger than ±127 into multiple HID reports."""
        self._send_command(f"P:{int(dx)}:{int(dy)}")

    def move_to(self, x, y, stop_evt=None, max_iters=15, tol=2):
        """Closed-loop absolute move to screen-pixel (x, y). Genshin's
        anti-cheat ignores SetCursorPos and synthetic SendInput-style
        cursor moves, so we drive the cursor with real HID relative
        deltas and check via GetCursorPos until it lands within `tol`
        pixels (axis-wise) of the target. Iterating compensates for
        Windows pointer-precision (Enhance Pointer Precision) which
        makes a single delta over- or under-shoot — usually converges
        in 1-2 iters with EPP off (AlbumRunner disables it for the
        run). Sleep between iters is 40 ms — long enough that the
        Arduino round-trip + cursor update is observable on the next
        GetCursorPos. Returns True on convergence, False on stop_evt
        or running out of iterations."""
        user32 = ctypes.windll.user32
        pt = ctypes.wintypes.POINT()
        for _ in range(max_iters):
            if stop_evt is not None and stop_evt.is_set():
                return False
            user32.GetCursorPos(ctypes.byref(pt))
            dx = int(x) - pt.x
            dy = int(y) - pt.y
            if abs(dx) <= tol and abs(dy) <= tol:
                return True
            self.mouse_move(dx, dy)
            # Cancellable wait — stop_evt.wait returns True if set.
            if stop_evt is not None:
                if stop_evt.wait(0.04):
                    return False
            else:
                time.sleep(0.04)
        return False

    def _mark_closed(self):
        """Terminal state for a deliberate close: no later write or read
        goes through, and `healthy` is False even if `ser.close()` raised
        and pyserial left `is_open` True. Not logged as a disconnect."""
        with self._write_lock:
            self._link_failed = True

    def close(self):
        ser = self.ser
        if not ser or not ser.is_open:
            self._mark_closed()
            return
        try:
            # Send UP to all standard rhythm keys just in case. Skipped
            # internally (no write, no log spam) if the link already died.
            for k in ['a', 's', 'd', 'j', 'k', 'l']:
                self.key_up(k)
            # Release mouse just in case
            self.mouse_up('left')
            self.mouse_up('right')
        finally:
            # Latch BEFORE closing the port so no writer slips in between,
            # and latch regardless of how the sweep or close() went.
            self._mark_closed()
            if self._close_port_quietly():
                print("Serial connection closed")

if __name__ == "__main__":
    # Test script if run directly
    try:
        ctrl = ArduinoHIDController()
    except ArduinoConnectionError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print("Testing keys. You should see keys being pressed in 3 seconds")
    time.sleep(3)
    
    print("Tapping A")
    ctrl.key_down('a')
    time.sleep(0.05)
    ctrl.key_up('a')
    time.sleep(1)
    
    print("Tapping Enter")
    ctrl.key_down('enter')
    time.sleep(0.05)
    ctrl.key_up('enter')
    time.sleep(1)
    
    ctrl.close()
