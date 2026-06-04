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
                     MACRO_CONFLICT_INTERRUPT, MacroController, QueueWriter,
                     UI_WINDOW_TITLE, focus_game_window, is_admin,
                     is_frozen, js_event_to_hotkey, load_settings,
                     macros_dir, make_input_backend, save_settings,
                     warn_if_not_admin)


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
        # in AlbumRunner.
        self._opts = {
            'mode': 'standalone',
            'songs': ALBUM_SONG_COUNT,
            'difficulty': [ALBUM_DIFFICULTY],
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

        self._bot = BotController(self._enqueue_status,
                                  controller_provider=self._get_arduino)
        self._macro = MacroController(controller_provider=self._get_arduino,
                                      on_status=self._enqueue_status)
        self._keybinds = KeybindManager(self._ui_hwnd)
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
        self._install_keybinds()
        self._drain_thread = threading.Thread(
            target=self._drain_loop, daemon=True, name='ui-drain')
        self._drain_thread.start()

    def detach(self):
        """Tear down on window close. Restore stdio first so any teardown
        prints land on the original streams. Idempotent — `closing` and
        `closed` events both call this and only the first call does
        work."""
        if self._detached:
            return
        self._detached = True
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._stopping = True
        self._drain_stop.set()

        # Wait for the drain thread to actually exit before pywebview
        # gets to dispose WebView2. Otherwise the drain's next 50 ms tick
        # fires evaluate_js into a disposed control and pywebview logs
        # `[pywebview] Error occurred in script ... ObjectDisposedException`
        # (the throw itself is swallowed by _push's bare except, but the
        # log line is emitted from inside pywebview before that). One
        # tick is 50 ms; 500 ms is generous.
        if self._drain_thread is not None:
            try:
                self._drain_thread.join(timeout=0.5)
            except Exception:
                pass

        # FIRST: drop the actual Win32 LL hooks via the captured handles.
        # This is the move that kills the mouse-lag-on-close — once the
        # WH_MOUSE_LL hook is gone, system mouse events stop routing
        # through our process, so it doesn't matter how slow the rest
        # of teardown (WebView2, pythonnet, etc) is.
        _release_ll_hooks()

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

    # ---- JS-callable methods ----

    def get_initial_state(self):
        with self._lock:
            return {
                **self._opts,
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

    def start_stop(self):
        if self._bot.is_running():
            self._bot.stop()
            return True
        if self._macro.is_busy():
            # Hotkey path arrives here — JS path uses start_stop_force
            # after a confirm prompt. Refuse so we never run the bot
            # concurrently with a macro on the same Arduino.
            print('[ui] macro is active — stop it first '
                  '(or use the UI Start button to confirm)')
            return False
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

    def restart_in_mode(self, mode):
        """Live mode switch from the JS layer. If the bot is running,
        stops it and immediately starts a fresh worker in `mode`. If
        idle, just records the new mode (same as set_mode). Returns True
        if a worker is running in `mode` after the call."""
        if mode not in ('standalone', 'album'):
            return False
        with self._lock:
            self._opts['mode'] = mode
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

        if not self._bot.is_running():
            return True
        ok = self._bot.restart_in_mode(mode, opts)
        if not ok:
            print(f'[ui] restart_in_mode({mode}) failed')
        return bool(ok)

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
        """Accept a list of difficulty names (any non-empty subset of
        normal/hard/pro/legendary) — the JS multi-checkbox path. Also
        accepts back-compat shapes: 'all' (expanded to the full
        4-element list) or a single difficulty string. Empty / fully-
        invalid input is rejected so the user can't end up with no
        difficulty selected."""
        order = list(ALBUM_DIFFICULTY_COORDS)
        if isinstance(d, (list, tuple)):
            chosen = [x for x in d if isinstance(x, str)
                      and x in ALBUM_DIFFICULTY_COORDS]
            if not chosen:
                return False
            # Canonicalize ordering so [legendary, normal] and
            # [normal, legendary] produce the same playback sequence.
            seen = set(chosen)
            normalized = [name for name in order if name in seen]
        elif d == 'all':
            normalized = list(order)
        elif isinstance(d, str) and d in ALBUM_DIFFICULTY_COORDS:
            normalized = [d]
        else:
            return False
        with self._lock:
            self._opts['difficulty'] = normalized
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

    # ---- macro (called from the Macros tab) ----

    def macro_toggle_record(self, force=False):
        """Start or stop recording. If the bot is running, refuse unless
        `force` is set — JS prompts the user with window.confirm and
        re-calls with force=True."""
        if self._bot.is_running():
            if not force:
                return {'ok': False, 'reason': 'bot_running'}
            self._stop_bot_blocking()
        excluded_kb, excluded_mouse = self._macro_excluded_bindings()
        ok = self._macro.toggle_record(excluded_kb=excluded_kb,
                                       excluded_mouse=excluded_mouse)
        return {'ok': bool(ok)}

    def macro_play(self, force=False):
        if self._bot.is_running():
            if not force:
                return {'ok': False, 'reason': 'bot_running'}
            self._stop_bot_blocking()
        # UI-button entry — user is in the UI, not the game. Pull
        # Genshin to the foreground so HID keystrokes from playback
        # land in the game, not the UI / desktop. Brief sleep covers
        # the Win11 window-switch animation; without it the first
        # event in a t≈0 macro can land on the UI window.
        focus_game_window()
        time.sleep(0.05)
        return {'ok': bool(self._macro.play())}

    def macro_stop(self):
        return {'ok': bool(self._macro.stop_play())}

    def macro_save_slot(self, n, name=None):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {'ok': False, 'reason': 'bad_slot'}
        return {'ok': bool(self._macro.save_slot(n, name=name))}

    def macro_load_slot(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {'ok': False, 'reason': 'bad_slot'}
        return {'ok': bool(self._macro.load_slot(n))}

    def macro_clear_slot(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {'ok': False, 'reason': 'bad_slot'}
        ok = bool(self._macro.clear_slot(n))
        if ok:
            self._clear_active_slot_refs(n)
        return {'ok': ok}

    def macro_rename_slot(self, n, name):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {'ok': False, 'reason': 'bad_slot'}
        return {'ok': bool(self._macro.rename_slot(n, name))}

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
        with self._lock:
            current = self._settings.get('input_backend', 'arduino')
        if name == current:
            return {'ok': True, 'reason': 'unchanged'}
        if self._bot.is_running() or self._macro.is_busy():
            if not force:
                return {'ok': False, 'reason': 'busy'}
            self._stop_bot_blocking()
            self._stop_macro_blocking()
        with self._arduino_lock:
            ard = self._arduino
            self._arduino = None
        if ard is not None:
            try:
                ard.close()
            except Exception as e:
                print(f"[ui] backend close error during switch: {e}")
        with self._lock:
            self._settings['input_backend'] = name
            save_settings(self._settings)
        self._enqueue_status({'input_backend': name})
        print(f"[ui] input backend set to {name!r}")
        return {'ok': True}

    def macro_get_events(self):
        """Returns the buffer for the events editor. Each event is
        {time, device, key, event_type}."""
        return self._macro.get_events()

    def macro_set_events(self, events):
        """Replace the buffer with a JS-edited list. Returns ok=False
        when the macro is busy (recording / playing)."""
        return {'ok': bool(self._macro.set_events(events))}

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
        if slot != 0 and not (1 <= slot <= 9):
            return {'ok': False, 'reason': 'bad_slot'}
        if slot and not (macros_dir() / f'macro_{slot}.json').exists():
            return {'ok': False, 'reason': 'empty_slot'}
        with self._lock:
            active = self._active_macros_locked()
            if slot:
                for i, item in enumerate(active):
                    if i != idx and item.get('slot') == slot:
                        return {'ok': False, 'reason': 'duplicate_slot'}
            active[idx]['slot'] = slot
            self._settings['macro_active_macros'] = active
            save_settings(self._settings)
        self._install_keybinds()
        self._push_active_status()
        return {'ok': True}

    def macro_set_active_hotkey(self, pos, payload):
        idx = self._active_macro_index(pos)
        if idx is None:
            return {'ok': False, 'reason': 'bad_index'}
        hotkey = js_event_to_hotkey(payload)
        if not hotkey:
            return {'ok': False, 'reason': 'bad_hotkey'}
        with self._lock:
            fixed_hotkeys = {
                str(v).strip().lower()
                for v in self._settings.get('keybinds', {}).values()
                if v
            }
            if hotkey in fixed_hotkeys:
                return {'ok': False, 'reason': 'duplicate_hotkey'}
            active = self._active_macros_locked()
            for i, item in enumerate(active):
                if i != idx and item.get('hotkey') == hotkey:
                    return {'ok': False, 'reason': 'duplicate_hotkey'}
            active[idx]['hotkey'] = hotkey
            self._settings['macro_active_macros'] = active
            save_settings(self._settings)
        self._install_keybinds()
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
        if self._bot.is_running():
            if not force:
                return {'ok': False, 'reason': 'bot_running'}
            self._stop_bot_blocking()
        focus_game_window()
        time.sleep(0.05)
        return self._play_active_macro(pos)

    def start_stop_force(self):
        """Same as start_stop but stops a running macro first. Called by
        JS after the user confirms the cross-mode prompt."""
        if self._macro.is_busy():
            self._stop_macro_blocking()
        return self.start_stop()

    def _stop_bot_blocking(self):
        """Synchronously stop the bot worker — bounded wait so a stuck
        worker doesn't freeze the UI thread. 5s matches BotController's
        own shutdown join timeout."""
        if not self._bot.is_running():
            return
        self._bot.stop()
        t = self._bot._thread
        if t is not None:
            t.join(timeout=5.0)

    def _stop_macro_blocking(self):
        if self._macro.state() == MacroController.STATE_RECORDING:
            self._macro.stop_record()
        elif self._macro.state() == MacroController.STATE_PLAYING:
            self._macro.stop_play()
            t = self._macro._play_thread
            if t is not None:
                t.join(timeout=2.0)

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
            if not (1 <= slot <= 9):
                slot = 0
            hotkey = str(item.get('hotkey') or '').strip().lower()
            out[i] = {'slot': slot, 'hotkey': hotkey}
        return out

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
        changed = False
        with self._lock:
            active = self._active_macros_locked()
            for item in active:
                if item.get('slot') == slot:
                    item['slot'] = 0
                    changed = True
            if changed:
                self._settings['macro_active_macros'] = active
                save_settings(self._settings)
        if changed:
            self._install_keybinds()
            self._push_active_status()

    def _play_active_macro(self, pos):
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
        return {'ok': bool(self._macro.play_slot(
            slot, conflict_policy=policy))}

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
        # Bot hotkeys — fire when Genshin or UI is focused.
        self._keybinds.set(ACTION_START_STOP, kb.get(ACTION_START_STOP),
                           self._hotkey_start_stop, scope='app')
        self._keybinds.set(ACTION_PAUSE, kb.get(ACTION_PAUSE),
                           self._hotkey_pause, scope='app')
        self._keybinds.set(ACTION_DEBUG, kb.get(ACTION_DEBUG),
                           self._hotkey_debug, scope='app')
        # Macro hotkeys — strict Genshin-only gate (per spec). Won't
        # fire from the UI window itself; user clicks the buttons there.
        self._keybinds.set(ACTION_MACRO_RECORD, kb.get(ACTION_MACRO_RECORD),
                           self._hotkey_macro_record, scope='game')
        self._keybinds.set(ACTION_MACRO_PLAY, kb.get(ACTION_MACRO_PLAY),
                           self._hotkey_macro_play, scope='game')
        self._keybinds.set(ACTION_MACRO_STOP, kb.get(ACTION_MACRO_STOP),
                           self._hotkey_macro_stop, scope='game')
        self._keybinds.set(ACTION_MACRO_SAVE, kb.get(ACTION_MACRO_SAVE),
                           self._hotkey_macro_save, scope='game')
        self._keybinds.set(ACTION_MACRO_LOAD, kb.get(ACTION_MACRO_LOAD),
                           self._hotkey_macro_load, scope='game')
        for pos, item in enumerate(
                self._settings.get('macro_active_macros', []), start=1):
            if pos > ACTIVE_MACRO_COUNT:
                break
            slot = item.get('slot') if isinstance(item, dict) else 0
            hotkey = item.get('hotkey') if isinstance(item, dict) else ''
            if slot and hotkey:
                self._keybinds.set(
                    f'macro_active_{pos}', hotkey,
                    lambda pos=pos: self._hotkey_macro_active(pos),
                    scope='game')

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

    # Macro hotkeys can't pop a confirmation dialog (no JS in this
    # thread), so cross-mode conflicts are refused with a log line. The
    # user can still trigger via UI buttons, which DO confirm.
    def _hotkey_macro_record(self):
        self._log_hotkey('macro_record')
        if self._bot.is_running():
            print('[macro] bot is running — stop bot first '
                  '(or use the UI button to confirm)')
            return
        excluded = self._macro_excluded_bindings()
        self._macro.toggle_record(excluded_kb=excluded[0],
                                  excluded_mouse=excluded[1])

    def _hotkey_macro_play(self):
        self._log_hotkey('macro_play')
        if self._bot.is_running():
            print('[macro] bot is running — stop bot first '
                  '(or use the UI button to confirm)')
            return
        self._macro.play()

    def _hotkey_macro_active(self, pos):
        self._log_hotkey(f'macro_active_{pos}')
        if self._bot.is_running():
            print('[macro] bot is running — stop bot first '
                  '(or use the UI button to confirm)')
            return
        self._play_active_macro(pos)

    def _hotkey_macro_stop(self):
        self._log_hotkey('macro_stop')
        self._macro.stop_play()

    def _hotkey_macro_save(self):
        self._log_hotkey('macro_save')
        self._macro.begin_save_pending()

    def _hotkey_macro_load(self):
        self._log_hotkey('macro_load')
        self._macro.begin_load_pending()

    def _macro_excluded_bindings(self):
        """Split current keybindings into (keyboard_names, mouse_buttons).
        Used so the macro hooks don't capture the very keys that control
        them (record-toggle, save, etc.). Modifier-combos like 'ctrl+y'
        are skipped — pressing bare 'y' won't trigger them, so the user
        can still record 'y' if they want."""
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

    def _get_arduino(self):
        """Lazy-build the chosen input backend (Arduino HID or
        software SendInput, per `_settings['input_backend']`). Returns
        the controller or None on init failure. The Arduino backend
        calls `sys.exit` on port-not-found so we trap SystemExit too —
        UI keeps running so the user can fix wiring or switch to the
        software backend. Failures don't latch: the next caller retries
        from scratch."""
        with self._arduino_lock:
            if self._arduino is not None:
                return self._arduino
            with self._lock:
                backend = self._settings.get('input_backend', 'arduino')
            try:
                self._arduino = make_input_backend(backend)
            except SystemExit:
                print(f'[ui] {backend} backend init failed — '
                      'check connection and try again, or switch to '
                      'the other backend in the UI.')
            except Exception as e:
                print(f'[ui] {backend} backend init error: {e}')
            return self._arduino

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
    # Hook `closing` (fires before WebView2/Edge starts its own shutdown)
    # so we can release the keyboard / mouse subscribers + signal threads
    # to stop while the host browser is still alive. `closed` fires after
    # the OS has destroyed the window — by then WebView2 has been
    # tearing down for hundreds of ms, holding its own input hooks.
    # Doing cleanup early gives the LL hooks a chance to come down
    # before the slow part starts.
    def _safe_closing():
        try:
            api.detach()
        except Exception as e:
            print(f"[ui] detach in closing: {e}")
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
