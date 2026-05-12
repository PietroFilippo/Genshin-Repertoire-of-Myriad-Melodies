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
import sys
import threading
import time
from pathlib import Path

import cv2

from config import (ALBUM_DIFFICULTY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_MATCH_THRESHOLD, ALBUM_NEXT_SONG_XY,
                    ALBUM_SONG_COUNT, KEY_POLL_DELAY_S, Y_SAMPLE_OFFSETS)
from controller import ArduinoHIDController
from game_window import find_game_window, get_game_geometry
from screen_watcher import ScreenWatcher
from song_player import ROI_SELECT_SONG, SongPlayer


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

# `ROI_SELECT_SONG` and `ROI_BEGIN_PERFORMANCE` live in `song_player.py`
# (owner of the song-flow state machine). `ROI_SELECT_SONG` is re-
# imported above for `AlbumRunner._find_select_song`, which handles
# the post-pause results-screen recovery the song-player's watcher
# can't see (the watcher dies on pause).


class AlbumRunner:
    def __init__(self, replay_canorus=False, difficulty=ALBUM_DIFFICULTY,
                 stop_evt=None, pause_evt=None, debug_evt=None,
                 status_cb=None, mid_song_start=False, controller=None):
        """
        Args:
            stop_evt: external Event — when set, abort cleanly. UI uses this
                for the Stop button.
            pause_evt: external Event — when set, the album loop blocks at
                the next safe boundary (between songs). Mid-song pause
                aborts the current song (releases keys via detector.stop)
                and waits at the next boundary. Clear to resume.
            debug_evt: external Event — when set, the rhythm-mode portion
                of `SongPlayer.play` spawns a viz thread that mirrors
                `standalone_runner.run_visualization`'s overlay. Toggling
                clears/recreates the cv2 window in real time without
                restarting the bot.
            status_cb: optional callable(dict) — emits status updates so
                the UI can mirror state ('state', 'song', 'fps', ...).
            mid_song_start: when True, the runner assumes a song is
                already in progress on screen (e.g. user switched from
                standalone → album mid-song). The first iteration skips
                the album-page check, the canorus check, and the entire
                Go Perform → difficulty → Begin Performance click chain
                — it spins up the detector + end-watcher directly on the
                song already playing, then proceeds with the normal
                album loop from song 2 onwards.
            difficulty: one of 'normal' / 'hard' / 'pro' / 'legendary'
                (single-difficulty run, classic flat loop), OR a list/
                tuple of any subset of those (per-position cycle through
                the chosen subset — the wheel only advances between
                positions, so each song-slot is played once per chosen
                difficulty), OR the literal string 'all' which is a
                shortcut for the full 4-element list. The list path is
                what the UI sends after multi-checkbox selection;
                single-string + 'all' are kept for CLI back-compat. The
                runner reorders the user's selection into canonical
                normal → hard → pro → legendary order regardless of
                checkbox click order. Each cycle's canorus check is
                independent because the canorus pill is per-difficulty
                in-game.
        """
        # Canonical card order — Genshin's UI presents the cards left
        # to right in this sequence, and each cycle plays a song at
        # increasing difficulty so the player's mental model stays
        # intact regardless of which subset the user picked.
        _ORDER = ('normal', 'hard', 'pro', 'legendary')
        if isinstance(difficulty, (list, tuple, set, frozenset)):
            chosen = {
                d.lower() for d in difficulty
                if isinstance(d, str) and d.lower() in ALBUM_DIFFICULTY_COORDS
            }
            if not chosen:
                raise ValueError(
                    f"no valid difficulties in: {difficulty!r}")
            self.difficulties = [d for d in _ORDER if d in chosen]
        elif difficulty == 'all':
            self.difficulties = list(_ORDER)
        elif difficulty in ALBUM_DIFFICULTY_COORDS:
            self.difficulties = [difficulty]
        else:
            raise ValueError(f"unknown difficulty: {difficulty!r}")
        self.replay_canorus = replay_canorus
        self.difficulty = self.difficulties[0]
        self.mid_song_start = bool(mid_song_start)
        self.stop_evt = stop_evt if stop_evt is not None else threading.Event()
        self.pause_evt = pause_evt if pause_evt is not None else threading.Event()
        self.debug_evt = debug_evt if debug_evt is not None else threading.Event()
        self.status_cb = status_cb
        self.hwnd = find_game_window(verbose=True)
        if not self.hwnd:
            print("ERROR: game window not found")
            sys.exit(1)
        geom = get_game_geometry(self.hwnd, stop_evt=self.stop_evt)
        if geom is None:
            # Stop fired while waiting for the game window to be visible.
            # Re-raise as RuntimeError so the BotController worker exits.
            raise RuntimeError("album-init aborted by stop_evt")
        self.region, self.scale_x, self.scale_y = geom

        # Disable Enhance Pointer Precision (EPP) for the run. EPP applies
        # a velocity-dependent gain curve so a single HID burst of N
        # mickeys travels 1.5x-3.5x N pixels, which makes the Arduino
        # backend's iterative move_to overshoot wildly and pinball off
        # the screen edges. Linear
        # (accel=0) gives a constant mickey:pixel ratio that converges in
        # 1-2 iters. Saved here and restored in close() so this works the
        # same whether the CLI or the UI is driving the runner — the CLI
        # also wraps with its own save/restore as belt-and-suspenders.
        self._saved_mouse = None
        try:
            self._saved_mouse = _get_mouse_params()
            _set_mouse_params(0, 0, 0)
            print(f"Pointer precision disabled (saved {self._saved_mouse})")
        except Exception as e:
            print(f"[warn] disable EPP: {e}")
        # Game enforces 16:9, so scale_x ~= scale_y. Use scale_x for
        # template resampling (single scalar AssetScale, mirroring BGI).
        self.scale = self.scale_x

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

        if controller is not None:
            self.controller = controller
            self._owns_controller = False
        else:
            self.controller = ArduinoHIDController()
            self._owns_controller = True

        # Screen automation — grab + scaling + template match + clicks +
        # waits. Templates dict passed by reference; we keep self.tpl
        # for _diagnose_album_page's raw matchTemplate call (which
        # needs the score even below threshold).
        self.watcher = ScreenWatcher(
            region=self.region, scale_x=self.scale_x, scale_y=self.scale_y,
            controller=self.controller, stop_evt=self.stop_evt,
            templates=self.tpl, match_threshold=ALBUM_MATCH_THRESHOLD)

        # Per-song state machine: intro click chain → detector + viz +
        # end-watcher → pause-resume → cleanup. AlbumRunner stays as
        # the album-level orchestrator (cycle loops, canorus skip,
        # wheel advance). `_dump_frame` is passed as the error-path
        # debug-frame callback so SongPlayer's failure messages land
        # in `_album_debug_*.png` next to album.py.
        self.song_player = SongPlayer(
            watcher=self.watcher, controller=self.controller,
            hwnd=self.hwnd, region=self.region,
            scale_x=self.scale_x, scale_y=self.scale_y,
            stop_evt=self.stop_evt, pause_evt=self.pause_evt,
            debug_evt=self.debug_evt, status_cb=self.status_cb,
            dump_frame_fn=self._dump_frame)

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

    # --- state checks (album-specific shortcuts over self.watcher.match) ---

    def _on_album_page(self, frame=None):
        if frame is None:
            frame = self.watcher.grab()
        roi = self.watcher.ref_rect_to_local(*ROI_GO_PERFORM)
        return self.watcher.match(frame, 'go_perform', roi=roi) is not None

    def _is_canorus(self, frame=None):
        if frame is None:
            frame = self.watcher.grab()
        roi = self.watcher.ref_rect_to_local(*ROI_CANORUS[self.difficulty])
        return self.watcher.match(frame, 'canorus', roi=roi) is not None

    def _find_select_song(self, frame=None):
        if frame is None:
            frame = self.watcher.grab()
        roi = self.watcher.ref_rect_to_local(*ROI_SELECT_SONG)
        return self.watcher.match(frame, 'select_song', roi=roi)

    # --- diagnostics --------------------------------------------------------

    def _dump_frame(self, tag):
        """Save current frame to disk so the user can see what was on
        screen when something went wrong."""
        out = Path(__file__).parent / f'_album_debug_{tag}.png'
        self.watcher.dump_frame(out)
        print(f"  diag: dumped {out.name}")

    def _diagnose_album_page(self):
        """On entry-check fail: print best score and dump frame + ROI to
        disk so the user can verify ROI placement / template suitability.
        Bypasses watcher.match because we need the raw score even below
        threshold; watcher.match filters those out by design."""
        frame = self.watcher.grab()
        x, y, w, h = self.watcher.ref_rect_to_local(*ROI_GO_PERFORM)
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

    # --- per-song flow ------------------------------------------------------

    def _next_song(self):
        self.watcher.click_ref(*ALBUM_NEXT_SONG_XY)
        self.watcher.stop_wait(0.8)

    def _wait_for_album_page(self, timeout=60.0):
        """Poll up to `timeout` for the album page anchor. Worst-case
        blocker for stop-responsiveness (60s default), so the poll uses
        stop-aware sleep."""
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_evt.is_set():
                return False
            if self._on_album_page():
                return True
            if self.watcher.stop_wait(0.5):
                return False
        return False

    # --- main loop ----------------------------------------------------------

    def _emit(self, update):
        if self.status_cb:
            try:
                self.status_cb(update)
            except Exception:
                pass

    def _await_resume(self):
        """Block while pause_evt is set; bail out if stop fires. Called at
        safe boundaries between songs."""
        if not self.pause_evt.is_set():
            return
        self._emit({'state': 'paused'})
        print("  paused — waiting for resume")
        while self.pause_evt.is_set() and not self.stop_evt.is_set():
            time.sleep(0.1)
        if not self.stop_evt.is_set():
            self._emit({'state': 'running'})
            print("  resumed")

    def run(self, songs_count=None):
        if songs_count is None:
            songs_count = ALBUM_SONG_COUNT
        songs_count = max(1, min(int(songs_count), ALBUM_SONG_COUNT))

        # Album-page check + canorus check require the album-page UI to
        # be on screen. When mid_song_start is set, the user is mid-song
        # in the rhythm minigame — neither check applies for the first
        # iteration. Subsequent iterations land on the album page after
        # _wait_for_album_page so the normal flow takes over.
        if not self.mid_song_start:
            if not self._on_album_page():
                print("ERROR: not on an album page. Open a country album "
                      "(Fontaine / Liyue / Mondstadt / ...), NOT the All Albums grid.")
                self._diagnose_album_page()
                return

        mode = "replay-canorus" if self.replay_canorus else "skip-on-canorus"
        print(f"Per-key {KEY_POLL_DELAY_S * 1000:.0f}ms, strip {Y_SAMPLE_OFFSETS}")
        diffs = self.difficulties
        if len(diffs) > 1:
            print(f"Album loop: ALL difficulties {diffs}, "
                  f"{songs_count}/{ALBUM_SONG_COUNT} songs each, {mode}")
        else:
            start_msg = "mid-song hand-off, " if self.mid_song_start else ""
            print(f"Album loop: {start_msg}{songs_count}/{ALBUM_SONG_COUNT} songs, "
                  f"difficulty={diffs[0]}, {mode}")

        self._emit({'state': 'running', 'mode': 'album',
                    'song': f'0/{songs_count}'})

        if len(diffs) == 1:
            # Single-difficulty: keep the legacy flat loop. Each song
            # gets one next-song click after it (so 12-song runs cycle
            # the wheel cleanly; partial runs leave the wheel at the
            # song-after-the-last-played).
            self.difficulty = diffs[0]
            self._run_one_album(songs_count, self.mid_song_start)
        else:
            # All-difficulties: iterate song positions in the outer
            # loop, difficulties in the inner loop. Wheel only advances
            # between positions, not between difficulties at the same
            # position, so each song is played once per difficulty.
            self._run_all_difficulties(songs_count)

        print("\nAlbum complete" if not self.stop_evt.is_set() else "\nAlbum stopped")
        self._emit({'state': 'idle', 'fps': 0.0})

    def _run_one_album(self, songs_count, mid_song_first_iter):
        """Run one full album cycle at `self.difficulty`. Returns True if
        the cycle completed normally (next difficulty can run); False if
        stop fired or an unrecoverable state error happened."""
        for i in range(songs_count):
            if self.stop_evt.is_set():
                print("\nstop requested — exiting album loop")
                return False
            self._await_resume()
            if self.stop_evt.is_set():
                return False

            mid_song_iter = (i == 0 and mid_song_first_iter)

            print(f"\n--- Song {i+1}/{songs_count} "
                  f"[{self.difficulty}] ---")
            self._emit({'song': f'{i+1}/{songs_count}'})
            if not self.replay_canorus and not mid_song_iter:
                frame = self.watcher.grab()
                if self._is_canorus(frame):
                    print("  skip — already canorus")
                    self._next_song()
                    continue

            print("  playing")
            if not self.song_player.play(self.difficulty, skip_intro=mid_song_iter):
                if self.stop_evt.is_set():
                    return False
                print("  aborting album (state machine broke; check coords).")
                return False

            # Pause may have fired mid-song — song_player.play aborted
            # and we land here. Wait at the boundary before clicking
            # next-song.
            self._await_resume()
            if self.stop_evt.is_set():
                return False

            # After a mid-song pause the rhythm game may have ended while
            # the bot was idle, leaving the results screen up (the watcher
            # was killed on pause, so nobody clicked Select Song). Check
            # for it now and click through if present.
            if not self._on_album_page():
                res = self._find_select_song()
                if res is not None:
                    print("  clicking Select Song (results screen after pause)")
                    _, (cx, cy) = res
                    self.watcher.click_local(cx, cy)
                    if self.watcher.stop_wait(2.0):
                        return False

            if not self._wait_for_album_page(timeout=60.0):
                if self.stop_evt.is_set():
                    return False
                print("  WARN: never returned to album page; stopping")
                return False

            self._next_song()

        return True

    def _run_all_difficulties(self, songs_count):
        """Per-position cycle through all configured difficulties.

        Outer loop = song position (1..songs_count). Inner loop =
        difficulty. The wheel advances ONLY between positions, not
        between difficulties at the same position — that way each
        song-slot is played once per difficulty. Click rules:

          - Between difficulties at the same position: no next-song.
          - Between positions: one next-song click.
          - After the last position: one extra next-song click ONLY
            when songs_count == ALBUM_SONG_COUNT (12), which rolls the
            wheel back to its starting slot. Partial runs leave the
            wheel at the last-played position.

        Examples:
          songs_count=1  -> 0 next-song clicks total (stays at song 1
            for all 4 difficulties).
          songs_count=12 -> 11 inter-position clicks + 1 final = 12
            clicks total, wheel cycles back to the starting song.

        Canorus skip is per-difficulty: skipping at one difficulty just
        moves to the next difficulty for the same position; the wheel
        does not advance.
        """
        diffs = self.difficulties

        for i in range(songs_count):
            if self.stop_evt.is_set():
                print("\nstop requested — exiting album loop")
                return
            self._await_resume()
            if self.stop_evt.is_set():
                return

            for d_idx, diff in enumerate(diffs):
                if self.stop_evt.is_set():
                    return
                self._await_resume()
                if self.stop_evt.is_set():
                    return

                self.difficulty = diff
                mid_song_iter = (i == 0 and d_idx == 0
                                 and self.mid_song_start)

                print(f"\n--- Song {i+1}/{songs_count} [{diff}] ---")
                self._emit({'song': f'{i+1}/{songs_count} [{diff}]'})

                if not self.replay_canorus and not mid_song_iter:
                    frame = self.watcher.grab()
                    if self._is_canorus(frame):
                        print(f"  skip — already canorus at {diff}")
                        continue   # next difficulty; do NOT advance wheel

                print("  playing")
                if not self.song_player.play(self.difficulty, skip_intro=mid_song_iter):
                    if self.stop_evt.is_set():
                        return
                    print("  aborting album (state machine broke; check coords).")
                    return

                self._await_resume()
                if self.stop_evt.is_set():
                    return

                # Mid-song pause may have left the results screen up.
                if not self._on_album_page():
                    res = self._find_select_song()
                    if res is not None:
                        print("  clicking Select Song (results screen after pause)")
                        _, (cx, cy) = res
                        self.watcher.click_local(cx, cy)
                        if self.watcher.stop_wait(2.0):
                            return

                if not self._wait_for_album_page(timeout=60.0):
                    if self.stop_evt.is_set():
                        return
                    print("  WARN: never returned to album page; stopping")
                    return

                # Intentionally NO _next_song here — next iter of the
                # difficulty loop plays the same song at the next diff.

            # All difficulties for this position done. Advance wheel to
            # the next position UNLESS this was the last one.
            if i < songs_count - 1:
                self._next_song()

        # Final wheel reset: only when the user ran the full 12-song
        # album. The 12th next-song click cycles the wheel back to the
        # starting song slot. Partial runs leave the wheel where it is.
        if not self.stop_evt.is_set() and songs_count == ALBUM_SONG_COUNT:
            print("\nrolling wheel back to first song")
            self._next_song()

    def close(self):
        if getattr(self, '_saved_mouse', None) is not None:
            try:
                _set_mouse_params(*self._saved_mouse)
                print("Pointer precision restored")
            except Exception as e:
                print(f"[warn] restore EPP: {e}")
            self._saved_mouse = None
        if getattr(self, '_owns_controller', True):
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
    parser.add_argument(
        '--songs', type=int, default=ALBUM_SONG_COUNT,
        help=f"Number of songs to run (1..{ALBUM_SONG_COUNT}). "
             f"Defaults to the full album ({ALBUM_SONG_COUNT}).")
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

    # EPP save / restore is handled by AlbumRunner (so UI mode gets it
    # too). See AlbumRunner.__init__ for the rationale.
    runner = AlbumRunner(replay_canorus=replay, difficulty=difficulty)
    try:
        runner.run(songs_count=args.songs)
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
