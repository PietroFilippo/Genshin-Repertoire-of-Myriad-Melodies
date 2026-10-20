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
    python pc_client/album.py [--backend software|arduino]
(default backend: INPUT_BACKEND_DEFAULT in config.py, like main.py).
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
                    ALBUM_SONG_COUNT, INPUT_BACKEND_DEFAULT,
                    KEY_POLL_DELAY_S, Y_SAMPLE_OFFSETS)
from game_window import (MSG_WINDOW_NOT_FOUND, WINDOW_ALIVE, GameWindowRef,
                         GameWindowUnavailable, find_game_window,
                         lost_message, read_game_geometry,
                         wait_game_geometry)
from screen_watcher import REFUSED_NOT_IN_FRONT, ScreenWatcher
from song_player import (MSG_GAME_NOT_IN_FRONT, ROI_GO_PERFORM,
                         ROI_SELECT_SONG, SongPlayer)
from standalone_runner import MSG_ARDUINO_UNAVAILABLE, make_fallback_arduino

# How often the album's pause waits check that the game still exists.
WINDOW_CHECK_S = 0.5


# 1080p reference ROIs (search bounding boxes), derived from screenshots
# of the current English UI.
# Per-difficulty Canorus pill — inline row on the album song-detail panel.
ROI_CANORUS = {
    'normal':    (440, 445, 220, 50),
    'hard':      (440, 530, 220, 50),
    'pro':       (440, 615, 220, 50),
    'legendary': (440, 700, 220, 50),
}

# `ROI_GO_PERFORM` (album-page anchor + click target — see the ROI-width
# note next to its definition), `ROI_SELECT_SONG` and
# `ROI_BEGIN_PERFORMANCE` live in `song_player.py` (owner of the
# song-flow state machine, which also needs Go Perform to verify a
# mid-song hand-off). `ROI_GO_PERFORM` / `ROI_SELECT_SONG` are re-
# imported above for the album-page check and for
# `AlbumRunner._find_select_song`, which serves `_after_song`'s
# results-screen recovery: a mid-song hand-off that landed on the
# results screen, a resume that found the song already over, or an
# end-watcher Select Song click that didn't register. (SongPlayer's end
# watcher stays alive during a pause; this is the backstop for the
# cases it doesn't cover.)

# `AlbumRunner.run()` outcomes.
RESULT_COMPLETED = 'completed'
RESULT_STOPPED = 'stopped'
RESULT_FAILED = 'failed'

# User-facing `last_error` strings.
MSG_NOT_ON_ALBUM_PAGE = ("Not on a country album page — open one (not the "
                         "All Albums grid), then press Start.")
MSG_NO_RETURN_TO_ALBUM = ("The game never returned to the album page after "
                          "a song — check the game, then press Start.")
MSG_NEXT_SONG_FAILED = ("Couldn't click the next song on the album wheel — "
                        "keep the game window fully on screen, then press "
                        "Start.")
MSG_SONG_FAILED = ("Album run stopped on an unexpected screen — open a "
                   "country album page, then press Start.")


class AlbumSetupError(RuntimeError):
    """AlbumRunner couldn't start: game window missing / closed, Arduino
    unavailable, or stop fired while waiting for the window. The
    user-facing reason has already been reported via
    `status_cb({'last_error': ...})` where one applies."""


class AlbumRunner:
    def __init__(self, replay_canorus=False, difficulty=ALBUM_DIFFICULTY,
                 stop_evt=None, pause_evt=None, debug_evt=None,
                 status_cb=None, mid_song_start=False, controller=None):
        """
        Args:
            stop_evt: external Event — when set, abort cleanly. UI uses this
                for the Stop button.
            pause_evt: external Event — when set, the album loop blocks at
                the next safe boundary. Between songs it waits at the
                song boundary; during the intro click chain it holds
                before the next click; mid-song it stops the detector
                (releasing keys) and the viz while SongPlayer's end
                watcher stays alive, and resume starts a fresh detector
                on the same song. Clear to resume.
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
                album loop from song 2 onwards. The hint is verified:
                if the album page is actually showing, the run starts
                normally; SongPlayer re-checks for the rhythm minigame
                before skipping the intro.
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
        # Most recent user-facing error reported this run ('' = none, or
        # cleared). Lets a generic failure message avoid overwriting a
        # specific one already on screen (e.g. "window closed").
        self._last_error = ''
        # Sticky: the run's game window is no longer usable — closed or
        # changed, already reported (see _window_gone).
        self._window_lost = False
        # album_progress state (reset + first emitted at run() start).
        self._progress = {'index': 0, 'total': 0,
                          'difficulty': self.difficulties[0],
                          'difficulties': list(self.difficulties),
                          'results': []}
        # Set before anything can raise so close() is always safe.
        self._saved_mouse = None
        self._owns_controller = False
        self.controller = None

        hwnd = find_game_window(verbose=True)
        if not hwnd:
            print("ERROR: game window not found")
            self._emit({'last_error': MSG_WINDOW_NOT_FOUND})
            raise AlbumSetupError("game window not found")
        # The run is bound to this window, the game PROCESS owning it and
        # a handle to that process (held until close(); it also keeps the
        # PID from being reused). Never re-pointed at another window.
        try:
            self._game = GameWindowRef(hwnd)
        except GameWindowUnavailable as e:
            # Its owning process can't be read: not a window to run on.
            self._emit({'last_error': str(e)})
            raise AlbumSetupError(str(e)) from None
        # From here on any failure must close() — releases the process
        # handle, restores EPP if touched, closes a controller we opened.
        try:
            geom = wait_game_geometry(self._game, stop_evt=self.stop_evt,
                                      status_cb=self._emit)
            if geom is None:
                if self.stop_evt.is_set():
                    # Stop fired while waiting for the game window to be visible.
                    # Re-raise as RuntimeError so the BotController worker exits.
                    raise AlbumSetupError("album-init aborted by stop_evt")
                # Closed / changed while we waited — wait_game_geometry
                # reported it.
                raise AlbumSetupError("game window closed")
            self.region, self.scale_x, self.scale_y = geom
            # Game enforces 16:9, so scale_x ~= scale_y. Use scale_x for
            # template resampling (single scalar AssetScale, mirroring BGI).
            self.scale = self.scale_x

            # Assets + controller come BEFORE the EPP toggle below: both can
            # raise (missing template, Arduino not connected), and an
            # exception out of __init__ never reaches the caller's close() —
            # EPP would stay disabled until the user logs off.
            assets_dir = Path(__file__).parent / 'assets' / '1920x1080'
            # 1080p originals, kept so a mid-run window resize can rescale.
            self._tpl_src = {
                'go_perform':   self._load_tpl(assets_dir / 'btn_go_perform.png'),
                'begin_perf':   self._load_tpl(assets_dir / 'btn_begin_performance.png'),
                'select_song':  self._load_tpl(assets_dir / 'btn_select_song.png'),
                'canorus':      self._load_tpl(assets_dir / 'music_canorus.png'),
            }
            self.tpl = {}
            self._rescale_templates()

            if controller is not None:
                self.controller = controller
                self._owns_controller = False
            else:
                self.controller, failure = make_fallback_arduino(
                    cancel_evt=self.stop_evt)
                if self.controller is None:
                    if self.stop_evt.is_set():
                        raise AlbumSetupError("album-init aborted by stop_evt")
                    self._emit({'last_error': failure})
                    raise AlbumSetupError(failure)
                self._owns_controller = True

            # Screen automation — grab + scaling + template match + clicks +
            # waits. Templates dict passed by reference; we keep self.tpl
            # for _diagnose_album_page's raw matchTemplate call (which
            # needs the score even below threshold).
            # `live_geometry_fn` lets every click re-read the game's
            # client rect right before mouse-down, catching a move /
            # resize our ~0.5 s poll hasn't published yet — and refusing
            # outright once the window isn't the game's (see
            # _live_geometry).
            # `window_gone_fn` ends the watcher's waits as soon as the
            # game is closed or its window changed (reported once, by
            # _window_gone). `target_check_fn`: right before mouse-down,
            # the window under the cursor must belong to the game (an
            # app alt-tabbed over the target never gets the click).
            self.watcher = ScreenWatcher(
                region=self.region, scale_x=self.scale_x, scale_y=self.scale_y,
                controller=self.controller, stop_evt=self.stop_evt,
                templates=self.tpl, match_threshold=ALBUM_MATCH_THRESHOLD,
                live_geometry_fn=self._live_geometry,
                window_gone_fn=self._window_gone,
                target_check_fn=self._game.owns_point)

            # Per-song state machine: intro click chain → detector + viz +
            # end-watcher → pause-resume → cleanup. AlbumRunner stays as
            # the album-level orchestrator (cycle loops, canorus skip,
            # wheel advance). `_dump_frame` is passed as the error-path
            # debug-frame callback so SongPlayer's failure messages land
            # in `_album_debug_*.png` next to album.py. `_emit` (not the
            # raw status_cb) so we see the last_error it reports.
            self.song_player = SongPlayer(
                watcher=self.watcher, controller=self.controller,
                hwnd=self.hwnd, region=self.region,
                scale_x=self.scale_x, scale_y=self.scale_y,
                stop_evt=self.stop_evt, pause_evt=self.pause_evt,
                debug_evt=self.debug_evt, status_cb=self._emit,
                dump_frame_fn=self._dump_frame,
                refresh_geometry_fn=self._refresh_geometry,
                game_pid=self.game_pid)

            # Disable Enhance Pointer Precision (EPP) for the run. EPP applies
            # a velocity-dependent gain curve so a single HID burst of N
            # mickeys travels 1.5x-3.5x N pixels, which makes the Arduino
            # backend's iterative move_to overshoot wildly and pinball off
            # the screen edges. Linear
            # (accel=0) gives a constant mickey:pixel ratio that converges in
            # 1-2 iters. Saved here and restored in close() so this works the
            # same whether the CLI or the UI is driving the runner (neither
            # touches EPP itself; both call close()).
            # Last step of __init__ on purpose (see the assets note above).
            try:
                self._saved_mouse = _get_mouse_params()
                _set_mouse_params(0, 0, 0)
                print(f"Pointer precision disabled (saved {self._saved_mouse})")
            except Exception as e:
                print(f"[warn] disable EPP: {e}")

            print(f"Album: {self.region['width']}x{self.region['height']} @ "
                  f"({self.region['left']},{self.region['top']}), "
                  f"scale {self.scale_x:.3f}x{self.scale_y:.3f}, "
                  f"difficulty={self.difficulty}")
        except BaseException:
            # The caller never gets a runner to close(), so undo here:
            # release the process handle, restore EPP if it was touched,
            # close a controller we opened.
            self.close()
            raise

    @staticmethod
    def _load_tpl(path):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"failed to load template: {path}")
        return img

    def _rescale_templates(self):
        """(Re)build `self.tpl` from the 1080p originals at
        `self.scale`. Updates the dict in place — the watcher holds a
        reference to it."""
        for k, t in self._tpl_src.items():
            if abs(self.scale - 1.0) > 0.01:
                nw = max(1, int(round(t.shape[1] * self.scale)))
                nh = max(1, int(round(t.shape[0] * self.scale)))
                self.tpl[k] = cv2.resize(t, (nw, nh),
                                         interpolation=cv2.INTER_LINEAR)
            else:
                self.tpl[k] = t

    # --- window geometry ----------------------------------------------------

    @property
    def hwnd(self):
        """The game window the run is bound to (fixed for the run)."""
        return self._game.hwnd

    @property
    def game_pid(self):
        """The game process the run is bound to (0 = unreadable)."""
        return self._game.pid

    def _live_geometry(self):
        """The watcher's pre-click check (any thread, side-effect free):
        the window's live geometry while it is still the game's — process
        running, IsWindow, owned by the game's PID. A window the game
        destroyed, or an HWND recycled to another app (with a perfectly
        valid rect), reads None: the click is refused, no input."""
        if not self._game.alive():
            return None
        return read_game_geometry(self.hwnd)

    def _refresh_geometry(self, block=True):
        """Re-read the game window (windowed Genshin may have been moved
        or resized) and push any change into the watcher + song player.

        block=True waits — stop-aware — while the game is minimized
        (song boundaries, resume). block=False is a single cheap read
        that keeps the last-known geometry while the window is minimized
        / off-screen (SongPlayer calls it every ~0.5 s mid-song). Either
        way the window must still be the game's: a closed game or a
        changed window ends the run.

        Returns False iff the run should end: stop fired, or the window
        is no longer the game's (already reported as last_error)."""
        if self._window_lost:
            return False
        if block:
            geom = wait_game_geometry(self._game, stop_evt=self.stop_evt,
                                      status_cb=self._emit)
            if geom is None:
                if not self.stop_evt.is_set():
                    # Closed / changed — wait_game_geometry reported it.
                    self._window_lost = True
                return False
        else:
            if self._window_gone():
                return False
            geom = read_game_geometry(self.hwnd)
            if geom is None:
                # Minimized / off-screen: keep the last-known geometry.
                return True
        self._apply_geometry(*geom)
        return True

    def _window_gone(self):
        """True once the run's window is no longer the game's: the game
        process exited (MSG_WINDOW_CLOSED) or the window was destroyed /
        recycled while the game runs — e.g. recreated on a fullscreen or
        resolution switch (MSG_WINDOW_CHANGED; the run never follows
        another window). Reported once; sticky, so every later step
        fails fast. A minimized / off-screen game is NOT gone.

        Non-blocking while the window is fine (every ~0.5 s poll); once
        it isn't, `GameWindowRef.classify` looks for up to
        game_window.LOST_CONFIRM_S (stop-aware) to pick the message. A
        Stop meanwhile returns True with nothing reported (Stop wins)."""
        if self._window_lost:
            return True
        state = self._game.classify(self.stop_evt)
        if state == WINDOW_ALIVE:
            return False
        if self.stop_evt.is_set():
            return True
        self._window_lost = True
        message = lost_message(state)
        print(f"ERROR: {message} (ending the album run)")
        self._emit({'last_error': message})
        return True

    def _apply_geometry(self, region, scale_x, scale_y):
        if (region == self.region and scale_x == self.scale_x
                and scale_y == self.scale_y):
            return False
        resized = ((region['width'], region['height'])
                   != (self.region['width'], self.region['height']))
        self.region = dict(region)
        self.scale_x, self.scale_y = scale_x, scale_y
        if resized:
            self.scale = self.scale_x
            self._rescale_templates()
        self.watcher.update_geometry(self.region, scale_x, scale_y)
        self.song_player.update_geometry(self.region, scale_x, scale_y)
        print(f"[album] game window {'resized' if resized else 'moved'} — "
              f"capture now {self.region['width']}x{self.region['height']} "
              f"@ ({self.region['left']},{self.region['top']})")
        return True

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
        """Click the next song on the album wheel. One retry if the
        click didn't land (the Arduino backend's `move_to` can miss
        convergence). Returns True once a click went through and the
        wheel settled; False on stop (including during the settle wait),
        a closed game, or a second miss (reported as last_error)."""
        for attempt in range(2):
            if self.watcher.click_ref(*ALBUM_NEXT_SONG_XY):
                # Settle wait — Stop here must propagate, otherwise the
                # final wheel reset would report the run as completed.
                # So must a game closed during the settle (Stop wins).
                if self.watcher.stop_wait(0.8):
                    return False
                return not self._window_gone()
            # Missed: re-read the window first — waits (stop-aware) for a
            # minimized game, ends on a closed game / changed window
            # (reported).
            if self.stop_evt.is_set() or not self._refresh_geometry(True):
                return False
            if attempt == 0:
                print("  WARN: next-song click didn't land; retrying")
        if self.watcher.last_refusal == REFUSED_NOT_IN_FRONT:
            self._fail("  ERROR: another window was in front of the game "
                       "at the album wheel; stopping", MSG_GAME_NOT_IN_FRONT)
        else:
            self._fail("  ERROR: couldn't click the next song on the album "
                       "wheel; stopping", MSG_NEXT_SONG_FAILED)
        return False

    def _wait_for_album_page(self, timeout=60.0):
        """Poll up to `timeout` for the album page anchor. Worst-case
        blocker for stop-responsiveness (60s default), so the poll uses
        stop-aware sleep, and gives up at once if the game was closed
        (reported by _window_gone)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_evt.is_set():
                return False
            if self._window_gone():
                return False
            if self._on_album_page():
                return True
            if self.watcher.stop_wait(0.5):
                return False
        return False

    def _song_boundary(self):
        """Safe point between songs: honor pause, then re-read the game
        window (it may have been moved / resized during the last song or
        the pause). Returns False if the run should end — stop, or the
        window is gone (already reported)."""
        if self.stop_evt.is_set():
            return False
        self._await_resume()
        if self.stop_evt.is_set():
            return False
        return self._refresh_geometry(block=True)

    def _after_song(self):
        """Pause boundary + results-screen recovery + wait for the album
        page. Returns None once the album page is back, else
        RESULT_STOPPED / RESULT_FAILED."""
        # Pause may still be set — e.g. the song ended (and the end
        # watcher clicked through) while the bot was paused mid-song.
        # Wait at the boundary before clicking next-song.
        if not self._song_boundary():
            return self._halt_result()

        # The results screen can still be up here: a mid-song hand-off
        # that landed on the results screen, a resume that found the
        # song already over, or an end-watcher Select Song click that
        # didn't register. Click through if present.
        if not self._on_album_page():
            res = self._find_select_song()
            if res is not None:
                print("  clicking Select Song (results screen still up)")
                _, (cx, cy) = res
                if (not self.watcher.click_local(cx, cy)
                        and not self.stop_evt.is_set()):
                    print("  WARN: Select Song click didn't land; "
                          "waiting for the album page anyway")
                if self.watcher.stop_wait(2.0):
                    return RESULT_STOPPED

        if not self._wait_for_album_page(timeout=60.0):
            if self.stop_evt.is_set():
                return RESULT_STOPPED
            return self._fail("  WARN: never returned to album page; "
                              "stopping", MSG_NO_RETURN_TO_ALBUM)
        return None

    def _finish_wheel(self, songs_count):
        """End of a completed cycle. Final wheel reset: only when the
        user ran the full 12-song album. The 12th next-song click cycles
        the wheel back to the starting song slot. Partial runs leave the
        wheel where it is (on the last-played slot)."""
        if self.stop_evt.is_set():
            return RESULT_STOPPED
        if songs_count == ALBUM_SONG_COUNT:
            print("\nrolling wheel back to first song")
            if not self._next_song():
                return self._halt_result()
        if self.stop_evt.is_set():
            return RESULT_STOPPED
        return RESULT_COMPLETED

    # --- main loop ----------------------------------------------------------

    def _emit(self, update):
        if 'last_error' in update:
            self._last_error = update['last_error'] or ''
        if self.status_cb:
            try:
                self.status_cb(update)
            except Exception:
                pass

    def _fail(self, log_line, user_message=None):
        """Log + report an unrecoverable album error. Returns
        RESULT_FAILED so call sites can `return self._fail(...)`.
        A step that failed because the game was closed (click refused,
        page never came back) reports that instead of `user_message`,
        which would send the user looking for the wrong problem."""
        print(log_line)
        if self._window_gone():
            return RESULT_FAILED
        if user_message:
            self._emit({'last_error': user_message})
        return RESULT_FAILED

    def _halt_result(self):
        """Outcome for a step that returned "can't continue": stopped if
        the user pressed Stop, otherwise a failure that was already
        reported where it happened."""
        return RESULT_STOPPED if self.stop_evt.is_set() else RESULT_FAILED

    def _song_failed(self):
        """`song_player.play` returned False."""
        if self.stop_evt.is_set():
            return RESULT_STOPPED
        # SongPlayer reports a specific last_error for the failures it
        # recognises; only fall back to the generic one if it didn't.
        return self._fail("  aborting album (state machine broke; check "
                          "coords).",
                          None if self._last_error else MSG_SONG_FAILED)

    # --- album_progress (UI progress bar; see ui-data-contract.md) ----------
    #
    # status_cb({'album_progress': {index, total, difficulty,
    # difficulties, results}}): `index` is the 1-based position being
    # played (0 before the first), `results` has one entry per FINISHED
    # position: 'played' (>=1 difficulty actually played), 'skipped'
    # (every chosen difficulty skipped at Canorus) or 'failed'. A position
    # cut short by Stop isn't finished, so it gets no entry. Emitted at
    # run start, when each position starts, when each further difficulty
    # within it starts, and when it finishes. Every emit is a fresh copy,
    # so later appends can't mutate a snapshot the UI already holds.

    def _progress_reset(self, total):
        self._progress = {'index': 0, 'total': total,
                          'difficulty': self.difficulties[0],
                          'difficulties': list(self.difficulties),
                          'results': []}
        self._emit_progress()

    def _emit_progress(self):
        p = self._progress
        self._emit({'album_progress': {
            'index': p['index'], 'total': p['total'],
            'difficulty': p['difficulty'],
            'difficulties': list(p['difficulties']),
            'results': list(p['results'])}})

    def _progress_position(self, index, difficulty):
        self._progress['index'] = index
        self._progress['difficulty'] = difficulty
        self._emit_progress()

    def _progress_difficulty(self, difficulty):
        self._progress['difficulty'] = difficulty
        self._emit_progress()

    def _progress_finish(self, outcome):
        self._progress['results'].append(outcome)
        self._emit_progress()

    def _position_halted(self, result, position_open=True):
        """A started position ended early: record 'failed' unless it was
        a Stop (unfinished positions get no entry). Returns `result`."""
        if position_open and result == RESULT_FAILED:
            self._progress_finish('failed')
        return result

    def _await_resume(self):
        """Block while pause_evt is set; bail out if stop fires or the
        game is closed (reported; the caller's geometry re-read then
        ends the run). Called at safe boundaries between songs."""
        if not self.pause_evt.is_set():
            return
        self._emit({'state': 'paused'})
        print("  paused — waiting for resume")
        next_check = time.time() + WINDOW_CHECK_S
        while self.pause_evt.is_set() and not self.stop_evt.is_set():
            time.sleep(0.1)
            if time.time() >= next_check:
                next_check = time.time() + WINDOW_CHECK_S
                if self._window_gone():
                    return
        if not self.stop_evt.is_set():
            self._emit({'state': 'running'})
            print("  resumed")

    def run(self, songs_count=None):
        """Play the album. Returns RESULT_COMPLETED, RESULT_STOPPED, or
        RESULT_FAILED (reason logged + reported as last_error)."""
        if songs_count is None:
            songs_count = ALBUM_SONG_COUNT
        songs_count = max(1, min(int(songs_count), ALBUM_SONG_COUNT))
        self._progress_reset(songs_count)

        # The UI tags any Standalone→Album switch made while Standalone
        # was running as a mid-song hand-off, but the user may already
        # be back on the album page. Then there's no song to attach to —
        # start normally (album-page + canorus checks included).
        if self.mid_song_start and self._on_album_page():
            print("mid-song hand-off requested, but the album page is "
                  "showing — starting normally")
            self.mid_song_start = False

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
                self._emit({'last_error': MSG_NOT_ON_ALBUM_PAGE})
                return RESULT_FAILED

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
            # Single-difficulty: keep the legacy flat loop. One
            # next-song click between songs, plus a final one only on a
            # full 12-song run (rolls the wheel back to the first song);
            # partial runs leave the wheel at the last-played slot —
            # same rule as the multi-difficulty path / UI help text.
            self.difficulty = diffs[0]
            result = self._run_one_album(songs_count, self.mid_song_start)
        else:
            # All-difficulties: iterate song positions in the outer
            # loop, difficulties in the inner loop. Wheel only advances
            # between positions, not between difficulties at the same
            # position, so each song is played once per difficulty.
            result = self._run_all_difficulties(songs_count)

        if result != RESULT_COMPLETED and self.stop_evt.is_set():
            result = RESULT_STOPPED
        if result == RESULT_COMPLETED:
            print("\nAlbum complete")
        elif result == RESULT_STOPPED:
            print("\nAlbum stopped")
        else:
            print("\nAlbum aborted — see the error above")
        self._emit({'state': 'idle', 'fps': 0.0})
        return result

    def _run_one_album(self, songs_count, mid_song_first_iter):
        """Run one full album cycle at `self.difficulty`. Returns
        RESULT_COMPLETED if the cycle completed normally, RESULT_STOPPED
        if stop fired, RESULT_FAILED on an unrecoverable state error
        (already logged + reported).

        Wheel rule (matches `_run_all_difficulties` and the UI help
        text): one next-song click between songs — canorus skips
        included — plus one final click only when songs_count ==
        ALBUM_SONG_COUNT, which rolls the wheel back to the starting
        song. Partial runs leave the wheel at the last-played slot."""
        for i in range(songs_count):
            if self.stop_evt.is_set():
                print("\nstop requested — exiting album loop")
                return RESULT_STOPPED
            if not self._song_boundary():
                return self._halt_result()

            mid_song_iter = (i == 0 and mid_song_first_iter)

            print(f"\n--- Song {i+1}/{songs_count} "
                  f"[{self.difficulty}] ---")
            self._emit({'song': f'{i+1}/{songs_count}'})
            # One difficulty: position start == difficulty start.
            self._progress_position(i + 1, self.difficulty)
            skipped = False
            if not self.replay_canorus and not mid_song_iter:
                frame = self.watcher.grab()
                if self._is_canorus(frame):
                    print("  skip — already canorus")
                    skipped = True

            if not skipped:
                print("  playing")
                if not self.song_player.play(self.difficulty,
                                             skip_intro=mid_song_iter):
                    return self._position_halted(self._song_failed())
                halt = self._after_song()
                if halt is not None:
                    return self._position_halted(halt)
            self._progress_finish('skipped' if skipped else 'played')

            # Advance to the next song — not after the last one (see
            # the wheel rule above; _finish_wheel handles the 12th).
            if i < songs_count - 1:
                if not self._next_song():
                    return self._halt_result()

        return self._finish_wheel(songs_count)

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

        Returns RESULT_COMPLETED / RESULT_STOPPED / RESULT_FAILED like
        `_run_one_album`.
        """
        diffs = self.difficulties

        for i in range(songs_count):
            if self.stop_evt.is_set():
                print("\nstop requested — exiting album loop")
                return RESULT_STOPPED

            position_open = False
            played_any = False
            for d_idx, diff in enumerate(diffs):
                if not self._song_boundary():
                    return self._position_halted(self._halt_result(),
                                                 position_open)

                self.difficulty = diff
                mid_song_iter = (i == 0 and d_idx == 0
                                 and self.mid_song_start)

                print(f"\n--- Song {i+1}/{songs_count} [{diff}] ---")
                self._emit({'song': f'{i+1}/{songs_count} [{diff}]'})
                if not position_open:
                    self._progress_position(i + 1, diff)
                    position_open = True
                else:
                    self._progress_difficulty(diff)

                if not self.replay_canorus and not mid_song_iter:
                    frame = self.watcher.grab()
                    if self._is_canorus(frame):
                        print(f"  skip — already canorus at {diff}")
                        continue   # next difficulty; do NOT advance wheel

                print("  playing")
                if not self.song_player.play(self.difficulty, skip_intro=mid_song_iter):
                    return self._position_halted(self._song_failed())

                # Mid-song pause may have left the results screen up.
                halt = self._after_song()
                if halt is not None:
                    return self._position_halted(halt)
                played_any = True

                # Intentionally NO _next_song here — next iter of the
                # difficulty loop plays the same song at the next diff.

            self._progress_finish('played' if played_any else 'skipped')

            # All difficulties for this position done. Advance wheel to
            # the next position UNLESS this was the last one.
            if i < songs_count - 1:
                if not self._next_song():
                    return self._halt_result()

        return self._finish_wheel(songs_count)

    def close(self):
        if getattr(self, '_saved_mouse', None) is not None:
            try:
                _set_mouse_params(*self._saved_mouse)
                print("Pointer precision restored")
            except Exception as e:
                print(f"[warn] restore EPP: {e}")
            self._saved_mouse = None
        game = getattr(self, '_game', None)
        if game is not None:
            game.close()            # release the game's process handle
        # Also reached from __init__'s failure guard, so the controller
        # may not exist yet; only close one we opened ourselves.
        if (getattr(self, '_owns_controller', False)
                and getattr(self, 'controller', None) is not None):
            try:
                self.controller.close()
            except Exception:
                pass
            self._owns_controller = False


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
    parser.add_argument(
        '--backend', choices=('software', 'arduino'),
        help="Input backend. Defaults to INPUT_BACKEND_DEFAULT in "
             f"config.py (currently '{INPUT_BACKEND_DEFAULT}').")
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

    def _restore_timer():
        if timer_boosted:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    # Same backend choice as main.py: INPUT_BACKEND_DEFAULT (config.py)
    # unless --backend overrides it; ui_settings.json is the UI's, not
    # ours. Imported here, not at module top: ui_core imports this module.
    from ui_core import make_input_backend
    backend = args.backend or INPUT_BACKEND_DEFAULT
    print(f"Input backend: {backend}")
    try:
        controller = make_input_backend(backend)
    except SystemExit:
        # Older adapters sys.exit on a missing board.
        print(f"ERROR: {MSG_ARDUINO_UNAVAILABLE}")
        _restore_timer()
        sys.exit(1)
    except (RuntimeError, ImportError) as e:
        # ArduinoConnectionError (a RuntimeError) carries the user-facing
        # reason; ImportError = the backend's package isn't installed.
        print(f"ERROR: {e}")
        _restore_timer()
        sys.exit(1)

    # EPP save / restore is handled by AlbumRunner (so UI mode gets it
    # too). See AlbumRunner.__init__ for the rationale. The runner doesn't
    # own an injected backend, so we close it ourselves.
    try:
        runner = AlbumRunner(replay_canorus=replay, difficulty=difficulty,
                             controller=controller)
    except (RuntimeError, SystemExit) as e:
        # AlbumSetupError (game window not found / closed) or a missing
        # template — fail cleanly instead of a traceback.
        print(f"ERROR: {e}")
        controller.close()
        _restore_timer()
        sys.exit(1)
    result = RESULT_STOPPED
    try:
        result = runner.run(songs_count=args.songs)
    except KeyboardInterrupt:
        print("\nCtrl+C")
    finally:
        runner.close()
        controller.close()
        _restore_timer()
        print("Shutdown complete")
    if result == RESULT_FAILED:
        sys.exit(1)


if __name__ == '__main__':
    main()
