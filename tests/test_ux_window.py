"""Frameless-window chrome (v1.6.2): edge / corner resize, remembered
size / position / maximized state, and the page theme.

Covers start_window_resize (edge validation, one at a time, refused while
maximized), the resize math for every edge incl. min-size clamping, the
resize loop (exit on button release / teardown, final rect persisted),
toggle_maximize persistence, the startup restore (exact rect, then
re-maximize), the close-time save, settings sanitation for window_rect /
window_maximized / theme, the launch-time on-screen check (off-screen
fallback, frame compensation, oversize fit), set_theme persistence and
get_initial_state's new fields (no side effects).

Every Win32 call goes through a fake _UiWin32 (api._win32 / win=), and
BridgeApi instances are hand-built (object.__new__), so no window, hook or
real settings file is touched.

Run:  python -m unittest discover -s tests -p "test_*.py"
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc_client'))

import ui_core                                   # noqa: E402

try:
    import ui                                    # noqa: E402
except Exception:                                # pragma: no cover
    ui = None

HWND = 0x1234


def _wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.002)
    return pred()


class FakeWin:
    """Stands in for ui._UiWin32. `feed` is a list of cursor positions the
    resize loop consumes one per poll; the primary button reads as held
    while entries remain (or while `hold` is True)."""

    def __init__(self, rect=(100, 100, 820, 820), scale=1.0,
                 work=(0, 0, 1920, 1040), areas=None, frame=(16, 39)):
        self.rect = tuple(rect)
        self.scale = scale
        self.work = work
        self.areas = [work] if areas is None else areas
        self.frame = frame
        self.zoomed = False
        self.iconic = False
        self.cursor = (500, 500)
        self.feed = []
        self.hold = False
        self.set_calls = []
        self.fail_set = False

    def window_rect(self, hwnd):
        return self.rect

    def set_window_rect(self, hwnd, rect):
        self.set_calls.append(tuple(rect))
        if self.fail_set:
            return False
        self.rect = tuple(rect)
        return True

    def cursor_pos(self):
        if self.feed:
            self.cursor = self.feed.pop(0)
        return self.cursor

    def primary_button_vk(self):
        return 0x01

    def button_down(self, vk):
        return bool(self.feed) or self.hold

    def is_zoomed(self, hwnd):
        return self.zoomed

    def is_iconic(self, hwnd):
        return self.iconic

    def dpi_scale(self, hwnd):
        return self.scale

    def system_scale(self):
        return self.scale

    def frame_delta(self):
        return self.frame

    def monitor_work_area(self, hwnd):
        return self.work

    def work_areas(self):
        return list(self.areas)


class ResizeMathTests(unittest.TestCase):
    START = (100, 200, 900, 900)          # 800 x 700
    MIN = (640, 600)

    def r(self, edge, dx, dy):
        return ui.resize_rect(edge, self.START, dx, dy, *self.MIN)

    @unittest.skipIf(ui is None, 'ui.py not importable headless')
    def test_each_edge_moves_only_its_sides(self):
        cases = {
            'n': (100, 170, 900, 900), 's': (100, 200, 900, 930),
            'e': (100, 200, 920, 900), 'w': (80, 200, 900, 900),
            'ne': (100, 170, 920, 900), 'nw': (80, 170, 900, 900),
            'se': (100, 200, 920, 930), 'sw': (80, 200, 900, 930),
        }
        # Outward drag: n/w go up/left (-), s/e go down/right (+).
        for edge, want in cases.items():
            with self.subTest(edge=edge):
                dx = -20 if 'w' in edge else 20
                dy = -30 if 'n' in edge else 30
                self.assertEqual(self.r(edge, dx, dy), want)

    @unittest.skipIf(ui is None, 'ui.py not importable headless')
    def test_unrelated_axis_is_ignored(self):
        self.assertEqual(self.r('e', 50, 999), (100, 200, 950, 900))
        self.assertEqual(self.r('n', 999, -10), (100, 190, 900, 900))

    @unittest.skipIf(ui is None, 'ui.py not importable headless')
    def test_min_size_clamps_keep_the_opposite_edge(self):
        big = 5000
        self.assertEqual(self.r('w', big, 0), (900 - 640, 200, 900, 900))
        self.assertEqual(self.r('e', -big, 0), (100, 200, 100 + 640, 900))
        self.assertEqual(self.r('n', 0, big), (100, 900 - 600, 900, 900))
        self.assertEqual(self.r('s', 0, -big), (100, 200, 900, 200 + 600))
        self.assertEqual(self.r('nw', big, big),
                         (900 - 640, 900 - 600, 900, 900))
        self.assertEqual(self.r('se', -big, -big),
                         (100, 200, 100 + 640, 200 + 600))
        self.assertEqual(self.r('ne', -big, big),
                         (100, 900 - 600, 100 + 640, 900))
        self.assertEqual(self.r('sw', big, -big),
                         (900 - 640, 200, 900, 200 + 600))

    @unittest.skipIf(ui is None, 'ui.py not importable headless')
    def test_growing_is_unbounded(self):
        # Spanning monitors like a normal window: no max clamp.
        self.assertEqual(self.r('se', 3000, 2000), (100, 200, 3900, 2900))
        self.assertEqual(self.r('nw', -3000, -2000), (-2900, -1800, 900, 900))


def _make_api(win=None, settings=None):
    api = object.__new__(ui.BridgeApi)
    api._lock = threading.Lock()
    api._resize_lock = threading.Lock()
    api._resize_thread = None
    api._settings = dict(settings or {})
    api._tearing_down = False
    api._restore_rect = None
    api._normal_rect = None
    api._startup_rect = None
    api._startup_maximize = False
    api._hwnd_cache = HWND
    api._window = None
    api._win32 = win or FakeWin()
    return api


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class StartWindowResizeTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ui, 'save_settings')
        self.save = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ui, 'RESIZE_POLL_S', 0.001)
        p.start()
        self.addCleanup(p.stop)

    def _join(self, api):
        t = api._resize_thread
        if t is not None:
            t.join(2.0)
            self.assertFalse(t.is_alive())

    def test_edge_validation(self):
        api = _make_api()
        for bad in ('', 'N', 'north', 'nes', 'en', ' n', None, 5, ['n'],
                    {'edge': 'n'}):
            with self.subTest(edge=bad):
                self.assertEqual(api.start_window_resize(bad),
                                 {'ok': False, 'reason': 'bad_edge'})
        self.assertIsNone(api._resize_thread)
        for edge in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
            with self.subTest(edge=edge):
                self.assertEqual(api.start_window_resize(edge), {'ok': True})
                self._join(api)

    def test_drag_follows_cursor_until_release_and_persists(self):
        win = FakeWin(rect=(100, 100, 820, 820))
        api = _make_api(win)
        # The first position is the start cursor start_window_resize reads
        # synchronously; the loop consumes the rest, and the button goes
        # up with the last one.
        win.feed = [(820, 820), (830, 825), (900, 870), (950, 900)]
        self.assertEqual(api.start_window_resize('se'), {'ok': True})
        self._join(api)
        self.assertEqual(win.rect, (100, 100, 950, 900))
        self.assertEqual(api._normal_rect, (100, 100, 850, 800))
        self.assertEqual(api._settings['window_rect'],
                         {'x': 100, 'y': 100, 'width': 850, 'height': 800})
        self.assertIs(api._settings['window_maximized'], False)
        self.assertEqual(self.save.call_count, 1)

    def test_release_before_first_poll_changes_nothing(self):
        win = FakeWin()
        api = _make_api(win)
        api.start_window_resize('w')     # button already up, cursor still
        self._join(api)
        self.assertEqual(win.set_calls, [])
        self.assertEqual(win.rect, (100, 100, 820, 820))

    def test_unchanged_rect_is_not_reapplied(self):
        win = FakeWin()
        api = _make_api(win)
        win.feed = [(400, 400), (410, 400), (410, 400), (410, 420),
                    (420, 400)]
        api.start_window_resize('e')
        self._join(api)
        self.assertEqual(win.set_calls,
                         [(100, 100, 830, 820), (100, 100, 840, 820)])

    def test_min_size_uses_window_dpi(self):
        win = FakeWin(rect=(0, 0, 1200, 1000), scale=1.5)
        api = _make_api(win)
        win.feed = [(0, 0), (5000, 5000)]
        api.start_window_resize('nw')
        self._join(api)
        self.assertEqual(win.rect, (1200 - 960, 1000 - 900, 1200, 1000))
        # Persisted in logical px (physical / 1.5).
        self.assertEqual(api._settings['window_rect'],
                         {'x': 160, 'y': 67, 'width': 640, 'height': 600})

    def test_teardown_ends_the_loop_without_saving(self):
        win = FakeWin()
        win.hold = True                              # button never released
        api = _make_api(win)
        self.assertEqual(api.start_window_resize('s'), {'ok': True})
        t = api._resize_thread
        time.sleep(0.02)
        self.assertTrue(t.is_alive())
        api._tearing_down = True
        t.join(1.0)
        self.assertFalse(t.is_alive())
        self.save.assert_not_called()

    def test_maximize_mid_drag_ends_the_loop_without_saving(self):
        win = FakeWin()
        win.hold = True
        api = _make_api(win)
        api.start_window_resize('e')
        t = api._resize_thread
        time.sleep(0.02)
        api._restore_rect = (100, 100, 720, 720)     # toggle_maximize ran
        t.join(1.0)
        self.assertFalse(t.is_alive())
        self.save.assert_not_called()

    def test_loop_is_capped(self):
        win = FakeWin()
        win.hold = True
        api = _make_api(win)
        with mock.patch.object(ui, 'RESIZE_MAX_S', 0.05):
            api.start_window_resize('e')
            t = api._resize_thread
            t.join(1.0)
        self.assertFalse(t.is_alive())

    def test_one_resize_at_a_time(self):
        win = FakeWin()
        win.hold = True
        api = _make_api(win)
        self.assertEqual(api.start_window_resize('e'), {'ok': True})
        first = api._resize_thread
        self.assertEqual(api.start_window_resize('w'),
                         {'ok': False, 'reason': 'busy'})
        self.assertIs(api._resize_thread, first)
        win.hold = False
        self._join(api)
        self.assertEqual(api.start_window_resize('w'), {'ok': True})
        self._join(api)

    def test_refused_while_maximized_minimized_or_closing(self):
        win = FakeWin()
        api = _make_api(win)
        api._restore_rect = (1, 2, 700, 700)
        self.assertEqual(api.start_window_resize('se'),
                         {'ok': False, 'reason': 'maximized'})
        api._restore_rect = None
        win.zoomed = True
        self.assertEqual(api.start_window_resize('se'),
                         {'ok': False, 'reason': 'maximized'})
        win.zoomed, win.iconic = False, True
        self.assertEqual(api.start_window_resize('se'),
                         {'ok': False, 'reason': 'maximized'})
        win.iconic = False
        api._tearing_down = True
        self.assertEqual(api.start_window_resize('se'),
                         {'ok': False, 'reason': 'closing'})
        api._tearing_down = False
        api._hwnd_cache = 0
        with mock.patch.object(ui.BridgeApi, '_ui_hwnd', return_value=0):
            self.assertEqual(api.start_window_resize('se'),
                             {'ok': False, 'reason': 'no_window'})
        self.assertIsNone(api._resize_thread)
        self.assertEqual(win.set_calls, [])

    def test_win32_failure_is_reported(self):
        win = FakeWin()
        win.cursor_pos = lambda: None
        api = _make_api(win)
        self.assertEqual(api.start_window_resize('se'),
                         {'ok': False, 'reason': 'win32_failed'})


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class MaximizeAndRestoreTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ui, 'save_settings')
        self.save = p.start()
        self.addCleanup(p.stop)

    def test_toggle_maximize_snaps_and_persists(self):
        win = FakeWin(rect=(100, 100, 820, 820), work=(0, 0, 1920, 1040))
        api = _make_api(win)
        self.assertEqual(api.toggle_maximize(), {'maximized': True})
        self.assertEqual(win.rect, (0, 0, 1920, 1040))
        self.assertEqual(api._restore_rect, (100, 100, 720, 720))
        self.assertIs(api._settings['window_maximized'], True)
        self.assertEqual(api._settings['window_rect'],
                         {'x': 100, 'y': 100, 'width': 720, 'height': 720})
        self.assertEqual(api.toggle_maximize(), {'maximized': False})
        self.assertEqual(win.rect, (100, 100, 820, 820))
        self.assertIsNone(api._restore_rect)
        self.assertIs(api._settings['window_maximized'], False)
        self.assertEqual(api._settings['window_rect'],
                         {'x': 100, 'y': 100, 'width': 720, 'height': 720})
        self.assertEqual(self.save.call_count, 2)

    def test_failed_snap_stays_restored_and_saves_nothing(self):
        win = FakeWin()
        win.fail_set = True
        api = _make_api(win)
        self.assertEqual(api.toggle_maximize(), {'maximized': False})
        self.assertIsNone(api._restore_rect)
        self.save.assert_not_called()

    def test_startup_maximize_is_one_shot_and_waits_for_hwnd(self):
        win = FakeWin()
        api = _make_api(win)
        api._startup_maximize = True
        api._hwnd_cache = 0
        with mock.patch.object(ui.BridgeApi, '_ui_hwnd', return_value=0):
            self.assertFalse(api._apply_startup_maximize())
        self.assertTrue(api._startup_maximize)          # still pending
        api._hwnd_cache = HWND
        self.assertTrue(api._apply_startup_maximize())
        self.assertFalse(api._startup_maximize)
        self.assertEqual(win.rect, win.work)
        self.assertFalse(api._apply_startup_maximize())  # consumed
        self.assertEqual(api.toggle_maximize(), {'maximized': False})

    def test_startup_maximize_reads_maximized_throughout(self):
        """The page / drain never see a one-tick "restored" while the
        startup re-maximize is in flight, and a second caller (`shown` +
        `loaded`) doesn't toggle twice."""
        api = _make_api(FakeWin())
        api._startup_maximize = True
        seen = []

        def fake_toggle():
            seen.append(api._is_maximized())
            seen.append(api._apply_startup_maximize())     # concurrent call
            api._restore_rect = (1, 2, 700, 700)
            return {'maximized': True}
        api.toggle_maximize = fake_toggle
        self.assertTrue(api._apply_startup_maximize())
        self.assertEqual(seen, [True, False])
        self.assertFalse(api._startup_maximize)
        self.assertTrue(api._is_maximized())

    def test_startup_geometry_snaps_to_exact_rect_once(self):
        win = FakeWin(rect=(300, 200, 1099, 851))     # a pixel off
        api = _make_api(win)
        api._startup_rect = (300, 200, 1100, 850)
        self.assertTrue(api._apply_startup_geometry())
        self.assertEqual(win.rect, (300, 200, 1100, 850))
        self.assertEqual(api._normal_rect, (300, 200, 800, 650))
        self.assertIsNone(api._startup_rect)
        self.assertFalse(api._apply_startup_geometry())
        self.assertEqual(len(win.set_calls), 1)

    def test_startup_window_restores_rect_then_maximizes(self):
        win = FakeWin(rect=(300, 200, 1100, 850))
        api = _make_api(win)
        api._startup_rect = (300, 200, 1100, 850)
        api._startup_maximize = True
        ui._apply_startup_window(api)
        self.assertEqual(win.rect, win.work)
        self.assertEqual(api._restore_rect, (300, 200, 800, 650))
        self.assertEqual(win.set_calls, [win.work])     # rect already exact


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class CloseTimePersistenceTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ui, 'save_settings')
        self.save = p.start()
        self.addCleanup(p.stop)

    def test_saves_live_rect_when_restored(self):
        win = FakeWin(rect=(50, 60, 850, 760))
        api = _make_api(win)
        api._persist_window_geometry_on_close()
        self.assertEqual(api._settings['window_rect'],
                         {'x': 50, 'y': 60, 'width': 800, 'height': 700})
        self.assertIs(api._settings['window_maximized'], False)

    def test_minimized_keeps_last_tracked_rect(self):
        win = FakeWin(rect=(50, 60, 850, 760))
        api = _make_api(win)
        api._track_window_rect()                        # drain tick
        win.iconic = True
        win.rect = (-32000, -32000, -31840, -31972)     # minimized rect
        api._persist_window_geometry_on_close()
        self.assertEqual(api._settings['window_rect'],
                         {'x': 50, 'y': 60, 'width': 800, 'height': 700})

    def test_maximized_saves_restore_rect_and_flag(self):
        win = FakeWin(rect=(0, 0, 1920, 1040))
        api = _make_api(win)
        api._restore_rect = (10, 20, 700, 650)
        api._persist_window_geometry_on_close()
        self.assertEqual(api._settings['window_rect'],
                         {'x': 10, 'y': 20, 'width': 700, 'height': 650})
        self.assertIs(api._settings['window_maximized'], True)

    def test_natively_zoomed_counts_as_maximized(self):
        win = FakeWin(rect=(5, 5, 805, 705))
        api = _make_api(win)
        api._track_window_rect()
        win.zoomed = True
        win.rect = (-8, -8, 1928, 1088)
        api._persist_window_geometry_on_close()
        self.assertIs(api._settings['window_maximized'], True)
        self.assertEqual(api._settings['window_rect']['width'], 800)

    def test_no_hwnd_or_unchanged_saves_nothing(self):
        api = _make_api(FakeWin())
        api._hwnd_cache = 0
        api._persist_window_geometry_on_close()
        self.save.assert_not_called()
        api._hwnd_cache = HWND
        api._persist_window_geometry_on_close()
        self.assertEqual(self.save.call_count, 1)
        api._persist_window_geometry_on_close()          # nothing changed
        self.assertEqual(self.save.call_count, 1)

    def test_win32_errors_never_escape(self):
        win = FakeWin()
        win.window_rect = mock.Mock(side_effect=OSError('gone'))
        win.is_zoomed = mock.Mock(side_effect=OSError('gone'))
        api = _make_api(win)
        api._persist_window_geometry_on_close()          # no raise
        api._track_window_rect()

    def test_detach_persists_before_hiding(self):
        api = _make_api(FakeWin())
        order = []
        api._persist_window_geometry_on_close = lambda: order.append('save')
        api._hide_window_for_teardown = lambda: order.append('hide')
        api._detached = False
        api._drain_stop = threading.Event()
        api._drain_thread = None
        api._push_in_flight = False
        api._orig_stdout, api._orig_stderr = sys.stdout, sys.stderr
        api._bot = mock.Mock()
        api._macro = mock.Mock()
        api._keybinds = mock.Mock()
        api._engine_lock = threading.RLock()
        api._arduino_lock = threading.Lock()
        api._arduino = None
        with mock.patch.object(ui, '_release_ll_hooks'), \
                mock.patch.object(ui.keyboard, 'unhook_all'), \
                mock.patch.object(ui.mouse, 'unhook_all'):
            api.detach()
        self.assertEqual(order, ['save', 'hide'])


class SettingsSanitationTests(unittest.TestCase):
    def _load(self, data):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'ui_settings.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            with mock.patch.object(ui_core, 'settings_path',
                                   return_value=path):
                return ui_core.load_settings()

    def test_defaults_without_file(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ui_core, 'settings_path',
                                   return_value=Path(td) / 'none.json'):
                s = ui_core.load_settings()
        self.assertEqual(s['theme'], 'score')
        self.assertIsNone(s['window_rect'])
        self.assertIs(s['window_maximized'], False)

    def test_theme(self):
        for raw, want in (('light', 'light'), ('dark', 'dark'),
                          ('system', 'system'), ('score', 'score'),
                          (' Dark ', 'dark'), ('neon', 'score'),
                          (None, 'score'), (3, 'score'), ([], 'score')):
            with self.subTest(raw=raw):
                self.assertEqual(self._load({'theme': raw})['theme'], want)

    def test_window_maximized_is_strict_bool(self):
        for raw, want in ((True, True), (False, False), ('true', False),
                          (1, False), (None, False)):
            with self.subTest(raw=raw):
                self.assertIs(
                    self._load({'window_maximized': raw})['window_maximized'],
                    want)

    def test_window_rect(self):
        ok = {'x': -1500, 'y': 40, 'width': 900, 'height': 700}
        self.assertEqual(self._load({'window_rect': ok})['window_rect'], ok)
        cases = [
            ({'x': 1, 'y': 2, 'width': 700.4, 'height': 650.6},
             {'x': 1, 'y': 2, 'width': 700, 'height': 651}),
            ({'x': 1, 'y': 2, 'width': 100, 'height': 599},     # raised
             {'x': 1, 'y': 2, 'width': 640, 'height': 600}),
            ({'x': 1, 'y': 2, 'width': 10001, 'height': 700}, None),
            ({'x': 200000, 'y': 2, 'width': 800, 'height': 700}, None),
            ({'x': True, 'y': 2, 'width': 800, 'height': 700}, None),
            ({'x': '1', 'y': 2, 'width': 800, 'height': 700}, None),
            ({'x': 1, 'y': 2, 'width': float('nan'), 'height': 700}, None),
            ({'x': 1, 'y': 2, 'width': float('inf'), 'height': 700}, None),
            ({'x': 1, 'y': 2, 'width': 800}, None),
            ([1, 2, 800, 700], None),
            ('garbage', None),
            (None, None),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ui_core.normalize_window_rect(raw), want)
        self.assertIsNone(
            self._load({'window_rect': {'x': 'a'}})['window_rect'])

    def test_round_trip_through_save(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'ui_settings.json'
            with mock.patch.object(ui_core, 'settings_path',
                                   return_value=path):
                s = ui_core.load_settings()
                s.update(theme='light', window_maximized=True,
                         window_rect={'x': 5, 'y': 6, 'width': 800,
                                      'height': 700})
                ui_core.save_settings(s)
                again = ui_core.load_settings()
        self.assertEqual(again['theme'], 'light')
        self.assertIs(again['window_maximized'], True)
        self.assertEqual(again['window_rect'],
                         {'x': 5, 'y': 6, 'width': 800, 'height': 700})


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class InitialGeometryTests(unittest.TestCase):
    DEFAULT = ({'width': 720, 'height': 720, 'x': None, 'y': None}, None)

    def geom(self, rect, **kw):
        return ui.initial_window_geometry({'window_rect': rect},
                                          win=FakeWin(**kw))

    def test_no_or_bad_rect_is_default_centered(self):
        self.assertEqual(ui.initial_window_geometry({}, win=FakeWin()),
                         self.DEFAULT)
        self.assertEqual(self.geom('junk'), self.DEFAULT)

    def test_on_screen_rect_is_frame_compensated(self):
        kwargs, target = self.geom(
            {'x': 300, 'y': 200, 'width': 800, 'height': 650})
        # pywebview sizes the form with a sizable frame, then drops it and
        # keeps the client size: ask for the frame on top (16 x 39 here).
        self.assertEqual(kwargs, {'width': 816, 'height': 689,
                                  'x': 300, 'y': 200})
        self.assertEqual(target, (300, 200, 1100, 850))

    def test_scaled_display(self):
        kwargs, target = self.geom(
            {'x': 100, 'y': 100, 'width': 800, 'height': 700},
            scale=1.5, work=(0, 0, 2880, 1560), frame=(24, 57))
        self.assertEqual(target, (150, 150, 1350, 1200))
        self.assertEqual(kwargs, {'width': 816, 'height': 738,
                                  'x': 100, 'y': 100})

    def test_off_screen_falls_back(self):
        for rect in ({'x': 5000, 'y': 100, 'width': 800, 'height': 700},
                     {'x': -900, 'y': 100, 'width': 800, 'height': 700},
                     {'x': 100, 'y': -500, 'width': 800, 'height': 700},
                     {'x': 100, 'y': 1030, 'width': 800, 'height': 700},
                     {'x': 1850, 'y': 100, 'width': 800, 'height': 700}):
            with self.subTest(rect=rect):
                self.assertEqual(self.geom(rect), self.DEFAULT)

    def test_partly_off_screen_with_reachable_titlebar_is_kept(self):
        kwargs, target = self.geom(
            {'x': 1700, 'y': 600, 'width': 800, 'height': 700})
        self.assertEqual(target, (1700, 600, 2500, 1300))

    def test_second_monitor_and_vanished_monitor(self):
        left = (-1920, 0, 0, 1040)
        rect = {'x': -1500, 'y': 100, 'width': 800, 'height': 700}
        _kw, target = self.geom(rect, areas=[(0, 0, 1920, 1040), left])
        self.assertEqual(target, (-1500, 100, -700, 800))
        self.assertEqual(self.geom(rect, areas=[(0, 0, 1920, 1040)]),
                         self.DEFAULT)

    def test_oversized_rect_is_fit_to_its_work_area(self):
        kwargs, target = self.geom(
            {'x': 100, 'y': 50, 'width': 3000, 'height': 2000})
        self.assertEqual(target, (0, 0, 1920, 1040))
        self.assertEqual(kwargs, {'width': 1936, 'height': 1079,
                                  'x': 0, 'y': 0})

    def test_win32_failure_falls_back(self):
        win = FakeWin()
        win.work_areas = mock.Mock(side_effect=OSError('x'))
        self.assertEqual(
            ui.initial_window_geometry(
                {'window_rect': {'x': 1, 'y': 1, 'width': 800,
                                 'height': 700}}, win=win),
            self.DEFAULT)
        self.assertEqual(self.geom({'x': 1, 'y': 1, 'width': 800,
                                    'height': 700}, areas=[]),
                         self.DEFAULT)


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class ThemeAndInitialStateTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ui, 'save_settings')
        self.save = p.start()
        self.addCleanup(p.stop)

    def _api(self, **settings):
        api = _make_api(FakeWin(), settings={
            'keybinds': {}, 'theme': 'score', **settings})
        api._opts = {**ui_core.default_bot_options(), 'debug': False}
        api._status_lock = threading.Lock()
        api._status = {'last_error': ''}
        api._macro = mock.Mock()
        api._macro.snapshot.return_value = {}
        return api

    def test_set_theme_persists_atomically(self):
        api = self._api()
        self.assertEqual(api.set_theme('light'),
                         {'ok': True, 'theme': 'light'})
        self.assertEqual(api._settings['theme'], 'light')
        self.assertEqual(self.save.call_count, 1)
        self.assertEqual(api.set_theme('light'),
                         {'ok': True, 'theme': 'light'})
        self.assertEqual(self.save.call_count, 1)        # unchanged
        for name in ('dark', 'system', 'score'):
            self.assertTrue(api.set_theme(name)['ok'])
        self.assertEqual(self.save.call_count, 4)

    def test_set_theme_refuses_unknown(self):
        api = self._api()
        for bad in ('neon', 'Light', '', None, 1, ['dark']):
            with self.subTest(theme=bad):
                self.assertEqual(api.set_theme(bad),
                                 {'ok': False, 'reason': 'bad_theme'})
        self.assertEqual(api._settings['theme'], 'score')
        self.save.assert_not_called()

    def test_set_theme_writes_the_real_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'ui_settings.json'
            with mock.patch.object(ui_core, 'settings_path',
                                   return_value=path), \
                    mock.patch.object(ui, 'save_settings',
                                      ui_core.save_settings):
                api = self._api(**ui_core.load_settings())
                api.set_theme('dark')
                self.assertEqual(ui_core.load_settings()['theme'], 'dark')
                self.assertEqual(sorted(p.name for p in Path(td).iterdir()),
                                 ['ui_settings.json'])

    def test_native_background_needs_a_form(self):
        api = self._api()
        self.assertFalse(api._apply_native_background('#0a0a0a'))
        api._window = object()                       # no .native
        self.assertFalse(api._apply_native_background('#0a0a0a'))

    def test_theme_backgrounds(self):
        self.assertEqual(set(ui.THEME_BACKGROUNDS), set(ui_core.UI_THEMES))
        self.assertEqual(ui.theme_background('score'), '#171a21')
        self.assertEqual(ui.theme_background('bogus'),
                         ui.theme_background('score'))
        for color in ui.THEME_BACKGROUNDS.values():
            self.assertRegex(color, r'^#[0-9a-f]{6}$')

    def test_system_theme_follows_windows_app_mode(self):
        """Like the page: prefers-color-scheme dark → Dark, else Light;
        Score navy only when the mode can't be read."""
        for light, want in ((True, ui.THEME_BACKGROUNDS['light']),
                            (False, ui.THEME_BACKGROUNDS['dark']),
                            (None, ui.THEME_BACKGROUNDS['score'])):
            with self.subTest(light=light), mock.patch.object(
                    ui, '_apps_use_light_theme', return_value=light):
                self.assertEqual(ui.theme_background('system'), want)
                self.assertEqual(ui.theme_background('score'), '#171a21')

    def test_theme_backgrounds_match_style_css(self):
        """THEME_BACKGROUNDS mirrors each theme's --bg in style.css."""
        css_path = (Path(__file__).resolve().parents[1] / 'pc_client'
                    / 'web' / 'style.css')
        css = css_path.read_text(encoding='utf-8')
        import re

        def bg(selector):
            i = css.find(selector + ' {')
            self.assertGreaterEqual(i, 0, selector)
            m = re.search(r'--bg:\s*(#[0-9a-fA-F]{6})', css[i:])
            return m.group(1).lower()
        self.assertEqual(ui.THEME_BACKGROUNDS['score'], bg(':root'))
        self.assertEqual(ui.THEME_BACKGROUNDS['light'],
                         bg('[data-theme="light"]'))
        self.assertEqual(ui.THEME_BACKGROUNDS['dark'],
                         bg('[data-theme="dark"]'))

    def test_initial_state_fields_without_side_effects(self):
        api = self._api(theme='dark')
        before = json.dumps(api._settings, sort_keys=True)
        s = api.get_initial_state()
        self.assertEqual(s['theme'], 'dark')
        self.assertIs(s['maximized'], False)
        api._restore_rect = (1, 2, 700, 700)
        self.assertIs(api.get_initial_state()['maximized'], True)
        api._restore_rect = None
        api._startup_maximize = True                 # about to maximize
        self.assertIs(api.get_initial_state()['maximized'], True)
        self.assertTrue(api._startup_maximize)       # not consumed
        api._settings['theme'] = 'weird'
        self.assertEqual(api.get_initial_state()['theme'], 'score')
        api._settings['theme'] = 'dark'
        self.assertEqual(json.dumps(api._settings, sort_keys=True), before)
        self.save.assert_not_called()
        self.assertEqual(api._win32.set_calls, [])
        self.assertIsNone(api._resize_thread)

    def test_initial_state_tolerates_hand_built_instances(self):
        """Older hand-built BridgeApi instances (other suites) lack the
        new attributes."""
        api = self._api()
        for name in ('_restore_rect', '_startup_maximize'):
            delattr(api, name)
        self.assertIs(api.get_initial_state()['maximized'], False)


@unittest.skipIf(ui is None, 'ui.py not importable headless')
class DrainTrackingTests(unittest.TestCase):
    def test_drain_status_carries_maximized_and_tracks_rect(self):
        import queue
        win = FakeWin(rect=(10, 20, 810, 720))
        api = _make_api(win)
        api._status_lock = threading.Lock()
        api._status = {'last_error': ''}
        api._opts = {'debug': False}
        api._bot = mock.Mock()
        api._bot.is_running.return_value = False
        api._arduino = None
        api._window = object()
        api._stopping = False
        api._drain_stop = threading.Event()
        api._log_queue = queue.Queue()
        api._orig_stderr = sys.__stderr__
        pushes = []
        api._push = lambda status, log: pushes.append(dict(status)) or True
        with mock.patch.object(ui, 'is_admin', return_value=True), \
                mock.patch.object(ui, '_find_game_window', return_value=None):
            t = threading.Thread(target=api._drain_loop, daemon=True)
            t.start()
            try:
                self.assertTrue(_wait_until(lambda: len(pushes) >= 1, 1.0))
                self.assertIs(pushes[0]['maximized'], False)
                self.assertEqual(api._normal_rect, (10, 20, 800, 700))
                api._restore_rect = (10, 20, 800, 700)
                self.assertTrue(_wait_until(
                    lambda: pushes[-1].get('maximized') is True, 1.0))
            finally:
                api._drain_stop.set()
                t.join(1.0)

    def test_track_only_while_restored(self):
        win = FakeWin(rect=(10, 20, 810, 720))
        api = _make_api(win)
        api._track_window_rect()
        self.assertEqual(api._normal_rect, (10, 20, 800, 700))
        win.rect = (0, 0, 1920, 1040)
        for attr in ('iconic', 'zoomed'):
            setattr(win, attr, True)
            api._track_window_rect()
            setattr(win, attr, False)
        api._restore_rect = (10, 20, 800, 700)
        api._track_window_rect()
        self.assertEqual(api._normal_rect, (10, 20, 800, 700))
        api._restore_rect = None
        api._hwnd_cache = 0
        api._track_window_rect()
        self.assertEqual(api._normal_rect, (10, 20, 800, 700))


if __name__ == '__main__':
    unittest.main()
