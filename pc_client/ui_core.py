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
import contextlib
import ctypes
import ctypes.wintypes
import inspect
import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import keyboard
import mouse

from album import AlbumRunner
from config import (ALBUM_DIFFICULTY, ALBUM_SONG_COUNT, GAME_WINDOW_TITLE,
                    INPUT_BACKEND_DEFAULT, KEYS, UI_KEYBINDS_DEFAULT)
from config import ALBUM_DIFFICULTY_COORDS
from macro_engine import MacroEngine
from standalone_runner import run_standalone
from system_setup import boost_timer, restore_timer, set_dpi_aware


APP_VERSION = '1.5.5'
# Bumped by hand on every build that goes to the user. Printed at startup
# and stamped into the window title so "which build is running" is never
# a guess — a whole debugging round was lost to that ambiguity.
UI_WINDOW_TITLE = 'Genshin Rhythm Bot'
MACRO_SLOT_COUNT = 18


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
        "The UI buttons still respond, but the game runs elevated, so "
        "Windows also blocks Software-backend keypresses and clicks "
        "from reaching it: the bot and macros will appear to run while "
        "the game ignores them. (The Arduino backend sends real USB "
        "input and is not affected by this — hotkeys still are.)",
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
    ACTION_MACRO_SAVE:   f'Save Slot (1-{MACRO_SLOT_COUNT})',
    ACTION_MACRO_LOAD:   f'Load Slot (1-{MACRO_SLOT_COUNT})',
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

_VALID_BACKENDS = ('arduino', 'software')
ACTIVE_MACRO_COUNT = 3
MACRO_CONFLICT_IGNORE = 'ignore'
MACRO_CONFLICT_INTERRUPT = 'interrupt'
_VALID_MACRO_CONFLICT_POLICIES = (
    MACRO_CONFLICT_IGNORE,
    MACRO_CONFLICT_INTERRUPT,
)


def _normalize_backend(name):
    name = str(name or '').strip().lower()
    return name if name in _VALID_BACKENDS else INPUT_BACKEND_DEFAULT


def _default_active_macros():
    return [{'slot': 0, 'hotkey': ''} for _ in range(ACTIVE_MACRO_COUNT)]


def _validate_saved_binding(action, raw):
    """validate_hotkey for a binding read from disk — never raises.
    Returns (canonical or None, reason or None)."""
    try:
        return validate_hotkey(action, raw)
    except Exception:
        return None, HOTKEY_REASON_UNSUPPORTED


def _canonical_or_raw(hotkey):
    try:
        return normalize_hotkey(hotkey)
    except Exception:
        return str(hotkey or '').strip().lower()


def _sanitize_keybinds(saved):
    """Validate persisted bot/macro keybinds against the current rules
    (validate_hotkey): a binding that is no longer usable — the reserved
    'mouse:left', an unparseable / unmappable key, a bare rhythm key on a
    bot action — falls back to that action's UI_KEYBINDS_DEFAULT value,
    with a log line. Duplicates are compared in canonical form (first
    action in default order keeps it). An explicitly empty binding stays
    unbound. Unknown action keys are carried through untouched. Never
    raises — a hand-edited settings file must not brick the launch."""
    kb = dict(UI_KEYBINDS_DEFAULT)
    if not isinstance(saved, dict):
        return kb
    used = set()
    for action, default in UI_KEYBINDS_DEFAULT.items():
        default = str(default or '')
        raw = saved.get(action, default)
        raw = raw.strip() if isinstance(raw, str) else default
        if not raw:
            kb[action] = ''
            continue
        canonical, reason = _validate_saved_binding(action, raw)
        if canonical is not None and canonical not in used:
            used.add(canonical)
            kb[action] = canonical
            continue
        why = (f'duplicates another binding ({canonical!r})'
               if canonical is not None else f'is not usable ({reason})')
        fallback, _ = (_validate_saved_binding(action, default)
                       if default else (None, None))
        if fallback is not None and fallback not in used:
            used.add(fallback)
            kb[action] = fallback
            print(f"[ui] settings: {action} binding {raw!r} {why} — "
                  f"reset to default {fallback!r}")
        else:
            kb[action] = ''
            print(f"[ui] settings: {action} binding {raw!r} {why} — "
                  "left unbound")
    for action, raw in saved.items():
        if action not in kb and isinstance(action, str):
            kb[action] = raw if isinstance(raw, str) else ''
    return kb


def _normalize_active_macros(value, reserved_hotkeys=()):
    out = _default_active_macros()
    if not isinstance(value, list):
        return out
    # Duplicates are compared in canonical form ('Control+A' == 'ctrl+a').
    used_hotkeys = {
        _canonical_or_raw(h) for h in reserved_hotkeys if h
    }
    for i, item in enumerate(value[:ACTIVE_MACRO_COUNT]):
        if not isinstance(item, dict):
            continue
        try:
            slot = int(item.get('slot') or 0)
        except (TypeError, ValueError):
            slot = 0
        if not (1 <= slot <= MACRO_SLOT_COUNT):
            slot = 0
        hotkey = str(item.get('hotkey') or '').strip().lower()
        if hotkey:
            canonical, reason = _validate_saved_binding(
                f'macro_active_{i + 1}', hotkey)
            if canonical is None:
                print(f"[ui] settings: active macro {i + 1} hotkey "
                      f"{hotkey!r} is not usable ({reason}) — cleared")
                hotkey = ''
            elif canonical in used_hotkeys:
                print(f"[ui] settings: active macro {i + 1} hotkey "
                      f"{hotkey!r} duplicates another binding — cleared")
                hotkey = ''
            else:
                hotkey = canonical
        if hotkey:
            used_hotkeys.add(hotkey)
        out[i] = {'slot': slot, 'hotkey': hotkey}
    return out


def _normalize_macro_conflict_policy(value):
    value = str(value or '').strip().lower()
    if value in _VALID_MACRO_CONFLICT_POLICIES:
        return value
    return MACRO_CONFLICT_IGNORE


# Main-tab run options persisted across launches (`bot_options` key in
# ui_settings.json). `debug` is deliberately NOT persisted — the viz
# window costs CPU and should only open when asked for in this session.
BOT_MODES = ('standalone', 'album')
_PERSISTED_BOT_OPTION_KEYS = ('mode', 'songs', 'difficulty', 'replay_canorus')


def normalize_difficulty(d):
    """Canonicalize a difficulty selection to a non-empty list in card
    order (normal → hard → pro → legendary). Accepts a list/tuple of
    names, the legacy 'all' shortcut, or a single name. Returns None when
    nothing valid is left so callers can reject instead of guessing."""
    order = list(ALBUM_DIFFICULTY_COORDS)
    if isinstance(d, (list, tuple)):
        seen = {x for x in d if isinstance(x, str) and x in order}
        if not seen:
            return None
        return [name for name in order if name in seen]
    if d == 'all':
        return list(order)
    if isinstance(d, str) and d in order:
        return [d]
    return None


def clamp_song_count(n):
    """int-ify + clamp to 1..ALBUM_SONG_COUNT. None on garbage (incl.
    bools, which int() would happily turn into 0/1)."""
    if isinstance(n, bool):
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return max(1, min(ALBUM_SONG_COUNT, n))


def default_bot_options():
    return {
        'mode': 'standalone',
        'songs': ALBUM_SONG_COUNT,
        'difficulty': normalize_difficulty(ALBUM_DIFFICULTY) or ['legendary'],
        'replay_canorus': False,
    }


def normalize_bot_options(value):
    """Validate a persisted `bot_options` dict field-by-field; anything
    missing or malformed falls back to its default so a hand-edited
    settings file can't brick the Main tab."""
    out = default_bot_options()
    if not isinstance(value, dict):
        return out
    mode = str(value.get('mode') or '').strip().lower()
    if mode in BOT_MODES:
        out['mode'] = mode
    songs = clamp_song_count(value.get('songs'))
    if songs is not None:
        out['songs'] = songs
    diff = normalize_difficulty(value.get('difficulty'))
    if diff:
        out['difficulty'] = diff
    if isinstance(value.get('replay_canorus'), bool):
        out['replay_canorus'] = value['replay_canorus']
    return out


def bot_options_for_settings(opts):
    """The persisted subset of the live Main-tab `_opts` dict."""
    return normalize_bot_options(
        {k: opts.get(k) for k in _PERSISTED_BOT_OPTION_KEYS})


def load_settings():
    """Load UI settings (keybinds + input_backend). Falls back to config
    defaults on missing file or parse errors."""
    p = settings_path()
    if not p.exists():
        return {
            'keybinds': dict(UI_KEYBINDS_DEFAULT),
            'input_backend': INPUT_BACKEND_DEFAULT,
            'macro_active_macros': _default_active_macros(),
            'macro_conflict_policy': MACRO_CONFLICT_IGNORE,
            'bot_options': default_bot_options(),
        }
    try:
        with p.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        kb = _sanitize_keybinds(data.get('keybinds'))
        backend = _normalize_backend(data.get('input_backend'))
        active = _normalize_active_macros(
            data.get('macro_active_macros'), reserved_hotkeys=kb.values())
        policy = _normalize_macro_conflict_policy(
            data.get('macro_conflict_policy'))
        return {
            'keybinds': kb,
            'input_backend': backend,
            'macro_active_macros': active,
            'macro_conflict_policy': policy,
            'bot_options': normalize_bot_options(data.get('bot_options')),
        }
    except Exception as e:
        print(f"[ui] failed to load settings: {e} — using defaults")
        return {
            'keybinds': dict(UI_KEYBINDS_DEFAULT),
            'input_backend': INPUT_BACKEND_DEFAULT,
            'macro_active_macros': _default_active_macros(),
            'macro_conflict_policy': MACRO_CONFLICT_IGNORE,
            'bot_options': default_bot_options(),
        }


def _accepts_kwarg(fn, name):
    """True if `fn` can be called with keyword `name` (explicitly or via
    **kwargs). Used to stay compatible with providers / constructors
    that predate an optional keyword."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in params.values())


def make_input_backend(name, cancel_evt=None):
    """Factory: build the chosen input backend. 'arduino' (default)
    uses real USB HID via the Leonardo; 'software' uses Win32
    SendInput / mouse_event — no hardware, but no combat anti-cheat
    bypass either (see `software_input.py` header). Imports are
    inside the function so a missing pyserial / missing keyboard /
    missing mouse package only breaks the backend that needs it,
    not the whole UI.

    `cancel_evt` is forwarded to the Arduino constructor so a Stop
    during its ~2 s post-open settle aborts the build (it raises
    `ArduinoConnectionError` then). Arduino failures raise
    `ArduinoConnectionError` (a RuntimeError) with a user-facing
    message — callers catch it."""
    backend = _normalize_backend(name)
    if backend == 'software':
        from software_input import SoftwareInputController
        return SoftwareInputController()
    from controller import ArduinoHIDController
    if _accepts_kwarg(ArduinoHIDController, 'cancel_evt'):
        return ArduinoHIDController(cancel_evt=cancel_evt)
    return ArduinoHIDController()


try:
    from macro_engine import atomic_write_json as _atomic_write_json
except ImportError:
    _atomic_write_json = None


def _atomic_write_json_fallback(path, obj, **json_kwargs):
    """Same temp-file + os.replace pattern as macro_engine's helper, for
    builds where it isn't available. A crash / power cut mid-write leaves
    either the old file or the new one — never a truncated JSON that
    would silently reset every keybind on next launch."""
    path = Path(path)
    tmp = path.with_name(
        f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as fh:
            json.dump(obj, fh, **json_kwargs)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def save_settings(settings):
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        writer = _atomic_write_json or _atomic_write_json_fallback
        writer(p, settings, indent=2)
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


# Genshin's HWND, refreshed off the hot path.
#
# WHY THIS CACHE EXISTS: is_allowed_game_only() runs on the WH_MOUSE_LL /
# WH_KEYBOARD_LL callback for every hotkey press. It used to call
# _window_title(fg), and GetWindowTextW against a window owned by ANOTHER
# process sends WM_GETTEXT and blocks until that process's message loop
# replies. Doing that inside a low-level hook means a stuttering game can
# overrun LowLevelHooksTimeout, at which point Windows silently and
# permanently unhooks us: every later press vanishes with no error. That
# was the "hotkey stops working until I wait a bit" bug.
#
# FindWindowW does not message the target, so it is safe to call from the
# refresher thread; the hook callback then only does GetForegroundWindow
# and an integer compare, neither of which can block.
_game_hwnd_lock = threading.Lock()
_game_hwnd = 0


def refresh_game_hwnd():
    """Re-resolve Genshin's HWND. Safe to call from a background thread;
    must never be called from a hook callback."""
    global _game_hwnd
    hwnd = 0
    try:
        user32 = ctypes.windll.user32
        titles = GAME_WINDOW_TITLE
        if isinstance(titles, str):
            titles = (titles,)
        for title in titles:
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
    except Exception:
        hwnd = 0
    with _game_hwnd_lock:
        _game_hwnd = hwnd or 0
    return _game_hwnd


_refresher_started = False


def _start_game_hwnd_refresher(period=1.0):
    global _refresher_started
    if _refresher_started:
        return
    _refresher_started = True

    def loop():
        while True:
            refresh_game_hwnd()
            time.sleep(period)
    threading.Thread(target=loop, daemon=True,
                     name='game-hwnd-refresh').start()


def is_allowed_game_only():
    """Stricter gate — only Genshin's window passes. Used for macro
    hotkeys + macro recording capture: the macro tool should not arm or
    record while the user is in another app.

    HOT PATH — runs inside a low-level hook callback. Only
    GetForegroundWindow plus an integer compare; nothing here may send a
    window message or open a process handle (see the note above)."""
    fg = _foreground_hwnd()
    if not fg:
        return False
    with _game_hwnd_lock:
        cached = _game_hwnd
    return bool(cached) and fg == cached


FOCUS_GAME_NOT_FOUND = 'game_not_found'
FOCUS_GAME_FAILED = 'focus_failed'
FOCUS_GAME_STOPPED = 'stopped'


def _same_hwnd(a, b):
    """HWND equality that survives ctypes restype differences: some call
    sites bind FindWindowW.restype = HWND (int / None), others use the
    default c_int (sign-truncated). HWNDs only carry 32 significant bits,
    so compare the low 32."""
    try:
        a = int(a or 0)
        b = int(b or 0)
    except (TypeError, ValueError):
        return False
    return bool(a) and bool(b) and (a & 0xFFFFFFFF) == (b & 0xFFFFFFFF)


def focus_game_window_ex(timeout=1.0, stop_evt=None):
    """Bring Genshin to the foreground and VERIFY it got there. Returns
    `(hwnd, reason)`: `(hwnd, '')` once GetForegroundWindow() is the game,
    else `(0, FOCUS_GAME_NOT_FOUND | FOCUS_GAME_FAILED | FOCUS_GAME_STOPPED)`.

    Used by UI buttons that fire input into the game (macro Play) so the
    user doesn't have to alt-tab between clicking and the keys arriving.
    The verification matters: when Genshin isn't running, or the shell
    refuses the focus change, playback used to start anyway and type the
    macro into whatever window was in front (usually this UI).

    Three-step dance because Win32 fights us on each step:
      1. ShowWindow(SW_RESTORE) — un-minimize.
      2. BringWindowToTop — raise Z-order (cosmetic without focus).
      3. SetForegroundWindow — actual focus. Windows blocks this from
         background processes; the UI is the foreground app at the
         moment the user clicks, so the call is allowed. If it still
         returns 0 (some shell states refuse), fall back to the
         undocumented SwitchToThisWindow which the alt-tab dialog uses
         and which has fewer restrictions.
    Then poll GetForegroundWindow for up to `timeout` seconds (the switch
    is asynchronous); `stop_evt` (e.g. app shutdown) aborts the wait."""
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    titles = GAME_WINDOW_TITLE
    if isinstance(titles, str):
        titles = (titles,)
    hwnd = 0
    try:
        for title in titles:
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
    except Exception as e:
        print(f"[ui] focus_game_window lookup error: {e}")
        hwnd = 0
    if not hwnd:
        return 0, FOCUS_GAME_NOT_FOUND
    if _same_hwnd(_foreground_hwnd(), hwnd):
        return hwnd, ''
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        ok = user32.SetForegroundWindow(hwnd)
        if not ok and hasattr(user32, 'SwitchToThisWindow'):
            user32.SwitchToThisWindow(hwnd, True)
    except Exception as e:
        print(f"[ui] focus_game_window error: {e}")
    deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
    while True:
        if _same_hwnd(_foreground_hwnd(), hwnd):
            return hwnd, ''
        if stop_evt is not None and stop_evt.is_set():
            return 0, FOCUS_GAME_STOPPED
        if time.monotonic() >= deadline:
            return 0, FOCUS_GAME_FAILED
        time.sleep(0.02)


def focus_game_window(timeout=1.0, stop_evt=None):
    """Back-compat wrapper: the game HWND once it is verifiably the
    foreground window, else 0 (not found / focus refused / stopped). Use
    `focus_game_window_ex` when the caller needs the reason."""
    return focus_game_window_ex(timeout=timeout, stop_evt=stop_evt)[0]


# --- keybind manager --------------------------------------------------------

# Modifier name (as the keyboard package spells it) -> Win32 virtual keys
# that GetAsyncKeyState reports for it. Left/right Ctrl and Alt share a
# scan code, so either side's VK counts. 'alt gr' is LCtrl+RAlt.
_MODIFIER_VKS_BY_NAME = (
    ('left shift', (0xA0,)), ('right shift', (0xA1,)),
    ('left ctrl', (0xA2, 0xA3)), ('right ctrl', (0xA2, 0xA3)),
    ('left alt', (0xA4, 0xA5)), ('right alt', (0xA4, 0xA5)),
    ('left windows', (0x5B,)), ('right windows', (0x5C,)),
    ('alt gr', (0xA5,)),
)
_get_async_key_state = None


def _modifier_vks_by_scan_code():
    """{scan code: VKs} for every modifier, resolved through the keyboard
    package's own name tables. Built off the hot path (bind time)."""
    out = {}
    for name, vks in _MODIFIER_VKS_BY_NAME:
        try:
            codes = keyboard.key_to_scan_codes(name)
        except Exception:
            continue
        for code in codes:
            out[code] = tuple(sorted(set(out.get(code, ())) | set(vks)))
    return out


def _async_key_down(vk):
    """True / False from GetAsyncKeyState's high bit, None if unknown.
    Safe on the hook thread: it reads the input state and sends no window
    message, so it cannot block on another process."""
    global _get_async_key_state
    try:
        fn = _get_async_key_state
        if fn is None:
            fn = ctypes.WINFUNCTYPE(ctypes.c_short, ctypes.c_int)(
                ('GetAsyncKeyState', ctypes.windll.user32))
            _get_async_key_state = fn
        return bool(fn(vk) & 0x8000)
    except Exception:
        return None


class KeybindManager:
    """Global hotkeys with foreground gating and rebind support. Each
    action has at most one binding. Bindings starting with 'mouse:<btn>'
    route to `mouse.on_button`; everything else is matched by this class
    on ONE non-suppressing `keyboard.hook` (see `_on_kb_event` for why
    `keyboard.add_hotkey` is no longer used)."""

    # A hotkey callback must NEVER run on the listener thread.
    # `mouse.on_button` and `keyboard` hooks invoke handlers inline on
    # the package's own listener thread, so any blocking work in a callback
    # stalls ALL input hook processing for its duration. `play_slot` can
    # join a winding-down worker for up to 2 s while holding its command
    # lock — which froze the hooks for 2 s, swallowed every press that
    # arrived meanwhile, and produced the reported "spamming the hotkey
    # does nothing, wait a moment and it works". Callbacks are handed to a
    # single dispatcher thread instead: the listener returns immediately
    # and ordering between triggers is preserved.
    _DISPATCH_MAX = 8

    # Capture prompts suspend every hotkey (capture_begin in ui.py). This
    # caps how long a suspension can outlive a page that never sends the
    # matching resume (lost bridge call, reload, crash) -- the JS prompt
    # gives up slightly earlier so the two never disagree.
    CAPTURE_SUSPEND_MAX_S = 15.0

    # A KEY_DOWN for a key that is already held is OS auto-repeat and must
    # not re-fire the binding. Exception: when that key's previous DOWN is
    # older than this, its KEY_UP was lost (secure desktop, hook installed
    # mid-press) and this is a real new press. Windows auto-repeat ticks
    # are <= ~1 s apart even at the slowest typematic settings; measured
    # hook delivery stalls reach ~2.6 s (see the note below).
    _KB_STALE_HOLD_S = 3.0

    # A modifier the event stream says is held but Windows says is up only
    # counts as a lost KEY_UP (UAC prompt / secure desktop swallowed it)
    # when its last KEY_DOWN is at least this much older than the press
    # being matched. Younger than that, the KEY_UP is most likely still
    # queued behind this press (delivery lag) and the event stream is the
    # truth. A modifier Windows reports DOWN is never dropped, however
    # long it has been held.
    _KB_MOD_RECHECK_AFTER_S = 1.0

    def __init__(self, ui_hwnd_provider):
        # ui_hwnd_provider() returns the current UI HWND. Called per-key
        # so it stays correct if the underlying window is recreated.
        self._ui_hwnd_provider = ui_hwnd_provider
        self._mouse_hooks = {}   # action -> mouse hook handle
        self._bindings = {}      # action -> hotkey string
        self._callbacks = {}     # action -> python callable
        self._scopes = {}        # action -> 'app' | 'game'
        # Keyboard matcher state. `_kb_entries` / `_kb_index` are replaced
        # wholesale under `_bind_lock` (never mutated in place) so the hook
        # thread reads them without taking a lock. `_kb_held` is only ever
        # touched by the hook thread.
        self._bind_lock = threading.Lock()
        self._kb_hook = None     # remover returned by keyboard.hook
        self._kb_entries = {}    # action -> (action, requirements, main codes, gate)
        self._kb_index = {}      # scan code -> tuple of entries
        self._kb_held = {}       # scan code -> time of its latest KEY_DOWN
        self._kb_mod_vks = {}    # modifier scan code -> VKs (see HK5 note)
        # Capture suspension (see suspend/resume). Tokens order the
        # begin/end bridge calls, which pywebview runs on separate threads;
        # `_capture_lock` makes each check-and-update atomic. The gate only
        # READS `_suspended_until` (one float), lock-free.
        self._capture_lock = threading.Lock()
        self._suspended_until = 0.0
        self._capture_token = 0
        self._capture_ended_token = 0
        self._dispatch_q = queue.Queue(maxsize=self._DISPATCH_MAX)
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name='hotkey-dispatch')
        self._dispatch_thread.start()
        self._last_hook_event = time.monotonic()
        refresh_game_hwnd()            # prime it before any press arrives
        _start_game_hwnd_refresher()

    # NO all-events mouse hook here, and no dead-hook watchdog.
    #
    # Both were built on a theory the harness disproved. tools/hook_diag.py,
    # 10 minutes, 207,613 events: the hook "died" 15 times and RECOVERED ON
    # ITS OWN every time, with a max callback of 1.4 ms. A hook Windows
    # unhooks for overrunning LowLevelHooksTimeout never comes back -- so
    # that was never the mechanism. What actually happens is transient
    # 1-2.6 s stalls in event DELIVERY: presses arrive late, not lost.
    #
    # Against that, both mitigations were net harmful:
    #   * the probe hooked EVERY mouse event -- ~345 Python callbacks per
    #     second on the listener thread, which is itself delivery load
    #   * the watchdog could not tell a stall from a dead hook, and each
    #     re-registration is a window where a real press IS lost
    #
    # Hotkeys stay on mouse.on_button / keyboard.add_hotkey, which fire only
    # on the bindings themselves and cost nothing between presses.
    #
    # Update: keyboard bindings moved from keyboard.add_hotkey to one
    # keyboard.hook + `_on_kb_event` (see there). That is a per-KEY-event
    # Python call, but the keyboard package already runs Python for every
    # key event (direct_callback + pre_process_event) whether or not we
    # hook, and key events are a few per second -- not the ~345/s of mouse
    # moves above. Mouse bindings still use mouse.on_button only.

    def _dispatch_loop(self):
        while True:
            action, callback, refused = self._dispatch_q.get()
            if callback is None:
                print(f"[hotkey] {action} ignored - {refused}")
                self._dispatch_q.task_done()
                continue
            print(f"[hotkey] {action} pressed")
            t0 = time.monotonic()
            try:
                callback()
            except Exception as e:
                print(f"[ui] hotkey callback error ({action}): {e}")
            finally:
                took = (time.monotonic() - t0) * 1000
                if took > 100:
                    # Worth seeing: this is time the OLD code spent
                    # blocking the input listener.
                    print(f"[ui] hotkey {action} handler took {took:.0f} ms")
                self._dispatch_q.task_done()

    @staticmethod
    def _is_mouse(binding):
        return isinstance(binding, str) and binding.lower().startswith('mouse:')

    @staticmethod
    def _mouse_button(binding):
        return binding.split(':', 1)[1].lower()

    def set(self, action, hotkey, callback, scope='app'):
        """Bind (or rebind) `action` to `hotkey`. Replaces any existing
        binding. `scope` controls the foreground gate: 'app' allows
        Genshin or the UI (default), 'game' is Genshin-only.

        Returns True when installed (or when `hotkey` is empty, which just
        unbinds). A binding `validate_hotkey` refuses -- left click, a key
        the `keyboard` package cannot map, a rhythm key on a bot action --
        returns False and leaves the action's CURRENT binding installed."""
        if not hotkey:
            self.clear(action)
            return True
        canonical, reason = validate_hotkey(action, hotkey)
        parsed = None
        if reason is None and not self._is_mouse(canonical):
            try:
                parsed = parse_keyboard_hotkey(canonical)
            except Exception:
                reason = HOTKEY_REASON_UNSUPPORTED
        if reason is not None:
            print(f"[ui] not binding {hotkey!r} to {action}: "
                  f"{HOTKEY_REASON_TEXT.get(reason, reason)}")
            return False
        wrapped = self._gate(callback, scope, action)
        # Install the new hook BEFORE dropping the old binding, so an
        # install failure (hook can't start) leaves the old one working.
        handle = None
        try:
            if parsed is None:
                btn = self._mouse_button(canonical)
                # 'down' AND 'double'. The mouse package rewrites a press
                # that lands within the double-click interval of the
                # previous button event -- ANY button, press or release --
                # into 'double' INSTEAD of 'down' (never both; see
                # mouse/_winmouse.py), so 'down' alone silently ignored
                # e.g. Stop on Mouse 5 right after an attack click.
                # Release stays excluded: it would double-fire toggles.
                handle = mouse.on_button(wrapped, buttons=[btn],
                                         types=['down', 'double'])
            else:
                with self._bind_lock:
                    self._ensure_kb_hook_locked()
        except Exception as e:
            print(f"[ui] failed to bind {hotkey!r} for {action}: {e}")
            return False
        self.clear(action)
        if parsed is None:
            self._mouse_hooks[action] = handle
        else:
            _canon, requirements, main_codes = parsed
            with self._bind_lock:
                entries = dict(self._kb_entries)
                entries[action] = (action, requirements, main_codes, wrapped)
                self._publish_kb_locked(entries)
        self._bindings[action] = canonical
        self._callbacks[action] = callback
        self._scopes[action] = scope
        return True

    def clear(self, action):
        if action in self._kb_entries:
            with self._bind_lock:
                entries = dict(self._kb_entries)
                entries.pop(action, None)
                self._publish_kb_locked(entries)
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

    # ---- capture suspension ----
    #
    # While the UI's capture prompt is open the user is PICKING a key, not
    # issuing a command: pressing F8 to try it as a binding used to stop
    # the bot, F10 toggled the debug window. suspend() makes the gate DROP
    # every press (not queue it -- a queued F8 would fire the moment the
    # prompt closed). It auto-expires after CAPTURE_SUSPEND_MAX_S.
    #
    # `token` orders the begin/end pair: pywebview runs each bridge call
    # on its own thread, so "cancel capture A, arm capture B" can arrive
    # as begin(B) then end(A). A resume only lifts the suspension it
    # belongs to (or an older one); a begin whose token was already ended
    # is ignored. Calls without a token act unconditionally.
    #
    # Each check-and-write runs under `_capture_lock`: unlocked, resume(A)
    # could pass its token check, suspend(B) install B's suspension, and
    # then A's write of 0.0 leave B's prompt open with hotkeys live. The
    # gate never takes this lock -- it only reads `_suspended_until`.

    def suspend(self, token=None, seconds=None):
        limit = self.CAPTURE_SUSPEND_MAX_S
        try:
            secs = limit if seconds is None else max(
                0.0, min(float(seconds), limit))
        except (TypeError, ValueError):
            secs = limit
        with self._capture_lock:
            if token is not None:
                if token <= self._capture_ended_token:
                    return False
                self._capture_token = max(self._capture_token, token)
            self._suspended_until = time.monotonic() + secs
        return True

    def resume(self, token=None):
        with self._capture_lock:
            if token is not None:
                self._capture_ended_token = max(
                    self._capture_ended_token, token)
                if token < self._capture_token:
                    return False
            self._suspended_until = 0.0
        return True

    def is_suspended(self):
        return time.monotonic() < self._suspended_until

    # ---- keyboard matcher ----

    def _ensure_kb_hook_locked(self):
        if self._kb_hook is None:
            # Non-suppressing: the key still reaches the game / every other
            # app. Never Win32 RegisterHotKey, which reserves the key
            # system-wide until the process exits.
            self._kb_hook = keyboard.hook(self._on_kb_event, suppress=False)

    def _publish_kb_locked(self, entries):
        if not self._kb_mod_vks and entries:
            # Bind time, not the hot path: resolve modifier VKs and create
            # the GetAsyncKeyState prototype once.
            self._kb_mod_vks = _modifier_vks_by_scan_code()
            _async_key_down(0x10)
        index = {}
        for entry in entries.values():
            for code in entry[2]:
                index.setdefault(code, []).append(entry)
        self._kb_entries = entries
        self._kb_index = {code: tuple(v) for code, v in index.items()}

    def _drop_lost_modifiers(self, held, entries, code, t):
        """Forget held modifiers whose KEY_UP never reached us.

        The event stream is the source of truth for `held`, but a release
        that happens on the secure desktop (UAC prompt, Ctrl+Alt+Del) is
        never delivered to a low-level hook, so Ctrl would stay "held"
        forever and plain F8 would keep resolving to a 'ctrl+f8' binding.
        Only on this candidate path -- a fresh press of a bound key whose
        decision depends on a modifier -- ask Windows (GetAsyncKeyState)
        about the modifiers involved. Never aged by time alone: a modifier
        Windows reports down stays held however long it has been held."""
        mod_vks = self._kb_mod_vks
        for entry in entries:
            for req in entry[1]:
                for sc in req:
                    if sc == code:
                        continue
                    since = held.get(sc)
                    if since is None:
                        continue
                    vks = mod_vks.get(sc)
                    if not vks:
                        continue         # a chord co-key, not a modifier
                    if t - since < self._KB_MOD_RECHECK_AFTER_S:
                        continue         # its KEY_UP may still be queued
                    states = [_async_key_down(vk) for vk in vks]
                    if None in states or any(states):
                        continue         # held, or Windows can't tell
                    held.pop(sc, None)

    def _on_kb_event(self, event):
        """Match one key event against the keyboard bindings.

        Why not keyboard.add_hotkey: it fires only when the set of ALL
        currently-pressed keys equals the hotkey exactly. While the bot (or
        a macro) holds a rhythm key / W / shift -- Arduino HID and software
        input both reach the hook -- F8 was seen as {a, f8} and never
        fired. And it fired on every OS auto-repeat KEY_DOWN, so holding Y
        restarted recording (wiping the buffer) and holding F9 flapped
        Pause/Resume.

        Rules here:
          * fire on the KEY_DOWN of a binding's main key, once per physical
            press -- auto-repeat DOWNs of a held key are ignored and the
            key re-arms on its KEY_UP (`_KB_STALE_HOLD_S` covers a lost UP)
          * every modifier the binding names must be held (left/right
            variants + aliases resolved the way the keyboard package does,
            via key_to_scan_codes); extra held keys -- rhythm keys, W,
            extra modifiers -- do not block it
          * if several bindings match, the most specific wins: with both
            'f8' and 'shift+f8' bound, shift+F8 fires only 'shift+f8'

        HOT PATH: runs on the keyboard package's event thread. Dict lookups
        only -- no locks, no print, no I/O. The one exception is
        `_drop_lost_modifiers`: a handful of GetAsyncKeyState calls (no
        window message, cannot block), and only on a fresh press of a bound
        key that has modifier-carrying candidates. Must return a falsy
        value: GenericListener.invoke_handlers stops at the first truthy
        handler, which would starve MacroController's recording hook."""
        try:
            code = event.scan_code
            held = self._kb_held
            if event.event_type == keyboard.KEY_UP:
                held.pop(code, None)
                return None
            if event.event_type != keyboard.KEY_DOWN:
                return None
            t = event.time or time.time()
            last = held.get(code)
            held[code] = t
            if last is not None and (t - last) < self._KB_STALE_HOLD_S:
                return None              # auto-repeat of a held key
            entries = self._kb_index.get(code)
            if not entries:
                return None
            for entry in entries:
                if entry[1]:
                    self._drop_lost_modifiers(held, entries, code, t)
                    break
            best = []
            best_n = -1
            for entry in entries:
                requirements = entry[1]
                n = len(requirements)
                if n < best_n:
                    continue
                for req in requirements:
                    if req.isdisjoint(held):
                        break
                else:
                    if n > best_n:
                        best_n = n
                        best = [entry]
                    else:
                        best.append(entry)
            for entry in best:
                entry[3]()
        except Exception:
            pass
        return None

    def _gate(self, callback, scope, action='?'):
        def wrapper(*_args, **_kwargs):
            # NOTHING slow may happen here. This runs on the WH_MOUSE_LL /
            # WH_KEYBOARD_LL callback, and Windows permanently unhooks a
            # low-level hook whose callback exceeds LowLevelHooksTimeout.
            # Once that happens every later press vanishes with no trace --
            # exactly the "spamming does nothing" failure. In particular NO
            # print() here: print goes through QueueWriter, which mirrors to
            # the original stdout, and in a --windowed build that is not a
            # real console.
            self._last_hook_event = time.monotonic()
            if self._last_hook_event < self._suspended_until:
                # Capture prompt open: dropped, not queued (see suspend).
                return
            if scope == 'game':
                ok = is_allowed_game_only()
                why = 'Genshin not foreground'
            else:
                try:
                    ui_hwnd = self._ui_hwnd_provider()
                except Exception:
                    ui_hwnd = 0
                ok = is_allowed_foreground(ui_hwnd)
                why = 'app not foreground'
            try:
                self._dispatch_q.put_nowait(
                    (action, callback if ok else None, None if ok else why))
            except queue.Full:
                pass
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
    # A literal '+' / ',' can't sit inside a 'shift+...' string (Shift+'='
    # used to save as the unbindable 'shift++'); these are the names the
    # keyboard package documents for them.
    '+': 'plus',
    ',': 'comma',
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


# --- hotkey validation + parsing --------------------------------------------

# Refusal reasons, returned to the UI as {'ok': False, 'reason': ...}.
HOTKEY_REASON_LEFT_CLICK = 'left_click_reserved'
HOTKEY_REASON_UNSUPPORTED = 'unsupported_key'
HOTKEY_REASON_RHYTHM_KEY = 'rhythm_key'
HOTKEY_REASON_TEXT = {
    HOTKEY_REASON_LEFT_CLICK:
        'left click is every attack in Genshin and every album click',
    HOTKEY_REASON_UNSUPPORTED:
        'the keyboard package cannot map that key on this layout',
    HOTKEY_REASON_RHYTHM_KEY:
        'the bot presses a s d j k l itself, so every note would trigger it',
}

# Left click is every attack in Genshin and every album-automation click:
# bound to Play it replayed the macro on each attack, bound to Start/Stop
# it toggled the bot on each click. It is also what people press
# "somewhere else" to abandon a capture prompt, which is how it got bound
# by accident. No action may use it.
_FORBIDDEN_BINDINGS = frozenset({'mouse:left'})
_MOUSE_BINDING_BUTTONS = ('left', 'right', 'middle', 'x', 'x2')

# Bot controls can't be a bare rhythm key: the bot presses those itself
# (Arduino HID and software input both reach the hook), so an A note would
# toggle Pause.
_RHYTHM_KEY_NAMES = frozenset(str(k).strip().lower() for k in KEYS)

_LITERAL_KEY_NAMES = {'+': 'plus', ',': 'comma'}

# Stable modifier order so 'shift+ctrl+f8' and 'ctrl+shift+f8' compare
# (and are stored) the same.
_MODIFIER_ORDER = {'ctrl': 0, 'alt': 1, 'alt gr': 1, 'shift': 2,
                   'windows': 3}

# AltGr reaches the hook as the keyboard package's 'alt gr' (scan code
# 541) while Chromium reports it as ctrlKey+altKey, so an AltGr capture is
# stored as 'ctrl+alt+<key>'. Let 541 satisfy plain 'ctrl' / 'alt'.
_ALTGR_SCAN_CODE = 541


def _modifier_kind(name):
    for prefix in ('left ', 'right '):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _split_hotkey(text):
    s = str(text or '').strip().lower()
    if not s:
        return []
    if s == '+':
        return ['+']
    if s.endswith('++'):                 # legacy 'shift++'
        head = s[:-2]
        return (head.split('+') if head else []) + ['+']
    return s.split('+')


def _canonical_key_name(token):
    # keyboard.normalize_name: 'control' -> 'ctrl', 'win' -> 'windows',
    # 'escape' -> 'esc', 'plus' -> '+' ... (raises on an empty token).
    name = keyboard.normalize_name(token.strip())
    return _LITERAL_KEY_NAMES.get(name, name)


def parse_keyboard_hotkey(hotkey):
    """Resolve a keyboard binding for `KeybindManager`'s matcher.

    Returns (canonical, requirements, main_codes):
      canonical    -- normalized string ('ctrl+shift+f8'), modifiers in a
                      fixed order
      requirements -- tuple of frozensets of scan codes, one per modifier;
                      each is satisfied by ANY of its codes being held
      main_codes   -- frozenset of scan codes of the last (main) key

    Names resolve through keyboard.key_to_scan_codes, so left/right
    variants, aliases and layout-specific punctuation behave exactly as
    they did under keyboard.add_hotkey. Raises ValueError for anything the
    package can't map on this layout (dead keys, media keys,
    'unidentified', 'process', ...)."""
    tokens = _split_hotkey(hotkey)
    if not tokens:
        raise ValueError('empty hotkey')
    names = [_canonical_key_name(t) for t in tokens]
    main = names[-1]
    mods = sorted(set(names[:-1]),
                  key=lambda n: (_MODIFIER_ORDER.get(_modifier_kind(n), 9), n))
    requirements = []
    for name in mods:
        codes = set(keyboard.key_to_scan_codes(name))
        if name in ('ctrl', 'alt'):
            codes.add(_ALTGR_SCAN_CODE)
        requirements.append(frozenset(codes))
    main_codes = frozenset(keyboard.key_to_scan_codes(main))
    if not main_codes:
        raise ValueError(f'{main!r} has no scan code')
    return '+'.join(mods + [main]), tuple(requirements), main_codes


def validate_hotkey(action, hotkey):
    """Check a binding before it is saved or installed.

    Returns (canonical_binding, None) when usable, (None, reason) when not;
    reason is one of the HOTKEY_REASON_* codes."""
    raw = str(hotkey or '').strip().lower()
    if not raw:
        return None, HOTKEY_REASON_UNSUPPORTED
    if raw.startswith('mouse:'):
        canonical = 'mouse:' + raw.split(':', 1)[1].strip()
        if canonical in _FORBIDDEN_BINDINGS:
            return None, HOTKEY_REASON_LEFT_CLICK
        if canonical.split(':', 1)[1] not in _MOUSE_BINDING_BUTTONS:
            return None, HOTKEY_REASON_UNSUPPORTED
        return canonical, None
    try:
        canonical, requirements, _main = parse_keyboard_hotkey(raw)
    except Exception:
        return None, HOTKEY_REASON_UNSUPPORTED
    if (action in BOT_ACTIONS and not requirements
            and canonical in _RHYTHM_KEY_NAMES):
        return None, HOTKEY_REASON_RHYTHM_KEY
    return canonical, None


def normalize_hotkey(hotkey):
    """Canonical form for equality checks (duplicate detection). Falls
    back to the stripped/lowercased text when it doesn't parse."""
    raw = str(hotkey or '').strip().lower()
    if not raw:
        return ''
    if raw.startswith('mouse:'):
        return 'mouse:' + raw.split(':', 1)[1].strip()
    try:
        return parse_keyboard_hotkey(raw)[0]
    except Exception:
        return raw


# --- bot orchestration ------------------------------------------------------

# AlbumRunner's "couldn't start" error (user-facing message). Imported
# defensively; an empty tuple in an `except` clause catches nothing.
try:
    from album import AlbumSetupError as _AlbumSetupError
    _ALBUM_SETUP_ERRORS = (_AlbumSetupError,)
except ImportError:
    _ALBUM_SETUP_ERRORS = ()


class BotController:
    """Owns the bot worker thread and the events it watches. Exposes
    start / pause / resume / stop / toggle_debug methods called from the
    UI thread (or from hotkey callbacks dispatched on the keyboard
    package's listener thread)."""

    STATE_IDLE = 'idle'
    STATE_RUNNING = 'running'
    STATE_PAUSED = 'paused'
    STATE_STOPPING = 'stopping'

    # Shown in the Status card when a run ends on an exception with no
    # more specific `last_error` reported by the runner itself.
    CRASH_MESSAGE = 'Bot stopped unexpectedly — see Logs'
    ALBUM_FAILED_MESSAGE = 'Album run failed — see Logs'
    BACKEND_UNAVAILABLE_PREFIX = 'Input backend unavailable — '

    def __init__(self, on_status, controller_provider=None):
        self._on_status = on_status
        # Optional callable returning the long-lived InputBackend shared
        # with the macro tool. If it accepts a `cancel_evt` keyword it
        # gets this controller's stop event, so a Stop during a slow
        # backend build (Arduino's ~2 s settle) is honored. When a
        # provider is configured, a None return / exception is a hard,
        # user-visible failure — the runners are never allowed to fall
        # back to building their own ArduinoHIDController (that path
        # sys.exit()s on a missing board and would race the macro tool
        # for the COM port). Without a provider (CLI-style use) the
        # runners still build their own.
        self._controller_provider = controller_provider
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._debug_evt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        # Serializes restart_in_mode against itself so rapid mode-radio
        # toggles don't interleave a stop+start with another stop+start.
        self._restart_lock = threading.Lock()
        # Command generation, bumped (under _lock) by every stop(). A
        # restart_in_mode that is blocked joining the old worker re-checks
        # it before starting the new mode, so a user Stop that lands
        # during that join wins instead of being silently undone.
        self._generation = 0
        # True once anything reported a non-empty `last_error` during the
        # current run — the crash fallback must not clobber a specific
        # message (e.g. "Not on album page") with the generic one.
        self._run_error_reported = False
        self._timer_boosted = False
        self.state = self.STATE_IDLE
        self._mode = ''
        self._mode_options = {}

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def is_stopping(self):
        """Worker still alive but a stop has been requested."""
        return self.is_running() and self._stop_evt.is_set()

    @property
    def generation(self):
        return self._generation

    @property
    def debug_enabled(self):
        return self._debug_evt.is_set()

    def start(self, mode, options):
        """Spawn the worker. mode: 'standalone' | 'album'.
        options for album: {'songs': int, 'difficulty': str,
        'replay_canorus': bool, 'debug': bool}."""
        with self._lock:
            return self._start_locked(mode, options)

    def _start_locked(self, mode, options):
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
        self._run_error_reported = False
        self.state = self.STATE_RUNNING
        # A new Start clears whatever the previous run reported.
        self._emit({'state': self.state, 'mode': mode,
                    'debug': self._debug_evt.is_set(),
                    'last_error': ''})

        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f'bot-{mode}')
        self._thread.start()
        return True

    def stop(self):
        """Request stop. Never blocks. Returns the new command generation
        (see restart_in_mode)."""
        with self._lock:
            self._generation += 1
            gen = self._generation
        if not self.is_running():
            self.state = self.STATE_IDLE
            self._emit({'state': self.state})
            return gen
        # Reflect "stopping" immediately so the UI updates on the next
        # status tick instead of waiting for the worker thread to actually
        # exit (which can take 100ms+ if it has to tear down a cv2 window
        # or finish the current detector poll).
        self._stop_evt.set()
        self._pause_evt.clear()  # unblock any pause-waiters
        self.state = self.STATE_STOPPING
        self._emit({'state': self.state})
        return gen

    def pause_toggle(self):
        # Ignore while stopping: the thread is still alive, but flipping
        # the pause event (and the status to "paused") would only confuse
        # the UI — the run is already on its way out.
        if not self.is_running() or self._stop_evt.is_set():
            return
        if self._pause_evt.is_set():
            self._pause_evt.clear()
            self.state = self.STATE_RUNNING
            self._emit({'state': self.state})
        else:
            self._pause_evt.set()
            self.state = self.STATE_PAUSED
            self._emit({'state': self.state})

    def set_debug(self, enabled):
        """Explicitly set (not toggle) the debug-viz flag. Safe while idle
        — the value is then just the preset the next run starts with
        (start() re-derives it from its options). Returns the new value."""
        enabled = bool(enabled)
        if enabled:
            self._debug_evt.set()
        else:
            self._debug_evt.clear()
        self._emit({'debug': enabled})
        return enabled

    def toggle_debug(self):
        """Flip the debug-viz flag. Returns the new value."""
        return self.set_debug(not self._debug_evt.is_set())

    def restart_in_mode(self, mode, options):
        """Live mode switch. Stops the running worker (blocking up to 5s
        for clean exit), then starts a fresh one in `mode`. Returns True
        on successful start. Used by the UI's mode radio when toggled
        while the bot is running so the user doesn't have to manually
        Stop → flip → Start. Serialized via _restart_lock so rapid
        toggles don't race two stop+start sequences against each other.

        Refuses (returns False) when a Stop is already in flight at entry,
        or when another stop() lands while we're joining the old worker —
        the user's Stop always wins over a pending mode switch."""
        with self._restart_lock:
            if self.is_running():
                if self._stop_evt.is_set():
                    return False
                gen = self.stop()
                t = self._thread
                if t is not None:
                    t.join(timeout=5.0)
            else:
                with self._lock:
                    gen = self._generation
            with self._lock:
                if self._generation != gen:
                    return False
                return self._start_locked(mode, options)

    def shutdown(self):
        """Final teardown — called on UI close. Stop the bot and wait."""
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._timer_boosted:
            restore_timer()
            self._timer_boosted = False

    def _emit(self, update):
        # Runners report failures through this same callback
        # ({'last_error': ...}); remember that one was reported so the
        # generic crash message below doesn't overwrite it. An empty
        # string is a runner CLEARING its earlier error (e.g. the
        # minimized-window wait ended) — re-arm the fallback then.
        if isinstance(update, dict) and 'last_error' in update:
            self._run_error_reported = bool(update.get('last_error'))
        try:
            self._on_status(update)
        except Exception:
            pass

    def _report_error(self, message):
        """Surface `message` as last_error unless this run already
        reported something more specific. Returns True if it was set."""
        if self._run_error_reported:
            return False
        self._emit({'last_error': message})
        return True

    def report_error(self, message):
        """Public hook for the UI layer (e.g. a backend that died mid-run):
        same first-error-wins rule as the runners' own reports."""
        return self._report_error(message)

    def _report_crash(self):
        # A stop-triggered abort (e.g. "album-init aborted by stop_evt")
        # is the user's own Stop, not a failure worth flagging.
        if self._stop_evt.is_set():
            return
        self._report_error(self.CRASH_MESSAGE)

    def _resolve_controller(self):
        """Call the provider → (controller or None, failure detail)."""
        provider = self._controller_provider
        try:
            if _accepts_kwarg(provider, 'cancel_evt'):
                ctrl = provider(cancel_evt=self._stop_evt)
            else:
                ctrl = provider()
        except SystemExit as e:
            print(f"[ui] input backend init exited ({e.code!r})")
            return None, ''
        except Exception as e:
            print(f"[ui] controller_provider failed: {e}")
            return None, str(e)
        return ctrl, ''

    def _ensure_system_setup(self):
        """One-time DPI + timer boost. Idempotent across runs because
        timeBeginPeriod/EndPeriod are reference-counted by the OS."""
        set_dpi_aware()
        if not self._timer_boosted:
            self._timer_boosted = boost_timer()

    def _run(self):
        try:
            self._ensure_system_setup()
            ctrl = None
            if self._controller_provider is not None:
                ctrl, detail = self._resolve_controller()
                if ctrl is None:
                    # Hard failure — never let the runners fall back to
                    # building their own backend. Silent if the user hit
                    # Stop while the backend was still being built.
                    if not self._stop_evt.is_set():
                        msg = (self.BACKEND_UNAVAILABLE_PREFIX
                               + (detail or 'see Logs'))
                        print(f"[ui] {msg}")
                        self._report_error(msg)
                    return
            if self._stop_evt.is_set():
                return
            if self._mode == 'standalone':
                run_standalone(self._stop_evt, self._debug_evt,
                               status_cb=self._emit,
                               pause_evt=self._pause_evt,
                               controller=ctrl)
                # run_standalone only returns without a stop request when
                # it couldn't find (or lost) the game window. The runner
                # normally says which via last_error; this is the floor.
                if not self._stop_evt.is_set():
                    self._report_error(self.CRASH_MESSAGE)
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
                    result = runner.run(
                        songs_count=opts.get('songs', ALBUM_SONG_COUNT))
                finally:
                    runner.close()
                # The runner reports its own specific reason on failure;
                # this is the floor if it somehow didn't.
                if result == 'failed' and not self._stop_evt.is_set():
                    self._report_error(self.ALBUM_FAILED_MESSAGE)
            else:
                print(f"[ui] unknown mode: {self._mode}")
                self._report_error(self.CRASH_MESSAGE)
        except _ALBUM_SETUP_ERRORS as e:
            # Expected setup failure (game window missing / closed, stop
            # during the window wait). Message is user-facing; the runner
            # usually reported it already — don't log it as a crash.
            print(f"[ui] album setup: {e}")
            if not self._stop_evt.is_set():
                self._report_error(str(e) or self.ALBUM_FAILED_MESSAGE)
        except SystemExit as e:
            # Legacy paths (pre-ArduinoConnectionError controller, the
            # album runner's window check) still sys.exit() deep inside
            # constructors. In a worker thread that silently ends the
            # thread and the UI just flips to idle; make it visible.
            # Deliberately SystemExit only, not BaseException:
            # KeyboardInterrupt is only ever raised in the main thread,
            # so there is nothing else here worth swallowing.
            print(f"[ui] worker exited via sys.exit({e.code!r})")
            self._report_crash()
        except Exception as e:
            if self._stop_evt.is_set():
                # Abort raised by the user's own Stop (e.g. "album-init
                # aborted by stop_evt") — not a crash, no traceback.
                print(f"[ui] worker stopped: {e}")
            else:
                print(f"[ui] worker crashed: {e}")
                try:
                    traceback.print_exc()
                except Exception:
                    pass
            self._report_crash()
        finally:
            self.state = self.STATE_IDLE
            # Re-emit debug too: after the run it is just the preset for
            # the next Start (the viz window itself is gone).
            self._emit({'state': self.state, 'fps': 0.0, 'song': '',
                        'debug': self._debug_evt.is_set()})


# --- macro tool -------------------------------------------------------------

class MacroResult(dict):
    """Outcome of a MacroController command: a plain dict for the JS
    bridge (`{'ok': bool, 'reason': str, 'message': str, ...}`) that is
    also truthy/falsy on `ok`, so callers and tests written against the
    old bool returns keep working unchanged."""

    def __bool__(self):
        return bool(self.get('ok'))


def _macro_ok(**extra):
    return MacroResult(ok=True, **extra)


def _macro_refused(reason, message='', **extra):
    return MacroResult(ok=False, reason=reason, message=message, **extra)


# Hotkey-string modifier names -> the names the `keyboard` package reports
# in hook events (left/right variants differ).
_CHORD_KEY_ALIASES = {
    'ctrl': {'ctrl', 'left ctrl', 'right ctrl', 'control'},
    'control': {'ctrl', 'left ctrl', 'right ctrl', 'control'},
    'shift': {'shift', 'left shift', 'right shift'},
    'alt': {'alt', 'left alt', 'right alt', 'alt gr'},
    'windows': {'windows', 'left windows', 'right windows'},
}


def _kb_name_variants(name):
    """{name, keyboard-canonical name}. Stored bindings use hotkey-string
    spellings ('plus', 'comma', 'escape') while hook events report the
    key's own name ('+', ',', 'esc'); comparing both forms keeps those
    keys matched."""
    name = (name or '').strip().lower()
    if not name:
        return set()
    out = {name}
    try:
        out.add(str(keyboard.normalize_name(name)).lower())
    except Exception:
        pass
    return out


def _hotkey_chord_parts(binding):
    """{part: event-name aliases} for a modifier chord like 'ctrl+y'.
    Empty for mouse bindings and single keys — those are filtered whole
    by the recorder's exclusion sets instead."""
    b = (binding or '').strip().lower()
    if not b or b.startswith('mouse:') or '+' not in b:
        return {}
    parts = {}
    for part in b.split('+'):
        part = part.strip()
        if part:
            names = set(_CHORD_KEY_ALIASES.get(part, {part}))
            names |= _kb_name_variants(part)
            parts[part] = names
    return parts


class MacroController:
    """Standalone-of-macro_tool.py adapted for the UI. Records keyboard
    keys + mouse BUTTONS (no cursor movement, no wheel) from inside
    Genshin (foreground-gated), replays them via the shared input
    backend, and persists numbered slots under `macros_dir()`.

    Three states: idle / recording / playing. Mutually exclusive with the
    rhythm bot — the UI confirms with the user before stopping one to
    start the other (see BridgeApi).

    Commands return `MacroResult` (truthy on success). User-actionable
    failures also land in `snapshot()['macro_error']` until the next
    successful command, so the Macros tab can show them even when the
    command came from an in-game hotkey."""

    STATE_IDLE = 'idle'
    STATE_RECORDING = 'recording'
    STATE_PLAYING = 'playing'

    SLOT_TIMEOUT_S = 4.0
    TRIGGER_DEBOUNCE_S = 0.25
    # Live event count pushed while recording (the full snapshot reads
    # every slot file, so the ticker sends only this one field).
    LIVE_COUNT_INTERVAL_S = 0.25
    # Slot picker polls the foreground at this cadence and cancels as
    # soon as the user leaves the game.
    SLOT_FOCUS_POLL_S = 0.1

    def __init__(self, controller_provider, on_status):
        # Shared with BotController so we don't fight for the COM port.
        self._controller_provider = controller_provider
        self._on_status = on_status
        self._lock = threading.Lock()
        # Serialize whole user commands (play active, interrupt, stop).
        # `_lock` only protects field snapshots and is released while an
        # interrupted worker joins. Without this outer lock, a stop hotkey
        # can target the old worker during that hand-off and miss the new
        # playback that starts immediately after.
        self._command_lock = threading.Lock()
        # Last trigger time per playback source. Repeated callbacks refresh
        # the timestamp so holding/spamming a macro hotkey cannot restart it
        # as soon as the current playback exits. Protected by _command_lock.
        self._last_play_trigger_ts = {}
        self._state = self.STATE_IDLE
        # Event buffer + auto-repeat suppression + playback timing live
        # in MacroEngine. We own the state machine, hooks, foreground
        # gating, slot-picker, and exclusion filter — see macro_engine.py
        # for the contract split.
        self._engine = MacroEngine()
        self._kb_hook = None
        self._mouse_hook = None
        self._play_thread = None
        self._stop_play_evt = threading.Event()
        self._playing_slot = None
        self._playing_name = ''
        self._playing_source = ''
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
        self._pending_slot_bank = 0
        self._pending_lock = threading.Lock()
        # Set to cancel the current picker's watcher thread (timeout +
        # leave-the-game). One Event per armed picker.
        self._pending_cancel_evt = None
        # Suppressing keyboard hook for the picker + scan codes whose
        # press it swallowed (their repeats / release are swallowed too).
        self._picker_hook = None
        self._picker_swallowed = set()
        # Buffer revision — bumped whenever the buffer's contents or the
        # slot it belongs to change (record start/stop, load, editor
        # apply, loaded slot cleared). The JS events editor pins the
        # revision it fetched and the bridge refuses a stale editor Save,
        # so edits made against slot A can never be written into slot B
        # after B was loaded (equal event counts used to hide that).
        self._buffer_rev = 0
        # Last user-actionable failure for the Macros status pill.
        # Cleared by the next successful command.
        self._error = ''
        # Inputs pressed during the current recording: key id -> recorded
        # name, and mouse buttons. A release is recorded only if its press
        # was (see _on_kb_event).
        self._rec_held_keys = {}
        self._rec_held_mouse = set()
        # Chord parts (record hotkey, e.g. ctrl+y) still physically held
        # when recording started — their presses/releases are dropped
        # until the key comes up once.
        self._rec_ignore_parts = {}
        self._rec_ticker_stop = None
        # Event count of the macro currently playing (an active slot's
        # count can differ from the working buffer's).
        self._playing_events = 0

    # ---- introspection ----

    def state(self):
        return self._state

    def is_busy(self):
        return self._state != self.STATE_IDLE

    def list_slots(self):
        d = macros_dir()
        if not d.exists():
            return []
        return [n for n in range(1, MACRO_SLOT_COUNT + 1)
                if (d / f'macro_{n}.json').exists()]

    def slot_names(self):
        """Return {slot_n: name} for occupied slots. Names live inside
        each slot file's `name` field; legacy bare-list files (from any
        v1 saves before names landed) report '' so the UI shows the
        generic 'saved' tag instead of a missing-name placeholder."""
        d = macros_dir()
        names = {}
        if not d.exists():
            return names
        for n in range(1, MACRO_SLOT_COUNT + 1):
            p = d / f'macro_{n}.json'
            if not p.exists():
                continue
            try:
                _events, name = MacroEngine.read(p)
                names[n] = name
            except Exception:
                names[n] = ''
        return names

    def snapshot(self):
        """Status fields the UI drain pushes to JS."""
        return {
            'macro_state': self._state,
            'macro_events': self._engine.event_count(),
            'macro_slots': self.list_slots(),
            'macro_slot_names': self.slot_names(),
            'macro_loaded': self._engine.loaded_slot() or 0,
            'macro_dirty': self._engine.is_dirty(),
            'macro_pending': self._pending_action or '',
            'macro_playing_slot': self._playing_slot or 0,
            'macro_playing_name': self._playing_name,
            'macro_playing_source': self._playing_source,
            # Events in the macro that is playing — an active slot's count
            # differs from the working buffer's. 0 when not playing.
            'macro_playing_events': self._playing_events,
            # Buffer revision for the events editor (see __init__).
            'macro_rev': self._buffer_rev,
            # Last user-actionable failure ('' = none).
            'macro_error': self._error,
        }

    def event_count(self):
        return self._engine.event_count()

    def error(self):
        return self._error

    def set_error(self, message):
        """Publish a user-actionable failure detected outside the
        controller (bridge level: game window missing / focus refused,
        bot hand-off timed out, ...) so every macro error surfaces in
        the same status field. '' clears it."""
        message = str(message or '').strip()
        changed = message != self._error
        self._error = message
        if message:
            print(f"[macro] {message}")
        if changed:
            self._emit()

    def clear_error(self):
        self.set_error('')

    def _fail(self, reason, message, **extra):
        """Refusal the user can act on: logged, stored for the status
        pill, returned to the bridge. Never emits — callers emit after
        releasing `_lock` (the snapshot reads every slot file)."""
        self._error = message
        print(f"[macro] {message}")
        return _macro_refused(reason, message, **extra)

    def _succeed(self, **extra):
        self._error = ''
        return _macro_ok(**extra)

    def _finish(self, result, err_before):
        """Emit after a command — outside every lock — when something the
        UI shows changed: success, or a new / cleared error. Plain
        refusals (debounce, already playing) stay silent so hotkey spam
        doesn't re-read every slot file."""
        if result or self._error != err_before:
            self._emit()
        return result

    def get_events(self):
        """Copy of the buffer for the JS event editor. Each event is
        shallow-copied so JS edits to one don't smear into our state."""
        return [dict(ev) for ev in self._engine.events]

    def get_events_snapshot(self):
        """Buffer copy plus the revision / loaded slot it belongs to,
        read together so the editor pins exactly what it is editing."""
        with self._lock:
            return {
                'events': [dict(ev) for ev in self._engine.events],
                'rev': self._buffer_rev,
                'loaded': self._engine.loaded_slot() or 0,
                'dirty': self._engine.is_dirty(),
                'state': self._state,
            }

    def set_events(self, events):
        """Replace the buffer with a JS-edited list (no revision check,
        no persistence). The editor's Save uses `apply_events`."""
        return self.apply_events(events)

    def apply_events(self, events, expected_rev=None, expected_slot=None,
                     persist=False):
        """Replace the buffer with a JS-edited list. Engine handles
        per-event validation + sort. Serialized with every other buffer /
        slot command, and refused ('stale', no error set — the editor
        asks the user) when the buffer moved on since the editor fetched:
          - `expected_rev` must equal the current buffer revision;
          - `expected_slot` (> 0) must still be the loaded slot, so edits
            made to slot A never land in slot B.
        With `persist`, the cleaned events are also written back to the
        loaded slot (keeping its name) inside the same command hold, so
        nothing can interleave between apply and save. An all-empty
        result is never persisted — the buffer is left untouched and
        the caller offers Clear slot instead ('empty').

        Result extras: rev, count, dropped, applied, saved_slot, loaded."""
        err_before = self._error
        result = self._apply_events(events, expected_rev, expected_slot,
                                    persist)
        return self._finish(result, err_before)

    def _apply_events(self, events, expected_rev, expected_slot, persist):
        with self._command('apply edited events',
                           timeout=self._SLOT_CMD_WAIT_S) as ok:
            if not ok:
                return self._fail('busy', 'Another macro command is still '
                                          'running - try again.')
            if self._state != self.STATE_IDLE:
                return self._fail(self._state, f"Can't change the macro "
                                               f"while {self._state}.")
            if not isinstance(events, list):
                return self._fail('bad_payload', 'Edited events were not a '
                                                 'list - nothing applied.')
            loaded = self._engine.loaded_slot() or 0
            stale = self._stale_check(expected_rev, expected_slot, loaded)
            if stale is not None:
                return stale
            # Validate on a scratch engine first: an empty result must not
            # replace the buffer when the caller wanted it persisted.
            cleaned = MacroEngine().replace_events(events) or []
            dropped = len(events) - len(cleaned)
            target = loaded if persist else 0
            if target and not cleaned:
                print(f"[macro] edit refused - every event was removed; "
                      f"slot {target} left unchanged")
                return _macro_refused(
                    'empty', f'Every event was removed - slot {target} '
                             'was not changed.',
                    rev=self._buffer_rev, loaded=loaded, dropped=dropped)
            with self._lock:
                self._engine.replace_events(cleaned)
                self._buffer_rev += 1
                rev = self._buffer_rev
            print(f"[macro] events replaced - {len(cleaned)} valid"
                  + (f", {dropped} dropped" if dropped else ''))
            extra = {'rev': rev, 'count': len(cleaned), 'dropped': dropped,
                     'applied': True}
            if target:
                written = self._write_slot_locked(target, None)
                if not written:
                    written.update(extra, saved_slot=0, loaded=loaded)
                    return written
            result = self._succeed(saved_slot=target, loaded=loaded, **extra)
            if dropped:
                self._error = (f"{dropped} invalid event"
                               f"{'s were' if dropped != 1 else ' was'} "
                               f"dropped - see the Logs tab.")
            return result

    def _stale_check(self, expected_rev, expected_slot, loaded):
        """None when the caller's view of the buffer is current, else a
        'stale' refusal carrying the current rev / loaded slot."""
        if expected_rev is not None:
            try:
                rev_ok = int(expected_rev) == self._buffer_rev
            except (TypeError, ValueError):
                rev_ok = False
            if not rev_ok:
                print(f"[macro] edit refused - the macro changed since the "
                      f"editor loaded it (rev {expected_rev} -> "
                      f"{self._buffer_rev})")
                return _macro_refused(
                    'stale', 'The macro changed since the editor loaded it.',
                    rev=self._buffer_rev, loaded=loaded)
        if expected_slot:
            try:
                slot_ok = int(expected_slot) == loaded
            except (TypeError, ValueError):
                slot_ok = False
            if not slot_ok:
                now = f'slot {loaded}' if loaded else 'no slot'
                print(f"[macro] edit refused - editor was showing slot "
                      f"{expected_slot}, {now} is loaded now")
                return _macro_refused(
                    'stale', f'The editor was showing slot {expected_slot}, '
                             f'but {now} is loaded now.',
                    rev=self._buffer_rev, loaded=loaded)
        return None

    # ---- record ----

    def start_record(self, excluded_kb, excluded_mouse, chord=None):
        """Install the capture hooks and start a fresh buffer. `chord` is
        the record hotkey when recording was started from it (e.g.
        'ctrl+y'): chord keys still held right now are ignored until
        they come up, so the hotkey itself never lands in the macro."""
        err_before = self._error
        return self._finish(
            self._start_record(excluded_kb, excluded_mouse, chord),
            err_before)

    def _start_record(self, excluded_kb, excluded_mouse, chord):
        with self._command('record start',
                           timeout=self._SLOT_CMD_WAIT_S) as ok:
            if not ok:
                return self._fail('busy', 'Another macro command is still '
                                          'running - try again.')
            hook_error = None
            with self._lock:
                if self._state != self.STATE_IDLE:
                    print(f"[macro] cannot record — state is {self._state}")
                    return _macro_refused(
                        self._state, f"Can't record while {self._state}.")
                discarded = (self._engine.event_count()
                             if self._engine.is_dirty() else 0)
                # Both spellings of each excluded key: bindings are stored
                # canonical ('plus'), hook events say '+'.
                self._excluded_kb = set()
                for b in excluded_kb:
                    self._excluded_kb |= _kb_name_variants(b)
                self._excluded_mouse = {b.lower() for b in excluded_mouse if b}
                self._rec_held_keys = {}
                self._rec_held_mouse = set()
                self._rec_ignore_parts = {}
                try:
                    self._kb_hook = keyboard.hook(self._on_kb_event)
                    self._mouse_hook = mouse.hook(self._on_mouse_event)
                except Exception as e:
                    hook_error = e
                    self._unhook()
                if hook_error is None:
                    # Computed after the hooks are in, so a chord key
                    # released in between is seen as up, not left pending.
                    self._rec_ignore_parts = self._held_chord_parts(chord)
                    # Engine clears the buffer, resets the start timestamp,
                    # and detaches loaded-slot — see MacroEngine.begin_record.
                    self._engine.begin_record()
                    self._buffer_rev += 1
                    self._state = self.STATE_RECORDING
                    self._start_record_ticker_locked()
            if hook_error is not None:
                return self._fail('hook_failed', f"Couldn't start recording "
                                                 f"(input hook failed: "
                                                 f"{hook_error}).")
        if discarded:
            # The UI confirms before this; an in-game hotkey can't.
            print(f"[macro] warning: recording replaced {discarded} "
                  f"unsaved event{'s' if discarded != 1 else ''}")
        print("[macro] recording started (capture only inside Genshin)")
        return self._succeed()

    def stop_record(self, chord=None):
        """Remove the hooks and finish the buffer. `chord` is the record
        hotkey when the stop came from it: its presses reach the recorder
        before the hotkey callback does, so the trailing run of chord-key
        presses is stripped — playback never re-presses the hotkey."""
        err_before = self._error
        return self._finish(self._stop_record(chord), err_before)

    def _stop_record(self, chord):
        with self._command('record stop',
                           timeout=self._SLOT_CMD_WAIT_S) as ok:
            if not ok:
                return self._fail('busy', 'Another macro command is still '
                                          'running - try again.')
            with self._lock:
                if self._state != self.STATE_RECORDING:
                    return _macro_refused('not_recording')
                self._stop_record_ticker_locked()
                self._unhook()
                stripped = self._strip_trailing_chord_locked(chord)
                # Engine marks the buffer dirty if anything was captured.
                self._engine.end_record()
                self._buffer_rev += 1
                self._state = self.STATE_IDLE
                count = self._engine.event_count()
        if stripped:
            print(f"[macro] dropped {stripped} trailing record-hotkey "
                  f"press{'es' if stripped != 1 else ''}")
        print(f"[macro] stopped recording — {count} events")
        return self._succeed(count=count)

    def toggle_record(self, excluded_kb=(), excluded_mouse=(), chord=None):
        if self._state == self.STATE_RECORDING:
            return self.stop_record(chord=chord)
        if self._state == self.STATE_IDLE:
            return self.start_record(excluded_kb, excluded_mouse, chord=chord)
        print(f"[macro] cannot toggle record — state is {self._state}")
        return _macro_refused(self._state, f"Can't record while {self._state}.")

    @staticmethod
    def _held_chord_parts(chord):
        """{part: aliases} for the parts of `chord` physically down now."""
        held = {}
        for part, names in _hotkey_chord_parts(chord).items():
            try:
                down = keyboard.is_pressed(part)
            except Exception:
                # Unknown to the keyboard package -> can't appear in hook
                # events either; never ignore real presses on a guess.
                down = False
            if down:
                held[part] = names
        return held

    def _strip_trailing_chord_locked(self, chord):
        parts = _hotkey_chord_parts(chord)
        if not parts:
            return 0
        names = set().union(*parts.values())
        events = self._engine.events
        n = 0
        for ev in reversed(events):
            if (ev.get('device') == 'keyboard'
                    and ev.get('event_type') == 'down'
                    and ev.get('key') in names):
                n += 1
                continue
            break
        if n:
            # In place on the engine's buffer reference (no engine API for
            # trimming yet). replace_events() would re-validate and could
            # drop recorded names its canonicalizer doesn't know.
            del events[-n:]
        return n

    def _start_record_ticker_locked(self):
        stop = threading.Event()
        self._rec_ticker_stop = stop
        threading.Thread(target=self._record_ticker, args=(stop,),
                         daemon=True, name='macro-rec-count').start()

    def _stop_record_ticker_locked(self):
        if self._rec_ticker_stop is not None:
            self._rec_ticker_stop.set()
            self._rec_ticker_stop = None

    def _record_ticker(self, stop):
        """Live event count while recording. Pushes only `macro_events` —
        the full snapshot re-reads every slot file. Holds `_lock` while
        pushing so a tick can never land after stop_record's final
        status (which may have trimmed the count)."""
        last = -1
        while not stop.wait(self.LIVE_COUNT_INTERVAL_S):
            with self._lock:
                if stop.is_set():
                    return
                n = self._engine.event_count()
                if n == last:
                    continue
                last = n
                try:
                    self._on_status({'macro_events': n})
                except Exception:
                    pass

    # ---- playback ----

    def play(self):
        err_before = self._error
        return self._finish(self._play_buffer(), err_before)

    def _play_buffer(self):
        with self._command('play (buffer)') as ok:
            if not ok:
                return _macro_refused('busy')
            if self._is_repeated_play_trigger_locked(('buffer', None)):
                return _macro_refused('debounce')
            # Wait out a pending stop so the buffer Play button behaves
            # like the slot hotkeys — see the docstring above.
            with self._lock:
                winding_down = (self._state == self.STATE_PLAYING
                                and self._stop_play_evt.is_set())
                old_thread = self._play_thread if winding_down else None
            if old_thread is not None:
                old_thread.join(timeout=2.0)
            with self._lock:
                if self._state == self.STATE_PLAYING:
                    return _macro_refused('playing')
                if self._state != self.STATE_IDLE:
                    print(f"[macro] cannot play - state is {self._state}")
                    return _macro_refused(self._state)
                if self._engine.event_count() == 0:
                    return self._fail('empty_buffer', 'Nothing to play - '
                                      'record or load a macro first.')
                controller = self._resolve_controller()
                if controller is None:
                    return self._fail('no_backend', self._NO_BACKEND_MSG)
                events = [dict(ev) for ev in self._engine.events]
                self._start_playback_locked(
                    controller, events, slot=None, name='', source='buffer')
        return self._succeed()

    def play_slot(self, n, conflict_policy=MACRO_CONFLICT_IGNORE):
        err_before = self._error
        return self._finish(self._play_slot(n, conflict_policy), err_before)

    def _play_slot(self, n, conflict_policy):
        with self._command(f'play slot {n}') as ok:
            if not ok:
                return _macro_refused('busy')
            n_int = self._slot_number(n)
            if n_int is None:
                return _macro_refused('bad_slot')
            if self._is_repeated_play_trigger_locked(('slot', n_int)):
                return _macro_refused('debounce')
            path = macros_dir() / f'macro_{n_int}.json'
            if not path.exists():
                return self._fail('empty_slot', f'Active macro slot {n_int} '
                                                'is empty.')
            try:
                events, name = MacroEngine.read(path)
            except ValueError as e:
                return self._fail('malformed', f'Slot {n_int} is malformed: '
                                               f'{e}')
            except Exception as e:
                return self._fail('read_failed', f"Couldn't read slot "
                                                 f"{n_int}: {e}")
            if not events:
                return self._fail('empty_slot', f'Slot {n_int} has no '
                                                'events.')
            controller = self._resolve_controller()
            if controller is None:
                return self._fail('no_backend', self._NO_BACKEND_MSG)

            conflict_policy = _normalize_macro_conflict_policy(conflict_policy)
            while True:
                old_thread = None
                with self._lock:
                    if self._state == self.STATE_RECORDING:
                        print("[macro] cannot play active slot while recording")
                        return _macro_refused('recording')
                    if self._state == self.STATE_PLAYING:
                        if self._stop_play_evt.is_set():
                            # A stop is already in flight — the worker is
                            # winding down (safety sweep). Wait for it
                            # instead of refusing, otherwise
                            # stop-then-restart is a race the user has to
                            # time by hand. Deliberately does NOT stamp
                            # the debounce: this press is not spam.
                            old_thread = self._play_thread
                        elif self._playing_slot == n_int:
                            print(f"[macro] slot {n_int} ignored - it is "
                                  "already the macro that is playing")
                            return _macro_refused('already_playing')
                        elif conflict_policy != MACRO_CONFLICT_INTERRUPT:
                            print(f"[macro] slot {n_int} ignored - another macro "
                                  "is already playing")
                            return _macro_refused('playing')
                        else:
                            old_thread = self._play_thread
                            print(f"[macro] interrupting slot "
                                  f"{self._playing_slot} for slot {n_int}")
                            self._stop_play_evt.set()
                    else:
                        self._start_playback_locked(
                            controller, [dict(ev) for ev in events],
                            slot=n_int, name=name, source='active')
                        break
                if old_thread is None:
                    continue
                joined_at = time.monotonic()
                old_thread.join(timeout=1.0)
                if old_thread.is_alive():
                    return self._fail(
                        'interrupt_timeout', 'The previous macro did not stop '
                        f'within 1 s - slot {n_int} was not started.')
                print(f"[macro] previous playback ended in "
                      f"{(time.monotonic() - joined_at) * 1000:.0f} ms")
        return self._succeed()

    def stop_play(self):
        with self._command_lock:
            with self._lock:
                if self._state != self.STATE_PLAYING:
                    print(f"[macro] stop ignored - nothing is playing "
                          f"(state {self._state})")
                    return _macro_refused('not_playing')
                stop_evt = self._stop_play_evt
                # An explicit stop is a deliberate "I want to re-trigger"
                # signal, so clear the debounce rather than making the
                # user wait it out before starting again.
                self._last_play_trigger_ts.clear()
            stop_evt.set()
        return _macro_ok()

    def _start_playback_locked(self, controller, events, slot, name, source):
        self._stop_play_evt = threading.Event()
        self._state = self.STATE_PLAYING
        self._playing_slot = slot
        self._playing_name = name or ''
        self._playing_source = source
        self._playing_events = len(events)
        self._play_thread = threading.Thread(
            target=self._play_run,
            args=(controller, events, self._stop_play_evt, slot, name, source),
            daemon=True, name='macro-play')
        self._play_thread.start()

    def _play_run(self, controller, events, stop_evt, slot, name, source):
        label = f"slot {slot}"
        if name:
            label += f" ({name!r})"
        if source == 'buffer':
            label = 'current buffer'
        print(f"[macro] playing {len(events)} events from {label}")
        try:
            # Engine iterates events, sleeps cancellably, dispatches to
            # the controller, and releases held macro inputs on exit.
            MacroEngine.play_events(events, controller, stop_evt=stop_evt,
                                    rhythm_keys=KEYS)
        except Exception as e:
            print(f"[macro] playback error: {e}")
        finally:
            with self._lock:
                if self._stop_play_evt is stop_evt:
                    self._state = self.STATE_IDLE
                    self._playing_slot = None
                    self._playing_name = ''
                    self._playing_source = ''
                    self._playing_events = 0
            print("[macro] playback finished")
            self._emit()

    # ---- slots ----

    # Every buffer- or slot-changing command below runs under
    # `_command_lock` (the same hold play / interrupt / stop use), so a
    # save can't interleave with an editor apply, a rename can't overwrite
    # a concurrent save of the same slot, and a load can't land mid-save.

    def save_slot(self, n, name=None, expected_rev=None):
        """Persist the current buffer to slot n. If `name` is None, the
        existing slot's name is preserved (so a quick re-save doesn't
        wipe a previously-set name); pass an explicit '' to clear the
        name. `expected_rev` (optional) refuses with 'stale' if the
        buffer changed since the caller looked at it."""
        err_before = self._error
        return self._finish(self._save_slot(n, name, expected_rev),
                            err_before)

    def _save_slot(self, n, name, expected_rev):
        n_int = self._slot_number(n)
        if n_int is None:
            return self._fail('bad_slot', f'Invalid slot {n!r}.')
        with self._command(f'save slot {n_int}',
                           timeout=self._SLOT_CMD_WAIT_S) as ok:
            if not ok:
                return self._fail('busy', 'Another macro command is still '
                                          'running - try again.')
            if self._state != self.STATE_IDLE:
                return self._fail(self._state, f"Can't save while "
                                               f"{self._state} - stop first.")
            if self._engine.event_count() == 0:
                return self._fail('empty_buffer', 'Nothing to save - record '
                                                  'or load a macro first.')
            stale = self._stale_check(expected_rev, None,
                                      self._engine.loaded_slot() or 0)
            if stale is not None:
                return stale
            return self._write_slot_locked(n_int, name)

    def _write_slot_locked(self, n_int, name):
        """Write the buffer to slot n_int. Caller holds `_command_lock`.
        Marks the buffer clean only if the revision it wrote is still the
        current one. The engine's write is atomic (temp + replace), so a
        failure leaves the previous slot file intact."""
        path = macros_dir() / f'macro_{n_int}.json'
        if name is None:
            final_name = self._read_slot_name(n_int)
        else:
            final_name = self._normalize_name(name)
        with self._lock:
            rev = self._buffer_rev
            count = self._engine.event_count()
        try:
            # Contract (macro_engine): returns None, raises on failure.
            self._engine.save(path, name=final_name)
        except Exception as e:
            return self._fail('save_failed', f"Couldn't save slot {n_int}: "
                                             f"{e}")
        with self._lock:
            if rev == self._buffer_rev:
                self._engine.mark_saved(n_int)
        label = f' as {final_name!r}' if final_name else ''
        print(f"[macro] saved {count} events to slot {n_int}{label}")
        return self._succeed(slot=n_int, name=final_name, count=count,
                             rev=self._buffer_rev)

    def load_slot(self, n):
        err_before = self._error
        return self._finish(self._load_slot(n), err_before)

    def _load_slot(self, n):
        n_int = self._slot_number(n)
        if n_int is None:
            return self._fail('bad_slot', f'Invalid slot {n!r}.')
        with self._command(f'load slot {n_int}',
                           timeout=self._SLOT_CMD_WAIT_S) as ok:
            if not ok:
                return self._fail('busy', 'Another macro command is still '
                                          'running - try again.')
            if self._state != self.STATE_IDLE:
                return self._fail(self._state, f"Can't load while "
                                               f"{self._state} - stop first.")
            path = macros_dir() / f'macro_{n_int}.json'
            if not path.exists():
                return self._fail('empty_slot', f'Slot {n_int} is empty.')
            with self._lock:
                discarded = (self._engine.event_count()
                             if self._engine.is_dirty() else 0)
                prev = self._engine.loaded_slot() or 0
                try:
                    name = self._engine.load(path, slot=n_int)
                except ValueError as e:
                    load_error = ('malformed', f'Slot {n_int} is malformed: '
                                               f'{e}')
                except Exception as e:
                    load_error = ('load_failed', f"Couldn't load slot "
                                                 f"{n_int}: {e}")
                else:
                    load_error = None
                    self._buffer_rev += 1
                    count = self._engine.event_count()
            if load_error is not None:
                return self._fail(*load_error)
        if discarded:
            # The UI confirms first; an in-game hotkey can't, so say it.
            src = f' (slot {prev}*)' if prev else ''
            print(f"[macro] warning: discarded {discarded} unsaved "
                  f"event{'s' if discarded != 1 else ''}{src} by loading "
                  f"slot {n_int}")
        label = f' ({name!r})' if name else ''
        print(f"[macro] loaded slot {n_int}{label} — {count} events")
        return self._succeed(slot=n_int, name=name, count=count,
                             rev=self._buffer_rev)

    def clear_slot(self, n):
        err_before = self._error
        return self._finish(self._clear_slot(n), err_before)

    def _clear_slot(self, n):
        n_int = self._slot_number(n)
        if n_int is None:
            return self._fail('bad_slot', f'Invalid slot {n!r}.')
        with self._command(f'clear slot {n_int}',
                           timeout=self._SLOT_CMD_WAIT_S) as ok:
            if not ok:
                return self._fail('busy', 'Another macro command is still '
                                          'running - try again.')
            path = macros_dir() / f'macro_{n_int}.json'
            try:
                if path.exists():
                    path.unlink()
                    print(f"[macro] cleared slot {n_int}")
                else:
                    print(f"[macro] slot {n_int} already empty")
            except Exception as e:
                return self._fail('clear_failed', f"Couldn't clear slot "
                                                  f"{n_int}: {e}")
            detached = False
            with self._lock:
                # If the cleared slot was the one in memory, drop the
                # loaded tag — the buffer's events still exist but no
                # longer correspond to any saved slot, so they are now
                # unsaved work (and the editor must re-pin).
                if (self._engine.loaded_slot() or 0) == n_int:
                    self._engine.detach_slot(n_int)
                    if self._engine.event_count():
                        self._engine.mark_dirty()
                    self._buffer_rev += 1
                    detached = True
        return self._succeed(slot=n_int, detached=detached)

    def rename_slot(self, n, name):
        """Update slot n's name in place. Does not touch events. Refuses
        on empty slot (nothing to rename)."""
        err_before = self._error
        return self._finish(self._rename_slot(n, name), err_before)

    def _rename_slot(self, n, name):
        n_int = self._slot_number(n)
        if n_int is None:
            return self._fail('bad_slot', f'Invalid slot {n!r}.')
        with self._command(f'rename slot {n_int}',
                           timeout=self._SLOT_CMD_WAIT_S) as ok:
            if not ok:
                return self._fail('busy', 'Another macro command is still '
                                          'running - try again.')
            path = macros_dir() / f'macro_{n_int}.json'
            if not path.exists():
                return self._fail('empty_slot', f'Slot {n_int} is empty - '
                                                'nothing to rename.')
            new_name = self._normalize_name(name)
            try:
                # Atomic rewrite keeping the events (macro_engine); raises
                # on a missing / malformed file or a failed write, leaving
                # the old file intact.
                MacroEngine.rename_slot(path, new_name)
            except Exception as e:
                return self._fail('rename_failed', f"Couldn't rename slot "
                                                   f"{n_int}: {e}")
            print(f"[macro] renamed slot {n_int} -> {new_name!r}"
                  if new_name else f"[macro] cleared name on slot {n_int}")
        return self._succeed(slot=n_int, name=new_name)

    @staticmethod
    def _slot_number(n):
        """int slot in 1..MACRO_SLOT_COUNT, else None."""
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            return None
        return n_int if 1 <= n_int <= MACRO_SLOT_COUNT else None

    @staticmethod
    def _read_slot_name(n_int):
        """Name stored in slot n_int ('' if empty / legacy / unreadable).
        One file, not the whole grid (slot_names reads all 18)."""
        path = macros_dir() / f'macro_{n_int}.json'
        if not path.exists():
            return ''
        try:
            return MacroEngine.read(path)[1]
        except Exception:
            return ''

    @staticmethod
    def _normalize_name(name):
        """Strip whitespace + cap length so a runaway paste can't bloat
        a slot file. 80 chars is enough for human-readable labels and
        keeps the slot button from blowing out in the UI."""
        if name is None:
            return ''
        return str(name).strip()[:80]

    # ---- save/load slot picker (driven by hotkeys) ----

    def begin_save_pending(self):
        """Arm a 4-second slot picker — 1-9 select the first bank and
        0 then 1-9 select slots 10-18. ESC cancels. Called from the
        macro_save hotkey; UI buttons call save_slot directly."""
        if self._state != self.STATE_IDLE:
            print(f"[macro] cannot save while {self._state}")
            return _macro_refused(self._state)
        if self._engine.event_count() == 0:
            err_before = self._error
            return self._finish(
                self._fail('empty_buffer', 'Nothing to save - record or '
                                           'load a macro first.'),
                err_before)
        self._set_pending('save')
        return _macro_ok()

    def begin_load_pending(self):
        if self._state != self.STATE_IDLE:
            print(f"[macro] cannot load while {self._state}")
            return _macro_refused(self._state)
        self._set_pending('load')
        return _macro_ok()

    # The picker listens with ONE suppressing `keyboard.hook` instead of
    # per-digit `keyboard.add_hotkey`s. add_hotkey matches the full set of
    # held keys, so a digit pressed while walking (W held) never matched
    # and leaked into the game; it also re-fired on auto-repeat. The hook
    # looks only at the event's own key, KEY_DOWN only, and ignores
    # repeats.
    #
    # A suppressing hook runs INSIDE the WH_KEYBOARD_LL callback and its
    # return value is the verdict (truthy = let the key through, falsy =
    # swallow), so `_picker_key_hook` must stay hook-safe —
    # is_allowed_game_only is just GetForegroundWindow + an int compare —
    # and offload everything else to threads.
    #
    # Keys only count while Genshin is the foreground window: a digit
    # typed after alt-tabbing into chat used to be swallowed AND
    # overwrite the slot. Outside the game the key passes through
    # untouched and the picker cancels; the watcher thread also cancels
    # it within SLOT_FOCUS_POLL_S of the user leaving the game. In game
    # the chosen key is swallowed (down, repeats and its release) so the
    # digit doesn't also switch party members.

    _PICKER_KEYS = frozenset({'0', '1', '2', '3', '4', '5', '6', '7', '8',
                              '9', 'esc'})

    def _set_pending(self, action):
        with self._pending_lock:
            self._clear_pending_locked()
            self._pending_action = action
            self._pending_slot_bank = 0
            if self._picker_hook is None:
                try:
                    self._picker_hook = keyboard.hook(self._picker_key_hook,
                                                      suppress=True)
                except Exception as e:
                    print(f"[macro] slot picker hook failed: {e}")
            cancel_evt = threading.Event()
            self._pending_cancel_evt = cancel_evt
            threading.Thread(
                target=self._pending_watch,
                args=(cancel_evt, time.monotonic() + self.SLOT_TIMEOUT_S),
                daemon=True, name='macro-slot-picker').start()
        print(f"[macro] {action}: press 1-9 for slots 1-9, "
              f"0 then 1-9 for slots 10-18, "
              f"ESC to cancel ({int(self.SLOT_TIMEOUT_S)}s, in Genshin only)")
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
        if self._pending_cancel_evt is not None:
            self._pending_cancel_evt.set()
            self._pending_cancel_evt = None
        self._pending_action = None
        self._pending_slot_bank = 0
        # Keep the hook until every key it swallowed has been released,
        # so the game never sees a lone release or the auto-repeat of the
        # chosen digit; the hook drops itself after that last release.
        if not self._picker_swallowed:
            self._drop_picker_hook_locked()

    def _drop_picker_hook_locked(self):
        h = self._picker_hook
        self._picker_hook = None
        if h is not None:
            try:
                keyboard.unhook(h)
            except Exception:
                pass

    def _drop_idle_picker_hook(self):
        with self._pending_lock:
            if self._pending_action is None and not self._picker_swallowed:
                self._drop_picker_hook_locked()

    def _picker_key_hook(self, event):
        # HOOK THREAD (see above). True = let the key through.
        name = (event.name or '').lower() if event.name else ''
        if name not in self._PICKER_KEYS:
            return True
        scan = event.scan_code
        if event.event_type == 'up':
            if scan in self._picker_swallowed:
                self._picker_swallowed.discard(scan)
                if (not self._picker_swallowed
                        and self._pending_action is None):
                    # Unhooking from inside the hook would mutate the
                    # list the package is iterating — do it off-thread.
                    threading.Thread(target=self._drop_idle_picker_hook,
                                     daemon=True).start()
                return False
            return True
        if scan in self._picker_swallowed:
            return False                 # auto-repeat of a consumed key
        if self._pending_action is None:
            return True
        try:
            # Already down before this event (held since before the
            # picker armed) -> auto-repeat, not a choice.
            if keyboard.is_pressed(scan):
                return True
        except Exception:
            pass
        if not is_allowed_game_only():
            self._slot_left_game_async()
            return True
        self._picker_swallowed.add(scan)
        if name == 'esc':
            self._slot_cancel_async()
        elif name == '0':
            self._slot_high_bank_async()
        else:
            self._slot_chosen_async(int(name))
        return False

    def _pending_watch(self, cancel_evt, deadline):
        """Ends the picker on timeout or as soon as Genshin loses the
        foreground. Exits quietly once `cancel_evt` is set (choice made,
        Esc, re-armed, shutdown)."""
        while not cancel_evt.wait(self.SLOT_FOCUS_POLL_S):
            if time.monotonic() >= deadline:
                self._end_pending(cancel_evt, 'timed out')
                return
            if not is_allowed_game_only():
                self._end_pending(cancel_evt, 'cancelled - left the game')
                return

    def _end_pending(self, cancel_evt, why):
        with self._pending_lock:
            if self._pending_cancel_evt is not cancel_evt:
                return              # a newer picker (or none) owns it now
            action = self._pending_action
            self._clear_pending_locked()
        if action:
            print(f"[macro] {action} {why}")
        self._emit()

    def _slot_chosen_async(self, digit):
        # keyboard package fires this on its listener thread; offload so
        # we don't block the listener on file IO.
        threading.Thread(target=self._handle_slot_choice,
                         args=(digit,), daemon=True).start()

    def _slot_left_game_async(self):
        evt = self._pending_cancel_evt
        if evt is not None:
            threading.Thread(target=self._end_pending,
                             args=(evt, 'cancelled - left the game'),
                             daemon=True).start()

    def _handle_slot_choice(self, digit):
        with self._pending_lock:
            action = self._pending_action
            slot = self._pending_slot_bank + digit
            self._clear_pending_locked()
        if action == 'save':
            # No confirm dialog is possible from an in-game hotkey; keep
            # the overwrite but make it unmistakable in the log.
            path = macros_dir() / f'macro_{slot}.json'
            if path.exists():
                old = self._read_slot_name(slot)
                print(f"[macro] overwriting slot {slot}"
                      + (f" ({old!r})" if old else '')
                      + " from the in-game save hotkey")
            self.save_slot(slot)
        elif action == 'load':
            self.load_slot(slot)
        self._emit()

    def _slot_high_bank_async(self):
        # This runs on the keyboard listener. A single atomic assignment
        # preserves 0 -> digit ordering without blocking or off-thread races.
        # The following digit callback offloads the actual file IO.
        if self._pending_action is not None:
            self._pending_slot_bank = 9

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
            self._stop_record_ticker_locked()
            self._unhook()
            self._state = self.STATE_IDLE
            self._playing_slot = None
            self._playing_name = ''
            self._playing_source = ''
            self._playing_events = 0
        with self._pending_lock:
            self._clear_pending_locked()
            self._picker_swallowed.clear()
            self._drop_picker_hook_locked()
        if self._play_thread is not None:
            self._play_thread.join(timeout=2.0)

    # ---- internal ----

    _COMMAND_LOCK_WAIT_S = 0.4
    # Buffer / slot commands (UI buttons, the slot picker, record toggle)
    # are deliberate one-shot actions, not spam-prone triggers: wait longer
    # than an interrupt hand-off (play_slot may hold the lock ~1 s while
    # the old worker joins) before reporting "busy".
    _SLOT_CMD_WAIT_S = 2.5

    _NO_BACKEND_MSG = ('Input backend unavailable - check the Arduino '
                       'connection or switch to Software on the Main tab.')

    @contextlib.contextmanager
    def _command(self, what, timeout=None):
        """Acquire the command lock or give up.

        A trigger that cannot get the lock promptly is STALE by the time it
        would run — the user has usually pressed again — so drop it with a
        log line instead of queueing. Combined with the off-listener
        dispatch in KeybindManager, this is what makes spamming a hotkey
        behave: presses either act now or say why they did not."""
        if timeout is None:
            timeout = self._COMMAND_LOCK_WAIT_S
        if not self._command_lock.acquire(timeout=timeout):
            print(f"[macro] {what} dropped - another command still running")
            yield False
            return
        try:
            yield True
        finally:
            self._command_lock.release()

    def _is_repeated_play_trigger_locked(self, trigger_key):
        """True if `trigger_key` fired within TRIGGER_DEBOUNCE_S. A press
        that passes refreshes the window, so a held hotkey cannot
        relaunch the instant playback exits.

        This used to also swallow the press right after an explicit stop:
        the previous worker was still winding down, so the press stamped
        the window here and was then refused by the state check below,
        killing every press for the next 250 ms. That was the "stop the
        macro, press play once, nothing happens" bug. The fix is in
        `play_slot` / `play`, which now wait the winding-down worker out
        and start, so no refusal reaches this stamp."""
        now = time.monotonic()
        last = self._last_play_trigger_ts.get(trigger_key)
        if last is not None and now - last < self.TRIGGER_DEBOUNCE_S:
            print(f"[macro] {trigger_key[0]} {trigger_key[1] or ''} ignored - "
                  f"debounce ({(now - last) * 1000:.0f} ms since last, "
                  f"need {self.TRIGGER_DEBOUNCE_S * 1000:.0f})")
            return True
        self._last_play_trigger_ts[trigger_key] = now
        return False

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

    # Recording filter rules (both devices):
    #   * PRESSES are captured only while Genshin is the foreground
    #     window — anything typed in another app (browser, terminal, the
    #     UI) is dropped.
    #   * RELEASES are captured iff their press was captured in this
    #     recording — even if Genshin lost focus in between (press W in
    #     game, alt-tab, release W: the release used to be dropped, so the
    #     next W press was swallowed as auto-repeat and the macro held W
    #     to the end). Releases of keys pressed BEFORE recording started
    #     (e.g. the record hotkey itself) are never captured.

    def _chord_part_for(self, name):
        for part, names in self._rec_ignore_parts.items():
            if name in names:
                return part
        return None

    def _on_kb_event(self, event):
        if self._state != self.STATE_RECORDING:
            return
        name = (event.name or '').lower() if event.name else ''
        if not name or name in self._excluded_kb:
            return
        etype = event.event_type
        if self._rec_ignore_parts:
            # Record-hotkey chord keys still held from the press that
            # started this recording: ignore until they come up once.
            part = self._chord_part_for(name)
            if part is not None:
                if etype == 'up':
                    self._rec_ignore_parts.pop(part, None)
                return
        # Key identity by scan code so a release whose name changed with
        # Shift ('1' down, '!' up) still matches its press.
        scan = getattr(event, 'scan_code', None)
        key_id = (scan, bool(getattr(event, 'is_keypad', False))) if scan \
            else name
        if etype == 'up':
            recorded = self._rec_held_keys.pop(key_id, None)
            if recorded is not None:
                self._engine.record_keyboard(recorded, 'up')
            return
        if not is_allowed_game_only():
            return
        recorded = self._rec_held_keys.setdefault(key_id, name)
        # Engine applies the auto-repeat suppression rule + timestamp.
        self._engine.record_keyboard(recorded, etype)

    def _on_mouse_event(self, event):
        if self._state != self.STATE_RECORDING:
            return
        # Buttons only: cursor movement (MoveEvent) and the wheel
        # (WheelEvent) are never recorded — playback has no cursor model.
        if not isinstance(event, mouse.ButtonEvent):
            return
        btn = (event.button or '').lower()
        if not btn or btn in self._excluded_mouse:
            return
        etype = event.event_type
        if etype == 'up':
            if btn in self._rec_held_mouse:
                self._rec_held_mouse.discard(btn)
                self._engine.record_mouse(btn, 'up')
            return
        if not is_allowed_game_only():
            return
        self._rec_held_mouse.add(btn)        # 'down' or 'double'
        self._engine.record_mouse(btn, etype)

    def _emit(self):
        try:
            self._on_status(self.snapshot())
        except Exception:
            pass
