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
