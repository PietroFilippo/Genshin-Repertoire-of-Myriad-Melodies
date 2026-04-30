# pc_client/ui.py
"""
Tkinter front-end for the rhythm bot. Single window with mode selector
(Standalone / Album), difficulty / song-count / skip-canorus controls,
Start/Pause/Stop buttons, a real-time debug-viz toggle, and a status pane.
Hotkeys (configurable in-app) trigger the same actions and only fire while
Genshin Impact is the foreground window.

Run:
    python pc_client/ui.py

Existing CLI entrypoints (main.py, album.py) still work standalone — this
file is additive.
"""
import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import keyboard

import main as bot_main
from album import AlbumRunner
from config import (ALBUM_DIFFICULTY, ALBUM_DIFFICULTY_COORDS,
                    ALBUM_SONG_COUNT, GAME_WINDOW_TITLE, UI_KEYBINDS_DEFAULT)


SETTINGS_PATH = Path(__file__).parent / 'ui_settings.json'
UI_WINDOW_TITLE = 'Genshin Rhythm Bot'

# Marker arg: when present, skip the elevation check. Set on the elevated
# relaunch so a UAC failure loop is impossible.
_ELEVATION_MARKER = '--no-elevate'


# --- admin / UAC ------------------------------------------------------------
#
# Genshin Impact's anti-cheat (mhyprot) blocks low-level keyboard hooks
# coming from non-elevated processes. Without admin, the `keyboard`
# package's hotkeys silently drop while Genshin owns the foreground —
# that's why macro_tool.py prints a giant admin warning. The UI takes the
# stronger position and self-elevates via UAC on launch so the user
# doesn't have to remember to right-click → "Run as administrator".

def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _try_relaunch_as_admin():
    """Re-spawn this script under UAC. Returns True iff a new elevated
    process was launched (caller should exit). Returns False if the user
    declined the UAC prompt or ShellExecute failed for any reason —
    caller should continue without admin and warn the user."""
    script = os.path.abspath(sys.argv[0])
    script_dir = os.path.dirname(script)
    # Forward original args plus the marker so the elevated child skips
    # the elevation check and doesn't UAC-loop if something goes wrong.
    forwarded = [a for a in sys.argv[1:] if a != _ELEVATION_MARKER]
    params = ' '.join(f'"{a}"' for a in [script] + forwarded + [_ELEVATION_MARKER])
    SW_SHOWNORMAL = 1
    # lpDirectory pinned to the script's folder so the elevated process'
    # cwd matches the non-elevated launch (matters for any future
    # cwd-relative file I/O even though current code uses absolute paths).
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, 'runas', sys.executable, params, script_dir, SW_SHOWNORMAL)
    # ShellExecuteW returns an HINSTANCE > 32 on success; <=32 means
    # error (5 / SE_ERR_ACCESSDENIED is typical when the user declines
    # the UAC prompt).
    return int(rc) > 32


def _ensure_admin_or_warn():
    """Self-elevate on launch. If already admin, no-op. If elevation
    succeeds, the original process exits and the elevated one takes
    over. If the user declines UAC, show a Tk warning so the message
    surfaces even when the script was double-clicked (no console)."""
    if _is_admin():
        return
    if _ELEVATION_MARKER in sys.argv:
        # Elevated relaunch came back without admin — don't recurse.
        # Warn and continue; user will hit a non-functional in-game gate.
        print("[ui] elevation marker present but not admin — giving up.")
        return
    if _try_relaunch_as_admin():
        sys.exit(0)
    # User declined or ShellExecute failed. Surface a Tk dialog so the
    # warning is visible without a console.
    try:
        warn_root = tk.Tk()
        warn_root.withdraw()
        messagebox.showwarning(
            'Administrator required',
            "Couldn't elevate to administrator — Genshin's anti-cheat "
            "blocks hotkeys from non-admin processes, so F8 / F9 / F10 "
            "will not work while the game is focused.\n\n"
            "Close this window, right-click the launcher and choose "
            "\"Run as administrator\" to enable in-game hotkeys.\n\n"
            "The UI buttons (Start / Stop / Pause / Debug toggle) will "
            "still work normally.")
        warn_root.destroy()
    except Exception:
        pass

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
    if not SETTINGS_PATH.exists():
        return {'keybinds': dict(UI_KEYBINDS_DEFAULT)}
    try:
        with SETTINGS_PATH.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        kb = dict(UI_KEYBINDS_DEFAULT)
        kb.update(data.get('keybinds', {}))
        return {'keybinds': kb}
    except Exception as e:
        print(f"[ui] failed to load settings: {e} — using defaults")
        return {'keybinds': dict(UI_KEYBINDS_DEFAULT)}


def save_settings(settings):
    try:
        with SETTINGS_PATH.open('w', encoding='utf-8') as fh:
            json.dump(settings, fh, indent=2)
    except Exception as e:
        print(f"[ui] failed to save settings: {e}")


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


def is_allowed_foreground(ui_hwnd):
    """Hotkey gate: pass if the focused window is the Genshin client OR
    the bot UI itself. Title-based to avoid the per-keypress
    find_game_window log spam and to handle Genshin window-class
    quirks where hwnd identity didn't compare equal."""
    fg = _foreground_hwnd()
    if not fg:
        return False
    if ui_hwnd and fg == ui_hwnd:
        return True
    title = _window_title(fg)
    if title == UI_WINDOW_TITLE:
        return True
    return title in GAME_WINDOW_TITLE


# --- keybind manager --------------------------------------------------------

class KeybindManager:
    """Thin wrapper around `keyboard.add_hotkey` with foreground gating and
    rebind support. Each action has at most one active hook at a time."""

    def __init__(self, ui_hwnd_provider):
        # ui_hwnd_provider() returns the current UI HWND. Called per-key
        # so it stays correct if Tk re-creates its underlying window.
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


# --- bot orchestration ------------------------------------------------------

class BotController:
    """Owns the bot worker thread and the events it watches. Exposes
    start / pause / resume / stop / toggle_debug methods called from the
    Tk thread (or from hotkey callbacks dispatched on the keyboard
    package's listener thread)."""

    STATE_IDLE = 'idle'
    STATE_RUNNING = 'running'
    STATE_PAUSED = 'paused'

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
        self._stop_evt.set()
        self._pause_evt.clear()  # unblock any pause-waiters

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
                                        status_cb=self._emit)
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


# --- Tk app -----------------------------------------------------------------

class App:
    POLL_MS = 100  # status panel refresh

    def __init__(self, root):
        self.root = root
        root.title(UI_WINDOW_TITLE)
        root.geometry('420x540')
        root.resizable(False, False)
        root.protocol('WM_DELETE_WINDOW', self._on_close)

        self.settings = load_settings()
        self._status = {'state': BotController.STATE_IDLE, 'mode': '',
                        'song': '', 'fps': 0.0, 'debug': False}
        self._status_lock = threading.Lock()
        self._capture_action = None  # while != None, next key event rebinds

        self.bot = BotController(self._enqueue_status)
        # winfo_id() on Windows returns the underlying HWND, which is what
        # GetForegroundWindow returns. We capture it lazily because
        # winfo_id() must be called after the widget is realized — pass a
        # callable so the gate fetches it on each keypress.
        self.keybinds = KeybindManager(lambda: self._ui_hwnd())

        self._build_ui()
        self._install_keybinds()
        self.root.after(self.POLL_MS, self._poll_status)

    def _ui_hwnd(self):
        try:
            return int(self.root.winfo_id())
        except Exception:
            return 0

    # ----- UI layout -----

    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}

        # Mode selector
        mode_frame = ttk.LabelFrame(self.root, text='Mode')
        mode_frame.pack(fill='x', **pad)
        self.mode_var = tk.StringVar(value='standalone')
        ttk.Radiobutton(mode_frame, text='Standalone (manual song select)',
                        variable=self.mode_var, value='standalone',
                        command=self._on_mode_change).pack(anchor='w')
        ttk.Radiobutton(mode_frame, text='Album',
                        variable=self.mode_var, value='album',
                        command=self._on_mode_change).pack(anchor='w')

        # Album options (greyed out in standalone)
        self.album_frame = ttk.LabelFrame(self.root, text='Album options')
        self.album_frame.pack(fill='x', **pad)

        row = ttk.Frame(self.album_frame)
        row.pack(fill='x', pady=2)
        ttk.Label(row, text='Songs:').pack(side='left')
        self.songs_var = tk.IntVar(value=ALBUM_SONG_COUNT)
        self.songs_spin = ttk.Spinbox(row, from_=1, to=ALBUM_SONG_COUNT,
                                      textvariable=self.songs_var, width=5)
        self.songs_spin.pack(side='left', padx=4)
        ttk.Label(row, text=f'(each album has {ALBUM_SONG_COUNT} songs)').pack(side='left')

        row = ttk.Frame(self.album_frame)
        row.pack(fill='x', pady=2)
        ttk.Label(row, text='Difficulty:').pack(side='left')
        self.difficulty_var = tk.StringVar(value=ALBUM_DIFFICULTY)
        self.diff_box = ttk.Combobox(row, textvariable=self.difficulty_var,
                                     values=list(ALBUM_DIFFICULTY_COORDS),
                                     state='readonly', width=12)
        self.diff_box.pack(side='left', padx=4)

        self.replay_canorus_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.album_frame,
                        text='Replay songs already at Canorus',
                        variable=self.replay_canorus_var).pack(anchor='w', pady=2)

        # Debug checkbox — only clickable while bot is running. Hot-toggle
        # via the configured hotkey. _poll_status flips state=normal when
        # running and back to disabled on stop.
        self.debug_var = tk.BooleanVar(value=False)
        self.debug_checkbox = ttk.Checkbutton(
            self.root,
            text='Show debug visualization (toggle live with hotkey)',
            variable=self.debug_var,
            command=self._on_debug_checkbox,
            state='disabled')
        self.debug_checkbox.pack(anchor='w', **pad)

        # Action buttons
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', **pad)
        self.btn_start = ttk.Button(btn_frame, text='Start',
                                    command=self._on_start_stop)
        self.btn_start.pack(side='left', expand=True, fill='x', padx=2)
        self.btn_pause = ttk.Button(btn_frame, text='Pause',
                                    command=self._on_pause)
        self.btn_pause.pack(side='left', expand=True, fill='x', padx=2)

        # Keybinds
        kb_frame = ttk.LabelFrame(self.root, text='Hotkeys '
                                  '(only fire when Genshin is focused)')
        kb_frame.pack(fill='x', **pad)
        self.kb_labels = {}
        self.kb_buttons = {}
        for action in (ACTION_START_STOP, ACTION_PAUSE, ACTION_DEBUG):
            row = ttk.Frame(kb_frame)
            row.pack(fill='x', pady=2)
            ttk.Label(row, text=ACTION_LABELS[action] + ':',
                      width=18).pack(side='left')
            lbl = ttk.Label(row, text=self._kb(action),
                            relief='sunken', anchor='center', width=15)
            lbl.pack(side='left', padx=4)
            self.kb_labels[action] = lbl
            btn = ttk.Button(row, text='Rebind',
                             command=lambda a=action: self._begin_capture(a))
            btn.pack(side='left', padx=4)
            self.kb_buttons[action] = btn

        # Status panel
        st_frame = ttk.LabelFrame(self.root, text='Status')
        st_frame.pack(fill='both', expand=True, **pad)
        self.lbl_state = ttk.Label(st_frame, text='State: idle')
        self.lbl_state.pack(anchor='w')
        self.lbl_song = ttk.Label(st_frame, text='Song: —')
        self.lbl_song.pack(anchor='w')
        self.lbl_fps = ttk.Label(st_frame, text='Viz FPS: —')
        self.lbl_fps.pack(anchor='w')
        self.lbl_debug = ttk.Label(st_frame, text='Debug: off')
        self.lbl_debug.pack(anchor='w')
        admin_text = 'Admin: yes' if _is_admin() else \
            'Admin: NO — in-game hotkeys will not fire'
        admin_color = 'green' if _is_admin() else 'red'
        self.lbl_admin = ttk.Label(st_frame, text=admin_text,
                                   foreground=admin_color)
        self.lbl_admin.pack(anchor='w')
        self.lbl_capture = ttk.Label(st_frame, text='', foreground='blue')
        self.lbl_capture.pack(anchor='w')

        self._on_mode_change()  # set initial enable state

    def _kb(self, action):
        return self.settings['keybinds'].get(action, '') or '<unset>'

    # ----- UI handlers -----

    def _on_mode_change(self):
        is_album = self.mode_var.get() == 'album'
        for child in self.album_frame.winfo_children():
            self._set_state_recursive(child,
                                      'normal' if is_album else 'disabled')

    @staticmethod
    def _set_state_recursive(widget, state):
        try:
            # Combobox uses 'readonly' to mean "uneditable but selectable".
            # 'normal' would unlock free-text typing — not what we want.
            if isinstance(widget, ttk.Combobox) and state == 'normal':
                widget.configure(state='readonly')
            else:
                widget.configure(state=state)
        except tk.TclError:
            pass
        for c in widget.winfo_children():
            App._set_state_recursive(c, state)

    def _on_debug_checkbox(self):
        # If the bot is running, mirror the checkbox into a live toggle so
        # toggling here behaves the same as the hotkey.
        if self.bot.is_running():
            current = self._status.get('debug', False)
            if current != self.debug_var.get():
                self.bot.toggle_debug()

    def _on_start_stop(self):
        if self.bot.is_running():
            self.bot.stop()
            return
        opts = {
            'songs': int(self.songs_var.get()),
            'difficulty': self.difficulty_var.get(),
            'replay_canorus': bool(self.replay_canorus_var.get()),
            'debug': bool(self.debug_var.get()),
        }
        if not self.bot.start(self.mode_var.get(), opts):
            print('[ui] start failed (already running?)')

    def _on_pause(self):
        self.bot.pause_toggle()

    def _begin_capture(self, action):
        """Capture next keypress as the new hotkey for `action`. Cancels
        on Escape."""
        if self._capture_action is not None:
            return
        self._capture_action = action
        self.lbl_capture.configure(
            text=f'Press a key to bind {ACTION_LABELS[action]} '
                 f'(Esc to cancel)…')
        self.kb_buttons[action].configure(text='…')
        # Bind a top-level key listener; resolved on first non-modifier press.
        self.root.bind_all('<Key>', self._on_capture_key)

    def _on_capture_key(self, event):
        action = self._capture_action
        if action is None:
            return
        keysym = event.keysym
        if keysym == 'Escape':
            self._end_capture(restore=True)
            return
        # Skip modifier-only events (waiting for the actual key).
        if keysym in ('Shift_L', 'Shift_R', 'Control_L', 'Control_R',
                      'Alt_L', 'Alt_R', 'Meta_L', 'Meta_R',
                      'Super_L', 'Super_R'):
            return
        hotkey = self._tk_event_to_hotkey(event)
        if not hotkey:
            self._end_capture(restore=True)
            return
        # Save + rebind.
        self.settings['keybinds'][action] = hotkey
        save_settings(self.settings)
        self._install_keybinds()
        self._end_capture(restore=False)

    def _end_capture(self, restore):
        action = self._capture_action
        self._capture_action = None
        self.lbl_capture.configure(text='')
        try:
            self.root.unbind_all('<Key>')
        except tk.TclError:
            pass
        if action is not None:
            self.kb_buttons[action].configure(text='Rebind')
            self.kb_labels[action].configure(text=self._kb(action))

    @staticmethod
    def _tk_event_to_hotkey(event):
        """Translate a Tk Key event to a `keyboard` package hotkey string.
        Handles modifier state via event.state bitmask."""
        key = event.keysym.lower()
        # Tk function keys arrive as 'F8' etc. — lowercase already done.
        # Letter/digit keys: prefer event.char when alphanumeric.
        if len(key) == 1 and key.isalnum():
            pass  # key already minimal
        elif key.startswith('f') and key[1:].isdigit():
            pass  # f-keys
        # Modifier bits: 0x0001=Shift, 0x0004=Control, 0x20000=Alt
        parts = []
        state = event.state
        if state & 0x0004:
            parts.append('ctrl')
        if state & 0x20000:
            parts.append('alt')
        if state & 0x0001:
            parts.append('shift')
        parts.append(key)
        return '+'.join(parts)

    def _install_keybinds(self):
        self.keybinds.clear_all()
        kb = self.settings['keybinds']
        self.keybinds.set(ACTION_START_STOP, kb.get(ACTION_START_STOP),
                          self._hotkey_start_stop)
        self.keybinds.set(ACTION_PAUSE, kb.get(ACTION_PAUSE),
                          self._hotkey_pause)
        self.keybinds.set(ACTION_DEBUG, kb.get(ACTION_DEBUG),
                          self._hotkey_debug)
        # Refresh labels.
        for action, lbl in self.kb_labels.items():
            lbl.configure(text=self._kb(action))

    # Hotkey callbacks dispatch onto the Tk thread so widget updates from
    # the keyboard listener thread don't trip Tk's threading rules.
    def _hotkey_start_stop(self):
        self.root.after(0, self._on_start_stop)

    def _hotkey_pause(self):
        self.root.after(0, self._on_pause)

    def _hotkey_debug(self):
        self.root.after(0, self._toggle_debug_from_hotkey)

    def _toggle_debug_from_hotkey(self):
        # Mirror into the checkbox + delegate to the bot only if running;
        # otherwise just flip the start-state checkbox.
        if self.bot.is_running():
            self.bot.toggle_debug()
            # status update will refresh the checkbox via _poll_status
        else:
            self.debug_var.set(not self.debug_var.get())

    # ----- status panel polling -----

    def _enqueue_status(self, update):
        with self._status_lock:
            self._status.update(update)

    def _poll_status(self):
        with self._status_lock:
            snap = dict(self._status)

        self.lbl_state.configure(text=f"State: {snap.get('state', 'idle')}")
        self.lbl_song.configure(text=f"Song: {snap.get('song') or '—'}")
        fps = snap.get('fps', 0.0)
        self.lbl_fps.configure(
            text=f"Viz FPS: {fps:.1f}" if fps else 'Viz FPS: —')
        debug_on = bool(snap.get('debug', False))
        self.lbl_debug.configure(text=f"Debug: {'on' if debug_on else 'off'}")
        # Sync the checkbox (without recursing into the command callback).
        if self.debug_var.get() != debug_on:
            self.debug_var.set(debug_on)

        running = self.bot.is_running()
        self.btn_start.configure(text='Stop' if running else 'Start')
        self.btn_pause.configure(
            state='normal' if running else 'disabled',
            text='Resume' if snap.get('state') == BotController.STATE_PAUSED
                          else 'Pause')
        # Debug checkbox is only clickable while the bot is running.
        # When stopped, force it back off and disable so the visual state
        # never disagrees with the (always-off) bot debug_evt.
        self.debug_checkbox.configure(
            state='normal' if running else 'disabled')
        if not running and self.debug_var.get():
            self.debug_var.set(False)

        self.root.after(self.POLL_MS, self._poll_status)

    # ----- shutdown -----

    def _on_close(self):
        print('[ui] closing — stopping bot')
        self.keybinds.clear_all()
        self.bot.shutdown()
        self.root.destroy()


def main():
    # Must run before Tk's mainloop so the UAC handoff happens before any
    # work (keyboard hooks, controller open, etc.) is done. If we elevated,
    # this call exits the current (non-admin) process.
    _ensure_admin_or_warn()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
