"""Regression tests for the Main-tab / bot-orchestration UX fixes.

Covers BotController (last_error reporting, no silent controller=None
fallback, SystemExit containment, Stop-wins-over-pending-restart, pause
while stopping, explicit debug set), the persisted Main-tab options
helpers, settings save, make_input_backend's cancel_evt forwarding, and
the BridgeApi backend plumbing (_get_arduino health rebuild, atomic
backend switch, force-start refusal, change-only status drain).

BridgeApi methods are exercised on a hand-built instance
(object.__new__) so no window / hooks / settings file are touched.

Run:  python -m unittest discover -s tests -p "test_*.py"
"""
import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc_client'))

import ui_core                                   # noqa: E402
from ui_core import BotController                # noqa: E402


def _wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class _StatusSink:
    """Collects BotController status updates like BridgeApi._status."""

    def __init__(self):
        self.lock = threading.Lock()
        self.updates = []
        self.status = {}

    def __call__(self, update):
        with self.lock:
            self.updates.append(dict(update))
            self.status.update(update)

    def get(self, key, default=None):
        with self.lock:
            return self.status.get(key, default)


class _BotTestBase(unittest.TestCase):
    def setUp(self):
        # Keep the worker from touching process-wide Windows state.
        for name in ('set_dpi_aware', 'boost_timer', 'restore_timer'):
            p = mock.patch.object(ui_core, name, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.sink = _StatusSink()

    def make_bot(self, provider):
        bot = BotController(self.sink, controller_provider=provider)
        self.addCleanup(self._shutdown, bot)
        return bot

    @staticmethod
    def _shutdown(bot):
        bot.stop()
        if bot._thread is not None:
            bot._thread.join(timeout=2.0)

    def wait_idle(self, bot):
        self.assertTrue(_wait_until(lambda: not bot.is_running()))
        self.assertTrue(_wait_until(lambda: self.sink.get('state') == 'idle'))


def _blocking_standalone(started=None, exit_delay=0.0):
    """Fake run_standalone: runs until stop_evt, then lingers a bit."""
    def fake(stop_evt, debug_evt, status_cb=None, pause_evt=None,
             controller=None):
        if started is not None:
            started.set()
        stop_evt.wait(5.0)
        if exit_delay:
            time.sleep(exit_delay)
    return fake


class BackendFailureTests(_BotTestBase):
    """U4: a failed backend is a hard, visible error — never a fallback."""

    def test_provider_none_sets_last_error_and_never_runs_runner(self):
        runner = mock.Mock()
        with mock.patch.object(ui_core, 'run_standalone', runner):
            bot = self.make_bot(lambda: None)
            self.assertTrue(bot.start('standalone', {}))
            self.wait_idle(bot)
        runner.assert_not_called()
        self.assertTrue(self.sink.get('last_error').startswith(
            BotController.BACKEND_UNAVAILABLE_PREFIX))

    def test_provider_exception_message_is_user_visible(self):
        def provider():
            raise RuntimeError('Arduino not found — plug it in')
        with mock.patch.object(ui_core, 'run_standalone', mock.Mock()):
            bot = self.make_bot(provider)
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertEqual(
            self.sink.get('last_error'),
            BotController.BACKEND_UNAVAILABLE_PREFIX
            + 'Arduino not found — plug it in')

    def test_provider_system_exit_is_contained(self):
        def provider():
            raise SystemExit(1)
        with mock.patch.object(ui_core, 'run_standalone', mock.Mock()):
            bot = self.make_bot(provider)
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertTrue(self.sink.get('last_error'))

    def test_provider_gets_stop_event_and_stop_during_build_is_silent(self):
        seen = {}
        entered = threading.Event()

        def provider(cancel_evt=None):
            seen['evt'] = cancel_evt
            entered.set()
            cancel_evt.wait(2.0)           # the Arduino's 2 s settle
            raise RuntimeError('Arduino connection cancelled')

        runner = mock.Mock()
        with mock.patch.object(ui_core, 'run_standalone', runner):
            bot = self.make_bot(provider)
            bot.start('standalone', {})
            self.assertTrue(entered.wait(2.0))
            self.assertIs(seen['evt'], bot._stop_evt)
            t0 = time.monotonic()
            bot.stop()
            self.wait_idle(bot)
            self.assertLess(time.monotonic() - t0, 1.0)
        runner.assert_not_called()
        self.assertEqual(self.sink.get('last_error'), '')


class WorkerCrashTests(_BotTestBase):
    """U4 / U5: crashes and sys.exit() inside runners become last_error."""

    def test_system_exit_in_runner_reports_crash(self):
        def runner(*a, **k):
            raise SystemExit(1)
        with mock.patch.object(ui_core, 'run_standalone', runner):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'),
                         BotController.CRASH_MESSAGE)

    def test_runner_specific_error_is_not_clobbered(self):
        def runner(stop_evt, debug_evt, status_cb=None, **k):
            status_cb({'last_error': 'Genshin window not found'})
            raise RuntimeError('boom')
        with mock.patch.object(ui_core, 'run_standalone', runner):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'),
                         'Genshin window not found')

    def test_standalone_returning_without_stop_is_flagged(self):
        with mock.patch.object(ui_core, 'run_standalone',
                               lambda *a, **k: None):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'),
                         BotController.CRASH_MESSAGE)

    def test_abort_caused_by_user_stop_is_not_an_error(self):
        started = threading.Event()

        def runner(stop_evt, *a, **k):
            started.set()
            stop_evt.wait(2.0)
            raise RuntimeError('album-init aborted by stop_evt')
        with mock.patch.object(ui_core, 'run_standalone', runner):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            bot.stop()
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'), '')

    def test_new_start_clears_previous_error(self):
        with mock.patch.object(ui_core, 'run_standalone',
                               lambda *a, **k: None):
            bot = self.make_bot(lambda: None)
            bot.start('standalone', {})
            self.wait_idle(bot)
            self.assertTrue(self.sink.get('last_error'))
        started = threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               _blocking_standalone(started)):
            bot._controller_provider = lambda: object()
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            self.assertEqual(self.sink.get('last_error'), '')
            bot.stop()
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'), '')

    def test_runner_clearing_error_rearms_fallback(self):
        """A runner emitting last_error '' clears its earlier report, so a
        later crash still surfaces the generic message."""
        def runner(stop_evt, debug_evt, status_cb=None, **k):
            status_cb({'last_error': 'Game window minimized'})
            status_cb({'last_error': ''})
            raise RuntimeError('boom')
        with mock.patch.object(ui_core, 'run_standalone', runner):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'),
                         BotController.CRASH_MESSAGE)

    def test_album_setup_error_becomes_last_error(self):
        import album
        album_cls = mock.Mock(side_effect=album.AlbumSetupError(
            'Genshin window not found — start the game'))
        with mock.patch.object(ui_core, 'AlbumRunner', album_cls), \
                mock.patch('traceback.print_exc') as tb:
            bot = self.make_bot(lambda: object())
            bot.start('album', {'difficulty': ['legendary']})
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'),
                         'Genshin window not found — start the game')
        tb.assert_not_called()

    def test_album_failed_result_has_error_floor(self):
        for result, expected in (('failed',
                                  BotController.ALBUM_FAILED_MESSAGE),
                                 ('completed', '')):
            with self.subTest(result=result):
                self.sink = _StatusSink()
                runner = mock.Mock()
                runner.run.return_value = result
                with mock.patch.object(ui_core, 'AlbumRunner',
                                       mock.Mock(return_value=runner)):
                    bot = self.make_bot(lambda: object())
                    bot.start('album', {'difficulty': ['legendary']})
                    self.wait_idle(bot)
                self.assertEqual(self.sink.get('last_error'), expected)
                runner.close.assert_called_once()

    def test_album_runner_crash_reports(self):
        album = mock.Mock(side_effect=ValueError('bad difficulty'))
        with mock.patch.object(ui_core, 'AlbumRunner', album):
            bot = self.make_bot(lambda: object())
            bot.start('album', {'difficulty': ['legendary']})
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('last_error'),
                         BotController.CRASH_MESSAGE)


class RestartAndPauseTests(_BotTestBase):
    def test_stop_during_restart_join_wins(self):
        """R5: Stop pressed while restart_in_mode is joining the old
        worker must not be undone by the new mode starting."""
        started = threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               _blocking_standalone(started, exit_delay=0.3)):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            result = {}
            t = threading.Thread(
                target=lambda: result.setdefault(
                    'ok', bot.restart_in_mode('standalone', {})))
            t.start()
            # Let restart's own stop() land, then the user's Stop.
            self.assertTrue(_wait_until(lambda: bot._stop_evt.is_set()))
            time.sleep(0.05)
            bot.stop()
            t.join(timeout=5.0)
            self.assertFalse(result['ok'])
            self.wait_idle(bot)

    def test_restart_without_interference_starts_new_mode(self):
        started = threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               _blocking_standalone(started)):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            started.clear()
            self.assertTrue(bot.restart_in_mode('standalone', {}))
            self.assertTrue(started.wait(2.0))
            self.assertTrue(bot.is_running())

    def test_restart_refused_when_stop_already_in_flight(self):
        started = threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               _blocking_standalone(started, exit_delay=0.3)):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            bot.stop()
            self.assertFalse(bot.restart_in_mode('album', {}))
            self.wait_idle(bot)
            self.assertFalse(bot.is_running())

    def test_pause_ignored_while_stopping(self):
        """U24."""
        started = threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               _blocking_standalone(started, exit_delay=0.3)):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            bot.stop()
            self.assertTrue(bot.is_running())     # still winding down
            bot.pause_toggle()
            self.assertFalse(bot._pause_evt.is_set())
            self.assertEqual(self.sink.get('state'), 'stopping')
            self.wait_idle(bot)


class DebugTests(_BotTestBase):
    """U12: explicit set, toggle returns the new value, exit re-emits."""

    def test_set_debug_is_a_set_not_a_toggle(self):
        bot = self.make_bot(lambda: object())
        self.assertTrue(bot.set_debug(True))
        self.assertTrue(bot.set_debug(True))
        self.assertTrue(bot.debug_enabled)
        self.assertFalse(bot.set_debug(False))
        self.assertFalse(bot.set_debug(False))
        self.assertFalse(bot.debug_enabled)
        self.assertTrue(bot.toggle_debug())
        self.assertEqual(self.sink.get('debug'), True)

    def test_worker_exit_emits_debug(self):
        started = threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               _blocking_standalone(started)):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {'debug': True})
            self.assertTrue(started.wait(2.0))
            bot.stop()
            self.wait_idle(bot)
        last = [u for u in self.sink.updates if u.get('state') == 'idle'][-1]
        self.assertIn('debug', last)


class BotOptionHelpersTests(unittest.TestCase):
    """U7 / U15 helpers."""

    def test_clamp_song_count(self):
        self.assertEqual(ui_core.clamp_song_count(0), 1)
        self.assertEqual(ui_core.clamp_song_count(13), ui_core.ALBUM_SONG_COUNT)
        self.assertEqual(ui_core.clamp_song_count('5'), 5)
        self.assertIsNone(ui_core.clamp_song_count(''))
        self.assertIsNone(ui_core.clamp_song_count('abc'))
        self.assertIsNone(ui_core.clamp_song_count(None))
        self.assertIsNone(ui_core.clamp_song_count(True))

    def test_normalize_difficulty(self):
        self.assertEqual(ui_core.normalize_difficulty(['legendary', 'normal']),
                         ['normal', 'legendary'])
        self.assertEqual(ui_core.normalize_difficulty('all'),
                         ['normal', 'hard', 'pro', 'legendary'])
        self.assertEqual(ui_core.normalize_difficulty('pro'), ['pro'])
        self.assertIsNone(ui_core.normalize_difficulty([]))
        self.assertIsNone(ui_core.normalize_difficulty(['bogus', 3]))
        self.assertIsNone(ui_core.normalize_difficulty(None))

    def test_normalize_bot_options_field_by_field(self):
        got = ui_core.normalize_bot_options({
            'mode': 'ALBUM', 'songs': 99, 'difficulty': ['hard', 'x'],
            'replay_canorus': 'yes', 'debug': True})
        self.assertEqual(got, {'mode': 'album',
                               'songs': ui_core.ALBUM_SONG_COUNT,
                               'difficulty': ['hard'],
                               'replay_canorus': False})
        self.assertEqual(ui_core.normalize_bot_options('garbage'),
                         ui_core.default_bot_options())

    def test_debug_is_not_persisted(self):
        opts = {'mode': 'album', 'songs': 3, 'difficulty': ['pro'],
                'replay_canorus': True, 'debug': True,
                'mid_song_start': True}
        self.assertEqual(ui_core.bot_options_for_settings(opts),
                         {'mode': 'album', 'songs': 3,
                          'difficulty': ['pro'], 'replay_canorus': True})


class SettingsPersistenceTests(unittest.TestCase):
    def test_bot_options_round_trip_and_atomic_save(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'sub' / 'ui_settings.json'
            with mock.patch.object(ui_core, 'settings_path',
                                   return_value=path):
                s = ui_core.load_settings()
                self.assertEqual(s['bot_options'],
                                 ui_core.default_bot_options())
                s['bot_options'] = {'mode': 'album', 'songs': 4,
                                    'difficulty': ['hard', 'pro'],
                                    'replay_canorus': True}
                ui_core.save_settings(s)
                self.assertEqual(
                    json.loads(path.read_text(encoding='utf-8'))
                    ['bot_options']['songs'], 4)
                # No temp files left behind.
                self.assertEqual([p.name for p in path.parent.iterdir()],
                                 ['ui_settings.json'])
                again = ui_core.load_settings()
                self.assertEqual(again['bot_options'], s['bot_options'])

    def test_corrupt_bot_options_fall_back(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'ui_settings.json'
            path.write_text(json.dumps({'bot_options': {
                'mode': 'nope', 'songs': 'x', 'difficulty': 'zzz'}}),
                encoding='utf-8')
            with mock.patch.object(ui_core, 'settings_path',
                                   return_value=path):
                s = ui_core.load_settings()
        self.assertEqual(s['bot_options'], ui_core.default_bot_options())

    def test_saved_keybinds_are_sanitized(self):
        """C-hotkeys request: unusable saved bindings fall back to the
        action default; duplicates compared in canonical form."""
        defaults = ui_core.UI_KEYBINDS_DEFAULT
        kb = ui_core._sanitize_keybinds({
            'start_stop': 'mouse:left',           # reserved
            'pause': 'F9',                        # canonicalized
            'debug': 'definitely-not-a-key',      # unparseable
            'macro_record': 42,                   # wrong type
            'macro_save': '',                     # explicitly unbound
            'future_action': 'k',                 # unknown: carried
        })
        self.assertEqual(kb['start_stop'], defaults['start_stop'])
        self.assertEqual(kb['pause'], 'f9')
        self.assertEqual(kb['debug'], defaults['debug'])
        self.assertEqual(kb['macro_record'], defaults['macro_record'])
        self.assertEqual(kb['macro_save'], '')
        self.assertEqual(kb['future_action'], 'k')
        self.assertEqual(ui_core._sanitize_keybinds('garbage'),
                         dict(defaults))

    def test_duplicate_keybind_falls_back(self):
        kb = ui_core._sanitize_keybinds({'start_stop': 'f9', 'pause': 'F9'})
        self.assertEqual(kb['start_stop'], 'f9')
        # pause collided (canonical) and its default f9 is taken too.
        self.assertEqual(kb['pause'], '')

    def test_active_macro_hotkeys_sanitized(self):
        active = ui_core._normalize_active_macros(
            [{'slot': 1, 'hotkey': 'mouse:left'},
             {'slot': 2, 'hotkey': 'F8'},
             {'slot': 3, 'hotkey': 'G'}],
            reserved_hotkeys=['f8'])
        self.assertEqual([a['hotkey'] for a in active], ['', '', 'g'])
        self.assertEqual([a['slot'] for a in active], [1, 2, 3])

    def test_fallback_writer_is_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'x.json'
            path.write_text('{"old": 1}', encoding='utf-8')
            with self.assertRaises(TypeError):
                ui_core._atomic_write_json_fallback(path, {'bad': object()})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                             {'old': 1})
            ui_core._atomic_write_json_fallback(path, {'new': 2}, indent=2)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                             {'new': 2})
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()),
                             ['x.json'])


class MakeInputBackendTests(unittest.TestCase):
    def test_cancel_evt_forwarded_to_arduino(self):
        import controller
        seen = {}

        class FakeArduino:
            def __init__(self, cancel_evt=None):
                seen['cancel_evt'] = cancel_evt

        evt = threading.Event()
        with mock.patch.object(controller, 'ArduinoHIDController',
                               FakeArduino):
            ui_core.make_input_backend('arduino', cancel_evt=evt)
        self.assertIs(seen['cancel_evt'], evt)

    def test_legacy_constructor_without_cancel_evt(self):
        import controller

        class LegacyArduino:
            def __init__(self):
                pass

        with mock.patch.object(controller, 'ArduinoHIDController',
                               LegacyArduino):
            got = ui_core.make_input_backend('arduino',
                                             cancel_evt=threading.Event())
        self.assertIsInstance(got, LegacyArduino)

    def test_accepts_kwarg(self):
        self.assertTrue(ui_core._accepts_kwarg(lambda cancel_evt=None: 0,
                                               'cancel_evt'))
        self.assertTrue(ui_core._accepts_kwarg(lambda **kw: 0, 'cancel_evt'))
        self.assertFalse(ui_core._accepts_kwarg(lambda: 0, 'cancel_evt'))


# --- BridgeApi plumbing ------------------------------------------------------

try:
    import ui                                    # noqa: E402
except Exception as _e:                          # pragma: no cover
    ui = None
    _UI_IMPORT_ERROR = _e


class _FakeCtrl:
    def __init__(self, kind, healthy=True, close_gate=None):
        self.kind = kind
        self._healthy = healthy
        self.closed = False
        self._close_gate = close_gate

    @property
    def healthy(self):
        return self._healthy

    def close(self):
        if self._close_gate is not None:
            self._close_gate.wait(2.0)
        self.closed = True


class _FakeBot:
    def __init__(self, running=False):
        self.running = running
        self.started = []

    def is_running(self):
        return self.running

    def is_stopping(self):
        return False

    def stop(self):
        self.running = False

    def start(self, mode, opts):
        self.started.append((mode, opts))
        self.running = True
        return True

    def report_error(self, msg):
        return True

    @property
    def debug_enabled(self):
        return False

    _thread = None


class _FakeMacro:
    STATE_RECORDING = 'recording'
    STATE_PLAYING = 'playing'

    def __init__(self, busy=False):
        self.busy = busy

    def is_busy(self):
        return self.busy

    def state(self):
        return 'playing' if self.busy else 'idle'


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class BridgeBackendTests(unittest.TestCase):
    def make_api(self, backend='software'):
        api = object.__new__(ui.BridgeApi)
        api._lock = threading.Lock()
        api._arduino_lock = threading.Lock()
        api._engine_lock = threading.RLock()
        api._status_lock = threading.Lock()
        api._status = {'last_error': ''}
        api._settings = {'input_backend': backend,
                         'bot_options': ui_core.default_bot_options()}
        api._opts = {**ui_core.default_bot_options(), 'debug': False}
        api._arduino = None
        api._backend_error = ''
        api._theme_reminder_shown = True
        api._tearing_down = False
        api._restart_pending = 0
        api._push_in_flight = False
        api._stopping = False
        api._detached = False
        api._close_confirmed = False
        api._bot = _FakeBot()
        api._macro = _FakeMacro()
        p = mock.patch.object(ui, 'save_settings')
        self.save_settings = p.start()
        self.addCleanup(p.stop)
        return api

    def test_unhealthy_cached_backend_is_closed_and_rebuilt(self):
        api = self.make_api()
        dead = _FakeCtrl('software', healthy=False)
        api._arduino = dead
        with mock.patch.object(ui, 'make_input_backend',
                               side_effect=lambda n, cancel_evt=None:
                               _FakeCtrl(n)):
            got = api._get_arduino()
        self.assertTrue(dead.closed)
        self.assertIsNot(got, dead)
        self.assertIs(api._arduino, got)

    def test_connection_error_surfaces_message_to_bot(self):
        api = self.make_api('arduino')
        err = ui.ArduinoConnectionError('Arduino not found — plug it in')
        with mock.patch.object(ui, 'make_input_backend', side_effect=err):
            self.assertIsNone(api._get_arduino())
            self.assertEqual(api._backend_error,
                             'Arduino not found — plug it in')
            with self.assertRaises(RuntimeError) as cm:
                api._bot_backend(cancel_evt=threading.Event())
        self.assertIn('Arduino not found', str(cm.exception))

    def test_cancel_evt_reaches_factory(self):
        api = self.make_api('arduino')
        evt = threading.Event()
        seen = {}

        def factory(name, cancel_evt=None):
            seen['evt'] = cancel_evt
            return _FakeCtrl(name)
        with mock.patch.object(ui, 'make_input_backend', side_effect=factory):
            api._bot_backend(cancel_evt=evt)
        self.assertIs(seen['evt'], evt)

    def test_build_during_switch_gets_new_backend_type(self):
        """M5/R4: a lazy build racing the old backend's close() must
        produce the NEW type, never the old one."""
        api = self.make_api('arduino')
        gate = threading.Event()
        old = _FakeCtrl('arduino', close_gate=gate)
        api._arduino = old
        built = []

        def factory(name, cancel_evt=None):
            built.append(name)
            return _FakeCtrl(name)

        with mock.patch.object(ui, 'make_input_backend', side_effect=factory):
            switch = threading.Thread(
                target=lambda: api.set_input_backend('software'))
            switch.start()
            self.assertTrue(_wait_until(lambda: api._arduino is None))
            got = {}
            builder = threading.Thread(
                target=lambda: got.setdefault('c', api._get_arduino()))
            builder.start()
            time.sleep(0.05)
            gate.set()                     # let the old close() finish
            switch.join(2.0)
            builder.join(2.0)
        self.assertTrue(old.closed)
        self.assertEqual(built, ['software'])
        self.assertEqual(got['c'].kind, 'software')

    def test_forced_switch_refused_if_worker_does_not_stop(self):
        api = self.make_api('arduino')
        api._bot = _FakeBot(running=True)
        api._bot.stop = lambda: None       # stuck worker
        api._stop_bot_blocking = lambda: False
        api._stop_macro_blocking = lambda: None
        res = api.set_input_backend('software', force=True)
        self.assertEqual(res, {'ok': False, 'reason': 'still_busy'})
        self.assertEqual(api._settings['input_backend'], 'arduino')

    def test_force_start_refused_while_macro_still_busy(self):
        api = self.make_api()
        api._macro = _FakeMacro(busy=True)
        api._stop_macro_blocking = lambda: None   # macro never stops
        self.assertEqual(api.start_stop_force(),
                         {'ok': False, 'reason': 'busy'})
        self.assertEqual(api._bot.started, [])

    def test_force_start_never_turns_into_stop(self):
        api = self.make_api()
        api._bot = _FakeBot(running=True)
        self.assertTrue(api.start_stop_force()['ok'])
        self.assertTrue(api._bot.running)

    def test_main_tab_options_persist(self):
        api = self.make_api()
        self.assertTrue(api.set_songs('0'))
        self.assertEqual(api._opts['songs'], 1)
        self.assertFalse(api.set_songs('abc'))
        self.assertTrue(api.set_difficulty(['legendary', 'hard']))
        self.assertTrue(api.set_replay_canorus(True))
        self.assertTrue(api.set_mode('album'))
        self.assertEqual(api._settings['bot_options'], {
            'mode': 'album', 'songs': 1,
            'difficulty': ['hard', 'legendary'], 'replay_canorus': True})
        self.assertTrue(self.save_settings.called)

    def test_drain_pushes_only_on_change_or_log(self):
        """U21."""
        api = self.make_api()
        api._window = object()
        api._stopping = False
        api._drain_stop = threading.Event()
        api._log_queue = queue.Queue()
        api._orig_stderr = sys.__stderr__
        pushes = []
        api._push = lambda status, log: pushes.append((dict(status), log)) or True
        with mock.patch.object(ui, 'is_admin', return_value=True):
            t = threading.Thread(target=api._drain_loop, daemon=True)
            t.start()
            try:
                time.sleep(0.4)            # ~8 ticks, nothing changes
                self.assertEqual(len(pushes), 1)
                api._log_queue.put('hello\n')
                self.assertTrue(_wait_until(lambda: len(pushes) == 2, 1.0))
                self.assertEqual(pushes[-1][1], 'hello\n')
                api._enqueue_status({'state': 'running'})
                self.assertTrue(_wait_until(lambda: len(pushes) == 3, 1.0))
            finally:
                api._drain_stop.set()
                t.join(1.0)


# --- Direction A data contract (genshin-ui-mocks/ui-data-contract.md) -------

_STAMP_RE = r'^\d\d:\d\d:\d\d '


class LogTimestampTests(unittest.TestCase):
    """QueueWriter prefixes every log LINE with `HH:MM:SS `."""

    def _drain(self, q):
        out = ''
        while not q.empty():
            out += q.get_nowait()
        return out

    def test_every_line_stamped_once(self):
        q = queue.Queue()
        w = ui_core.QueueWriter(q)
        print('[album] song', 3, file=w)      # print = 4 separate writes
        w.write('first\nsecond\n')
        w.write('par')
        w.write('tial line\n')
        w.write('\n')
        lines = self._drain(q).split('\n')
        self.assertEqual(lines[-1], '')
        lines = lines[:-1]
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertRegex(line, _STAMP_RE)
        self.assertEqual([ln[9:] for ln in lines],
                         ['[album] song 3', 'first', 'second',
                          'partial line', ''])

    def test_js_regex_contract(self):
        import re
        q = queue.Queue()
        ui_core.QueueWriter(q).write('[sw-input] ready\nuntagged\n')
        pat = re.compile(r'^(\d\d:\d\d:\d\d) (?:\[([^\]]+)\]\s*)?(.*)$')
        first, second = self._drain(q).splitlines()
        m1, m2 = pat.match(first), pat.match(second)
        self.assertEqual((m1.group(2), m1.group(3)), ('sw-input', 'ready'))
        self.assertEqual((m2.group(2), m2.group(3)), (None, 'untagged'))

    def test_stdout_and_stderr_share_line_state(self):
        q = queue.Queue()
        out = ui_core.QueueWriter(q)
        err = ui_core.QueueWriter(q)
        out.write('no newline yet')
        err.write(' + tail\n')                 # same pane line
        self.assertEqual(self._drain(q).count(':'), 2)   # one stamp

    def test_mirror_gets_stamped_text_and_return_is_input_len(self):
        q = queue.Queue()
        mirror = mock.Mock()
        w = ui_core.QueueWriter(q, mirror=mirror)
        self.assertEqual(w.write('hello\n'), len('hello\n'))
        self.assertRegex(mirror.write.call_args[0][0], _STAMP_RE + 'hello\n')

    def test_concurrent_writers_keep_lines_whole(self):
        q = queue.Queue()
        w = ui_core.QueueWriter(q)

        def spam(tag):
            for i in range(200):
                w.write(f'[{tag}] line {i}\n')
        threads = [threading.Thread(target=spam, args=(t,))
                   for t in ('a', 'b', 'c')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = self._drain(q).splitlines()
        self.assertEqual(len(lines), 600)
        for line in lines:
            self.assertRegex(line, _STAMP_RE + r'\[[abc]\] line \d+$')


class RunMetadataTests(_BotTestBase):
    """album_progress + run_started_at owned by BotController."""

    def test_start_resets_then_run_sets_started_at(self):
        started = threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               _blocking_standalone(started)):
            bot = self.make_bot(lambda: object())
            t0 = time.time()
            bot.start('standalone', {})
            first = self.sink.updates[0]
            self.assertEqual(first['run_started_at'], 0)
            self.assertIsNone(first['album_progress'])
            self.assertTrue(started.wait(2.0))
            self.assertGreaterEqual(self.sink.get('run_started_at'), t0)
            bot.stop()
            self.wait_idle(bot)
        # Kept after the run ends (UI shows the final elapsed).
        self.assertGreaterEqual(self.sink.get('run_started_at'), t0)

    def test_backend_failure_never_sets_started_at(self):
        with mock.patch.object(ui_core, 'run_standalone', mock.Mock()):
            bot = self.make_bot(lambda: None)
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertEqual(self.sink.get('run_started_at'), 0)

    def test_album_progress_merged_copied_kept_and_reset(self):
        results = []

        class FakeAlbum:
            def __init__(self, status_cb=None, **kw):
                self.cb = status_cb

            def run(self, songs_count=None):
                self.cb({'album_progress': {
                    'index': 0, 'total': 2, 'difficulty': 'hard',
                    'difficulties': ['hard'], 'results': results}})
                results.append('played')
                self.cb({'album_progress': {'index': 1,
                                            'results': results}})
                results.append('skipped')       # mutated after emitting
                return 'completed'

            def close(self):
                pass

        with mock.patch.object(ui_core, 'AlbumRunner', FakeAlbum):
            bot = self.make_bot(lambda: object())
            bot.start('album', {'difficulty': ['hard']})
            self.wait_idle(bot)
        prog = self.sink.get('album_progress')
        self.assertEqual(prog, {'index': 1, 'total': 2, 'difficulty': 'hard',
                                'difficulties': ['hard'],
                                'results': ['played']})
        # Next Start resets it to None.
        with mock.patch.object(ui_core, 'run_standalone',
                               lambda *a, **k: None):
            bot.start('standalone', {})
            self.wait_idle(bot)
        self.assertIsNone(self.sink.get('album_progress'))


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class DirectionABridgeTests(unittest.TestCase):
    def make_api(self, backend='software'):
        api = BridgeBackendTests.make_api(self, backend)
        api._detached = False
        api._close_confirmed = False
        api._window = None
        api._hwnd_cache = 0
        return api

    # -- prereq tip ----------------------------------------------------------

    def test_dismiss_prereq_tip_persists_and_mirrors(self):
        api = self.make_api()
        self.assertEqual(api.dismiss_prereq_tip(), {'ok': True})
        self.assertIs(api._settings['prereq_tip_dismissed'], True)
        self.assertEqual(self.save_settings.call_count, 1)
        self.assertIs(api._status['prereq_tip_dismissed'], True)
        api.dismiss_prereq_tip()                 # idempotent: no re-save
        self.assertEqual(self.save_settings.call_count, 1)
        api._macro.snapshot = lambda: {}
        api._settings['keybinds'] = {}
        self.assertIs(api.get_initial_state()['prereq_tip_dismissed'], True)

    def test_prereq_tip_setting_sanitized(self):
        for raw, want in ((True, True), ('yes', False), (1, False),
                          (None, False)):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as td:
                path = Path(td) / 'ui_settings.json'
                path.write_text(json.dumps({'prereq_tip_dismissed': raw}),
                                encoding='utf-8')
                with mock.patch.object(ui_core, 'settings_path',
                                       return_value=path):
                    self.assertIs(
                        ui_core.load_settings()['prereq_tip_dismissed'], want)

    # -- lanes / game window -------------------------------------------------

    def test_lanes_fallbacks_and_normalization(self):
        api = self.make_api()
        idle = [False] * 6
        with mock.patch.object(ui, '_detector_mod', object()):
            api._bot.running = True
            self.assertEqual(api._read_lanes(), idle)      # no function
        fake = mock.Mock()
        with mock.patch.object(ui, '_detector_mod', fake):
            fake.active_lanes.return_value = (1, 0, 0, 0, 0, 1)
            api._bot.running = False
            self.assertEqual(api._read_lanes(), idle)      # bot idle
            api._bot.running = True
            self.assertEqual(api._read_lanes(),
                             [True, False, False, False, False, True])
            fake.active_lanes.return_value = None
            self.assertEqual(api._read_lanes(), idle)
            fake.active_lanes.return_value = [True]
            self.assertEqual(api._read_lanes(), [True] + [False] * 5)
            fake.active_lanes.side_effect = RuntimeError('torn down')
            self.assertEqual(api._read_lanes(), idle)

    def test_game_window_poll(self):
        api = self.make_api()
        with mock.patch.object(ui, '_find_game_window', return_value=1234):
            self.assertIs(api._poll_game_window(), True)
        with mock.patch.object(ui, '_find_game_window', return_value=None):
            self.assertIs(api._poll_game_window(), False)
        with mock.patch.object(ui, '_find_game_window',
                               side_effect=OSError('x')):
            self.assertIs(api._poll_game_window(), False)

    def test_drain_pushes_lanes_and_window_state(self):
        api = self.make_api()
        api._window = object()
        api._stopping = False
        api._drain_stop = threading.Event()
        api._log_queue = queue.Queue()
        api._orig_stderr = sys.__stderr__
        pushes = []
        api._push = lambda status, log: pushes.append(dict(status)) or True
        lanes = {'v': [False] * 6}
        api._read_lanes = lambda: list(lanes['v'])
        with mock.patch.object(ui, 'is_admin', return_value=True), \
                mock.patch.object(ui, '_find_game_window', return_value=7):
            t = threading.Thread(target=api._drain_loop, daemon=True)
            t.start()
            try:
                self.assertTrue(_wait_until(lambda: len(pushes) >= 1, 1.0))
                self.assertIs(pushes[0]['game_window_found'], True)
                self.assertEqual(pushes[0]['lanes'], [False] * 6)
                n = len(pushes)
                lanes['v'] = [False, True, False, False, False, False]
                self.assertTrue(_wait_until(lambda: len(pushes) > n, 1.0))
                self.assertEqual(pushes[-1]['lanes'][1], True)
            finally:
                api._drain_stop.set()
                t.join(1.0)

    # -- window close --------------------------------------------------------

    def _macro_with(self, recording=False, dirty=False):
        m = _FakeMacro()
        m.state = lambda: 'recording' if recording else 'idle'
        m._engine = mock.Mock()
        m._engine.is_dirty.return_value = dirty
        return m

    def test_allow_close_without_unsaved_work_never_asks(self):
        api = self.make_api()
        api._macro = self._macro_with()
        api._ask_discard_unsaved_macro = mock.Mock(return_value=False)
        self.assertTrue(api.allow_close())
        api._ask_discard_unsaved_macro.assert_not_called()

    def test_allow_close_asks_when_dirty_or_recording(self):
        for kw in ({'dirty': True}, {'recording': True}):
            with self.subTest(**kw):
                api = self.make_api()
                api._macro = self._macro_with(**kw)
                api._ask_discard_unsaved_macro = mock.Mock(return_value=False)
                self.assertFalse(api.allow_close())        # No → cancel
                api._ask_discard_unsaved_macro.return_value = True
                self.assertTrue(api.allow_close())         # Yes → close
                self.assertTrue(api._close_confirmed)
                api._ask_discard_unsaved_macro.reset_mock()
                self.assertTrue(api.allow_close())         # asked once
                api._ask_discard_unsaved_macro.assert_not_called()

    def test_close_confirmed_skips_native_prompt(self):
        api = self.make_api()
        api._macro = self._macro_with(dirty=True)
        api._ask_discard_unsaved_macro = mock.Mock(return_value=False)
        gate = {}

        class FakeWindow:
            def destroy(self_inner):
                # destroy() → FormClosing → our closing handler.
                gate['allowed'] = api.allow_close()
                if gate['allowed']:
                    api._detached = True

        api._window = FakeWindow()
        self.assertTrue(api.close(True))
        self.assertTrue(gate['allowed'])
        api._ask_discard_unsaved_macro.assert_not_called()

    def test_close_unconfirmed_can_be_cancelled(self):
        api = self.make_api()
        api._macro = self._macro_with(dirty=True)
        api._ask_discard_unsaved_macro = mock.Mock(return_value=False)

        class FakeWindow:
            def destroy(self_inner):
                if api.allow_close():
                    api._detached = True

        api._window = FakeWindow()
        self.assertFalse(api.close())
        api._ask_discard_unsaved_macro.assert_called_once()
        self.assertFalse(api._detached)

    def test_pywebview_closing_false_cancels(self):
        """The installed pywebview must treat a False return from a
        `closing` handler as cancel — the close gate relies on it."""
        from webview.event import Event
        evt = Event(None, True)                # closing is a locking event
        evt += lambda: False
        self.assertTrue(evt.set())             # True → args.Cancel = True
        ok = Event(None, True)
        ok += lambda: None
        self.assertFalse(ok.set())


# --- review follow-ups: BU1 / BU2 / BU3 / BU8 --------------------------------

class StateLockTests(_BotTestBase):
    """BU3: Stop / Pause / worker finalization publish state under one
    lock and decide on the run flag, not thread liveness."""

    def _finished_bot_with_lingering_thread(self):
        """A bot whose run already finalized ('idle' published) but whose
        thread object still reports alive — the window the race lived in."""
        with mock.patch.object(ui_core, 'run_standalone',
                               lambda *a, **k: None):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.wait_idle(bot)
        linger = threading.Event()
        t = threading.Thread(target=linger.wait, args=(2.0,), daemon=True)
        t.start()
        self.addCleanup(linger.set)
        bot._thread = t
        self.assertTrue(bot.is_running())
        return bot

    def test_stop_after_finalizer_does_not_publish_stopping(self):
        bot = self._finished_bot_with_lingering_thread()
        bot.stop()
        self.assertEqual(self.sink.get('state'), 'idle')
        self.assertFalse(bot._stop_evt.is_set())

    def test_pause_after_finalizer_does_not_publish_paused(self):
        bot = self._finished_bot_with_lingering_thread()
        n = len(self.sink.updates)
        bot.pause_toggle()
        self.assertEqual(len(self.sink.updates), n)
        self.assertFalse(bot._pause_evt.is_set())
        self.assertEqual(self.sink.get('state'), 'idle')

    def test_racing_stop_and_pause_always_end_idle(self):
        for i in range(25):
            self.sink = _StatusSink()

            def runner(stop_evt, *a, **k):
                stop_evt.wait(0.002 * (i % 4))
            with mock.patch.object(ui_core, 'run_standalone', runner):
                bot = self.make_bot(lambda: object())
                bot.start('standalone', {})
                hammer = [threading.Thread(target=bot.stop),
                          threading.Thread(target=bot.pause_toggle),
                          threading.Thread(target=bot.pause_toggle)]
                for t in hammer:
                    t.start()
                for t in hammer:
                    t.join(2.0)
                self.wait_idle(bot)
                time.sleep(0.01)
            states = [u['state'] for u in self.sink.updates if 'state' in u]
            self.assertEqual(states[-1], 'idle', f'iteration {i}: {states}')


class RestartStopBarrierTests(_BotTestBase):
    """BU9: a Stop that lands between restart_in_mode's stop-in-flight
    check and its own internal stop must abort the restart (it used to be
    absorbed into the restart's newer generation → bot restarted after
    the user's Stop)."""

    def _counting_runner(self, runs, started):
        def runner(stop_evt, *a, **k):
            runs.append(1)
            started.set()
            stop_evt.wait(5.0)
            time.sleep(0.05)
        return runner

    def test_stop_between_check_and_internal_stop_aborts(self):
        runs, started = [], threading.Event()
        injected = threading.Event()
        restart_thread = {}
        with mock.patch.object(ui_core, 'run_standalone',
                               self._counting_runner(runs, started)):
            bot = self.make_bot(lambda: object())

            class BarrierEvent(threading.Event):
                """The restart thread's stop-in-flight check reads False,
                then — before restart issues its own stop — an external
                Stop runs to completion on another thread."""
                def is_set(self_inner):
                    value = threading.Event.is_set(self_inner)
                    if (threading.current_thread() is restart_thread.get('t')
                            and not injected.is_set()):
                        injected.set()
                        s = threading.Thread(target=bot.stop)
                        s.start()
                        s.join(2.0)
                    return value

            bot._stop_evt = BarrierEvent()
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            result = {}
            t = threading.Thread(target=lambda: result.setdefault(
                'ok', bot.restart_in_mode('standalone', {})))
            restart_thread['t'] = t
            t.start()
            t.join(5.0)
            self.assertTrue(injected.is_set(), 'barrier never hit')
            self.assertFalse(result['ok'])
            self.wait_idle(bot)
        self.assertEqual(len(runs), 1, 'restart started a new run after Stop')
        self.assertFalse(bot.is_running())

    def test_stale_request_generation_aborts(self):
        """Bridge path: the request generation is captured before waiting
        for the engine lock; a Stop after that (old run already gone by the
        time restart runs) must still win."""
        runs, started = [], threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               self._counting_runner(runs, started)):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            req_gen = bot.generation
            bot.stop()                        # user's Stop after the request
            self.wait_idle(bot)
            self.assertFalse(bot.restart_in_mode(
                'standalone', {}, expected_generation=req_gen))
        self.assertEqual(len(runs), 1)
        self.assertFalse(bot.is_running())

    def test_matching_request_generation_still_restarts(self):
        runs, started = [], threading.Event()
        with mock.patch.object(ui_core, 'run_standalone',
                               self._counting_runner(runs, started)):
            bot = self.make_bot(lambda: object())
            bot.start('standalone', {})
            self.assertTrue(started.wait(2.0))
            started.clear()
            self.assertTrue(bot.restart_in_mode(
                'standalone', {}, expected_generation=bot.generation))
            self.assertTrue(started.wait(2.0))
        self.assertEqual(len(runs), 2)


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class ReviewBridgeTests(unittest.TestCase):
    def make_api(self):
        api = BridgeBackendTests.make_api(self)
        api._hwnd_cache = 0
        api._window = None
        return api

    # -- BU2: Stop wins over a pending mode switch at the bridge -------------

    def _real_bot(self, api, exit_delay):
        for name in ('set_dpi_aware', 'boost_timer', 'restore_timer'):
            p = mock.patch.object(ui_core, name, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        started = threading.Event()
        p = mock.patch.object(ui_core, 'run_standalone',
                              _blocking_standalone(started, exit_delay))
        p.start()
        self.addCleanup(p.stop)
        album = mock.Mock()
        p = mock.patch.object(ui_core, 'AlbumRunner', album)
        p.start()
        self.addCleanup(p.stop)
        sink = _StatusSink()
        api._enqueue_status = sink
        api._bot = BotController(sink, controller_provider=lambda: object())
        self.addCleanup(_BotTestBase._shutdown, api._bot)
        return started, album, sink

    def test_f8_during_restart_join_stops_without_waiting(self):
        api = self.make_api()
        started, album, sink = self._real_bot(api, exit_delay=0.4)
        self.assertTrue(api.start_stop())
        self.assertTrue(started.wait(2.0))
        result = {}
        t = threading.Thread(
            target=lambda: result.setdefault('ok', api.restart_in_mode('album')))
        t.start()
        self.assertTrue(_wait_until(lambda: api._bot._stop_evt.is_set()))
        time.sleep(0.05)                       # restart is joining now
        t0 = time.monotonic()
        self.assertTrue(api.start_stop())      # F8
        self.assertLess(time.monotonic() - t0, 0.2,
                        'Stop waited for the engine lock')
        t.join(5.0)
        self.assertFalse(result['ok'])
        self.assertTrue(_wait_until(lambda: not api._bot.is_running()))
        album.assert_not_called()

    def test_f8_while_restart_waits_for_engine_lock(self):
        api = self.make_api()
        started, album, sink = self._real_bot(api, exit_delay=0.2)
        api.start_stop()
        self.assertTrue(started.wait(2.0))
        holder_go = threading.Event()
        holding = threading.Event()

        def holder():
            with api._engine_lock:
                holding.set()
                holder_go.wait(2.0)
        h = threading.Thread(target=holder)
        h.start()
        self.assertTrue(holding.wait(1.0))
        t = threading.Thread(target=lambda: api.restart_in_mode('album'))
        t.start()
        self.assertTrue(_wait_until(lambda: api._restart_pending > 0))
        t0 = time.monotonic()
        api.start_stop()                       # F8 → Stop, lock-free
        self.assertLess(time.monotonic() - t0, 0.2)
        holder_go.set()
        h.join(2.0)
        t.join(5.0)
        self.assertTrue(_wait_until(lambda: not api._bot.is_running()))
        album.assert_not_called()

    # -- BU1: teardown refuses / serializes ownership transitions ------------

    def test_start_paths_and_backend_refuse_while_tearing_down(self):
        api = self.make_api()
        api._tearing_down = True
        with mock.patch.object(ui, 'make_input_backend') as factory:
            self.assertIsNone(api._get_arduino())
            factory.assert_not_called()
        self.assertFalse(api.start_stop())
        self.assertEqual(api.start_stop_force(),
                         {'ok': False, 'reason': 'closing'})
        self.assertEqual(api.set_input_backend('arduino'),
                         {'ok': False, 'reason': 'closing'})
        api._bot = _FakeBot(running=True)
        self.assertFalse(api.restart_in_mode('album'))
        self.assertEqual(api._bot.started, [])

    def _detach_ready(self, api, order):
        api._drain_stop = threading.Event()
        api._drain_thread = None
        api._orig_stdout = sys.stdout
        api._orig_stderr = sys.stderr
        api._keybinds = mock.Mock()
        api._keybinds.clear_all.side_effect = lambda: order.append('keybinds')
        api._macro.shutdown = lambda: order.append('macro.shutdown')
        bot = api._bot
        bot.shutdown = lambda: order.append('bot.shutdown')
        old_stop = bot.stop
        bot.stop = lambda: (order.append('bot.stop'), old_stop())[1]
        ard = mock.Mock()
        ard.close.side_effect = lambda: order.append('backend.close')
        api._arduino = ard
        for target, name in ((ui, '_release_ll_hooks'),
                             (ui.keyboard, 'unhook_all'),
                             (ui.mouse, 'unhook_all')):
            p = mock.patch.object(
                target, name,
                side_effect=lambda n=name, t=target: order.append(
                    'hooks' if n == '_release_ll_hooks' else
                    f'{t.__name__}.unhook_all'))
            p.start()
            self.addCleanup(p.stop)
        return ard

    def test_queued_start_cannot_resume_after_teardown(self):
        api = self.make_api()
        order = []
        ard = self._detach_ready(api, order)
        holder_go = threading.Event()
        holding = threading.Event()

        def holder():                          # a transition in progress
            with api._engine_lock:
                holding.set()
                holder_go.wait(3.0)
                order.append('holder.release')
        h = threading.Thread(target=holder)
        h.start()
        self.assertTrue(holding.wait(1.0))
        starter = threading.Thread(target=api.start_stop)   # queued Start
        starter.start()
        time.sleep(0.05)
        closer = threading.Thread(target=api.detach)
        closer.start()
        self.assertTrue(_wait_until(lambda: api._tearing_down))
        time.sleep(0.05)
        self.assertNotIn('backend.close', order)   # waits for the holder
        holder_go.set()
        for t in (h, starter, closer):
            t.join(3.0)
        self.assertEqual(api._bot.started, [])
        ard.close.assert_called_once()
        self.assertLess(order.index('holder.release'),
                        order.index('backend.close'))

    def test_teardown_lock_wait_is_bounded(self):
        api = self.make_api()
        order = []
        self._detach_ready(api, order)
        release = threading.Event()

        def stuck():
            with api._engine_lock:
                release.wait(3.0)
        s = threading.Thread(target=stuck)
        s.start()
        self.addCleanup(lambda: (release.set(), s.join(3.0)))
        time.sleep(0.02)
        with mock.patch.object(ui, 'TEARDOWN_ENGINE_LOCK_TIMEOUT_S', 0.1):
            t0 = time.monotonic()
            api.detach()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn('backend.close', order)

    # -- BU8: hooks first, drain can't block the close -----------------------

    def test_detach_order_hooks_first_then_stop_pushing(self):
        api = self.make_api()
        order = []
        self._detach_ready(api, order)
        drain = mock.Mock()
        drain.join.side_effect = lambda timeout=None: order.append(
            ('drain.join', api._stopping, api._drain_stop.is_set()))
        api._drain_thread = drain
        api.detach()
        self.assertEqual(order[0], 'hooks')
        join = [o for o in order if isinstance(o, tuple)]
        self.assertEqual(join, [('drain.join', True, True)])
        self.assertLess(order.index('hooks'), order.index(join[0]))
        self.assertLess(order.index(join[0]), order.index('backend.close'))
        self.assertLess(order.index('bot.shutdown'),
                        order.index('backend.close'))
        self.assertTrue(api._tearing_down)

    def test_detach_does_not_wait_on_in_flight_push(self):
        api = self.make_api()
        order = []
        self._detach_ready(api, order)
        drain = mock.Mock()
        api._drain_thread = drain
        api._push_in_flight = True
        api.detach()
        drain.join.assert_not_called()

    def test_push_skips_evaluate_once_stopping_and_tracks_in_flight(self):
        api = self.make_api()
        seen = {}
        win = mock.Mock()
        win.evaluate_js.side_effect = lambda s: seen.setdefault(
            'in_flight', api._push_in_flight)
        api._window = win
        self.assertTrue(api._push({'state': 'idle'}, ''))
        self.assertTrue(seen['in_flight'])
        self.assertFalse(api._push_in_flight)
        api._stopping = True
        self.assertFalse(api._push({'state': 'idle'}, ''))
        self.assertEqual(win.evaluate_js.call_count, 1)
        self.assertFalse(api._push_in_flight)


if __name__ == '__main__':
    unittest.main()
