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

import ctypes
import ctypes.wintypes
import threading
import time

from config import KEYS, PIXEL_THRESHOLD, KEY_POLL_DELAY_S, Y_SAMPLE_OFFSETS

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

_user32.GetDC.argtypes = [ctypes.wintypes.HWND]
_user32.GetDC.restype = ctypes.wintypes.HDC
_user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_gdi32.GetPixel.argtypes = [ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.GetPixel.restype = ctypes.wintypes.DWORD


def _blue(colorref):
    # COLORREF layout: 0x00BBGGRR
    return (colorref >> 16) & 0xFF


class NoteDetector:
    def __init__(self, hwnd, column_centers, hit_line_y, controller):
        """
        Args:
            hwnd: HWND of the game window (used for GetDC).
            column_centers: list of 6 X-coords in client-area coordinates.
            hit_line_y: Y-coord of hit line in client-area coordinates.
            controller: ArduinoHIDController instance — key_down/key_up
                are called from worker threads (the controller already
                serializes serial writes via its internal write lock).
        """
        self.hwnd = hwnd
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

    def start(self):
        self._stop.clear()
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
        self._stop.set()
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

            hdc = _user32.GetDC(hwnd)
            try:
                present = self._all_dark(hdc, cx)
            finally:
                _user32.ReleaseDC(hwnd, hdc)

            if not present:
                continue

            self.controller.key_down(key)
            self.pressed[key] = True
            print(f"[{time.time():.3f}] {key.upper()} DOWN")

            while not self._stop.is_set():
                time.sleep(delay)
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
