# pc_client/song_player.py
"""Single-song rhythm-minigame player for the album runner. Owns the
intro click chain (Go Perform → difficulty card → Begin Performance),
the `NoteDetector` + viz-thread lifecycle for one song, the end-of-
song watcher that clicks Select Song on detection, and mid-song
pause/resume that recycles the detector while keeping the watcher
alive.

Caller responsibilities (AlbumRunner owns these):
- Album-page navigation between songs (`_next_song`, post-pause
  results-screen recovery, `_wait_for_album_page`).
- Canorus skip and wheel-advance bookkeeping.
- EPP toggling for the run — see ScreenWatcher / `InputBackend`
  contract docs.
- Difficulty selection (passed per `play()` call so multi-difficulty
  cycles don't need to mutate shared state between songs).
"""
import threading
import time

from config import (ALBUM_BEGIN_PERFORMANCE_XY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_END_POLL_S, ALBUM_GO_PERFORM_XY,
                    REF_COLUMN_X, REF_HIT_LINE_Y)
from detector import NoteDetector
from standalone_runner import run_visualization


# 1080p reference ROIs owned by SongPlayer. `ROI_SELECT_SONG` is
# re-exported for album.py's post-pause results-screen check
# (`AlbumRunner._find_select_song`), which has to recover after a
# pause killed our watcher mid-song.
ROI_BEGIN_PERFORMANCE = (970, 975, 320, 75)
ROI_SELECT_SONG = (1260, 975, 260, 75)


class SongPlayer:
    def __init__(self, watcher, controller, hwnd, region, scale_x, scale_y,
                 stop_evt, pause_evt, debug_evt,
                 status_cb=None, dump_frame_fn=None):
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
                centers + hit-line Y to the live resolution.
            stop_evt / pause_evt / debug_evt: external events. Stop
                aborts. Pause stops the detector + viz at the next
                tick while the watcher stays alive (so a natural
                end-of-song during a pause is still handled).
            status_cb: optional `callable(dict)` — emits 'state' /
                'fps' updates for the UI status pill.
            dump_frame_fn: optional `callable(tag: str)` — invoked on
                error paths (e.g. "Go Perform click missed?") so the
                caller can write a debug PNG using its own
                path-naming convention. AlbumRunner passes its
                `_dump_frame` here.
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

    def play(self, difficulty, skip_intro=False):
        """Play one song at `difficulty`. Returns True on normal
        completion or stop, False on an unrecoverable state error
        (which prints a message + dumps a debug frame via
        `dump_frame_fn` so the caller can decide whether to abort
        the album)."""
        if not skip_intro:
            if not self._intro_chain(difficulty):
                return False
        else:
            print("  mid-song hand-off — skipping intro, attaching "
                  "detector to the song already in progress")
        return self._rhythm_phase()

    # ---- intro click chain -------------------------------------------------

    def _intro_chain(self, difficulty):
        """album page → difficulty selection → Begin Performance.
        Returns True if we land in the rhythm minigame, False on
        stop or unrecoverable state."""
        # album page → difficulty selection
        self.watcher.click_ref(*ALBUM_GO_PERFORM_XY)
        if self.stop_evt.is_set():
            return False
        if not self.watcher.wait_for('begin_perf', ROI_BEGIN_PERFORMANCE,
                                     timeout=5.0, present=True):
            if self.stop_evt.is_set():
                return False
            print("  ERROR: difficulty screen never appeared "
                  "(Go Perform click missed?). Aborting song.")
            self._dump('postclick_go_perform')
            return False

        # pick difficulty card
        dx, dy = ALBUM_DIFFICULTY_COORDS[difficulty]
        self.watcher.click_ref(dx, dy)
        if self.watcher.stop_wait(0.3):
            return False

        # start rhythm minigame; verify we left the diff screen
        self.watcher.click_ref(*ALBUM_BEGIN_PERFORMANCE_XY)
        if self.stop_evt.is_set():
            return False
        if not self.watcher.wait_for('begin_perf', ROI_BEGIN_PERFORMANCE,
                                     timeout=5.0, present=False):
            if self.stop_evt.is_set():
                return False
            print("  ERROR: still on difficulty screen after Begin "
                  "Performance click. Aborting song.")
            return False
        if self.watcher.stop_wait(0.8):
            return False
        return True

    # ---- rhythm + watcher + viz --------------------------------------------

    def _rhythm_phase(self):
        """Spawn detector + end-watcher + optional viz. Block until
        the song ends (watcher clicks Select Song) or stop fires.
        Handles mid-song pause by stopping the detector + viz while
        the watcher stays alive — a natural end during a pause still
        gets handled, and resume spins up a fresh detector for the
        same song."""
        column_centers = [int(x * self.scale_x) for x in REF_COLUMN_X]
        hit_line_y = int(REF_HIT_LINE_Y * self.scale_y)
        detector = NoteDetector(self.hwnd, column_centers, hit_line_y,
                                self.controller)
        detector.start()

        viz_thread, viz_stop = self._start_viz(detector, column_centers,
                                               hit_line_y)

        watcher_evt = threading.Event()
        end_watcher_thread = threading.Thread(
            target=self._end_watcher, args=(watcher_evt,), daemon=True)
        end_watcher_thread.start()

        try:
            while not watcher_evt.wait(0.5):
                if self.stop_evt.is_set():
                    break
                if not end_watcher_thread.is_alive():
                    print("  WARN: watcher thread died "
                          "(see traceback above); aborting song")
                    break

                if self.pause_evt.is_set():
                    # --- mid-song pause ---
                    # Stop detector (releases keys) and viz, but keep
                    # the watcher alive so it can detect end-of-song.
                    print("  paused mid-song — releasing keys")
                    detector.stop()
                    viz_stop.set()
                    if viz_thread is not None:
                        viz_thread.join(timeout=2.0)
                        viz_thread = None
                    self._emit({'state': 'paused'})

                    # Idle until resume, stop, or song-end.
                    while (self.pause_evt.is_set()
                           and not self.stop_evt.is_set()
                           and not watcher_evt.is_set()):
                        time.sleep(0.1)

                    if self.stop_evt.is_set() or watcher_evt.is_set():
                        break

                    # --- resume: fresh detector + viz for the same song
                    print("  resumed — restarting detector")
                    self._emit({'state': 'running'})
                    detector = NoteDetector(self.hwnd, column_centers,
                                            hit_line_y, self.controller)
                    detector.start()
                    viz_thread, viz_stop = self._start_viz(
                        detector, column_centers, hit_line_y)
        finally:
            watcher_evt.set()
            end_watcher_thread.join(timeout=2.0)
            detector.stop()
            viz_stop.set()
            if viz_thread is not None:
                viz_thread.join(timeout=2.0)

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
                    pin_console=False)
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
        `ALBUM_END_POLL_S` seconds."""
        while not local_stop.is_set() and not self.stop_evt.is_set():
            end = time.time() + ALBUM_END_POLL_S
            while time.time() < end:
                if local_stop.wait(0.2):
                    return
                if self.stop_evt.is_set():
                    return
            res = self._find_select_song()
            if res is not None:
                _, (cx, cy) = res
                self.watcher.click_local(cx, cy)
                local_stop.set()
                return

    def _find_select_song(self, frame=None):
        if frame is None:
            frame = self.watcher.grab()
        roi = self.watcher.ref_rect_to_local(*ROI_SELECT_SONG)
        return self.watcher.match(frame, 'select_song', roi=roi)

    # ---- internal helpers --------------------------------------------------

    def _emit(self, update):
        if self.status_cb:
            try:
                self.status_cb(update)
            except Exception:
                pass

    def _dump(self, tag):
        if self.dump_frame_fn is None:
            return
        try:
            self.dump_frame_fn(tag)
        except Exception as e:
            print(f"[song-player] dump_frame_fn({tag!r}) failed: {e}")
