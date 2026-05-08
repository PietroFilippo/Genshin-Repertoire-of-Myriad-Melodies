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
import time
from pathlib import Path

import keyboard
import mouse

import main as bot_main
from album import AlbumRunner
from config import (ALBUM_DIFFICULTY, ALBUM_SONG_COUNT, GAME_WINDOW_TITLE,
                    KEYS, UI_KEYBINDS_DEFAULT)


UI_WINDOW_TITLE = 'Genshin Rhythm Bot'


def is_frozen():
    """True when running from a PyInstaller bundle."""
    return getattr(sys, 'frozen', False)


def _appdata_settings_dir():
    """`%APPDATA%\\GenshinRhythmBot` (Roaming). Falls back to
    `~/AppData/Roaming/GenshinRhythmBot` if APPDATA is unset."""
    base = os.environ.get('APPDATA')
    if base:
        return Path(base) / 'GenshinRhythmBot'
    return Path.home() / 'AppData' / 'Roaming' / 'GenshinRhythmBot'


def settings_path():
    """User-writable settings file. For the bundled .exe, live under
    `%APPDATA%\\GenshinRhythmBot` so the download folder stays clean and
    settings survive a re-extract / move of the .exe. For dev launches,
    sit next to the source script (gitignored)."""
    if is_frozen():
        path = _appdata_settings_dir() / 'ui_settings.json'
        _migrate_legacy_settings(path)
        return path
    return Path(__file__).parent / 'ui_settings.json'


def _migrate_legacy_settings(new_path):
    """One-shot move: pre-AppData builds wrote `ui_settings.json` next to
    the .exe. If that legacy file exists and the new location does not,
    move it so existing users keep their keybinds without re-binding."""
    try:
        legacy = Path(sys.executable).parent / 'ui_settings.json'
        if legacy.exists() and not new_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            legacy.replace(new_path)
            print(f"[ui] migrated settings: {legacy} -> {new_path}")
    except Exception as e:
        print(f"[ui] settings migration skipped: {e}")


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
ACTION_MACRO_RECORD = 'macro_record'
ACTION_MACRO_PLAY = 'macro_play'
ACTION_MACRO_STOP = 'macro_stop'
ACTION_MACRO_SAVE = 'macro_save'
ACTION_MACRO_LOAD = 'macro_load'
ACTION_LABELS = {
    ACTION_START_STOP:   'Start / Stop',
    ACTION_PAUSE:        'Pause / Resume',
    ACTION_DEBUG:        'Toggle Debug',
    ACTION_MACRO_RECORD: 'Record / Stop',
    ACTION_MACRO_PLAY:   'Play Macro',
    ACTION_MACRO_STOP:   'Stop Playback',
    ACTION_MACRO_SAVE:   'Save Slot (1-9)',
    ACTION_MACRO_LOAD:   'Load Slot (1-9)',
}
# Bot hotkeys — fire while Genshin OR the UI is focused.
BOT_ACTIONS = (ACTION_START_STOP, ACTION_PAUSE, ACTION_DEBUG)
# Macro hotkeys — fire only while Genshin is focused (per spec).
MACRO_ACTIONS = (ACTION_MACRO_RECORD, ACTION_MACRO_PLAY, ACTION_MACRO_STOP,
                 ACTION_MACRO_SAVE, ACTION_MACRO_LOAD)


def macros_dir():
    """Where macro slot files live. Mirror settings_path semantics:
    `%APPDATA%\\GenshinRhythmBot\\macros\\` for the bundled .exe (so a
    re-extract of the onedir doesn't wipe slots), `pc_client/macros/` in
    dev. Distinct from the standalone macro_tool.py's slot dir
    (`pc_client/macro_<n>.json` next to the script) so the two tools
    don't stomp each other's saves."""
    if is_frozen():
        return _appdata_settings_dir() / 'macros'
    return Path(__file__).parent / 'macros'


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


def is_allowed_game_only():
    """Stricter gate — only Genshin's window passes. Used for macro
    hotkeys + macro recording capture: the macro tool should not arm or
    record while the user is in another app."""
    fg = _foreground_hwnd()
    if not fg:
        return False
    title = _window_title(fg)
    if title in GAME_WINDOW_TITLE:
        return True
    proc = _foreground_process_name()
    return proc in _GAME_EXE_NAMES


# --- keybind manager --------------------------------------------------------

class KeybindManager:
    """Wrapper around `keyboard.add_hotkey` + `mouse.on_button` with
    foreground gating and rebind support. Each action has at most one
    active hook at a time. Bindings starting with 'mouse:<btn>' route to
    the mouse package; everything else routes to keyboard."""

    def __init__(self, ui_hwnd_provider):
        # ui_hwnd_provider() returns the current UI HWND. Called per-key
        # so it stays correct if the underlying window is recreated.
        self._ui_hwnd_provider = ui_hwnd_provider
        self._kb_hooks = {}      # action -> keyboard hotkey handle
        self._mouse_hooks = {}   # action -> mouse hook handle
        self._bindings = {}      # action -> hotkey string
        self._callbacks = {}     # action -> python callable
        self._scopes = {}        # action -> 'app' | 'game'

    @staticmethod
    def _is_mouse(binding):
        return isinstance(binding, str) and binding.lower().startswith('mouse:')

    @staticmethod
    def _mouse_button(binding):
        return binding.split(':', 1)[1].lower()

    def set(self, action, hotkey, callback, scope='app'):
        """Bind (or rebind) `action` to `hotkey`. Replaces any existing
        binding. `scope` controls the foreground gate: 'app' allows
        Genshin or the UI (default), 'game' is Genshin-only."""
        self.clear(action)
        if not hotkey:
            return
        wrapped = self._gate(callback, scope)
        try:
            if self._is_mouse(hotkey):
                btn = self._mouse_button(hotkey)
                # types=['down'] only — release is symmetric and would
                # double-fire toggles.
                handle = mouse.on_button(wrapped, buttons=[btn],
                                         types=['down'])
                self._mouse_hooks[action] = handle
            else:
                handle = keyboard.add_hotkey(hotkey, wrapped, suppress=False)
                self._kb_hooks[action] = handle
        except Exception as e:
            print(f"[ui] failed to bind {hotkey!r} for {action}: {e}")
            return
        self._bindings[action] = hotkey
        self._callbacks[action] = callback
        self._scopes[action] = scope

    def clear(self, action):
        h = self._kb_hooks.pop(action, None)
        if h is not None:
            try:
                keyboard.remove_hotkey(h)
            except KeyError:
                pass
        h = self._mouse_hooks.pop(action, None)
        if h is not None:
            try:
                mouse.unhook(h)
            except (KeyError, ValueError):
                pass
        self._bindings.pop(action, None)
        self._callbacks.pop(action, None)
        self._scopes.pop(action, None)

    def clear_all(self):
        for action in list(self._bindings):
            self.clear(action)

    def get_bindings(self):
        """Return {action: binding} snapshot — used by MacroController to
        build its excluded-keys set (so the record hotkey itself isn't
        captured into the macro)."""
        return dict(self._bindings)

    def _gate(self, callback, scope):
        def wrapper(*_args, **_kwargs):
            if scope == 'game':
                if not is_allowed_game_only():
                    return
            else:
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
    """Translate a JS keydown / mousedown payload to a hotkey string.
    Keyboard payload: {'key': str, 'code': str, 'ctrlKey': bool, ...} →
    'ctrl+f8' / 'alt+t' style. Mouse payload: {'mouseButton': str} →
    'mouse:<btn>' (btn ∈ left/right/middle/x/x2). Returns None on a
    pure-modifier press or unrecognized button."""
    if not isinstance(payload, dict):
        return None
    mb = payload.get('mouseButton')
    if mb:
        mb = str(mb).lower()
        if mb in ('left', 'right', 'middle', 'x', 'x2'):
            return f'mouse:{mb}'
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

    def __init__(self, on_status, controller_provider=None):
        self._on_status = on_status
        # Optional callable returning a long-lived ArduinoHIDController
        # shared with the macro tool. Without it (or if it returns None),
        # the worker creates a fresh controller per run — fine for CLI,
        # but in the UI the macro tool would race for the same COM port.
        self._controller_provider = controller_provider
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._debug_evt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        # Serializes restart_in_mode against itself so rapid mode-radio
        # toggles don't interleave a stop+start with another stop+start.
        self._restart_lock = threading.Lock()
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

    def restart_in_mode(self, mode, options):
        """Live mode switch. Stops the running worker (blocking up to 5s
        for clean exit), then starts a fresh one in `mode`. Returns True
        on successful start. Used by the UI's mode radio when toggled
        while the bot is running so the user doesn't have to manually
        Stop → flip → Start. Serialized via _restart_lock so rapid
        toggles don't race two stop+start sequences against each other."""
        with self._restart_lock:
            if self.is_running():
                self.stop()
                t = self._thread
                if t is not None:
                    t.join(timeout=5.0)
            return self.start(mode, options)

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
            ctrl = None
            if self._controller_provider is not None:
                try:
                    ctrl = self._controller_provider()
                except Exception as e:
                    print(f"[ui] controller_provider failed: {e}")
                    ctrl = None
            if self._mode == 'standalone':
                bot_main.run_standalone(self._stop_evt, self._debug_evt,
                                        status_cb=self._emit,
                                        pause_evt=self._pause_evt,
                                        controller=ctrl)
            elif self._mode == 'album':
                opts = self._mode_options
                runner = AlbumRunner(replay_canorus=opts.get('replay_canorus', False),
                                     difficulty=opts.get('difficulty', ALBUM_DIFFICULTY),
                                     mid_song_start=opts.get('mid_song_start', False),
                                     stop_evt=self._stop_evt,
                                     pause_evt=self._pause_evt,
                                     debug_evt=self._debug_evt,
                                     status_cb=self._emit,
                                     controller=ctrl)
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


# --- macro tool -------------------------------------------------------------

class MacroController:
    """Standalone-of-macro_tool.py adapted for the UI. Records keyboard +
    mouse events from inside Genshin (foreground-gated), replays them via
    a shared ArduinoHIDController, and persists up to 9 numbered slots
    under `macros_dir()`.

    Three states: idle / recording / playing. Mutually exclusive with the
    rhythm bot — the UI confirms with the user before stopping one to
    start the other (see BridgeApi)."""

    STATE_IDLE = 'idle'
    STATE_RECORDING = 'recording'
    STATE_PLAYING = 'playing'

    SLOT_TIMEOUT_S = 4.0

    def __init__(self, controller_provider, on_status):
        # Shared with BotController so we don't fight for the COM port.
        self._controller_provider = controller_provider
        self._on_status = on_status
        self._lock = threading.Lock()
        self._state = self.STATE_IDLE
        self._events = []
        self._start_ts = 0.0
        self._kb_hook = None
        self._mouse_hook = None
        self._play_thread = None
        self._stop_play_evt = threading.Event()
        # Bindings reserved as macro hotkeys — filtered out of capture so
        # the user pressing the record-stop key doesn't end up inside the
        # macro itself.
        self._excluded_kb = set()
        self._excluded_mouse = set()
        # Slot-pending state — armed for SLOT_TIMEOUT_S after a save/load
        # hotkey fires. Only one pending action at a time; re-arming
        # cancels the prior one.
        self._pending_action = None
        self._slot_hotkey_handles = []
        self._slot_timer = None
        self._pending_lock = threading.Lock()

    # ---- introspection ----

    def state(self):
        return self._state

    def is_busy(self):
        return self._state != self.STATE_IDLE

    def list_slots(self):
        d = macros_dir()
        if not d.exists():
            return []
        return [n for n in range(1, 10)
                if (d / f'macro_{n}.json').exists()]

    def snapshot(self):
        """Status fields the UI drain pushes to JS."""
        return {
            'macro_state': self._state,
            'macro_events': len(self._events),
            'macro_slots': self.list_slots(),
            'macro_pending': self._pending_action or '',
        }

    # ---- record ----

    def start_record(self, excluded_kb, excluded_mouse):
        with self._lock:
            if self._state != self.STATE_IDLE:
                print(f"[macro] cannot record — state is {self._state}")
                return False
            self._events = []
            self._start_ts = time.time()
            self._excluded_kb = {b.lower() for b in excluded_kb if b}
            self._excluded_mouse = {b.lower() for b in excluded_mouse if b}
            try:
                self._kb_hook = keyboard.hook(self._on_kb_event)
                self._mouse_hook = mouse.hook(self._on_mouse_event)
            except Exception as e:
                print(f"[macro] hook install failed: {e}")
                self._unhook()
                return False
            self._state = self.STATE_RECORDING
        print(f"[macro] recording started (capture only inside Genshin)")
        self._emit()
        return True

    def stop_record(self):
        with self._lock:
            if self._state != self.STATE_RECORDING:
                return False
            self._unhook()
            self._state = self.STATE_IDLE
        print(f"[macro] stopped recording — {len(self._events)} events")
        self._emit()
        return True

    def toggle_record(self, excluded_kb=(), excluded_mouse=()):
        if self._state == self.STATE_RECORDING:
            return self.stop_record()
        if self._state == self.STATE_IDLE:
            return self.start_record(excluded_kb, excluded_mouse)
        print(f"[macro] cannot toggle record — state is {self._state}")
        return False

    # ---- playback ----

    def play(self):
        with self._lock:
            if self._state != self.STATE_IDLE:
                print(f"[macro] cannot play — state is {self._state}")
                return False
            if not self._events:
                print("[macro] no macro recorded / loaded")
                return False
            controller = self._resolve_controller()
            if controller is None:
                print("[macro] play aborted — no Arduino controller")
                return False
            self._stop_play_evt.clear()
            self._state = self.STATE_PLAYING
            self._play_thread = threading.Thread(
                target=self._play_run, args=(controller,),
                daemon=True, name='macro-play')
            self._play_thread.start()
        self._emit()
        return True

    def stop_play(self):
        if self._state != self.STATE_PLAYING:
            return False
        self._stop_play_evt.set()
        return True

    def _play_run(self, controller):
        print(f"[macro] playing {len(self._events)} events")
        start = time.time()
        try:
            for ev in self._events:
                if self._stop_play_evt.is_set():
                    break
                target = start + ev.get('time', 0.0)
                wait = target - time.time()
                if wait > 0 and self._stop_play_evt.wait(wait):
                    break
                device = ev.get('device', 'keyboard')
                etype = ev.get('event_type')
                key = ev.get('key', '')
                if device == 'keyboard':
                    if etype == 'down':
                        controller.key_down(key)
                    elif etype == 'up':
                        controller.key_up(key)
                elif device == 'mouse':
                    if etype in ('down', 'double'):
                        controller.mouse_down(key)
                    elif etype == 'up':
                        controller.mouse_up(key)
            # Belt-and-suspenders: release every rhythm key + L/R mouse so
            # an interrupted macro doesn't leave a key stuck down in-game.
            for k in KEYS:
                controller.key_up(k)
            controller.mouse_up('left')
            controller.mouse_up('right')
        except Exception as e:
            print(f"[macro] playback error: {e}")
        finally:
            with self._lock:
                self._state = self.STATE_IDLE
            print("[macro] playback finished")
            self._emit()

    # ---- slots ----

    def save_slot(self, n):
        if not (1 <= int(n) <= 9):
            return False
        if self._state != self.STATE_IDLE:
            print(f"[macro] cannot save while {self._state}")
            return False
        if not self._events:
            print("[macro] nothing to save — record or load first")
            return False
        d = macros_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / f'macro_{int(n)}.json'
            with path.open('w', encoding='utf-8') as f:
                json.dump(self._events, f, indent=2)
            print(f"[macro] saved {len(self._events)} events to slot {n}")
        except Exception as e:
            print(f"[macro] save slot {n} failed: {e}")
            return False
        self._emit()
        return True

    def load_slot(self, n):
        if not (1 <= int(n) <= 9):
            return False
        if self._state != self.STATE_IDLE:
            print(f"[macro] cannot load while {self._state}")
            return False
        d = macros_dir()
        path = d / f'macro_{int(n)}.json'
        if not path.exists():
            print(f"[macro] slot {n} is empty")
            return False
        try:
            with path.open('r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                print(f"[macro] slot {n} malformed")
                return False
            self._events = data
            print(f"[macro] loaded slot {n} ({len(self._events)} events)")
        except Exception as e:
            print(f"[macro] load slot {n} failed: {e}")
            return False
        self._emit()
        return True

    def clear_slot(self, n):
        if not (1 <= int(n) <= 9):
            return False
        d = macros_dir()
        path = d / f'macro_{int(n)}.json'
        try:
            if path.exists():
                path.unlink()
                print(f"[macro] cleared slot {n}")
            else:
                print(f"[macro] slot {n} already empty")
        except Exception as e:
            print(f"[macro] clear slot {n} failed: {e}")
            return False
        self._emit()
        return True

    # ---- save/load slot picker (driven by hotkeys) ----

    def begin_save_pending(self):
        """Arm a 4-second slot picker — pressing 1-9 saves the current
        macro to that slot, ESC cancels. Called from the macro_save
        hotkey; UI buttons skip this and call save_slot directly."""
        if self._state != self.STATE_IDLE:
            print(f"[macro] cannot save while {self._state}")
            return False
        if not self._events:
            print("[macro] nothing to save")
            return False
        self._set_pending('save')
        return True

    def begin_load_pending(self):
        if self._state != self.STATE_IDLE:
            print(f"[macro] cannot load while {self._state}")
            return False
        self._set_pending('load')
        return True

    def _set_pending(self, action):
        with self._pending_lock:
            self._clear_pending_locked()
            self._pending_action = action
            for d in range(1, 10):
                try:
                    h = keyboard.add_hotkey(
                        str(d), self._slot_chosen_async, args=(d,),
                        suppress=True)
                    self._slot_hotkey_handles.append(h)
                except Exception as e:
                    print(f"[macro] register slot hotkey {d}: {e}")
            try:
                h = keyboard.add_hotkey('esc', self._slot_cancel_async,
                                        suppress=True)
                self._slot_hotkey_handles.append(h)
            except Exception:
                pass
            self._slot_timer = threading.Timer(self.SLOT_TIMEOUT_S,
                                               self._slot_timeout_async)
            self._slot_timer.daemon = True
            self._slot_timer.start()
        print(f"[macro] {action}: press 1-9 to choose slot, "
              f"ESC to cancel ({int(self.SLOT_TIMEOUT_S)}s)")
        self._emit()

    def _clear_pending_locked(self):
        for h in self._slot_hotkey_handles:
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._slot_hotkey_handles.clear()
        if self._slot_timer is not None:
            self._slot_timer.cancel()
            self._slot_timer = None
        self._pending_action = None

    def _slot_chosen_async(self, slot):
        # keyboard package fires this on its listener thread; offload so
        # we don't block the listener on file IO.
        threading.Thread(target=self._handle_slot_choice,
                         args=(slot,), daemon=True).start()

    def _handle_slot_choice(self, slot):
        with self._pending_lock:
            action = self._pending_action
            self._clear_pending_locked()
        if action == 'save':
            self.save_slot(slot)
        elif action == 'load':
            self.load_slot(slot)
        self._emit()

    def _slot_cancel_async(self):
        threading.Thread(target=self._handle_slot_cancel,
                         daemon=True).start()

    def _handle_slot_cancel(self):
        with self._pending_lock:
            action = self._pending_action
            self._clear_pending_locked()
        if action:
            print(f"[macro] {action} cancelled")
        self._emit()

    def _slot_timeout_async(self):
        with self._pending_lock:
            action = self._pending_action
            if action is None:
                return
            self._clear_pending_locked()
        print(f"[macro] {action} timed out")
        self._emit()

    # ---- shutdown ----

    def shutdown(self):
        self._stop_play_evt.set()
        with self._lock:
            self._unhook()
            self._state = self.STATE_IDLE
        with self._pending_lock:
            self._clear_pending_locked()
        if self._play_thread is not None:
            self._play_thread.join(timeout=2.0)

    # ---- internal ----

    def _resolve_controller(self):
        if self._controller_provider is None:
            return None
        try:
            return self._controller_provider()
        except Exception as e:
            print(f"[macro] controller provider failed: {e}")
            return None

    def _unhook(self):
        if self._kb_hook is not None:
            try:
                keyboard.unhook(self._kb_hook)
            except Exception:
                pass
            self._kb_hook = None
        if self._mouse_hook is not None:
            try:
                mouse.unhook(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None

    def _on_kb_event(self, event):
        if self._state != self.STATE_RECORDING:
            return
        # Capture only inside Genshin — anything the user types in
        # another app (browser, terminal, the UI) is dropped.
        if not is_allowed_game_only():
            return
        name = (event.name or '').lower() if event.name else ''
        if not name or name in self._excluded_kb:
            return
        # Suppress repeat 'down' events — keyboard sends one per OS auto-
        # repeat tick which would balloon the event list and replay as
        # rapid-fire taps.
        if event.event_type == 'down' and self._events:
            for prior in reversed(self._events):
                if prior['device'] != 'keyboard' or prior['key'] != name:
                    continue
                if prior['event_type'] == 'down':
                    return
                break
        self._events.append({
            'time': time.time() - self._start_ts,
            'device': 'keyboard',
            'key': name,
            'event_type': event.event_type,
        })

    def _on_mouse_event(self, event):
        if self._state != self.STATE_RECORDING:
            return
        if not isinstance(event, mouse.ButtonEvent):
            return
        if not is_allowed_game_only():
            return
        btn = (event.button or '').lower()
        if not btn or btn in self._excluded_mouse:
            return
        ev_type = 'down' if event.event_type == 'double' else event.event_type
        self._events.append({
            'time': time.time() - self._start_ts,
            'device': 'mouse',
            'key': btn,
            'event_type': ev_type,
        })

    def _emit(self):
        try:
            self._on_status(self.snapshot())
        except Exception:
            pass
