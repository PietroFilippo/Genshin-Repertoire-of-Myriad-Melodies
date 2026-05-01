# pc_client/ui_core.py
"""
UI-agnostic logic shared by any front-end. Holds the bot orchestration
(`BotController`), keybind plumbing (`KeybindManager`), settings
persistence, admin check, foreground gate, and a thread-safe
stdout/stderr queue redirector. The PyWebView entry point in `ui.py`
consumes this module; no Tk dependency.

Admin elevation is now handled by the PyInstaller-embedded UAC manifest
(`uac-admin`), so the runtime no longer self-elevates. Dev launches via
`python ui.py` show a warning message box if not admin.
"""
import ctypes
import ctypes.wintypes
import json
import os
import queue
import sys
import threading
from pathlib import Path

import keyboard

import main as bot_main
from album import AlbumRunner
from config import (ALBUM_DIFFICULTY, ALBUM_SONG_COUNT, GAME_WINDOW_TITLE,
                    UI_KEYBINDS_DEFAULT)


UI_WINDOW_TITLE = 'Genshin Rhythm Bot'


def is_frozen():
    """True when running from a PyInstaller bundle."""
    return getattr(sys, 'frozen', False)


def settings_path():
    """User-writable settings file. For the bundled .exe, sit next to the
    executable (onedir folder is user-writable). For dev launches, sit
    next to the source script."""
    if is_frozen():
        return Path(sys.executable).parent / 'ui_settings.json'
    return Path(__file__).parent / 'ui_settings.json'


# MessageBoxW flags.
_MB_OK = 0x0
_MB_ICONWARNING = 0x30


# --- admin check ------------------------------------------------------------
#
# Genshin Impact's anti-cheat (mhyprot) blocks low-level keyboard hooks
# coming from non-elevated processes. Without admin, the `keyboard`
# package's hotkeys silently drop while Genshin owns the foreground.
# The bundled .exe carries a `requireAdministrator` manifest so UAC fires
# on launch — by the time we reach Python, we are guaranteed admin. For
# dev launches (`python ui.py`) we just check + warn.

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _message_box(text, title, flags=_MB_ICONWARNING | _MB_OK):
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    except Exception:
        pass


def warn_if_not_admin():
    """Surface a native message box if we're not running as admin. The
    bundled .exe never reaches this path (manifest enforces elevation).
    Dev launches without admin still get a visible warning."""
    if is_admin():
        return
    _message_box(
        "Not running as administrator — Genshin's anti-cheat blocks "
        "hotkeys from non-admin processes, so the configured hotkeys "
        "will not work while the game is focused.\n\n"
        "Close this window and relaunch as administrator (right-click "
        "the launcher → \"Run as administrator\").\n\n"
        "The UI buttons (Start / Stop / Debug toggle) will still "
        "work normally.",
        'Administrator required')


# Action names — used as dict keys in keybinds + UI labels.
ACTION_START_STOP = 'start_stop'
ACTION_PAUSE = 'pause'
ACTION_DEBUG = 'debug'
ACTION_LABELS = {
    ACTION_START_STOP: 'Start / Stop',
    ACTION_PAUSE:      'Pause / Resume',
    ACTION_DEBUG:      'Toggle Debug',
}


# --- settings persistence ---------------------------------------------------

def load_settings():
    """Load UI settings (currently just keybinds). Falls back to config
    defaults on missing file or parse errors."""
    p = settings_path()
    if not p.exists():
        return {'keybinds': dict(UI_KEYBINDS_DEFAULT)}
    try:
        with p.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        kb = dict(UI_KEYBINDS_DEFAULT)
        kb.update(data.get('keybinds', {}))
        return {'keybinds': kb}
    except Exception as e:
        print(f"[ui] failed to load settings: {e} — using defaults")
        return {'keybinds': dict(UI_KEYBINDS_DEFAULT)}


def save_settings(settings):
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('w', encoding='utf-8') as fh:
            json.dump(settings, fh, indent=2)
    except Exception as e:
        print(f"[ui] failed to save settings: {e}")


# --- log pane plumbing ------------------------------------------------------

class QueueWriter:
    """File-like object that pushes writes onto a thread-safe queue. The UI
    drain loop pops from this queue and forwards to the JS log pane via
    evaluate_js. Avoids any UI-thread coupling — every worker thread
    (detector, album loop, keyboard package listener) writes here lock-free."""

    def __init__(self, q, mirror=None):
        self._q = q
        # mirror lets us tee to the real stdout/stderr too — handy when
        # ui.py is launched from a console for live debugging.
        self._mirror = mirror

    def write(self, s):
        if not s:
            return 0
        try:
            self._q.put_nowait(s)
        except queue.Full:
            pass
        if self._mirror is not None:
            try:
                self._mirror.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        if self._mirror is not None:
            try:
                self._mirror.flush()
            except Exception:
                pass

    def isatty(self):
        return False


# --- foreground-window gate -------------------------------------------------

def _foreground_hwnd():
    return ctypes.windll.user32.GetForegroundWindow()


def _window_title(hwnd):
    """Read a window's title text. Empty string on failure."""
    if not hwnd:
        return ''
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


# Genshin's pause menu doesn't change the window title, but in some cases
# (alt-tab from pause, overlay popups) the focused HWND temporarily belongs
# to a child/overlay window with a different title. The process exe is the
# stable identifier across all of these.
_GAME_EXE_NAMES = {'GenshinImpact.exe', 'YuanShen.exe'}


def _foreground_process_name():
    """Best-effort: return the exe basename of the process that owns the
    foreground window. Empty string on any failure (used for fallback
    matching, so silent failure is fine)."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    fg = user32.GetForegroundWindow()
    if not fg:
        return ''
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    if not pid.value:
        return ''
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                             pid.value)
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = ctypes.wintypes.DWORD(len(buf))
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        if not ok:
            return ''
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)


def is_allowed_foreground(ui_hwnd):
    """Hotkey gate: pass if the focused window is the Genshin client OR
    the bot UI itself. Falls back to process-exe match so the gate keeps
    passing while Genshin is paused / showing an overlay (the focused
    HWND may not have the "Genshin Impact" title in those states)."""
    fg = _foreground_hwnd()
    if not fg:
        return False
    if ui_hwnd and fg == ui_hwnd:
        return True
    title = _window_title(fg)
    if title == UI_WINDOW_TITLE:
        return True
    if title in GAME_WINDOW_TITLE:
        return True
    proc = _foreground_process_name()
    return proc in _GAME_EXE_NAMES


# --- keybind manager --------------------------------------------------------

class KeybindManager:
    """Thin wrapper around `keyboard.add_hotkey` with foreground gating and
    rebind support. Each action has at most one active hook at a time."""

    def __init__(self, ui_hwnd_provider):
        # ui_hwnd_provider() returns the current UI HWND. Called per-key
        # so it stays correct if the underlying window is recreated.
        self._ui_hwnd_provider = ui_hwnd_provider
        self._hooks = {}      # action -> hook handle
        self._bindings = {}   # action -> hotkey string
        self._callbacks = {}  # action -> python callable

    def set(self, action, hotkey, callback):
        """Bind (or rebind) `action` to `hotkey`. Replaces any existing
        binding for the same action."""
        self.clear(action)
        if not hotkey:
            return
        wrapped = self._gate(callback)
        try:
            handle = keyboard.add_hotkey(hotkey, wrapped, suppress=False)
        except Exception as e:
            print(f"[ui] failed to bind {hotkey!r} for {action}: {e}")
            return
        self._hooks[action] = handle
        self._bindings[action] = hotkey
        self._callbacks[action] = callback

    def clear(self, action):
        h = self._hooks.pop(action, None)
        if h is not None:
            try:
                keyboard.remove_hotkey(h)
            except KeyError:
                pass
        self._bindings.pop(action, None)
        self._callbacks.pop(action, None)

    def clear_all(self):
        for action in list(self._hooks):
            self.clear(action)

    def _gate(self, callback):
        def wrapper():
            try:
                ui_hwnd = self._ui_hwnd_provider()
            except Exception:
                ui_hwnd = 0
            if not is_allowed_foreground(ui_hwnd):
                return
            try:
                callback()
            except Exception as e:
                print(f"[ui] hotkey callback error: {e}")
        return wrapper


# --- JS keydown -> `keyboard` package hotkey string -------------------------

# DOM key names that are pure modifiers — wait for the actual key.
_MODIFIER_KEYS = {'Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'OS'}

# Map a few special DOM key names to what the `keyboard` package expects.
# Most keys (letters, digits, F-keys lowercased) work as-is; this handles
# the ones that don't.
_KEY_NAME_FIXUPS = {
    ' ': 'space',
    'Spacebar': 'space',
    'Escape': 'esc',
    'Esc': 'esc',
    'Enter': 'enter',
    'Return': 'enter',
    'Tab': 'tab',
    'Backspace': 'backspace',
    'Delete': 'delete',
    'Del': 'delete',
    'Insert': 'insert',
    'Ins': 'insert',
    'Home': 'home',
    'End': 'end',
    'PageUp': 'page up',
    'PageDown': 'page down',
    'ArrowUp': 'up',
    'ArrowDown': 'down',
    'ArrowLeft': 'left',
    'ArrowRight': 'right',
    'CapsLock': 'caps lock',
    'NumLock': 'num lock',
    'ScrollLock': 'scroll lock',
    'PrintScreen': 'print screen',
    'Pause': 'pause',
    'ContextMenu': 'menu',
}


def js_event_to_hotkey(payload):
    """Translate a JS keydown payload to a `keyboard` package hotkey string.
    Payload shape: {'key': str, 'code': str, 'ctrlKey': bool,
    'altKey': bool, 'shiftKey': bool, 'metaKey': bool}.

    Returns lowercase hotkey like 'ctrl+f8' or 'alt+t', or None if the
    event was just a modifier press."""
    if not isinstance(payload, dict):
        return None
    key = payload.get('key', '')
    if not key or key in _MODIFIER_KEYS:
        return None
    # Letter / digit: lowercase. F-keys: 'F8' -> 'f8'. Punctuation passes
    # through and is fixed up via the table below.
    norm = _KEY_NAME_FIXUPS.get(key, key)
    if len(norm) == 1:
        norm = norm.lower()
    elif norm.startswith('F') and norm[1:].isdigit():
        norm = norm.lower()
    parts = []
    if payload.get('ctrlKey'):
        parts.append('ctrl')
    if payload.get('altKey'):
        parts.append('alt')
    if payload.get('shiftKey'):
        parts.append('shift')
    parts.append(norm)
    return '+'.join(parts)


# --- bot orchestration ------------------------------------------------------

class BotController:
    """Owns the bot worker thread and the events it watches. Exposes
    start / pause / resume / stop / toggle_debug methods called from the
    UI thread (or from hotkey callbacks dispatched on the keyboard
    package's listener thread)."""

    STATE_IDLE = 'idle'
    STATE_RUNNING = 'running'
    STATE_PAUSED = 'paused'
    STATE_STOPPING = 'stopping'

    def __init__(self, on_status):
        self._on_status = on_status
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._debug_evt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._timer_boosted = False
        self.state = self.STATE_IDLE
        self._mode = ''
        self._mode_options = {}

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, mode, options):
        """Spawn the worker. mode: 'standalone' | 'album'.
        options for album: {'songs': int, 'difficulty': str,
        'replay_canorus': bool, 'debug': bool}."""
        with self._lock:
            if self.is_running():
                return False
            self._stop_evt.clear()
            self._pause_evt.clear()
            if options.get('debug', False):
                self._debug_evt.set()
            else:
                self._debug_evt.clear()

            self._mode = mode
            self._mode_options = dict(options)
            self.state = self.STATE_RUNNING
            self._emit({'state': self.state, 'mode': mode,
                        'debug': self._debug_evt.is_set()})

            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f'bot-{mode}')
            self._thread.start()
            return True

    def stop(self):
        if not self.is_running():
            self.state = self.STATE_IDLE
            self._emit({'state': self.state})
            return
        # Reflect "stopping" immediately so the UI updates on the next
        # status tick instead of waiting for the worker thread to actually
        # exit (which can take 100ms+ if it has to tear down a cv2 window
        # or finish the current detector poll).
        self._stop_evt.set()
        self._pause_evt.clear()  # unblock any pause-waiters
        self.state = self.STATE_STOPPING
        self._emit({'state': self.state})

    def pause_toggle(self):
        if not self.is_running():
            return
        if self._pause_evt.is_set():
            self._pause_evt.clear()
            self.state = self.STATE_RUNNING
            self._emit({'state': self.state})
        else:
            self._pause_evt.set()
            self.state = self.STATE_PAUSED
            self._emit({'state': self.state})

    def toggle_debug(self):
        if self._debug_evt.is_set():
            self._debug_evt.clear()
        else:
            self._debug_evt.set()
        self._emit({'debug': self._debug_evt.is_set()})

    def shutdown(self):
        """Final teardown — called on UI close. Stop the bot and wait."""
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._timer_boosted:
            bot_main.restore_timer()
            self._timer_boosted = False

    def _emit(self, update):
        try:
            self._on_status(update)
        except Exception:
            pass

    def _ensure_system_setup(self):
        """One-time DPI + timer boost. Idempotent across runs because
        timeBeginPeriod/EndPeriod are reference-counted by the OS."""
        bot_main.set_dpi_aware()
        if not self._timer_boosted:
            self._timer_boosted = bot_main.boost_timer()

    def _run(self):
        try:
            self._ensure_system_setup()
            if self._mode == 'standalone':
                bot_main.run_standalone(self._stop_evt, self._debug_evt,
                                        status_cb=self._emit,
                                        pause_evt=self._pause_evt)
            elif self._mode == 'album':
                opts = self._mode_options
                runner = AlbumRunner(replay_canorus=opts.get('replay_canorus', False),
                                     difficulty=opts.get('difficulty', ALBUM_DIFFICULTY),
                                     stop_evt=self._stop_evt,
                                     pause_evt=self._pause_evt,
                                     debug_evt=self._debug_evt,
                                     status_cb=self._emit)
                try:
                    runner.run(songs_count=opts.get('songs', ALBUM_SONG_COUNT))
                finally:
                    runner.close()
            else:
                print(f"[ui] unknown mode: {self._mode}")
        except Exception as e:
            print(f"[ui] worker crashed: {e}")
        finally:
            self.state = self.STATE_IDLE
            self._emit({'state': self.state, 'fps': 0.0, 'song': ''})
