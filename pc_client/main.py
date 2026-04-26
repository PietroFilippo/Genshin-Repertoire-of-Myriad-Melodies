import cv2
import mss
import numpy as np
import time
import ctypes
import ctypes.wintypes
import sys

from config import (KEYS, DEBUG_MODE, PIXEL_THRESHOLD, KEY_POLL_DELAY_S,
                    Y_SAMPLE_OFFSETS,
                    REF_WIDTH, REF_HEIGHT, REF_COLUMN_X, REF_HIT_LINE_Y,
                    GAME_WINDOW_TITLE)
from detector import NoteDetector
from controller import ArduinoHIDController


def find_game_window():
    """Find the Genshin Impact window and return its handle."""
    user32 = ctypes.windll.user32
    for title in GAME_WINDOW_TITLE:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            print(f"Found game window: \"{title}\"")
            return hwnd
    return None


def get_game_geometry(hwnd):
    """Get the game client area position and size from its window handle.

    Waits for the game window to be restored and visible.

    Returns:
        capture_region: dict with top, left, width, height (screen coords)
        scale_x: horizontal scale factor vs 1920
        scale_y: vertical scale factor vs 1080
    """
    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    point = ctypes.wintypes.POINT(0, 0)

    SW_RESTORE = 9
    first_wait = True

    while True:
        if user32.IsIconic(hwnd):
            if first_wait:
                print("Game window is minimized. Attempting to restore...")
                user32.ShowWindow(hwnd, SW_RESTORE)
                print("Please Alt-Tab into Genshin Impact...")
                first_wait = False
            time.sleep(1)
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
        time.sleep(1)

    capture_region = {
        "top": top,
        "left": left,
        "width": client_width,
        "height": client_height,
    }

    scale_x = client_width / REF_WIDTH
    scale_y = client_height / REF_HEIGHT

    return capture_region, scale_x, scale_y


def auto_detect():
    """Auto-detect game window and compute all positions.

    Returns:
        capture_region: mss-compatible dict
        column_centers: list of 6 X-coords (relative to capture region)
        hit_line_y: Y-coord (relative to capture region)
    """
    hwnd = find_game_window()
    if not hwnd:
        titles = ", ".join(f'"{t}"' for t in GAME_WINDOW_TITLE)
        print(f"ERROR: Could not find Genshin Impact window ({titles}).")
        print("Make sure the game is running before starting the bot.")
        sys.exit(1)

    capture_region, scale_x, scale_y = get_game_geometry(hwnd)

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


def run_visualization(capture_region, column_centers, hit_line_y, detector):
    """Debug visualization loop. Decoupled from detection — purely shows
    state. The detector threads update detector.pressed independently.
    Press 'q' in the window to quit.
    """
    cv2.namedWindow("Vision Context", cv2.WINDOW_NORMAL)
    cv2.setWindowProperty("Vision Context", cv2.WND_PROP_TOPMOST, 1)
    cv2.moveWindow("Vision Context", 0, 0)

    last_time = time.time()
    with mss.mss() as sct:
        while True:
            # Keep terminal pinned on top so log scroll stays visible.
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
                return


def main():
    print("Initializing program")
    print()

    # Make this process DPI-aware so GDI GetPixel coords match the
    # client-rect-derived coords used by the calibration logic.
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        print(f"[warn] SetProcessDPIAware failed: {e}")

    # Boost system timer resolution to 1ms so time.sleep(0.005) actually
    # sleeps ~5ms instead of the Windows default ~15.6ms quantum. Without
    # this the per-key poll rate collapses and the whole point of fast
    # detection is lost.
    timer_boosted = False
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        timer_boosted = True
    except Exception as e:
        print(f"[warn] timeBeginPeriod failed: {e}")

    capture_region, column_centers, hit_line_y = auto_detect()

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
            run_visualization(capture_region, column_centers, hit_line_y, detector)
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
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        print("Shutdown complete")


if __name__ == '__main__':
    main()
