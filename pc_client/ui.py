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

import webview

import ui_core
from config import (ALBUM_DIFFICULTY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_SONG_COUNT)
from ui_core import (ACTION_DEBUG, ACTION_PAUSE, ACTION_START_STOP,
                     BotController, KeybindManager, QueueWriter,
                     UI_WINDOW_TITLE, is_admin, is_frozen,
                     js_event_to_hotkey, load_settings, save_settings,
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
