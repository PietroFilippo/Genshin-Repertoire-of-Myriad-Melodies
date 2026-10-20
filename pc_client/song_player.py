# pc_client/song_player.py
"""Single-song rhythm-minigame player for the album runner. Owns the
intro click chain (Go Perform → difficulty card → Begin Performance),
the `NoteDetector` + viz-thread lifecycle for one song, the end-of-
song watcher that clicks Select Song on detection, and mid-song
pause/resume that recycles the detector while keeping the watcher
alive.

Pause is honored at every navigation boundary of the intro chain and
before the detector starts (stop-aware — Stop still wins within
~100 ms). After a resume the screen is re-identified before the next
click, because the user may have navigated in-game while paused.

The mid-song hand-off (`play(skip_intro=True)`) is verified, not
trusted: if the rhythm minigame isn't on screen but the album page or
difficulty screen is, the normal intro runs from there instead of
waiting forever for a results screen that will never come.

Caller responsibilities (AlbumRunner owns these):
- Album-page navigation between songs (`_next_song`, post-pause
  results-screen recovery, `_wait_for_album_page`).
- Canorus skip and wheel-advance bookkeeping.
- EPP toggling for the run — see ScreenWatcher / `InputBackend`
  contract docs.
- Difficulty selection (passed per `play()` call so multi-difficulty
  cycles don't need to mutate shared state between songs).
- Window-geometry tracking (`refresh_geometry_fn`): re-reading the game
  window, rescaling templates and calling `update_geometry` here.
"""
import threading
import time

from config import (ALBUM_BEGIN_PERFORMANCE_XY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_END_POLL_S, ALBUM_GO_PERFORM_XY,
                    PIXEL_THRESHOLD, Y_SAMPLE_OFFSETS)
from detector import NoteDetector
from game_window import same_hwnd, scaled_layout
from standalone_runner import run_visualization


# 1080p reference ROIs owned by SongPlayer. `ROI_SELECT_SONG` is
# re-exported for album.py's results-screen recovery
# (`AlbumRunner._find_select_song`, used by `_after_song`): a hand-off
# that landed on the results screen, a resume that found the song
# already over, or an end-watcher click that didn't register. (Our end
# watcher stays alive during a pause.)
ROI_BEGIN_PERFORMANCE = (970, 975, 320, 75)
ROI_SELECT_SONG = (1260, 975, 260, 75)

# "Go Perform" pill (album-page anchor + click target). ROIs need to be
# wider than `tpl.shape[1]` for matchTemplate's sliding window — leaving
# generous horizontal slack so a few-pixel UI shift doesn't push the
# template clean off the ROI edge (cost a debugging session to learn).
# Lives here (re-exported to album.py) because the hand-off / resume
# screen identification needs it too.
ROI_GO_PERFORM = (1530, 975, 380, 75)

# Screens `_identify_screen` can recognise.
SCREEN_ALBUM = 'album'            # country album page ("Go Perform" pill)
SCREEN_DIFFICULTY = 'difficulty'  # difficulty cards ("Begin Performance")
SCREEN_RESULTS = 'results'        # post-song results ("Select Song")
SCREEN_RHYTHM = 'rhythm'          # rhythm minigame (Hu Tao hit line lit)
# Internal intro step: difficulty card clicked, Begin Performance next.
_STEP_BEGIN = 'begin'

# How long to look for a recognisable screen on a hand-off / after a
# resume. Covers the brief transitions (song intro, results fade-in).
SCREEN_DETECT_TIMEOUT_S = 3.0

# Rhythm-minigame check: lanes whose hit-line samples all read bright
# (>= PIXEL_THRESHOLD) — the detector's own idle signal. Half the lanes
# tolerates notes covering a few columns in the sampled frame.
RHYTHM_MIN_LIT_COLUMNS = 3

# Mid-song window tracking cadence (non-blocking geometry re-read).
GEOMETRY_POLL_S = 0.5

# `_pause_gate` results.
_GATE_CLEAR = 'clear'
_GATE_RESUMED = 'resumed'
_GATE_STOP = 'stop'

# User-facing `last_error` strings.
MSG_GO_PERFORM_FAILED = ("Couldn't open the song's difficulty screen — make "
                         "sure a country album page is showing, then press "
                         "Start.")
MSG_BEGIN_FAILED = ("The song didn't start after Begin Performance — check "
                    "the game, then press Start.")
MSG_CLICK_FAILED = ("The mouse couldn't reach the {what} button — keep the "
                    "game window fully on screen, then press Start.")
MSG_NO_RHYTHM = ("Rhythm minigame not detected — open a country album page, "
                 "then press Start.")
MSG_UNKNOWN_SCREEN = ("Couldn't recognise the game screen after resuming — "
                      "open a country album page, then press Start.")


class SongPlayer:
    def __init__(self, watcher, controller, hwnd, region, scale_x, scale_y,
                 stop_evt, pause_evt, debug_evt,
                 status_cb=None, dump_frame_fn=None,
                 refresh_geometry_fn=None):
        """
        Args:
            watcher: `ScreenWatcher` — clicks, match, waits. Shared
                with AlbumRunner so the watcher's `stop_evt` lines up
                with ours.
            controller: `InputBackend` — handed to `NoteDetector` for
                rhythm-key emission.
            hwnd: Genshin window handle (GDI `GetPixel` target for
                the detector's per-key polls).
            region, scale_x, scale_y: capture geometry. `region` goes
                to the viz thread; the scales rescale 1080p column
                centers + hit-line Y to the live resolution. Updated
                in place via `update_geometry`.
            stop_evt / pause_evt / debug_evt: external events. Stop
                aborts. Pause stops the detector + viz at the next
                tick while the watcher stays alive (so a natural
                end-of-song during a pause is still handled), and
                holds the intro chain at its next click.
            status_cb: optional `callable(dict)` — emits 'state' /
                'fps' / 'last_error' updates for the UI status card.
            dump_frame_fn: optional `callable(tag: str)` — invoked on
                error paths (e.g. "Go Perform click missed?") so the
                caller can write a debug PNG using its own
                path-naming convention. AlbumRunner passes its
                `_dump_frame` here.
            refresh_geometry_fn: optional `callable(block: bool) ->
                bool` — re-read the game window and push any change
                back via `update_geometry` (+ the watcher). block=True
                may wait (stop-aware) for a minimized game; block=False
                is a cheap single read (a minimized game keeps the last
                geometry). Returns False iff the run should end (stop,
                or the game window is gone — already reported), for
                either mode. AlbumRunner passes its `_refresh_geometry`.
        """
        self.watcher = watcher
        self.controller = controller
        self.hwnd = hwnd
        self.region = region
        self.scale_x = scale_x
        self.scale_y = scale_y
        self.stop_evt = stop_evt
        self.pause_evt = pause_evt
        self.debug_evt = debug_evt
        self.status_cb = status_cb
        self.dump_frame_fn = dump_frame_fn
        self.refresh_geometry_fn = refresh_geometry_fn

    def update_geometry(self, region, scale_x, scale_y):
        """New capture geometry (game window moved / resized). Picked
        up by `_rhythm_phase` on its next tick: a resize rebuilds the
        detector for the new lane layout, a move restarts the viz."""
        self.region = dict(region)
        self.scale_x = scale_x
        self.scale_y = scale_y

    def update_hwnd(self, hwnd):
        """The game recreated its window (fullscreen toggle, display-mode
        change): new HWND for the detector's GDI reads and focus gate.
        `_rhythm_phase` rebuilds the detector (+ viz) on its next tick."""
        self.hwnd = hwnd

    def play(self, difficulty, skip_intro=False):
        """Play one song at `difficulty`. Returns True on normal
        completion or stop, False on an unrecoverable state error
        (which prints a message, reports `last_error` and dumps a
        debug frame via `dump_frame_fn` so the caller can decide
        whether to abort the album). A stop during the intro also
        returns False — callers check `stop_evt` to tell them apart.

        `skip_intro=True` is the mid-song hand-off. The screen is
        checked first: rhythm minigame → attach the detector; album
        page / difficulty screen → run the intro from there; results
        screen → return True (the song already ended; the caller's
        results-screen recovery clicks Select Song); anything else →
        fail with `last_error`."""
        if skip_intro:
            state = self._resolve_handoff()
            if state is None:
                return False
        else:
            state = SCREEN_ALBUM
        state = self._advance_to_song(difficulty, state)
        if state == SCREEN_RHYTHM:
            return self._rhythm_phase()
        if state == SCREEN_RESULTS:
            return True
        return False

    # ---- screen identification ---------------------------------------------

    def _resolve_handoff(self):
        """Verify the mid-song hand-off. Returns the screen to continue
        from, or None on stop / failure (failure reported)."""
        state = self._identify_screen(SCREEN_DETECT_TIMEOUT_S)
        if self.stop_evt.is_set():
            return None
        if state == SCREEN_RHYTHM:
            print("  mid-song hand-off — skipping intro, attaching "
                  "detector to the song already in progress")
        elif state == SCREEN_ALBUM:
            print("  mid-song hand-off requested, but the album page is "
                  "showing — running the normal intro")
        elif state == SCREEN_DIFFICULTY:
            print("  mid-song hand-off requested, but the difficulty "
                  "screen is showing — continuing the intro from there")
        elif state == SCREEN_RESULTS:
            print("  mid-song hand-off: the song already finished "
                  "(results screen showing)")
        else:
            print("  ERROR: mid-song hand-off requested but the rhythm "
                  "minigame isn't on screen (no album page / difficulty "
                  "/ results screen either). Aborting song.")
            self._fail(MSG_NO_RHYTHM)
            self._dump('handoff_no_rhythm')
            return None
        return state

    def _identify_screen(self, timeout):
        """Poll (stop-aware) until the current screen is recognisable,
        up to `timeout` seconds. Returns a SCREEN_* constant, or None if
        stopped / nothing recognisable showed up."""
        end = time.time() + timeout
        while True:
            if self.stop_evt.is_set():
                return None
            state = self._classify(self.watcher.grab())
            if state is not None:
                return state
            if time.time() >= end:
                return None
            if self.watcher.stop_wait(0.2):
                return None

    def _classify(self, frame):
        # Templates first: they're specific, so a bright patch on one of
        # these screens can't be mistaken for the minigame's hit line.
        w = self.watcher
        if w.match(frame, 'go_perform',
                   roi=w.ref_rect_to_local(*ROI_GO_PERFORM)) is not None:
            return SCREEN_ALBUM
        if w.match(frame, 'begin_perf',
                   roi=w.ref_rect_to_local(*ROI_BEGIN_PERFORMANCE)) is not None:
            return SCREEN_DIFFICULTY
        if self._find_select_song(frame) is not None:
            return SCREEN_RESULTS
        if self._rhythm_minigame_visible(frame):
            return SCREEN_RHYTHM
        return None

    def _rhythm_minigame_visible(self, frame):
        """Same sensor the detector uses (Hu Tao theme): an idle lane's
        hit-line background reads blue >= PIXEL_THRESHOLD at every
        sample offset. `frame` is the BGR capture-region grab, so
        channel 0 is blue and coords are client-relative."""
        column_centers, hit_line_y = scaled_layout(self.scale_x,
                                                   self.scale_y)
        h, w = frame.shape[:2]
        lit = 0
        for cx in column_centers:
            if not 0 <= cx < w:
                continue
            vals = [int(frame[hit_line_y + dy, cx, 0])
                    for dy in Y_SAMPLE_OFFSETS
                    if 0 <= hit_line_y + dy < h]
            if vals and min(vals) >= PIXEL_THRESHOLD:
                lit += 1
        return lit >= RHYTHM_MIN_LIT_COLUMNS

    # ---- intro click chain -------------------------------------------------

    def _advance_to_song(self, difficulty, state):
        """album page → difficulty selection → Begin Performance,
        starting from `state`. Returns SCREEN_RHYTHM once we land in the
        rhythm minigame (or SCREEN_RESULTS if a resume found the song
        already over), None on stop or unrecoverable state (reported).

        Pause is checked before every click. After a resume the screen
        is re-identified and the chain continues from whatever is
        actually showing — the user may have navigated in-game while
        paused, and a blind click could land on the wrong screen. Each
        non-resume step strictly advances or fails, so this can't loop
        without user input."""
        while state not in (SCREEN_RHYTHM, SCREEN_RESULTS):
            if state is None or self.stop_evt.is_set():
                return None
            gate = self._pause_gate()
            if gate == _GATE_STOP:
                return None
            if gate == _GATE_RESUMED:
                state = self._identify_screen(SCREEN_DETECT_TIMEOUT_S)
                if state is None:
                    if self.stop_evt.is_set():
                        return None
                    print("  ERROR: couldn't recognise the screen after "
                          "resume. Aborting song.")
                    self._fail(MSG_UNKNOWN_SCREEN)
                    self._dump('resume_unknown_screen')
                    return None
                continue
            if state == SCREEN_ALBUM:
                state = self._step_go_perform()
            elif state == SCREEN_DIFFICULTY:
                state = self._step_pick_difficulty(difficulty)
            elif state == _STEP_BEGIN:
                state = self._step_begin_performance()
            else:
                return None
        return state

    def _step_go_perform(self):
        # album page → difficulty selection
        if not self.watcher.click_ref(*ALBUM_GO_PERFORM_XY):
            return self._click_failed('Go Perform', 'click_go_perform')
        if not self.watcher.wait_for('begin_perf', ROI_BEGIN_PERFORMANCE,
                                     timeout=5.0, present=True):
            if self.stop_evt.is_set():
                return None
            print("  ERROR: difficulty screen never appeared "
                  "(Go Perform click missed?). Aborting song.")
            self._fail(MSG_GO_PERFORM_FAILED)
            self._dump('postclick_go_perform')
            return None
        return SCREEN_DIFFICULTY

    def _step_pick_difficulty(self, difficulty):
        # pick difficulty card
        dx, dy = ALBUM_DIFFICULTY_COORDS[difficulty]
        if not self.watcher.click_ref(dx, dy):
            return self._click_failed(f'{difficulty.capitalize()} card',
                                      'click_difficulty')
        if self.watcher.stop_wait(0.3):
            return None
        return _STEP_BEGIN

    def _step_begin_performance(self):
        # start rhythm minigame; verify we left the diff screen
        if not self.watcher.click_ref(*ALBUM_BEGIN_PERFORMANCE_XY):
            return self._click_failed('Begin Performance',
                                      'click_begin_performance')
        if not self.watcher.wait_for('begin_perf', ROI_BEGIN_PERFORMANCE,
                                     timeout=5.0, present=False):
            if self.stop_evt.is_set():
                return None
            print("  ERROR: still on difficulty screen after Begin "
                  "Performance click. Aborting song.")
            self._fail(MSG_BEGIN_FAILED)
            self._dump('postclick_begin_performance')
            return None
        if self.watcher.stop_wait(0.8):
            return None
        return SCREEN_RHYTHM

    def _click_failed(self, what, dump_tag):
        """A click the watcher refused to issue (stop, or the cursor
        never reached the target). Treated like a missed click."""
        if self.stop_evt.is_set():
            return None
        print(f"  ERROR: {what} click skipped — the cursor never reached "
              f"the button. Aborting song.")
        self._fail(MSG_CLICK_FAILED.format(what=what))
        self._dump(dump_tag)
        return None

    def _pause_gate(self):
        """Navigation-boundary pause. Returns _GATE_CLEAR when not
        paused, _GATE_RESUMED after a pause ended (geometry re-read),
        _GATE_STOP if stop fired (or the window is gone — also checked
        every GEOMETRY_POLL_S while paused). The wait goes through
        `watcher.stop_wait`, so Stop wins within ~100 ms."""
        if not self.pause_evt.is_set():
            return _GATE_STOP if self.stop_evt.is_set() else _GATE_CLEAR
        self._emit({'state': 'paused'})
        print("  paused during song intro — waiting for resume")
        next_geom = time.time() + GEOMETRY_POLL_S
        while self.pause_evt.is_set():
            if self.watcher.stop_wait(0.1):
                return _GATE_STOP
            if time.time() >= next_geom:
                next_geom = time.time() + GEOMETRY_POLL_S
                if not self._refresh_geometry(block=False):
                    return _GATE_STOP      # game closed (reported)
        if self.stop_evt.is_set():
            return _GATE_STOP
        print("  resumed")
        self._emit({'state': 'running'})
        if not self._refresh_geometry(block=True):
            return _GATE_STOP
        return _GATE_RESUMED

    # ---- rhythm + watcher + viz --------------------------------------------

    def _rhythm_phase(self):
        """Spawn end-watcher + detector + optional viz. Block until
        the song ends (watcher clicks Select Song) or stop fires.
        Handles pause (mid-song, or before the detector ever started)
        by stopping the detector + viz while the watcher stays alive —
        a natural end during a pause still gets handled, and resume
        spins up a fresh detector for the same song.

        The game window is re-read every GEOMETRY_POLL_S (non-blocking)
        so the end-watcher captures / clicks the right rectangle if the
        window is moved or resized. A resize changes the lane layout and
        rebuilds the detector; a move only restarts the viz (the
        detector reads client-relative pixels, so a move doesn't affect
        it). A minimized game keeps the last-known geometry; a closed one
        (`refresh_geometry_fn(False)` returns False, while playing or
        paused) ends the song at once. Returns False only if the game
        window disappeared (already reported); True on song end or
        stop."""
        watcher_evt = threading.Event()
        end_watcher_thread = threading.Thread(
            target=self._end_watcher, args=(watcher_evt,), daemon=True)
        end_watcher_thread.start()

        st = {'detector': None, 'layout': None, 'hwnd': None,
              'viz_thread': None, 'viz_stop': threading.Event(),
              'viz_region': None}

        def stop_viz():
            st['viz_stop'].set()
            if st['viz_thread'] is not None:
                st['viz_thread'].join(timeout=2.0)
                st['viz_thread'] = None

        def start_viz():
            column_centers, hit_line_y = st['layout']
            st['viz_thread'], st['viz_stop'] = self._start_viz(
                st['detector'], column_centers, hit_line_y)
            st['viz_region'] = dict(self.region)

        def stop_detection():
            if st['detector'] is not None:
                st['detector'].stop()
                st['detector'] = None
            stop_viz()

        def start_detection(layout):
            column_centers, hit_line_y = layout
            st['layout'] = layout
            st['hwnd'] = self.hwnd
            st['detector'] = NoteDetector(self.hwnd, column_centers,
                                          hit_line_y, self.controller)
            st['detector'].start()
            start_viz()

        window_lost = False
        try:
            while True:
                if self.stop_evt.is_set() or watcher_evt.is_set():
                    break
                if not end_watcher_thread.is_alive():
                    print("  WARN: watcher thread died "
                          "(see traceback above); aborting song")
                    break

                if self.pause_evt.is_set():
                    # --- pause (mid-song, or before the first start) ---
                    # Stop detector (releases keys) and viz, but keep
                    # the watcher alive so it can detect end-of-song.
                    if st['detector'] is not None:
                        print("  paused mid-song — releasing keys")
                    else:
                        print("  paused before the song started — "
                              "holding input")
                    stop_detection()
                    self._emit({'state': 'paused'})

                    # Idle until resume, stop, or song-end. Keep
                    # tracking the window so the still-running watcher
                    # follows a game the user moves while paused.
                    next_geom = time.time() + GEOMETRY_POLL_S
                    while (self.pause_evt.is_set()
                           and not self.stop_evt.is_set()
                           and not watcher_evt.is_set()):
                        time.sleep(0.1)
                        if time.time() >= next_geom:
                            if not self._refresh_geometry(block=False):
                                # Game closed while paused (reported).
                                window_lost = not self.stop_evt.is_set()
                                break
                            next_geom = time.time() + GEOMETRY_POLL_S

                    if (window_lost or self.stop_evt.is_set()
                            or watcher_evt.is_set()):
                        break

                    # --- resume: fresh detector + viz for the same song
                    print("  resumed — restarting detector")
                    self._emit({'state': 'running'})
                    if not self._refresh_geometry(block=True):
                        window_lost = not self.stop_evt.is_set()
                        break
                    continue

                if st['detector'] is not None:
                    if not self._refresh_geometry(block=False):
                        # Game closed mid-song (already reported) — the
                        # finally below stops the detector, releasing
                        # every held key.
                        window_lost = not self.stop_evt.is_set()
                        break
                layout = scaled_layout(self.scale_x, self.scale_y)
                # A recreated game window (new HWND, via update_hwnd)
                # needs a fresh detector too: the old one reads — and
                # gates focus on — a handle that no longer exists.
                recreated = (st['detector'] is not None
                             and not same_hwnd(self.hwnd, st['hwnd']))
                if (st['detector'] is None or layout != st['layout']
                        or recreated):
                    if recreated:
                        print("  game window recreated — rebuilding the "
                              "detector for the new window")
                    elif st['detector'] is not None:
                        print("  game window resized — rebuilding the "
                              "detector for the new lane layout")
                    stop_detection()
                    # Teardown joins threads (up to ~0.5 s per lane +
                    # 2 s viz) — Stop, Pause or the song ending may have
                    # landed meanwhile. Re-check at the loop top instead
                    # of starting a detector that would press keys after
                    # Stop.
                    if (self.stop_evt.is_set() or self.pause_evt.is_set()
                            or watcher_evt.is_set()):
                        continue
                    start_detection(layout)
                elif (st['viz_thread'] is not None
                      and self.region != st['viz_region']):
                    stop_viz()
                    if (self.stop_evt.is_set() or self.pause_evt.is_set()
                            or watcher_evt.is_set()):
                        continue
                    start_viz()

                watcher_evt.wait(GEOMETRY_POLL_S)
        finally:
            watcher_evt.set()
            end_watcher_thread.join(timeout=2.0)
            stop_detection()

        if window_lost:
            return False
        self.watcher.stop_wait(2.0)
        return True

    def _start_viz(self, detector, column_centers, hit_line_y):
        """Spin up a viz thread for the given detector. Returns
        `(thread, stop_event)` or `(None, stop_event)` when
        `debug_evt` is None. Used for initial start and after each
        mid-song pause/resume cycle."""
        vs = threading.Event()
        if self.debug_evt is None:
            return None, vs
        cap = {'top': self.region['top'], 'left': self.region['left'],
               'width': self.region['width'],
               'height': self.region['height']}

        def _worker():
            try:
                run_visualization(
                    cap, column_centers, hit_line_y, detector,
                    stop_evt=vs, debug_evt=self.debug_evt,
                    fps_callback=(lambda f: self._emit({'fps': f}))
                                  if self.status_cb else None,
                    pin_console=False, game_hwnd=self.hwnd)
            except Exception as e:
                print(f"[viz] worker died: {e}")

        vt = threading.Thread(target=_worker, daemon=True,
                              name='album-viz')
        vt.start()
        return vt, vs

    def _end_watcher(self, local_stop):
        """Poll for the 'Select Song' pill at the post-song results
        screen. Click it on detection and signal the caller via
        `local_stop`. Cancellable via global `stop_evt` too — the
        inner wait ticks at 0.2s so the watcher reacts within ~200ms
        while the actual template match runs every
        `ALBUM_END_POLL_S` seconds. A click the watcher refused (cursor
        didn't converge) is retried on the next poll."""
        while not local_stop.is_set() and not self.stop_evt.is_set():
            end = time.time() + ALBUM_END_POLL_S
            while time.time() < end:
                if local_stop.wait(0.2):
                    return
                if self.stop_evt.is_set():
                    return
            if self._click_select_song_if_shown():
                local_stop.set()
                return

    def _click_select_song_if_shown(self, max_recaptures=3):
        """Match Select Song and click it. Returns True once clicked.

        The album thread can swap the watcher's geometry (window moved /
        resized) between our grab and our click; the match's local
        coords would then be applied to the new window — a resize could
        put the click outside the game. Tag the capture with the
        watcher's geometry generation (read BEFORE grabbing), let
        `click_local` refuse a stale click, and re-capture right away."""
        for _ in range(max_recaptures):
            gen = self.watcher.generation
            res = self._find_select_song()
            if res is None:
                return False
            _, (cx, cy) = res
            if self.watcher.click_local(cx, cy, generation=gen):
                return True
            if self.stop_evt.is_set():
                return False
            if self.watcher.generation != gen:
                print("  game window moved/resized before the Select Song "
                      "click — re-capturing")
                continue
            print("  WARN: Select Song click didn't land — retrying "
                  "on the next poll")
            return False
        return False

    def _find_select_song(self, frame=None):
        if frame is None:
            frame = self.watcher.grab()
        roi = self.watcher.ref_rect_to_local(*ROI_SELECT_SONG)
        return self.watcher.match(frame, 'select_song', roi=roi)

    # ---- internal helpers --------------------------------------------------

    def _refresh_geometry(self, block):
        if self.refresh_geometry_fn is None:
            return True
        try:
            return bool(self.refresh_geometry_fn(block))
        except Exception as e:
            print(f"[song-player] geometry refresh failed: {e}")
            return True

    def _emit(self, update):
        if self.status_cb:
            try:
                self.status_cb(update)
            except Exception:
                pass

    def _fail(self, message):
        # A step that failed because the game was closed (click refused,
        # screen never appeared) was reported as that by
        # `refresh_geometry_fn` (returns False) — don't bury it under a
        # misleading "click failed" / "check the game".
        if not self._refresh_geometry(block=False):
            return
        self._emit({'last_error': message})

    def _dump(self, tag):
        if self.dump_frame_fn is None:
            return
        try:
            self.dump_frame_fn(tag)
        except Exception as e:
            print(f"[song-player] dump_frame_fn({tag!r}) failed: {e}")
