"""Regression tests for the rhythm / album engine UX fixes.

Headless: Win32 (GDI, foreground window, window geometry), mss, cv2
windows and the input backend are all replaced with small fakes, so the
suite runs without the game, a display, or an Arduino.

Finding IDs in the class docstrings refer to the UX review:
  R1  detector keeps typing into other apps when Genshin loses focus
  R6  mid-song hand-off trusted blindly (waits forever on album page)
  R7  EPP disabled before setup that can fail → never restored
  R8  pause ignored during the album intro click chain
  R9  quick Pause→Resume ends a Standalone run
  R10 window move / resize leaves capture geometry stale
  R12 click issued even though the cursor never reached the target
  R21 failed album runs reported as "Album complete"
  R22 single- vs multi-difficulty runs leave the wheel in different places
  U23 FPS readout frozen after the debug viz closes

Run:  python -m unittest discover -s tests -p "test_*.py"
"""
import contextlib
import io
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc_client'))

import album                                            # noqa: E402
import detector                                         # noqa: E402
import game_window                                      # noqa: E402
import screen_watcher                                   # noqa: E402
import song_player                                      # noqa: E402
import standalone_runner                                # noqa: E402
from config import (ALBUM_BEGIN_PERFORMANCE_XY,         # noqa: E402
                    ALBUM_DIFFICULTY_COORDS, ALBUM_GO_PERFORM_XY,
                    ALBUM_SONG_COUNT, KEYS, REF_HIT_LINE_Y)


REGION_1080 = {'top': 0, 'left': 0, 'width': 1920, 'height': 1080}
GAME_HWND = 4242


def _quiet():
    return contextlib.redirect_stdout(io.StringIO())


def _wait_until(pred, timeout=2.0, step=0.005):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class Recorder:
    """status_cb stand-in."""

    def __init__(self):
        self.updates = []
        self._lock = threading.Lock()

    def __call__(self, update):
        with self._lock:
            self.updates.append(dict(update))

    def values(self, key):
        with self._lock:
            return [u[key] for u in self.updates if key in u]


class FakeInput:
    """InputBackend stand-in that records every call."""

    def __init__(self, converge=True):
        self.converge = converge
        self.calls = []
        self._lock = threading.Lock()
        self.closed = False

    def _rec(self, *call):
        with self._lock:
            self.calls.append(call)

    def key_down(self, key):
        self._rec('key_down', key)

    def key_up(self, key):
        self._rec('key_up', key)

    def mouse_down(self, button):
        self._rec('mouse_down', button)

    def mouse_up(self, button):
        self._rec('mouse_up', button)

    def mouse_move(self, dx, dy):
        self._rec('mouse_move', dx, dy)

    def move_to(self, x, y, stop_evt=None, max_iters=15, tol=2):
        self._rec('move_to', x, y)
        if stop_evt is not None and stop_evt.is_set():
            return False
        return self.converge

    def close(self):
        self.closed = True

    def snapshot(self):
        with self._lock:
            return list(self.calls)


# --- R1: detector foreground gate --------------------------------------------

class DetectorForegroundGate(unittest.TestCase):
    """R1 — no keystrokes while another app has focus; held keys are
    released; the per-key hot path never calls Win32 focus APIs."""

    def setUp(self):
        self.fg = GAME_HWND
        self.fg_calls = 0
        self.pids = {GAME_HWND: 100, 999: 100, 777: 200}

        user32 = mock.MagicMock()
        user32.GetDC.return_value = 1
        user32.ReleaseDC.return_value = 1
        gdi32 = mock.MagicMock()
        gdi32.GetPixel.return_value = 0          # blue 0 → note present

        def fg():
            self.fg_calls += 1
            return self.fg

        patches = [
            mock.patch.object(detector, '_user32', user32),
            mock.patch.object(detector, '_gdi32', gdi32),
            mock.patch.object(detector, '_foreground_hwnd', side_effect=fg),
            mock.patch.object(detector, '_window_pid',
                              side_effect=lambda h: self.pids.get(h, 0)),
            mock.patch.object(detector, 'FOCUS_POLL_S', 0.02),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctrl = FakeInput()
        self.det = detector.NoteDetector(GAME_HWND, [10] * len(KEYS), 50,
                                         self.ctrl)
        quiet = _quiet()
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.addCleanup(self.det.stop)

    def _downs(self):
        return [c for c in self.ctrl.snapshot() if c[0] == 'key_down']

    def test_focus_loss_releases_held_keys_and_blocks_new_presses(self):
        self.det.start()
        self.assertTrue(_wait_until(
            lambda: all(self.det.pressed[k] for k in KEYS)))

        self.fg = 777                      # another app takes focus
        self.assertTrue(_wait_until(
            lambda: not any(self.det.pressed[k] for k in KEYS)))
        ups = {c[1] for c in self.ctrl.snapshot() if c[0] == 'key_up'}
        self.assertEqual(ups, set(KEYS))

        downs_while_unfocused = len(self._downs())
        time.sleep(0.15)                   # pixels still dark the whole time
        self.assertEqual(len(self._downs()), downs_while_unfocused)

        self.fg = GAME_HWND                # back in the game
        self.assertTrue(_wait_until(
            lambda: all(self.det.pressed[k] for k in KEYS)))

    def test_starting_unfocused_sends_nothing(self):
        self.fg = 777
        self.det.start()
        time.sleep(0.15)
        self.assertEqual(self._downs(), [])

    def test_game_process_overlay_counts_as_focused(self):
        self.fg = 999                      # different HWND, same game PID
        self.det.start()
        self.assertTrue(_wait_until(
            lambda: all(self.det.pressed[k] for k in KEYS)))

    def test_hot_path_does_not_poll_foreground_window(self):
        self.det.start()
        time.sleep(0.3)
        # One focus thread @20 ms ≈ 15 calls. Six 5 ms key loops polling
        # it themselves would be hundreds.
        self.assertLess(self.fg_calls, 40)


# --- R12 / R10: ScreenWatcher -------------------------------------------------

class DetectorActiveLanes(unittest.TestCase):
    """Direction A lane strip — `detector.active_lanes()`."""

    def setUp(self):
        user32 = mock.MagicMock()
        user32.GetDC.return_value = 1
        gdi32 = mock.MagicMock()
        gdi32.GetPixel.return_value = 0xFF0000   # blue 255 → idle lane
        patches = [
            mock.patch.object(detector, '_user32', user32),
            mock.patch.object(detector, '_gdi32', gdi32),
            mock.patch.object(detector, '_foreground_hwnd',
                              return_value=GAME_HWND),
            mock.patch.object(detector, '_window_pid', return_value=100),
            mock.patch.object(detector, 'FOCUS_POLL_S', 0.02),
            mock.patch.object(detector, '_active_detector', None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        quiet = _quiet()
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def _det(self):
        d = detector.NoteDetector(GAME_HWND, [10] * len(KEYS), 50,
                                  FakeInput())
        self.addCleanup(d.stop)
        return d

    def test_all_false_when_no_detector_runs(self):
        self.assertEqual(detector.active_lanes(), [False] * 6)

    def test_reflects_running_detector_then_clears_on_stop(self):
        d = self._det()
        d.start()
        d.pressed['s'] = True
        d.pressed['k'] = True
        self.assertEqual(detector.active_lanes(),
                         [False, True, False, False, True, False])
        d.stop()
        self.assertEqual(detector.active_lanes(), [False] * 6)

    def test_newest_detector_wins_when_recycled(self):
        old, new = self._det(), self._det()
        old.start()
        old.pressed['a'] = True
        new.start()                       # e.g. resume / geometry rebuild
        new.pressed['l'] = True
        self.assertEqual(detector.active_lanes(),
                         [False, False, False, False, False, True])
        old.stop()                        # late teardown of the old one
        self.assertEqual(detector.active_lanes()[5], True)
        new.stop()
        self.assertEqual(detector.active_lanes(), [False] * 6)


class ScreenWatcherClicks(unittest.TestCase):

    def setUp(self):
        for name in ('CLICK_HOVER_S', 'CLICK_HOLD_S', 'CLICK_SETTLE_S'):
            p = mock.patch.object(screen_watcher, name, 0)
            p.start()
            self.addCleanup(p.stop)
        self.stop = threading.Event()

    def _watcher(self, ctrl):
        return screen_watcher.ScreenWatcher(
            REGION_1080, 1.0, 1.0, ctrl, self.stop, templates={})

    def test_no_click_when_move_to_does_not_converge(self):
        """R12"""
        ctrl = FakeInput(converge=False)
        with _quiet():
            ok = self._watcher(ctrl).click_ref(100, 100)
        self.assertFalse(ok)
        self.assertNotIn('mouse_down', [c[0] for c in ctrl.snapshot()])

    def test_click_issued_when_converged(self):
        ctrl = FakeInput(converge=True)
        with _quiet():
            ok = self._watcher(ctrl).click_ref(100, 100)
        self.assertTrue(ok)
        kinds = [c[0] for c in ctrl.snapshot()]
        self.assertEqual(kinds, ['move_to', 'mouse_down', 'mouse_up'])

    def test_stop_returns_false_without_clicking(self):
        self.stop.set()
        ctrl = FakeInput(converge=True)
        with _quiet():
            self.assertFalse(self._watcher(ctrl).click_ref(100, 100))
        self.assertNotIn('mouse_down', [c[0] for c in ctrl.snapshot()])

    def test_update_geometry_moves_clicks_and_rois(self):
        """R10"""
        ctrl = FakeInput()
        w = self._watcher(ctrl)
        w.update_geometry({'top': 100, 'left': 200, 'width': 1280,
                           'height': 720}, 2 / 3, 2 / 3)
        self.assertEqual(w.ref_to_screen(960, 540), (840, 460))
        self.assertEqual(w.ref_rect_to_local(300, 300, 30, 30),
                         (200, 200, 20, 20))
        with _quiet():
            w.click_local(10, 20)
        self.assertEqual(ctrl.snapshot()[0], ('move_to', 210, 120))

    def test_stale_generation_click_is_refused(self):
        """RR1 — coords matched on the old geometry must not be applied
        to the new window."""
        ctrl = FakeInput()
        w = self._watcher(ctrl)
        gen = w.generation
        w.update_geometry({'top': 0, 'left': 0, 'width': 1280,
                           'height': 720}, 2 / 3, 2 / 3)
        self.assertNotEqual(w.generation, gen)
        with _quiet():
            self.assertFalse(w.click_local(1390, 1010, generation=gen))
        self.assertEqual(ctrl.snapshot(), [])

    def test_geometry_change_during_cursor_travel_aborts_click(self):
        """RR1 — resized while move_to was in flight."""
        ctrl = FakeInput()
        w = self._watcher(ctrl)
        orig = ctrl.move_to

        def move_and_resize(*a, **k):
            w.update_geometry({'top': 0, 'left': 0, 'width': 1280,
                               'height': 720}, 2 / 3, 2 / 3)
            return orig(*a, **k)

        ctrl.move_to = move_and_resize
        with _quiet():
            self.assertFalse(w.click_local(100, 100,
                                           generation=w.generation))
        self.assertNotIn('mouse_down', [c[0] for c in ctrl.snapshot()])


# --- game_window: geometry + last_error ---------------------------------------

class FakeUser32:
    def __init__(self, exists=True, iconic=(), size=(1920, 1080),
                 origin=(0, 0), find=GAME_HWND):
        self.exists = exists
        self.iconic = list(iconic)   # successive IsIconic results
        self.size = size
        self.origin = origin
        self.find = find
        self.shown = []

    def FindWindowW(self, cls, title):
        return self.find

    def IsWindow(self, hwnd):
        return self.exists

    def IsIconic(self, hwnd):
        return self.iconic.pop(0) if self.iconic else False

    def ShowWindow(self, hwnd, cmd):
        self.shown.append(cmd)

    def GetClientRect(self, hwnd, ref):
        ref._obj.left, ref._obj.top = 0, 0
        ref._obj.right, ref._obj.bottom = self.size

    def GetWindowRect(self, hwnd, ref):
        self.GetClientRect(hwnd, ref)

    def ClientToScreen(self, hwnd, ref):
        ref._obj.x, ref._obj.y = self.origin


class GameWindowGeometry(unittest.TestCase):

    def _patch(self, fake):
        p = mock.patch.object(game_window, '_user32', return_value=fake)
        p.start()
        self.addCleanup(p.stop)

    def test_read_geometry_is_non_blocking(self):
        self._patch(FakeUser32(iconic=[True]))
        self.assertIsNone(game_window.read_game_geometry(GAME_HWND))
        self._patch(FakeUser32(size=(640, 480)))
        self.assertIsNone(game_window.read_game_geometry(GAME_HWND))
        self._patch(FakeUser32(size=(1280, 720), origin=(50, 60)))
        region, sx, sy = game_window.read_game_geometry(GAME_HWND)
        self.assertEqual(region, {'top': 60, 'left': 50, 'width': 1280,
                                  'height': 720})
        self.assertAlmostEqual(sx, 2 / 3)

    def test_minimized_wait_reports_then_clears_last_error(self):
        self._patch(FakeUser32(iconic=[True]))
        rec = Recorder()
        with _quiet():
            geom = game_window.get_game_geometry(
                GAME_HWND, stop_evt=threading.Event(), status_cb=rec)
        self.assertIsNotNone(geom)
        self.assertEqual(rec.values('last_error'),
                         [game_window.MSG_WINDOW_MINIMIZED, ''])

    def test_closed_window_returns_none_without_stop(self):
        self._patch(FakeUser32(exists=False))
        rec, stop = Recorder(), threading.Event()
        with _quiet():
            self.assertIsNone(game_window.get_game_geometry(
                GAME_HWND, stop_evt=stop, status_cb=rec))
        self.assertFalse(stop.is_set())
        self.assertEqual(rec.values('last_error'),
                         [game_window.MSG_WINDOW_CLOSED])

    def test_auto_detect_reports_missing_window(self):
        self._patch(FakeUser32(find=0))
        rec = Recorder()
        with _quiet():
            self.assertIsNone(game_window.auto_detect(status_cb=rec))
        self.assertEqual(rec.values('last_error'),
                         [game_window.MSG_WINDOW_NOT_FOUND])


# --- standalone runner --------------------------------------------------------

class FakeDetector:
    instances = []

    def __init__(self, hwnd, column_centers, hit_line_y, controller):
        self.hwnd = hwnd
        self.column_centers = column_centers
        self.hit_line_y = hit_line_y
        self.started = False
        self.stopped = False
        self.pressed = {k: False for k in KEYS}
        FakeDetector.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class StandaloneRunner(unittest.TestCase):

    def setUp(self):
        FakeDetector.instances = []
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.debug = threading.Event()
        self.rec = Recorder()
        moved = {'top': 40, 'left': 30, 'width': 1280, 'height': 720}
        patches = [
            mock.patch.object(standalone_runner, 'auto_detect',
                              return_value=(dict(REGION_1080),
                                            [417, 628, 844, 1061, 1277, 1493],
                                            921)),
            mock.patch.object(standalone_runner, 'find_game_window',
                              return_value=GAME_HWND),
            mock.patch.object(standalone_runner, 'get_game_geometry',
                              return_value=(moved, 2 / 3, 2 / 3)),
            mock.patch.object(standalone_runner, 'NoteDetector', FakeDetector),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, viz, controller=None):
        with mock.patch.object(standalone_runner, 'run_visualization',
                               side_effect=viz), _quiet():
            standalone_runner.run_standalone(
                self.stop, self.debug, status_cb=self.rec,
                pause_evt=self.pause,
                controller=controller if controller is not None
                else FakeInput())

    def test_quick_pause_resume_keeps_running(self):
        """R9 — pause already cleared by the time the detector is torn
        down must restart detection, not end the run."""
        calls = []

        def viz(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                self.pause.set()
                self.pause.clear()          # resumed during teardown
                return standalone_runner.VIZ_EXIT_PAUSE
            self.stop.set()
            return standalone_runner.VIZ_EXIT_STOP

        self._run(viz)
        self.assertEqual(len(FakeDetector.instances), 2)
        self.assertTrue(all(d.stopped for d in FakeDetector.instances))
        self.assertEqual(self.rec.values('state')[-1], 'idle')

    def test_resume_rebuilds_detector_for_new_geometry(self):
        """R10 — pause, window resized, resume → new lane layout."""
        calls = []

        def viz(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                self.pause.set()
                threading.Timer(0.05, self.pause.clear).start()
                return standalone_runner.VIZ_EXIT_PAUSE
            self.stop.set()
            return standalone_runner.VIZ_EXIT_STOP

        self._run(viz)
        self.assertIn('paused', self.rec.values('state'))
        first, second = FakeDetector.instances
        self.assertEqual(first.column_centers[0], 417)
        self.assertEqual(second.column_centers[0], int(417 * 2 / 3))
        self.assertEqual(second.hit_line_y, int(REF_HIT_LINE_Y * 2 / 3))
        self.assertEqual(calls[1][0]['left'], 30)   # viz got the new region

    def test_resize_mid_play_rebuilds_detector(self):
        """RR4 — resized while playing (no pause): the viz's geometry
        poll reports it and a detector for the new layout takes over."""
        small = {'top': 0, 'left': 0, 'width': 1280, 'height': 720}
        calls = []

        def viz(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                self.assertIsNotNone(kwargs['geometry_poll']())
                return standalone_runner.VIZ_EXIT_GEOMETRY
            self.stop.set()
            return standalone_runner.VIZ_EXIT_STOP

        with mock.patch.object(standalone_runner, 'read_game_geometry',
                               return_value=(small, 2 / 3, 2 / 3)):
            self._run(viz)
        first, second = FakeDetector.instances
        self.assertTrue(first.stopped)
        self.assertEqual(second.column_centers[0], int(417 * 2 / 3))
        self.assertEqual(calls[1][0]['width'], 1280)
        self.assertNotIn('paused', self.rec.values('state'))

    def test_fallback_arduino_failure_reports_last_error(self):
        message = "No Arduino found — plug in the Leonardo"

        class Failing:
            def __init__(self, cancel_evt=None):
                raise standalone_runner.ArduinoConnectionError(message)

        with mock.patch.object(standalone_runner, 'ArduinoHIDController',
                               Failing), _quiet():
            standalone_runner.run_standalone(
                self.stop, self.debug, status_cb=self.rec,
                pause_evt=self.pause, controller=None)
        self.assertEqual(self.rec.values('last_error'), [message])
        self.assertEqual(FakeDetector.instances, [])

    def test_fallback_arduino_sys_exit_is_contained(self):
        class Exiting:
            def __init__(self):
                raise SystemExit(1)

        with mock.patch.object(standalone_runner, 'ArduinoHIDController',
                               Exiting), _quiet():
            standalone_runner.run_standalone(
                self.stop, self.debug, status_cb=self.rec,
                pause_evt=self.pause, controller=None)
        self.assertEqual(self.rec.values('last_error'),
                         [standalone_runner.MSG_ARDUINO_UNAVAILABLE])


class VisualizationWindow(unittest.TestCase):
    """U23 — FPS drops to 0.0 when the viz window closes.
    RR3 — the preview never keeps focus away from the game.
    RR4 — geometry polling: move retargets in place, resize exits.
    RR6 — every test has an independent, bounded stop timer, so a
    regression fails instead of hanging the suite."""

    VIZ_HWND = 5151
    COLS = [417, 628, 844, 1061, 1277, 1493]

    def setUp(self):
        self.fg = [GAME_HWND]
        self.focus_given = []
        self.non_activating = []

        def give_focus(hwnd):
            self.focus_given.append(hwnd)
            self.fg[0] = hwnd
            return True

        patches = [
            mock.patch.object(standalone_runner, '_viz_hwnd',
                              return_value=self.VIZ_HWND),
            mock.patch.object(standalone_runner, '_foreground_window',
                              side_effect=lambda: self.fg[0]),
            mock.patch.object(standalone_runner, '_make_non_activating',
                              side_effect=self.non_activating.append),
            mock.patch.object(standalone_runner, '_keep_topmost'),
            mock.patch.object(standalone_runner, '_give_focus_to',
                              side_effect=give_focus),
            mock.patch.object(standalone_runner, 'VIZ_FOCUS_CHECK_S', 0.0),
            mock.patch.object(standalone_runner, 'GEOMETRY_POLL_S', 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run_viz(self, stop, debug=None, pause=None, fps_cb=None,
                 on_frame=None, on_create=None, geometry_poll=None,
                 timeout=3.0):
        """Run run_visualization against mocked cv2 / mss. `on_frame(n)`
        runs on every grab. A Timer sets `stop` after `timeout` no matter
        what, so a broken termination path can't hang the suite."""
        self.grabs = []

        def grab(region):
            self.grabs.append(dict(region))
            if on_frame is not None:
                on_frame(len(self.grabs))
            return np.zeros((region['height'], region['width'], 4),
                           dtype=np.uint8)

        sct = mock.MagicMock()
        sct.__enter__.return_value = sct
        sct.grab.side_effect = grab
        cv2_mocks = dict(namedWindow=mock.DEFAULT,
                         setWindowProperty=mock.DEFAULT,
                         resizeWindow=mock.DEFAULT, moveWindow=mock.DEFAULT,
                         destroyWindow=mock.DEFAULT, imshow=mock.DEFAULT,
                         waitKey=mock.DEFAULT, getWindowProperty=mock.DEFAULT)
        safety = threading.Timer(timeout, stop.set)
        safety.start()
        try:
            with mock.patch.object(standalone_runner.mss, 'mss',
                                   return_value=sct), \
                    mock.patch.multiple(standalone_runner.cv2,
                                        **cv2_mocks) as m, _quiet():
                m['waitKey'].return_value = -1
                m['getWindowProperty'].return_value = 1.0
                if on_create is not None:
                    m['namedWindow'].side_effect = lambda *a: on_create()
                self.cv2 = m
                return standalone_runner.run_visualization(
                    dict(REGION_1080), self.COLS, 921,
                    FakeDetector(GAME_HWND, [], 0, None), stop_evt=stop,
                    debug_evt=debug, fps_callback=fps_cb, pin_console=False,
                    pause_evt=pause, game_hwnd=GAME_HWND,
                    geometry_poll=geometry_poll)
        finally:
            safety.cancel()

    def test_debug_off_zeroes_fps(self):
        """U23 / RR6"""
        stop, debug = threading.Event(), threading.Event()
        debug.set()
        seen = []

        def fps_cb(f):
            seen.append(f)
            if f > 0:
                debug.clear()      # user turns debug off mid-run
            else:
                stop.set()

        reason = self._run_viz(stop, debug=debug, fps_cb=fps_cb)
        self.assertEqual(reason, standalone_runner.VIZ_EXIT_STOP)
        self.assertGreater(seen[0], 0)
        self.assertIn(0.0, seen)
        self.assertEqual(seen[-1], 0.0)

    def test_pause_exit_reason_and_fps_zero(self):
        stop, debug, pause = (threading.Event(), threading.Event(),
                              threading.Event())
        debug.set()
        seen = []

        def fps_cb(f):
            seen.append(f)
            if f > 0:
                pause.set()

        reason = self._run_viz(stop, debug=debug, pause=pause, fps_cb=fps_cb)
        self.assertEqual(reason, standalone_runner.VIZ_EXIT_PAUSE)
        self.assertIn(0.0, seen)

    def test_preview_is_non_activating_and_not_cv2_topmost(self):
        """RR3 — cv2's TOPMOST setter activates the window; it must not
        be used when we have the HWND."""
        stop = threading.Event()
        self._run_viz(stop, on_frame=lambda n: n >= 3 and stop.set())
        self.assertEqual(self.non_activating, [self.VIZ_HWND])
        self.cv2['setWindowProperty'].assert_not_called()
        self.assertEqual(self.focus_given, [])

    def test_focus_stolen_on_creation_is_handed_back(self):
        """RR3 — namedWindow activated the preview: give focus back to
        whoever had it (here the game)."""
        stop = threading.Event()

        def created():
            self.fg[0] = self.VIZ_HWND

        self._run_viz(stop, on_create=created,
                      on_frame=lambda n: n >= 2 and stop.set())
        self.assertEqual(self.focus_given[0], GAME_HWND)
        self.assertEqual(self.fg[0], GAME_HWND)

    def test_safety_net_returns_focus_to_game_mid_run(self):
        """RR3"""
        stop = threading.Event()

        def frame(n):
            if n == 3:
                self.fg[0] = self.VIZ_HWND      # preview became foreground
            if n >= 6:
                stop.set()

        self._run_viz(stop, on_frame=frame)
        self.assertEqual(self.focus_given, [GAME_HWND])

    def test_fallback_topmost_when_hwnd_unknown(self):
        stop = threading.Event()
        with mock.patch.object(standalone_runner, '_viz_hwnd',
                               return_value=0):
            self._run_viz(stop, on_frame=lambda n: n >= 2 and stop.set())
        self.assertEqual(self.cv2['setWindowProperty'].call_count, 1)
        self.assertEqual(self.non_activating, [])

    def test_move_retargets_capture_in_place(self):
        """RR4"""
        stop = threading.Event()
        moved = dict(REGION_1080, left=200, top=100)
        polls = [(moved, self.COLS, 921)]
        reason = self._run_viz(
            stop, geometry_poll=lambda: polls.pop(0) if polls else None,
            on_frame=lambda n: n >= 4 and stop.set())
        self.assertEqual(reason, standalone_runner.VIZ_EXIT_STOP)
        self.assertEqual(self.grabs[-1]['left'], 200)
        self.assertEqual(self.grabs[-1]['top'], 100)

    def test_resize_exits_for_detector_rebuild(self):
        """RR4"""
        stop = threading.Event()
        small = {'top': 0, 'left': 0, 'width': 1280, 'height': 720}
        cols, hit = game_window.scaled_layout(2 / 3, 2 / 3)
        polls = [None, (small, cols, hit)]
        reason = self._run_viz(
            stop, geometry_poll=lambda: polls.pop(0) if polls else None)
        self.assertEqual(reason, standalone_runner.VIZ_EXIT_GEOMETRY)
        self.assertFalse(stop.is_set())


class GeometryTrackerTests(unittest.TestCase):
    """RR4 — the tracker behind Standalone / CLI mid-play polling."""

    def _tracker(self):
        return standalone_runner.GeometryTracker(
            GAME_HWND, dict(REGION_1080),
            *game_window.scaled_layout(1.0, 1.0))

    def test_unchanged_or_unusable_returns_none(self):
        t = self._tracker()
        for geom in ((dict(REGION_1080), 1.0, 1.0), None):
            with mock.patch.object(standalone_runner, 'read_game_geometry',
                                   return_value=geom):
                self.assertIsNone(t.poll())

    def test_move_vs_resize(self):
        t = self._tracker()
        old = t.current
        moved = dict(REGION_1080, left=10)
        with mock.patch.object(standalone_runner, 'read_game_geometry',
                               return_value=(moved, 1.0, 1.0)), _quiet():
            new = t.poll()
        self.assertFalse(standalone_runner.layout_changed(old, new))
        small = {'top': 0, 'left': 0, 'width': 1280, 'height': 720}
        with mock.patch.object(standalone_runner, 'read_game_geometry',
                               return_value=(small, 2 / 3, 2 / 3)), _quiet():
            newer = t.poll()
        self.assertTrue(standalone_runner.layout_changed(new, newer))
        self.assertEqual(t.current[2], int(REF_HIT_LINE_Y * 2 / 3))


# --- AlbumRunner --------------------------------------------------------------

def _tpl(path):
    return np.zeros((20, 40, 3), dtype=np.uint8)


class AlbumRunnerBase(unittest.TestCase):

    def setUp(self):
        self.rec = Recorder()
        self.set_mouse = mock.MagicMock()
        self.get_mouse = mock.MagicMock(return_value=(6, 10, 1))
        patches = [
            mock.patch.object(album, 'find_game_window',
                              return_value=GAME_HWND),
            mock.patch.object(album, 'get_game_geometry',
                              return_value=(dict(REGION_1080), 1.0, 1.0)),
            mock.patch.object(album.AlbumRunner, '_load_tpl',
                              staticmethod(_tpl)),
            mock.patch.object(album, '_get_mouse_params', self.get_mouse),
            mock.patch.object(album, '_set_mouse_params', self.set_mouse),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make_runner(self, **kwargs):
        kwargs.setdefault('controller', FakeInput())
        kwargs.setdefault('status_cb', self.rec)
        with _quiet():
            return album.AlbumRunner(**kwargs)


class AlbumRunnerSetup(AlbumRunnerBase):
    """R7 — EPP is only touched after setup that can fail, and restored
    if anything after it raises."""

    def test_missing_template_leaves_epp_untouched(self):
        with mock.patch.object(album.AlbumRunner, '_load_tpl',
                               staticmethod(mock.Mock(
                                   side_effect=RuntimeError('no tpl')))):
            with self.assertRaises(RuntimeError):
                self.make_runner()
        self.set_mouse.assert_not_called()

    def test_arduino_failure_leaves_epp_untouched(self):
        msg = "No Arduino found — plug in the Leonardo"
        with mock.patch.object(album, 'make_fallback_arduino',
                               return_value=(None, msg)):
            with self.assertRaises(album.AlbumSetupError):
                self.make_runner(controller=None)
        self.set_mouse.assert_not_called()
        self.assertEqual(self.rec.values('last_error'), [msg])

    def test_failure_after_epp_disable_restores_it(self):
        own = FakeInput()
        self.set_mouse.side_effect = [KeyboardInterrupt(), None]
        with mock.patch.object(album, 'make_fallback_arduino',
                               return_value=(own, None)):
            with self.assertRaises(KeyboardInterrupt):
                self.make_runner(controller=None)
        self.assertEqual(self.set_mouse.call_args_list[-1],
                         mock.call(6, 10, 1))
        self.assertTrue(own.closed)

    def test_missing_window_reports_last_error(self):
        with mock.patch.object(album, 'find_game_window', return_value=None):
            with self.assertRaises(album.AlbumSetupError):
                self.make_runner()
        self.get_mouse.assert_not_called()
        self.assertEqual(self.rec.values('last_error'),
                         [game_window.MSG_WINDOW_NOT_FOUND])


class AlbumLoopFixture(AlbumRunnerBase):
    """Runner with the screen / song / wheel steps stubbed. No tests."""

    def setUp(self):
        super().setUp()
        self.clicks = []

    def runner(self, difficulty='legendary', canorus=False, play=True,
               album_page=True, **kwargs):
        r = self.make_runner(difficulty=difficulty, **kwargs)
        r._on_album_page = lambda frame=None: album_page
        r._is_canorus = lambda frame=None: canorus
        r._diagnose_album_page = lambda: None
        r._refresh_geometry = lambda block=True: True
        r._wait_for_album_page = lambda timeout=60.0: True
        r.watcher.grab = lambda: np.zeros((1080, 1920, 3), dtype=np.uint8)
        r.song_player.play = mock.MagicMock(return_value=play)
        r._next_song = lambda: self.clicks.append(1) or True
        return r

    def _run(self, r, songs):
        with _quiet() as out:
            result = r.run(songs_count=songs)
        return result, out.getvalue()


class AlbumRunnerLoop(AlbumLoopFixture):
    """R21 / R22 — outcome propagation and wheel position."""

    def test_wheel_rule_single_difficulty_partial(self):
        result, _ = self._run(self.runner(), 3)
        self.assertEqual(result, album.RESULT_COMPLETED)
        self.assertEqual(len(self.clicks), 2)     # stays on last played

    def test_wheel_rule_single_difficulty_full(self):
        self._run(self.runner(), ALBUM_SONG_COUNT)
        self.assertEqual(len(self.clicks), ALBUM_SONG_COUNT)

    def test_wheel_rule_matches_multi_difficulty(self):
        self._run(self.runner(difficulty=['normal', 'hard']), 3)
        self.assertEqual(len(self.clicks), 2)
        self.clicks.clear()
        self._run(self.runner(difficulty=['normal', 'hard']),
                  ALBUM_SONG_COUNT)
        self.assertEqual(len(self.clicks), ALBUM_SONG_COUNT)

    def test_wheel_rule_with_canorus_skips(self):
        r = self.runner(canorus=True)
        self._run(r, 3)
        self.assertEqual(len(self.clicks), 2)
        r.song_player.play.assert_not_called()

    def test_song_failure_is_not_reported_complete(self):
        for diff in ('legendary', ['normal', 'pro']):
            self.rec.updates.clear()
            result, out = self._run(self.runner(difficulty=diff, play=False),
                                    3)
            self.assertEqual(result, album.RESULT_FAILED)
            self.assertNotIn('Album complete', out)
            self.assertEqual(self.rec.values('last_error'),
                             [album.MSG_SONG_FAILED])

    def test_specific_song_error_not_overwritten(self):
        r = self.runner(play=False)

        def play(*a, **k):
            r._emit({'last_error': song_player.MSG_BEGIN_FAILED})
            return False

        r.song_player.play = play
        self._run(r, 3)
        self.assertEqual(self.rec.values('last_error'),
                         [song_player.MSG_BEGIN_FAILED])

    def test_not_on_album_page(self):
        result, _ = self._run(self.runner(album_page=False), 3)
        self.assertEqual(result, album.RESULT_FAILED)
        self.assertEqual(self.rec.values('last_error'),
                         [album.MSG_NOT_ON_ALBUM_PAGE])

    def test_never_returned_to_album_page(self):
        r = self.runner()
        r._wait_for_album_page = lambda timeout=60.0: False
        result, _ = self._run(r, 3)
        self.assertEqual(result, album.RESULT_FAILED)
        self.assertEqual(self.rec.values('last_error'),
                         [album.MSG_NO_RETURN_TO_ALBUM])

    def test_stop_reports_stopped(self):
        r = self.runner()
        r.stop_evt.set()
        result, out = self._run(r, 3)
        self.assertEqual(result, album.RESULT_STOPPED)
        self.assertIn('Album stopped', out)

    def test_handoff_ignored_when_album_page_showing(self):
        """R6 — UI said mid-song, but the user is on the album page."""
        r = self.runner(mid_song_start=True)
        self._run(r, 1)
        self.assertFalse(r.mid_song_start)
        self.assertEqual(r.song_player.play.call_args.kwargs['skip_intro'],
                         False)


class AlbumProgress(AlbumLoopFixture):
    """Direction A progress bar — status_cb({'album_progress': ...})."""

    def progress(self):
        return self.rec.values('album_progress')

    def test_single_difficulty_with_canorus_skip(self):
        r = self.runner()
        canorus = iter([False, True, False])
        r._is_canorus = lambda frame=None: next(canorus)
        result, _ = self._run(r, 3)
        self.assertEqual(result, album.RESULT_COMPLETED)
        p = self.progress()
        self.assertEqual(p[0], {'index': 0, 'total': 3,
                                'difficulty': 'legendary',
                                'difficulties': ['legendary'],
                                'results': []})
        self.assertEqual([(x['index'], len(x['results'])) for x in p],
                         [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2), (3, 2),
                          (3, 3)])
        self.assertEqual(p[-1]['results'], ['played', 'skipped', 'played'])
        self.assertTrue(self.rec.values('song'))   # legacy string kept

    def test_multi_difficulty_emits_each_difficulty(self):
        r = self.runner(difficulty=['hard', 'normal'])   # any order in
        skip = {(1, 'hard'), (2, 'normal'), (2, 'hard')}
        r._is_canorus = lambda frame=None: (
            (r._progress['index'], r.difficulty) in skip)
        self._run(r, 2)
        p = self.progress()
        self.assertEqual(p[0]['difficulties'], ['normal', 'hard'])
        self.assertEqual(
            [(x['index'], x['difficulty'], len(x['results'])) for x in p],
            [(0, 'normal', 0), (1, 'normal', 0), (1, 'hard', 0),
             (1, 'hard', 1), (2, 'normal', 1), (2, 'hard', 1),
             (2, 'hard', 2)])
        self.assertEqual(p[-1]['results'], ['played', 'skipped'])

    def test_failed_position_recorded(self):
        for diff in ('legendary', ['normal', 'pro']):
            self.rec.updates.clear()
            r = self.runner(difficulty=diff)
            # Third play() fails: position 3 (single) / position 2 at
            # Normal (two difficulties per position).
            r.song_player.play = mock.MagicMock(side_effect=[True, True,
                                                             False, False])
            result, _ = self._run(r, 3)
            self.assertEqual(result, album.RESULT_FAILED)
            expected = (['played', 'played', 'failed']
                        if diff == 'legendary' else ['played', 'failed'])
            self.assertEqual(self.progress()[-1]['results'], expected)

    def test_stopped_position_gets_no_entry(self):
        r = self.runner()

        def play(*a, **k):
            if r.song_player.play.call_count == 2:
                r.stop_evt.set()
                return False
            return True

        r.song_player.play = mock.MagicMock(side_effect=play)
        result, _ = self._run(r, 3)
        self.assertEqual(result, album.RESULT_STOPPED)
        self.assertEqual(self.progress()[-1]['results'], ['played'])
        self.assertEqual(self.progress()[-1]['index'], 2)

    def test_mid_song_handoff_position_counts_as_played(self):
        r = self.runner(mid_song_start=True, album_page=False)
        r._find_select_song = lambda frame=None: None
        result, _ = self._run(r, 2)
        self.assertEqual(result, album.RESULT_COMPLETED)
        self.assertTrue(r.song_player.play.call_args_list[0]
                        .kwargs['skip_intro'])
        self.assertEqual(self.progress()[-1]['results'],
                         ['played', 'played'])

    def test_run_start_emitted_even_if_not_on_album_page(self):
        self._run(self.runner(album_page=False), 3)
        self.assertEqual(self.progress(),
                         [{'index': 0, 'total': 3, 'difficulty': 'legendary',
                           'difficulties': ['legendary'], 'results': []}])

    def test_emitted_snapshots_are_not_mutated_later(self):
        self._run(self.runner(), 2)
        first = self.progress()[0]
        self.assertEqual(first['results'], [])


class AlbumRunnerClicksAndGeometry(AlbumRunnerBase):

    def test_stop_during_next_song_settle_propagates(self):
        """RR5"""
        r = self.make_runner()
        r.watcher.click_ref = mock.MagicMock(return_value=True)

        def settle(secs):
            r.stop_evt.set()
            return True

        r.watcher.stop_wait = settle
        self.assertFalse(r._next_song())
        self.assertEqual(r.watcher.click_ref.call_count, 1)
        self.assertEqual(self.rec.values('last_error'), [])

    def test_stop_during_final_wheel_reset_is_not_completed(self):
        """RR5 — full album, Stop lands while the last click settles."""
        r = self.make_runner()
        r.watcher.click_ref = mock.MagicMock(return_value=True)
        r.watcher.stop_wait = lambda secs: r.stop_evt.set() or True
        with _quiet():
            self.assertEqual(r._finish_wheel(ALBUM_SONG_COUNT),
                             album.RESULT_STOPPED)
        r.stop_evt.clear()
        r._next_song = lambda: r.stop_evt.set() or True
        with _quiet():
            self.assertEqual(r._finish_wheel(ALBUM_SONG_COUNT),
                             album.RESULT_STOPPED)

    def test_next_song_retries_once_then_fails(self):
        """R12"""
        r = self.make_runner()
        r.watcher.click_ref = mock.MagicMock(return_value=False)
        with _quiet():
            self.assertFalse(r._next_song())
        self.assertEqual(r.watcher.click_ref.call_count, 2)
        self.assertEqual(self.rec.values('last_error'),
                         [album.MSG_NEXT_SONG_FAILED])

    def test_window_move_updates_watcher_and_song_player(self):
        """R10"""
        r = self.make_runner()
        moved = {'top': 50, 'left': 70, 'width': 1920, 'height': 1080}
        with mock.patch.object(album, 'read_game_geometry',
                               return_value=(moved, 1.0, 1.0)), _quiet():
            self.assertTrue(r._refresh_geometry(block=False))
        self.assertEqual(r.watcher.region, moved)
        self.assertEqual(r.song_player.region, moved)
        self.assertEqual(r.tpl['go_perform'].shape, (20, 40, 3))

    def test_window_resize_rescales_templates(self):
        r = self.make_runner()
        small = {'top': 0, 'left': 0, 'width': 960, 'height': 540}
        with mock.patch.object(album, 'read_game_geometry',
                               return_value=(small, 0.5, 0.5)), _quiet():
            r._refresh_geometry(block=False)
        self.assertEqual(r.watcher.scale_x, 0.5)
        self.assertEqual(r.song_player.scale_y, 0.5)
        self.assertEqual(r.tpl['go_perform'].shape, (10, 20, 3))
        self.assertIs(r.watcher.templates, r.tpl)

    def test_minimized_mid_song_keeps_last_geometry(self):
        r = self.make_runner()
        with mock.patch.object(album, 'read_game_geometry',
                               return_value=None):
            self.assertTrue(r._refresh_geometry(block=False))
        self.assertEqual(r.watcher.region, REGION_1080)


# --- SongPlayer ----------------------------------------------------------------

_TPL_SCREEN = {'go_perform': 'album', 'begin_perf': 'difficulty',
               'select_song': 'results'}


class FakeWatcher:
    """Screen-state model for SongPlayer: clicks move between screens
    the way the game's UI does."""

    def __init__(self, stop_evt):
        self.stop_evt = stop_evt
        self.screen = 'album'
        self.clicks = []
        self.click_result = True
        self.on_click = None
        self.generation = 0

    def grab(self):
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        if self.screen == 'rhythm':
            frame[REF_HIT_LINE_Y - 8:REF_HIT_LINE_Y + 9, :, 0] = 255
        return frame

    def ref_rect_to_local(self, x, y, w, h):
        return (x, y, w, h)

    def match(self, frame, tpl_name, roi=None, threshold=None):
        if _TPL_SCREEN.get(tpl_name) == self.screen:
            return 0.99, (10, 10)
        return None

    def click_ref(self, x, y):
        self.clicks.append((x, y))
        if not self.click_result:
            return False
        if (x, y) == ALBUM_GO_PERFORM_XY and self.screen == 'album':
            self.screen = 'difficulty'
        elif (x, y) == ALBUM_BEGIN_PERFORMANCE_XY \
                and self.screen == 'difficulty':
            self.screen = 'rhythm'
        if self.on_click:
            self.on_click((x, y))
        return True

    def click_local(self, x, y, generation=None):
        self.clicks.append(('local', x, y))
        return self.click_result

    def wait_for(self, tpl_name, ref_roi, timeout=5.0, present=True):
        return (_TPL_SCREEN.get(tpl_name) == self.screen) == present

    def stop_wait(self, secs):
        return self.stop_evt.wait(secs * 0.02)


GO = ALBUM_GO_PERFORM_XY
CARD = ALBUM_DIFFICULTY_COORDS['legendary']
BEGIN = ALBUM_BEGIN_PERFORMANCE_XY


class SongPlayerBase(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(song_player, 'SCREEN_DETECT_TIMEOUT_S', 0.2)
        p.start()
        self.addCleanup(p.stop)
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.rec = Recorder()
        self.dumps = []
        self.watcher = FakeWatcher(self.stop)
        self.geometry_fn = mock.MagicMock(return_value=True)
        self.sp = song_player.SongPlayer(
            self.watcher, FakeInput(), GAME_HWND, dict(REGION_1080), 1.0,
            1.0, self.stop, self.pause, None, status_cb=self.rec,
            dump_frame_fn=self.dumps.append,
            refresh_geometry_fn=self.geometry_fn)
        self.rhythm = mock.MagicMock(return_value=True)
        self.sp._rhythm_phase = self.rhythm

    def play(self, **kwargs):
        with _quiet():
            return self.sp.play('legendary', **kwargs)


class SongPlayerHandoff(SongPlayerBase):
    """R6 — skip_intro is verified against what's on screen."""

    def test_rhythm_on_screen_attaches_directly(self):
        self.watcher.screen = 'rhythm'
        self.assertTrue(self.play(skip_intro=True))
        self.rhythm.assert_called_once()
        self.assertEqual(self.watcher.clicks, [])

    def test_album_page_falls_back_to_intro(self):
        self.watcher.screen = 'album'
        self.assertTrue(self.play(skip_intro=True))
        self.assertEqual(self.watcher.clicks, [GO, CARD, BEGIN])
        self.rhythm.assert_called_once()

    def test_difficulty_screen_continues_intro(self):
        self.watcher.screen = 'difficulty'
        self.assertTrue(self.play(skip_intro=True))
        self.assertEqual(self.watcher.clicks, [CARD, BEGIN])

    def test_results_screen_returns_without_detector(self):
        self.watcher.screen = 'results'
        self.assertTrue(self.play(skip_intro=True))
        self.rhythm.assert_not_called()

    def test_unknown_screen_fails_with_last_error(self):
        self.watcher.screen = 'world'
        self.assertFalse(self.play(skip_intro=True))
        self.rhythm.assert_not_called()
        self.assertEqual(self.rec.values('last_error'),
                         [song_player.MSG_NO_RHYTHM])
        self.assertEqual(self.dumps, ['handoff_no_rhythm'])


class SongPlayerIntro(SongPlayerBase):

    def test_normal_intro(self):
        self.assertTrue(self.play())
        self.assertEqual(self.watcher.clicks, [GO, CARD, BEGIN])

    def test_click_that_never_lands_aborts_like_a_miss(self):
        """R12"""
        self.watcher.click_result = False
        self.assertFalse(self.play())
        self.assertEqual(self.watcher.clicks, [GO])
        self.assertIn('Go Perform', self.rec.values('last_error')[0])
        self.assertEqual(self.dumps, ['click_go_perform'])
        self.rhythm.assert_not_called()

    def test_pause_before_first_click_holds_input(self):
        """R8"""
        self.pause.set()
        result = []
        t = threading.Thread(target=lambda: result.append(self.play()))
        t.start()
        time.sleep(0.15)
        self.assertEqual(self.watcher.clicks, [])
        self.assertIn('paused', self.rec.values('state'))
        self.pause.clear()
        t.join(2.0)
        self.assertEqual(result, [True])
        self.assertEqual(self.watcher.clicks, [GO, CARD, BEGIN])
        self.geometry_fn.assert_called_with(True)

    def test_stop_wins_while_paused_in_intro(self):
        self.pause.set()
        result = []
        t = threading.Thread(target=lambda: result.append(self.play()))
        t.start()
        time.sleep(0.05)
        started = time.time()
        self.stop.set()
        t.join(1.0)
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - started, 0.5)
        self.assertEqual(result, [False])
        self.assertEqual(self.watcher.clicks, [])

    def test_resume_reidentifies_screen_before_next_click(self):
        """R8 — the user went back to the album page while paused."""
        def back_to_album():
            self.watcher.screen = 'album'

        def on_click(xy):
            if xy == GO:
                # Difficulty screen is up; user pauses, presses Esc in
                # game (back to the album page), then resumes.
                self.watcher.on_click = None
                self.pause.set()
                threading.Timer(0.05, back_to_album).start()
                threading.Timer(0.10, self.pause.clear).start()

        self.watcher.on_click = on_click
        self.assertTrue(self.play())
        self.assertEqual(self.watcher.clicks, [GO, GO, CARD, BEGIN])


class SongPlayerEndWatcherClick(unittest.TestCase):
    """RR1 — window resized between the Select Song match and the click:
    re-capture instead of clicking old local coords on the new window
    (reproduced as a click at (1390,1010), outside a 1280x720 game)."""

    def setUp(self):
        for name in ('CLICK_HOVER_S', 'CLICK_HOLD_S', 'CLICK_SETTLE_S'):
            p = mock.patch.object(screen_watcher, name, 0)
            p.start()
            self.addCleanup(p.stop)

    def test_resize_between_match_and_click_recaptures(self):
        stop = threading.Event()
        ctrl = FakeInput()
        w = screen_watcher.ScreenWatcher(dict(REGION_1080), 1.0, 1.0, ctrl,
                                         stop, templates={})
        w.grab = lambda: np.zeros((10, 10, 3), dtype=np.uint8)
        resized = []

        def match(frame, tpl_name, roi=None, threshold=None):
            # Select Song at ref (1390, 1010), at the capture's scale.
            pos = (int(1390 * w.scale_x), int(1010 * w.scale_y))
            if not resized:
                resized.append(True)
                # Album thread applies a resize right after this match.
                w.update_geometry({'top': 0, 'left': 0, 'width': 1280,
                                   'height': 720}, 2 / 3, 2 / 3)
            return 0.99, pos

        w.match = match
        sp = song_player.SongPlayer(w, ctrl, GAME_HWND, dict(REGION_1080),
                                    1.0, 1.0, stop, threading.Event(), None)
        with _quiet():
            self.assertTrue(sp._click_select_song_if_shown())
        moves = [c for c in ctrl.snapshot() if c[0] == 'move_to']
        self.assertEqual(moves, [('move_to', int(1390 * 2 / 3),
                                  int(1010 * 2 / 3))])
        self.assertIn('mouse_down', [c[0] for c in ctrl.snapshot()])


class SongPlayerRhythmPhase(unittest.TestCase):
    """R8 / R10 — pause before the detector starts; a resize rebuilds the
    detector for the new lane layout."""

    def setUp(self):
        FakeDetector.instances = []
        for target, value in ((song_player, 'NoteDetector'),
                              (song_player, 'GEOMETRY_POLL_S')):
            p = mock.patch.object(target, value,
                                  FakeDetector if value == 'NoteDetector'
                                  else 0.03)
            p.start()
            self.addCleanup(p.stop)
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.song_end = threading.Event()
        self.rec = Recorder()
        self.geometry_fn = mock.MagicMock(return_value=True)
        self.sp = song_player.SongPlayer(
            FakeWatcher(self.stop), FakeInput(), GAME_HWND,
            dict(REGION_1080), 1.0, 1.0, self.stop, self.pause, None,
            status_cb=self.rec, refresh_geometry_fn=self.geometry_fn)

        def end_watcher(local_stop):
            while not local_stop.is_set():
                if self.song_end.wait(0.01):
                    local_stop.set()

        self.sp._end_watcher = end_watcher

    def _start(self):
        out = []
        t = threading.Thread(
            target=lambda: out.append(self.sp._rhythm_phase()), daemon=True)
        with _quiet():
            t.start()
        return t, out

    def test_pause_at_entry_defers_detector(self):
        self.pause.set()
        with _quiet():
            t, out = self._start()
            time.sleep(0.1)
            self.assertEqual(FakeDetector.instances, [])
            self.assertIn('paused', self.rec.values('state'))
            self.pause.clear()
            self.assertTrue(_wait_until(lambda: FakeDetector.instances))
            self.song_end.set()
            t.join(2.0)
        self.assertEqual(out, [True])
        self.assertTrue(FakeDetector.instances[0].stopped)

    def test_resize_rebuilds_detector(self):
        with _quiet():
            t, out = self._start()
            self.assertTrue(_wait_until(lambda: FakeDetector.instances))
            self.sp.update_geometry({'top': 0, 'left': 0, 'width': 1280,
                                     'height': 720}, 2 / 3, 2 / 3)
            self.assertTrue(_wait_until(
                lambda: len(FakeDetector.instances) == 2))
            self.song_end.set()
            t.join(2.0)
        first, second = FakeDetector.instances
        self.assertTrue(first.stopped)
        self.assertEqual(second.column_centers[0], int(417 * 2 / 3))
        self.geometry_fn.assert_any_call(False)

    def test_stop_during_resize_teardown_does_not_restart(self):
        """RR2 — Stop lands while the old detector is being joined."""
        stop = self.stop

        class StopOnTeardown(FakeDetector):
            def stop(self):
                super().stop()
                stop.set()

        with mock.patch.object(song_player, 'NoteDetector',
                               StopOnTeardown), _quiet():
            t, out = self._start()
            self.assertTrue(_wait_until(lambda: FakeDetector.instances))
            self.sp.update_geometry({'top': 0, 'left': 0, 'width': 1280,
                                     'height': 720}, 2 / 3, 2 / 3)
            t.join(2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(FakeDetector.instances), 1)
        self.assertEqual(out, [True])

    def test_pause_during_resize_teardown_does_not_restart(self):
        """RR2 — Pause lands during teardown: no detector until resume."""
        pause = self.pause

        class PauseOnTeardown(FakeDetector):
            def stop(self):
                super().stop()
                pause.set()

        with mock.patch.object(song_player, 'NoteDetector',
                               PauseOnTeardown), _quiet():
            t, out = self._start()
            self.assertTrue(_wait_until(lambda: FakeDetector.instances))
            self.sp.update_geometry({'top': 0, 'left': 0, 'width': 1280,
                                     'height': 720}, 2 / 3, 2 / 3)
            time.sleep(0.15)
            self.assertEqual(len(FakeDetector.instances), 1)
            self.stop.set()
            t.join(2.0)
        self.assertEqual(len(FakeDetector.instances), 1)

    def test_window_lost_on_resume_returns_false(self):
        self.geometry_fn.side_effect = lambda block: not block
        with _quiet():
            t, out = self._start()
            self.assertTrue(_wait_until(lambda: FakeDetector.instances))
            self.pause.set()
            time.sleep(0.1)
            self.pause.clear()
            t.join(2.0)
        self.assertEqual(out, [False])


if __name__ == '__main__':
    unittest.main()
