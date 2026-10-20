# pc_client/game_window.py
"""Genshin Impact window detection and capture-region computation.

Used by the standalone rhythm runner, the album runner, and the
calibration script. Authoritative module for "where on the screen is
the game and how do its coordinates map to our 1080p reference?"
"""
import ctypes
import ctypes.wintypes
import time

from config import (GAME_WINDOW_TITLE, REF_WIDTH, REF_HEIGHT,
                    REF_COLUMN_X, REF_HIT_LINE_Y)


# User-facing `last_error` strings (shown in the UI status card via
# `status_cb({'last_error': ...})`). Short, plain-English, actionable.
MSG_WINDOW_NOT_FOUND = ("Genshin Impact window not found — start the game, "
                        "then press Start.")
MSG_WINDOW_CLOSED = ("Genshin Impact window closed — start the game, "
                     "then press Start.")
MSG_WINDOW_MINIMIZED = ("Genshin Impact is minimized — restore the game "
                        "window to continue.")
MSG_WINDOW_NOT_VISIBLE = ("Genshin Impact window is too small or off-screen "
                          "— make it at least 800x600 and visible.")

# Smallest client area we accept as "the game is actually on screen".
_MIN_CLIENT_W = 800
_MIN_CLIENT_H = 600


def _user32():
    # Indirection so tests can stub the Win32 surface without patching
    # the process-global `ctypes.windll`.
    return ctypes.windll.user32


def _report(status_cb, message):
    """Best-effort `last_error` emission. '' clears a previously
    reported wait-state error once the window is usable again."""
    if status_cb is None:
        return
    try:
        status_cb({'last_error': message})
    except Exception:
        pass


def find_game_window(verbose=False):
    """Find the Genshin Impact window and return its handle. Silent by
    default — pass verbose=True for the legacy "Found game window" log
    line. UI hotkey paths call this on every keypress so logging is off."""
    user32 = _user32()
    for title in GAME_WINDOW_TITLE:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            if verbose:
                print(f"Found game window: \"{title}\"")
            return hwnd
    return None


def scaled_layout(scale_x, scale_y):
    """Scale the 1080p reference column centers + hit-line Y to the
    live resolution. Positions are relative to the client area (capture
    region) — the detector's GDI reads use the client DC and mss
    captures from the capture-region origin, so no screen offset is
    needed. Returns (column_centers, hit_line_y)."""
    column_centers = [int(x * scale_x) for x in REF_COLUMN_X]
    hit_line_y = int(REF_HIT_LINE_Y * scale_y)
    return column_centers, hit_line_y


def _client_area(hwnd):
    """(left, top, width, height) of the client area in screen coords,
    falling back to the window rect when the client rect reads empty."""
    user32 = _user32()
    rect = ctypes.wintypes.RECT()
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
        point = ctypes.wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(point))
        top = point.y
        left = point.x
    return left, top, client_width, client_height


def _usable(left, top, width, height):
    # Check for valid dimensions (at least 800x600) and valid position
    return (width >= _MIN_CLIENT_W and height >= _MIN_CLIENT_H
            and left > -10000 and top > -10000)


def _geometry(left, top, width, height):
    capture_region = {
        "top": top,
        "left": left,
        "width": width,
        "height": height,
    }
    scale_x = width / REF_WIDTH
    scale_y = height / REF_HEIGHT
    return capture_region, scale_x, scale_y


def read_game_geometry(hwnd):
    """Non-blocking single read of the game's capture geometry.

    Returns (capture_region, scale_x, scale_y) when the window exists, is
    restored and is at least 800x600 on screen; None otherwise
    (minimized, too small, off-screen, or the window is gone). Callers
    treat None as "keep the last-known geometry".

    Cheap — IsWindow / IsIconic / GetClientRect / ClientToScreen read
    kernel-side window state and never message the game — so it is
    safe to call every few hundred ms from a worker thread to track a
    windowed game being moved or resized."""
    if not hwnd:
        return None
    user32 = _user32()
    try:
        if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
            return None
        left, top, width, height = _client_area(hwnd)
    except Exception:
        return None
    if not _usable(left, top, width, height):
        return None
    return _geometry(left, top, width, height)


def get_game_geometry(hwnd, stop_evt=None, status_cb=None):
    """Get the game client area position and size from its window handle.

    Waits for the game window to be restored and visible. If `stop_evt` is
    provided and gets set during the wait, returns None so a UI Stop
    button can abort the bot before the user has alt-tabbed back to the
    game.

    Also returns None (without `stop_evt` being set) if the window no
    longer exists — e.g. the game was closed. Callers that need to tell
    the two apart check `stop_evt.is_set()` after the call, the same
    escape hatch `InputBackend.move_to` uses.

    `status_cb` (optional `callable(dict)`) receives a user-facing
    `last_error` while we are blocked waiting on the user (minimized /
    off-screen / closed), and `last_error: ''` once a reported wait
    clears so the UI doesn't keep showing a stale problem.

    Returns:
        (capture_region, scale_x, scale_y) on success, or None if stopped
        or the window is gone.
    """
    user32 = _user32()

    SW_RESTORE = 9
    first_wait = True
    reported = False

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

        if not user32.IsWindow(hwnd):
            print("ERROR: Genshin Impact window closed.")
            _report(status_cb, MSG_WINDOW_CLOSED)
            return None

        if user32.IsIconic(hwnd):
            if first_wait:
                print("Game window is minimized. Attempting to restore...")
                user32.ShowWindow(hwnd, SW_RESTORE)
                print("Please Alt-Tab into Genshin Impact...")
                first_wait = False
                _report(status_cb, MSG_WINDOW_MINIMIZED)
                reported = True
            if _sleep_or_stop(1):
                return None
            continue

        left, top, client_width, client_height = _client_area(hwnd)

        if _usable(left, top, client_width, client_height):
            break

        if first_wait:
            print(f"Waiting for game window to be fully visible (current size: {client_width}x{client_height})...")
            first_wait = False
            _report(status_cb, MSG_WINDOW_NOT_VISIBLE)
            reported = True
        if _sleep_or_stop(1):
            return None

    if reported:
        _report(status_cb, '')
    return _geometry(left, top, client_width, client_height)


def auto_detect(stop_evt=None, status_cb=None):
    """Auto-detect game window and compute all positions.

    `status_cb` (optional) receives a `last_error` when the game window
    can't be found or we're waiting on the user to restore it.

    Returns:
        (capture_region, column_centers, hit_line_y) on success, or None
        if the window wasn't found / was closed, or stop_evt was set while
        waiting for the game window (check `stop_evt.is_set()` to tell
        them apart).
    """
    hwnd = find_game_window(verbose=True)
    if not hwnd:
        titles = ", ".join(f'"{t}"' for t in GAME_WINDOW_TITLE)
        print(f"ERROR: Could not find Genshin Impact window ({titles}).")
        print("Make sure the game is running before starting the bot.")
        _report(status_cb, MSG_WINDOW_NOT_FOUND)
        return None

    geom = get_game_geometry(hwnd, stop_evt=stop_evt, status_cb=status_cb)
    if geom is None:
        return None
    capture_region, scale_x, scale_y = geom

    # Scale reference 1080p positions to actual resolution.
    # Positions are relative to the client area (capture region), so no
    # additional offset is needed — mss captures from capture_region origin.
    column_centers, hit_line_y = scaled_layout(scale_x, scale_y)

    print(f"Game resolution: {capture_region['width']}x{capture_region['height']}")
    print(f"Game position: ({capture_region['left']}, {capture_region['top']})")
    print(f"Scale: {scale_x:.3f}x, {scale_y:.3f}y")
    print(f"Column centers: {column_centers}")
    print(f"Hit line Y: {hit_line_y}")

    return capture_region, column_centers, hit_line_y
