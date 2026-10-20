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
  RC1 game closed mid-run: Album loops until Stop with no last_error,
      Standalone never notices (minimized must still just wait)
  RC2 detector focus gate trusts a recycled game HWND (another process)
  RC3 album.py CLI ignores INPUT_BACKEND_DEFAULT (always the Arduino)
  RC4 calibrate.py isn't DPI-aware (overlay misplaced on scaled displays)
  W1  game closed during the final wheel click's settle → "completed"
  W2  the run's window stops being the game's while it runs (recreated on
      a fullscreen / resolution switch, HWND recycled to another app): end
      with MSG_WINDOW_CHANGED; never follow, click or type elsewhere
      (RG4-RG8: process handle held for the run, PID-matched pre-click)

Run:  python -m unittest discover -s tests -p "test_*.py"
"""
import contextlib
import io
import os
import subprocess
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
NEW_HWND = 5353      # the window Genshin recreated (fullscreen toggle, ...)


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

    def test_runs_pid_anchor_beats_the_hwnds_current_owner(self):
        """RG1 — a detector built with the run's game PID never adopts the
        PID of whatever its HWND belongs to now: a handle already owned
        by another app at start() gets no input."""
        self.pids[GAME_HWND] = 200          # handle now owned by another app
        det = detector.NoteDetector(GAME_HWND, [10] * len(KEYS), 50,
                                    self.ctrl, pid=100)
        self.addCleanup(det.stop)
        det.start()
        self.assertEqual(det._game_pid, 100)
        time.sleep(0.15)
        self.assertEqual(self._downs(), [])

    def test_recycled_game_hwnd_of_another_process_is_not_focused(self):
        """RC2 — Genshin closed and Windows recycled its HWND for another
        app's window: same handle, different owning PID → release held
        keys and press nothing (the keys would land in that app)."""
        self.det.start()
        self.assertTrue(_wait_until(
            lambda: all(self.det.pressed[k] for k in KEYS)))
        self.pids[GAME_HWND] = 200          # handle now owned by another app
        self.assertTrue(_wait_until(
            lambda: not any(self.det.pressed[k] for k in KEYS)))
        downs = len(self._downs())
        time.sleep(0.15)                   # pixels still dark the whole time
        self.assertEqual(len(self._downs()), downs)

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


# A handle with bit 31 set, as the three ctypes representations in play
# report it: signed c_int (default restype, e.g. GetForegroundWindow),
# sign-extended HWND / c_void_p (ui.py binds FindWindowW to that), and the
# bare 32-bit value.
BIT31_SIGNED = -2147478988                     # 0x80001234 as c_int
BIT31_HWND = 0xFFFFFFFF80001234                # same handle via HWND
BIT31_RAW = 0x80001234


class HwndComparisons(unittest.TestCase):
    """MR12 — HWND equality across ctypes restypes (bit 31 set)."""

    def test_same_hwnd_across_representations(self):
        same = game_window.same_hwnd
        self.assertTrue(same(BIT31_SIGNED, BIT31_HWND))
        self.assertTrue(same(BIT31_HWND, BIT31_RAW))
        self.assertTrue(same(BIT31_SIGNED, BIT31_RAW))
        self.assertFalse(same(BIT31_SIGNED, 0x80001235))
        self.assertFalse(same(0, 0))
        self.assertFalse(same(None, BIT31_HWND))

    def test_detector_gate_sees_game_focused_with_bit31_handle(self):
        """The foreground gate must not decide "not focused" just because
        GetForegroundWindow (signed) and FindWindowW (HWND) disagree on
        representation — with the PID fallback unavailable too."""
        det = detector.NoteDetector(BIT31_HWND, [10] * len(KEYS), 50,
                                    FakeInput())
        with mock.patch.object(detector, '_foreground_hwnd',
                               return_value=BIT31_SIGNED), \
                mock.patch.object(detector, '_window_pid', return_value=0):
            self.assertIs(det._is_game_foreground(), True)
        det.hwnd = BIT31_SIGNED
        with mock.patch.object(detector, '_foreground_hwnd',
                               return_value=BIT31_HWND), \
                mock.patch.object(detector, '_window_pid', return_value=0):
            self.assertIs(det._is_game_foreground(), True)
        with mock.patch.object(detector, '_foreground_hwnd',
                               return_value=0x80001235), \
                mock.patch.object(detector, '_window_pid', return_value=0):
            self.assertIs(det._is_game_foreground(), False)

    def test_detector_gate_rejects_only_a_known_pid_mismatch(self):
        """RC2 — same HWND: a different owning PID rejects; an unknown
        PID (lookup failed, either side) keeps the HWND-only answer."""
        det = detector.NoteDetector(GAME_HWND, [10] * len(KEYS), 50,
                                    FakeInput())
        det._game_pid = 100
        with mock.patch.object(detector, '_foreground_hwnd',
                               return_value=GAME_HWND):
            for live, expected in ((100, True), (0, True), (200, False)):
                with mock.patch.object(detector, '_window_pid',
                                       return_value=live):
                    self.assertIs(det._is_game_foreground(), expected, live)
            det._game_pid = 0
            with mock.patch.object(detector, '_window_pid',
                                   return_value=200):
                self.assertIs(det._is_game_foreground(), True)

    def test_detector_window_pid_accepts_any_representation(self):
        for h in (BIT31_SIGNED, BIT31_HWND, BIT31_RAW):
            self.assertIsInstance(detector._window_pid(h), int)

    def test_viz_focus_guard_matches_bit31_preview(self):
        """Preview HWND (HWND restype) vs foreground (signed) for the same
        window → the guard still hands focus back to the game."""
        with mock.patch.object(standalone_runner, '_foreground_window',
                               return_value=BIT31_SIGNED), \
                mock.patch.object(standalone_runner, '_keep_topmost'), \
                mock.patch.object(standalone_runner, '_give_focus_to',
                                  return_value=True) as give, _quiet():
            standalone_runner._guard_viz_focus(BIT31_HWND, GAME_HWND)
        give.assert_called_once_with(GAME_HWND)


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

    def _live_watcher(self, ctrl, live):
        return screen_watcher.ScreenWatcher(
            REGION_1080, 1.0, 1.0, ctrl, self.stop, templates={},
            live_geometry_fn=lambda: live[0])

    def test_live_rect_change_mid_hover_blocks_mouse_down(self):
        """RR7 — the window shrank right after the last geometry poll, so
        nobody called update_geometry and the generation is unchanged.
        The live client-rect read right before mouse-down must refuse the
        click, publish the new geometry and bump the generation."""
        ctrl = FakeInput()
        live = [(dict(REGION_1080), 1.0, 1.0)]
        w = self._live_watcher(ctrl, live)
        small = {'top': 0, 'left': 0, 'width': 1280, 'height': 720}
        gen = w.generation
        real_wait = w.stop_wait

        def hover(secs):                  # live rect changes mid-hover
            live[0] = (small, 2 / 3, 2 / 3)
            return real_wait(secs)

        w.stop_wait = hover
        with _quiet():
            self.assertFalse(w.click_local(1390, 1010, generation=gen))
        self.assertNotIn('mouse_down', [c[0] for c in ctrl.snapshot()])
        self.assertEqual(w.region, small)
        self.assertNotEqual(w.generation, gen)

    def test_live_check_refuses_when_window_unusable(self):
        """RR7 — minimized / closed between poll and click."""
        ctrl = FakeInput()
        w = self._live_watcher(ctrl, [None])
        with _quiet():
            self.assertFalse(w.click_ref(500, 500))
        self.assertNotIn('mouse_down', [c[0] for c in ctrl.snapshot()])

    def test_click_ref_recomputes_target_after_live_change(self):
        """RR7 — a reference click is still valid on the new window, so
        click_ref re-targets once instead of failing."""
        ctrl = FakeInput()
        live = [(dict(REGION_1080), 1.0, 1.0)]
        w = self._live_watcher(ctrl, live)
        real_move = ctrl.move_to

        def move(x, y, **k):
            live[0] = ({'top': 0, 'left': 0, 'width': 1280, 'height': 720},
                       2 / 3, 2 / 3)
            return real_move(x, y, **k)

        ctrl.move_to = move
        with _quiet():
            self.assertTrue(w.click_ref(1500, 900))
        moves = [c[1:] for c in ctrl.snapshot() if c[0] == 'move_to']
        self.assertEqual(moves, [(1500, 900), (1000, 600)])
        self.assertEqual([c[0] for c in ctrl.snapshot()].count('mouse_down'),
                         1)

    def test_unchanged_live_rect_clicks_normally(self):
        ctrl = FakeInput()
        w = self._live_watcher(ctrl, [(dict(REGION_1080), 1.0, 1.0)])
        with _quiet():
            self.assertTrue(w.click_local(100, 100,
                                          generation=w.generation))
        self.assertIn('mouse_down', [c[0] for c in ctrl.snapshot()])

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

GAME_PID = 7100      # the game process a run is bound to
FOREIGN_PID = 9900   # some other app


class FakeUser32:
    """Win32 window surface as game_window sees it, plus the run's process
    model (`win32()` wires both in):
    - windows are owned by GAME_PID unless `owners` says otherwise — an
      HWND recycled to another app keeps a perfectly valid rect;
    - FindWindowW returns `find` while that window is alive;
    - the game's process handle: OpenProcess fails if `open_fails`; it is
      signaled once `exited` (None: once GAME_PID owns no live window, so
      a closed game window with nothing else reads as an exited game)."""

    def __init__(self, exists=True, iconic=(), size=(1920, 1080),
                 origin=(0, 0), find=GAME_HWND, windows=None):
        self.exists = exists
        self.iconic = list(iconic)   # successive IsIconic results
        self.size = size
        self.origin = origin
        self.find = find
        self.shown = []
        # Optional {hwnd: (w, h)} of the live windows (any owner). None =
        # a single window per `exists` / `size` (handle `find`).
        self.windows = windows
        self.owners = {}
        self.exited = None
        self.open_fails = False
        self.opened = []
        self.closed = []

    @staticmethod
    def _h(hwnd):
        return int(getattr(hwnd, 'value', hwnd) or 0)  # HWND() or int

    def _live(self):
        if self.windows is not None:
            return list(self.windows)
        return [self.find] if self.exists and self.find else []

    def owner(self, hwnd):
        return self.owners.get(self._h(hwnd), GAME_PID)

    # --- scenarios ---
    def recreate(self, new_hwnd, size):
        """The (still running) game replaced its window — fullscreen /
        resolution switch."""
        self.windows = {new_hwnd: size}
        self.find = new_hwnd
        self.exited = False

    def destroy(self):
        """The game destroyed its window but still runs."""
        self.windows = {}
        self.exists = False
        self.exited = False

    def quit(self):
        """The game process exited."""
        self.windows = {}
        self.exists = False
        self.exited = True

    def recycle(self, hwnd, pid=FOREIGN_PID):
        """Windows handed `hwnd` to another app's window (valid rect)."""
        self.owners[self._h(hwnd)] = pid

    # --- kernel32 (patched in by win32()) ---
    def open_process(self, pid):
        if self.open_fails or not pid:
            return None
        handle = ('process', pid, len(self.opened))
        self.opened.append(handle)
        return handle

    def process_exited(self, handle):
        if self.exited is not None:
            return self.exited
        return not any(self.owner(h) == GAME_PID for h in self._live())

    def close_handle(self, handle):
        self.closed.append(handle)

    # --- user32 ---
    def FindWindowW(self, cls, title):
        return self.find if self.find and self.IsWindow(self.find) else 0

    def IsWindow(self, hwnd):
        if self.windows is not None:
            return self._h(hwnd) in self.windows
        return self.exists

    def GetWindowThreadProcessId(self, hwnd, ref):
        ref._obj.value = self.owner(hwnd) if self.IsWindow(hwnd) else 0
        return 1

    def IsIconic(self, hwnd):
        return self.iconic.pop(0) if self.iconic else False

    def ShowWindow(self, hwnd, cmd):
        self.shown.append(cmd)

    def GetClientRect(self, hwnd, ref):
        size = self.size
        if self.windows is not None:
            size = self.windows.get(self._h(hwnd), (0, 0))
        ref._obj.left, ref._obj.top = 0, 0
        ref._obj.right, ref._obj.bottom = size

    def GetWindowRect(self, hwnd, ref):
        self.GetClientRect(hwnd, ref)

    def ClientToScreen(self, hwnd, ref):
        ref._obj.x, ref._obj.y = self.origin


def win32(fake):
    """Patch game_window's Win32 surface with `fake` (user32 + the game's
    process handle) and shorten the closed-vs-changed look (keeps lost-
    window tests fast) — a context manager."""
    stack = contextlib.ExitStack()

    def patch(name, **kwargs):
        stack.enter_context(mock.patch.object(game_window, name, **kwargs))

    patch('_user32', return_value=fake)
    patch('_open_process',
          side_effect=getattr(fake, 'open_process', lambda pid: None))
    patch('_process_exited',
          side_effect=getattr(fake, 'process_exited', lambda h: False))
    patch('_close_handle',
          side_effect=getattr(fake, 'close_handle', lambda h: None))
    patch('LOST_CONFIRM_S', new=0.05)
    patch('LOST_POLL_S', new=0.01)
    return stack


class GameWindowGeometry(unittest.TestCase):

    def _patch(self, fake):
        stack = win32(fake)
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)

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

    def test_window_gone_only_when_closed(self):
        """RC1 — closed vs minimized / too small / off-screen (all of
        which read_game_geometry reports as None)."""
        self._patch(FakeUser32(exists=False))
        self.assertTrue(game_window.window_gone(GAME_HWND))
        self.assertTrue(game_window.window_gone(0))
        self.assertTrue(game_window.window_gone(None))
        for fake in (FakeUser32(iconic=[True]), FakeUser32(size=(640, 480)),
                     FakeUser32(origin=(-32000, -32000))):
            self._patch(fake)
            self.assertIsNone(game_window.read_game_geometry(GAME_HWND))
            self.assertFalse(game_window.window_gone(GAME_HWND))

    def test_window_gone_lookup_error_counts_as_present(self):
        broken = mock.MagicMock()
        broken.IsWindow.side_effect = OSError('boom')
        self._patch(broken)
        self.assertFalse(game_window.window_gone(GAME_HWND))

    # --- the run's window classifier (GameWindowRef) ---

    A = game_window.WINDOW_ALIVE
    CHANGED = game_window.WINDOW_CHANGED
    CLOSED = game_window.WINDOW_CLOSED

    def ref(self, fake, **kwargs):
        self._patch(fake)
        r = game_window.GameWindowRef(GAME_HWND, **kwargs)
        self.addCleanup(r.close)
        return r

    def test_classify_alive_minimized_changed_closed(self):
        """ALIVE while ours (minimized too); CHANGED once the still-running
        game destroys / recreates it — never followed; CLOSED once the
        game process exits."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        r = self.ref(fake)
        self.assertEqual(r.pid, GAME_PID)
        self.assertIsNotNone(r.handle)
        self.assertEqual(r.classify(), self.A)
        fake.iconic = [True]
        self.assertIsNone(game_window.read_game_geometry(GAME_HWND))
        self.assertEqual(r.classify(), self.A)        # minimized: wait
        fake.recreate(NEW_HWND, (1280, 720))
        self.assertEqual(r.classify(), self.CHANGED)
        self.assertEqual(r.hwnd, GAME_HWND)           # never followed
        fake.destroy()
        self.assertEqual(r.classify(), self.CHANGED)
        fake.quit()
        self.assertEqual(r.classify(), self.CLOSED)

    def test_process_exit_is_closed_even_with_the_window_up(self):
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        r = self.ref(fake)
        fake.exited = True                            # handle signaled
        self.assertFalse(r.alive())
        self.assertEqual(r.classify(), self.CLOSED)

    def test_recycled_foreign_hwnd_with_valid_rect_is_changed(self):
        """RG4 — our HWND now belongs to another app and has a perfectly
        valid rect: not alive (no click / input) and CHANGED; CLOSED if
        the game itself exited."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        r = self.ref(fake)
        fake.recycle(GAME_HWND)
        fake.exited = False                           # the game runs on
        self.assertIsNotNone(game_window.read_game_geometry(GAME_HWND))
        self.assertFalse(r.alive())
        self.assertEqual(r.classify(), self.CHANGED)
        fake.exited = True
        self.assertEqual(r.classify(), self.CLOSED)

    def test_pid_reuse_cannot_revive_the_run(self):
        """RG5 — the game exited and a new process got the same PID number
        (owning a "Genshin Impact" window). The handle we hold is the OLD
        process: signaled, so CLOSED — nothing adopted."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        r = self.ref(fake)
        fake.windows = {NEW_HWND: (1920, 1080)}       # owner: GAME_PID
        fake.find = NEW_HWND
        fake.exited = True
        self.assertEqual(r.classify(), self.CLOSED)
        self.assertEqual(r.hwnd, GAME_HWND)

    def test_no_process_handle_falls_back_to_the_title(self):
        """OpenProcess failed: CHANGED while some "Genshin Impact" window
        exists; CLOSED only once none appears across several looks."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        fake.open_fails = True
        r = self.ref(fake)
        self.assertIsNone(r.handle)
        self.assertEqual(r.classify(), self.A)
        fake.recreate(NEW_HWND, (1280, 720))
        self.assertEqual(r.classify(), self.CHANGED)
        fake.windows = {}
        looks = []
        real_find = game_window.find_game_window
        with mock.patch.object(game_window, 'find_game_window',
                               side_effect=lambda: looks.append(1)
                               or real_find()):
            self.assertEqual(r.classify(), self.CLOSED)
        self.assertGreaterEqual(len(looks), 2)
        # ... and a window showing up during the look means CHANGED.
        reappear = threading.Timer(0.05, fake.recreate,
                                   (NEW_HWND, (1280, 720)))
        reappear.start()
        self.addCleanup(reappear.cancel)
        with mock.patch.object(game_window, 'LOST_CONFIRM_S', 2.0):
            self.assertEqual(r.classify(), self.CHANGED)

    def test_lost_window_look_is_stop_aware(self):
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        fake.open_fails = True
        r = self.ref(fake)
        fake.windows = {}
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        started = time.time()
        with mock.patch.object(game_window, 'LOST_CONFIRM_S', 10.0):
            r.classify(stop)
        self.assertLess(time.time() - started, 1.0)

    def test_unreadable_pid_never_follows_anything(self):
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        fake.owners[GAME_HWND] = 0                    # PID unreadable
        with mock.patch.object(game_window, '_PID_READ_RETRY_S', 0):
            r = self.ref(fake)
        self.assertEqual((r.pid, r.handle), (0, None))
        self.assertTrue(r.alive())                    # IsWindow alone
        fake.recreate(NEW_HWND, (1280, 720))
        self.assertEqual(r.classify(), self.CHANGED)
        self.assertEqual(r.hwnd, GAME_HWND)

    def test_pid_read_is_retried(self):
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        real = fake.GetWindowThreadProcessId
        reads = []

        def flaky(hwnd, ref):
            reads.append(hwnd)
            if len(reads) == 1:
                ref._obj.value = 0
                return 0
            return real(hwnd, ref)

        fake.GetWindowThreadProcessId = flaky
        with mock.patch.object(game_window, '_PID_READ_RETRY_S', 0):
            r = self.ref(fake)
        self.assertEqual(r.pid, GAME_PID)
        self.assertIsNotNone(r.handle)

    def test_close_releases_the_handle_once(self):
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        r = self.ref(fake)
        r.close()
        r.close()
        self.assertEqual(fake.closed, fake.opened)
        self.assertEqual(len(fake.opened), 1)

    def test_wait_geometry_returns_or_reports_why_not(self):
        fake = FakeUser32(windows={GAME_HWND: (1280, 720)})
        r = self.ref(fake)
        rec, stop = Recorder(), threading.Event()
        with _quiet():
            region, _, _ = game_window.wait_game_geometry(r, stop, rec)
            self.assertEqual(region['width'], 1280)
            fake.recreate(NEW_HWND, (1280, 720))
            self.assertIsNone(game_window.wait_game_geometry(r, stop, rec))
            fake.quit()
            self.assertIsNone(game_window.wait_game_geometry(r, stop, rec))
        self.assertFalse(stop.is_set())
        self.assertEqual(rec.values('last_error'),
                         [game_window.MSG_WINDOW_CHANGED,
                          game_window.MSG_WINDOW_CLOSED])

    def test_wait_geometry_window_changed_during_minimized_wait(self):
        """The window is recreated while we wait for it to be restored:
        the wait ends with MSG_WINDOW_CHANGED (the minimized notice is
        cleared first) — the new window is not followed."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)}, iconic=[True])
        r = self.ref(fake)
        rec = Recorder()
        swap = threading.Timer(0.1, fake.recreate, (NEW_HWND, (1280, 720)))
        swap.start()
        self.addCleanup(swap.cancel)
        with _quiet():
            self.assertIsNone(game_window.wait_game_geometry(
                r, stop_evt=threading.Event(), status_cb=rec))
        self.assertEqual(rec.values('last_error'),
                         [game_window.MSG_WINDOW_MINIMIZED, '',
                          game_window.MSG_WINDOW_CHANGED])

    def test_process_handle_on_real_win32(self):
        """RG5's mechanism on the real kernel32: the handle opened at run
        start reports the exit — and keeps reporting it — after the
        process is gone (holding it also keeps the PID from reuse)."""
        child = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(0.2)'])
        handle = game_window._open_process(child.pid)
        self.assertIsNotNone(handle)
        self.addCleanup(game_window._close_handle, handle)
        self.assertFalse(game_window._process_exited(handle))
        child.wait(10)
        self.assertTrue(game_window._process_exited(handle))
        self.assertIsNone(game_window._open_process(0))


# --- standalone runner --------------------------------------------------------

class FakeDetector:
    instances = []

    def __init__(self, hwnd, column_centers, hit_line_y, controller,
                 pid=0):
        self.hwnd = hwnd
        self.pid = pid
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
            mock.patch.object(standalone_runner, 'wait_game_geometry',
                              return_value=(moved, 2 / 3, 2 / 3)),
            mock.patch.object(standalone_runner, 'NoteDetector', FakeDetector),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # The run binds to GAME_HWND (owned by GAME_PID); `_run_real_viz`
        # swaps in its own fake.
        stack = win32(FakeUser32(windows={GAME_HWND: (1920, 1080)}))
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)

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

    def test_pause_during_resize_teardown_routes_through_pause(self):
        """RR8 — Pause lands while the resized-away detector is being
        torn down: the replacement must not start (or report 'running')
        until resume."""
        small = {'top': 0, 'left': 0, 'width': 1280, 'height': 720}
        pause = self.pause
        started_while_paused = []

        class PauseOnTeardown(FakeDetector):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                started_while_paused.append(pause.is_set())

            def stop(self):
                super().stop()
                if len(FakeDetector.instances) == 1:
                    pause.set()
                    threading.Timer(0.1, pause.clear).start()

        calls = []

        def viz(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                kwargs['geometry_poll']()
                return standalone_runner.VIZ_EXIT_GEOMETRY
            self.stop.set()
            return standalone_runner.VIZ_EXIT_STOP

        with mock.patch.object(standalone_runner, 'NoteDetector',
                               PauseOnTeardown), \
                mock.patch.object(standalone_runner, 'read_game_geometry',
                                  return_value=(small, 2 / 3, 2 / 3)):
            self._run(viz)
        self.assertEqual(self.rec.values('state'),
                         ['running', 'paused', 'running', 'idle'])
        self.assertEqual(len(FakeDetector.instances), 2)
        self.assertEqual(started_while_paused, [False, False])

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

    def _run_real_viz(self, fake_user32, timeout=3.0, controller=None):
        """run_standalone with the REAL viz loop + GeometryTracker +
        read_game_geometry / GameWindowRef over `fake_user32` (debug off,
        so no cv2 window). A Timer sets Stop after `timeout`, so a
        regression fails instead of hanging. Returns the elapsed time."""
        if controller is None:
            controller = FakeInput()
        sct = mock.MagicMock()
        sct.__enter__.return_value = sct
        safety = threading.Timer(timeout, self.stop.set)
        with win32(fake_user32), \
                mock.patch.object(standalone_runner, 'GEOMETRY_POLL_S',
                                  0.01), \
                mock.patch.object(standalone_runner.mss, 'mss',
                                  return_value=sct), _quiet():
            safety.start()
            try:
                started = time.time()
                standalone_runner.run_standalone(
                    self.stop, self.debug, status_cb=self.rec,
                    pause_evt=self.pause, controller=controller)
                return time.time() - started
            finally:
                safety.cancel()

    def test_game_closed_mid_play_ends_run_with_error(self):
        """RC1 — Genshin closed while Standalone plays: the run ends by
        itself (no Stop), the detector is stopped (keys released) and
        the UI gets an actionable last_error."""
        fake = FakeUser32()
        closer = threading.Timer(0.1, setattr, (fake, 'exists', False))
        closer.start()
        self.addCleanup(closer.cancel)
        elapsed = self._run_real_viz(fake)
        self.assertFalse(self.stop.is_set())
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.rec.values('last_error'),
                         [game_window.MSG_WINDOW_CLOSED])
        self.assertEqual(self.rec.values('state')[-1], 'idle')
        self.assertEqual(len(FakeDetector.instances), 1)
        self.assertTrue(FakeDetector.instances[0].stopped)

    def test_recreated_window_ends_run_as_changed(self):
        """The game recreated its window (fullscreen / resolution switch):
        the run ends by itself with MSG_WINDOW_CHANGED — no detector on
        the new window — and the process handle is released."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        swap = threading.Timer(0.1, fake.recreate, (NEW_HWND, (1280, 720)))
        swap.start()
        self.addCleanup(swap.cancel)
        elapsed = self._run_real_viz(fake)
        self.assertFalse(self.stop.is_set())
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.rec.values('last_error'),
                         [game_window.MSG_WINDOW_CHANGED])
        self.assertEqual(self.rec.values('state')[-1], 'idle')
        self.assertEqual([d.hwnd for d in FakeDetector.instances],
                         [GAME_HWND])
        self.assertTrue(FakeDetector.instances[0].stopped)
        self.assertEqual(len(fake.opened), 1)
        self.assertEqual(fake.closed, fake.opened)

    def test_recycled_foreign_hwnd_gets_no_keys(self):
        """RG4 — our HWND is recycled to another app's window (valid rect,
        foreground) while the game runs on. With the REAL detector (every
        pixel a "note"): not one key reaches it, the run ends with
        MSG_WINDOW_CHANGED, no detector is built on anything else."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        fg = [GAME_HWND]
        ctrl = FakeInput()
        built = []
        marks = {}

        def make_detector(*args, **kwargs):
            built.append((args[0], kwargs.get('pid')))
            return detector.NoteDetector(*args, **kwargs)

        def downs():
            return [c for c in ctrl.snapshot() if c[0] == 'key_down']

        def recycle():
            marks['before'] = len(downs())
            fake.recycle(GAME_HWND)            # same handle, other owner
            fake.exited = False                # the game itself runs on

        user32 = mock.MagicMock()
        user32.GetDC.return_value = 1
        gdi32 = mock.MagicMock()
        gdi32.GetPixel.return_value = 0                # blue 0: note
        timer = threading.Timer(0.15, recycle)
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(standalone_runner, 'NoteDetector',
                               make_detector), \
                mock.patch.object(detector, '_user32', user32), \
                mock.patch.object(detector, '_gdi32', gdi32), \
                mock.patch.object(detector, '_foreground_hwnd',
                                  side_effect=lambda: fg[0]), \
                mock.patch.object(detector, '_window_pid',
                                  side_effect=lambda h: fake.owner(h)
                                  if fake.IsWindow(h) else 0), \
                mock.patch.object(detector, 'FOCUS_POLL_S', 0.01):
            self._run_real_viz(fake, controller=ctrl)
        self.assertFalse(self.stop.is_set())           # ended by itself
        self.assertGreater(marks['before'], 0)         # it played the game
        self.assertEqual(len(downs()), marks['before'])
        self.assertEqual(built, [(GAME_HWND, GAME_PID)])
        self.assertEqual(self.rec.values('last_error'),
                         [game_window.MSG_WINDOW_CHANGED])

    def test_minimized_mid_play_keeps_running(self):
        """RC1 — minimized is not closed: play waits (until Stop here)
        with no error and no detector churn."""
        fake = FakeUser32(iconic=[True] * 100000)
        stopper = threading.Timer(0.3, self.stop.set)
        stopper.start()
        self.addCleanup(stopper.cancel)
        elapsed = self._run_real_viz(fake)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertEqual(self.rec.values('last_error'), [])
        self.assertEqual(len(FakeDetector.instances), 1)

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

    def test_focus_handback_on_creation_with_bit31_preview(self):
        """MR12 — preview HWND via HWND restype, foreground reported as
        the signed c_int of the same window: still recognised."""
        stop = threading.Event()

        def created():
            self.fg[0] = BIT31_SIGNED

        with mock.patch.object(standalone_runner, '_viz_hwnd',
                               return_value=BIT31_HWND):
            self._run_viz(stop, on_create=created,
                          on_frame=lambda n: n >= 2 and stop.set())
        self.assertEqual(self.focus_given[0], GAME_HWND)

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

    def test_closed_game_exits_viz_debug_on_or_off(self):
        """RC1 — the poll keeps running with Debug off (no window) too."""
        for debug_on in (False, True):
            stop, debug = threading.Event(), threading.Event()
            if debug_on:
                debug.set()
            polls = [None, standalone_runner.WINDOW_GONE]
            reason = self._run_viz(
                stop, debug=debug,
                geometry_poll=lambda: polls.pop(0) if polls else None)
            self.assertEqual(reason, standalone_runner.VIZ_EXIT_WINDOW_GONE,
                             debug_on)
            self.assertFalse(stop.is_set())
            if debug_on:
                self.cv2['destroyWindow'].assert_called()

    def test_changed_window_exits_viz(self):
        stop = threading.Event()
        polls = [None, standalone_runner.WINDOW_CHANGED]
        reason = self._run_viz(
            stop, geometry_poll=lambda: polls.pop(0) if polls else None)
        self.assertEqual(reason, standalone_runner.VIZ_EXIT_WINDOW_CHANGED)
        self.assertFalse(stop.is_set())


class GeometryTrackerTests(unittest.TestCase):
    """RR4 — the tracker behind Standalone / CLI mid-play polling, over the
    fake game window (every poll first checks the window is still the
    game's)."""

    def setUp(self):
        self.fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        stack = win32(self.fake)
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)

    def _tracker(self):
        t = standalone_runner.GeometryTracker(
            GAME_HWND, dict(REGION_1080),
            *game_window.scaled_layout(1.0, 1.0))
        self.addCleanup(t.close)
        return t

    def test_unchanged_or_unusable_returns_none(self):
        t = self._tracker()
        for geom in ((dict(REGION_1080), 1.0, 1.0), None):
            with mock.patch.object(standalone_runner, 'read_game_geometry',
                                   return_value=geom):
                self.assertIsNone(t.poll())

    def test_closed_game_reported_minimized_is_not(self):
        """RC1 — the tracker behind Standalone / CLI play tells a closed
        game (end the run) from a minimized one (keep waiting)."""
        t = self._tracker()
        self.fake.iconic = [True]
        self.assertIsNone(t.poll())
        self.fake.quit()
        self.assertEqual(t.poll(), standalone_runner.WINDOW_GONE)
        self.assertEqual(t.current[0], REGION_1080)   # last-known kept

    def test_recreated_window_is_changed_not_followed(self):
        t = self._tracker()
        self.fake.recreate(NEW_HWND, (1280, 720))
        self.assertEqual(t.poll(), standalone_runner.WINDOW_CHANGED)
        self.assertEqual(t.hwnd, GAME_HWND)
        self.assertEqual(t.current[0], REGION_1080)

    def test_recycled_foreign_hwnd_is_changed_despite_valid_rect(self):
        """RG4 — its (different, valid) rect is never used."""
        t = self._tracker()
        self.fake.recycle(GAME_HWND)
        self.fake.exited = False
        self.fake.windows[GAME_HWND] = (1280, 720)
        self.assertEqual(t.poll(), standalone_runner.WINDOW_CHANGED)
        self.assertEqual(t.current[0], REGION_1080)

    def test_close_releases_the_process_handle(self):
        t = self._tracker()
        self.assertEqual(t.pid, GAME_PID)
        t.close()
        self.assertEqual(self.fake.closed, self.fake.opened)
        self.assertEqual(len(self.fake.opened), 1)

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
        # The game's windows as Win32 reports them: the REAL
        # follow / resolve / read_game_geometry run over this fake.
        # Tests close the game (`fake.windows = {}`) or have it recreate
        # its window (`fake.recreate(...)`).
        self.fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})
        stack = win32(self.fake)
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        patches = [
            mock.patch.object(album, 'find_game_window',
                              return_value=GAME_HWND),
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

    def test_setup_failure_releases_the_process_handle(self):
        with mock.patch.object(album.AlbumRunner, '_load_tpl',
                               staticmethod(mock.Mock(
                                   side_effect=RuntimeError('no tpl')))):
            with self.assertRaises(RuntimeError):
                self.make_runner()
        self.assertEqual(len(self.fake.opened), 1)
        self.assertEqual(self.fake.closed, self.fake.opened)

    def test_close_releases_the_process_handle(self):
        r = self.make_runner()
        self.assertEqual(r.game_pid, GAME_PID)
        with _quiet():
            r.close()
        self.assertEqual(self.fake.closed, self.fake.opened)

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
        self.assertEqual(self.rec.values('last_error'), [])


class AlbumWindowFixture(AlbumRunnerBase):
    """Runner over the fake game windows with the rhythm phase's detector
    / viz stubbed. Every runner arms a Stop timer so a regression fails
    instead of hanging. No tests."""

    def setUp(self):
        super().setUp()
        FakeDetector.instances = []

        def viz(*args, stop_evt=None, **kwargs):
            stop_evt.wait(5.0)

        for name, value in (('NoteDetector', FakeDetector),
                            ('GEOMETRY_POLL_S', 0.02),
                            ('run_visualization', viz)):
            p = mock.patch.object(song_player, name, value)
            p.start()
            self.addCleanup(p.stop)

    def close_game(self):
        self.fake.windows = {}           # no game window left at all

    def runner(self, timeout=3.0):
        r = self.make_runner()
        r.watcher.grab = lambda: np.zeros((1080, 1920, 3), dtype=np.uint8)
        r.song_player.dump_frame_fn = lambda tag: None
        safety = threading.Timer(timeout, r.stop_evt.set)
        safety.start()
        self.addCleanup(safety.cancel)
        return r

    def closed_error_only(self):
        self.assertEqual(self.rec.values('last_error'),
                         [game_window.MSG_WINDOW_CLOSED])


class AlbumGameClosed(AlbumWindowFixture):
    """RC1 — Genshin closed mid-album: the run ends promptly as FAILED
    with MSG_WINDOW_CLOSED, instead of looping until Stop (rhythm phase)
    or reporting a misleading click / navigation error after a long
    timeout."""

    def test_refresh_tells_closed_from_minimized(self):
        r = self.runner()
        with mock.patch.object(album, 'read_game_geometry',
                               return_value=None), _quiet():
            self.assertTrue(r._refresh_geometry(block=False))   # minimized
            self.assertEqual(self.rec.values('last_error'), [])
            self.close_game()
            self.assertFalse(r._refresh_geometry(block=False))
            self.assertFalse(r._refresh_geometry(block=True))   # sticky
        self.closed_error_only()

    def test_closed_mid_song_fails_run_promptly(self):
        """The reported bug: rhythm phase kept polling a closed game."""
        r = self.runner()
        r._on_album_page = lambda frame=None: True
        r._is_canorus = lambda frame=None: False
        sp = r.song_player
        sp._advance_to_song = lambda difficulty, state: song_player.SCREEN_RHYTHM
        sp._end_watcher = lambda local_stop: local_stop.wait(5.0)
        closer = threading.Timer(0.1, self.close_game)
        closer.start()
        self.addCleanup(closer.cancel)
        with mock.patch.object(album, 'read_game_geometry',
                               return_value=None), _quiet() as out:
            started = time.time()
            result = r.run(songs_count=3)
            elapsed = time.time() - started
        self.assertEqual(result, album.RESULT_FAILED)
        self.assertFalse(r.stop_evt.is_set())
        self.assertLess(elapsed, 2.0)
        self.closed_error_only()
        self.assertNotIn('Album complete', out.getvalue())
        self.assertEqual(self.rec.values('album_progress')[-1]['results'],
                         ['failed'])
        self.assertEqual(len(FakeDetector.instances), 1)
        self.assertTrue(FakeDetector.instances[0].stopped)

    def test_closed_while_waiting_for_album_page(self):
        r = self.runner()
        r._on_album_page = lambda frame=None: False
        r._find_select_song = lambda frame=None: None
        closer = threading.Timer(0.2, self.close_game)
        closer.start()
        self.addCleanup(closer.cancel)
        with _quiet():
            started = time.time()
            result = r._after_song()
            elapsed = time.time() - started
        self.assertEqual(result, album.RESULT_FAILED)
        self.assertLess(elapsed, 1.5)
        self.closed_error_only()

    def test_next_song_click_refused_by_closed_game(self):
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(return_value=False)
        self.close_game()
        with _quiet():
            self.assertFalse(r._next_song())
        self.assertEqual(r.watcher.click_ref.call_count, 1)   # no retry
        self.closed_error_only()

    def test_intro_click_refused_by_closed_game(self):
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(return_value=False)
        self.close_game()
        with mock.patch.object(album, 'read_game_geometry',
                               return_value=None), _quiet():
            self.assertFalse(r.song_player.play('legendary'))
        self.closed_error_only()

    def test_closed_during_intro_wait_fails_fast(self):
        """Go Perform clicked, then the game closed: the watcher's 5 s
        wait for the difficulty screen gives up at once."""
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(side_effect=lambda *a: (
            self.close_game() or True))
        r.watcher.match = lambda *a, **k: None
        with mock.patch.object(album, 'read_game_geometry',
                               return_value=None), _quiet():
            started = time.time()
            self.assertFalse(r.song_player.play('legendary'))
            elapsed = time.time() - started
        self.assertLess(elapsed, 1.0)
        self.closed_error_only()

    def test_closed_while_paused_between_songs(self):
        r = self.runner()
        r.pause_evt.set()
        closer = threading.Timer(0.1, self.close_game)
        closer.start()
        self.addCleanup(closer.cancel)
        with mock.patch.object(album, 'WINDOW_CHECK_S', 0.02), _quiet():
            started = time.time()
            self.assertFalse(r._song_boundary())
            elapsed = time.time() - started
        self.assertFalse(r.stop_evt.is_set())
        self.assertLess(elapsed, 1.5)
        self.closed_error_only()
        self.assertEqual(r._halt_result(), album.RESULT_FAILED)

    def test_closed_during_final_wheel_settle_is_not_completed(self):
        """W1 — the 12th (wheel-reset) click landed, then the game closed
        during its 0.8 s settle: FAILED + MSG_WINDOW_CLOSED, not
        "Album complete"."""
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(return_value=True)
        r.watcher.stop_wait = lambda secs: self.close_game() or False
        with _quiet():
            self.assertEqual(r._finish_wheel(ALBUM_SONG_COUNT),
                             album.RESULT_FAILED)
        self.closed_error_only()

    def test_stop_wins_over_closure_during_wheel_settle(self):
        """W1 — Stop keeps precedence: stopped, no error."""
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(return_value=True)
        r.watcher.stop_wait = lambda secs: (self.close_game()
                                            or r.stop_evt.set() or True)
        with _quiet():
            self.assertEqual(r._finish_wheel(ALBUM_SONG_COUNT),
                             album.RESULT_STOPPED)
        self.assertEqual(self.rec.values('last_error'), [])


class AlbumWindowChanged(AlbumWindowFixture):
    """The run's window stops being the game's while the game runs —
    recreated on a fullscreen / resolution switch, or its HWND recycled to
    another app: the run ends with MSG_WINDOW_CHANGED and never follows
    another window; the game exiting is MSG_WINDOW_CLOSED (RG4-RG8)."""

    def recreate(self, size=(1280, 720)):
        self.fake.recreate(NEW_HWND, size)

    def changed_error_only(self):
        self.assertEqual(self.rec.values('last_error'),
                         [game_window.MSG_WINDOW_CHANGED])

    def assert_not_followed(self, r):
        self.assertEqual((r.hwnd, r.song_player.hwnd),
                         (GAME_HWND, GAME_HWND))
        self.assertEqual(r.watcher.region, REGION_1080)

    def test_navigation_check_reports_changed_once(self):
        r = self.runner()
        self.recreate()
        with _quiet():
            self.assertTrue(r._window_gone())
            self.assertTrue(r._window_gone())         # sticky
        self.changed_error_only()
        self.assert_not_followed(r)

    def test_blocking_refresh_ends_on_changed_window(self):
        r = self.runner()
        self.recreate(size=(1920, 1080))
        with _quiet():
            self.assertFalse(r._refresh_geometry(block=True))
            self.assertFalse(r._refresh_geometry(block=False))
        self.changed_error_only()
        self.assert_not_followed(r)

    def test_mid_song_window_change_ends_song(self):
        """Recreated mid-song: the song ends at once (detector stopped —
        keys released) with MSG_WINDOW_CHANGED; nothing on the new
        window."""
        r = self.runner()
        sp = r.song_player
        sp._end_watcher = lambda local_stop: local_stop.wait(5.0)
        timer = threading.Timer(0.1, self.recreate)
        timer.start()
        self.addCleanup(timer.cancel)
        with _quiet():
            self.assertFalse(sp._rhythm_phase())
        self.assertFalse(r.stop_evt.is_set())
        self.assertEqual([d.hwnd for d in FakeDetector.instances],
                         [GAME_HWND])
        self.assertEqual(FakeDetector.instances[0].pid, GAME_PID)
        self.assertTrue(FakeDetector.instances[0].stopped)
        self.changed_error_only()

    def test_intro_click_on_changed_window_reports_changed(self):
        """RG6 — not a generic "couldn't click Go Perform" failure."""
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(
            side_effect=lambda *a: self.recreate() or False)
        with _quiet():
            self.assertFalse(r.song_player.play('legendary'))
        self.changed_error_only()

    def test_intro_wait_ends_on_changed_window(self):
        """RG6 — the 5 s wait for the difficulty screen ends at once."""
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(
            side_effect=lambda *a: self.recreate() or True)
        r.watcher.match = lambda *a, **k: None
        with _quiet():
            started = time.time()
            self.assertFalse(r.song_player.play('legendary'))
            elapsed = time.time() - started
        self.assertLess(elapsed, 1.0)
        self.changed_error_only()

    def test_click_refused_on_recycled_foreign_hwnd(self):
        """RG4 — our HWND recycled to another app (valid rect, same
        place): no mouse-down, from the runner's thread or the end
        watcher's — and no handle moves."""
        for name in ('CLICK_HOVER_S', 'CLICK_HOLD_S', 'CLICK_SETTLE_S'):
            p = mock.patch.object(screen_watcher, name, 0)
            p.start()
            self.addCleanup(p.stop)
        r = self.runner()
        self.fake.recycle(GAME_HWND)
        self.fake.exited = False
        results = []
        with _quiet():
            results.append(r.watcher.click_ref(500, 500))
            t = threading.Thread(target=lambda: results.append(
                r.watcher.click_local(10, 10,
                                      generation=r.watcher.generation)))
            t.start()
            t.join(2.0)
        self.assertEqual(results, [False, False])
        self.assertNotIn('mouse_down',
                         [c[0] for c in r.controller.snapshot()])
        self.assert_not_followed(r)

    def test_next_song_click_on_changed_window(self):
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(
            side_effect=lambda *a: self.recreate() or False)
        with _quiet():
            self.assertFalse(r._next_song())
        self.assertEqual(r.watcher.click_ref.call_count, 1)   # no retry
        self.changed_error_only()

    def _final_settle(self, during_settle):
        r = self.runner()
        r.watcher.click_ref = mock.MagicMock(return_value=True)
        r.watcher.stop_wait = lambda secs: during_settle() or False
        with _quiet():
            return r._finish_wheel(ALBUM_SONG_COUNT)

    def test_final_settle_alive_completes(self):
        """W1 / RG8 — what happened during the 12th click's settle
        decides the outcome."""
        self.assertEqual(self._final_settle(lambda: None),
                         album.RESULT_COMPLETED)
        self.assertEqual(self.rec.values('last_error'), [])

    def test_final_settle_window_changed_fails(self):
        self.assertEqual(self._final_settle(self.recreate),
                         album.RESULT_FAILED)
        self.changed_error_only()

    def test_final_settle_unknown_liveness_closed_fails(self):
        """RG8 — no process handle (OpenProcess failed), the window died
        and no game window shows up: CLOSED -> failed, never completed."""
        self.fake.open_fails = True
        self.assertEqual(self._final_settle(self.fake.destroy),
                         album.RESULT_FAILED)
        self.closed_error_only()

    def test_final_settle_unknown_liveness_changed_fails(self):
        self.fake.open_fails = True
        self.assertEqual(self._final_settle(self.recreate),
                         album.RESULT_FAILED)
        self.changed_error_only()


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

    def test_game_closed_while_paused_in_intro(self):
        """RC1 — paused before a click, then the game closed: the gate
        ends the song (no click, no waiting for a resume that can't
        help). The album runner reported the reason."""
        self.addCleanup(self.stop.set)
        self.pause.set()
        self.geometry_fn.side_effect = lambda block: False
        result = []
        with mock.patch.object(song_player, 'GEOMETRY_POLL_S', 0.02):
            t = threading.Thread(target=lambda: result.append(self.play()))
            t.start()
            t.join(2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(self.watcher.clicks, [])
        self.assertFalse(self.stop.is_set())

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

    def _closed_after_start(self, pause_first):
        """RC1 — the game closes (non-blocking refresh says "end the
        run") while the song plays or is paused: return False promptly
        with the detector stopped (keys released), no Stop needed."""
        self.addCleanup(self.stop.set)          # never leak the thread
        closed = threading.Event()
        self.geometry_fn.side_effect = lambda block: not closed.is_set()
        with _quiet():
            t, out = self._start()
            self.assertTrue(_wait_until(lambda: FakeDetector.instances))
            if pause_first:
                self.pause.set()
                time.sleep(0.1)
            closed.set()
            t.join(2.0)
        self.assertFalse(t.is_alive())
        self.assertFalse(self.stop.is_set())
        self.assertEqual(out, [False])
        self.assertEqual(len(FakeDetector.instances), 1)
        self.assertTrue(FakeDetector.instances[0].stopped)

    def test_game_closed_mid_song_returns_false(self):
        self._closed_after_start(pause_first=False)

    def test_game_closed_while_paused_mid_song_returns_false(self):
        self._closed_after_start(pause_first=True)


# --- CLI entrypoints -------------------------------------------------------------

class AlbumCliBackend(unittest.TestCase):
    """RC3 — `python pc_client/album.py` honors INPUT_BACKEND_DEFAULT (and
    --backend) like main.py, instead of always opening the Arduino."""

    def _main(self, extra_argv, backend_error=None):
        import ui_core
        made = []
        ctrl = FakeInput()

        def make(name, cancel_evt=None):
            made.append(name)
            if backend_error is not None:
                raise backend_error
            return ctrl

        runner = mock.MagicMock()
        runner.run.return_value = album.RESULT_COMPLETED
        runner_cls = mock.MagicMock(return_value=runner)
        argv = ['album.py', '--difficulty', 'hard', '--replay-canorus',
                '--songs', '2'] + extra_argv
        code = None
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(ui_core, 'make_input_backend',
                                  side_effect=make), \
                mock.patch.object(album, 'AlbumRunner', runner_cls), \
                mock.patch.object(album, 'ctypes'), _quiet() as out:
            try:
                album.main()
            except SystemExit as e:
                code = e.code
        return made, ctrl, runner_cls, code, out.getvalue()

    def test_default_backend_comes_from_config(self):
        from config import INPUT_BACKEND_DEFAULT
        made, ctrl, runner_cls, code, _ = self._main([])
        self.assertEqual(made, [INPUT_BACKEND_DEFAULT])
        self.assertIs(runner_cls.call_args.kwargs['controller'], ctrl)
        self.assertTrue(ctrl.closed)          # the CLI owns the backend
        self.assertIsNone(code)

    def test_backend_flag_overrides_default(self):
        for name in ('software', 'arduino'):
            made, ctrl, runner_cls, code, _ = self._main(['--backend', name])
            self.assertEqual(made, [name])
            self.assertIs(runner_cls.call_args.kwargs['controller'], ctrl)
            self.assertIsNone(code)

    def test_arduino_failure_exits_1_with_message(self):
        msg = "No Arduino found — plug in the Leonardo"
        made, ctrl, runner_cls, code, out = self._main(
            ['--backend', 'arduino'],
            backend_error=standalone_runner.ArduinoConnectionError(msg))
        self.assertEqual(code, 1)
        self.assertIn(f"ERROR: {msg}", out)
        runner_cls.assert_not_called()


class CalibrateDpi(unittest.TestCase):
    """RC4 — calibrate.py must be DPI-aware before reading geometry."""

    def test_dpi_aware_before_any_geometry_read(self):
        import calibrate
        order = []
        with mock.patch.object(calibrate, 'set_dpi_aware',
                               side_effect=lambda: order.append('dpi')), \
                mock.patch.object(calibrate, 'auto_detect',
                                  side_effect=lambda *a, **k:
                                  order.append('detect')), _quiet():
            with self.assertRaises(SystemExit):
                calibrate.main()         # auto_detect -> None -> exit 1
        self.assertEqual(order, ['dpi', 'detect'])


class MainCliGameClosed(unittest.TestCase):
    """RC1 — `python pc_client/main.py` exits cleanly (keys released,
    backend closed, timer restored, exit 1 + message) when Genshin is
    closed mid-run, headless or with the debug viz."""

    def _run(self, debug_mode, fake=None, on_sleep=None):
        import main as cli
        FakeDetector.instances = []
        ctrl = FakeInput()
        restore = mock.MagicMock()
        sleeps = []
        if fake is None:
            fake = FakeUser32(exists=False)

        def sleep(secs):                  # bounds a regression: Ctrl+C
            sleeps.append(secs)
            if on_sleep is not None:
                on_sleep(len(sleeps))
            if len(sleeps) > 50:
                raise KeyboardInterrupt

        patches = dict(
            DEBUG_MODE=debug_mode,
            set_dpi_aware=mock.MagicMock(),
            boost_timer=mock.MagicMock(return_value=True),
            restore_timer=restore,
            auto_detect=mock.MagicMock(return_value=(
                dict(REGION_1080), [417, 628, 844, 1061, 1277, 1493], 921)),
            find_game_window=mock.MagicMock(return_value=GAME_HWND),
            make_input_backend=mock.MagicMock(return_value=ctrl),
            NoteDetector=FakeDetector,
            ctypes=mock.MagicMock(),
            cv2=mock.MagicMock(),
            time=mock.MagicMock(sleep=sleep),
            run_visualization=mock.MagicMock(
                side_effect=lambda *a, **k: k['geometry_poll']() and (
                    standalone_runner.VIZ_EXIT_WINDOW_GONE)))
        code = None
        with mock.patch.multiple(cli, **patches), win32(fake), \
                _quiet() as out:
            try:
                cli.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), ctrl, restore

    def test_game_closed_exits_cleanly(self):
        for debug_mode in (False, True):
            code, out, ctrl, restore = self._run(debug_mode)
            self.assertEqual(code, 1, debug_mode)
            self.assertIn(game_window.MSG_WINDOW_CLOSED, out)
            self.assertIn('Shutdown complete', out)
            self.assertTrue(all(d.stopped for d in FakeDetector.instances))
            self.assertTrue(ctrl.closed)
            restore.assert_called_once()

    def test_headless_exits_on_changed_window(self):
        """The game recreated its window: the CLI shuts down cleanly with
        MSG_WINDOW_CHANGED (exit 1) — no detector on the new window — and
        releases the process handle."""
        fake = FakeUser32(windows={GAME_HWND: (1920, 1080)})

        def on_sleep(n):
            if n == 1:
                fake.recreate(NEW_HWND, (1280, 720))

        code, out, ctrl, _ = self._run(False, fake=fake, on_sleep=on_sleep)
        self.assertEqual(code, 1)
        self.assertIn(game_window.MSG_WINDOW_CHANGED, out)
        self.assertEqual([d.hwnd for d in FakeDetector.instances],
                         [GAME_HWND])
        self.assertTrue(FakeDetector.instances[0].stopped)
        self.assertEqual(fake.closed, fake.opened)


if __name__ == '__main__':
    unittest.main()
