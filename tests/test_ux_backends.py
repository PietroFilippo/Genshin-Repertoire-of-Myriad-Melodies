"""Regression tests for the backend + macro-engine UX fixes.

Covers: Arduino link health (`healthy`), construction failures
(`ArduinoConnectionError`, cancellable settle wait), the scheduled-macro
signature cache after a failed replacement upload, the extended Arduino
key map / unsupported-button handling, atomic slot writes, editor key
validation, and the software backend's keyboard replay-flag recovery.

Headless: every serial port is a fake, and the only real `keyboard`
call presses a name that can't resolve (nothing is sent to the OS).
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc_client'))

import serial  # noqa: E402

import controller as controller_module  # noqa: E402
import macro_engine as macro_engine_module  # noqa: E402
from macro_engine import MacroEngine, atomic_write_json  # noqa: E402


def _event(t, device, key, event_type):
    return {'time': t, 'device': device, 'key': key, 'event_type': event_type}


class ModelFirmwareSerial:
    """Stateful firmware-v3 sketch model. Unlike a canned responder it
    tracks which buffer is committed on the board and records exactly
    what each START ran, so a stale host-side cache shows up as the
    wrong macro being played."""

    def __init__(self):
        self.is_open = True
        self.writes = []
        self.started = []
        self.loaded = None
        self._loading = None
        self._expected = None
        self._responses = bytearray()
        # Test knobs.
        self.reject_next_commit = False
        self.drop_next_commit_ack = False
        self.on_command = None

    @property
    def in_waiting(self):
        return len(self._responses)

    def reset_input_buffer(self):
        self._responses.clear()

    def read(self, count):
        result = bytes(self._responses[:count])
        del self._responses[:count]
        return result

    def close(self):
        self.is_open = False

    def write(self, payload):
        command = payload.decode('ascii').strip()
        self.writes.append(command)
        if self.on_command is not None:
            self.on_command(command)
        if command == 'V':
            self._reply('V:3:TM:160')
        elif command.startswith('L:'):
            _, count, crc = command.split(':')
            self._expected = (int(count), int(crc))
            self._loading = []
            self.loaded = None          # firmware clears macroLoaded on L
            self._reply('L:OK')
        elif command.startswith('E:'):
            _, at_ms, op, code = command.split(':')
            self._loading.append((int(at_ms), int(op), int(code)))
        elif command == 'C':
            if self.reject_next_commit:
                self.reject_next_commit = False
                self._loading = None
                self._reply('X:COMMIT:0:0')
            else:
                self.loaded = tuple(self._loading)
                self._loading = None
                if self.drop_next_commit_ack:
                    self.drop_next_commit_ack = False
                else:
                    self._reply(f'R:{self._expected[0]}:{self._expected[1]}')
        elif command == 'S':
            if self.loaded is None:
                self._reply('X:NOLOAD')
            else:
                self.started.append(self.loaded)
                self._reply('S:OK')
                self._reply(f'D:{len(self.loaded)}:80:20:0:0:0')
        elif command == 'A':
            self._reply('A:OK')
        return len(payload)

    def _reply(self, line):
        self._responses.extend((line + '\n').encode('ascii'))


class ScriptedSerial:
    """Serial double whose write outcome is scripted per call: None =
    success, otherwise the exception instance to raise."""

    def __init__(self, script=(), in_waiting_exc=None):
        self.is_open = True
        self.script = list(script)
        self.writes = []
        self.attempts = 0
        self.in_waiting_exc = in_waiting_exc
        self.closed = False

    @property
    def in_waiting(self):
        if self.in_waiting_exc is not None:
            raise self.in_waiting_exc
        return 0

    def read(self, count):
        return b''

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True
        self.is_open = False

    def write(self, payload):
        self.attempts += 1
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome
        self.writes.append(payload.decode('ascii').strip())
        return len(payload)


class PatternSerial:
    """Serial double whose outcome depends on the ORDER writes reach the
    port. `write` runs under the controller's write lock, so `index` is
    the true serialized order regardless of thread interleaving."""

    def __init__(self, is_timeout):
        self.is_open = True
        self.is_timeout = is_timeout
        self.index = 0
        self.writes = []
        self.outcomes = []

    in_waiting = 0

    def read(self, count):
        return b''

    def close(self):
        self.is_open = False

    def write(self, payload):
        index = self.index
        self.index += 1
        timed_out = self.is_timeout(index)
        self.outcomes.append(timed_out)
        if timed_out:
            raise serial.SerialTimeoutException('Write timeout')
        self.writes.append(payload.decode('ascii').strip())
        return len(payload)


class HookedLock:
    """Drop-in for threading.Lock that runs a one-shot `after_release`
    hook in the releasing thread right after release. Lets a test park
    a writer in the gap between leaving the lock and any bookkeeping it
    does afterwards — the window the racy timeout counter lived in."""

    def __init__(self):
        self._lock = threading.Lock()
        self.after_release = None

    def acquire(self, blocking=True, timeout=-1):
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()
        hook, self.after_release = self.after_release, None
        if hook is not None:
            hook()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


def _bare_controller(ser):
    """ArduinoHIDController without touching a real port — mirrors the
    fixture style in test_scheduled_macro.py."""
    ctrl = controller_module.ArduinoHIDController.__new__(
        controller_module.ArduinoHIDController)
    ctrl.port = 'COM-TEST'
    ctrl.ser = ser
    ctrl._write_lock = threading.Lock()
    ctrl._macro_lock = threading.Lock()
    ctrl._rx_buffer = bytearray()
    ctrl._pending_response_lines = []
    ctrl._timed_macro_supported = None
    ctrl._timed_macro_protocol_version = 0
    ctrl._timed_macro_max_events = 0
    ctrl._loaded_timed_signature = None
    return ctrl


def _quiet(fn, *args, **kwargs):
    out = StringIO()
    with redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


MACRO_A = [
    _event(0.0, 'keyboard', 'e', 'down'),
    _event(0.01, 'keyboard', 'e', 'up'),
]
MACRO_B = [
    _event(0.0, 'keyboard', 'q', 'down'),
    _event(0.02, 'keyboard', 'q', 'up'),
]


# ---------------------------------------------------------------- M7

class TimedSignatureCacheTests(unittest.TestCase):
    def _packed(self, events):
        return tuple(controller_module._compile_timed_macro_events(events)[0])

    def test_rejected_replacement_upload_forces_reupload_of_old_macro(self):
        fw = ModelFirmwareSerial()
        ctrl = _bare_controller(fw)

        handled, _ = _quiet(ctrl.play_timed_macro, MACRO_A)
        self.assertTrue(handled)

        fw.reject_next_commit = True
        handled, _ = _quiet(ctrl.play_timed_macro, MACRO_B)
        self.assertFalse(handled)            # pre-START failure -> fallback
        self.assertIsNone(ctrl._loaded_timed_signature)

        before = len(fw.writes)
        handled, _ = _quiet(ctrl.play_timed_macro, MACRO_A)
        self.assertTrue(handled)
        again = fw.writes[before:]
        self.assertTrue(any(w.startswith('L:') for w in again),
                        "old macro must be re-uploaded, not START-ed blind")
        self.assertEqual(self._packed(MACRO_A), fw.started[-1])

    def test_lost_commit_ack_never_starts_the_other_macro(self):
        # Board commits B but the host never sees R: (user hit Stop while
        # waiting). The host must not then treat A as still loaded.
        fw = ModelFirmwareSerial()
        ctrl = _bare_controller(fw)
        _quiet(ctrl.play_timed_macro, MACRO_A)

        stop_evt = threading.Event()
        fw.drop_next_commit_ack = True
        fw.on_command = lambda cmd: stop_evt.set() if cmd == 'C' else None
        handled, _ = _quiet(ctrl.play_timed_macro, MACRO_B, stop_evt=stop_evt)
        fw.on_command = None
        self.assertFalse(handled)
        self.assertEqual(self._packed(MACRO_B), fw.loaded)   # board has B
        self.assertIsNone(ctrl._loaded_timed_signature)

        handled, _ = _quiet(ctrl.play_timed_macro, MACRO_A)
        self.assertTrue(handled)
        self.assertEqual(self._packed(MACRO_A), fw.started[-1])
        self.assertNotIn(self._packed(MACRO_B), fw.started)

    def test_signature_is_cached_only_after_verified_commit(self):
        fw = ModelFirmwareSerial()
        ctrl = _bare_controller(fw)
        _quiet(ctrl.play_timed_macro, MACRO_A)
        packed, crc = controller_module._compile_timed_macro_events(MACRO_A)
        self.assertEqual((len(packed), crc), ctrl._loaded_timed_signature)

        # Unchanged macro: START only, no re-upload.
        before = len(fw.writes)
        _quiet(ctrl.play_timed_macro, MACRO_A)
        self.assertEqual(['S'], fw.writes[before:])


# ---------------------------------------------------------------- M14

class ArduinoKeyMapTests(unittest.TestCase):
    EXPECTED = {
        'f1': 0xC2, 'f5': 0xC6, 'f12': 0xCD, 'f13': 0xF0, 'f24': 0xFB,
        'insert': 0xD1, 'home': 0xD2, 'page up': 0xD3, 'delete': 0xD4,
        'end': 0xD5, 'page down': 0xD6, 'backspace': 0xB2, 'tab': 0xB3,
        'caps lock': 0xC1, 'enter': 0xB0, 'esc': 0xB1, 'space': 32,
        'left shift': 0x81, 'e': ord('e'), '1': ord('1'),
    }

    def test_common_keys_map_to_keyboard_h_codes(self):
        for name, code in self.EXPECTED.items():
            with self.subTest(key=name):
                self.assertEqual(code, controller_module._key_code(name))

    def test_non_ascii_single_char_is_rejected_not_sent_as_hid_usage(self):
        # ord('ç') = 231 would be read by Keyboard.press as HID usage 95
        # (keypad 7) — a silent wrong key.
        self.assertEqual(0, controller_module._key_code('ç'))

    def test_realtime_and_timed_paths_agree(self):
        ctrl = _bare_controller(ScriptedSerial())
        _quiet(ctrl.key_down, 'f5')
        _quiet(ctrl.key_up, 'page down')
        self.assertEqual(['K:198:DOWN', 'K:214:UP'], ctrl.ser.writes)
        packed, _ = controller_module._compile_timed_macro_events([
            _event(0.0, 'keyboard', 'f5', 'down'),
            _event(0.0, 'keyboard', 'page down', 'up'),
        ])
        self.assertEqual([198, 214], [code for _, _, code in packed])

    def test_unknown_key_is_logged_once_and_not_sent(self):
        ctrl = _bare_controller(ScriptedSerial())
        _, log = _quiet(ctrl.key_down, 'not_a_real_key')
        _, log2 = _quiet(ctrl.key_up, 'not_a_real_key')
        self.assertEqual([], ctrl.ser.writes)
        self.assertIn("key 'not_a_real_key' isn't supported by the Arduino "
                      "backend", log)
        self.assertEqual('', log2)

    def test_side_mouse_buttons_are_skipped_never_left_clicked(self):
        ctrl = _bare_controller(ScriptedSerial())
        _, log = _quiet(ctrl.mouse_down, 'x2')
        _quiet(ctrl.mouse_up, 'x2')
        _quiet(ctrl.mouse_down, 'x')
        self.assertEqual([], ctrl.ser.writes)
        self.assertIn("mouse button 'x2' isn't supported by the Arduino "
                      "backend", log)
        self.assertEqual(1, log.count('x2'))

    def test_mouse_methods_never_raise_on_bad_input(self):
        ctrl = _bare_controller(ScriptedSerial())
        _quiet(ctrl.mouse_down, None)
        _quiet(ctrl.mouse_up, None)
        self.assertEqual([], ctrl.ser.writes)

    def test_timed_compiler_rejects_side_buttons(self):
        for button in ('x', 'x2'):
            with self.subTest(button=button), self.assertRaises(ValueError):
                controller_module._compile_timed_macro_events([
                    _event(0.0, 'mouse', button, 'down'),
                ])

    def test_side_button_macro_falls_back_to_host_and_skips_button(self):
        fw = ModelFirmwareSerial()
        ctrl = _bare_controller(fw)
        engine = MacroEngine()
        engine._events = [
            _event(0.0, 'mouse', 'x2', 'down'),
            _event(0.0, 'mouse', 'x2', 'up'),
            _event(0.0, 'keyboard', 'e', 'down'),
            _event(0.0, 'keyboard', 'e', 'up'),
        ]
        _, log = _quiet(engine.play, ctrl)
        self.assertFalse(any(w.startswith('L:') for w in fw.writes))
        self.assertIn('K:101:DOWN', fw.writes)
        self.assertNotIn('M:1:DOWN', fw.writes)
        self.assertIn("mouse button 'x2' isn't supported", log)


# ------------------------------------------------- v1.6.1 keyboard `double`

class KeyboardDoubleTimedTests(unittest.TestCase):
    """The events editor offers `double` for keyboard rows too. Like a
    mouse `double`, it must play as a press on the board scheduler — not
    be rejected (which silently forced the host scheduler)."""

    EVENTS = [
        _event(0.0, 'keyboard', 'e', 'double'),
        _event(0.05, 'keyboard', 'e', 'up'),
    ]

    def test_compiler_encodes_keyboard_double_as_key_down(self):
        packed, _ = controller_module._compile_timed_macro_events(self.EVENTS)
        self.assertEqual([
            (0, controller_module._TIMED_KEY_DOWN, ord('e')),
            (50, controller_module._TIMED_KEY_UP, ord('e')),
        ], packed)

    def test_keyboard_double_macro_stays_on_the_board_scheduler(self):
        fw = ModelFirmwareSerial()
        ctrl = _bare_controller(fw)
        engine = MacroEngine()
        engine._events = [dict(ev) for ev in self.EVENTS]
        _, log = _quiet(engine.play, ctrl)
        self.assertNotIn('using host scheduler', log)
        self.assertEqual(
            ((0, controller_module._TIMED_KEY_DOWN, ord('e')),
             (50, controller_module._TIMED_KEY_UP, ord('e'))),
            fw.started[-1])
        self.assertNotIn('K:101:DOWN', fw.writes)    # host path never ran


# ---------------------------------------------------------------- R11

class ArduinoHealthTests(unittest.TestCase):
    def test_open_link_is_healthy(self):
        self.assertTrue(_bare_controller(ScriptedSerial()).healthy)

    def test_hard_write_failure_latches_unhealthy_and_logs_once(self):
        ser = ScriptedSerial([serial.SerialException('WriteFile failed')])
        ctrl = _bare_controller(ser)
        _, log = _quiet(ctrl.key_down, 'a')
        self.assertFalse(ctrl.healthy)
        self.assertEqual(1, log.count('Arduino disconnected'))
        self.assertIn('stop and press Start again to reconnect', log)

        attempts = ser.attempts
        _, log2 = _quiet(lambda: [ctrl.key_down('s') for _ in range(20)])
        self.assertEqual(attempts, ser.attempts,
                         "dead link must not be written to again")
        self.assertEqual('', log2)
        self.assertFalse(ctrl.healthy)        # stays False

    def test_os_error_on_write_is_also_fatal(self):
        ctrl = _bare_controller(ScriptedSerial([OSError(22, 'bad command')]))
        _quiet(ctrl.mouse_down, 'left')
        self.assertFalse(ctrl.healthy)

    def test_isolated_write_timeouts_are_transient(self):
        timeout = serial.SerialTimeoutException('Write timeout')
        ser = ScriptedSerial([timeout, timeout, None, timeout, timeout, None])
        ctrl = _bare_controller(ser)
        _quiet(lambda: [ctrl.key_down('a') for _ in range(6)])
        self.assertTrue(ctrl.healthy)
        self.assertEqual(2, len(ser.writes))

    def test_consecutive_write_timeouts_latch_unhealthy(self):
        timeout = serial.SerialTimeoutException('Write timeout')
        ser = ScriptedSerial([timeout] * 3)
        ctrl = _bare_controller(ser)
        _, log = _quiet(lambda: [ctrl.key_down('a') for _ in range(5)])
        self.assertFalse(ctrl.healthy)
        self.assertEqual(3, ser.attempts)
        self.assertEqual(1, log.count('Arduino disconnected'))

    def test_read_failure_latches_unhealthy(self):
        ser = ScriptedSerial(in_waiting_exc=serial.SerialException(
            'ClearCommError failed'))
        ctrl = _bare_controller(ser)
        lines, log = _quiet(ctrl._read_response_lines)
        self.assertEqual([], lines)
        self.assertIn('Arduino disconnected', log)
        self.assertFalse(ctrl.healthy)

    def test_healthy_probe_detects_port_unplugged_while_idle(self):
        ser = ScriptedSerial(in_waiting_exc=serial.SerialException(
            'ClearCommError failed (PermissionError(13))'))
        ctrl = _bare_controller(ser)
        healthy, log = _quiet(lambda: ctrl.healthy)
        self.assertFalse(healthy)
        self.assertEqual(0, ser.attempts)     # probe never writes
        self.assertEqual(1, log.count('Arduino disconnected'))

    def test_close_sweeps_then_makes_controller_unhealthy(self):
        ser = ScriptedSerial()
        ctrl = _bare_controller(ser)
        _, log = _quiet(ctrl.close)
        self.assertIn('K:97:UP', ser.writes)          # rhythm sweep ran
        self.assertIn('M:1:UP', ser.writes)
        self.assertTrue(ser.closed)
        self.assertFalse(ctrl.healthy)
        self.assertEqual(1, log.count('Serial connection closed'))

        writes = list(ser.writes)
        _, log2 = _quiet(ctrl.close)                  # idempotent
        _quiet(ctrl.key_down, 'a')
        self.assertEqual(writes, ser.writes)
        self.assertNotIn('Serial connection closed', log2)

    def test_failed_port_close_still_disables_the_same_controller(self):
        # pyserial can raise from close() and leave is_open True; the
        # controller must still be terminally dead afterwards.
        ser = ScriptedSerial()
        ser.close = mock.Mock(side_effect=OSError('handle already gone'))
        ctrl = _bare_controller(ser)
        _, log = _quiet(ctrl.close)                   # must not raise
        self.assertTrue(ser.is_open)                  # fake really stayed open
        self.assertIn('K:97:UP', ser.writes)          # sweep attempted first
        self.assertIn('close failed', log)
        self.assertNotIn('Serial connection closed', log)
        self.assertFalse(ctrl.healthy)

        writes = list(ser.writes)
        _quiet(ctrl.key_down, 'a')
        _quiet(ctrl.mouse_down, 'left')
        self.assertEqual(writes, ser.writes)
        self.assertEqual([], _quiet(ctrl._read_response_lines)[0])

    def test_close_marks_dead_even_if_the_sweep_is_interrupted(self):
        ser = ScriptedSerial()
        ctrl = _bare_controller(ser)
        with mock.patch.object(ctrl, 'key_up',
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _quiet(ctrl.close)
        self.assertTrue(ser.closed)
        self.assertFalse(ctrl.healthy)

    def test_close_on_dead_link_skips_the_sweep_writes(self):
        ser = ScriptedSerial([serial.SerialException('gone')])
        ctrl = _bare_controller(ser)
        _quiet(ctrl.key_down, 'a')
        attempts = ser.attempts
        _, log = _quiet(ctrl.close)
        self.assertEqual(attempts, ser.attempts)
        self.assertTrue(ser.closed)
        self.assertNotIn('write', log)

    def test_success_from_another_thread_breaks_the_timeout_run(self):
        # Reviewer's interleaving: in port order A times out, B succeeds,
        # then C and D time out. That is never 3 consecutive timeouts, so
        # the link must stay up and B's key must still be releasable.
        timeout = serial.SerialTimeoutException('Write timeout')
        ser = ScriptedSerial([timeout, None, timeout, timeout])
        ctrl = _bare_controller(ser)
        lock = HookedLock()
        ctrl._write_lock = lock

        def run_b_in_the_gap():
            # A has just left the lock after its timeout. B's entire
            # successful write (and any reset it does) lands right here,
            # before A's thread gets to run another line.
            b = threading.Thread(target=ctrl.key_down, args=('s',))
            b.start()
            b.join(5)

        lock.after_release = run_b_in_the_gap

        def scenario():
            a = threading.Thread(target=ctrl.key_down, args=('a',))
            a.start()
            a.join(5)
            ctrl.key_down('d')          # C: timeout
            ctrl.key_down('j')          # D: timeout
            ctrl.key_up('s')            # must still reach the board

        _, log = _quiet(scenario)
        self.assertIn('K:115:DOWN', ser.writes)
        self.assertIn('K:115:UP', ser.writes)
        self.assertNotIn('Arduino disconnected', log)
        self.assertTrue(ctrl.healthy)

        _quiet(ctrl.close)
        self.assertIn('K:97:UP', ser.writes)        # sweep not suppressed

    def test_detector_threads_never_latch_without_three_timeouts_in_a_row(self):
        # Six rhythm-detector-style threads hammer the port; every third
        # write (in port order) succeeds. Whatever the interleaving, the
        # link must stay up, and the latch must agree with port order.
        ser = PatternSerial(lambda index: index % 3 != 2)
        ctrl = _bare_controller(ser)
        keys = ['a', 's', 'd', 'j', 'k', 'l']
        start = threading.Barrier(len(keys))

        def hammer(key):
            start.wait()
            for _ in range(150):
                ctrl.key_down(key)
                ctrl.key_up(key)

        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            def scenario():
                threads = [threading.Thread(target=hammer, args=(k,))
                           for k in keys]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
            _, log = _quiet(scenario)
        finally:
            sys.setswitchinterval(previous)

        self.assertEqual(len(keys) * 300, ser.index)
        self.assertNotIn('Arduino disconnected', log)
        self.assertTrue(ctrl.healthy)

    def test_latch_happens_at_the_third_consecutive_timeout_in_port_order(self):
        # Two successes then timeouts forever: exactly 3 timeouts reach
        # the port, then every later write (from any thread) is skipped.
        ser = PatternSerial(lambda index: index >= 2)
        ctrl = _bare_controller(ser)

        def scenario():
            threads = [threading.Thread(
                target=lambda k=k: [ctrl.key_down(k) for _ in range(20)])
                for k in ['a', 's', 'd', 'j', 'k', 'l']]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)

        _, log = _quiet(scenario)
        self.assertEqual([False, False, True, True, True], ser.outcomes)
        self.assertEqual(1, log.count('Arduino disconnected'))
        self.assertFalse(ctrl.healthy)

    def test_software_backend_is_always_healthy(self):
        import software_input
        ctrl = software_input.SoftwareInputController.__new__(
            software_input.SoftwareInputController)
        self.assertTrue(ctrl.healthy)


# ---------------------------------------------------------------- R14

class _FakePort:
    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.closed = False
        self.in_waiting = 0

    def close(self):
        self.closed = True
        self.is_open = False


class ArduinoConstructionTests(unittest.TestCase):
    def test_error_type_is_a_runtime_error(self):
        self.assertTrue(issubclass(controller_module.ArduinoConnectionError,
                                   RuntimeError))

    def test_no_board_raises_user_facing_error_not_system_exit(self):
        with mock.patch.object(controller_module, 'SERIAL_PORT', None), \
             mock.patch.object(controller_module, '_auto_detect_port',
                               return_value=None):
            with self.assertRaises(
                    controller_module.ArduinoConnectionError) as cm:
                controller_module.ArduinoHIDController()
        self.assertIn('No Arduino found', str(cm.exception))
        self.assertIn('Software', str(cm.exception))

    def test_open_failure_names_the_port_and_reason(self):
        exc = serial.SerialException(
            "could not open port 'COM9': PermissionError(13, "
            "'Access is denied.', None, 5)")
        with mock.patch.object(controller_module, 'SERIAL_PORT', 'COM9'), \
             mock.patch.object(controller_module.serial, 'Serial',
                               side_effect=exc):
            with self.assertRaises(
                    controller_module.ArduinoConnectionError) as cm:
                _quiet(controller_module.ArduinoHIDController)
        message = str(cm.exception)
        self.assertTrue(message.startswith("Couldn't open COM9: "), message)
        self.assertEqual(1, message.count('COM9'))
        self.assertIn('Access is denied', message)

    def test_settle_wait_is_cancellable_and_closes_the_port(self):
        ports = []

        def make_port(*args, **kwargs):
            ports.append(_FakePort())
            return ports[-1]

        cancel_evt = threading.Event()
        timer = threading.Timer(0.05, cancel_evt.set)
        with mock.patch.object(controller_module, 'SERIAL_PORT', 'COM9'), \
             mock.patch.object(controller_module.serial, 'Serial',
                               side_effect=make_port):
            started = time.perf_counter()
            timer.start()
            with self.assertRaises(
                    controller_module.ArduinoConnectionError) as cm:
                _quiet(controller_module.ArduinoHIDController,
                       cancel_evt=cancel_evt)
            elapsed = time.perf_counter() - started
        timer.cancel()
        self.assertEqual('Arduino connection cancelled', str(cm.exception))
        self.assertLess(elapsed, 1.0)
        self.assertEqual(1, len(ports))
        self.assertTrue(ports[0].closed)

    def test_preset_cancel_does_not_open_the_port(self):
        cancel_evt = threading.Event()
        cancel_evt.set()
        with mock.patch.object(controller_module, 'SERIAL_PORT', 'COM9'), \
             mock.patch.object(controller_module.serial, 'Serial') as ctor:
            with self.assertRaises(controller_module.ArduinoConnectionError):
                _quiet(controller_module.ArduinoHIDController,
                       cancel_evt=cancel_evt)
        ctor.assert_not_called()

    def test_uncancelled_connect_waits_on_the_event_and_is_healthy(self):
        cancel_evt = mock.Mock()
        cancel_evt.is_set.return_value = False
        cancel_evt.wait.return_value = False
        with mock.patch.object(controller_module, 'SERIAL_PORT', 'COM9'), \
             mock.patch.object(controller_module.serial, 'Serial',
                               side_effect=_FakePort), \
             mock.patch.object(controller_module.time, 'sleep') as sleep:
            ctrl, _ = _quiet(controller_module.ArduinoHIDController,
                             cancel_evt=cancel_evt)
        cancel_evt.wait.assert_called_once_with(2.0)
        sleep.assert_not_called()
        self.assertTrue(ctrl.healthy)


# ---------------------------------------------------------------- M8

class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _leftovers(self):
        return sorted(p.name for p in self.dir.iterdir()
                      if p.name.endswith('.tmp'))

    def test_round_trip_and_parent_creation(self):
        target = self.dir / 'nested' / 'settings.json'
        atomic_write_json(target, {'a': 1, 'b': [1, 2]}, indent=2)
        self.assertEqual({'a': 1, 'b': [1, 2]},
                         json.loads(target.read_text(encoding='utf-8')))

    def test_serialization_failure_leaves_existing_file_untouched(self):
        target = self.dir / 'slot.json'
        target.write_text('{"name": "old", "events": []}', encoding='utf-8')
        with self.assertRaises(TypeError):
            atomic_write_json(target, {'bad': object()})
        self.assertEqual('{"name": "old", "events": []}',
                         target.read_text(encoding='utf-8'))
        self.assertEqual([], self._leftovers())

    def test_replace_failure_leaves_existing_file_and_no_temp(self):
        target = self.dir / 'slot.json'
        target.write_text('ORIGINAL', encoding='utf-8')
        with mock.patch.object(macro_engine_module.os, 'replace',
                               side_effect=OSError(28, 'No space left')):
            with self.assertRaises(OSError):
                atomic_write_json(target, {'name': 'new'})
        self.assertEqual('ORIGINAL', target.read_text(encoding='utf-8'))
        self.assertEqual([], self._leftovers())

    def test_write_failure_after_temp_creation_cleans_up(self):
        target = self.dir / 'slot.json'
        target.write_text('ORIGINAL', encoding='utf-8')
        with mock.patch.object(macro_engine_module.os, 'fsync',
                               side_effect=OSError(5, 'I/O error')):
            with self.assertRaises(OSError):
                atomic_write_json(target, {'name': 'new'})
        self.assertEqual('ORIGINAL', target.read_text(encoding='utf-8'))
        self.assertEqual([], self._leftovers())

    def test_sharing_violation_on_replace_is_retried(self):
        target = self.dir / 'slot.json'
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, 'being used by another process')
            return real_replace(src, dst)

        with mock.patch.object(macro_engine_module.os, 'replace',
                               side_effect=flaky), \
             mock.patch.object(macro_engine_module, '_REPLACE_RETRY_S', 0):
            atomic_write_json(target, {'ok': True})
        self.assertEqual(3, len(calls))
        self.assertEqual({'ok': True},
                         json.loads(target.read_text(encoding='utf-8')))

    def test_engine_save_failure_raises_and_keeps_old_slot(self):
        target = self.dir / 'macro_1.json'
        MacroEngine.write_slot(target, [_event(0.0, 'keyboard', 'e', 'down')],
                               'old')
        before = target.read_bytes()
        engine = MacroEngine()
        engine._events = [_event(0.0, 'keyboard', 'q', 'down')]
        with mock.patch.object(macro_engine_module.os, 'replace',
                               side_effect=OSError(28, 'No space left')):
            with self.assertRaises(OSError):
                engine.save(target, name='new')
        self.assertEqual(before, target.read_bytes())
        self.assertGreater(len(before), 0)
        self.assertEqual([], self._leftovers())

    def test_engine_save_round_trips(self):
        target = self.dir / 'macro_2.json'
        engine = MacroEngine()
        engine._events = [_event(0.5, 'mouse', 'left', 'down')]
        self.assertIsNone(engine.save(target, name='combo'))
        events, name = MacroEngine.read(target)
        self.assertEqual('combo', name)
        self.assertEqual(engine.events, events)

    def test_rename_slot_keeps_events_and_upgrades_legacy_list(self):
        target = self.dir / 'macro_3.json'
        legacy = [_event(0.0, 'keyboard', 'e', 'down')]
        target.write_text(json.dumps(legacy), encoding='utf-8')
        MacroEngine.rename_slot(target, 'renamed')
        events, name = MacroEngine.read(target)
        self.assertEqual('renamed', name)
        self.assertEqual(legacy, events)

    def test_rename_slot_failure_keeps_old_file(self):
        target = self.dir / 'macro_4.json'
        MacroEngine.write_slot(target, [_event(0.0, 'keyboard', 'e', 'up')],
                               'keep')
        before = target.read_bytes()
        with mock.patch.object(macro_engine_module.os, 'replace',
                               side_effect=OSError(28, 'No space left')):
            with self.assertRaises(OSError):
                MacroEngine.rename_slot(target, 'lost')
        self.assertEqual(before, target.read_bytes())


# ---------------------------------------------------------------- M6

def _keyboard_or_skip():
    try:
        import keyboard
    except Exception as exc:                        # pragma: no cover
        raise unittest.SkipTest(f'keyboard package unavailable: {exc}')
    return keyboard


class EditorValidationTests(unittest.TestCase):
    def setUp(self):
        _keyboard_or_skip()

    def _replace(self, events):
        engine = MacroEngine()
        cleaned, log = _quiet(engine.replace_events, events)
        return cleaned, log

    def test_unknown_key_is_dropped_with_a_log_naming_it(self):
        cleaned, log = self._replace([
            _event(0.0, 'keyboard', 'not_a_real_key', 'down'),
            _event(0.1, 'keyboard', 'e', 'down'),
        ])
        self.assertEqual(['e'], [ev['key'] for ev in cleaned])
        self.assertIn("'not_a_real_key'", log)
        self.assertIn('dropped event 0', log)

    def test_key_combinations_are_rejected(self):
        cleaned, log = self._replace([
            _event(0.0, 'keyboard', 'ctrl+a', 'down'),
        ])
        self.assertEqual([], cleaned)
        self.assertIn("'ctrl+a'", log)

    def test_aliases_are_canonicalized_for_both_backends(self):
        cleaned, _ = self._replace([
            _event(0.0, 'keyboard', 'Escape', 'down'),
            _event(0.1, 'keyboard', 'page_up', 'down'),
            _event(0.2, 'keyboard', 'Left Shift', 'down'),
            _event(0.3, 'keyboard', 'F5', 'down'),
        ])
        keys = [ev['key'] for ev in cleaned]
        self.assertEqual(['esc', 'page up', 'left shift', 'f5'], keys)
        for key in keys:
            self.assertNotEqual(0, controller_module._key_code(key), key)

    def test_mouse_buttons_are_validated(self):
        cleaned, log = self._replace([
            _event(0.0, 'mouse', 'left', 'down'),
            _event(0.1, 'mouse', 'x', 'down'),
            _event(0.2, 'mouse', 'x2', 'up'),
            _event(0.3, 'mouse', 'wheel', 'down'),
            _event(0.4, 'mouse', 'x3', 'down'),
        ])
        self.assertEqual(['left', 'x', 'x2'], [ev['key'] for ev in cleaned])
        self.assertIn("'wheel'", log)
        self.assertIn("'x3'", log)

    def test_validation_is_skipped_when_keyboard_is_unavailable(self):
        with mock.patch.object(macro_engine_module, '_keyboard_package',
                               return_value=None):
            cleaned, _ = self._replace([
                _event(0.0, 'keyboard', 'not_a_real_key', 'down'),
            ])
        self.assertEqual(['not_a_real_key'], [ev['key'] for ev in cleaned])

    def test_validator_crash_does_not_drop_user_data(self):
        broken = mock.Mock()
        broken.normalize_name.side_effect = RuntimeError('tables exploded')
        with mock.patch.object(macro_engine_module, '_keyboard_package',
                               return_value=broken):
            cleaned, _ = self._replace([
                _event(0.0, 'keyboard', 'e', 'down'),
            ])
        self.assertEqual(['e'], [ev['key'] for ev in cleaned])


class SoftwareReplayFlagTests(unittest.TestCase):
    def setUp(self):
        self.keyboard = _keyboard_or_skip()
        import software_input
        self.ctrl = software_input.SoftwareInputController.__new__(
            software_input.SoftwareInputController)
        self.listener = self.keyboard._listener
        self._had_flag = hasattr(self.listener, 'is_replaying')
        self._saved = getattr(self.listener, 'is_replaying', None)

    def tearDown(self):
        if self._had_flag:
            self.listener.is_replaying = self._saved

    def test_installed_package_still_has_the_replay_flag(self):
        # Tripwire: if a keyboard upgrade renames the flag, the recovery
        # in software_input becomes a silent no-op.
        with self.assertRaises(ValueError):
            self.keyboard.press('zz_not_a_key_zz')
        self.assertTrue(self.listener.is_replaying)
        self.listener.is_replaying = False

    def test_bad_key_down_does_not_leave_hotkeys_disabled(self):
        _, log = _quiet(self.ctrl.key_down, 'not_a_real_key')
        self.assertIn("key_down 'not_a_real_key' failed", log)
        self.assertFalse(self.listener.is_replaying)

    def test_bad_key_up_does_not_leave_hotkeys_disabled(self):
        _, log = _quiet(self.ctrl.key_up, 'not_a_real_key')
        self.assertIn("key_up 'not_a_real_key' failed", log)
        self.assertFalse(self.listener.is_replaying)

    def test_recovery_with_a_fake_keyboard_module(self):
        import software_input
        fake = mock.Mock()
        fake._listener = mock.Mock(is_replaying=False)

        def press(name):
            fake._listener.is_replaying = True
            raise ValueError('Key %r is not mapped' % name)

        fake.press.side_effect = press
        with mock.patch.object(software_input, 'keyboard', fake):
            _quiet(self.ctrl.key_down, 'bogus')
        self.assertFalse(fake._listener.is_replaying)


if __name__ == '__main__':
    unittest.main()
