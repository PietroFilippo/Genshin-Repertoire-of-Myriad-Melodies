"""Regression tests for the Macros-tab UX fixes (C-macro package).

Headless: no real keyboard / mouse hooks are installed (the hook
installers are patched), no real window focus happens (user32 is faked),
and every slot lives in a temp dir — never pc_client/macros/.
"""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc_client'))

import mouse  # noqa: E402
import ui_core  # noqa: E402
from ui_core import MacroController  # noqa: E402


def _ev(t, key, event_type, device='keyboard'):
    return {'time': t, 'device': device, 'key': key, 'event_type': event_type}


def _write_slot(macro_dir, n, events, name=''):
    path = Path(macro_dir) / f'macro_{n}.json'
    path.write_text(json.dumps({'name': name, 'events': events}),
                    encoding='utf-8')
    return path


def _read_slot(macro_dir, n):
    return json.loads((Path(macro_dir) / f'macro_{n}.json')
                      .read_text(encoding='utf-8'))


def _kb(name, event_type, scan_code=None):
    return types.SimpleNamespace(name=name, event_type=event_type,
                                 scan_code=scan_code, is_keypad=False)


def _wait_for(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Recorder:
    """Controller stub for playback tests."""

    def __init__(self):
        self.calls = []

    def key_down(self, key):
        self.calls.append(('key_down', key))

    def key_up(self, key):
        self.calls.append(('key_up', key))

    def mouse_down(self, button):
        self.calls.append(('mouse_down', button))

    def mouse_up(self, button):
        self.calls.append(('mouse_up', button))


class _TempSlots(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        p = mock.patch.object(ui_core, 'macros_dir', return_value=self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._td.cleanup)
        self.status = []
        self.macro = MacroController(lambda: None, self.status.append)
        self.addCleanup(self.macro.shutdown)


# ---- M1 / M11 / M12 / M9: events editor + buffer revision ----------------

class BufferRevisionTests(_TempSlots):
    def test_loading_equal_count_slot_bumps_revision(self):
        _write_slot(self.dir, 1, [_ev(0, 'a', 'down'), _ev(.1, 'a', 'up')])
        _write_slot(self.dir, 2, [_ev(0, 'b', 'down'), _ev(.1, 'b', 'up')])
        self.assertTrue(self.macro.load_slot(1))
        snap1 = self.macro.get_events_snapshot()
        self.assertTrue(self.macro.load_slot(2))
        snap2 = self.macro.get_events_snapshot()
        self.assertEqual(len(snap1['events']), len(snap2['events']))
        self.assertNotEqual(snap1['rev'], snap2['rev'])
        self.assertEqual((1, 2), (snap1['loaded'], snap2['loaded']))
        self.assertEqual(snap2['rev'], self.macro.snapshot()['macro_rev'])

    def test_stale_editor_save_never_writes_into_newly_loaded_slot(self):
        _write_slot(self.dir, 1, [_ev(0, 'a', 'down'), _ev(.1, 'a', 'up')])
        _write_slot(self.dir, 2, [_ev(0, 'b', 'down'), _ev(.1, 'b', 'up')])
        self.macro.load_slot(1)
        snap = self.macro.get_events_snapshot()     # editor opened on slot 1
        self.macro.load_slot(2)                      # same event count
        edited = [_ev(0, 'a', 'down'), _ev(.5, 'a', 'up')]
        res = self.macro.apply_events(edited, expected_rev=snap['rev'],
                                      expected_slot=snap['loaded'],
                                      persist=True)
        self.assertFalse(res)
        self.assertEqual('stale', res['reason'])
        self.assertEqual(2, res['loaded'])
        self.assertEqual('b', _read_slot(self.dir, 2)['events'][0]['key'])
        self.assertEqual(0.1, _read_slot(self.dir, 1)['events'][1]['time'])
        self.assertEqual('', self.macro.error())   # editor asks, no error

    def test_slot_mismatch_alone_is_stale(self):
        _write_slot(self.dir, 3, [_ev(0, 'a', 'down')])
        self.macro.load_slot(3)
        rev = self.macro.get_events_snapshot()['rev']
        res = self.macro.apply_events([_ev(0, 'c', 'down')],
                                      expected_rev=rev, expected_slot=4,
                                      persist=True)
        self.assertEqual('stale', res['reason'])

    def test_editor_save_applies_and_persists_in_one_command(self):
        _write_slot(self.dir, 1, [_ev(0, 'a', 'down'), _ev(.1, 'a', 'up')],
                    name='keep me')
        self.macro.load_slot(1)
        snap = self.macro.get_events_snapshot()
        res = self.macro.apply_events(
            [_ev(.3, 'a', 'up'), _ev(0, 'a', 'down')],
            expected_rev=snap['rev'], expected_slot=1, persist=True)
        self.assertTrue(res)
        self.assertEqual(1, res['saved_slot'])
        self.assertGreater(res['rev'], snap['rev'])
        payload = _read_slot(self.dir, 1)
        self.assertEqual('keep me', payload['name'])
        self.assertEqual([0, 0.3], [e['time'] for e in payload['events']])
        self.assertFalse(self.macro.snapshot()['macro_dirty'])

    def test_emptying_a_loaded_slot_is_refused_and_buffer_untouched(self):
        _write_slot(self.dir, 1, [_ev(0, 'a', 'down'), _ev(.1, 'a', 'up')])
        self.macro.load_slot(1)
        snap = self.macro.get_events_snapshot()
        res = self.macro.apply_events([], expected_rev=snap['rev'],
                                      expected_slot=1, persist=True)
        self.assertEqual('empty', res['reason'])
        self.assertEqual(2, self.macro.event_count())
        self.assertEqual(snap['rev'], self.macro.get_events_snapshot()['rev'])
        self.assertEqual(2, len(_read_slot(self.dir, 1)['events']))

    def test_failed_persist_keeps_buffer_dirty_and_reports(self):
        _write_slot(self.dir, 1, [_ev(0, 'a', 'down')])
        self.macro.load_slot(1)
        snap = self.macro.get_events_snapshot()
        with mock.patch.object(self.macro._engine, 'save',
                               side_effect=OSError('disk full')):
            res = self.macro.apply_events([_ev(0, 'b', 'down')],
                                          expected_rev=snap['rev'],
                                          expected_slot=1, persist=True)
        self.assertFalse(res)
        self.assertEqual('save_failed', res['reason'])
        self.assertTrue(res['applied'])
        self.assertTrue(self.macro.snapshot()['macro_dirty'])
        self.assertIn('disk full', self.macro.snapshot()['macro_error'])
        self.assertEqual('a', _read_slot(self.dir, 1)['events'][0]['key'])
        # The next successful command clears the error.
        self.assertTrue(self.macro.save_slot(1))
        self.assertEqual('', self.macro.snapshot()['macro_error'])

    def test_dropped_rows_are_counted_and_surfaced(self):
        res = self.macro.apply_events([
            _ev(0, 'a', 'down'),
            {'time': 0.1, 'device': 'joystick', 'key': 'a',
             'event_type': 'down'},
        ])
        self.assertTrue(res)
        self.assertEqual(1, res['dropped'])
        self.assertEqual(1, res['count'])
        self.assertIn('dropped', self.macro.snapshot()['macro_error'])

    def test_double_event_type_survives_editor_round_trip(self):
        res = self.macro.apply_events([
            _ev(0, 'left', 'double', device='mouse'),
            _ev(0.05, 'left', 'up', device='mouse'),
        ])
        self.assertTrue(res)
        self.assertEqual('double', self.macro.get_events()[0]['event_type'])

    def test_save_marks_clean_only_the_revision_it_wrote(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])

        def concurrent_change(*_a, **_k):
            # Simulates the buffer moving on while the write is in flight.
            self.macro._buffer_rev += 1

        with mock.patch.object(self.macro._engine, 'save',
                               side_effect=concurrent_change):
            self.assertTrue(self.macro.save_slot(5))
        self.assertTrue(self.macro.snapshot()['macro_dirty'])

    def test_slot_commands_wait_for_a_running_command(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.macro._command_lock.acquire()
        releaser = threading.Timer(0.2, self.macro._command_lock.release)
        releaser.start()
        t0 = time.monotonic()
        self.assertTrue(self.macro.save_slot(2, 'x'))
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)
        releaser.join()

    def test_busy_slot_command_reports_instead_of_silently_failing(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.macro._SLOT_CMD_WAIT_S = 0.05
        with self.macro._command_lock:
            res = self.macro.save_slot(2)
        self.assertEqual('busy', res['reason'])
        self.assertTrue(self.macro.error())

    def test_save_with_stale_expected_rev_is_refused(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        rev = self.macro.get_events_snapshot()['rev']
        self.macro.apply_events([_ev(0, 'b', 'down')])
        res = self.macro.save_slot(4, 'late', expected_rev=rev)
        self.assertEqual('stale', res['reason'])
        self.assertFalse((self.dir / 'macro_4.json').exists())


# ---- U17: clearing slots -------------------------------------------------

class ClearSlotTests(_TempSlots):
    def test_clearing_loaded_slot_detaches_and_marks_unsaved(self):
        _write_slot(self.dir, 6, [_ev(0, 'a', 'down')])
        self.macro.load_slot(6)
        rev = self.macro.get_events_snapshot()['rev']
        res = self.macro.clear_slot(6)
        self.assertTrue(res)
        self.assertTrue(res['detached'])
        snap = self.macro.snapshot()
        self.assertEqual(0, snap['macro_loaded'])
        self.assertTrue(snap['macro_dirty'])
        self.assertGreater(snap['macro_rev'], rev)
        self.assertEqual(1, snap['macro_events'])

    def test_rename_uses_atomic_engine_write_and_keeps_events(self):
        _write_slot(self.dir, 7, [_ev(0, 'a', 'down')], name='old')
        self.assertTrue(self.macro.rename_slot(7, '  new  '))
        payload = _read_slot(self.dir, 7)
        self.assertEqual('new', payload['name'])
        self.assertEqual(1, len(payload['events']))
        res = self.macro.rename_slot(8, 'nothing there')
        self.assertEqual('empty_slot', res['reason'])


# ---- M13 / M15 / U18: recording filter + live count ----------------------

class RecordingFilterTests(_TempSlots):
    def setUp(self):
        super().setUp()
        self.in_game = True
        self.pressed = set()
        patches = [
            mock.patch.object(ui_core.keyboard, 'hook', return_value='kb'),
            mock.patch.object(ui_core.keyboard, 'unhook'),
            mock.patch.object(ui_core.mouse, 'hook', return_value='ms'),
            mock.patch.object(ui_core.mouse, 'unhook'),
            mock.patch.object(ui_core.keyboard, 'is_pressed',
                              side_effect=lambda k: k in self.pressed),
            mock.patch.object(ui_core, 'is_allowed_game_only',
                              side_effect=lambda: self.in_game),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def keys(self):
        return [(e['key'], e['event_type']) for e in self.macro.get_events()]

    def test_release_after_alt_tab_is_recorded(self):
        self.assertTrue(self.macro.start_record((), ()))
        self.macro._on_kb_event(_kb('w', 'down', 17))
        self.in_game = False
        self.macro._on_kb_event(_kb('w', 'up', 17))
        self.macro._on_kb_event(_kb('e', 'down', 18))   # typed elsewhere
        self.macro._on_kb_event(_kb('e', 'up', 18))
        self.in_game = True
        self.macro._on_kb_event(_kb('w', 'down', 17))   # NOT auto-repeat
        self.macro._on_kb_event(_kb('w', 'up', 17))
        self.macro.stop_record()
        self.assertEqual([('w', 'down'), ('w', 'up'),
                          ('w', 'down'), ('w', 'up')], self.keys())

    def test_mouse_release_outside_game_and_press_outside_dropped(self):
        self.macro.start_record((), ())
        self.macro._on_mouse_event(mouse.ButtonEvent('down', 'left', 0))
        self.in_game = False
        self.macro._on_mouse_event(mouse.ButtonEvent('up', 'left', 0))
        self.macro._on_mouse_event(mouse.ButtonEvent('down', 'right', 0))
        self.macro._on_mouse_event(mouse.ButtonEvent('up', 'right', 0))
        self.macro.stop_record()
        self.assertEqual([('left', 'down'), ('left', 'up')], self.keys())

    def test_release_of_key_pressed_before_recording_is_dropped(self):
        self.macro.start_record((), ())
        self.macro._on_kb_event(_kb('q', 'up', 16))
        self.macro.stop_record()
        self.assertEqual([], self.keys())

    def test_release_matches_press_by_scan_code(self):
        self.macro.start_record((), ())
        self.macro._on_kb_event(_kb('1', 'down', 2))
        self.macro._on_kb_event(_kb('!', 'up', 2))      # Shift came down
        self.macro.stop_record()
        self.assertEqual([('1', 'down'), ('1', 'up')], self.keys())

    def test_record_chord_is_filtered_at_both_ends(self):
        self.pressed = {'ctrl', 'y'}      # still held from the start press
        self.macro.start_record((), (), chord='ctrl+y')
        self.pressed = set()
        self.macro._on_kb_event(_kb('y', 'down', 21))   # auto-repeat
        self.macro._on_kb_event(_kb('y', 'up', 21))
        self.macro._on_kb_event(_kb('ctrl', 'up', 29))
        # Unrelated uses of the same keys are kept.
        self.macro._on_kb_event(_kb('ctrl', 'down', 29))
        self.macro._on_kb_event(_kb('c', 'down', 46))
        self.macro._on_kb_event(_kb('c', 'up', 46))
        self.macro._on_kb_event(_kb('ctrl', 'up', 29))
        self.macro._on_kb_event(_kb('y', 'down', 21))
        self.macro._on_kb_event(_kb('y', 'up', 21))
        # The stop chord reaches the recorder before the hotkey callback.
        self.macro._on_kb_event(_kb('ctrl', 'down', 29))
        self.macro._on_kb_event(_kb('y', 'down', 21))
        self.macro.stop_record(chord='ctrl+y')
        self.assertEqual([('ctrl', 'down'), ('c', 'down'), ('c', 'up'),
                          ('ctrl', 'up'), ('y', 'down'), ('y', 'up')],
                         self.keys())

    def test_excluded_binding_matches_canonical_spelling(self):
        self.macro.start_record({'plus'}, ())
        self.macro._on_kb_event(_kb('+', 'down', 13))
        self.macro._on_kb_event(_kb('+', 'up', 13))
        self.macro.stop_record()
        self.assertEqual([], self.keys())

    def test_live_event_count_while_recording(self):
        self.macro.LIVE_COUNT_INTERVAL_S = 0.02
        self.macro.start_record((), ())
        self.macro._on_kb_event(_kb('a', 'down', 30))
        self.macro._on_kb_event(_kb('a', 'up', 30))
        self.assertTrue(_wait_for(lambda: any(
            set(s) == {'macro_events', 'macro_status_gen'}
            and s['macro_events'] == 2 for s in self.status)))
        self.macro.stop_record()

    def test_recording_bumps_revision_and_discards_with_warning(self):
        self.macro.apply_events([_ev(0, 'z', 'down')])   # unsaved buffer
        rev = self.macro.snapshot()['macro_rev']
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.macro.start_record((), ())
        self.assertIn('replaced 1 unsaved event', out.getvalue())
        self.macro.stop_record()
        self.assertGreater(self.macro.snapshot()['macro_rev'], rev)


# ---- M10 (hotkey side) ---------------------------------------------------

class LoadDiscardWarningTests(_TempSlots):
    def test_load_over_unsaved_buffer_logs_warning(self):
        _write_slot(self.dir, 2, [_ev(0, 'a', 'down')])
        self.macro.apply_events([_ev(0, 'q', 'down'), _ev(.1, 'q', 'up')])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.macro.load_slot(2))
        self.assertIn('discarded 2 unsaved events', out.getvalue())

    def test_load_empty_slot_reports_error(self):
        res = self.macro.load_slot(9)
        self.assertEqual('empty_slot', res['reason'])
        self.assertIn('Slot 9 is empty', self.macro.snapshot()['macro_error'])


# ---- U18: playback count for active slots ---------------------------------

class PlayingCountTests(_TempSlots):
    def test_active_slot_playback_reports_its_own_event_count(self):
        _write_slot(self.dir, 1, [_ev(0, 'x', 'down'), _ev(5, 'x', 'up'),
                                  _ev(5.1, 'y', 'down')])
        self.macro.apply_events([_ev(0, 'q', 'down')])
        controller = _Recorder()
        self.macro._controller_provider = lambda: controller
        self.assertTrue(self.macro.play_slot(1))
        snap = self.macro.snapshot()
        self.assertEqual(3, snap['macro_playing_events'])
        self.assertEqual(1, snap['macro_events'])
        self.macro.stop_play()
        self.assertTrue(_wait_for(
            lambda: self.macro.state() == MacroController.STATE_IDLE))
        self.assertEqual(0, self.macro.snapshot()['macro_playing_events'])

    def test_no_backend_is_a_visible_error(self):
        self.macro.apply_events([_ev(0, 'q', 'down')])
        res = self.macro.play()
        self.assertEqual('no_backend', res['reason'])
        self.assertTrue(self.macro.snapshot()['macro_error'])


# ---- M16: slot picker -------------------------------------------------------

class SlotPickerTests(_TempSlots):
    def setUp(self):
        super().setUp()
        self.in_game = True
        self.down_scans = set()
        self.unhook = mock.MagicMock()
        patches = [
            mock.patch.object(ui_core.keyboard, 'hook',
                              return_value='picker-hook'),
            mock.patch.object(ui_core.keyboard, 'unhook', self.unhook),
            mock.patch.object(ui_core.keyboard, 'is_pressed',
                              side_effect=lambda sc: sc in self.down_scans),
            mock.patch.object(ui_core, 'is_allowed_game_only',
                              side_effect=lambda: self.in_game),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.chosen = []
        p = mock.patch.object(self.macro, 'save_slot',
                              side_effect=lambda n: self.chosen.append(n))
        p.start()
        self.addCleanup(p.stop)

    def test_digit_in_game_is_swallowed_and_chosen_even_with_w_held(self):
        self.down_scans = {17}            # W held while walking
        self.assertTrue(self.macro.begin_save_pending())
        self.assertFalse(self.macro._picker_key_hook(_kb('3', 'down', 4)))
        self.assertTrue(_wait_for(lambda: self.chosen == [3]))
        # Its auto-repeat and release are swallowed too, then the hook goes.
        self.assertFalse(self.macro._picker_key_hook(_kb('3', 'down', 4)))
        self.assertFalse(self.macro._picker_key_hook(_kb('3', 'up', 4)))
        self.assertTrue(_wait_for(lambda: self.unhook.called))
        self.assertEqual([3], self.chosen)

    def test_digit_outside_game_passes_through_and_cancels(self):
        self.macro.begin_save_pending()
        self.in_game = False
        self.assertTrue(self.macro._picker_key_hook(_kb('1', 'down', 2)))
        self.assertTrue(_wait_for(lambda: self.macro._pending_action is None))
        time.sleep(0.05)
        self.assertEqual([], self.chosen)

    def test_leaving_the_game_cancels_the_picker(self):
        self.macro.begin_save_pending()
        self.in_game = False
        self.assertTrue(_wait_for(lambda: self.macro._pending_action is None))
        self.assertEqual([], self.chosen)

    def test_held_digit_repeat_is_not_a_choice(self):
        self.macro.begin_save_pending()
        self.down_scans = {2}             # '1' already down before arming
        self.assertTrue(self.macro._picker_key_hook(_kb('1', 'down', 2)))
        time.sleep(0.05)
        self.assertEqual([], self.chosen)
        self.macro._handle_slot_cancel()

    def test_zero_then_nine_picks_slot_18(self):
        self.macro.begin_save_pending()
        self.assertFalse(self.macro._picker_key_hook(_kb('0', 'down', 11)))
        self.assertFalse(self.macro._picker_key_hook(_kb('0', 'up', 11)))
        self.assertFalse(self.macro._picker_key_hook(_kb('9', 'down', 10)))
        self.assertTrue(_wait_for(lambda: self.chosen == [18]))

    def test_non_picker_keys_always_pass(self):
        self.macro.begin_save_pending()
        self.assertTrue(self.macro._picker_key_hook(_kb('w', 'down', 17)))
        self.macro._handle_slot_cancel()

    def test_hotkey_overwrite_is_logged_with_old_name(self):
        _write_slot(self.dir, 5, [_ev(0, 'a', 'down')], name='old combo')
        self.macro._pending_action = 'save'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.macro._handle_slot_choice(5)
        self.assertIn("overwriting slot 5 ('old combo')", out.getvalue())
        self.assertEqual([5], self.chosen)


# ---- M4 / U9: verified focus ------------------------------------------------

class _FakeUser32:
    def __init__(self, game=0, fg=500, accepts=True):
        self.game = game
        self.fg = fg
        self.accepts = accepts

    def FindWindowW(self, _cls, _title):
        return self.game

    def GetForegroundWindow(self):
        return self.fg

    def IsIconic(self, _h):
        return 0

    def ShowWindow(self, _h, _cmd):
        return 1

    def BringWindowToTop(self, _h):
        return 1

    def SetForegroundWindow(self, h):
        if self.accepts:
            self.fg = h
            return 1
        return 0

    def SwitchToThisWindow(self, _h, _alt):
        pass


class FocusGameWindowTests(unittest.TestCase):
    def _run(self, user32, **kw):
        fake = types.SimpleNamespace(windll=types.SimpleNamespace(user32=user32))
        with mock.patch.object(ui_core, 'ctypes', fake):
            return ui_core.focus_game_window_ex(**kw)

    def test_game_not_running(self):
        self.assertEqual((0, ui_core.FOCUS_GAME_NOT_FOUND),
                         self._run(_FakeUser32(game=0)))

    def test_focus_refused_is_reported_not_assumed(self):
        res = self._run(_FakeUser32(game=1234, accepts=False), timeout=0.05)
        self.assertEqual((0, ui_core.FOCUS_GAME_FAILED), res)

    def test_focus_verified(self):
        self.assertEqual((1234, ''), self._run(_FakeUser32(game=1234)))

    def test_stop_event_aborts_wait(self):
        evt = threading.Event()
        evt.set()
        res = self._run(_FakeUser32(game=1234, accepts=False), timeout=5,
                        stop_evt=evt)
        self.assertEqual((0, ui_core.FOCUS_GAME_STOPPED), res)


# ---- Bridge (ui.py) — refusals reach JS ------------------------------------

class _FakeBot:
    def __init__(self, running=False, stops=True):
        self.running = running
        self.stops = stops
        self._thread = None

    def is_running(self):
        return self.running

    def stop(self):
        if self.stops:
            self.running = False


class _BridgeBase(_TempSlots):
    """BridgeApi built without __init__ (no settings file, no window)."""

    def setUp(self):
        super().setUp()
        try:
            import ui
        except Exception as e:                       # pragma: no cover
            self.skipTest(f'ui.py not importable headless: {e}')
        self.ui = ui
        self.controller = _Recorder()
        self.macro._controller_provider = lambda: self.controller
        api = ui.BridgeApi.__new__(ui.BridgeApi)
        api._engine_lock = threading.RLock()
        api._lock = threading.Lock()
        api._macro = self.macro
        api._bot = _FakeBot()
        api._drain_stop = threading.Event()
        api._backend_error = ''
        api._settings = {
            'keybinds': {},
            'macro_active_macros': [{'slot': 0, 'hotkey': ''},
                                    {'slot': 4, 'hotkey': 'f2'},
                                    {'slot': 0, 'hotkey': ''}],
            'macro_conflict_policy': 'ignore',
        }
        api._install_keybinds = lambda: None
        api._enqueue_status = lambda _u: None
        self.api = api
        for p in (mock.patch.object(ui, 'save_settings'),
                  mock.patch.object(ui, 'macros_dir', return_value=self.dir)):
            p.start()
            self.addCleanup(p.stop)


class BridgeMacroTests(_BridgeBase):
    def test_play_refuses_when_game_is_not_running(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        with mock.patch.object(self.ui, 'focus_game_window_ex',
                               return_value=(0, 'game_not_found')):
            res = self.api.macro_play(False)
        self.assertEqual('game_not_found', res['reason'])
        self.assertIn("isn't running", self.macro.snapshot()['macro_error'])
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertEqual([], self.controller.calls)

    def test_play_refuses_when_focus_fails(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        with mock.patch.object(self.ui, 'focus_game_window_ex',
                               return_value=(0, 'focus_failed')):
            res = self.api.macro_play(False)
        self.assertEqual('focus_failed', res['reason'])
        self.assertTrue(res['message'])

    def test_empty_buffer_does_not_yank_focus(self):
        with mock.patch.object(self.ui, 'focus_game_window_ex') as focus:
            res = self.api.macro_play(False)
        focus.assert_not_called()
        self.assertEqual('empty_buffer', res['reason'])

    def test_forced_play_refuses_if_bot_did_not_exit(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.api._bot = _FakeBot(running=True, stops=False)
        self.assertEqual('bot_running', self.api.macro_play(False)['reason'])
        with mock.patch.object(self.ui, 'focus_game_window_ex',
                               return_value=(1, '')):
            res = self.api.macro_play(True)
        self.assertEqual('bot_stop_timeout', res['reason'])
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())

    def test_play_active_starts_after_verified_focus(self):
        _write_slot(self.dir, 4, [_ev(0, 'k', 'down'), _ev(0.01, 'k', 'up')])
        with mock.patch.object(self.ui, 'focus_game_window_ex',
                               return_value=(1, '')), \
             mock.patch.object(ui_core, 'is_allowed_game_only',
                               return_value=True):
            res = self.api.macro_play_active(2, False)
        self.assertTrue(res['ok'])
        self.assertTrue(_wait_for(lambda: ('key_down', 'k')
                                  in self.controller.calls))

    def test_clear_slot_reports_unassigned_active_macros(self):
        _write_slot(self.dir, 4, [_ev(0, 'k', 'down')])
        res = self.api.macro_clear_slot(4)
        self.assertTrue(res['ok'])
        self.assertEqual(['B'], res['unassigned'])
        self.assertEqual(0, self.api._settings['macro_active_macros'][1]['slot'])

    def test_duplicate_active_slot_explains_itself(self):
        _write_slot(self.dir, 4, [_ev(0, 'k', 'down')])
        res = self.api.macro_set_active_slot(1, 4)
        self.assertEqual('duplicate_slot', res['reason'])
        self.assertIn('active macro B', res['message'])
        self.assertIn('active macro B', self.macro.snapshot()['macro_error'])

    def test_get_events_returns_revision_envelope(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        res = self.api.macro_get_events()
        self.assertEqual(['events', 'rev', 'loaded', 'dirty', 'state'],
                         list(res))
        stale = self.api.macro_set_events([_ev(0, 'b', 'down')],
                                          res['rev'] - 1, 0, False)
        self.assertEqual('stale', stale['reason'])
        self.assertIs(type(stale), dict)


# ---- Review round 2 (MR1-MR7) --------------------------------------------

class _HookedRecording(_TempSlots):
    """Recording with hook installers + foreground patched."""

    def setUp(self):
        super().setUp()
        self.in_game = True
        self.pressed = set()
        patches = [
            mock.patch.object(ui_core.keyboard, 'hook', return_value='kb'),
            mock.patch.object(ui_core.keyboard, 'unhook'),
            mock.patch.object(ui_core.mouse, 'hook', return_value='ms'),
            mock.patch.object(ui_core.mouse, 'unhook'),
            mock.patch.object(ui_core.keyboard, 'is_pressed',
                              side_effect=lambda k: k in self.pressed),
            mock.patch.object(ui_core, 'is_allowed_game_only',
                              side_effect=lambda: self.in_game),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def keys(self):
        return [(e['key'], e['event_type']) for e in self.macro.get_events()]


class RecordingSessionTests(_HookedRecording):
    """MR1: an ingestion callback can never append outside its session."""

    def _paused_callback(self, event, session):
        """Run _on_kb_event on a thread, paused inside the foreground probe
        (i.e. after the cheap state pre-check, before the session lock)."""
        entered, gate = threading.Event(), threading.Event()

        def slow_probe():
            entered.set()
            gate.wait(2.0)
            return True

        patch = mock.patch.object(ui_core, 'is_allowed_game_only',
                                  side_effect=slow_probe)
        patch.start()
        t = threading.Thread(target=self.macro._on_kb_event,
                             args=(event, session))
        t.start()
        self.assertTrue(entered.wait(1.0))
        patch.stop()
        return t, gate

    def test_paused_callback_cannot_append_after_stop_apply_save(self):
        self.macro.start_record((), ())
        sid = self.macro._rec_live_session
        self.macro._on_kb_event(_kb('a', 'down', 30), sid)
        t, gate = self._paused_callback(_kb('x', 'down', 45), sid)
        self.assertTrue(self.macro.stop_record())
        snap = self.macro.get_events_snapshot()
        self.assertTrue(self.macro.apply_events(
            [_ev(0, 'b', 'down')], expected_rev=snap['rev'],
            expected_slot=snap['loaded']))
        self.assertTrue(self.macro.save_slot(3, 'saved'))
        rev = self.macro.get_events_snapshot()['rev']
        gate.set()
        t.join(1.0)
        self.assertFalse(t.is_alive())
        after = self.macro.get_events_snapshot()
        self.assertEqual([('b', 'down')], self.keys())
        self.assertEqual(rev, after['rev'])
        self.assertFalse(after['dirty'])
        self.assertEqual(_read_slot(self.dir, 3)['events'], after['events'])

    def test_callback_from_old_session_cannot_land_in_next_recording(self):
        self.macro.start_record((), ())
        old = self.macro._rec_live_session
        t, gate = self._paused_callback(_kb('x', 'down', 45), old)
        self.macro.stop_record()
        self.macro.start_record((), ())
        self.assertNotEqual(old, self.macro._rec_live_session)
        gate.set()
        t.join(1.0)
        self.macro.stop_record()
        self.assertEqual([], self.keys())

    def test_stop_waits_for_an_append_in_progress(self):
        self.macro.start_record((), ())
        sid = self.macro._rec_live_session
        real = self.macro._engine.record_keyboard
        inside, gate = threading.Event(), threading.Event()

        def slow_append(name, etype):
            real(name, etype)
            inside.set()
            gate.wait(2.0)

        self.macro._engine.record_keyboard = slow_append
        t = threading.Thread(target=self.macro._on_kb_event,
                             args=(_kb('x', 'down', 45), sid))
        t.start()
        self.assertTrue(inside.wait(1.0))
        done = threading.Event()
        stopper = threading.Thread(
            target=lambda: (self.macro.stop_record(), done.set()))
        stopper.start()
        self.assertFalse(done.wait(0.15))     # blocked on the append
        gate.set()
        t.join(1.0)
        stopper.join(1.0)
        self.assertTrue(done.is_set())
        snap = self.macro.get_events_snapshot()
        self.assertEqual([('x', 'down')], self.keys())
        self.assertEqual('idle', snap['state'])

    def test_hooks_are_installed_with_their_session(self):
        installed = []
        with mock.patch.object(ui_core.keyboard, 'hook',
                               side_effect=lambda cb: installed.append(cb)
                               or 'kb'):
            self.macro.start_record((), ())
            installed[0](_kb('q', 'down', 16))  # live session -> recorded
            self.macro.stop_record()
            self.assertEqual([('q', 'down')], self.keys())
            self.macro.start_record((), ())
        self.assertEqual(2, len(installed))
        installed[0](_kb('w', 'down', 17))      # first session's hook
        installed[1](_kb('e', 'down', 18))      # current session's hook
        self.macro.stop_record()
        self.assertEqual([('e', 'down')], self.keys())


class ChordTrimTests(_HookedRecording):
    """MR6: a hotkey Stop removes exactly the press that stopped it."""

    def test_tapped_and_released_chord_is_removed_entirely(self):
        self.macro.start_record((), (), track_chord='ctrl+y')
        for ev in (_kb('a', 'down', 30), _kb('a', 'up', 30),
                   _kb('ctrl', 'down', 29), _kb('y', 'down', 21),
                   _kb('y', 'up', 21), _kb('ctrl', 'up', 29)):
            self.macro._on_kb_event(ev)
        self.macro.stop_record(chord='ctrl+y')
        self.assertEqual([('a', 'down'), ('a', 'up')], self.keys())

    def test_ctrl_held_for_an_earlier_ctrl_click_is_kept(self):
        self.macro.start_record((), (), track_chord='ctrl+y')
        self.macro._on_kb_event(_kb('ctrl', 'down', 29))
        self.macro._on_mouse_event(mouse.ButtonEvent('down', 'left', 0))
        self.macro._on_mouse_event(mouse.ButtonEvent('up', 'left', 0))
        self.macro._on_kb_event(_kb('y', 'down', 21))
        self.macro.stop_record(chord='ctrl+y')
        self.assertEqual([('ctrl', 'down'), ('left', 'down'),
                          ('left', 'up')], self.keys())

    def test_bare_main_key_is_an_unrelated_use(self):
        self.macro.start_record((), (), track_chord='ctrl+y')
        for ev in (_kb('y', 'down', 21), _kb('y', 'up', 21),
                   _kb('a', 'down', 30), _kb('a', 'up', 30),
                   # hotkey callback beat the recorder: only Ctrl arrived
                   _kb('ctrl', 'down', 29)):
            self.macro._on_kb_event(ev)
        self.macro.stop_record(chord='ctrl+y')
        self.assertEqual([('y', 'down'), ('y', 'up'), ('a', 'down'),
                          ('a', 'up')], self.keys())

    def test_ui_stop_keeps_everything(self):
        self.macro.start_record((), (), track_chord='ctrl+y')
        for ev in (_kb('ctrl', 'down', 29), _kb('y', 'down', 21),
                   _kb('y', 'up', 21), _kb('ctrl', 'up', 29)):
            self.macro._on_kb_event(ev)
        self.macro.stop_record()                 # UI button: no chord
        self.assertEqual(4, len(self.keys()))


class StatusOrderTests(_HookedRecording):
    """MR3: an older snapshot can't publish after (and stick over) a
    newer one."""

    def test_stale_recording_snapshot_is_dropped(self):
        self.macro.start_record((), ())
        gate, entered = threading.Event(), threading.Event()
        real = self.macro.slot_names
        block = {'on': True}

        def slow_names():
            if block['on']:
                block['on'] = False
                entered.set()
                gate.wait(2.0)
            return real()

        self.macro.slot_names = slow_names
        old = threading.Thread(target=self.macro._emit)
        old.start()                          # reads state = recording
        self.assertTrue(entered.wait(1.0))
        self.macro.stop_record()             # publishes newer 'idle'
        gate.set()
        old.join(1.0)
        full = [s for s in self.status if 'macro_state' in s]
        self.assertEqual('idle', full[-1]['macro_state'])
        gens = [s['macro_status_gen'] for s in self.status]
        self.assertEqual(gens, sorted(gens))


class DiscardGuardTests(_TempSlots):
    """MR2: in-game Record / Load over an unsaved buffer need two presses."""

    def test_first_press_arms_second_proceeds(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.assertFalse(self.macro.confirm_discard_hotkey('record', 'Y'))
        snap = self.macro.snapshot()
        self.assertEqual('confirm_record', snap['macro_pending'])
        self.assertIn('press Y again within 3 s', snap['macro_pending_hint'])
        self.assertTrue(self.macro.confirm_discard_hotkey('record', 'Y'))
        self.assertEqual('', self.macro.snapshot()['macro_pending'])

    def test_nothing_unsaved_proceeds_at_once(self):
        self.assertTrue(self.macro.confirm_discard_hotkey('load', 'F11'))
        _write_slot(self.dir, 1, [_ev(0, 'a', 'down')])
        self.macro.load_slot(1)                       # clean buffer
        self.assertTrue(self.macro.confirm_discard_hotkey('load', 'F11'))

    def test_window_expires_and_actions_do_not_mix(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.macro.DISCARD_CONFIRM_S = 0.05
        self.assertFalse(self.macro.confirm_discard_hotkey('load'))
        self.assertFalse(self.macro.confirm_discard_hotkey('record'))
        time.sleep(0.1)
        self.assertFalse(self.macro.confirm_discard_hotkey('record'))
        self.assertTrue(_wait_for(
            lambda: self.macro.snapshot()['macro_pending'] == '', 0.5))


class ForegroundAtStartTests(_TempSlots):
    """MR4: backend first, then the game must be in front right before
    the worker starts."""

    def test_backend_resolved_before_foreground_check(self):
        order = []
        self.macro.apply_events([_ev(0, 'q', 'down')])
        controller = _Recorder()

        def provider():
            order.append('backend')
            return controller

        def probe():
            order.append('foreground')
            return False

        self.macro._controller_provider = provider
        with mock.patch.object(ui_core, 'is_allowed_game_only',
                               side_effect=probe), \
             mock.patch.object(ui_core, 'refresh_game_hwnd'):
            res = self.macro.play(require_foreground=True)
        self.assertEqual('not_foreground', res['reason'])
        self.assertEqual('backend', order[0])
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertEqual([], controller.calls)

    def test_active_slot_rechecks_foreground(self):
        _write_slot(self.dir, 2, [_ev(0, 'k', 'down')])
        self.macro._controller_provider = lambda: _Recorder()
        with mock.patch.object(ui_core, 'is_allowed_game_only',
                               return_value=False), \
             mock.patch.object(ui_core, 'refresh_game_hwnd'):
            res = self.macro.play_slot(2, require_foreground=True)
        self.assertEqual('not_foreground', res['reason'])
        self.assertTrue(self.macro.snapshot()['macro_error'])


class PickerReleaseTests(_TempSlots):
    """MR5: swallowed keys are released by scan code, with an expiry."""

    def setUp(self):
        super().setUp()
        self.in_game = True
        self.unhook = mock.MagicMock()
        for p in (mock.patch.object(ui_core.keyboard, 'hook',
                                    return_value='picker-hook'),
                  mock.patch.object(ui_core.keyboard, 'unhook', self.unhook),
                  mock.patch.object(ui_core.keyboard, 'is_pressed',
                                    return_value=False),
                  mock.patch.object(ui_core, 'is_allowed_game_only',
                                    side_effect=lambda: self.in_game)):
            p.start()
            self.addCleanup(p.stop)
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.chosen = []
        p = mock.patch.object(self.macro, 'save_slot',
                              side_effect=lambda n: self.chosen.append(n))
        p.start()
        self.addCleanup(p.stop)

    def test_release_renamed_by_shift_still_clears_the_swallow(self):
        self.macro.begin_save_pending()
        self.assertFalse(self.macro._picker_key_hook(_kb('1', 'down', 2)))
        self.assertTrue(_wait_for(lambda: self.chosen == [1]))
        # Shift went down before '1' came up: the release says '!'.
        self.assertFalse(self.macro._picker_key_hook(_kb('!', 'up', 2)))
        self.assertEqual({}, self.macro._picker_swallowed)
        self.in_game = False                  # e.g. typing in chat now
        self.assertTrue(self.macro._picker_key_hook(_kb('1', 'down', 2)))
        self.assertTrue(_wait_for(lambda: self.unhook.called))

    def test_lost_release_expires(self):
        self.macro.PICKER_SWALLOW_MAX_S = 0.05
        self.macro.begin_save_pending()
        self.assertFalse(self.macro._picker_key_hook(_kb('2', 'down', 3)))
        self.assertTrue(_wait_for(lambda: self.chosen == [2]))
        time.sleep(0.1)                       # the release never came
        self.assertTrue(self.macro._picker_key_hook(_kb('2', 'down', 3)))
        self.assertTrue(_wait_for(lambda: self.unhook.called))


class SlotAssociationTests(_TempSlots):
    """MR7: tying the buffer to a slot moves the revision; a slot-0 editor
    can't save over a buffer that now belongs to a slot."""

    def test_save_to_new_slot_bumps_revision_and_refuses_slot0_editor(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        editor = self.macro.get_events_snapshot()       # loaded 0
        self.assertEqual(0, editor['loaded'])
        self.assertTrue(self.macro.save_slot(3, 'three'))
        now = self.macro.get_events_snapshot()
        self.assertGreater(now['rev'], editor['rev'])
        res = self.macro.apply_events([_ev(0, 'b', 'down')],
                                      expected_rev=editor['rev'],
                                      expected_slot=0)
        self.assertEqual('stale', res['reason'])
        res = self.macro.apply_events([_ev(0, 'b', 'down')],
                                      expected_rev=now['rev'],
                                      expected_slot=0)
        self.assertEqual('stale', res['reason'])
        self.assertEqual('a', _read_slot(self.dir, 3)['events'][0]['key'])

    def test_resaving_the_loaded_slot_keeps_the_revision(self):
        _write_slot(self.dir, 4, [_ev(0, 'a', 'down')])
        self.macro.load_slot(4)
        rev = self.macro.get_events_snapshot()['rev']
        self.macro._engine.mark_dirty()
        self.assertTrue(self.macro.save_slot(4))
        self.assertEqual(rev, self.macro.get_events_snapshot()['rev'])


class BridgeRoundTwoTests(_BridgeBase):
    """MR2 / MR4 through the bridge + hotkey bodies."""

    def setUp(self):
        super().setUp()
        self.api._settings['keybinds'] = {
            self.ui.ACTION_MACRO_RECORD: 'ctrl+y',
            self.ui.ACTION_MACRO_LOAD: 'f11',
        }
        self.api._keybinds = types.SimpleNamespace(get_bindings=lambda: {})
        for p in (mock.patch.object(ui_core.keyboard, 'hook',
                                    return_value='kb'),
                  mock.patch.object(ui_core.keyboard, 'unhook'),
                  mock.patch.object(ui_core.mouse, 'hook', return_value='ms'),
                  mock.patch.object(ui_core.mouse, 'unhook'),
                  mock.patch.object(ui_core.keyboard, 'is_pressed',
                                    return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def test_record_hotkey_over_unsaved_needs_second_press(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        self.api._hotkey_macro_record()
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertIn('Ctrl+Y', self.macro.snapshot()['macro_pending_hint'])
        self.api._hotkey_macro_record()
        self.assertEqual(MacroController.STATE_RECORDING, self.macro.state())
        self.macro.stop_record()

    def test_load_hotkey_over_unsaved_needs_second_press(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        with mock.patch.object(self.macro, 'begin_load_pending') as arm:
            self.api._hotkey_macro_load()
            arm.assert_not_called()
            self.api._hotkey_macro_load()
            arm.assert_called_once()

    def test_ui_play_prepares_backend_before_switching_focus(self):
        order = []
        self.macro.apply_events([_ev(0, 'a', 'down')])

        def provider():
            order.append('backend')
            return self.controller

        def focus(**_kw):
            order.append('focus')
            return (1, '')

        self.macro._controller_provider = provider
        with mock.patch.object(self.ui, 'focus_game_window_ex',
                               side_effect=focus), \
             mock.patch.object(ui_core, 'is_allowed_game_only',
                               return_value=True):
            res = self.api.macro_play(False)
        self.assertTrue(res['ok'])
        self.assertEqual(['backend', 'focus'], order[:2])
        self.macro.stop_play()

    def test_ui_play_refuses_if_user_left_the_game_after_focus(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        with mock.patch.object(self.ui, 'focus_game_window_ex',
                               return_value=(1, '')), \
             mock.patch.object(ui_core, 'is_allowed_game_only',
                               return_value=False), \
             mock.patch.object(ui_core, 'refresh_game_hwnd'):
            res = self.api.macro_play(False)
        self.assertEqual('not_foreground', res['reason'])
        self.assertEqual([], self.controller.calls)

    def test_hotkey_play_rechecks_foreground(self):
        self.macro.apply_events([_ev(0, 'a', 'down')])
        with mock.patch.object(ui_core, 'is_allowed_game_only',
                               return_value=False), \
             mock.patch.object(ui_core, 'refresh_game_hwnd'):
            self.api._hotkey_macro_play()
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertEqual([], self.controller.calls)


class ShutdownLatchTests(_HookedRecording):
    """BU1 follow-up: nothing starts after MacroController.shutdown()."""

    def test_starts_after_shutdown_are_refused(self):
        _write_slot(self.dir, 1, [_ev(0, 'k', 'down')])
        self.macro.apply_events([_ev(0, 'q', 'down')])
        controller = _Recorder()
        self.macro._controller_provider = lambda: controller
        self.macro.shutdown()
        ui_core.keyboard.hook.reset_mock()
        self.assertEqual('closing', self.macro.start_record((), ())['reason'])
        self.assertEqual('closing', self.macro.play()['reason'])
        self.assertEqual('closing', self.macro.play_slot(1)['reason'])
        self.assertEqual('closing', self.macro.begin_save_pending()['reason'])
        self.assertEqual('closing', self.macro.begin_load_pending()['reason'])
        ui_core.keyboard.hook.assert_not_called()
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertEqual([], controller.calls)

    def test_play_waiting_on_backend_cannot_start_after_shutdown(self):
        self.macro.apply_events([_ev(0, 'q', 'down')])
        controller = _Recorder()
        entered, gate = threading.Event(), threading.Event()

        def slow_provider():                 # first Arduino connect
            entered.set()
            gate.wait(2.0)
            return controller

        self.macro._controller_provider = slow_provider
        result = {}
        t = threading.Thread(
            target=lambda: result.update(res=self.macro.play()))
        t.start()
        self.assertTrue(entered.wait(1.0))
        self.macro.shutdown()                # app closes meanwhile
        gate.set()
        t.join(2.0)
        self.assertEqual('closing', result['res']['reason'])
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertEqual([], controller.calls)


class BridgeTeardownTests(_BridgeBase):
    """BU1 follow-up: macro starts queued behind detach() refuse."""

    def setUp(self):
        super().setUp()
        self.api._settings['keybinds'] = {
            self.ui.ACTION_MACRO_RECORD: 'ctrl+y',
            self.ui.ACTION_MACRO_LOAD: 'f11',
        }
        self.api._keybinds = types.SimpleNamespace(get_bindings=lambda: {})
        self.kb_hook = mock.MagicMock(return_value='kb')
        for p in (mock.patch.object(ui_core.keyboard, 'hook', self.kb_hook),
                  mock.patch.object(ui_core.keyboard, 'unhook'),
                  mock.patch.object(ui_core.mouse, 'hook', return_value='ms'),
                  mock.patch.object(ui_core.mouse, 'unhook'),
                  mock.patch.object(ui_core.keyboard, 'is_pressed',
                                    return_value=False),
                  mock.patch.object(ui_core, 'is_allowed_game_only',
                                    return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        _write_slot(self.dir, 4, [_ev(0, 'k', 'down')])
        self.api._tearing_down = True

    def test_button_starts_refuse_with_closing(self):
        with mock.patch.object(self.ui, 'focus_game_window_ex',
                               return_value=(1, '')) as focus:
            self.assertEqual({'ok': False, 'reason': 'closing'},
                             self.api.macro_toggle_record(False))
            self.assertEqual('closing', self.api.macro_toggle_record(True)['reason'])
            self.macro.apply_events([_ev(0, 'a', 'down')])
            self.assertEqual('closing', self.api.macro_play(False)['reason'])
            self.assertEqual('closing',
                             self.api.macro_play_active(2, False)['reason'])
        focus.assert_not_called()
        self.kb_hook.assert_not_called()
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertEqual([], self.controller.calls)

    def test_hotkey_starts_do_nothing(self):
        self.api._hotkey_macro_record()
        self.api._hotkey_macro_play()
        self.api._hotkey_macro_active(2)
        self.api._hotkey_macro_save()
        self.api._hotkey_macro_load()
        self.kb_hook.assert_not_called()
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())
        self.assertEqual('', self.macro.snapshot()['macro_pending'])
        self.assertEqual([], self.controller.calls)

    def test_stopping_a_recording_still_works(self):
        self.api._tearing_down = False
        self.assertTrue(self.api.macro_toggle_record(False)['ok'])
        self.api._tearing_down = True
        self.assertTrue(self.api.macro_toggle_record(False)['ok'])
        self.assertEqual(MacroController.STATE_IDLE, self.macro.state())


# ---- static frontend checks (U19 / M23) ------------------------------------

class FrontendStaticTests(unittest.TestCase):
    def test_editor_offers_double_and_no_mojibake(self):
        js = (ROOT / 'pc_client' / 'web' / 'app.js').read_text(encoding='utf-8')
        html = (ROOT / 'pc_client' / 'web' / 'index.html').read_text(
            encoding='utf-8')
        self.assertIn('<option value="double"', js)
        for bad in ('Â·', 'â€', 'Ã'):
            self.assertNotIn(bad, js)
            self.assertNotIn(bad, html)


if __name__ == '__main__':
    unittest.main()
