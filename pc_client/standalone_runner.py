# pc_client/standalone_runner.py
"""Standalone-mode rhythm runner — the orchestrator the UI uses.

Wraps `NoteDetector` (rhythm engine) plus an OpenCV visualization
window and exposes them as `run_standalone(...)`. Both the UI's
`BotController` and the album-mode runner consume `run_visualization`
directly; only the UI calls `run_standalone`.

CLI's `main.py` doesn't go through `run_standalone` — it builds its
own detector + DEBUG_MODE-branch loop. See `pc_client/main.py`.
"""
import time
import ctypes
from collections import deque

import cv2
import mss
import numpy as np

from config import (KEYS, PIXEL_THRESHOLD, KEY_POLL_DELAY_S,
                    Y_SAMPLE_OFFSETS)
from detector import NoteDetector
from controller import ArduinoHIDController
from game_window import find_game_window, auto_detect


def run_visualization(capture_region, column_centers, hit_line_y, detector,
                      stop_evt=None, debug_evt=None, fps_callback=None,
                      pin_console=True, pause_evt=None):
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
        pause_evt: when set, exit cleanly so the caller can take down the
            detector and idle. Treated as a soft stop — caller decides
            whether to resume (re-enter this function) or quit.
    """
    window_open = False

    # Preview geometry — fixed once per call, since capture_region is too.
    # Drawing overlays on the preview (post-resize) instead of the native
    # frame avoids the "tiny illegible cluster in the corner" artifact:
    # native fontScale=1 text on a 1920-wide frame became ~6px tall after
    # the resize, which the user then saw stretched + blurred in the
    # window. Drawing post-resize keeps text crisp at the displayed size.
    #
    # Also crop the captured frame to a band around the hit line before
    # resize. The full client area includes the game's top HUD (combo
    # counter, score, etc.) which has nothing to do with detection and
    # otherwise leaks into the preview as visual noise. The band keeps
    # ~70% of the lane height above the hit line (enough context to see
    # notes falling) plus a small margin below it.
    PREVIEW_W = 640
    LOG_PANEL_W = 180        # dedicated strip right of the lane image
    CANVAS_W = PREVIEW_W + LOG_PANEL_W
    BAND_ABOVE_PCT = 0.50    # ~half the captured height above the hit
                             # line. Tight enough to drop the game's
                             # top-of-screen HUD (combo counter / score)
                             # while keeping enough lane context to see
                             # incoming notes
    BAND_BELOW_PX = 90       # extends through the game's own A/S/D/J/K/L
                             # column key indicators below the hit line —
                             # no need to draw our own labels since the
                             # game already shows them
    band_top = max(0, hit_line_y
                   - int(capture_region["height"] * BAND_ABOVE_PCT))
    band_bottom = min(capture_region["height"],
                      hit_line_y + BAND_BELOW_PX)
    band_h = band_bottom - band_top

    viz_scale = PREVIEW_W / capture_region["width"]
    preview_h = int(band_h * viz_scale)
    preview_col_x = [int(cx * viz_scale) for cx in column_centers]
    preview_hit_y = int((hit_line_y - band_top) * viz_scale)
    band_size = (PREVIEW_W, preview_h)
    initial_window_size = (CANVAS_W, preview_h + 40)  # +40 for titlebar

    # Live log tail — DOWN/UP transitions of detector.pressed, displayed in
    # the viz window so the user can see real-time keypresses without
    # alt-tabbing to the UI's Logs tab. The log panel lives in its own
    # right-side strip so it never overlaps the lane (the L column would
    # otherwise sit underneath it).
    LOG_MAX = max(6, (preview_h - 30) // 16)
    log_lines = deque(maxlen=LOG_MAX)
    prev_pressed = {k: False for k in KEYS}

    def open_window():
        cv2.namedWindow("Vision Context", cv2.WINDOW_NORMAL)
        cv2.setWindowProperty("Vision Context", cv2.WND_PROP_TOPMOST, 1)
        cv2.resizeWindow("Vision Context", *initial_window_size)
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
            if pause_evt is not None and pause_evt.is_set():
                if window_open:
                    close_window()
                    window_open = False
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

            # Sample blue values at native resolution BEFORE the crop /
            # resize — the preview's interpolation would smear the readout.
            blue_vals = []
            for cx in column_centers:
                if (0 <= hit_line_y < frame.shape[0]
                        and 0 <= cx < frame.shape[1]):
                    blue_vals.append(int(frame[hit_line_y, cx, 0]))
                else:
                    blue_vals.append(-1)

            # Crop to the lane band (drops the game's top HUD).
            band = frame[band_top:band_bottom, :, :]

            # Detect KEY DOWN/UP transitions and append to the live log tail.
            now_ts = time.strftime('%H:%M:%S')
            for k in KEYS:
                cur = detector.pressed[k]
                if cur != prev_pressed[k]:
                    arrow = 'DOWN' if cur else 'UP'
                    log_lines.append((cur, f"[{now_ts}] {k.upper()} {arrow}"))
                    prev_pressed[k] = cur

            band_resized = cv2.resize(band, band_size)

            # Composite: lane image on the left (now extends past the hit
            # line so the game's own A/S/D/J/K/L column letters are
            # visible), log strip on the right.
            canvas = np.zeros((preview_h, CANVAS_W, 3), dtype=np.uint8)
            canvas[:, :PREVIEW_W] = band_resized

            # Hit line + column verticals — drawn on the canvas's lane
            # area at native preview pixel sizes so they stay crisp.
            cv2.line(canvas, (0, preview_hit_y), (PREVIEW_W, preview_hit_y),
                     (0, 0, 255), 2)
            for i, cx in enumerate(preview_col_x):
                cv2.line(canvas, (cx, 0), (cx, preview_h), (255, 0, 0), 1)
                is_pressed = detector.pressed[KEYS[i]]
                dot_color = (0, 0, 255) if is_pressed else (0, 255, 0)
                # Single dot at hit line — the multi-offset strip clusters
                # to within 1-2px on the preview anyway.
                cv2.circle(canvas, (cx, preview_hit_y), 5, dot_color, -1)
                cv2.circle(canvas, (cx, preview_hit_y), 5, (255, 255, 255), 1)
                if blue_vals[i] >= 0:
                    cv2.putText(canvas, f"B:{blue_vals[i]}",
                                (cx - 22, preview_hit_y - 14),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                                (255, 255, 255), 1)

            curr_time = time.time()
            fps = 1.0 / (curr_time - last_time) if curr_time - last_time > 0 else 0
            last_time = curr_time
            if fps_callback is not None:
                try:
                    fps_callback(fps)
                except Exception:
                    pass

            # Header overlays — top-left, on the lane image.
            cv2.rectangle(canvas, (4, 4), (180, 70), (0, 0, 0), -1)
            cv2.putText(canvas, f"FPS: {fps:.1f}", (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.putText(canvas, f"B < {PIXEL_THRESHOLD}", (10, 46),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            cv2.putText(canvas,
                        f"poll {KEY_POLL_DELAY_S*1000:.0f}ms",
                        (10, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (0, 255, 255), 1)

            # Live key-log panel — right strip, fully outside the lane
            # image so it never overlaps the columns.
            log_x = PREVIEW_W + 8
            cv2.putText(canvas, "Key log", (log_x, 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            for n, (is_down, text) in enumerate(log_lines):
                color = (80, 200, 255) if is_down else (160, 160, 160)
                cv2.putText(canvas, text,
                            (log_x, 36 + n * 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)

            cv2.setWindowProperty("Vision Context", cv2.WND_PROP_TOPMOST, 1)
            cv2.imshow("Vision Context", canvas)

            key = cv2.waitKey(1) & 0xFF
            # X-button close — cv2 destroys the window; getWindowProperty
            # returns < 1 once it's gone. Treat the same as 'q'.
            try:
                visible = cv2.getWindowProperty(
                    "Vision Context", cv2.WND_PROP_VISIBLE)
            except cv2.error:
                visible = 0
            user_closed = (key == ord('q')) or (visible < 1)

            if user_closed:
                if debug_evt is not None:
                    # UI mode: closing the viz window only turns debug
                    # off, it does not stop the bot. Clearing debug_evt
                    # also flips the UI checkbox via the status drain.
                    debug_evt.clear()
                    if window_open:
                        close_window()
                        window_open = False
                    # Fall through to top of loop → idle branch.
                    continue
                else:
                    # CLI / no debug toggle — only way out is 'q'.
                    if stop_evt is not None:
                        stop_evt.set()
                    break

    if window_open:
        close_window()


def run_standalone(stop_evt, debug_evt, status_cb=None, pause_evt=None,
                   controller=None):
    """Reusable standalone-mode entrypoint for the UI. Blocks until
    stop_evt is set. Caller handles DPI/timer setup.

    If pause_evt is provided and gets set, the detector is stopped
    (releasing all keys) and the function idles until pause_evt is
    cleared. On resume a fresh detector is spun up.

    `controller` lets the UI inject a long-lived InputBackend
    shared across bot runs + the macro tool (only one process can hold
    the COM port). When None, a fresh `ArduinoHIDController` is created
    here and closed on exit — legacy fallback for callers that don't
    pass one. The CLI path picks its backend explicitly via
    `make_input_backend(...)` and passes it in."""
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

    own_controller = controller is None
    if own_controller:
        controller = ArduinoHIDController()

    fps_cb = None
    if status_cb:
        fps_cb = lambda f: status_cb({'fps': f})

    try:
        while not (stop_evt and stop_evt.is_set()):
            detector = NoteDetector(hwnd, column_centers, hit_line_y,
                                    controller)
            if status_cb:
                status_cb({'state': 'running', 'mode': 'standalone'})
            print(f"Starting per-key detection threads "
                  f"(poll {KEY_POLL_DELAY_S*1000:.0f}ms, "
                  f"strip {Y_SAMPLE_OFFSETS})")
            detector.start()

            try:
                # Viz loop self-gates on debug_evt and idles cheap when off.
                run_visualization(capture_region, column_centers, hit_line_y,
                                  detector, stop_evt=stop_evt,
                                  debug_evt=debug_evt, fps_callback=fps_cb,
                                  pin_console=False, pause_evt=pause_evt)
            finally:
                print("Stopping detector")
                detector.stop()

            # If we exited because of stop (not pause), we're done.
            if stop_evt and stop_evt.is_set():
                break
            if pause_evt is None or not pause_evt.is_set():
                break

            # --- paused: idle until resume or stop ---
            if status_cb:
                status_cb({'state': 'paused'})
            print("[standalone] paused — detector stopped, waiting for resume")
            while pause_evt.is_set():
                if stop_evt and stop_evt.is_set():
                    break
                time.sleep(0.1)
            if stop_evt and stop_evt.is_set():
                break
            print("[standalone] resumed")
            # Loop back to spin up a fresh detector.
    finally:
        if own_controller:
            controller.close()
        if status_cb:
            status_cb({'state': 'idle', 'fps': 0.0})
