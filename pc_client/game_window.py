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
# Also the reason a run ends when the game is closed mid-run (rhythm
# phase, album navigation) — see `GameWindowRef.classify`.
MSG_WINDOW_CLOSED = ("Genshin Impact window closed — start the game, "
                     "then press Start.")
# The run's window was destroyed or recycled while the game kept running
# (e.g. it recreated its window on a fullscreen / resolution switch). The
# run never follows another window on its own.
MSG_WINDOW_CHANGED = ("The game window changed (fullscreen or resolution "
                      "switch?) — press Start to continue.")
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


def same_hwnd(a, b):
    """HWND equality that survives ctypes restype differences. ui.py binds
    `FindWindowW.restype = HWND` on the process-global user32 (unsigned,
    sign-extended to 64 bits), while GetForegroundWindow keeps the default
    c_int (signed) — so for a handle with bit 31 set a raw `==` says two
    handles to the SAME window differ. HWNDs carry only 32 significant
    bits: compare the low 32. Null / missing handles never match (same
    semantics as ui_core._same_hwnd). Use this for every HWND comparison
    in the rhythm-engine files — a false "different window" in the
    detector's foreground gate would silently stop the bot playing."""
    try:
        a = int(a or 0)
        b = int(b or 0)
    except (TypeError, ValueError):
        return False
    return bool(a) and bool(b) and (a & 0xFFFFFFFF) == (b & 0xFFFFFFFF)


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


def window_gone(hwnd):
    """Non-blocking: True when this window handle no longer exists —
    `IsWindow` is false, or there is no handle at all. That alone does
    NOT mean Genshin was closed (it may have recreated its window): a
    run asks its `GameWindowRef.classify()`.

    A minimized, hidden, too-small or off-screen window is NOT gone:
    callers keep waiting for it. An error reading the state counts as
    "still there", so a ctypes hiccup can never end a run. `IsWindow`
    reads kernel-side window state and never messages the game, so this
    is safe every few hundred ms from any thread (not on the detector's
    5 ms hot path, though)."""
    if not hwnd:
        return True
    try:
        # HWND-wrapped: a handle that went through an HWND restype
        # (sign-extended to 64 bits) overflows the default c_int argument.
        return not _user32().IsWindow(ctypes.wintypes.HWND(hwnd))
    except Exception:
        return False


def read_game_geometry(hwnd):
    """Non-blocking single read of the game's capture geometry.

    Returns (capture_region, scale_x, scale_y) when the window exists, is
    restored and is at least 800x600 on screen; None otherwise
    (minimized, too small, off-screen, or the window is gone). Callers
    treat None as "keep the last-known geometry"; a run's
    `GameWindowRef.classify()` tells a closed game / changed window (end
    the run) from a minimized one (wait).

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


# --- is the run's game window still the game's? -------------------------------
#
# A run is bound to the window it started on AND to the game process owning
# it (PID + a process handle held for the whole run, which also keeps the
# PID from being reused). No automatic adoption of another window — not by
# title, not by PID: every "the window isn't ours any more" ends the run.
#
# `GameWindowRef.classify()` outcomes:
WINDOW_ALIVE = 'alive'      # our window, owned by the game (minimized ok)
WINDOW_CHANGED = 'changed'  # destroyed / recycled while the game still runs
WINDOW_CLOSED = 'closed'    # the game process exited

# When the window is no longer ours, how long `classify` looks (stop-aware,
# every LOST_POLL_S) before choosing CLOSED vs CHANGED: the game may still
# be exiting after destroying its window (handle not signaled yet), and
# without a handle a recreated window may take a moment to appear. The run
# ends either way; this only picks the right message.
LOST_CONFIRM_S = 2.0
LOST_POLL_S = 0.25

# How often to retry reading the starting window's PID.
_PID_READ_ATTEMPTS = 3
_PID_READ_RETRY_S = 0.05

_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0

try:
    # Private handle (own prototypes) so nothing leaks onto the
    # process-global `ctypes.windll.kernel32`.
    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                 ctypes.wintypes.DWORD]
    _k32.OpenProcess.restype = ctypes.wintypes.HANDLE
    _k32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE,
                                         ctypes.wintypes.DWORD]
    _k32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
    _k32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    _k32.CloseHandle.restype = ctypes.wintypes.BOOL
except (AttributeError, OSError):
    _k32 = None


def window_pid(hwnd):
    """PID owning `hwnd`, 0 if unknown. GetWindowThreadProcessId reads the
    window object; it never messages the window."""
    if not hwnd:
        return 0
    pid = ctypes.wintypes.DWORD(0)
    try:
        _user32().GetWindowThreadProcessId(ctypes.wintypes.HWND(hwnd),
                                           ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value or 0)


def _open_process(pid):
    """SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION handle, or None
    (e.g. access denied). Holding it keeps the PID from being reused."""
    if not pid or _k32 is None:
        return None
    try:
        return _k32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False,
            pid) or None
    except Exception:
        return None


def _process_exited(handle):
    """True once the process behind `handle` has exited (handle signaled).
    A failed wait counts as "still running"."""
    try:
        return _k32.WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0
    except Exception:
        return False


def _close_handle(handle):
    try:
        _k32.CloseHandle(handle)
    except Exception:
        pass


def _lost_wait(stop_evt, secs):
    """Stop-aware sleep; True if stop fired."""
    if stop_evt is not None:
        return stop_evt.wait(secs)
    time.sleep(secs)
    return False


def lost_message(state):
    """The `last_error` for a run that ended on `state`."""
    return MSG_WINDOW_CLOSED if state == WINDOW_CLOSED else MSG_WINDOW_CHANGED


class GameWindowRef:
    """The game window a run is bound to — fixed for the whole run: HWND,
    the PID owning it (read with a few retries; 0 = unreadable) and a
    process handle (None if OpenProcess failed). Never re-pointed, so any
    thread may query it. `close()` it when the run ends."""

    def __init__(self, hwnd, pid=None):
        self.hwnd = hwnd
        if pid is None:
            pid = window_pid(hwnd)
            for _ in range(_PID_READ_ATTEMPTS - 1):
                if pid or window_gone(hwnd):
                    break
                time.sleep(_PID_READ_RETRY_S)
                pid = window_pid(hwnd)
        self.pid = pid or 0
        self.handle = _open_process(self.pid)

    def _window_is_ours(self):
        """IsWindow(hwnd) and still owned by the game's PID. PID unknown
        (ours or the window's): IsWindow alone decides."""
        if window_gone(self.hwnd):
            return False
        if not self.pid:
            return True
        owner = window_pid(self.hwnd)
        return not owner or owner == self.pid

    def alive(self):
        """Non-blocking, side-effect free: the process hasn't exited (when
        we hold its handle) and the window is still ours (minimized /
        hidden included). The pre-click check: a recycled foreign HWND
        with a perfectly valid rect is NOT alive."""
        if self.handle is not None and _process_exited(self.handle):
            return False
        return self._window_is_ours()

    def classify(self, stop_evt=None):
        """WINDOW_ALIVE / WINDOW_CHANGED / WINDOW_CLOSED. Non-blocking
        while the window is ours (cheap enough for every ~0.5 s poll).
        Otherwise it looks for up to LOST_CONFIRM_S (stop-aware) to pick
        the message — the run ends either way:
          - with the process handle: CLOSED once it is signaled, else
            CHANGED (the game still runs on some other window);
          - without one: CHANGED as soon as a window titled
            GAME_WINDOW_TITLE exists, CLOSED if none appears (absence
            confirmed across several looks, a recreate gap is brief).
        A Stop during the look returns WINDOW_CLOSED — callers let Stop
        win (they check stop_evt) and report nothing."""
        if self.handle is not None and _process_exited(self.handle):
            return WINDOW_CLOSED
        if self._window_is_ours():
            return WINDOW_ALIVE
        deadline = time.monotonic() + LOST_CONFIRM_S
        while True:
            if self.handle is not None:
                if _process_exited(self.handle):
                    return WINDOW_CLOSED
            elif find_game_window():
                return WINDOW_CHANGED
            if time.monotonic() >= deadline:
                break
            if _lost_wait(stop_evt, LOST_POLL_S):
                return WINDOW_CLOSED
        return WINDOW_CHANGED if self.handle is not None else WINDOW_CLOSED

    def close(self):
        """Release the process handle (run end). Idempotent."""
        handle, self.handle = self.handle, None
        if handle is not None:
            _close_handle(handle)


def wait_game_geometry(window, stop_evt=None, status_cb=None):
    """`get_game_geometry` for a run's `GameWindowRef`: blocking and stop-
    aware like it (waits while minimized / off-screen), but checked
    against the run's window and process. Returns (capture_region,
    scale_x, scale_y), or None when stopped (check `stop_evt.is_set()`)
    or when the window is no longer the game's — MSG_WINDOW_CLOSED /
    MSG_WINDOW_CHANGED reported."""
    while True:
        if stop_evt is not None and stop_evt.is_set():
            return None
        state = window.classify(stop_evt)
        if stop_evt is not None and stop_evt.is_set():
            return None
        if state != WINDOW_ALIVE:
            break
        geom = get_game_geometry(window.hwnd, stop_evt=stop_evt,
                                 status_cb=status_cb, report_closed=False)
        if geom is not None and window.alive():
            return geom
        # Stopped, or the window died / was recycled during the wait:
        # loop — classify says which.
    print(f"ERROR: {lost_message(state)}")
    _report(status_cb, lost_message(state))
    return None


def get_game_geometry(hwnd, stop_evt=None, status_cb=None,
                      report_closed=True):
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

    `report_closed=False` leaves "the window is gone" to the caller (no
    log line, no MSG_WINDOW_CLOSED; a reported wait is cleared) — used by
    `wait_game_geometry`, which tells "closed" from "changed".

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
            if not report_closed:
                if reported:
                    _report(status_cb, '')
                return None
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
