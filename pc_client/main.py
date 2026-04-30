import cv2
import mss
import numpy as np
import time
import threading
import ctypes
import ctypes.wintypes
import sys

from config import (KEYS, DEBUG_MODE, PIXEL_THRESHOLD, KEY_POLL_DELAY_S,
                    Y_SAMPLE_OFFSETS,
                    REF_WIDTH, REF_HEIGHT, REF_COLUMN_X, REF_HIT_LINE_Y,
                    GAME_WINDOW_TITLE)
from detector import NoteDetector
from controller import ArduinoHIDController


def find_game_window(verbose=False):
    """Find the Genshin Impact window and return its handle. Silent by
    default — pass verbose=True for the legacy "Found game window" log
    line. UI hotkey paths call this on every keypress so logging is off."""
    user32 = ctypes.windll.user32
    for title in GAME_WINDOW_TITLE:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            if verbose:
                print(f"Found game window: \"{title}\"")
            return hwnd
    return None


def get_game_geometry(hwnd, stop_evt=None):
    """Get the game client area position and size from its window handle.

    Waits for the game window to be restored and visible. If `stop_evt` is
    provided and gets set during the wait, returns None so a UI Stop
    button can abort the bot before the user has alt-tabbed back to the
    game.

    Returns:
        (capture_region, scale_x, scale_y) on success, or None if stopped.
    """
    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    point = ctypes.wintypes.POINT(0, 0)

    SW_RESTORE = 9
    first_wait = True

    def _sleep_or_stop(seconds):
        # stop_evt.wait returns True when the event is set — translate to
        # "stop requested" for the caller's tight while-loop.
        if stop_evt is not None:
            return stop_evt.wait(seconds)
        time.sleep(seconds)
        return False

    while True:
        if stop_evt is not None and stop_evt.is_set():
            return None

        if user32.IsIconic(hwnd):
            if first_wait:
                print("Game window is minimized. Attempting to restore...")
                user32.ShowWindow(hwnd, SW_RESTORE)
                print("Please Alt-Tab into Genshin Impact...")
                first_wait = False
            if _sleep_or_stop(1):
                return None
            continue

        user32.GetClientRect(hwnd, ctypes.byref(rect))
        client_width = rect.right - rect.left
        client_height = rect.bottom - rect.top

        if client_width == 0 or client_height == 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            client_width = rect.right - rect.left
            client_height = rect.bottom - rect.top
            top = rect.top
            left = rect.left
        else:
            point.x = 0
            point.y = 0
            user32.ClientToScreen(hwnd, ctypes.byref(point))
            top = point.y
            left = point.x

        # Check for valid dimensions (at least 800x600) and valid position
        if client_width >= 800 and client_height >= 600 and left > -10000 and top > -10000:
            break

        if first_wait:
            print(f"Waiting for game window to be fully visible (current size: {client_width}x{client_height})...")
            first_wait = False
        if _sleep_or_stop(1):
            return None

    capture_region = {
        "top": top,
        "left": left,
        "width": client_width,
        "height": client_height,
    }

    scale_x = client_width / REF_WIDTH
    scale_y = client_height / REF_HEIGHT

    return capture_region, scale_x, scale_y


def auto_detect(stop_evt=None):
    """Auto-detect game window and compute all positions.

    Returns:
        (capture_region, column_centers, hit_line_y) on success, or None
        if stop_evt was set while waiting for the game window.
    """
    hwnd = find_game_window(verbose=True)
    if not hwnd:
        titles = ", ".join(f'"{t}"' for t in GAME_WINDOW_TITLE)
        print(f"ERROR: Could not find Genshin Impact window ({titles}).")
        print("Make sure the game is running before starting the bot.")
        return None

    geom = get_game_geometry(hwnd, stop_evt=stop_evt)
    if geom is None:
        return None
    capture_region, scale_x, scale_y = geom

    # Scale reference 1080p positions to actual resolution.
    # Positions are relative to the client area (capture region), so no
    # additional offset is needed — mss captures from capture_region origin.
    column_centers = [int(x * scale_x) for x in REF_COLUMN_X]
    hit_line_y = int(REF_HIT_LINE_Y * scale_y)

    print(f"Game resolution: {capture_region['width']}x{capture_region['height']}")
    print(f"Game position: ({capture_region['left']}, {capture_region['top']})")
    print(f"Scale: {scale_x:.3f}x, {scale_y:.3f}y")
    print(f"Column centers: {column_centers}")
    print(f"Hit line Y: {hit_line_y}")

    return capture_region, column_centers, hit_line_y


def run_visualization(capture_region, column_centers, hit_line_y, detector,
                      stop_evt=None, debug_evt=None, fps_callback=None,
                      pin_console=True):
    """Debug visualization loop. Decoupled from detection — purely shows
    state. The detector threads update detector.pressed independently.

    Args:
        stop_evt: when set, exit immediately. If None, run forever (the only
            way out is the 'q' key in the OpenCV window).
        debug_evt: when *not* set, the window is destroyed and the loop
            sleeps without grabbing or rendering frames (no perf hit). When
            set again, the window is re-created and rendering resumes.
            If None, viz is always-on.
        fps_callback: optional callable(fps_float). Called once per rendered
            frame so the UI can mirror viz FPS in its status panel.
        pin_console: when True, re-pins the console window topmost each
            frame (legacy CLI behavior). UI mode passes False to skip this.
    """
    window_open = False

    def open_window():
        cv2.namedWindow("Vision Context", cv2.WINDOW_NORMAL)
        cv2.setWindowProperty("Vision Context", cv2.WND_PROP_TOPMOST, 1)
        cv2.moveWindow("Vision Context", 0, 0)

    def close_window():
        try:
            cv2.destroyWindow("Vision Context")
        except cv2.error:
            pass

    last_time = time.time()
    with mss.mss() as sct:
        while True:
            if stop_evt is not None and stop_evt.is_set():
                break

            # Debug toggle: when off, drop the window and idle until on.
            if debug_evt is not None and not debug_evt.is_set():
                if window_open:
                    close_window()
                    window_open = False
                # Idle without rendering — no frame grab, no overlay work.
                if stop_evt is not None:
                    if stop_evt.wait(0.1):
                        break
                else:
                    time.sleep(0.1)
                continue

            if not window_open:
                open_window()
                window_open = True
                last_time = time.time()

            if pin_console:
                hwnd_console = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd_console:
                    ctypes.windll.user32.SetWindowPos(hwnd_console, -1, 0, 0, 0, 0, 3)

            screenshot = sct.grab(capture_region)
            frame = np.ascontiguousarray(np.array(screenshot)[:, :, :3])

            # Hit line
            cv2.line(frame, (0, hit_line_y), (capture_region["width"], hit_line_y),
                     (0, 0, 255), 2)

            # Per-column overlays - strip dots match the actual sample points.
            for i, cx in enumerate(column_centers):
                cv2.line(frame, (cx, 0), (cx, capture_region["height"]),
                         (255, 0, 0), 1)
                is_pressed = detector.pressed[KEYS[i]]
                dot_color = (0, 0, 255) if is_pressed else (0, 255, 0)
                for dy in Y_SAMPLE_OFFSETS:
                    y = hit_line_y + dy
                    if 0 <= y < frame.shape[0]:
                        cv2.circle(frame, (cx, y), 4, dot_color, -1)
                        cv2.circle(frame, (cx, y), 4, (255, 255, 255), 1)
                if 0 <= hit_line_y < frame.shape[0] and 0 <= cx < frame.shape[1]:
                    blue_val = int(frame[hit_line_y, cx, 0])
                    cv2.putText(frame, f"B:{blue_val}",
                                (cx - 20, hit_line_y - 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

            curr_time = time.time()
            fps = 1.0 / (curr_time - last_time) if curr_time - last_time > 0 else 0
            last_time = curr_time
            if fps_callback is not None:
                try:
                    fps_callback(fps)
                except Exception:
                    pass
            cv2.putText(frame, f"Viz FPS: {fps:.1f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            cv2.putText(frame, f"Threshold: B < {PIXEL_THRESHOLD}",
                        (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(frame,
                        f"Per-key {KEY_POLL_DELAY_S*1000:.0f}ms, strip {Y_SAMPLE_OFFSETS}",
                        (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)

            scale_factor = 400 / capture_region["width"]
            target_height = int(capture_region["height"] * scale_factor)
            cv2.setWindowProperty("Vision Context", cv2.WND_PROP_TOPMOST, 1)
            preview = cv2.resize(frame, (400, target_height))
            cv2.imshow("Vision Context", preview)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                if stop_evt is not None:
                    stop_evt.set()
                break

    if window_open:
        close_window()


def boost_timer():
    """Boost system timer to 1ms. Returns True if successful."""
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        return True
    except Exception as e:
        print(f"[warn] timeBeginPeriod failed: {e}")
        return False


def restore_timer():
    try:
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def set_dpi_aware():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        print(f"[warn] SetProcessDPIAware failed: {e}")


def run_standalone(stop_evt, debug_evt, status_cb=None):
    """Reusable standalone-mode entrypoint for the UI. Blocks until
    stop_evt is set. Caller handles DPI/timer setup."""
    detected = auto_detect(stop_evt=stop_evt)
    if detected is None:
        # Either game window unavailable, or stop fired during the
        # IsIconic-wait — bail without spinning up serial / detector.
        return
    capture_region, column_centers, hit_line_y = detected
    hwnd = find_game_window()
    if not hwnd:
        print("ERROR: lost game window after auto-detect.")
        return

    controller = ArduinoHIDController()
    detector = NoteDetector(hwnd, column_centers, hit_line_y, controller)
    if status_cb:
        status_cb({'state': 'running', 'mode': 'standalone'})
    print(f"Starting per-key detection threads "
          f"(poll {KEY_POLL_DELAY_S*1000:.0f}ms, strip {Y_SAMPLE_OFFSETS})")
    detector.start()

    fps_cb = None
    if status_cb:
        fps_cb = lambda f: status_cb({'fps': f})

    try:
        # Always run viz loop — it self-gates on debug_evt and idles cheap
        # when off (no grab, no overlay computation).
        run_visualization(capture_region, column_centers, hit_line_y, detector,
                          stop_evt=stop_evt, debug_evt=debug_evt,
                          fps_callback=fps_cb, pin_console=False)
    finally:
        print("Stopping detector")
        detector.stop()
        controller.close()
        if status_cb:
            status_cb({'state': 'idle', 'fps': 0.0})


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

    controller = ArduinoHIDController()
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
