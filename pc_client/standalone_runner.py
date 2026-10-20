# pc_client/standalone_runner.py
"""Standalone-mode rhythm runner — the orchestrator the UI uses.

Wraps `NoteDetector` (rhythm engine) plus an OpenCV visualization
window and exposes them as `run_standalone(...)`. Both the UI's
`BotController` and the album-mode runner consume `run_visualization`
directly; only the UI calls `run_standalone`.

CLI's `main.py` doesn't go through `run_standalone` — it builds its
own detector + DEBUG_MODE-branch loop. See `pc_client/main.py`.
"""
import inspect
import time
import ctypes
import ctypes.wintypes
from collections import deque

import cv2
import mss
import numpy as np

from config import (KEYS, PIXEL_THRESHOLD, KEY_POLL_DELAY_S,
                    Y_SAMPLE_OFFSETS)
from detector import NoteDetector
from controller import ArduinoHIDController
from game_window import (MSG_WINDOW_NOT_FOUND, find_game_window, auto_detect,
                         get_game_geometry, read_game_geometry,
                         same_hwnd, scaled_layout)

try:
    from controller import ArduinoConnectionError
except ImportError:
    ArduinoConnectionError = RuntimeError


MSG_ARDUINO_UNAVAILABLE = ("Arduino not connected — plug it in or switch the "
                           "input backend to Software, then press Start.")

# `run_visualization` exit reasons. The caller keeps the reason instead of
# re-reading the events afterwards: a quick Pause→Resume can clear
# pause_evt while the detector is still being torn down, and re-reading
# then looks like "neither stop nor pause" (that used to end the run).
VIZ_EXIT_STOP = 'stop'
VIZ_EXIT_PAUSE = 'pause'
VIZ_EXIT_QUIT = 'quit'
# The game window was resized (lane layout changed) — rebuild the detector
# and re-enter with the new geometry. A plain move is handled in place.
VIZ_EXIT_GEOMETRY = 'geometry'

# Non-blocking geometry poll cadence during active play (window moved /
# resized while a song runs) — same cadence as the album's SongPlayer.
GEOMETRY_POLL_S = 0.5

VIZ_WINDOW_TITLE = "Vision Context"
# How often the preview re-asserts topmost (non-activating) and checks it
# hasn't ended up as the foreground window. Off the detector hot path.
VIZ_FOCUS_CHECK_S = 0.25


# --- debug-preview window: never take focus from the game -----------------
#
# OpenCV's Win32 HighGUI shows its windows with activating calls
# (namedWindow -> ShowWindow(SW_SHOW); setWindowProperty(TOPMOST) ->
# SetWindowPos without SWP_NOACTIVATE). The detector only types while
# Genshin is the foreground window, so a preview that grabs focus would
# pause the bot — every time Debug is toggled on, and every song in album
# mode (the preview is recreated per song / geometry change). So:
#   1. After creating the preview, look up its HWND by title, add
#      WS_EX_NOACTIVATE and keep it topmost with SetWindowPos +
#      SWP_NOACTIVATE. OpenCV's activating TOPMOST setter is only used as
#      a one-off fallback when the HWND can't be found.
#   2. Safety net: if the preview is foreground anyway (creation already
#      activated it, or a backend quirk), hand focus straight back — to
#      whoever had it before the preview opened, later to the game.
#      Same restore -> SetForegroundWindow -> SwitchToThisWindow pattern
#      as ui_core.focus_game_window. Rate-limited to VIZ_FOCUS_CHECK_S in
#      the viz loop; never on the detector's per-key hot path.
# Side effect: keys typed "into" the preview no longer reach it, so 'q'
# only works if the user explicitly focuses it; the window's X button
# (and the UI's Debug toggle) still close it.
#
# Private user32 handle so these prototypes don't leak into the process-
# global `ctypes.windll.user32` that ui.py / ui_core.py rely on.
try:
    _vu32 = ctypes.WinDLL('user32', use_last_error=True)
    _H = ctypes.wintypes.HWND
    _vu32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR,
                                  ctypes.wintypes.LPCWSTR]
    _vu32.FindWindowW.restype = _H
    _vu32.GetForegroundWindow.argtypes = []
    _vu32.GetForegroundWindow.restype = _H
    _vu32.GetWindowLongPtrW.argtypes = [_H, ctypes.c_int]
    _vu32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _vu32.SetWindowLongPtrW.argtypes = [_H, ctypes.c_int, ctypes.c_ssize_t]
    _vu32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _vu32.SetWindowPos.argtypes = [_H, _H, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
    _vu32.SetWindowPos.restype = ctypes.wintypes.BOOL
    _vu32.IsWindow.argtypes = [_H]
    _vu32.IsWindow.restype = ctypes.wintypes.BOOL
    _vu32.IsIconic.argtypes = [_H]
    _vu32.IsIconic.restype = ctypes.wintypes.BOOL
    _vu32.ShowWindow.argtypes = [_H, ctypes.c_int]
    _vu32.ShowWindow.restype = ctypes.wintypes.BOOL
    _vu32.SetForegroundWindow.argtypes = [_H]
    _vu32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
    _vu32.SwitchToThisWindow.argtypes = [_H, ctypes.wintypes.BOOL]
    _vu32.SwitchToThisWindow.restype = None
except (AttributeError, OSError):
    _vu32 = None

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SW_RESTORE = 9


def _viz_hwnd():
    """HWND of our preview window, 0 if not found."""
    if _vu32 is None:
        return 0
    try:
        return _vu32.FindWindowW(None, VIZ_WINDOW_TITLE) or 0
    except Exception:
        return 0


def _foreground_window():
    if _vu32 is None:
        return 0
    try:
        return _vu32.GetForegroundWindow() or 0
    except Exception:
        return 0


def _keep_topmost(hwnd):
    """Topmost without activating (unlike cv2's WND_PROP_TOPMOST)."""
    if _vu32 is None or not hwnd:
        return
    try:
        _vu32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                           _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
    except Exception:
        pass


def _make_non_activating(hwnd):
    if _vu32 is None or not hwnd:
        return
    try:
        ex = _vu32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        _vu32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, ex | _WS_EX_NOACTIVATE)
    except Exception as e:
        print(f"[viz] couldn't make the preview non-activating: {e}")
    _keep_topmost(hwnd)


def _give_focus_to(hwnd):
    """Hand the foreground back to `hwnd` (restore if minimized ->
    SetForegroundWindow -> SwitchToThisWindow fallback). Windows allows it
    because it only runs right after OUR preview became foreground.
    Returns True if a focus change was requested."""
    if _vu32 is None or not hwnd:
        return False
    try:
        if not _vu32.IsWindow(hwnd):
            return False
        if _vu32.IsIconic(hwnd):
            _vu32.ShowWindow(hwnd, _SW_RESTORE)
        if not _vu32.SetForegroundWindow(hwnd):
            _vu32.SwitchToThisWindow(hwnd, True)
        return True
    except Exception:
        return False


def _guard_viz_focus(viz_hwnd, game_hwnd):
    """Safety net (rate-limited by the caller): keep the preview topmost
    and, if it is the foreground window, give focus back to the game."""
    if not viz_hwnd:
        return
    _keep_topmost(viz_hwnd)
    if not same_hwnd(_foreground_window(), viz_hwnd):
        return
    target = game_hwnd or find_game_window()
    if target and _give_focus_to(target):
        print("[viz] debug preview took focus — handing it back to the game")


def layout_changed(old, new):
    """`old` / `new` are (capture_region, column_centers, hit_line_y).
    True when the lane layout differs (resize) — the detector must be
    rebuilt; False for a plain move."""
    o_region, o_cols, o_hit = old
    n_region, n_cols, n_hit = new
    return (list(o_cols) != list(n_cols) or o_hit != n_hit
            or o_region['width'] != n_region['width']
            or o_region['height'] != n_region['height'])


class GeometryTracker:
    """Non-blocking tracker for a windowed game being moved / resized
    during active play. `poll()` is cheap (`read_game_geometry` never
    messages the game) — call it every ~GEOMETRY_POLL_S. Returns the new
    (capture_region, column_centers, hit_line_y) when anything changed,
    else None (no change, or the window isn't usable right now — keep
    the last-known geometry)."""

    def __init__(self, hwnd, capture_region, column_centers, hit_line_y,
                 tag='[standalone]'):
        self.hwnd = hwnd
        self.tag = tag
        self.set(capture_region, column_centers, hit_line_y)

    def set(self, capture_region, column_centers, hit_line_y):
        self.capture_region = dict(capture_region)
        self.column_centers = list(column_centers)
        self.hit_line_y = hit_line_y

    @property
    def current(self):
        return self.capture_region, self.column_centers, self.hit_line_y

    def poll(self):
        geom = read_game_geometry(self.hwnd)
        if geom is None:
            return None
        region, scale_x, scale_y = geom
        cols, hit = scaled_layout(scale_x, scale_y)
        new = (dict(region), cols, hit)
        old = self.current
        if new[0] == old[0] and new[1] == old[1] and new[2] == old[2]:
            return None
        what = 'resized' if layout_changed(old, new) else 'moved'
        self.set(*new)
        print(f"{self.tag} game window {what} — now "
              f"{region['width']}x{region['height']} @ "
              f"({region['left']},{region['top']})")
        return self.current


def make_fallback_arduino(cancel_evt=None):
    """Legacy fallback for callers that didn't inject an InputBackend.

    Returns `(controller, None)` on success, or `(None, message)` if the
    board couldn't be reached — `message` is user-facing (the adapter's
    own `ArduinoConnectionError` text when it has one). Catches
    SystemExit too: older adapters `sys.exit(1)` on a missing board,
    which would otherwise bypass every `except Exception` up the stack."""
    try:
        if _accepts_kwarg(ArduinoHIDController, 'cancel_evt'):
            return ArduinoHIDController(cancel_evt=cancel_evt), None
        return ArduinoHIDController(), None
    except ArduinoConnectionError as e:
        message = str(e) or MSG_ARDUINO_UNAVAILABLE
    except SystemExit:
        message = MSG_ARDUINO_UNAVAILABLE
    print(f"ERROR: {message}")
    return None, message


def _accepts_kwarg(fn, name):
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def run_visualization(capture_region, column_centers, hit_line_y, detector,
                      stop_evt=None, debug_evt=None, fps_callback=None,
                      pin_console=True, pause_evt=None, game_hwnd=None,
                      geometry_poll=None):
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
            frame so the UI can mirror viz FPS in its status panel, and
            with 0.0 whenever the window closes (debug off, pause, exit)
            so the panel doesn't keep a frozen FPS reading.
        pin_console: when True, re-pins the console window topmost each
            frame (legacy CLI behavior). UI mode passes False to skip this.
        pause_evt: when set, exit cleanly so the caller can take down the
            detector and idle. Treated as a soft stop — caller decides
            whether to resume (re-enter this function) or quit.
        game_hwnd: Genshin's HWND — where focus goes back to if the
            preview ever ends up foreground (see the focus note above).
        geometry_poll: optional callable() -> None | (capture_region,
            column_centers, hit_line_y), e.g. `GeometryTracker.poll`.
            Called every GEOMETRY_POLL_S whether debug is on or off. A
            move retargets the capture in place; a resize returns
            VIZ_EXIT_GEOMETRY so the caller rebuilds the detector.

    Returns why the loop exited: VIZ_EXIT_STOP (stop_evt), VIZ_EXIT_PAUSE
    (pause_evt), VIZ_EXIT_QUIT ('q' / window closed with no debug_evt) or
    VIZ_EXIT_GEOMETRY (game window resized; see geometry_poll).
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

    viz = {'hwnd': 0, 'next_focus_check': 0.0}

    def open_window():
        prev_fg = _foreground_window()
        cv2.namedWindow(VIZ_WINDOW_TITLE, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(VIZ_WINDOW_TITLE, *initial_window_size)
        cv2.moveWindow(VIZ_WINDOW_TITLE, 0, 0)
        hwnd = _viz_hwnd()
        viz['hwnd'] = hwnd
        viz['next_focus_check'] = 0.0   # check right after the 1st imshow
        if hwnd:
            _make_non_activating(hwnd)
            # namedWindow's ShowWindow(SW_SHOW) may already have activated
            # the preview — give focus back to whoever had it (the game, or
            # the UI if the user just ticked Debug there).
            if (prev_fg and not same_hwnd(prev_fg, hwnd)
                    and same_hwnd(_foreground_window(), hwnd)):
                _give_focus_to(prev_fg)
        else:
            # Preview HWND not found (non-Win32 HighGUI backend?) — fall
            # back to OpenCV's own topmost flag, once, not per frame.
            cv2.setWindowProperty(VIZ_WINDOW_TITLE, cv2.WND_PROP_TOPMOST, 1)

    def close_window():
        viz['hwnd'] = 0
        try:
            cv2.destroyWindow(VIZ_WINDOW_TITLE)
        except cv2.error:
            pass
        # Window gone → no more frames → zero the UI's FPS readout instead
        # of leaving the last rendered value frozen on the Status card.
        if fps_callback is not None:
            try:
                fps_callback(0.0)
            except Exception:
                pass

    exit_reason = VIZ_EXIT_STOP
    last_time = time.time()
    next_geom_poll = time.time() + GEOMETRY_POLL_S
    with mss.mss() as sct:
        while True:
            if stop_evt is not None and stop_evt.is_set():
                exit_reason = VIZ_EXIT_STOP
                break
            if pause_evt is not None and pause_evt.is_set():
                if window_open:
                    close_window()
                    window_open = False
                exit_reason = VIZ_EXIT_PAUSE
                break

            # Track a windowed game being moved / resized mid-play.
            if geometry_poll is not None and time.time() >= next_geom_poll:
                next_geom_poll = time.time() + GEOMETRY_POLL_S
                new_geom = geometry_poll()
                if new_geom is not None:
                    if layout_changed(
                            (capture_region, column_centers, hit_line_y),
                            new_geom):
                        if window_open:
                            close_window()
                            window_open = False
                        exit_reason = VIZ_EXIT_GEOMETRY
                        break
                    # Moved only: same layout, capture the new rectangle.
                    capture_region = dict(new_geom[0])

            # Debug toggle: when off, drop the window and idle until on.
            if debug_evt is not None and not debug_evt.is_set():
                if window_open:
                    close_window()
                    window_open = False
                # Idle without rendering — no frame grab, no overlay work.
                if stop_evt is not None:
                    if stop_evt.wait(0.1):
                        exit_reason = VIZ_EXIT_STOP
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
                    # 0x13 = NOSIZE|NOMOVE|NOACTIVATE: pin topmost without
                    # pulling focus off the game (detector pauses then).
                    ctypes.windll.user32.SetWindowPos(hwnd_console, -1, 0, 0, 0, 0, 0x13)

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

            cv2.imshow(VIZ_WINDOW_TITLE, canvas)

            key = cv2.waitKey(1) & 0xFF

            # Topmost re-assert (non-activating) + focus safety net — see
            # the note at the top of this module. Rate-limited.
            now = time.time()
            if now >= viz['next_focus_check']:
                viz['next_focus_check'] = now + VIZ_FOCUS_CHECK_S
                _guard_viz_focus(viz['hwnd'], game_hwnd)

            # X-button close — cv2 destroys the window; getWindowProperty
            # returns < 1 once it's gone. Treat the same as 'q'.
            try:
                visible = cv2.getWindowProperty(
                    VIZ_WINDOW_TITLE, cv2.WND_PROP_VISIBLE)
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
                    exit_reason = VIZ_EXIT_QUIT
                    break

    if window_open:
        close_window()
    return exit_reason


def _refresh_standalone_geometry(hwnd, stop_evt, status_cb):
    """Re-read the game geometry after a pause (windowed Genshin may have
    been moved or resized meanwhile). Blocks — stop-aware — while the
    game is minimized. Returns (hwnd, capture_region, column_centers,
    hit_line_y), or None if stop fired or the game window is gone."""
    # The user may have restarted the game during the pause — prefer a
    # fresh handle. FindWindowW doesn't message the target, so it's cheap.
    hwnd = find_game_window() or hwnd
    geom = get_game_geometry(hwnd, stop_evt=stop_evt, status_cb=status_cb)
    if geom is None:
        return None
    capture_region, scale_x, scale_y = geom
    column_centers, hit_line_y = scaled_layout(scale_x, scale_y)
    return hwnd, capture_region, column_centers, hit_line_y


def run_standalone(stop_evt, debug_evt, status_cb=None, pause_evt=None,
                   controller=None):
    """Reusable standalone-mode entrypoint for the UI. Blocks until
    stop_evt is set. Caller handles DPI/timer setup.

    If pause_evt is provided and gets set, the detector is stopped
    (releasing all keys) and the function idles until pause_evt is
    cleared. On resume the game geometry is re-read (the window may have
    moved / resized) and a fresh detector is spun up. During play the
    geometry is polled every GEOMETRY_POLL_S: a resize rebuilds the
    detector, a move retargets the viz capture.

    `controller` lets the UI inject a long-lived InputBackend
    shared across bot runs + the macro tool (only one process can hold
    the COM port). When None, a fresh `ArduinoHIDController` is created
    here and closed on exit — legacy fallback for callers that don't
    pass one. The CLI path picks its backend explicitly via
    `make_input_backend(...)` and passes it in.

    User-actionable failures (game window missing / closed / minimized,
    Arduino unavailable) are reported as `status_cb({'last_error': ...})`
    in addition to the log line."""
    def _error(message):
        if status_cb:
            try:
                status_cb({'last_error': message})
            except Exception:
                pass

    def _stopped():
        return bool(stop_evt and stop_evt.is_set())

    detected = auto_detect(stop_evt=stop_evt, status_cb=status_cb)
    if detected is None:
        # Either game window unavailable (auto_detect already reported
        # last_error), or stop fired during the IsIconic-wait — bail
        # without spinning up serial / detector.
        return
    capture_region, column_centers, hit_line_y = detected
    hwnd = find_game_window()
    if not hwnd:
        print("ERROR: lost game window after auto-detect.")
        _error(MSG_WINDOW_NOT_FOUND)
        return

    own_controller = controller is None
    if own_controller:
        controller, failure = make_fallback_arduino(cancel_evt=stop_evt)
        if controller is None:
            if not _stopped():
                _error(failure)
            return

    fps_cb = None
    if status_cb:
        fps_cb = lambda f: status_cb({'fps': f})

    # Windowed Genshin can be moved / resized mid-play: the viz loop polls
    # this every GEOMETRY_POLL_S (non-blocking). A move retargets the viz
    # capture in place (the detector reads client-relative pixels, so it
    # doesn't care); a resize exits the viz with VIZ_EXIT_GEOMETRY and we
    # rebuild the detector for the new lane layout below.
    tracker = GeometryTracker(hwnd, capture_region, column_centers,
                              hit_line_y)

    # Set when a pause interrupted the previous detector, so the resume path
    # (geometry re-read) runs even if pause_evt was already cleared again
    # (quick Pause→Resume while detector.stop() was joining).
    resume_pending = False

    try:
        while not _stopped():
            # --- pause / resume gate ---
            # Every loop-back passes here BEFORE a detector starts: after a
            # pause exit, and after a resize rebuild whose teardown a Pause
            # landed in (otherwise the replacement would start — and could
            # type — with Pause set).
            if resume_pending or (pause_evt is not None
                                  and pause_evt.is_set()):
                resume_pending = False
                if pause_evt is not None and pause_evt.is_set():
                    if status_cb:
                        status_cb({'state': 'paused'})
                    print("[standalone] paused — detector stopped, "
                          "waiting for resume")
                    while pause_evt.is_set():
                        if stop_evt is not None:
                            if stop_evt.wait(0.1):
                                break
                        else:
                            time.sleep(0.1)
                    if _stopped():
                        break
                print("[standalone] resumed")

                # Windowed Genshin may have been moved / resized while paused.
                old_region, old_cols, old_hit = tracker.current
                refreshed = _refresh_standalone_geometry(hwnd, stop_evt,
                                                         status_cb)
                if refreshed is None:
                    # Stop fired while waiting for a minimized game, or the
                    # game window is gone (get_game_geometry reported it).
                    break
                new_hwnd, new_region, new_cols, new_hit = refreshed
                if (new_region != old_region or new_cols != old_cols
                        or new_hit != old_hit
                        or not same_hwnd(new_hwnd, hwnd)):
                    print(f"[standalone] game window moved/resized — now "
                          f"{new_region['width']}x{new_region['height']} @ "
                          f"({new_region['left']},{new_region['top']})")
                hwnd = new_hwnd
                tracker.hwnd = hwnd
                tracker.set(new_region, new_cols, new_hit)
                # Paused again while we waited on the window? Back to
                # the gate rather than starting a detector.
                continue

            capture_region, column_centers, hit_line_y = tracker.current
            detector = NoteDetector(hwnd, column_centers, hit_line_y,
                                    controller)
            if status_cb:
                status_cb({'state': 'running', 'mode': 'standalone'})
            print(f"Starting per-key detection threads "
                  f"(poll {KEY_POLL_DELAY_S*1000:.0f}ms, "
                  f"strip {Y_SAMPLE_OFFSETS})")
            detector.start()

            reason = VIZ_EXIT_STOP
            try:
                # Viz loop self-gates on debug_evt and idles cheap when off.
                reason = run_visualization(
                    capture_region, column_centers, hit_line_y,
                    detector, stop_evt=stop_evt,
                    debug_evt=debug_evt, fps_callback=fps_cb,
                    pin_console=False, pause_evt=pause_evt,
                    game_hwnd=hwnd, geometry_poll=tracker.poll)
            finally:
                print("Stopping detector")
                detector.stop()

            if _stopped():
                break
            if reason == VIZ_EXIT_GEOMETRY:
                # Resized mid-play — tracker holds the new layout; loop
                # back to build a detector for it (the gate at the top
                # handles a Pause that landed during the teardown).
                continue
            if reason == VIZ_EXIT_PAUSE:
                # pause_evt may already be clear again (quick Pause→Resume
                # while detector.stop() was joining) — the gate still runs
                # the resume path instead of treating it as "run over".
                resume_pending = True
                continue
            # Exited because of 'q' / window close with no debug toggle.
            break
    finally:
        if own_controller:
            controller.close()
        if status_cb:
            status_cb({'state': 'idle', 'fps': 0.0})
