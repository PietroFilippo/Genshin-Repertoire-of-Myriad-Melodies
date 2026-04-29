# pc_client/album.py
"""
Auto-album runner. Plays one Genshin Impact "Repertoire of Myriad
Melodies" album page (Mondstadt / Liyue / Inazuma / Fontaine / ...),
running each unfinished song with the rhythm bot and rotating through
all 12 slots via the song-wheel on the left.

Per-song flow (current English UI):
  - verify on the album page (anchor: "Go Perform" pill bottom-right)
  - check Canorus pill in the target difficulty's inline row
  - if Canorus → click next-song slot, advance
  - else: click "Go Perform" → click difficulty card → click
    "Begin Performance" → run rhythm detector while a 5s watcher polls
    for the "Select Song" button → click it → wait for album page →
    advance via next-song click

Run from inside a country album page (NOT the All Albums grid):
    python pc_client/album.py
"""
import argparse
import ctypes
import ctypes.wintypes
import sys
import threading
import time
from pathlib import Path

import cv2
import mss
import numpy as np

from config import (ALBUM_BEGIN_PERFORMANCE_XY, ALBUM_DIFFICULTY,
                    ALBUM_DIFFICULTY_COORDS, ALBUM_END_POLL_S,
                    ALBUM_GO_PERFORM_XY, ALBUM_MATCH_THRESHOLD,
                    ALBUM_NEXT_SONG_XY, ALBUM_SONG_COUNT,
                    KEY_POLL_DELAY_S, REF_COLUMN_X, REF_HIT_LINE_Y,
                    Y_SAMPLE_OFFSETS)
from controller import ArduinoHIDController
from detector import NoteDetector
from main import find_game_window, get_game_geometry


# 1080p reference ROIs (search bounding boxes), derived from screenshots
# of the current English UI.
# Per-difficulty Canorus pill — inline row on the album song-detail panel.
ROI_CANORUS = {
    'normal':    (440, 445, 220, 50),
    'hard':      (440, 530, 220, 50),
    'pro':       (440, 615, 220, 50),
    'legendary': (440, 700, 220, 50),
}

# "Go Perform" pill (album-page anchor + click target). ROIs need to be
# wider than `tpl.shape[1]` for matchTemplate's sliding window — leaving
# generous horizontal slack so a few-pixel UI shift doesn't push the
# template clean off the ROI edge (cost a debugging session to learn).
ROI_GO_PERFORM  = (1530, 975, 380, 75)

# "Select Song" pill on the post-song results screen.
ROI_SELECT_SONG = (1260, 975, 260, 75)

# "Begin Performance" pill on the difficulty-selection screen — used as
# the anchor that the prior "Go Perform" click actually landed.
ROI_BEGIN_PERFORMANCE = (970, 975, 320, 75)


class AlbumRunner:
    def __init__(self, replay_canorus=False, difficulty=ALBUM_DIFFICULTY):
        if difficulty not in ALBUM_DIFFICULTY_COORDS:
            raise ValueError(f"unknown difficulty: {difficulty!r}")
        self.replay_canorus = replay_canorus
        self.difficulty = difficulty
        self.hwnd = find_game_window()
        if not self.hwnd:
            print("ERROR: game window not found")
            sys.exit(1)
        self.region, self.scale_x, self.scale_y = get_game_geometry(self.hwnd)
        # Game enforces 16:9, so scale_x ~= scale_y. Use scale_x for
        # template resampling (single scalar AssetScale, mirroring BGI).
        self.scale = self.scale_x
        # mss handles are thread-local in mss>=6: each thread that calls
        # grab() needs its own mss.mss() or it dies with
        # `_thread._local has no attribute 'srcdc'`. Lazily create one per
        # thread via threading.local().
        self._sct_local = threading.local()

        assets_dir = Path(__file__).parent / 'assets' / '1920x1080'
        self.tpl = {
            'go_perform':   self._load_tpl(assets_dir / 'btn_go_perform.png'),
            'begin_perf':   self._load_tpl(assets_dir / 'btn_begin_performance.png'),
            'select_song':  self._load_tpl(assets_dir / 'btn_select_song.png'),
            'canorus':      self._load_tpl(assets_dir / 'music_canorus.png'),
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
              f"difficulty={self.difficulty}")

    @staticmethod
    def _load_tpl(path):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"failed to load template: {path}")
        return img

    # --- screen / coord helpers --------------------------------------------

    def _grab(self):
        sct = getattr(self._sct_local, 'sct', None)
        if sct is None:
            sct = mss.mss()
            self._sct_local.sct = sct
        shot = sct.grab(self.region)
        return np.ascontiguousarray(np.array(shot)[:, :, :3])

    def _ref_to_screen(self, x, y):
        return (int(x * self.scale_x) + self.region['left'],
                int(y * self.scale_y) + self.region['top'])

    def _ref_rect_to_local(self, ref_x, ref_y, ref_w, ref_h):
        return (int(ref_x * self.scale_x), int(ref_y * self.scale_y),
                int(ref_w * self.scale_x), int(ref_h * self.scale_y))

    def _move_cursor_to(self, target_x, target_y, max_iters=15, tol=2):
        """Iteratively move the cursor to (target_x, target_y) via Arduino
        HID relative moves. Genshin's anti-cheat ignores SetCursorPos and
        SendInput-style cursor moves entirely — only real HID hardware
        deltas update the in-game cursor. We close the loop with
        GetCursorPos to compensate for Windows pointer-precision scaling
        (HID mickeys != screen pixels at non-default settings)."""
        pt = ctypes.wintypes.POINT()
        for _ in range(max_iters):
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            dx = target_x - pt.x
            dy = target_y - pt.y
            if abs(dx) <= tol and abs(dy) <= tol:
                return True
            self.controller.mouse_move(dx, dy)
            time.sleep(0.04)
        return False

    def _click_screen(self, sx, sy):
        converged = self._move_cursor_to(int(sx), int(sy))
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        print(f"    click target=({int(sx)},{int(sy)}), "
              f"final=({pt.x},{pt.y}), converged={converged}")
        # Longer hover + click hold than synthetic clicks need; some games
        # debounce sub-100ms presses or treat them as "tap-cancel".
        time.sleep(0.15)
        self.controller.mouse_down('left')
        time.sleep(0.10)
        self.controller.mouse_up('left')
        time.sleep(0.15)

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
        roi = self._ref_rect_to_local(*ROI_GO_PERFORM)
        return self._match(frame, self.tpl['go_perform'], roi=roi) is not None

    def _dump_frame(self, tag):
        """Save current frame to disk so the user can see what was on
        screen when something went wrong."""
        out = Path(__file__).parent / f'_album_debug_{tag}.png'
        cv2.imwrite(str(out), self._grab())
        print(f"  diag: dumped {out.name}")

    def _diagnose_album_page(self):
        """On entry-check fail: print best score and dump frame + ROI to
        disk so the user can verify ROI placement / template suitability."""
        frame = self._grab()
        x, y, w, h = self._ref_rect_to_local(*ROI_GO_PERFORM)
        haystack = frame[y:y + h, x:x + w]
        tpl = self.tpl['go_perform']
        if haystack.shape[0] < tpl.shape[0] or haystack.shape[1] < tpl.shape[1]:
            print(f"  diag: ROI {haystack.shape} smaller than template {tpl.shape}")
        else:
            result = cv2.matchTemplate(haystack, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            print(f"  diag: best go_perform score = {max_val:.3f} "
                  f"(threshold {ALBUM_MATCH_THRESHOLD}); "
                  f"ROI local=(x={x},y={y},w={w},h={h})")
        out_full = Path(__file__).parent / '_album_debug_grab.png'
        out_roi = Path(__file__).parent / '_album_debug_roi.png'
        cv2.imwrite(str(out_full), frame)
        cv2.imwrite(str(out_roi), haystack)
        print(f"  diag: dumped {out_full.name} and {out_roi.name}")

    def _is_canorus(self, frame=None):
        if frame is None:
            frame = self._grab()
        roi = self._ref_rect_to_local(*ROI_CANORUS[self.difficulty])
        return self._match(frame, self.tpl['canorus'], roi=roi) is not None

    def _find_select_song(self, frame=None):
        if frame is None:
            frame = self._grab()
        roi = self._ref_rect_to_local(*ROI_SELECT_SONG)
        return self._match(frame, self.tpl['select_song'], roi=roi)

    def _wait_for(self, tpl_key, ref_roi, timeout=5.0, present=True):
        """Poll until template is present (or absent) in `ref_roi`. Returns
        True iff the desired state is reached within timeout."""
        end = time.time() + timeout
        while time.time() < end:
            frame = self._grab()
            roi = self._ref_rect_to_local(*ref_roi)
            found = self._match(frame, self.tpl[tpl_key], roi=roi) is not None
            if found == present:
                return True
            time.sleep(0.2)
        return False

    # --- per-song flow ------------------------------------------------------

    def _next_song(self):
        self._click_ref(*ALBUM_NEXT_SONG_XY)
        time.sleep(0.8)

    def _wait_for_album_page(self, timeout=60.0):
        end = time.time() + timeout
        while time.time() < end:
            if self._on_album_page():
                return True
            time.sleep(0.5)
        return False

    def _play_song(self):
        # album page → difficulty selection
        self._click_ref(*ALBUM_GO_PERFORM_XY)
        if not self._wait_for('begin_perf', ROI_BEGIN_PERFORMANCE,
                              timeout=5.0, present=True):
            print("  ERROR: difficulty screen never appeared "
                  "(Go Perform click missed?). Aborting song.")
            self._dump_frame('postclick_go_perform')
            return False

        # pick difficulty card
        dx, dy = ALBUM_DIFFICULTY_COORDS[self.difficulty]
        self._click_ref(dx, dy)
        time.sleep(0.3)

        # start rhythm minigame; verify we left the diff screen
        self._click_ref(*ALBUM_BEGIN_PERFORMANCE_XY)
        if not self._wait_for('begin_perf', ROI_BEGIN_PERFORMANCE,
                              timeout=5.0, present=False):
            print("  ERROR: still on difficulty screen after Begin "
                  "Performance click. Aborting song.")
            return False
        time.sleep(0.8)

        # Fire up rhythm detector. Watcher signals end-of-song by clicking
        # the "Select Song" button when the results screen appears.
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
            # Polled wait so SIGINT (Ctrl+C) can wake the main thread on
            # Windows — Event.wait() with no timeout blocks in a native
            # condition variable that ignores Python signal delivery.
            # Also bail out if the watcher dies unexpectedly.
            while not stop_evt.wait(0.5):
                if not watcher.is_alive():
                    print("  WARN: watcher thread died "
                          "(see traceback above); aborting song")
                    break
        finally:
            detector.stop()

        time.sleep(2.0)
        return True

    def _end_watcher(self, stop_evt):
        while not stop_evt.is_set():
            if stop_evt.wait(ALBUM_END_POLL_S):
                return
            res = self._find_select_song()
            if res is not None:
                _, (cx, cy) = res
                self._click_local(cx, cy)
                stop_evt.set()
                return

    # --- main loop ----------------------------------------------------------

    def run(self):
        if not self._on_album_page():
            print("ERROR: not on an album page. Open a country album "
                  "(Fontaine / Liyue / Mondstadt / ...), NOT the All Albums grid.")
            self._diagnose_album_page()
            return

        mode = "replay-canorus" if self.replay_canorus else "skip-on-canorus"
        print(f"Per-key {KEY_POLL_DELAY_S * 1000:.0f}ms, strip {Y_SAMPLE_OFFSETS}")
        print(f"Album loop: {ALBUM_SONG_COUNT} songs, "
              f"difficulty={self.difficulty}, {mode}")

        for i in range(ALBUM_SONG_COUNT):
            print(f"\n--- Song {i+1}/{ALBUM_SONG_COUNT} ---")
            if not self.replay_canorus:
                frame = self._grab()
                if self._is_canorus(frame):
                    print("  skip — already canorus")
                    self._next_song()
                    continue

            print("  playing")
            if not self._play_song():
                print("  aborting album (state machine broke; check coords).")
                return

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


SPI_GETMOUSE = 0x0003
SPI_SETMOUSE = 0x0004


def _get_mouse_params():
    arr = (ctypes.c_int * 3)()
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETMOUSE, 0, arr, 0)
    return tuple(arr)


def _set_mouse_params(thresh1, thresh2, accel):
    arr = (ctypes.c_int * 3)(thresh1, thresh2, accel)
    ctypes.windll.user32.SystemParametersInfoW(SPI_SETMOUSE, 0, arr, 0)


def _prompt_replay_canorus():
    """Interactive y/n prompt. Returns True if user wants to replay
    Canorus'd songs, False to skip them."""
    print()
    print("Skip songs already at Canorus rank?")
    print("  [Y] yes, skip them (default)")
    print("  [N] no, replay every song")
    while True:
        ans = input("Choice [Y/n]: ").strip().lower()
        if ans in ('', 'y', 'yes'):
            return False
        if ans in ('n', 'no'):
            return True
        print("Enter Y or N.")


def _prompt_difficulty():
    """Interactive difficulty prompt. Returns one of
    'normal' | 'hard' | 'pro' | 'legendary'."""
    print()
    print("Select difficulty:")
    print("  [1] Normal")
    print("  [2] Hard")
    print("  [3] Pro")
    print("  [4] Legendary (default)")
    options = {
        '': 'legendary', '4': 'legendary', 'l': 'legendary', 'legendary': 'legendary',
        '1': 'normal', 'n': 'normal', 'normal': 'normal',
        '2': 'hard', 'h': 'hard', 'hard': 'hard',
        '3': 'pro', 'p': 'pro', 'pro': 'pro',
    }
    while True:
        ans = input("Choice [1/2/3/4]: ").strip().lower()
        if ans in options:
            return options[ans]
        print("Enter 1, 2, 3, or 4.")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-play one Genshin album page at the configured "
                    "difficulty.")
    parser.add_argument(
        '--replay-canorus', action='store_true',
        help="Play every song in the album, including ones already at "
             "Canorus rank on the chosen difficulty. If omitted, the "
             "script prompts at startup.")
    parser.add_argument(
        '--difficulty', choices=list(ALBUM_DIFFICULTY_COORDS),
        help="Skip the difficulty prompt and use this one. "
             "If omitted, the script prompts at startup.")
    args = parser.parse_args()

    replay = args.replay_canorus or _prompt_replay_canorus()
    difficulty = args.difficulty or _prompt_difficulty()

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

    # Disable Enhance Pointer Precision (EPP) for the run. EPP applies a
    # velocity-dependent gain curve so a single HID burst of N mickeys
    # travels 1.5x-3.5x N pixels, which makes our cursor closed-loop
    # overshoot wildly and pinball off screen edges. Linear (accel=0)
    # gives a constant mickey:pixel ratio that converges in 1-2 iters.
    saved_mouse = None
    try:
        saved_mouse = _get_mouse_params()
        _set_mouse_params(0, 0, 0)
        print(f"Pointer precision disabled (saved {saved_mouse})")
    except Exception as e:
        print(f"[warn] disable EPP: {e}")

    runner = AlbumRunner(replay_canorus=replay, difficulty=difficulty)
    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nCtrl+C")
    finally:
        runner.close()
        if saved_mouse is not None:
            try:
                _set_mouse_params(*saved_mouse)
                print("Pointer precision restored")
            except Exception as e:
                print(f"[warn] restore EPP: {e}")
        if timer_boosted:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        print("Shutdown complete")


if __name__ == '__main__':
    main()
