# pc_client/main.py
"""Standalone CLI entrypoint. Use the UI (`ui.py`) for the supported
front-end; this file is the bare-bones detector loop kept around for
quick iteration during tuning.

The reusable orchestration (`run_standalone`, `run_visualization`)
lives in `standalone_runner.py` — that's what the UI calls. CLI's
`main()` builds its own detector + DEBUG_MODE branch so Ctrl+C and
the legacy console-pin headless loop stay independent of the UI's
control flow.
"""
import cv2
import ctypes
import sys
import time

from config import (DEBUG_MODE, KEY_POLL_DELAY_S, Y_SAMPLE_OFFSETS,
                    INPUT_BACKEND_DEFAULT)
from detector import NoteDetector
from game_window import (MSG_WINDOW_CHANGED, MSG_WINDOW_CLOSED,
                         find_game_window, auto_detect)
from standalone_runner import (GEOMETRY_POLL_S, VIZ_EXIT_GEOMETRY,
                               VIZ_EXIT_WINDOW_CHANGED, VIZ_EXIT_WINDOW_GONE,
                               WINDOW_CHANGED, WINDOW_GONE, GeometryTracker,
                               run_visualization, layout_changed)
from system_setup import boost_timer, restore_timer, set_dpi_aware
from ui_core import make_input_backend

try:
    from controller import ArduinoConnectionError
except ImportError:
    ArduinoConnectionError = RuntimeError


def main():
    print("Initializing program")
    print()

    # Make this process DPI-aware so GDI GetPixel coords match the
    # client-rect-derived coords used by the calibration logic.
    set_dpi_aware()

    # Boost system timer resolution to 1ms so time.sleep(0.005) actually
    # sleeps ~5ms instead of the Windows default ~15.6ms quantum. Without
    # this the per-key poll rate collapses and the whole point of fast
    # detection is lost.
    timer_boosted = boost_timer()

    def _fail_exit():
        if timer_boosted:
            restore_timer()
        sys.exit(1)

    detected = auto_detect()
    if detected is None:
        _fail_exit()
    capture_region, column_centers, hit_line_y = detected

    # Re-fetch hwnd for the detector's GDI calls. find_game_window is cheap
    # (single FindWindowW) and auto_detect already validated the window.
    hwnd = find_game_window()
    if not hwnd:
        print("ERROR: lost game window after auto-detect.")
        _fail_exit()
    print()

    # Honor INPUT_BACKEND_DEFAULT so the CLI doesn't silently pin to
    # Arduino when the user has switched the default to software in
    # config.py. UI users get their per-session choice from
    # ui_settings.json; the CLI deliberately doesn't read that file.
    try:
        controller = make_input_backend(INPUT_BACKEND_DEFAULT)
    except (ArduinoConnectionError, SystemExit) as e:
        # ArduinoConnectionError's text is user-facing; SystemExit covers
        # older adapters that sys.exit on a missing board.
        print(f"ERROR: {e}")
        _fail_exit()
    if controller is None:
        print("ERROR: no input backend available.")
        _fail_exit()
    print("Note: keys are only sent while Genshin is the focused window.")

    # Windowed Genshin can be moved / resized while we run. A move needs
    # nothing (the detector reads client-relative pixels; the viz retargets
    # its capture itself); a resize changes the lane layout, so rebuild the
    # detector for it. The tracker holds the game's window + PID + process
    # handle for the run: a closed game or a changed window (destroyed /
    # recycled — never followed) ends the CLI with a message.
    tracker = GeometryTracker(hwnd, capture_region, column_centers,
                              hit_line_y, tag='[cli]')
    detector = NoteDetector(hwnd, column_centers, hit_line_y, controller,
                            pid=tracker.pid)

    print(f"Starting per-key detection threads "
          f"(poll {KEY_POLL_DELAY_S*1000:.0f}ms, strip {Y_SAMPLE_OFFSETS})")
    detector.start()

    def _rebuild_detector():
        nonlocal detector
        detector.stop()
        _, cols, hit = tracker.current
        detector = NoteDetector(tracker.hwnd, cols, hit, controller,
                                pid=tracker.pid)
        detector.start()

    # Why the run ended on the game window (closed / changed): shut down
    # cleanly, print it, then exit 1.
    lost = None
    lost_by_reason = {VIZ_EXIT_WINDOW_GONE: MSG_WINDOW_CLOSED,
                      VIZ_EXIT_WINDOW_CHANGED: MSG_WINDOW_CHANGED,
                      WINDOW_GONE: MSG_WINDOW_CLOSED,
                      WINDOW_CHANGED: MSG_WINDOW_CHANGED}
    try:
        if DEBUG_MODE:
            print("DEBUG_MODE on - visualization window will open. Close it "
                  "(X) or press Ctrl+C to exit.")
            while True:
                region, cols, hit = tracker.current
                reason = run_visualization(region, cols, hit, detector,
                                           pin_console=True,
                                           game_hwnd=tracker.hwnd,
                                           geometry_poll=tracker.poll)
                if reason in (VIZ_EXIT_WINDOW_GONE, VIZ_EXIT_WINDOW_CHANGED):
                    lost = lost_by_reason[reason]
                    break
                if reason != VIZ_EXIT_GEOMETRY:
                    break
                _rebuild_detector()
        else:
            print("DEBUG_MODE off - running headless. Ctrl+C to exit.")
            while True:
                hwnd_console = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd_console:
                    # 0x13 = NOSIZE|NOMOVE|NOACTIVATE: pin topmost without
                    # pulling focus off the game (detector pauses then).
                    ctypes.windll.user32.SetWindowPos(hwnd_console, -1, 0, 0, 0, 0, 0x13)
                old = tracker.current
                new = tracker.poll()
                if new in (WINDOW_GONE, WINDOW_CHANGED):
                    lost = lost_by_reason[new]
                    break
                if new is not None and layout_changed(old, new):
                    _rebuild_detector()
                time.sleep(GEOMETRY_POLL_S)
    except KeyboardInterrupt:
        print("\nCtrl+C received")
    finally:
        if lost:
            print(f"ERROR: {lost}")
        print("Shutting down")
        detector.stop()
        controller.close()
        tracker.close()
        cv2.destroyAllWindows()
        if timer_boosted:
            restore_timer()
        print("Shutdown complete")
    if lost:
        sys.exit(1)


if __name__ == '__main__':
    main()
