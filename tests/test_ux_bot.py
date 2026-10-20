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


if __name__ == '__main__':
    unittest.main()
