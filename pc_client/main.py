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
from game_window import find_game_window, auto_detect
from standalone_runner import run_visualization
from system_setup import boost_timer, restore_timer, set_dpi_aware
from ui_core import make_input_backend


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

    detected = auto_detect()
    if detected is None:
        sys.exit(1)
    capture_region, column_centers, hit_line_y = detected

    # Re-fetch hwnd for the detector's GDI calls. find_game_window is cheap
    # (single FindWindowW) and auto_detect already validated the window.
    hwnd = find_game_window()
    if not hwnd:
        print("ERROR: lost game window after auto-detect.")
        sys.exit(1)
    print()

    # Honor INPUT_BACKEND_DEFAULT so the CLI doesn't silently pin to
    # Arduino when the user has switched the default to software in
    # config.py. UI users get their per-session choice from
    # ui_settings.json; the CLI deliberately doesn't read that file.
    controller = make_input_backend(INPUT_BACKEND_DEFAULT)
    detector = NoteDetector(hwnd, column_centers, hit_line_y, controller)

    print(f"Starting per-key detection threads "
          f"(poll {KEY_POLL_DELAY_S*1000:.0f}ms, strip {Y_SAMPLE_OFFSETS})")
    detector.start()

    try:
        if DEBUG_MODE:
            print("DEBUG_MODE on - visualization window will open. Press 'q' to exit.")
            run_visualization(capture_region, column_centers, hit_line_y, detector,
                              pin_console=True)
        else:
            print("DEBUG_MODE off - running headless. Ctrl+C to exit.")
            while True:
                hwnd_console = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd_console:
                    ctypes.windll.user32.SetWindowPos(hwnd_console, -1, 0, 0, 0, 0, 3)
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCtrl+C received")
    finally:
        print("Shutting down")
        detector.stop()
        controller.close()
        cv2.destroyAllWindows()
        if timer_boosted:
            restore_timer()
        print("Shutdown complete")


if __name__ == '__main__':
    main()
