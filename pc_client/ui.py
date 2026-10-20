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


def _strip_motw_under(root):
    """Remove the `Zone.Identifier` NTFS alternate data stream from every
    file under `root`. Files extracted from a zip downloaded via browser
    inherit Mark-of-the-Web; the .NET Framework assembly loader silently
    fails to resolve `Python.Runtime.Loader.Initialize` from a
    MOTW-tagged DLL, which kills pywebview's WinForms backend. Stripping
    the ADS once at startup makes downstream loads trust the bundled
    assemblies. Cheap (~hundreds of os.remove on a missing path)."""
    if not root or not os.path.exists(root):
        return
    marker = os.path.join(root, '.motw_stripped')
    if os.path.exists(marker):
        return
    try:
        for dirpath, _, files in os.walk(root):
            for name in files:
                # Removing the ADS — a NoneFoundError just means the file
                # never carried MOTW; ignore.
                try:
                    os.remove(os.path.join(dirpath, name) + ':Zone.Identifier')
                except (FileNotFoundError, OSError):
                    pass
        # Drop a marker so subsequent launches skip the walk.
        try:
            with open(marker, 'w', encoding='utf-8') as fh:
                fh.write('1')
        except Exception:
            pass
    except Exception:
        pass


# Strip MOTW from bundled DLLs/assemblies before importing pywebview
# (which transitively loads pythonnet → Python.Runtime.dll). Only runs
# in frozen builds; source layouts have no MOTW.
if getattr(sys, 'frozen', False):
    _strip_motw_under(sys._MEIPASS)

import keyboard
import mouse
import keyboard._winkeyboard as _kb_winbackend
import mouse._winmouse as _ms_winbackend
import webview


# --- LL-hook capture ---------------------------------------------------------
# The `keyboard` + `mouse` packages each install a global low-level hook
# (WH_KEYBOARD_LL / WH_MOUSE_LL) via SetWindowsHookEx and never expose
# the handle, so we can't call UnhookWindowsHookEx ourselves. They DO
# register an atexit cleanup, but:
#   - mouse passes the right handle, but atexit fires after WebView2 +
#     Python finish their teardown — i.e. AFTER the user's mouse has
#     already been laggy for a second.
#   - keyboard's atexit is buggy (passes the callback object instead of
#     the HHOOK), so UnhookWindowsHookEx fails silently and the keyboard
#     hook only goes away when the OS reclaims the dying process.
#
# Workaround: monkey-patch SetWindowsHookEx in both backends *before*
# any hotkey is registered so we capture the HHOOK as the libraries
# install their hooks. Then in detach() we call UnhookWindowsHookEx
# directly while the host is still alive, dropping both LL hooks
# before WebView2's slow shutdown begins. Sub-50 ms vs ~1 s of mouse
# lag.

_captured_ll_hooks = {'kb': None, 'mouse': None}


def _wrap_set_hook(real_setter, slot):
    def wrapped(*args, **kwargs):
        handle = real_setter(*args, **kwargs)
        if handle:
            _captured_ll_hooks[slot] = handle
        return handle
    return wrapped


_kb_winbackend.SetWindowsHookEx = _wrap_set_hook(
    _kb_winbackend.SetWindowsHookEx, 'kb')
_ms_winbackend.SetWindowsHookEx = _wrap_set_hook(
    _ms_winbackend.SetWindowsHookEx, 'mouse')


def _release_ll_hooks():
    """Drop the captured WH_KEYBOARD_LL / WH_MOUSE_LL hooks. Returns
    a (kb_ok, mouse_ok) tuple for diagnostics. Idempotent — second
    call no-ops."""
    user32 = ctypes.windll.user32
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL
    results = []
    for slot in ('kb', 'mouse'):
        h = _captured_ll_hooks.get(slot)
        if not h:
            results.append(None)
            continue
        try:
            ok = bool(user32.UnhookWindowsHookEx(h))
        except Exception:
            ok = False
        _captured_ll_hooks[slot] = None
        results.append(ok)
    return tuple(results)

import ui_core
from config import (ALBUM_DIFFICULTY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_SONG_COUNT)
from ui_core import (ACTION_DEBUG, ACTION_MACRO_LOAD, ACTION_MACRO_PLAY,
                     ACTION_MACRO_RECORD, ACTION_MACRO_SAVE,
                     ACTION_MACRO_STOP, ACTION_PAUSE, ACTION_START_STOP,
                     ACTIVE_MACRO_COUNT, BOT_ACTIONS, BotController,
                     KeybindManager, MACRO_ACTIONS, MACRO_CONFLICT_IGNORE,
                     MACRO_CONFLICT_INTERRUPT, MACRO_SLOT_COUNT,
                     MacroController, QueueWriter,
                     APP_VERSION, UI_WINDOW_TITLE, focus_game_window, is_admin,
                     is_frozen, js_event_to_hotkey, load_settings,
                     macros_dir, make_input_backend, save_settings,
                     warn_if_not_admin)
from ui_core import bot_options_for_settings, clamp_song_count, normalize_bot_options, normalize_difficulty
from ui_core import normalize_hotkey, validate_hotkey
from ui_core import FOCUS_GAME_NOT_FOUND, FOCUS_GAME_STOPPED, focus_game_window_ex
# Imported defensively: a missing pyserial must only break the Arduino
# path (make_input_backend imports controller lazily for the same reason).
try:
    from controller import ArduinoConnectionError
except ImportError:
    ArduinoConnectionError = RuntimeError
# Live lane state for the Main-tab lane strip (`lanes`). Looked up with
# getattr each tick so a detector module without active_lanes() just
# reports all-idle lanes instead of breaking the drain.
try:
    import detector as _detector_mod
except Exception:
    _detector_mod = None
try:
    from game_window import find_game_window as _find_game_window
except Exception:
    _find_game_window = None

# Lane order matches config.KEYS (a s d j k l).
_LANE_COUNT = 6
# `game_window_found` poll period (FindWindowW — never blocks).
GAME_WINDOW_POLL_S = 0.5


def _resource_dir():
    """Where to read bundled read-only assets from. PyInstaller onedir
    extracts data files under sys._MEIPASS (which equals the .exe's
    `_internal/` folder for onedir, or a temp extraction dir for
    onefile). Source layouts read straight from the package directory."""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).parent


WEB_DIR = _resource_dir() / 'web'
INDEX_PATH = WEB_DIR / 'index.html'
ICON_PATH = _resource_dir() / 'assets' / 'icon.ico'

POLL_MS = 50          # status drain cadence — matches Tk version
# Unchanged status is re-pushed at most this often (keep-alive) — every
# other tick pushes only when the status changed or there is log text.
STATUS_KEEPALIVE_S = 1.0
# detach() waits at most this long for a pending engine-ownership
# transition before the final shutdown (longest legitimate hold: a forced
# backend switch = bot join 5 s + macro join 2 s).
TEARDOWN_ENGINE_LOCK_TIMEOUT_S = 8.0
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


APP_DISPLAY_NAME = 'Genshin Rhythm Bot'


def _register_app_id(icon_path):
    """Persist the AppUserModelID's display name + icon under HKCU so
    Windows shell uses them in the right-click jump-list / hover label
    instead of falling back to the process exe's File Description
    ("Python 3.x"). Idempotent — safe to run on every launch."""
    try:
        import winreg
        key_path = f'Software\\Classes\\AppUserModelId\\{APP_USER_MODEL_ID}'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, 'DisplayName', 0, winreg.REG_SZ,
                              APP_DISPLAY_NAME)
            if icon_path and os.path.exists(icon_path):
                # Same `,<index>` form as RelaunchIconResource — picks
                # the first icon group inside the .ico file.
                winreg.SetValueEx(k, 'IconResource', 0, winreg.REG_SZ,
                                  f'{os.path.abspath(icon_path)},0')
    except Exception as e:
        print(f"[ui] AppID registry write failed: {e}")


def _set_app_user_model_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
    except Exception as e:
        print(f"[ui] AppUserModelID set failed: {e}")


# --- per-window AppUserModelID via IPropertyStore --------------------------
# The process-wide call (`SetCurrentProcessExplicitAppUserModelID`) silently
# loses if any window — including hidden ones from pythonnet/.NET CLR or
# pywebview's WinForms backend — was created before we got to it. The
# per-window property is authoritative: the shell honors PKEY_AppUserModel_ID
# on the window's IPropertyStore even if a process-wide AppID was set
# differently. Without this, the right-click / hover labels keep falling
# back to python.exe's File Description.

class _GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', ctypes.c_ulong),
        ('Data2', ctypes.c_ushort),
        ('Data3', ctypes.c_ushort),
        ('Data4', ctypes.c_ubyte * 8),
    ]


def _make_guid(d1, d2, d3, d4):
    g = _GUID()
    g.Data1 = d1
    g.Data2 = d2
    g.Data3 = d3
    for i, b in enumerate(d4):
        g.Data4[i] = b
    return g


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [('fmtid', _GUID), ('pid', ctypes.wintypes.DWORD)]


# 24-byte PROPVARIANT layout on x64 (vt + 3 reserved words + 16-byte union).
class _PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ('vt', ctypes.wintypes.USHORT),
        ('wReserved1', ctypes.wintypes.WORD),
        ('wReserved2', ctypes.wintypes.WORD),
        ('wReserved3', ctypes.wintypes.WORD),
        ('val', ctypes.c_void_p),
        ('_pad', ctypes.c_void_p),
    ]


# All AppUserModel_* PROPERTYKEYs share fmtid {9F4C2855-...}; pid varies.
_AUM_FMTID = _make_guid(
    0x9F4C2855, 0x9F79, 0x4B39,
    [0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3])


def _aum_pkey(pid):
    pk = _PROPERTYKEY()
    pk.fmtid = _AUM_FMTID
    pk.pid = pid
    return pk


_PKEY_AppUserModel_ID = _aum_pkey(5)
# RelaunchDisplayNameResource (4): explicit text the shell shows in the
# jump-list / hover label for this window. Bypasses AppID display-name
# lookup, so it works even if the shell has already cached the wrong
# group identity for this exe.
_PKEY_AppUserModel_RelaunchDisplayNameResource = _aum_pkey(4)
# RelaunchIconResource (3): icon shown in the jump-list group header.
_PKEY_AppUserModel_RelaunchIconResource = _aum_pkey(3)
# RelaunchCommand (2): what to launch when the user "Pin to taskbar" +
# clicks the pinned tile later. Must be a full executable path string.
_PKEY_AppUserModel_RelaunchCommand = _aum_pkey(2)

# {886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99} — IID_IPropertyStore
_IID_IPropertyStore = _make_guid(
    0x886D8EEB, 0x8CF2, 0x4446,
    [0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99])


_VT_LPWSTR = 31


def _stamp_window_props(hwnd, props):
    """Stamp a list of (PROPERTYKEY, string) on the window's IPropertyStore.
    Per-window AppID + display name + icon resource together convince the
    shell to render this window with our identity in the jump-list, hover
    label, and pinned-tile display — even if the shell has already
    finalized taskbar grouping under python.exe's implicit AppID.

    PROPVARIANT is built by hand because InitPropVariantFromString is an
    inline propvarutil.h helper, not exported. The unicode buffers are
    kept alive in the local list `keepalive` so they outlive every
    SetValue + Commit pair (PropVariantClear would CoTaskMemFree them —
    we own the storage, so skip the clear)."""
    if not hwnd or not props:
        return
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.wintypes.LONG
        ole32.CoInitialize.argtypes = [ctypes.c_void_p]
        ole32.CoInitialize.restype = ctypes.wintypes.LONG

        ole32.CoInitialize(None)
        ps_ptr = ctypes.c_void_p()
        hr = shell32.SHGetPropertyStoreForWindow(
            hwnd, ctypes.byref(_IID_IPropertyStore), ctypes.byref(ps_ptr))
        if hr != 0 or not ps_ptr.value:
            try:
                sys.__stdout__.write(
                    f"[ui] SHGetPropertyStoreForWindow hr=0x{hr & 0xFFFFFFFF:x}\n")
                sys.__stdout__.flush()
            except Exception:
                pass
            return
        vtbl = ctypes.cast(
            ps_ptr,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        SetValueProto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.LONG, ctypes.c_void_p,
            ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))
        CommitProto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.LONG, ctypes.c_void_p)
        ReleaseProto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.ULONG, ctypes.c_void_p)
        SetValue = SetValueProto(vtbl[6])
        Commit = CommitProto(vtbl[7])
        Release = ReleaseProto(vtbl[2])
        try:
            keepalive = []
            for pkey, value in props:
                str_buf = ctypes.create_unicode_buffer(value)
                keepalive.append(str_buf)
                pv = _PROPVARIANT()
                ctypes.memset(ctypes.byref(pv), 0, ctypes.sizeof(pv))
                pv.vt = _VT_LPWSTR
                pv.val = ctypes.cast(str_buf, ctypes.c_void_p).value
                hr_set = SetValue(
                    ps_ptr, ctypes.byref(pkey), ctypes.byref(pv))
                try:
                    sys.__stdout__.write(
                        f"[ui] SetValue pid={pkey.pid} "
                        f"hr=0x{hr_set & 0xFFFFFFFF:x} val={value!r}\n")
                    sys.__stdout__.flush()
                except Exception:
                    pass
            hr_commit = Commit(ps_ptr)
            try:
                sys.__stdout__.write(
                    f"[ui] Commit hr=0x{hr_commit & 0xFFFFFFFF:x}\n")
                sys.__stdout__.flush()
            except Exception:
                pass
        finally:
            Release(ps_ptr)
    except Exception as e:
        try:
            sys.__stdout__.write(f"[ui] _stamp_window_props failed: {e}\n")
            sys.__stdout__.flush()
        except Exception:
            pass


def _set_window_app_id(hwnd, app_id, icon_path=None):
    """Convenience wrapper: stamp AppID + display name + icon resource +
    relaunch command. RelaunchIconResource needs the `,<index>` suffix
    so the shell's resource parser picks the first icon group out of
    the .ico file (without it, shell falls back to the host process exe
    icon = python.exe's). RelaunchCommand is required by some shell code
    paths to honor RelaunchIconResource — set it to the current
    interpreter + script."""
    props = [
        (_PKEY_AppUserModel_ID, app_id),
        (_PKEY_AppUserModel_RelaunchDisplayNameResource, APP_DISPLAY_NAME),
    ]
    if icon_path and os.path.exists(icon_path):
        icon_resource = f'{os.path.abspath(icon_path)},0'
        props.append(
            (_PKEY_AppUserModel_RelaunchIconResource, icon_resource))
    # Relaunch command — what the shell would run if the user pinned the
    # tile and clicked it later. Quote the script path in case it ever
    # contains a space.
    try:
        script = os.path.abspath(sys.argv[0])
        relaunch = f'"{sys.executable}" "{script}"'
        props.append((_PKEY_AppUserModel_RelaunchCommand, relaunch))
    except Exception:
        pass
    _stamp_window_props(hwnd, props)


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
        # Set to True after detach() runs so the closing+closed event
        # pair doesn't double-tear-down (closing fires first, closed
        # follows after the window destroys).
        self._detached = False

        # Live UI form state — kept in sync via the set_* methods so the
        # hotkey worker thread (which can't reach the JS layer) has a
        # snapshot to feed into BotController.start. `difficulty` is
        # always stored as a list of one or more difficulty names; a
        # single-element list reproduces the old single-difficulty
        # flat loop, multi-element triggers the per-position interleave
        # in AlbumRunner. Seeded from the persisted `bot_options` (mode,
        # songs, difficulty, replay_canorus — validated field-by-field);
        # `debug` always starts off.
        self._opts = {
            **normalize_bot_options(self._settings.get('bot_options')),
            'debug': False,
        }
        # One-line Hu Tao theme reminder on the first Start of a session.
        self._theme_reminder_shown = False
        # Last backend-build failure message (user-facing), surfaced as
        # the bot's last_error when the bot was the caller.
        self._backend_error = ''
        # Window rect saved by toggle_maximize (None = not maximized).
        self._restore_rect = None
        # Set once the user agreed to discard unsaved macro work (in-app
        # dialog → close(confirmed=True), or the native prompt) so the
        # close isn't asked about twice. See allow_close.
        self._close_confirmed = False
        # The page's events editor holds local edits Python's buffer
        # doesn't have yet (mirrored by macro_set_editor_dirty) — the
        # native close guard must count them too.
        self._editor_dirty = False
        # Highest update sequence applied to _editor_dirty; every bridge
        # call runs on its own thread, so updates can arrive reordered.
        self._editor_dirty_seq = 0
        # Bumped by every get_initial_state (one per page load) and handed
        # to the page; dirty updates carry it so calls a previous page left
        # in flight can never override the current page — no reliance on
        # the page's clock.
        self._editor_page_gen = 0
        # Set first thing in detach(): every start path, the backend
        # switch and _get_arduino refuse from then on.
        self._tearing_down = False
        # Count of BridgeApi.restart_in_mode calls in progress (waiting for
        # _engine_lock or joining the old run; changed under self._lock):
        # while > 0, start_stop treats a press as Stop and issues it
        # WITHOUT waiting for _engine_lock.
        self._restart_pending = 0
        # True while _push is inside evaluate_js (see detach).
        self._push_in_flight = False

        self._status_lock = threading.Lock()
        self._status = {
            'state': BotController.STATE_IDLE,
            'mode': '',
            'song': '',
            'fps': 0.0,
            'debug': False,
            # Why the last bot run ended badly (cleared on every Start).
            'last_error': '',
            # Direction A data contract (ui-data-contract.md):
            # lanes — a s d j k l pressed by the detector (drain-polled).
            'lanes': [False] * _LANE_COUNT,
            # album_progress — None or {index, total, difficulty,
            # difficulties, results} from the album runner; reset by
            # Start, kept after the run ends.
            'album_progress': None,
            # run_started_at — epoch secs the current/last run began
            # (0 from Start until it begins); UI derives Elapsed.
            'run_started_at': 0,
            # game_window_found — FindWindowW poll (drain, ~500 ms).
            'game_window_found': False,
            'prereq_tip_dismissed': bool(
                self._settings.get('prereq_tip_dismissed', False)),
            'admin': is_admin(),
            'keybinds': dict(self._settings['keybinds']),
            'macro_state': MacroController.STATE_IDLE,
            'macro_events': 0,
            'macro_slots': [],
            'macro_slot_names': {},
            'macro_loaded': 0,
            'macro_dirty': False,
            'macro_pending': '',
            'macro_playing_slot': 0,
            'macro_playing_name': '',
            'macro_playing_source': '',
            'macro_active_macros': [
                dict(x) for x in self._settings.get('macro_active_macros', [])
            ],
            'macro_conflict_policy': self._settings.get(
                'macro_conflict_policy', MACRO_CONFLICT_IGNORE),
            'input_backend': self._settings.get('input_backend', 'arduino'),
        }

        self._log_queue = queue.Queue(maxsize=LOG_QUEUE_SIZE)
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr

        # Single Arduino instance shared across the bot worker and the
        # macro tool — only one process can hold the COM port. Lazy-built
        # on first use so launching the UI without a board still loads
        # the page (errors surface in the log when the user clicks
        # Start / Record).
        self._arduino = None
        self._arduino_lock = threading.Lock()
        # Serializes engine-ownership transitions — bot start, macro
        # record/play start, backend switch — so two API/hotkey threads
        # can't both observe "the other engine is idle" and start on the
        # same backend. Reentrant so a *_force path can stop the other
        # engine and start its own under one hold.
        self._engine_lock = threading.RLock()

        self._bot = BotController(self._enqueue_status,
                                  controller_provider=self._bot_backend)
        self._macro = MacroController(controller_provider=self._get_arduino,
                                      on_status=self._enqueue_status)
        self._keybinds = KeybindManager(self._ui_hwnd)
        # Serializes every keybind install (see _install_keybinds). Lock
        # order: _keybind_lock, then self._lock -- never the reverse.
        self._keybind_lock = threading.RLock()
        self._drain_stop = threading.Event()
        self._drain_thread = None

        # Seed _status with the live macro snapshot — without this the
        # first drain tick (50 ms after attach) overwrites whatever the
        # JS init() pulled via get_initial_state with the empty defaults
        # above, so the Macros tab looks blank until something fires
        # _emit(). After this seed the drain pushes actual slot info on
        # tick 1.
        self._enqueue_status(self._macro.snapshot())

    # ---- lifecycle hooks called from main() ----

    def attach_window(self, window):
        self._window = window
        # Redirect stdio after the page loads — log queue will be drained
        # once the JS appendLog binding is reachable.
        sys.stdout = QueueWriter(self._log_queue, mirror=self._orig_stdout)
        sys.stderr = QueueWriter(self._log_queue, mirror=self._orig_stderr)
        # First line in the log pane on every launch. If this version does
        # not match what you just installed, you are running a different
        # build. Must come after the redirect above, or a --windowed .exe
        # (no console) never shows it.
        print(f"[ui] Genshin Rhythm Bot v{APP_VERSION}")
        self._install_keybinds()
        self._drain_thread = threading.Thread(
            target=self._drain_loop, daemon=True, name='ui-drain')
        self._drain_thread.start()

    def detach(self):
        """Tear down on window close. Drop the LL hooks first (see below),
        then restore stdio so any teardown prints land on the original
        streams. Idempotent — `closing` and `closed` events both call this
        and only the first call does work."""
        if self._detached:
            return
        self._detached = True
        # Refuse every new ownership transition from here on: bot Start /
        # mode switch / backend switch refuse, and _get_arduino won't
        # build a backend (so a macro Play can't get one either). A Start
        # already queued on _engine_lock must not resume after the final
        # backend close below and restart input while the window closes.
        self._tearing_down = True

        # FIRST: drop the actual Win32 LL hooks via the captured handles.
        # This is the move that kills the mouse-lag-on-close — once the
        # WH_MOUSE_LL hook is gone, system mouse events stop routing
        # through our process, so it doesn't matter how slow the rest
        # of teardown (WebView2, pythonnet, etc) is.
        _release_ll_hooks()

        # Stop pushing NOW (flags only — instant). _push re-checks
        # _stopping right before evaluate_js, and evaluate_js needs THIS
        # UI thread (pywebview runs `closing` on it), so the drain must
        # not start another call it can't finish until we return.
        self._stopping = True
        self._drain_stop.set()

        # Hide the window before any blocking teardown. `closing` runs
        # synchronously on the WinForms UI thread, and the bot / macro
        # joins below can take seconds mid-album — hidden, that wait is
        # invisible instead of a frozen window. Pure visibility change:
        # the hook / drain / WebView2 ordering is untouched (WebView2 is
        # only disposed after this handler returns).
        self._hide_window_for_teardown()
        # Ask the bot to wind down now (non-blocking) so its exit overlaps
        # the drain join + hook release; _bot.shutdown() below still joins.
        try:
            self._bot.stop()
        except Exception:
            pass
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr

        # Wait for the drain thread to actually exit before pywebview
        # gets to dispose WebView2. Otherwise the drain's next 50 ms tick
        # fires evaluate_js into a disposed control and pywebview logs
        # `[pywebview] Error occurred in script ... ObjectDisposedException`
        # (the throw itself is swallowed by _push's bare except, but the
        # log line is emitted from inside pywebview before that). One
        # tick is 50 ms; 500 ms is generous.
        # Exception: a push already IN FLIGHT is parked waiting for this
        # very UI thread and cannot finish until we return — joining would
        # only burn the full 500 ms, so skip the wait in that case (the
        # flags above guarantee it is the drain's last call).
        if self._drain_thread is not None:
            if self._push_in_flight:
                print('[ui] drain push in flight during close — not waiting')
            else:
                try:
                    self._drain_thread.join(timeout=0.5)
                except Exception:
                    pass

        # Then drop subscribers from the keyboard/mouse packages so any
        # in-flight callbacks don't see torn-down state.
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass

        try:
            self._keybinds.clear_all()
        except Exception:
            pass
        # Serialize the final shutdown against a pending ownership
        # transition (a Start / Play / backend switch that already holds
        # _engine_lock finishes first; anything queued behind us sees
        # _tearing_down and refuses). Bounded: the longest legitimate hold
        # is a forced backend switch (bot join 5 s + macro join 2 s).
        got_engine = False
        try:
            got_engine = self._engine_lock.acquire(
                timeout=TEARDOWN_ENGINE_LOCK_TIMEOUT_S)
        except Exception:
            got_engine = False
        if not got_engine:
            print('[ui] engine lock busy at close — tearing down anyway')
        try:
            try:
                self._macro.shutdown()
            except Exception as e:
                print(f"[ui] macro shutdown error: {e}")
            try:
                self._bot.shutdown()
            except Exception as e:
                print(f"[ui] bot shutdown error: {e}")
            # Close the shared Arduino last — both bot + macro have stopped
            # by now so no more writes are coming.
            with self._arduino_lock:
                ard = self._arduino
                self._arduino = None
            if ard is not None:
                try:
                    ard.close()
                except Exception as e:
                    print(f"[ui] arduino close error: {e}")
        finally:
            if got_engine:
                self._engine_lock.release()

    # ---- JS-callable methods ----

    def get_initial_state(self):
        with self._status_lock:
            last_error = self._status.get('last_error', '')
        with self._lock:
            # A (re)loaded page starts with no editor edits.
            self._editor_page_gen = getattr(self, '_editor_page_gen', 0) + 1
            self._editor_dirty = False
            self._editor_dirty_seq = 0
            return {
                **self._opts,
                'page_gen': self._editor_page_gen,
                'last_error': last_error,
                'prereq_tip_dismissed': bool(
                    self._settings.get('prereq_tip_dismissed', False)),
                'admin': is_admin(),
                'keybinds': dict(self._settings['keybinds']),
                'difficulties': list(ALBUM_DIFFICULTY_COORDS),
                'macro': self._macro.snapshot(),
                'macro_active_macros': [
                    dict(x) for x in self._settings.get(
                        'macro_active_macros', [])
                ],
                'macro_conflict_policy': self._settings.get(
                    'macro_conflict_policy', MACRO_CONFLICT_IGNORE),
                'input_backend': self._settings.get('input_backend', 'arduino'),
            }

    def dismiss_prereq_tip(self):
        """Hide the Main-tab prerequisites tip for good: persists
        `prereq_tip_dismissed: true` (atomic settings write) and mirrors
        it into the status snapshot. Idempotent."""
        with self._lock:
            if self._settings.get('prereq_tip_dismissed') is not True:
                self._settings['prereq_tip_dismissed'] = True
                save_settings(self._settings)
        self._enqueue_status({'prereq_tip_dismissed': True})
        return {'ok': True}

    def start_stop(self):
        """Start/Stop toggle (UI button + Start/Stop hotkey).

        The STOP side never waits for _engine_lock: a mode switch holds
        that lock while it joins the old run (up to 5 s), and a Stop
        queued behind it would only land after the new mode had already
        started — the user's Stop must win. BotController.stop() is
        lock-free here and bumps the command generation restart_in_mode
        re-checks before starting the new mode. A press while a mode
        switch is pending therefore counts as Stop, too."""
        if self._bot.is_running() or self._restart_pending > 0:
            self._bot.stop()
            return True
        if self._tearing_down:
            return False
        # Ownership check + start under _engine_lock so a macro Play /
        # backend switch on another thread can't observe "bot idle" and
        # start alongside us.
        with self._engine_lock:
            if self._bot.is_running():
                # Another path started the bot while we waited for the
                # lock; this press meant Start, so leave that run alone.
                return True
            if self._macro.is_busy():
                # Hotkey path arrives here — JS path uses start_stop_force
                # after a confirm prompt. Refuse so we never run the bot
                # concurrently with a macro on the same Arduino.
                print('[ui] macro is active — stop it first '
                      '(or use the UI Start button to confirm)')
                return False
            return self._start_bot_locked()

    def _start_bot_locked(self):
        """Start the bot with the current Main-tab options. Caller holds
        _engine_lock and has checked that the macro is idle. Refuses once
        teardown began (a Start queued on _engine_lock behind detach)."""
        if self._tearing_down:
            print('[ui] app is closing — bot not started')
            return False
        with self._lock:
            opts = dict(self._opts)
        mode = opts.pop('mode', 'standalone')
        if not self._theme_reminder_shown:
            self._theme_reminder_shown = True
            print('[ui] Reminder: the rhythm detector needs the Hu Tao '
                  'theme equipped in the rhythm minigame — other themes '
                  'misread notes. Album mode also expects the game text '
                  'in English.')
        ok = self._bot.start(mode, opts)
        if not ok:
            print('[ui] start failed (already running?)')
        return bool(ok)

    def pause(self):
        if self._bot.is_running():
            self._bot.pause_toggle()
        return True

    def toggle_debug(self):
        """Live debug-viz toggle (F10 / JS). Only acts while running —
        idle presets go through set_debug. Keeps _opts['debug'] in sync
        so the checkbox and the next Start agree with the live state."""
        with self._lock:
            if self._bot.is_running():
                self._opts['debug'] = bool(self._bot.toggle_debug())
        return True

    def set_mode(self, mode):
        if mode in ('standalone', 'album'):
            with self._lock:
                self._opts['mode'] = mode
                self._persist_bot_options_locked()
        return True

    def _persist_bot_options_locked(self):
        """Mirror the persisted subset of _opts (mode / songs / difficulty
        / replay_canorus) into ui_settings.json. Caller holds self._lock —
        same discipline as rebind's save. No-op when nothing changed."""
        opts = bot_options_for_settings(self._opts)
        if self._settings.get('bot_options') == opts:
            return
        self._settings['bot_options'] = opts
        save_settings(self._settings)

    def restart_in_mode(self, mode):
        """Live mode switch from the JS layer. If the bot is running,
        stops it and immediately starts a fresh worker in `mode`. If
        idle, just records the new mode (same as set_mode). Returns True
        if a worker is running in `mode` after the call."""
        if mode not in ('standalone', 'album'):
            return False
        with self._lock:
            self._opts['mode'] = mode
            self._persist_bot_options_locked()
            opts = dict(self._opts)
        opts.pop('mode', None)

        # Mid-song hand-off: switching from a running standalone session
        # into album means the user is mid-song in the rhythm minigame
        # and expects the album runner to keep playing the current song
        # rather than navigate back to the album page. Tag the new run
        # so AlbumRunner skips the Go Perform → difficulty → Begin
        # Performance click chain on iter 0.
        with self._status_lock:
            old_mode = self._status.get('mode')
            old_state = self._status.get('state')
        if (mode == 'album' and old_mode == 'standalone'
                and old_state == BotController.STATE_RUNNING):
            opts['mid_song_start'] = True

        # Command generation at request time — captured BEFORE the
        # running check and before waiting for _engine_lock: any Stop from
        # here on bumps it and BotController aborts the switch (a Stop
        # before this point is visible to the checks below instead).
        req_gen = getattr(self._bot, 'generation', None)
        if not self._bot.is_running():
            return True
        if self._tearing_down:
            return False
        # The whole stop → join → start runs under _engine_lock so a
        # macro Play / backend switch can't slip in between the old run's
        # exit and the new run's start. Bounded by BotController's 5 s
        # join. BotController itself refuses the start if a Stop landed
        # meanwhile (command generation) — the user's Stop always wins.
        # While this is pending (incl. waiting for the lock), start_stop
        # treats a press as Stop and issues it without the lock, so that
        # Stop really does land before the new run would start.
        with self._lock:
            self._restart_pending += 1
        try:
            with self._engine_lock:
                if not self._bot.is_running() or self._tearing_down:
                    return not self._tearing_down
                if self._macro.is_busy():
                    return False
                ok = self._bot.restart_in_mode(
                    mode, opts, expected_generation=req_gen)
        finally:
            with self._lock:
                self._restart_pending -= 1
        if not ok:
            print(f'[ui] switch to {mode} mode did not start a new run '
                  '(Stop requested, or the old run did not exit in time)')
        return bool(ok)

    def set_songs(self, n):
        n = clamp_song_count(n)
        if n is None:
            return False
        with self._lock:
            self._opts['songs'] = n
            self._persist_bot_options_locked()
        return True

    def set_difficulty(self, d):
        """Accept a list of difficulty names (any non-empty subset of
        normal/hard/pro/legendary) — the JS multi-checkbox path. Also
        accepts back-compat shapes: 'all' (expanded to the full
        4-element list) or a single difficulty string. Empty / fully-
        invalid input is rejected so the user can't end up with no
        difficulty selected."""
        # Canonicalizes ordering so [legendary, normal] and
        # [normal, legendary] produce the same playback sequence.
        normalized = normalize_difficulty(d)
        if not normalized:
            return False
        with self._lock:
            self._opts['difficulty'] = normalized
            self._persist_bot_options_locked()
        return True

    def set_replay_canorus(self, v):
        with self._lock:
            self._opts['replay_canorus'] = bool(v)
            self._persist_bot_options_locked()
        return True

    def set_debug(self, v):
        """Checkbox → explicit SET, never a toggle: a toggle could fight
        the F10 hotkey or a viz window the user already closed (which
        clears the event without the checkbox having moved). While idle
        this is just the preset the next Start uses."""
        v = bool(v)
        with self._lock:
            self._opts['debug'] = v
            self._bot.set_debug(v)
        return True

    def rebind(self, action, payload):
        """Translate a JS keydown / mousedown payload to a hotkey and
        rebind `action`. Returns the new hotkey string, or
        {'ok': False, 'reason': ...} with reason one of
        'duplicate_hotkey', 'left_click_reserved', 'unsupported_key',
        'rhythm_key', 'bad_action', 'install_failed'. Everything is
        validated, installed and verified BEFORE the settings are saved,
        so a refused key leaves the previous binding saved and installed
        (it used to save first and then only log
        "failed to bind" after the old binding was already gone)."""
        if action not in BOT_ACTIONS and action not in MACRO_ACTIONS:
            return {'ok': False, 'reason': 'bad_action'}
        hotkey, reason = validate_hotkey(action, js_event_to_hotkey(payload))
        if reason:
            return {'ok': False, 'reason': reason}
        # One _keybind_lock hold covers duplicate check -> install of the
        # PROPOSED binding -> verify -> commit + save. Settings are only
        # touched once the binding is verifiably installed, and no other
        # installer can run in between with an older snapshot (BU11).
        with self._keybind_lock:
            with self._lock:
                if self._hotkey_in_use_locked(hotkey, current_action=action):
                    return {'ok': False, 'reason': 'duplicate_hotkey'}
            self._install_keybinds(keybind_overrides={action: hotkey})
            if self._keybinds.get_bindings().get(action) != hotkey:
                # Validated yet not installed: put back exactly what the
                # (unchanged) settings say instead of persisting a binding
                # that does nothing.
                self._install_keybinds()
                return {'ok': False, 'reason': 'install_failed'}
            # The key the user just pressed to capture this binding may
            # still be in the listener backlog: never let THAT press fire it.
            self._keybinds.latch_until_release(action)
            with self._lock:
                self._settings['keybinds'][action] = hotkey
                save_settings(self._settings)
                keybinds = dict(self._settings['keybinds'])
        # Push the new keybind map immediately rather than waiting for the
        # next drain tick.
        self._enqueue_status({'keybinds': keybinds})
        return hotkey

    @staticmethod
    def _capture_token(token):
        try:
            return int(token) if token is not None else None
        except (TypeError, ValueError):
            return None

    def capture_begin(self, token=None):
        """JS opened a hotkey-capture prompt (Main or Macros tab). Every
        global hotkey press is DROPPED until capture_end, so pressing F8 to
        try it as a binding can't also stop the bot. Auto-expires after
        KeybindManager.CAPTURE_SUSPEND_MAX_S in case capture_end is lost.
        `token` (increasing per prompt) keeps out-of-order begin/end
        bridge calls from cancelling a newer prompt's suspension."""
        return bool(self._keybinds.suspend(token=self._capture_token(token)))

    def capture_end(self, token=None):
        """JS closed a capture prompt (bound, Esc, left click, blur,
        timeout). Re-enables hotkeys unless a newer prompt is open."""
        return bool(self._keybinds.resume(token=self._capture_token(token)))

    def clear_log(self):
        # JS already cleared the pane; just drain the backlog so a fast
        # post-clear write doesn't immediately re-fill it.
        try:
            while True:
                self._log_queue.get_nowait()
        except queue.Empty:
            pass
        return True

    # ---- macro (called from the Macros tab) ----

    # Macro bridge methods return {'ok': bool, 'reason': str, 'message':
    # str, ...}. `message` is human-readable; user-actionable failures are
    # ALSO published as `macro_error` in the status so the Macros pill
    # shows them (and in-game hotkey failures surface the same way).

    def macro_toggle_record(self, force=False):
        """Start or stop recording. If the bot is running, refuse unless
        `force` is set — JS prompts the user with window.confirm and
        re-calls with force=True. Check + (forced bot stop) + start run
        under _engine_lock so a bot Start can't slip in between."""
        if self._macro.state() == MacroController.STATE_RECORDING:
            # Stopping takes over nothing — no engine hand-off needed.
            return dict(self._macro.stop_record())
        with self._engine_lock:
            refusal = self._macro_claim_engine_locked(force)
            if refusal is not None:
                return refusal
            excluded_kb, excluded_mouse = self._macro_excluded_bindings()
            # No held-key filtering (the click wasn't the hotkey), but the
            # record hotkey is still tracked so stopping with it in game
            # removes exactly that press.
            res = self._macro.toggle_record(excluded_kb=excluded_kb,
                                            excluded_mouse=excluded_mouse,
                                            track_chord=self._record_binding())
        return dict(res)

    def macro_play(self, force=False):
        with self._engine_lock:
            refusal = self._macro_claim_engine_locked(force)
            if refusal is not None:
                return refusal
            # Refusals that don't need the game (empty buffer, busy) are
            # answered without yanking focus to Genshin first.
            if (self._macro.state() != MacroController.STATE_RECORDING
                    and self._macro.event_count() > 0):
                refusal = self._prepare_and_focus_for_macro()
                if refusal is not None:
                    return refusal
            # require_foreground: re-verified as the very last step before
            # the worker starts, after every wait above.
            return self._explain_backend_failure(
                self._macro.play(require_foreground=True))

    def macro_stop(self):
        return dict(self._macro.stop_play())

    def macro_save_slot(self, n, name=None, expected_rev=None):
        return dict(self._macro.save_slot(n, name=name,
                                          expected_rev=expected_rev))

    def macro_load_slot(self, n, expected_rev=None):
        """`expected_rev` (optional): the macro_rev the user confirmed
        discarding; refused with 'stale' if the buffer changed since."""
        return dict(self._macro.load_slot(n, expected_rev=expected_rev))

    def macro_clear_slot(self, n):
        """Delete slot n. Active macros pointing at it are unassigned;
        their letters come back in `unassigned` (e.g. ['B'])."""
        res = dict(self._macro.clear_slot(n))
        if res.get('ok'):
            res['unassigned'] = self._clear_active_slot_refs(res.get('slot'))
        return res

    def macro_rename_slot(self, n, name):
        return dict(self._macro.rename_slot(n, name))

    # ---- input backend selector ----

    def set_input_backend(self, name, force=False):
        """Switch the active input backend. 'arduino' (real USB HID via
        the Leonardo) or 'software' (Win32 SendInput / mouse_event, no
        hardware). Refuses while bot or macro is busy unless `force` is
        set — JS prompts the user on conflict and re-calls with
        force=True. Closes the current backend so its COM port / hooks
        come down before the next caller lazy-builds the new one."""
        name = (name or '').lower()
        if name not in ('arduino', 'software'):
            return {'ok': False, 'reason': 'bad_backend'}
        # Check → (forced stop) → swap all under _engine_lock so no bot /
        # macro start can interleave. The joins inside are the existing
        # bounded ones (bot 5 s, macro 2 s).
        if self._tearing_down:
            return {'ok': False, 'reason': 'closing'}
        with self._engine_lock:
            if self._tearing_down:
                return {'ok': False, 'reason': 'closing'}
            with self._lock:
                current = self._settings.get('input_backend', 'arduino')
            if name == current:
                return {'ok': True, 'reason': 'unchanged'}
            if self._bot.is_running() or self._macro.is_busy():
                if not force:
                    return {'ok': False, 'reason': 'busy'}
                self._stop_bot_blocking()
                self._stop_macro_blocking()
                if self._bot.is_running() or self._macro.is_busy():
                    # Never swap a backend out from under a live worker.
                    print('[ui] bot/macro did not stop in time — input '
                          'backend not switched; try again')
                    return {'ok': False, 'reason': 'still_busy'}
            # Flip the setting and drop the cached backend in ONE
            # _arduino_lock hold. _get_arduino reads the setting under
            # the same lock, so a lazy build can never produce the OLD
            # backend type after this point (it used to: the setting was
            # written only after close()). The old backend is closed while
            # still holding the lock so a new Arduino can't try to open
            # the COM port before the old handle is released.
            with self._arduino_lock:
                ard = self._arduino
                self._arduino = None
                with self._lock:
                    self._settings['input_backend'] = name
                    save_settings(self._settings)
                if ard is not None:
                    try:
                        ard.close()
                    except Exception as e:
                        print(f"[ui] backend close error during switch: {e}")
        self._enqueue_status({'input_backend': name})
        print(f"[ui] input backend set to {name!r}")
        return {'ok': True}

    def macro_get_events(self):
        """Buffer for the events editor: {'events': [{time, device, key,
        event_type}, ...], 'rev': int, 'loaded': slot|0, 'dirty': bool,
        'state': str}. The editor pins `rev` + `loaded` and sends them
        back on Save."""
        return self._macro.get_events_snapshot()

    def macro_set_events(self, events, expected_rev=None, expected_slot=None,
                         persist=False):
        """Replace the buffer with a JS-edited list. With `persist`, also
        write it back to the loaded slot in the same command (no
        set-then-save race). Refuses with reason 'stale' when the buffer
        changed since the editor fetched `expected_rev` / `expected_slot`,
        'empty' when persisting would leave a loaded slot with no events
        (JS offers Clear slot instead), or recording / playing / busy.
        Extras: rev, count, dropped, applied, saved_slot, loaded."""
        return dict(self._macro.apply_events(
            events, expected_rev=expected_rev, expected_slot=expected_slot,
            persist=bool(persist)))

    def macro_set_editor_dirty(self, dirty, seq=None, page_gen=None):
        """The events editor gained / lost local edits that are not in
        the macro buffer yet (JS mirrors its flag on every flip, cleared
        on save, discard and hide, and re-asserts it every few seconds).
        Only the titlebar close can ask about them in the page; Alt+F4 /
        taskbar Close reach allow_close without JS, so it reads this
        mirror.

        `seq` (optional, increasing per update): pywebview runs every
        bridge call on its own thread, so an older "false" can land after
        a newer "true". An update whose seq is not above the last applied
        one is dropped (returns False). Without `seq` (older page) the
        update always applies.

        `page_gen` (from get_initial_state): updates from an older page
        load are dropped outright, and `seq` is then a plain per-page
        counter compared only within the current page."""
        with self._lock:
            if seq is not None:
                try:
                    seq = int(seq)
                except (TypeError, ValueError):
                    seq = None
            if page_gen is not None:
                try:
                    page_gen = int(page_gen)
                except (TypeError, ValueError):
                    page_gen = None
            if page_gen is not None:
                current_gen = getattr(self, '_editor_page_gen', 0)
                if page_gen < current_gen:
                    return False
                if page_gen > current_gen:
                    self._editor_page_gen = page_gen
                    self._editor_dirty_seq = 0
            if seq is not None:
                if seq <= getattr(self, '_editor_dirty_seq', 0):
                    return False
                self._editor_dirty_seq = seq
            self._editor_dirty = bool(dirty)
        return True

    def macro_get_active_config(self):
        with self._lock:
            return {
                'active_macros': [
                    dict(x) for x in self._settings.get(
                        'macro_active_macros', [])
                ],
                'conflict_policy': self._settings.get(
                    'macro_conflict_policy', MACRO_CONFLICT_IGNORE),
            }

    def macro_set_active_slot(self, pos, slot):
        idx = self._active_macro_index(pos)
        if idx is None:
            return {'ok': False, 'reason': 'bad_index'}
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return {'ok': False, 'reason': 'bad_slot'}
        if slot != 0 and not (1 <= slot <= MACRO_SLOT_COUNT):
            return {'ok': False, 'reason': 'bad_slot'}
        if slot and not (macros_dir() / f'macro_{slot}.json').exists():
            msg = f'Slot {slot} is empty - save a macro there first.'
            self._macro.set_error(msg)
            return {'ok': False, 'reason': 'empty_slot', 'message': msg}
        duplicate_of = None
        with self._lock:
            active = self._active_macros_locked()
            if slot:
                for i, item in enumerate(active):
                    if i != idx and item.get('slot') == slot:
                        duplicate_of = i
                        break
            if duplicate_of is None:
                active[idx]['slot'] = slot
                self._settings['macro_active_macros'] = active
                save_settings(self._settings)
        if duplicate_of is not None:
            # Used to snap the dropdown back with no explanation.
            msg = (f'Slot {slot} is already assigned to active macro '
                   f'{self._active_letter(duplicate_of)}.')
            self._macro.set_error(msg)
            return {'ok': False, 'reason': 'duplicate_slot', 'message': msg}
        self._install_keybinds()
        self._push_active_status()
        self._macro.clear_error()
        return {'ok': True}

    def macro_set_active_hotkey(self, pos, payload):
        idx = self._active_macro_index(pos)
        if idx is None:
            return {'ok': False, 'reason': 'bad_index'}
        # Same rules as rebind (left click / unmappable keys refused),
        # checked before anything is saved.
        hotkey, reason = validate_hotkey(
            f'macro_active_{idx + 1}', js_event_to_hotkey(payload))
        if reason:
            return {'ok': False, 'reason': reason}
        action = f'macro_active_{idx + 1}'
        # Same transaction as rebind: install the PROPOSED hotkey, verify,
        # and only then commit + save, all under one _keybind_lock hold.
        # Only an entry with a slot is installed; `desired` (the snapshot
        # actually applied) says whether there was anything to verify.
        with self._keybind_lock:
            with self._lock:
                if self._hotkey_in_use_locked(hotkey,
                                              current_active_index=idx):
                    return {'ok': False, 'reason': 'duplicate_hotkey'}
            desired = self._install_keybinds(active_overrides={idx: hotkey})
            if (action in desired
                    and self._keybinds.get_bindings().get(action) != hotkey):
                self._install_keybinds()    # exactly what settings say
                return {'ok': False, 'reason': 'install_failed'}
            # As in rebind: the captured press must not fire the binding.
            self._keybinds.latch_until_release(action)
            with self._lock:
                active = self._active_macros_locked()
                active[idx]['hotkey'] = hotkey
                self._settings['macro_active_macros'] = active
                save_settings(self._settings)
        self._push_active_status()
        return {'ok': True, 'hotkey': hotkey}

    def macro_clear_active(self, pos):
        idx = self._active_macro_index(pos)
        if idx is None:
            return {'ok': False, 'reason': 'bad_index'}
        with self._lock:
            active = self._active_macros_locked()
            active[idx] = {'slot': 0, 'hotkey': ''}
            self._settings['macro_active_macros'] = active
            save_settings(self._settings)
        self._install_keybinds()
        self._push_active_status()
        return {'ok': True}

    def macro_set_conflict_policy(self, policy):
        policy = (policy or '').strip().lower()
        if policy not in (MACRO_CONFLICT_IGNORE, MACRO_CONFLICT_INTERRUPT):
            return {'ok': False, 'reason': 'bad_policy'}
        with self._lock:
            self._settings['macro_conflict_policy'] = policy
            save_settings(self._settings)
        self._push_active_status()
        return {'ok': True}

    def macro_play_active(self, pos, force=False):
        idx = self._active_macro_index(pos)
        if idx is None:
            return {'ok': False, 'reason': 'bad_index'}
        with self._engine_lock:
            refusal = self._macro_claim_engine_locked(force)
            if refusal is not None:
                return refusal
            with self._lock:
                slot = self._active_macros_locked()[idx].get('slot') or 0
            if (slot and self._macro.state() != MacroController.STATE_RECORDING
                    and (macros_dir() / f'macro_{slot}.json').exists()):
                refusal = self._prepare_and_focus_for_macro()
                if refusal is not None:
                    return refusal
            return self._play_active_macro(pos, require_foreground=True)

    def _prepare_and_focus_for_macro(self):
        """UI-button playback prelude. Build the input backend FIRST (a
        first Arduino connect takes ~2 s), THEN switch to Genshin and
        verify it — so the user can't alt-tab away between the focus check
        and playback. MacroController re-checks the foreground once more
        right before the worker starts (require_foreground). None on
        success, else a refusal dict (also published as macro_error)."""
        prep = self._macro.prepare_backend()
        if not prep:
            return self._explain_backend_failure(prep)
        return self._focus_game_for_macro()

    def _macro_claim_engine_locked(self, force):
        """Caller holds _engine_lock. None when the bot is idle (or was
        force-stopped AND its worker really exited); otherwise a refusal
        dict for JS: 'bot_running' (JS confirms, re-calls with force) or
        'bot_stop_timeout' (the forced stop didn't finish — never start a
        macro on a backend the bot may still be writing to), or 'closing'
        (detach() began — a start queued behind teardown must not install
        recording hooks or spawn a playback worker)."""
        if self._macro_closing():
            return {'ok': False, 'reason': 'closing'}
        if not self._bot.is_running():
            return None
        if not force:
            return {'ok': False, 'reason': 'bot_running'}
        if self._stop_bot_blocking() and not self._bot.is_running():
            return None
        msg = "The bot didn't stop in time - macro not started. Try again."
        self._macro.set_error(msg)
        return {'ok': False, 'reason': 'bot_stop_timeout', 'message': msg}

    def _macro_closing(self):
        """True once detach() started tearing the app down. Macro starts
        (buttons and hotkeys) refuse from then on; MacroController's own
        shutdown latch covers anything already past this check."""
        if getattr(self, '_tearing_down', False):
            print('[macro] start refused - the app is closing')
            return True
        return False

    def _focus_game_for_macro(self):
        """Bring Genshin to the front before UI-button playback and
        verify it got there. None on success; otherwise a refusal dict
        (also published as macro_error) — playback must never start
        into the UI or whatever other window is in front."""
        hwnd, reason = focus_game_window_ex(timeout=1.0,
                                            stop_evt=self._drain_stop)
        if hwnd:
            # Brief settle covers the Win11 window-switch animation;
            # without it the first event in a t≈0 macro can land on the
            # UI window.
            time.sleep(0.05)
            return None
        if reason == FOCUS_GAME_NOT_FOUND:
            msg = "Genshin isn't running - start the game, then press Play."
        elif reason == FOCUS_GAME_STOPPED:
            return {'ok': False, 'reason': reason}
        else:
            msg = ("Couldn't bring Genshin to the front - click into the "
                   "game and use the macro hotkey instead.")
        self._macro.set_error(msg)
        return {'ok': False, 'reason': reason, 'message': msg}

    @staticmethod
    def _active_letter(idx):
        """Row label the UI shows for active macro index idx (A, B, C)."""
        return chr(ord('A') + int(idx))

    def start_stop_force(self):
        """Same as start_stop but stops a running macro first. Called by
        JS after the user confirms the cross-mode prompt. Returns
        {'ok': bool}; {'ok': False, 'reason': 'busy'} when the macro did
        not actually stop within its bounded wait — the bot is never
        started alongside a still-running macro. {'ok': False, 'reason':
        'closing'} once the window is closing."""
        if self._tearing_down:
            return {'ok': False, 'reason': 'closing'}
        with self._engine_lock:
            if self._tearing_down:
                return {'ok': False, 'reason': 'closing'}
            if self._bot.is_running():
                # Raced with another Start — never turn a force-START
                # into a Stop.
                return {'ok': True, 'reason': 'already_running'}
            if self._macro.is_busy():
                self._stop_macro_blocking()
                if self._macro.is_busy():
                    print('[ui] macro did not stop in time — bot not '
                          'started; try again')
                    return {'ok': False, 'reason': 'busy'}
            return {'ok': bool(self._start_bot_locked())}

    def _stop_bot_blocking(self):
        """Synchronously stop the bot worker — bounded wait so a stuck
        worker doesn't freeze the UI thread. 5s matches BotController's
        own shutdown join timeout. Returns True when the bot is no longer
        running afterwards (callers must not take over the backend
        otherwise)."""
        if not self._bot.is_running():
            return True
        self._bot.stop()
        t = self._bot._thread
        if t is not None:
            t.join(timeout=5.0)
        return not self._bot.is_running()

    def _stop_macro_blocking(self):
        """Stop recording / playback synchronously (playback join bounded
        at 2 s). Returns True when the macro is idle afterwards."""
        if self._macro.state() == MacroController.STATE_RECORDING:
            self._macro.stop_record()
        elif self._macro.state() == MacroController.STATE_PLAYING:
            self._macro.stop_play()
            t = self._macro._play_thread
            if t is not None:
                t.join(timeout=2.0)
        return not self._macro.is_busy()

    def minimize(self):
        if self._window is not None:
            try:
                self._window.minimize()
            except Exception as e:
                print(f"[ui] minimize failed: {e}")
        return True

    def toggle_maximize(self):
        """Maximize to the monitor's WORK AREA (taskbar stays visible), or
        restore the previous rect. Returns {'maximized': bool} so the
        titlebar can swap its glyph.

        Not window.toggle_fullscreen() (covers the taskbar — that was the
        bug) and not window.maximize() either: for a frameless WinForms
        form (FormBorderStyle.None) DefWindowProc sizes the maximized
        window to the full monitor, i.e. it covers the taskbar too. So
        snap the rect ourselves with plain Win32 calls."""
        hwnd = self._ui_hwnd()
        if not hwnd:
            return {'maximized': self._restore_rect is not None}
        try:
            W = ctypes.wintypes
            # Private WinDLL instance: the argtypes below must not leak
            # onto the shared ctypes.windll.user32 function objects that
            # other modules (and pywebview) call with their own args.
            user32 = ctypes.WinDLL('user32')

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [('cbSize', W.DWORD), ('rcMonitor', W.RECT),
                            ('rcWork', W.RECT), ('dwFlags', W.DWORD)]

            user32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
            user32.GetWindowRect.restype = W.BOOL
            user32.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
            user32.MonitorFromWindow.restype = W.HMONITOR
            user32.GetMonitorInfoW.argtypes = [
                W.HMONITOR, ctypes.POINTER(_MONITORINFO)]
            user32.GetMonitorInfoW.restype = W.BOOL
            user32.SetWindowPos.argtypes = [
                W.HWND, W.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = W.BOOL
            SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
            MONITOR_DEFAULTTONEAREST = 2

            if self._restore_rect is None:
                cur = W.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(cur)):
                    return {'maximized': False}
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    return {'maximized': False}
                work = mi.rcWork
                if user32.SetWindowPos(
                        hwnd, None, work.left, work.top,
                        work.right - work.left, work.bottom - work.top,
                        SWP_NOZORDER | SWP_NOACTIVATE):
                    self._restore_rect = (cur.left, cur.top,
                                          cur.right - cur.left,
                                          cur.bottom - cur.top)
            else:
                x, y, w, h = self._restore_rect
                self._restore_rect = None
                user32.SetWindowPos(hwnd, None, x, y, w, h,
                                    SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception as e:
            print(f"[ui] maximize failed: {e}")
        return {'maximized': self._restore_rect is not None}

    def _hide_window_for_teardown(self):
        """SW_HIDE the UI window (see detach). Uses only the cached HWND
        — no lookups during teardown. Safe if the window is already gone
        (`closed` fallback path)."""
        hwnd = getattr(self, '_hwnd_cache', 0)
        if not hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            if user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass

    def close(self, confirmed=False):
        """Titlebar close. `confirmed=True` means the page already asked
        "discard the unsaved macro?" in its own dialog and the user agreed,
        so the native prompt in the `closing` handler (allow_close) is
        skipped. With the default False — old JS, or any caller that
        didn't ask — the closing handler asks natively when there is
        unsaved macro work. Returns True once the window is closing /
        closed, False if the user cancelled at the native prompt."""
        if confirmed:
            self._close_confirmed = True
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception as e:
                if confirmed:
                    self._close_confirmed = False
                print(f"[ui] close failed: {e}")
                return False
            # destroy() returns after FormClosing ran: detached = closed,
            # not detached = a closing handler cancelled it.
            return bool(self._detached)
        return True

    def allow_close(self):
        """`closing`-event gate. Runs on the WinForms UI thread BEFORE
        detach()'s hide-then-teardown — every close path goes through it
        (titlebar, Alt+F4, taskbar "Close window"). True = let the window
        close. False = the user chose to keep an unsaved / in-progress
        macro; the handler then returns False, which cancels the close
        (pywebview 6.2.1 winforms on_closing: any `closing` handler
        returning False sets FormClosingEventArgs.Cancel)."""
        if self._detached or getattr(self, '_close_confirmed', False):
            return True
        if not self._macro_has_unsaved_work():
            return True
        if self._ask_discard_unsaved_macro():
            self._close_confirmed = True
            return True
        print('[ui] close cancelled — unsaved macro kept')
        return False

    def _macro_has_unsaved_work(self):
        """Recording in progress, the macro buffer is dirty (unsaved
        changes), or the events editor holds unsaved edits — the same
        rule the page's in-app close confirm uses."""
        return (self._macro_buffer_unsaved()
                or bool(getattr(self, '_editor_dirty', False)))

    def _macro_buffer_unsaved(self):
        """Recording in progress, or the macro buffer is dirty."""
        try:
            if self._macro.state() == MacroController.STATE_RECORDING:
                return True
        except Exception:
            pass
        try:
            return bool(self._macro._engine.is_dirty())
        except Exception:
            pass
        try:
            return bool(self._macro.snapshot().get('macro_dirty'))
        except Exception:
            return False

    def _ask_discard_unsaved_macro(self):
        """Native Yes/No MessageBoxW owned by the UI window ("No" is the
        default button). True = discard and close. If the box can't be
        shown at all, allow the close rather than trap the user in a
        window that can't be closed."""
        try:
            recording = (self._macro.state()
                         == MacroController.STATE_RECORDING)
        except Exception:
            recording = False
        buffer_unsaved = recording or self._macro_buffer_unsaved()
        details = []
        if recording:
            details.append('A macro recording is still in progress.')
        elif buffer_unsaved:
            details.append('The current macro has unsaved changes — save '
                           'it to a slot first to keep it.')
        if getattr(self, '_editor_dirty', False):
            details.append('The events editor has edits that were not '
                           'saved — press Save changes in the editor to '
                           'keep them.')
        head = ('Discard the unsaved macro and close?' if buffer_unsaved
                else 'Discard unsaved macro changes and close?')
        text = head + ('\n\n' + '\n'.join(details) if details else '')
        MB_YESNO, MB_ICONWARNING, MB_DEFBUTTON2 = 0x4, 0x30, 0x100
        MB_SETFOREGROUND, MB_TOPMOST = 0x10000, 0x40000
        IDYES = 6
        try:
            owner = getattr(self, '_hwnd_cache', 0) or self._ui_hwnd()
            W = ctypes.wintypes
            # Private WinDLL: keep these argtypes off the shared user32.
            user32 = ctypes.WinDLL('user32')
            user32.MessageBoxW.argtypes = [W.HWND, W.LPCWSTR, W.LPCWSTR,
                                           ctypes.c_uint]
            user32.MessageBoxW.restype = ctypes.c_int
            rc = user32.MessageBoxW(
                owner or None, text, UI_WINDOW_TITLE,
                MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2
                | MB_SETFOREGROUND | MB_TOPMOST)
        except Exception as e:
            print(f"[ui] close prompt failed: {e}")
            return True
        if rc == 0:
            return True
        return rc == IDYES

    # ---- internal ----

    def _active_macro_index(self, pos):
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            return None
        if 1 <= pos <= ACTIVE_MACRO_COUNT:
            return pos - 1
        return None

    def _active_macros_locked(self):
        active = self._settings.get('macro_active_macros')
        if not isinstance(active, list):
            active = []
        out = [{'slot': 0, 'hotkey': ''} for _ in range(ACTIVE_MACRO_COUNT)]
        for i, item in enumerate(active[:ACTIVE_MACRO_COUNT]):
            if not isinstance(item, dict):
                continue
            try:
                slot = int(item.get('slot') or 0)
            except (TypeError, ValueError):
                slot = 0
            if not (1 <= slot <= MACRO_SLOT_COUNT):
                slot = 0
            hotkey = str(item.get('hotkey') or '').strip().lower()
            out[i] = {'slot': slot, 'hotkey': hotkey}
        return out

    def _hotkey_in_use_locked(self, hotkey, current_action=None,
                              current_active_index=None):
        # Compare canonical forms so 'escape' == 'esc' and
        # 'shift+ctrl+f8' == 'ctrl+shift+f8'.
        hotkey = normalize_hotkey(hotkey)
        if not hotkey:
            return False
        for action, binding in self._settings.get('keybinds', {}).items():
            if action == current_action:
                continue
            if normalize_hotkey(binding) == hotkey:
                return True
        for i, item in enumerate(self._active_macros_locked()):
            if i == current_active_index:
                continue
            if normalize_hotkey(item.get('hotkey')) == hotkey:
                return True
        return False

    def _push_active_status(self):
        with self._lock:
            active = [dict(x) for x in self._active_macros_locked()]
            policy = self._settings.get(
                'macro_conflict_policy', MACRO_CONFLICT_IGNORE)
        self._enqueue_status({
            'macro_active_macros': active,
            'macro_conflict_policy': policy,
        })

    def _clear_active_slot_refs(self, slot):
        """Unassign `slot` from every active macro (its hotkey stays
        bound). Returns the affected row letters, e.g. ['B']."""
        letters = []
        with self._lock:
            active = self._active_macros_locked()
            for i, item in enumerate(active):
                if item.get('slot') == slot:
                    item['slot'] = 0
                    letters.append(self._active_letter(i))
            if letters:
                self._settings['macro_active_macros'] = active
                save_settings(self._settings)
        if letters:
            print(f"[macro] slot {slot} unassigned from active macro "
                  f"{', '.join(letters)}")
            self._install_keybinds()
            self._push_active_status()
        return letters

    def _play_active_macro(self, pos, require_foreground=False):
        idx = self._active_macro_index(pos)
        if idx is None:
            return {'ok': False, 'reason': 'bad_index'}
        with self._lock:
            active = self._active_macros_locked()
            item = active[idx]
            slot = item.get('slot') or 0
            policy = self._settings.get(
                'macro_conflict_policy', MACRO_CONFLICT_IGNORE)
        if not slot:
            return {'ok': False, 'reason': 'inactive'}
        return self._explain_backend_failure(
            self._macro.play_slot(slot, conflict_policy=policy,
                                  require_foreground=require_foreground))

    def _record_binding(self):
        with self._lock:
            return self._settings.get('keybinds', {}).get(ACTION_MACRO_RECORD)

    @staticmethod
    def _binding_label(binding):
        """Human label for a stored binding, for in-game log / hint text
        ('ctrl+y' -> 'Ctrl+Y', 'mouse:x' -> 'Mouse 4')."""
        b = str(binding or '').strip().lower()
        if not b:
            return 'the hotkey'
        if b.startswith('mouse:'):
            return {'left': 'Left Click', 'right': 'Right Click',
                    'middle': 'Middle Click', 'x': 'Mouse 4',
                    'x2': 'Mouse 5'}.get(b[6:], b)
        return '+'.join(p.upper() if len(p) == 1 else p.capitalize()
                        for p in b.split('+'))

    def _explain_backend_failure(self, res):
        """MacroController only knows "no backend"; _get_arduino left the
        real reason (port not found, ...) in _backend_error. Put it in the
        user-facing macro_error."""
        res = dict(res)
        detail = getattr(self, '_backend_error', '')
        if res.get('reason') == 'no_backend' and detail:
            msg = f'Input backend unavailable: {detail}'
            self._macro.set_error(msg)
            res['message'] = msg
        return res

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

    def _install_keybinds(self, keybind_overrides=None, active_overrides=None):
        # Transactional (KeybindManager.apply): unchanged bindings stay
        # installed, changed ones are installed before the old hook is
        # removed, and only actions that are no longer wanted get cleared.
        # The old clear_all()-then-set() lost Play/Stop entirely when the
        # mouse hook failed to install, even on the rollback reinstall.
        #
        # Serialized by _keybind_lock, and the desired map is built INSIDE
        # it from the settings as they are at that moment: an installer
        # that built its snapshot first and applied it later could put an
        # older binding back over a newer rebind (BU11). rebind /
        # macro_set_active_hotkey hold the same (reentrant) lock across
        # their whole install -> verify -> commit, passing the proposed
        # binding as an override. Returns the desired map it applied.
        with self._keybind_lock:
            return self._install_keybinds_locked(keybind_overrides,
                                                 active_overrides)

    def _install_keybinds_locked(self, keybind_overrides, active_overrides):
        with self._lock:
            kb = dict(self._settings['keybinds'])
            actives = [dict(x) if isinstance(x, dict) else {}
                       for x in self._settings.get('macro_active_macros', [])]
        kb.update(keybind_overrides or {})
        for idx, hk in (active_overrides or {}).items():
            if 0 <= idx < len(actives):
                actives[idx]['hotkey'] = hk
        desired = {
            # Bot hotkeys — fire when Genshin or UI is focused.
            ACTION_START_STOP: (kb.get(ACTION_START_STOP),
                                self._hotkey_start_stop, 'app'),
            ACTION_PAUSE: (kb.get(ACTION_PAUSE), self._hotkey_pause, 'app'),
            ACTION_DEBUG: (kb.get(ACTION_DEBUG), self._hotkey_debug, 'app'),
            # Macro hotkeys — strict Genshin-only gate (per spec). Won't
            # fire from the UI window itself; user clicks the buttons there.
            ACTION_MACRO_RECORD: (kb.get(ACTION_MACRO_RECORD),
                                  self._hotkey_macro_record, 'game'),
            ACTION_MACRO_PLAY: (kb.get(ACTION_MACRO_PLAY),
                                self._hotkey_macro_play, 'game'),
            ACTION_MACRO_STOP: (kb.get(ACTION_MACRO_STOP),
                                self._hotkey_macro_stop, 'game'),
            ACTION_MACRO_SAVE: (kb.get(ACTION_MACRO_SAVE),
                                self._hotkey_macro_save, 'game'),
            ACTION_MACRO_LOAD: (kb.get(ACTION_MACRO_LOAD),
                                self._hotkey_macro_load, 'game'),
        }
        for pos, item in enumerate(actives, start=1):
            if pos > ACTIVE_MACRO_COUNT:
                break
            slot = item.get('slot') or 0
            hotkey = item.get('hotkey') or ''
            if slot and hotkey:
                desired[f'macro_active_{pos}'] = (
                    hotkey,
                    lambda pos=pos: self._hotkey_macro_active(pos),
                    'game')
        self._keybinds.apply(desired)
        # Print what is actually bound. "which button does what" has been
        # guessed at twice; the log should just say.
        try:
            for act, hk in sorted(self._keybinds.get_bindings().items()):
                print(f"[ui] bound {act:16} -> {hk!r}")
        except Exception:
            pass
        return desired

    # Hotkey callbacks run on KeybindManager's hotkey-dispatch worker
    # thread (never on the keyboard/mouse listener thread).
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
        # Same path as the JS toggle so _opts['debug'] stays in sync.
        self.toggle_debug()

    # Macro hotkeys can't pop a confirmation dialog (no JS in this
    # thread), so cross-mode conflicts are refused with a log line. The
    # user can still trigger via UI buttons, which DO confirm.
    #
    # Starts (record / play / active) hold _engine_lock across the bot
    # check + start so a bot Start can't slip in between. The record
    # hotkey's own binding is passed through as `chord` so a modifier
    # chord (ctrl+y) never leaks into the recording at either end.
    #
    # Record / Load over an unsaved buffer need a second press within
    # MacroController.DISCARD_CONFIRM_S (confirm_discard_hotkey) — the
    # first press only arms and says so. Play hotkeys pass
    # require_foreground so the game is re-verified as the last step
    # before playback, after any wait for _engine_lock or the backend.
    def _hotkey_macro_record(self):
        self._log_hotkey('macro_record')
        chord = self._record_binding()
        if self._macro.state() == MacroController.STATE_RECORDING:
            self._macro.stop_record(chord=chord)
            return
        if self._macro_closing():
            return
        if not self._macro.confirm_discard_hotkey(
                'record', self._binding_label(chord)):
            return
        with self._engine_lock:
            if self._macro_closing():
                return
            if self._bot.is_running():
                print('[macro] bot is running — stop bot first '
                      '(or use the UI button to confirm)')
                return
            excluded = self._macro_excluded_bindings()
            self._macro.toggle_record(excluded_kb=excluded[0],
                                      excluded_mouse=excluded[1],
                                      chord=chord)

    def _hotkey_macro_play(self):
        self._log_hotkey('macro_play')
        with self._engine_lock:
            if self._macro_closing():
                return
            if self._bot.is_running():
                print('[macro] bot is running — stop bot first '
                      '(or use the UI button to confirm)')
                return
            self._explain_backend_failure(
                self._macro.play(require_foreground=True))

    def _hotkey_macro_active(self, pos):
        self._log_hotkey(f'macro_active_{pos}')
        with self._engine_lock:
            if self._macro_closing():
                return
            if self._bot.is_running():
                print('[macro] bot is running — stop bot first '
                      '(or use the UI button to confirm)')
                return
            self._play_active_macro(pos, require_foreground=True)

    def _hotkey_macro_stop(self):
        self._log_hotkey('macro_stop')
        self._macro.stop_play()

    def _hotkey_macro_save(self):
        self._log_hotkey('macro_save')
        if self._macro_closing():
            return
        self._macro.begin_save_pending()

    def _hotkey_macro_load(self):
        self._log_hotkey('macro_load')
        if self._macro_closing():
            return
        with self._lock:
            key = self._settings.get('keybinds', {}).get(ACTION_MACRO_LOAD)
        if not self._macro.confirm_discard_hotkey(
                'load', self._binding_label(key)):
            return
        self._macro.begin_load_pending()

    def _macro_excluded_bindings(self):
        """Split current keybindings into (keyboard_names, mouse_buttons).
        Used so the macro hooks don't capture the very keys that control
        them (record-toggle, save, etc.). Modifier-combos like 'ctrl+y'
        are skipped — pressing bare 'y' won't trigger them, so the user
        can still record 'y' if they want. The record chord's own presses
        at the start / end of a recording are filtered separately (the
        `chord` argument of MacroController.start_record / stop_record)."""
        kb_names = set()
        mouse_btns = set()
        for binding in self._keybinds.get_bindings().values():
            if not binding:
                continue
            b = binding.lower()
            if b.startswith('mouse:'):
                mouse_btns.add(b.split(':', 1)[1])
            elif '+' not in b:
                kb_names.add(b)
        return kb_names, mouse_btns

    def _get_arduino(self, cancel_evt=None):
        """Lazy-build the chosen input backend (Arduino HID or
        software SendInput, per `_settings['input_backend']`). Returns
        the controller or None on init failure (the user-facing reason is
        left in `self._backend_error`). The Arduino backend raises
        `ArduinoConnectionError` (older builds: `sys.exit`, so SystemExit
        is still trapped) — UI keeps running so the user can fix wiring
        or switch to the software backend. Failures don't latch: the next
        caller retries from scratch.

        A cached backend that reports `healthy == False` (e.g. the board
        was unplugged mid-session) is closed and rebuilt rather than
        handed out again. `cancel_evt` (the bot's stop event when the bot
        worker is the caller) aborts the Arduino's ~2 s connect settle.

        The setting is read under _arduino_lock — set_input_backend flips
        it under the same lock, so a build here can't produce a stale
        backend type mid-switch. Must never take _engine_lock: the bot
        worker calls this while other threads may hold _engine_lock and
        join that worker.

        Once teardown began (detach) it refuses outright — no new or
        cached backend is handed out, so nothing (bot or macro) can
        restart input after the final backend close."""
        if self._tearing_down:
            self._backend_error = 'the app is closing'
            return None
        with self._arduino_lock:
            if self._tearing_down:
                self._backend_error = 'the app is closing'
                return None
            ctrl = self._arduino
            if ctrl is not None:
                try:
                    healthy = bool(getattr(ctrl, 'healthy', True))
                except Exception:
                    healthy = False
                if healthy:
                    return ctrl
                print('[ui] input backend reported unhealthy — reconnecting')
                self._arduino = None
                try:
                    ctrl.close()
                except Exception as e:
                    print(f'[ui] unhealthy backend close error: {e}')
            with self._lock:
                backend = self._settings.get('input_backend', 'arduino')
            self._backend_error = ''
            try:
                self._arduino = make_input_backend(backend,
                                                   cancel_evt=cancel_evt)
            except ArduinoConnectionError as e:
                self._backend_error = (str(e) or
                                       f'{backend} backend failed to start')
                if cancel_evt is not None and cancel_evt.is_set():
                    print(f'[ui] {backend} backend connect cancelled')
                else:
                    print(f'[ui] {backend} backend unavailable: '
                          f'{self._backend_error}')
            except SystemExit:
                self._backend_error = (
                    f'{backend} backend failed to start — check the '
                    'connection, or switch Input method in the Main tab')
                print(f'[ui] {backend} backend init failed — '
                      'check connection and try again, or switch to '
                      'the other backend in the UI.')
            except Exception as e:
                self._backend_error = f'{backend} backend error: {e}'
                print(f'[ui] {backend} backend init error: {e}')
            return self._arduino

    def _bot_backend(self, cancel_evt=None):
        """BotController's controller_provider. Same shared instance as
        the macro tool gets, but a failure raises with the user-facing
        reason so BotController can show it as last_error ("Input backend
        unavailable — <reason>") instead of silently falling back."""
        ctrl = self._get_arduino(cancel_evt=cancel_evt)
        if ctrl is None:
            raise RuntimeError(self._backend_error or 'see Logs')
        return ctrl

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

    def _sync_debug_opts(self):
        """Fold the live debug-viz flag into _opts while the bot runs
        (closing the viz window clears the event without going through
        set_debug) and return what the UI shows: the live state while
        running, the preset for the next Start otherwise."""
        with self._lock:
            if self._bot.is_running():
                live = self._bot.debug_enabled
                if self._opts.get('debug') != live:
                    self._opts['debug'] = live
            return bool(self._opts.get('debug'))

    def _check_backend_health(self):
        """While a bot run is live, surface a backend that died mid-run
        (e.g. Arduino unplugged) as last_error — the detector keeps
        "playing" into a dead link otherwise. Called ~1 Hz from the drain;
        reading `healthy` is a non-blocking probe (see controller.py).
        Only fills an empty last_error, so it fires once per run (Start
        clears it) and never clobbers a more specific runner message."""
        if not self._bot.is_running() or self._bot.is_stopping():
            return
        ctrl = self._arduino
        if ctrl is None:
            return
        try:
            healthy = bool(getattr(ctrl, 'healthy', True))
        except Exception:
            healthy = False
        if healthy:
            return
        with self._lock:
            backend = self._settings.get('input_backend', 'arduino')
        msg = ('Arduino disconnected — press Stop, then Start to reconnect'
               if backend == 'arduino' else
               'Input backend stopped working — press Stop, then Start')
        if self._bot.report_error(msg):
            print(f'[ui] {msg}')

    def _read_lanes(self):
        """`lanes`: which of a s d j k l the detector holds down right now
        (6 bools, KEYS order). All False when the bot isn't running or the
        detector module has no active_lanes() (yet). Called every drain
        tick — one function call + a 6-element list, nothing more. Always
        a NEW list, so the drain's by-value change detection sees edits."""
        idle = [False] * _LANE_COUNT
        fn = getattr(_detector_mod, 'active_lanes', None)
        if fn is None or not self._bot.is_running():
            return idle
        try:
            lanes = fn()
        except Exception:
            return idle
        if lanes is None:
            return idle
        try:
            out = [bool(x) for x in list(lanes)[:_LANE_COUNT]]
        except Exception:
            return idle
        out.extend([False] * (_LANE_COUNT - len(out)))
        return out

    def _poll_game_window(self):
        """`game_window_found`: FindWindowW over the game titles. Cheap and
        non-blocking (no message is sent to the game), drain-polled every
        GAME_WINDOW_POLL_S."""
        if _find_game_window is None:
            return False
        try:
            return bool(_find_game_window())
        except Exception:
            return False

    def _drain_loop(self):
        """Single-thread drain: snapshot status + drain log queue every
        POLL_MS, push both to the page in one evaluate_js call. Decoupling
        here keeps the JS channel quiet under burst log writes. A tick
        only pushes when the status changed or there is log text, plus a
        STATUS_KEEPALIVE_S keep-alive so a push the page missed (not ready
        yet, reload) self-heals — idle ticks no longer pay a blocking
        evaluate_js."""
        period = POLL_MS / 1000.0
        next_tick = time.monotonic()
        last_status = None
        last_push = 0.0
        last_health_check = 0.0
        last_window_poll = 0.0
        game_window_found = False
        while not self._drain_stop.is_set():
            try:
                # Drain *all* queued log chunks each tick.
                chunks = []
                try:
                    while True:
                        chunks.append(self._log_queue.get_nowait())
                except queue.Empty:
                    pass
                if time.monotonic() - last_health_check >= 1.0:
                    last_health_check = time.monotonic()
                    self._check_backend_health()
                if time.monotonic() - last_window_poll >= GAME_WINDOW_POLL_S:
                    last_window_poll = time.monotonic()
                    game_window_found = self._poll_game_window()
                debug = self._sync_debug_opts()
                lanes = self._read_lanes()
                with self._status_lock:
                    self._status['lanes'] = lanes
                    self._status['game_window_found'] = game_window_found
                    status = dict(self._status)
                status['admin'] = is_admin()
                status['debug'] = debug
                log_text = ''.join(chunks) if chunks else ''
                now = time.monotonic()
                if (log_text or status != last_status
                        or now - last_push >= STATUS_KEEPALIVE_S):
                    if self._push(status, log_text):
                        last_status = status
                        last_push = now
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
        """One evaluate_js with status (+ log text). Returns True if the
        call went through, False if skipped / failed (drain retries)."""
        if self._window is None or self._stopping:
            return False
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
        # Mark in-flight BEFORE the last _stopping check; detach sets
        # _stopping BEFORE reading _push_in_flight. So either this call
        # sees the close and skips, or detach sees the call in flight and
        # doesn't wait on a drain parked on the (busy) UI thread.
        self._push_in_flight = True
        try:
            if self._stopping:
                return False
            self._window.evaluate_js(script)
        except Exception:
            # Window gone or page not ready yet — silent skip so the next
            # tick still runs.
            return False
        finally:
            self._push_in_flight = False
        return True


def _on_closed(api):
    api.detach()
    # Natural Python shutdown follows — the LL hooks were already
    # unhooked from `closing` (or here, if `closing` didn't fire).
    # WebView2 + pythonnet take 500-1500 ms to tear down but the
    # mouse no longer cares because its hook is already gone.


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
    # Per-window AppID + display name + icon resource. The shell already
    # finalized taskbar grouping under python.exe's implicit AppID by the
    # time `shown` fires, so stamping AppID alone isn't enough — the
    # explicit RelaunchDisplayNameResource property is what overrides
    # the right-click label without a re-group.
    if hwnd:
        _set_window_app_id(hwnd, APP_USER_MODEL_ID, icon_path)
    if hwnd and icon_path:
        _set_window_icon(hwnd, icon_path)
    # Force Windows to drop and re-add this window's taskbar button so it
    # picks up the freshly-stamped AppID for grouping. ShowWindow(hide)
    # removes the taskbar entry; ShowWindow(show) re-creates it, this
    # time reading our PKEY_AppUserModel_ID. Brief flicker but no
    # alternative — once the shell has cached a window's AppID, only a
    # taskbar-button recreate updates it.
    if hwnd:
        try:
            user32 = ctypes.windll.user32
            SW_HIDE = 0
            SW_SHOW = 5
            user32.ShowWindow(hwnd, SW_HIDE)
            user32.ShowWindow(hwnd, SW_SHOW)
        except Exception as e:
            try:
                sys.__stdout__.write(f"[ui] taskbar refresh failed: {e}\n")
                sys.__stdout__.flush()
            except Exception:
                pass
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
                    sys.__stdout__.write(f"[ui] also applying icon + AppID to native HWND 0x{alt:x}\n")
                    sys.__stdout__.flush()
                except Exception:
                    pass
                _set_window_app_id(alt, APP_USER_MODEL_ID, icon_path)
                _set_window_icon(alt, icon_path)
    except Exception as e:
        try:
            sys.__stdout__.write(f"[ui] alt-hwnd icon apply failed: {e}\n")
            sys.__stdout__.flush()
        except Exception:
            pass
    api._icon_applied = True


def _warm_input_stack():
    """Build the `keyboard` package's scan-code table off the hot path.

    Measured: the first `keyboard.press(...)` in a process costs ~114 ms
    while it enumerates the OS keyboard layout. `key_to_scan_codes`
    triggers the same build and sends no input, so calling it at startup
    means the first dash of the first macro is on time."""
    try:
        import keyboard as _kb
        for name in ('left shift', 'shift', 'a'):
            _kb.key_to_scan_codes(name)
        print('[ui] input stack warm')
    except Exception as e:
        print(f'[ui] input warmup skipped: {e}')


def main():
    # In a frozen build, the embedded UAC manifest already enforced
    # elevation on launch — we'll always be admin here. Dev launches
    # (`python ui.py`) get a warning if not admin so the user knows
    # in-game hotkeys won't fire.
    if not is_frozen():
        warn_if_not_admin()
    # Persist display name + icon for our AppID so the taskbar right-click
    # / hover label reads "Genshin Rhythm Bot". Then bind this process to
    # that AppID before any window is created so the first taskbar
    # registration uses our identity. Both still useful even with a
    # custom .exe — the per-window stamp in _on_shown is the authoritative
    # override but these set the floor.
    _register_app_id(str(ICON_PATH) if ICON_PATH.exists() else None)
    _set_app_user_model_id()

    if not INDEX_PATH.exists():
        raise SystemExit(f'[ui] missing UI assets: {INDEX_PATH}')

    api = BridgeApi()
    icon = str(ICON_PATH) if ICON_PATH.exists() else None
    # (The version banner is printed by attach_window, right after stdio
    # is redirected — printed here it only reached the console, which a
    # --windowed .exe doesn't have, so it never showed up in Logs.)

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
    # Pay the `keyboard` package's ~110 ms scan-code table build NOW, on a
    # background thread, instead of inside the first macro trigger. The
    # input backend is built lazily on first use, so without this the very
    # first hotkey press of a session carries the whole cost — visible in
    # the log as "[input] software backend ready" landing between the
    # press and the play. See mavuika-macros.md 4.28. Started after
    # attach_window so its log line reaches the Logs tab.
    threading.Thread(target=_warm_input_stack, daemon=True,
                     name='input-warmup').start()
    # Hook `closing` (fires before WebView2/Edge starts its own shutdown)
    # so we can release the keyboard / mouse subscribers + signal threads
    # to stop while the host browser is still alive. `closed` fires after
    # the OS has destroyed the window — by then WebView2 has been
    # tearing down for hundreds of ms, holding its own input hooks.
    # Doing cleanup early gives the LL hooks a chance to come down
    # before the slow part starts.
    #
    # First, though, the unsaved-macro gate: Alt+F4 / taskbar "Close
    # window" never pass through the page's in-app confirm, so ask
    # natively here (allow_close) BEFORE detach's hide-then-teardown.
    # Returning False from a `closing` handler cancels the close
    # (pywebview 6.2.1: winforms on_closing → closing.set() → any False
    # return sets args.Cancel); the lambda must pass the value through.
    def _safe_closing():
        try:
            if not api.allow_close():
                return False
        except Exception as e:
            print(f"[ui] close gate error: {e}")
        try:
            api.detach()
        except Exception as e:
            print(f"[ui] detach in closing: {e}")
        return None
    try:
        window.events.closing += lambda: _safe_closing()
    except Exception:
        # Older pywebview without `closing` — fall through to closed.
        pass
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
