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
# phase, album navigation) — see `resolve_game_window`.
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
    NOT mean Genshin was closed (it may have recreated its window): to
    decide whether a run should end, use `resolve_game_window`.

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
    treat None as "keep the last-known geometry" and use
    `resolve_game_window` to tell a closed game (end the run) or a
    recreated window (adopt it) from a minimized one (wait).

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


# --- following the game across a recreated window ---------------------------
#
# Genshin can RECREATE its window (fullscreen / Alt+Enter toggle, resolution
# or display-mode change) while the game keeps running: the old HWND dies,
# possibly with a gap before the new one exists. A run is therefore anchored
# to the game PROCESS (the PID owning the window it started on), not to an
# HWND or a title — an unrelated window captioned "Genshin Impact" must
# never be adopted (the rebuilt detector would type into it).
#
# `resolve_game_window(hwnd, pid)` outcomes:
WINDOW_ALIVE = 'alive'        # `hwnd` exists (minimized / hidden included)
WINDOW_REPLACED = 'replaced'  # dead; the SAME process has a new game window
WINDOW_PENDING = 'pending'    # dead; no game window yet, process still alive
WINDOW_CLOSED = 'closed'      # dead; the game process is gone

# A PENDING game (process alive, no game window) is waited for like a
# minimized one — no input, polled every ~0.5 s, never reported as closed —
# for at most this long. Real recreate gaps last milliseconds to seconds; a
# process lingering a minute without any game window (hung on exit, crash
# dialog) is gone as far as the run is concerned: MSG_WINDOW_CLOSED.
PENDING_MAX_S = 60.0
# Poll cadence while blocked on a PENDING game (follow_game_geometry).
PENDING_POLL_S = 0.5

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87

_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND,
                                  ctypes.wintypes.LPARAM)

try:
    # Private handle (own prototypes + last-error) so nothing leaks onto
    # the process-global `ctypes.windll.kernel32`.
    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                 ctypes.wintypes.DWORD]
    _k32.OpenProcess.restype = ctypes.wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = [ctypes.wintypes.HANDLE,
                                        ctypes.POINTER(ctypes.wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
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


def _top_level_windows():
    """Every top-level window (visible or not), via EnumWindows."""
    found = []

    def collect(hwnd, _lparam):
        found.append(hwnd or 0)
        return True

    try:
        _user32().EnumWindows(_WNDENUMPROC(collect),
                              ctypes.wintypes.LPARAM(0))
    except Exception:
        pass
    return [h for h in found if h]


def _window_title(hwnd):
    """Caption of a top-level window. For another process's window
    GetWindowTextW reads the stored caption — no WM_GETTEXT, can't block
    on a busy game. Only called for windows the game process owns."""
    buf = ctypes.create_unicode_buffer(256)
    try:
        _user32().GetWindowTextW(ctypes.wintypes.HWND(hwnd), buf, len(buf))
    except Exception:
        return ''
    return buf.value


def _process_alive(pid):
    """True / False if the process is known running / exited; None if it
    can't be told (e.g. access denied) — callers treat None as alive."""
    if not pid or _k32 is None:
        return None
    try:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False,
                                  pid)
        if not handle:
            # ERROR_INVALID_PARAMETER: no such process (it exited and
            # nothing holds it open). Anything else: unknown.
            if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
                return False
            return None
        try:
            code = ctypes.wintypes.DWORD(0)
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    except Exception:
        return None


def resolve_game_window(hwnd, pid=0):
    """Where the run's game window is now -> (state, hwnd, pid):

    - WINDOW_ALIVE: `hwnd` exists (and, PID known, still belongs to it).
    - WINDOW_REPLACED: `hwnd` is dead, but the game process (`pid`) owns a
      top-level window titled GAME_WINDOW_TITLE — the returned handle.
      Never a window of another process, whatever its title.
    - WINDOW_PENDING: no such window, but the process is alive (it still
      owns some top-level window — Unity / IME keep hidden ones — or
      OpenProcess says STILL_ACTIVE / can't tell): a destroy -> create gap.
    - WINDOW_CLOSED: the process is gone.

    Unknown `pid` (0): the old title lookup, adopting only a window whose
    PID can be read (returned as the new anchor); otherwise CLOSED.

    Non-blocking (IsWindow, GetWindowThreadProcessId, EnumWindows,
    GetWindowTextW on the game's own windows, OpenProcess) — never messages
    the game. For the ~0.5 s owner-thread polls only; never on the
    detector's 5 ms loops. Use `GameWindowRef` rather than calling this
    directly: it also caps PENDING."""
    titles = set(GAME_WINDOW_TITLE)
    if hwnd and not window_gone(hwnd):
        owner = window_pid(hwnd) if pid else 0
        if not owner or owner == pid:      # only a KNOWN mismatch rejects
            return WINDOW_ALIVE, hwnd, pid
    if pid:
        owned = [h for h in _top_level_windows() if window_pid(h) == pid]
        for h in owned:
            if _window_title(h) in titles:
                return WINDOW_REPLACED, h, pid
        if owned or _process_alive(pid) is not False:
            return WINDOW_PENDING, hwnd, pid
        return WINDOW_CLOSED, hwnd, pid
    new = find_game_window()
    if new and not window_gone(new):
        new_pid = window_pid(new)
        if new_pid:                        # never adopt an unreadable PID
            return WINDOW_REPLACED, new, new_pid
    return WINDOW_CLOSED, hwnd, pid


class GameWindowRef:
    """The game window a run is bound to: HWND + the game's PID (captured
    from the starting HWND unless given). `check()` re-resolves it and
    adopts a recreated window of the same process.

    OWNER-THREAD ONLY: the thread running the run (album runner thread,
    standalone loop, CLI main loop) is the only one that may call
    `check()` / read-modify `hwnd` — other threads (the album end watcher)
    never adopt or swap handles, so handles can't split between holders."""

    def __init__(self, hwnd, pid=None):
        self.hwnd = hwnd
        self.pid = window_pid(hwnd) if pid is None else (pid or 0)
        self._pending_since = None

    def check(self):
        """WINDOW_ALIVE / WINDOW_REPLACED (adopted: `hwnd` / `pid` now the
        new window's) / WINDOW_PENDING (wait like a minimized game) /
        WINDOW_CLOSED (process gone, or PENDING for PENDING_MAX_S)."""
        state, hwnd, pid = resolve_game_window(self.hwnd, self.pid)
        if state != WINDOW_PENDING:
            self._pending_since = None
        if state == WINDOW_REPLACED:
            self.hwnd, self.pid = hwnd, pid
            print("Genshin Impact window was recreated — following the "
                  "new window.")
        elif state == WINDOW_PENDING:
            now = time.monotonic()
            if self._pending_since is None:
                self._pending_since = now
                print("Genshin Impact window disappeared — waiting for the "
                      "game to recreate it.")
            elif now - self._pending_since >= PENDING_MAX_S:
                print(f"Genshin Impact window didn't come back within "
                      f"{PENDING_MAX_S:.0f} s.")
                return WINDOW_CLOSED
        return state


def follow_game_geometry(window, stop_evt=None, status_cb=None):
    """`get_game_geometry` for a `GameWindowRef` that follows a recreated
    game window: blocking and stop-aware like it (waits for a restored
    window; waits through a PENDING gap, polling every PENDING_POLL_S).
    Returns (capture_region, scale_x, scale_y) — `window.hwnd` may now be
    a new handle the caller must adopt — or None when stopped (check
    `stop_evt.is_set()`) or when the game is gone (MSG_WINDOW_CLOSED
    reported)."""
    while True:
        if stop_evt is not None and stop_evt.is_set():
            return None
        state = window.check()
        if state == WINDOW_CLOSED:
            break
        if state == WINDOW_PENDING:
            if stop_evt is not None:
                if stop_evt.wait(PENDING_POLL_S):
                    return None
            else:
                time.sleep(PENDING_POLL_S)
            continue
        geom = get_game_geometry(window.hwnd, stop_evt=stop_evt,
                                 status_cb=status_cb, report_closed=False)
        if geom is not None:
            return geom
        # Stopped, or the window vanished during the wait: re-resolve.
    print("ERROR: Genshin Impact window closed.")
    _report(status_cb, MSG_WINDOW_CLOSED)
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
    `follow_game_geometry`, which then follows a recreated window.

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

    # Follows a window the game recreates during the wait; callers
    # re-fetch the HWND with find_game_window() afterwards.
    geom = follow_game_geometry(GameWindowRef(hwnd), stop_evt=stop_evt,
                                status_cb=status_cb)
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
