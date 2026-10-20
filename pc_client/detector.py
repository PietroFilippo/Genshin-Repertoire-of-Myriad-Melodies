# pc_client/detector.py
#
# Pixel-polling note detector.
#
# The original approach was a complex computer vision engine that converted the screen
# to HSV and used contour shape analysis (circularity, aspect ratio, density) to
# differentiate single taps, double taps, and holds. This was necessary because the 
# game's default (water/blue) theme is highly translucent and visually noisy. When notes
# are hit in the default theme, massive yellow glowing explosions obscure incoming notes,
# leading to complex edge cases (e.g., dropped holds, phantom taps from glow).
#
# By equipping the Hu Tao theme (costs 600 in-game currency), it bypasses
# the need for shape recognition entirely. The Hu Tao theme has a very specific visual 
# property: the hit-line background is a bright, solid color with an extremely high 
# Blue channel value (~230+). 
# 
# When ANY note (tap or hold) passes over the hit line in this theme, it is opaque and 
# completely blocks the background, causing the Blue channel to instantly drop below 200.
# Because of this, it no longer cares *what* shape the note is. It just asks: "Is the 
# background currently being blocked?"
#
# One simple rule handles all note types natively:
#   - pixel_blue < threshold  ->  background blocked (note present) -> KEY_DOWN
#   - pixel_blue >= threshold ->  background visible (note passed)  -> KEY_UP
#
# Taps produce a brief dark flash (quick DOWN/UP). Holds sustain the dark
# pixel (sustained DOWN). Consecutive holds create dark->bright->dark (DOWN->UP->DOWN).
# Over 400 lines of complex shape-detection logic, lockouts, and timers were 
# removed in favor of this simple, robust binary sensor.
#
# Foreground gate: HID/SendInput keystrokes go to whatever window has
# keyboard focus, and GetPixel happily reads a game that's visible on a
# second monitor while another app is focused — so without a gate the bot
# types "asdjkl" into that other app. One extra thread polls
# GetForegroundWindow every FOCUS_POLL_S and flips a plain bool; the six
# per-key 5 ms loops only read that bool (no extra Win32 calls on the hot
# path). While the game isn't foreground, held keys are released and no
# KEY_DOWN is issued. The debug-viz OpenCV window belongs to OUR process;
# standalone_runner makes it non-activating and hands focus back to the
# game if it ever takes the foreground, because while it's focused the
# gate (correctly) pauses input — keystrokes would land in the viz window,
# not the game.

import ctypes
import ctypes.wintypes
import threading
import time

from config import KEYS, PIXEL_THRESHOLD, KEY_POLL_DELAY_S, Y_SAMPLE_OFFSETS
from game_window import same_hwnd

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

_user32.GetDC.argtypes = [ctypes.wintypes.HWND]
_user32.GetDC.restype = ctypes.wintypes.HDC
_user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_gdi32.GetPixel.argtypes = [ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.GetPixel.restype = ctypes.wintypes.DWORD

# Foreground-gate poll interval. Worst-case "keys still flowing into the
# other app" window after an alt-tab is this plus one KEY_POLL_DELAY_S.
FOCUS_POLL_S = 0.05


def _blue(colorref):
    # COLORREF layout: 0x00BBGGRR
    return (colorref >> 16) & 0xFF


# GetForegroundWindow / GetWindowThreadProcessId are deliberately left at
# ctypes' default prototypes: `ctypes.windll.user32` is process-global, and
# ui_core's hotkey gate compares GetForegroundWindow's default c_int result
# against a cached HWND. Changing restype here would change it there too.
# So GetForegroundWindow returns a signed c_int while our hwnd may come from
# an HWND-typed FindWindowW (ui.py binds that restype): compare them only
# through game_window.same_hwnd (low 32 bits), never with a raw `==`.


def _foreground_hwnd():
    return _user32.GetForegroundWindow() or 0


# The most recently started NoteDetector that hasn't been stopped — the
# UI's live lane strip reads it via `active_lanes()`. Plain module global:
# set in start(), cleared in stop() only if it still points at that
# detector, so when detectors are recycled (pause/resume, geometry
# rebuild) and two exist briefly, the newest wins.
_active_detector = None


def active_lanes():
    """Live held state of the six lanes, in KEYS order (a s d j k l), from
    the currently running detector; all False when none is running.

    Called by the UI drain every ~50 ms. Lock-free and Win32-free: one
    global read plus six dict lookups of bools the key threads write — it
    adds nothing to the per-key 5 ms loops."""
    det = _active_detector
    if det is None:
        return [False] * len(KEYS)
    pressed = det.pressed
    return [bool(pressed.get(k, False)) for k in KEYS]


def _window_pid(hwnd):
    """Owning process id of `hwnd`, 0 on failure. Doesn't message the
    window, so it can't block on a busy game."""
    if not hwnd:
        return 0
    pid = ctypes.wintypes.DWORD(0)
    try:
        _user32.GetWindowThreadProcessId(ctypes.wintypes.HWND(hwnd),
                                         ctypes.byref(pid))
    except Exception:
        return 0
    return pid.value


class NoteDetector:
    def __init__(self, hwnd, column_centers, hit_line_y, controller, pid=0):
        """
        Args:
            hwnd: HWND of the game window (used for GetDC and for the
                foreground gate — input pauses while it isn't focused).
            column_centers: list of 6 X-coords in client-area coordinates.
            hit_line_y: Y-coord of hit line in client-area coordinates.
            controller: ArduinoHIDController instance — key_down/key_up
                are called from worker threads (the controller already
                serializes serial writes via its internal write lock).
            pid: the game process the run is anchored to (runners pass
                it); 0 = read it from `hwnd` at start(). The foreground
                gate only accepts windows of this process.
        """
        self.hwnd = hwnd
        self._anchor_pid = pid or 0
        self.column_centers = column_centers
        self.hit_line_y = hit_line_y
        self.controller = controller
        self.threshold = PIXEL_THRESHOLD
        self.poll_delay = KEY_POLL_DELAY_S
        self.y_offsets = list(Y_SAMPLE_OFFSETS)

        self._stop = threading.Event()
        self._threads = []
        # Per-key state - read by debug visualization in main thread.
        self.pressed = {key: False for key in KEYS}
        # Foreground gate — written only by the focus thread, read (plain
        # attribute load, no lock, no Win32) by the six key loops.
        self._game_focused = False
        self._game_pid = 0

    def start(self):
        global _active_detector
        self._stop.clear()
        _active_detector = self
        # Resolve the initial focus state synchronously so the key loops
        # never see a stale default before the focus thread's first tick.
        self._game_pid = self._anchor_pid or _window_pid(self.hwnd)
        focused = self._is_game_foreground()
        self._game_focused = bool(focused)
        if not self._game_focused:
            print("[detector] game not focused — input paused until "
                  "Genshin is the foreground window")
        ft = threading.Thread(target=self._focus_loop, daemon=True,
                              name="detect-focus")
        ft.start()
        self._threads.append(ft)
        for i, key in enumerate(KEYS):
            cx = self.column_centers[i]
            t = threading.Thread(
                target=self._key_loop,
                args=(key, cx),
                daemon=True,
                name=f"detect-{key}",
            )
            t.start()
            self._threads.append(t)

    def stop(self):
        global _active_detector
        self._stop.set()
        # Unregister first so the lane strip goes dark immediately instead
        # of showing stale lanes during the joins. Only if we're still the
        # newest — a replacement may already have registered itself.
        if _active_detector is self:
            _active_detector = None
        for t in self._threads:
            t.join(timeout=0.5)
        self._threads.clear()
        # Safety: release any key still down. Threads in mid-press exit
        # via the `_stop` check inside their inner loop and fire KEY_UP
        # themselves, but the controller-level all-keys-up in close()
        # is the final fallback.
        for key in list(self.pressed.keys()):
            if self.pressed[key]:
                self.controller.key_up(key)
                self.pressed[key] = False

    # --- foreground gate ----------------------------------------------------

    def _is_game_foreground(self):
        """True if Genshin (or another window owned by the game process,
        e.g. an in-game overlay) is foreground, False if some other app
        is, None if there is no foreground window right now (transient,
        mid alt-tab). Runs on the focus thread (and once in start()),
        never on the per-key hot path."""
        fg = _foreground_hwnd()
        if not fg:
            return None
        if same_hwnd(fg, self.hwnd):
            # Recycled-HWND guard: if Genshin closed and Windows handed
            # its handle to another app's window, the owning PID differs.
            # Only a KNOWN mismatch rejects — an unknown PID (lookup
            # failed) keeps the HWND-only answer, so a failed lookup can
            # never lock the real game out (same rule as ui_core's gate).
            if self._game_pid:
                live = _window_pid(fg)
                if live and live != self._game_pid:
                    return False
            return True
        if self._game_pid and _window_pid(fg) == self._game_pid:
            return True
        return False

    def _focus_loop(self):
        while not self._stop.wait(FOCUS_POLL_S):
            try:
                focused = self._is_game_foreground()
            except Exception:
                continue
            if focused is None:
                # No foreground window (alt-tab in flight) — keep the last
                # state rather than flapping a held note on a transient.
                continue
            if focused == self._game_focused:
                continue
            self._game_focused = focused
            if focused:
                print("[detector] game regained focus — resuming input")
            else:
                print("[detector] game lost focus — pausing input")

    def _all_dark(self, hdc, cx):
        for dy in self.y_offsets:
            if _blue(_gdi32.GetPixel(hdc, cx, self.hit_line_y + dy)) >= self.threshold:
                return False
        return True

    def _any_bright(self, hdc, cx):
        for dy in self.y_offsets:
            if _blue(_gdi32.GetPixel(hdc, cx, self.hit_line_y + dy)) >= self.threshold:
                return True
        return False

    def _key_loop(self, key, cx):
        delay = self.poll_delay
        hwnd = self.hwnd
        threshold = self.threshold

        while not self._stop.is_set():
            time.sleep(delay)

            # Foreground gate: never press while another app has focus.
            if not self._game_focused:
                continue

            hdc = _user32.GetDC(hwnd)
            try:
                present = self._all_dark(hdc, cx)
            finally:
                _user32.ReleaseDC(hwnd, hdc)

            if not present or not self._game_focused:
                continue

            self.controller.key_down(key)
            self.pressed[key] = True
            print(f"[{time.time():.3f}] {key.upper()} DOWN")

            while not self._stop.is_set():
                time.sleep(delay)
                # Focus lost mid-hold — release now (key_up below) so the
                # held key doesn't leak into whatever app took focus.
                if not self._game_focused:
                    break
                hdc = _user32.GetDC(hwnd)
                try:
                    released = self._any_bright(hdc, cx)
                finally:
                    _user32.ReleaseDC(hwnd, hdc)
                if released:
                    break

            self.controller.key_up(key)
            self.pressed[key] = False
            print(f"[{time.time():.3f}] {key.upper()} UP")
