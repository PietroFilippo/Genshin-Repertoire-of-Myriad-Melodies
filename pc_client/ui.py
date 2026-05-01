# pc_client/ui.py
"""
PyWebView front-end. Loads the HTML/CSS/JS in pc_client/web and bridges
JS calls to the Tk-free orchestration in `ui_core` (BotController +
KeybindManager). One drain thread @ 50ms snapshots the bot state and
log queue and pushes both to the page in a single evaluate_js call to
avoid hammering the cross-process JSON channel.

Run:
    python pc_client/ui.py
"""
import ctypes
import ctypes.wintypes
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import webview

import ui_core
from config import (ALBUM_DIFFICULTY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_SONG_COUNT)
from ui_core import (ACTION_DEBUG, ACTION_PAUSE, ACTION_START_STOP,
                     BotController, KeybindManager, QueueWriter,
                     UI_WINDOW_TITLE, ensure_admin_or_warn,
                     hide_attached_console, is_admin, js_event_to_hotkey,
                     load_settings, save_settings)

WEB_DIR = Path(__file__).parent / 'web'
INDEX_PATH = WEB_DIR / 'index.html'
ICON_PATH = Path(__file__).parent / 'assets' / 'icon.ico'

POLL_MS = 50          # status drain cadence — matches Tk version
LOG_QUEUE_SIZE = 10000

# Stable identity for Windows so the taskbar doesn't group us under
# python.exe and uses our own icon. Versioned so a future schema change
# doesn't merge with the old grouping. Must be set before any window
# is created.
APP_USER_MODEL_ID = 'GenshinRhythmBot.UI.1'

# Win32 constants for SendMessage(WM_SETICON), LoadImageW, SetClassLongPtrW.
_WM_SETICON = 0x0080
_ICON_SMALL = 0
_ICON_BIG = 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
# Class-icon offsets (negative because GetClassLong/SetClassLong takes a
# negative index for these "well-known" slots).
_GCLP_HICON = -14
_GCLP_HICONSM = -34


def _set_app_user_model_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
    except Exception as e:
        print(f"[ui] AppUserModelID set failed: {e}")


def _bind_user32_icon_apis():
    """Bind argtypes/restype for the user32 calls we need. Critical on
    x64: defaults truncate handles to 32 bits, so SendMessage / class-long
    setters end up writing garbage HICONs."""
    user32 = ctypes.windll.user32
    user32.LoadImageW.argtypes = [
        ctypes.wintypes.HINSTANCE, ctypes.wintypes.LPCWSTR,
        ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.LoadImageW.restype = ctypes.wintypes.HANDLE
    user32.SendMessageW.argtypes = [
        ctypes.wintypes.HWND, ctypes.c_uint,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = ctypes.wintypes.LPARAM
    user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
    user32.FindWindowW.restype = ctypes.wintypes.HWND
    # SetClassLongPtrW only exists on x64; on x86 the symbol is
    # SetClassLongW. ctypes will raise AttributeError if missing — caller
    # falls back.
    if hasattr(user32, 'SetClassLongPtrW'):
        user32.SetClassLongPtrW.argtypes = [
            ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_void_p,
        ]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
    return user32


def _set_window_icon(hwnd, icon_path):
    """Apply the .ico to a window so Win11's taskbar picks it up. Three
    things matter on Win11:
      1. ICON_BIG must be ≥256×256, otherwise the taskbar silently keeps
         the stale process-default (python.exe) icon. ICON_SMALL gets the
         titlebar/alt-tab small slot — keep that 32×32 for crispness.
      2. Setting the class icon via SetClassLongPtrW(GCLP_HICON/HICONSM)
         is the documented fallback the shell consults when WM_SETICON
         hasn't been processed yet — belt-and-suspenders.
      3. AppUserModelID must already be set (done in _set_app_user_model_id)
         so the taskbar groups by us, not by python.exe.
    """
    if not hwnd or not icon_path or not os.path.exists(icon_path):
        try:
            sys.__stdout__.write(
                f"[ui] icon skip: hwnd={hwnd} "
                f"path_exists={icon_path and os.path.exists(icon_path)}\n")
            sys.__stdout__.flush()
        except Exception:
            pass
        return
    user32 = _bind_user32_icon_apis()
    try:
        hicon_big = user32.LoadImageW(
            0, icon_path, _IMAGE_ICON, 256, 256, _LR_LOADFROMFILE)
        hicon_small = user32.LoadImageW(
            0, icon_path, _IMAGE_ICON, 32, 32, _LR_LOADFROMFILE)
        try:
            sys.__stdout__.write(
                f"[ui] icon load: big={hicon_big} small={hicon_small} "
                f"hwnd=0x{hwnd:x}\n")
            sys.__stdout__.flush()
        except Exception:
            pass
        if hicon_big:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG, hicon_big)
            if hasattr(user32, 'SetClassLongPtrW'):
                user32.SetClassLongPtrW(hwnd, _GCLP_HICON, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, hicon_small)
            if hasattr(user32, 'SetClassLongPtrW'):
                user32.SetClassLongPtrW(hwnd, _GCLP_HICONSM, hicon_small)
    except Exception as e:
        print(f"[ui] icon set failed: {e}")


class BridgeApi:
    """Public methods exposed to JS as `pywebview.api.*`. JS calls these
    directly; return values come back as a Promise on the JS side. Methods
    must be hashable on the main thread but pywebview dispatches each call
    on a dedicated thread, so anything that mutates BotController state is
    already safe (BotController locks internally)."""

    def __init__(self):
        self._window = None
        self._settings = load_settings()
        self._lock = threading.Lock()
        self._stopping = False

        # Live UI form state — kept in sync via the set_* methods so the
        # hotkey worker thread (which can't reach the JS layer) has a
        # snapshot to feed into BotController.start.
        self._opts = {
            'mode': 'standalone',
            'songs': ALBUM_SONG_COUNT,
            'difficulty': ALBUM_DIFFICULTY,
            'replay_canorus': False,
            'debug': False,
        }

        self._status_lock = threading.Lock()
        self._status = {
            'state': BotController.STATE_IDLE,
            'mode': '',
            'song': '',
            'fps': 0.0,
            'debug': False,
            'admin': is_admin(),
            'keybinds': dict(self._settings['keybinds']),
        }

        self._log_queue = queue.Queue(maxsize=LOG_QUEUE_SIZE)
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr

        self._bot = BotController(self._enqueue_status)
        self._keybinds = KeybindManager(self._ui_hwnd)
        self._drain_stop = threading.Event()
        self._drain_thread = None

    # ---- lifecycle hooks called from main() ----

    def attach_window(self, window):
        self._window = window
        # Redirect stdio after the page loads — log queue will be drained
        # once the JS appendLog binding is reachable.
        sys.stdout = QueueWriter(self._log_queue, mirror=self._orig_stdout)
        sys.stderr = QueueWriter(self._log_queue, mirror=self._orig_stderr)
        self._install_keybinds()
        self._drain_thread = threading.Thread(
            target=self._drain_loop, daemon=True, name='ui-drain')
        self._drain_thread.start()

    def detach(self):
        """Tear down on window close. Restore stdio first so any teardown
        prints land on the original streams."""
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._stopping = True
        self._drain_stop.set()
        try:
            self._keybinds.clear_all()
        except Exception:
            pass
        try:
            self._bot.shutdown()
        except Exception as e:
            print(f"[ui] bot shutdown error: {e}")

    # ---- JS-callable methods ----

    def get_initial_state(self):
        with self._lock:
            return {
                **self._opts,
                'admin': is_admin(),
                'keybinds': dict(self._settings['keybinds']),
                'difficulties': list(ALBUM_DIFFICULTY_COORDS),
            }

    def start_stop(self):
        if self._bot.is_running():
            self._bot.stop()
            return True
        with self._lock:
            opts = dict(self._opts)
        mode = opts.pop('mode', 'standalone')
        ok = self._bot.start(mode, opts)
        if not ok:
            print('[ui] start failed (already running?)')
        return bool(ok)

    def pause(self):
        if self._bot.is_running():
            self._bot.pause_toggle()
        return True

    def toggle_debug(self):
        if self._bot.is_running():
            self._bot.toggle_debug()
        return True

    def set_mode(self, mode):
        if mode in ('standalone', 'album'):
            with self._lock:
                self._opts['mode'] = mode
        return True

    def set_songs(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return False
        n = max(1, min(ALBUM_SONG_COUNT, n))
        with self._lock:
            self._opts['songs'] = n
        return True

    def set_difficulty(self, d):
        if d in ALBUM_DIFFICULTY_COORDS:
            with self._lock:
                self._opts['difficulty'] = d
        return True

    def set_replay_canorus(self, v):
        with self._lock:
            self._opts['replay_canorus'] = bool(v)
        return True

    def set_debug(self, v):
        v = bool(v)
        with self._lock:
            self._opts['debug'] = v
        # If running, mirror checkbox into a live toggle so it matches the
        # hotkey behavior (same shape as the Tk version).
        if self._bot.is_running() and self._status.get('debug') != v:
            self._bot.toggle_debug()
        return True

    def rebind(self, action, payload):
        """Translate a JS keydown payload to a `keyboard` package hotkey
        and rebind. Returns the new hotkey string, or None on parse fail."""
        hotkey = js_event_to_hotkey(payload)
        if not hotkey:
            return None
        with self._lock:
            self._settings['keybinds'][action] = hotkey
            save_settings(self._settings)
        self._install_keybinds()
        # Push the new keybind map immediately rather than waiting for the
        # next drain tick.
        self._enqueue_status({'keybinds': dict(self._settings['keybinds'])})
        return hotkey

    def clear_log(self):
        # JS already cleared the pane; just drain the backlog so a fast
        # post-clear write doesn't immediately re-fill it.
        try:
            while True:
                self._log_queue.get_nowait()
        except queue.Empty:
            pass
        return True

    def minimize(self):
        if self._window is not None:
            try:
                self._window.minimize()
            except Exception as e:
                print(f"[ui] minimize failed: {e}")
        return True

    def toggle_maximize(self):
        if self._window is not None:
            try:
                self._window.toggle_fullscreen()
            except Exception:
                pass
        return True

    def close(self):
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception as e:
                print(f"[ui] close failed: {e}")
        return True

    # ---- internal ----

    def _ui_hwnd(self):
        """HWND of the pywebview window. Cached after first successful
        probe — pywebview only exposes the native handle once `shown`
        fires, but we may be asked from a hotkey thread before that.
        Used by KeybindManager so the foreground gate matches our window
        without falling back to the (slower) title check.

        Try FindWindowW(title) first because that's the *top-level* HWND
        the OS tracks for foreground / taskbar; window.native.Handle on
        the EdgeChromium backend can be a child host on some setups."""
        cached = getattr(self, '_hwnd_cache', 0)
        if cached:
            return cached
        try:
            user32 = ctypes.windll.user32
            user32.FindWindowW.argtypes = [
                ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
            user32.FindWindowW.restype = ctypes.wintypes.HWND
            hwnd = user32.FindWindowW(None, UI_WINDOW_TITLE)
            if hwnd:
                self._hwnd_cache = int(hwnd)
                return self._hwnd_cache
        except Exception:
            pass
        # Fallback: pythonnet IntPtr. Slower (COM marshal) but works if
        # FindWindowW misses (e.g. title not yet applied).
        w = self._window
        if w is None:
            return 0
        try:
            native = getattr(w, 'native', None)
            if native is not None:
                handle = getattr(native, 'Handle', None)
                if handle is not None:
                    val = int(handle.ToInt64())
                    if val:
                        self._hwnd_cache = val
                        return val
        except Exception:
            pass
        return 0

    def resolve_hwnd_now(self):
        """Eager probe — call after the window is shown so the first
        hotkey press doesn't pay the FindWindowW + pythonnet round-trip."""
        self._hwnd_cache = 0
        return self._ui_hwnd()

    def _install_keybinds(self):
        self._keybinds.clear_all()
        kb = self._settings['keybinds']
        self._keybinds.set(ACTION_START_STOP, kb.get(ACTION_START_STOP),
                           self._hotkey_start_stop)
        self._keybinds.set(ACTION_PAUSE, kb.get(ACTION_PAUSE),
                           self._hotkey_pause)
        self._keybinds.set(ACTION_DEBUG, kb.get(ACTION_DEBUG),
                           self._hotkey_debug)

    # Hotkey callbacks run on the keyboard package's listener thread.
    # They only mutate threading.Events / BotController state (already
    # thread-safe); the UI catches up on the next drain tick.
    def _hotkey_start_stop(self):
        self._log_hotkey('start_stop')
        self.start_stop()

    def _hotkey_pause(self):
        self._log_hotkey('pause')
        if self._bot.is_running():
            self._bot.pause_toggle()

    def _hotkey_debug(self):
        self._log_hotkey('debug')
        if self._bot.is_running():
            self._bot.toggle_debug()

    def _log_hotkey(self, action):
        """Diagnostic — prints arrival timestamp + bot state. Enabled with
        env GENSHIN_BOT_UI_DEBUG=1 so hot path stays silent in normal use."""
        if os.environ.get('GENSHIN_BOT_UI_DEBUG') != '1':
            return
        ts = time.strftime('%H:%M:%S') + f'.{int((time.time()%1)*1000):03d}'
        running = self._bot.is_running()
        print(f"[hotkey {ts}] {action} fired — running={running}")

    def _enqueue_status(self, update):
        with self._status_lock:
            self._status.update(update)

    def _drain_loop(self):
        """Single-thread drain: snapshot status + drain log queue every
        POLL_MS, push both to the page in one evaluate_js call. Decoupling
        here keeps the JS channel quiet under burst log writes."""
        period = POLL_MS / 1000.0
        next_tick = time.monotonic()
        while not self._drain_stop.is_set():
            try:
                # Drain *all* queued log chunks each tick.
                chunks = []
                try:
                    while True:
                        chunks.append(self._log_queue.get_nowait())
                except queue.Empty:
                    pass
                with self._status_lock:
                    status = dict(self._status)
                    status['admin'] = is_admin()
                self._push(status, ''.join(chunks) if chunks else '')
            except Exception as e:
                # Best-effort logging, but don't let drain crash on a
                # transient evaluate_js failure (e.g. window torn down).
                try:
                    self._orig_stderr.write(f'[ui] drain error: {e}\n')
                except Exception:
                    pass
            next_tick += period
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # We fell behind — reset the schedule so we don't burn CPU
                # trying to catch up.
                next_tick = time.monotonic()

    def _push(self, status, log_text):
        if self._window is None or self._stopping:
            return
        # JSON-encode on the Python side so the JS argument is a literal
        # — avoids any worry about quote escaping in the evaluate_js
        # source string.
        status_json = json.dumps(status, ensure_ascii=False)
        log_json = json.dumps(log_text, ensure_ascii=False)
        script = (
            'try{if(window.updateStatus)updateStatus(' + status_json + ');}'
            'catch(e){console.error(e);} '
            + ('try{if(window.appendLog)appendLog(' + log_json + ');}'
               'catch(e){console.error(e);}' if log_text else '')
        )
        try:
            self._window.evaluate_js(script)
        except Exception:
            # Window gone or page not ready yet — silent skip so the next
            # tick still runs.
            pass


def _on_closed(api):
    api.detach()


def _on_shown(api, icon_path):
    """Apply the .ico via WM_SETICON once the HWND is realized. PyWebView's
    own icon= argument is inconsistent across backends; setting it here
    by hand is the canonical fix and also the moment the taskbar grabs
    its icon for the AppUserModelID. Also pre-resolve the HWND cache so
    the first hotkey press doesn't pay the title-search latency.
    Idempotent — safe to call from both `shown` and `loaded` events."""
    if getattr(api, '_icon_applied', False):
        return
    hwnd = api.resolve_hwnd_now()
    # Bypass the QueueWriter so this trace is visible on the launching
    # console even after stdio redirection.
    try:
        sys.__stdout__.write(f"[ui] shown event — resolved hwnd=0x{hwnd:x}\n")
        sys.__stdout__.flush()
    except Exception:
        pass
    if hwnd and icon_path:
        _set_window_icon(hwnd, icon_path)
    # If pywebview's native HWND differs from the FindWindowW result,
    # apply the icon to the other one too — costs nothing and covers
    # the case where the top-level taskbar window is the wrapper.
    try:
        w = api._window
        native = getattr(w, 'native', None) if w else None
        handle = getattr(native, 'Handle', None) if native else None
        if handle is not None:
            alt = int(handle.ToInt64())
            if alt and alt != hwnd:
                try:
                    sys.__stdout__.write(f"[ui] also applying icon to native HWND 0x{alt:x}\n")
                    sys.__stdout__.flush()
                except Exception:
                    pass
                _set_window_icon(alt, icon_path)
    except Exception as e:
        try:
            sys.__stdout__.write(f"[ui] alt-hwnd icon apply failed: {e}\n")
            sys.__stdout__.flush()
        except Exception:
            pass
    api._icon_applied = True


def main():
    # Self-elevate before any heavy lifting (keyboard hooks need admin to
    # fire while Genshin is foreground). If we elevated, this exits the
    # current (non-admin) process.
    ensure_admin_or_warn()
    hide_attached_console()
    # Must be set BEFORE create_window so the first taskbar registration
    # uses our identity instead of python.exe's.
    _set_app_user_model_id()

    if not INDEX_PATH.exists():
        raise SystemExit(f'[ui] missing UI assets: {INDEX_PATH}')

    api = BridgeApi()
    icon = str(ICON_PATH) if ICON_PATH.exists() else None

    window = webview.create_window(
        title=UI_WINDOW_TITLE,
        url=INDEX_PATH.as_uri(),
        js_api=api,
        width=720,
        height=720,
        min_size=(640, 600),
        frameless=True,
        easy_drag=False,
        resizable=True,
        background_color='#1a1a1a',
    )

    api.attach_window(window)
    window.events.closed += lambda: _on_closed(api)
    # Apply the icon on both shown (WPF surface visible) and loaded (page
    # content done). Some pywebview backends fire shown before native is
    # attached; loaded always fires after. _set_window_icon is idempotent
    # so doubling up costs nothing and covers either order.
    window.events.shown += lambda: _on_shown(api, icon)
    window.events.loaded += lambda: _on_shown(api, icon)

    # debug=True opens a right-click "Inspect" menu (DevTools) — handy
    # while the UI is being iterated; flip to False for normal use.
    debug_ui = os.environ.get('GENSHIN_BOT_UI_DEBUG') == '1'
    try:
        webview.start(icon=icon, debug=debug_ui)
    except TypeError:
        # Older pywebview builds don't accept icon=; fall back without.
        webview.start(debug=debug_ui)


if __name__ == '__main__':
    main()
