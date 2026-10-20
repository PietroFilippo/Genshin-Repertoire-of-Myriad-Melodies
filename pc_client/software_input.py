# pc_client/software_input.py
"""
Software-only input backend. Drop-in replacement for
`ArduinoHIDController` for users who don't want to wire up an
Arduino Leonardo. Uses the `keyboard` and `mouse` packages, which
wrap Win32 `SendInput` (keyboard) and `mouse_event` (mouse) under
the hood — same APIs BetterGenshinImpact uses for its own rhythm
auto-player (`Simulation.SendInput.Keyboard.KeyDown`).

What works in software mode
---------------------------
- Rhythm minigame keys (`a s d j k l`). The minigame is not under
  Genshin's combat anti-cheat hook so synthetic keystrokes go
  through fine. Verified against BGI's `AutoMusicGameTask`.
- Album-mode menu navigation. `mouse_event` absolute moves +
  left-button down/up land clicks on every UI button the album
  runner needs.
- Macro replay of keys + mouse buttons (the macro recorder doesn't
  capture cursor positions today, so absolute-move-driven macros
  aren't a concern).

What does NOT work
------------------
- Synthetic input through Genshin's combat anti-cheat (`mhyprot`):
  in-game camera rotation, combat clicks, attacks. The Arduino
  backend is the only path that drives those because it's real
  USB HID hardware. The rhythm bot doesn't need it; if your macros
  do, stay on the Arduino backend.
- Macro playback that synthesizes a key bound to a UI hotkey
  re-fires that hotkey through the global keyboard listener (no
  way to mark synthetic events from this layer). The Arduino
  backend doesn't have this problem because its events come from
  outside the Python process. Workaround: don't bind macro hotkeys
  to keys the macro itself uses.
"""
import keyboard
import mouse


# Names that the `keyboard` package accepts as-is. Includes the
# subset we actually pass through from macro recordings + album
# clicks — the `keyboard` package is permissive for unknown names
# (`keyboard.press('foo')` silently no-ops) so this is mostly
# documentation.
_KB_NAMES = frozenset({
    'esc', 'enter', 'tab', 'backspace', 'delete', 'space',
    'up', 'down', 'left', 'right',
    'shift', 'ctrl', 'alt',
    'home', 'end', 'page up', 'page down', 'insert',
    'caps lock', 'num lock', 'scroll lock',
})


class SoftwareInputController:
    """Same surface as `ArduinoHIDController`. Methods are silent on
    success and print a `[sw-input]` line on failure so a busted
    backend doesn't crash a worker thread."""

    def __init__(self):
        # No serial / port / hardware to acquire. The `keyboard` +
        # `mouse` packages start their listener threads lazily on
        # first hook install elsewhere in the UI (KeybindManager,
        # MacroController) — nothing for us to spin up here.
        self._warm_keyboard_tables()
        print("[input] software backend ready (Win32 SendInput / mouse_event)")

    @staticmethod
    def _warm_keyboard_tables():
        """Pay the `keyboard` package's lazy scan-code table build now.

        Measured on this repo: the FIRST `keyboard.press(...)` in a
        process costs **114 ms** while it enumerates the OS keyboard
        layout; every call after it is <=0.11 ms. `mouse` has no such
        cost (~0.000 ms), so a macro's mouse events are on time while
        its first keyboard event is ~7 frames late.

        That lands in the worst possible place: in every Mavuika combo
        the first keyboard event is the first DASH, and a dash 114 ms
        off its mark misses the spin hit it was supposed to cancel on —
        which reads in-game as "she dashes and just stays idle", on the
        first run after launch only. See mavuika-macros.md 4.28.

        `key_to_scan_codes` triggers the same table build and sends
        nothing, so this is free to call at construction.
        """
        try:
            for name in ('left shift', 'shift', 'a'):
                keyboard.key_to_scan_codes(name)
        except Exception as e:                      # pragma: no cover
            print(f"[sw-input] keyboard warmup skipped: {e}")

    # ---- keyboard ----

    @staticmethod
    def _norm_key(key):
        return (key or '').lower()

    def key_down(self, key):
        try:
            keyboard.press(self._norm_key(key))
        except Exception as e:
            print(f"[sw-input] key_down {key!r} failed: {e}")

    def key_up(self, key):
        try:
            keyboard.release(self._norm_key(key))
        except Exception as e:
            print(f"[sw-input] key_up {key!r} failed: {e}")

    # ---- mouse buttons ----

    @staticmethod
    def _norm_btn(button):
        return (button or 'left').lower()

    def mouse_down(self, button):
        try:
            mouse.press(button=self._norm_btn(button))
        except Exception as e:
            print(f"[sw-input] mouse_down {button!r} failed: {e}")

    def mouse_up(self, button):
        try:
            mouse.release(button=self._norm_btn(button))
        except Exception as e:
            print(f"[sw-input] mouse_up {button!r} failed: {e}")

    # ---- mouse motion ----

    def mouse_move(self, dx, dy):
        """Relative cursor delta in screen pixels. Provided for API
        parity with the Arduino backend; album-mode prefers
        `move_to` for absolute positioning."""
        try:
            mouse.move(int(dx), int(dy), absolute=False)
        except Exception as e:
            print(f"[sw-input] mouse_move ({dx},{dy}) failed: {e}")

    def move_to(self, x, y, stop_evt=None, max_iters=None, tol=None):
        """Absolute move to (x, y). One shot — `mouse_event` with
        `MOUSEEVENTF_ABSOLUTE` jumps directly to the target pixel
        regardless of EPP / pointer-precision settings, so we don't
        need the iterative convergence the Arduino backend does.
        `max_iters` / `tol` accepted for signature parity with
        `ArduinoHIDController.move_to` and ignored.
        Returns True on dispatch, False if the caller's stop_evt
        was already set."""
        if stop_evt is not None and stop_evt.is_set():
            return False
        try:
            mouse.move(int(x), int(y), absolute=True)
            return True
        except Exception as e:
            print(f"[sw-input] move_to ({x},{y}) failed: {e}")
            return False

    # ---- lifecycle ----

    def close(self):
        """Belt-and-suspenders sweep — release any rhythm key / mouse
        button still held. Mirrors `ArduinoHIDController.close()` so
        an interrupted run doesn't leave a key stuck in-game."""
        for k in ('a', 's', 'd', 'j', 'k', 'l'):
            self.key_up(k)
        self.mouse_up('left')
        self.mouse_up('right')
        print("[input] software backend closed")
