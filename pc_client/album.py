# pc_client/album.py
"""
Auto-album runner. Iterates one Genshin Impact "Repertoire of Myriad
Melodies" album page (Mondstadt / Liyue / Inazuma / ...), playing each
unfinished song with the rhythm bot and advancing to the next.

Mirrors BGI's AutoAlbumTask.cs flow:
  - verify on an album page (UiLeftTopAlbumIcon match)
  - for each of 13 song slots:
      - skip if already done (per-difficulty canorus match, or all-rewards
        match if ALBUM_USE_CANORUS_CHECK = False)
      - else: click white Confirm → click difficulty → click white Confirm
        → run rhythm detector while a 5s watcher polls for the
        return-to-list button → click it → wait for album page → next-arrow

Run from an album page (NOT the All-Songs list):
    python pc_client/album.py
"""
import ctypes
import sys
import threading
import time
from pathlib import Path

import cv2
import mouse
import mss
import numpy as np

from config import (ALBUM_DIFFICULTY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_END_POLL_S, ALBUM_MATCH_THRESHOLD,
                    ALBUM_NEXT_ARROW_XY, ALBUM_USE_CANORUS_CHECK,
                    ALBUM_WHITE_CONFIRM_THRESHOLD,
                    KEY_POLL_DELAY_S, REF_COLUMN_X, REF_HIT_LINE_Y,
                    Y_SAMPLE_OFFSETS)
from controller import ArduinoHIDController
from detector import NoteDetector
from main import find_game_window, get_game_geometry


# 1080p reference ROIs from BGI AutoMusicAssets.cs.
ROI_ALBUM_ICON = (0,   0,   150, 120)   # top-left
ROI_COMPLETE   = (900, 320, 100, 80)    # all-rewards indicator
ROI_CANORUS = {                         # per-difficulty canorus badge
    'normal': (450, 430, 200, 60),
    'hard':   (450, 520, 200, 60),
    'master': (450, 610, 200, 60),
    'pro':    (450, 690, 200, 60),
}


class AlbumRunner:
    def __init__(self):
        self.hwnd = find_game_window()
        if not self.hwnd:
            print("ERROR: game window not found")
            sys.exit(1)
        self.region, self.scale_x, self.scale_y = get_game_geometry(self.hwnd)
        # Game enforces 16:9, so scale_x ~= scale_y. Use scale_x for
        # template resampling (BGI's AssetScale is a single scalar).
        self.scale = self.scale_x
        self.sct = mss.mss()

        assets_dir = Path(__file__).parent / 'assets' / '1920x1080'
        self.tpl = {
            'icon':     self._load_tpl(assets_dir / 'ui_left_top_album_icon.png'),
            'list':     self._load_tpl(assets_dir / 'btn_list.png'),
            'complete': self._load_tpl(assets_dir / 'album_music_complate.png'),
            'canorus':  self._load_tpl(assets_dir / 'music_canorus.png'),
            'confirm':  self._load_tpl(assets_dir / 'btn_white_confirm.png'),
        }
        if abs(self.scale - 1.0) > 0.01:
            for k, t in self.tpl.items():
                nw = max(1, int(round(t.shape[1] * self.scale)))
                nh = max(1, int(round(t.shape[0] * self.scale)))
                self.tpl[k] = cv2.resize(t, (nw, nh),
                                         interpolation=cv2.INTER_LINEAR)

        self.controller = ArduinoHIDController()

        print(f"Album: {self.region['width']}x{self.region['height']} @ "
              f"({self.region['left']},{self.region['top']}), "
              f"scale {self.scale_x:.3f}x{self.scale_y:.3f}, "
              f"difficulty={ALBUM_DIFFICULTY}")

    @staticmethod
    def _load_tpl(path):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"failed to load template: {path}")
        return img

    # --- screen / coord helpers --------------------------------------------

    def _grab(self):
        shot = self.sct.grab(self.region)
        return np.ascontiguousarray(np.array(shot)[:, :, :3])

    def _ref_to_screen(self, x, y):
        return (int(x * self.scale_x) + self.region['left'],
                int(y * self.scale_y) + self.region['top'])

    def _ref_rect_to_local(self, ref_x, ref_y, ref_w, ref_h):
        return (int(ref_x * self.scale_x), int(ref_y * self.scale_y),
                int(ref_w * self.scale_x), int(ref_h * self.scale_y))

    def _frac_rect_to_local(self, fx, fy, fw, fh):
        # BGI's CutRightTop / CutRightBottom are fractions of the capture rect.
        w, h = self.region['width'], self.region['height']
        return (int(w * fx), int(h * fy), int(w * fw), int(h * fh))

    def _click_screen(self, sx, sy):
        mouse.move(sx, sy, absolute=True, duration=0)
        time.sleep(0.05)
        mouse.click('left')

    def _click_ref(self, ref_x, ref_y):
        self._click_screen(*self._ref_to_screen(ref_x, ref_y))

    def _click_local(self, lx, ly):
        self._click_screen(lx + self.region['left'],
                           ly + self.region['top'])

    # --- template matching --------------------------------------------------

    def _match(self, frame, tpl, roi=None, threshold=None):
        """Best-match within roi (or full frame). Returns (score, (cx,cy)) or None.
        cx,cy are in capture-region (local) coords."""
        if threshold is None:
            threshold = ALBUM_MATCH_THRESHOLD
        if roi is None:
            haystack = frame
            ox, oy = 0, 0
        else:
            x, y, w, h = roi
            x = max(0, x)
            y = max(0, y)
            x2 = min(frame.shape[1], x + w)
            y2 = min(frame.shape[0], y + h)
            haystack = frame[y:y2, x:x2]
            ox, oy = x, y
        if (haystack.shape[0] < tpl.shape[0]
                or haystack.shape[1] < tpl.shape[1]):
            return None
        result = cv2.matchTemplate(haystack, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return None
        cx = ox + max_loc[0] + tpl.shape[1] // 2
        cy = oy + max_loc[1] + tpl.shape[0] // 2
        return float(max_val), (cx, cy)

    # --- state checks -------------------------------------------------------

    def _on_album_page(self, frame=None):
        if frame is None:
            frame = self._grab()
        roi = self._ref_rect_to_local(*ROI_ALBUM_ICON)
        return self._match(frame, self.tpl['icon'], roi=roi) is not None

    def _is_canorus(self, frame=None):
        if frame is None:
            frame = self._grab()
        roi = self._ref_rect_to_local(*ROI_CANORUS[ALBUM_DIFFICULTY])
        return self._match(frame, self.tpl['canorus'], roi=roi) is not None

    def _is_complete(self, frame=None):
        if frame is None:
            frame = self._grab()
        roi = self._ref_rect_to_local(*ROI_COMPLETE)
        return self._match(frame, self.tpl['complete'], roi=roi) is not None

    def _find_btn_list(self, frame=None):
        if frame is None:
            frame = self._grab()
        # BGI: CutRightBottom(0.4, 0.2) = right 40% × bottom 20%
        roi = self._frac_rect_to_local(0.6, 0.8, 0.4, 0.2)
        return self._match(frame, self.tpl['list'], roi=roi)

    def _click_white_confirm(self):
        """Find + click the white Confirm button. BGI's
        Bv.ClickWhiteConfirmButton: full-screen template match, sleep 500ms,
        click centroid. Returns True iff found."""
        frame = self._grab()
        m = self._match(frame, self.tpl['confirm'],
                        threshold=ALBUM_WHITE_CONFIRM_THRESHOLD)
        if m is None:
            return False
        _, (cx, cy) = m
        time.sleep(0.5)
        self._click_local(cx, cy)
        return True

    # --- per-song flow ------------------------------------------------------

    def _next_song(self):
        self._click_ref(*ALBUM_NEXT_ARROW_XY)
        time.sleep(0.8)

    def _wait_for_album_page(self, timeout=60.0):
        end = time.time() + timeout
        while time.time() < end:
            if self._on_album_page():
                return True
            time.sleep(0.5)
        return False

    def _play_song(self):
        # Open song detail.
        if not self._click_white_confirm():
            print("  could not find white Confirm button (entry)")
            return False
        time.sleep(0.8)

        # Pick difficulty.
        dx, dy = ALBUM_DIFFICULTY_COORDS[ALBUM_DIFFICULTY]
        self._click_ref(dx, dy)
        time.sleep(0.2)

        # Confirm + start.
        if not self._click_white_confirm():
            print("  could not find white Confirm button (start)")
            return False
        time.sleep(0.5)

        # Fire up rhythm detector. Watcher signals end-of-song by clicking
        # the return-to-list button when it becomes visible.
        column_centers = [int(x * self.scale_x) for x in REF_COLUMN_X]
        hit_line_y = int(REF_HIT_LINE_Y * self.scale_y)
        detector = NoteDetector(self.hwnd, column_centers, hit_line_y,
                                self.controller)
        detector.start()

        stop_evt = threading.Event()
        watcher = threading.Thread(target=self._end_watcher,
                                   args=(stop_evt,), daemon=True)
        watcher.start()
        try:
            stop_evt.wait()
        finally:
            detector.stop()

        time.sleep(2.0)
        return True

    def _end_watcher(self, stop_evt):
        # First check after one poll interval (mirrors BGI: Delay(5000) then check).
        while not stop_evt.is_set():
            if stop_evt.wait(ALBUM_END_POLL_S):
                return
            res = self._find_btn_list()
            if res is not None:
                _, (cx, cy) = res
                self._click_local(cx, cy)
                stop_evt.set()
                return

    # --- main loop ----------------------------------------------------------

    def run(self):
        if not self._on_album_page():
            print("ERROR: not on an album page. Open Mondstadt/Liyue/etc. "
                  "album first (NOT the All Songs list).")
            return

        check_name = 'canorus' if ALBUM_USE_CANORUS_CHECK else 'complete'
        print(f"Per-key {KEY_POLL_DELAY_S * 1000:.0f}ms, strip {Y_SAMPLE_OFFSETS}")
        print(f"Album loop: 13 songs, difficulty={ALBUM_DIFFICULTY}, "
              f"skip-check={check_name}")

        for i in range(13):
            print(f"\n--- Song {i+1}/13 ---")
            frame = self._grab()
            already_done = (self._is_canorus(frame) if ALBUM_USE_CANORUS_CHECK
                            else self._is_complete(frame))
            if already_done:
                print("  skip — already done")
                self._next_song()
                continue

            print("  playing")
            ok = self._play_song()
            if not ok:
                print(f"  song {i+1} aborted; advancing anyway")

            if not self._wait_for_album_page(timeout=60.0):
                print("  WARN: never returned to album page; stopping")
                return

            self._next_song()

        print("\nAlbum complete")

    def close(self):
        try:
            self.controller.close()
        except Exception:
            pass


def main():
    print("Initializing album runner")

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        print(f"[warn] SetProcessDPIAware: {e}")

    timer_boosted = False
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        timer_boosted = True
    except Exception as e:
        print(f"[warn] timeBeginPeriod: {e}")

    runner = AlbumRunner()
    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nCtrl+C")
    finally:
        runner.close()
        if timer_boosted:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        print("Shutdown complete")


if __name__ == '__main__':
    main()
