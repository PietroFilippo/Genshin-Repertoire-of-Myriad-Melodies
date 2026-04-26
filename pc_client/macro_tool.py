import keyboard
import mouse
import time
import json
import threading
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import KEYS, MACRO_HOTKEYS as _DEFAULT_HOTKEYS
from controller import ArduinoHIDController

# Logical action names — single source of truth for what bindings the
# tool needs. ACTIONS (defined later) maps these to handler callables.
ACTION_NAMES = ('record', 'play', 'stop', 'save', 'load', 'exit')

# Runtime hotkey table. Starts as a copy of the defaults from config.py.
# Override file (macro_hotkeys.json) is layered on top at startup, and
# the in-tool config flow writes back to that file.
HOTKEYS = dict(_DEFAULT_HOTKEYS)

# === Macro state ===
recording = False
playing = False
macro_events = []
start_time = 0.0

arduino = None
_exit_event = threading.Event()

# Resolved from HOTKEYS in main() — used to filter macro control
# keys/buttons during recording so they are not captured as macro events.
_KB_HOTKEY_NAMES = set()
_MOUSE_HOTKEY_BUTTONS = set()

# === Slot save/load pending state ===
SLOT_TIMEOUT_S = 4.0
_pending_action = None
_slot_hotkey_handles = []
_slot_timer = None
_slot_lock = threading.Lock()


def _is_mouse(binding):
    return binding.lower().startswith('mouse:')


def _mouse_button(binding):
    return binding.split(':', 1)[1].lower()


_MOUSE_DISPLAY = {
    'x': 'Mouse 4', 'x2': 'Mouse 5',
    'left': 'Left Click', 'right': 'Right Click', 'middle': 'Middle Click',
}


def _display(binding):
    if _is_mouse(binding):
        return _MOUSE_DISPLAY.get(_mouse_button(binding), f'Mouse {_mouse_button(binding)}')
    return binding.upper()


def _slot_path(n):
    return os.path.join(os.path.dirname(__file__), f'macro_{n}.json')


def _legacy_path():
    return os.path.join(os.path.dirname(__file__), 'macro.json')


def _slot_exists(n):
    return os.path.exists(_slot_path(n))


def _list_slots():
    return [s for s in range(1, 10) if _slot_exists(s)]


# === Hotkey override file ===
def _overrides_path():
    return os.path.join(os.path.dirname(__file__), 'macro_hotkeys.json')


def _load_overrides():
    """Layer macro_hotkeys.json on top of the defaults in config.py."""
    path = _overrides_path()
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to load hotkey overrides: {e}")
        return
    if not isinstance(data, dict):
        return
    for action in ACTION_NAMES:
        v = data.get(action)
        if isinstance(v, str) and v:
            HOTKEYS[action] = v
    print(f"[MACRO] Loaded hotkey overrides from {os.path.basename(path)}")


def _save_overrides():
    path = _overrides_path()
    try:
        with open(path, 'w') as f:
            json.dump(HOTKEYS, f, indent=4)
        print(f"[MACRO] Saved hotkeys to {os.path.basename(path)}")
    except Exception as e:
        print(f"[ERROR] Failed to save overrides: {e}")


def _capture_one(timeout):
    """Wait up to `timeout` seconds for the next keyboard or mouse-button
    press and return its binding string ('y', 'f11', 'mouse:x', ...).
    Returns None on timeout.
    """
    holder = {}
    fired = threading.Event()

    def on_kb(e):
        if fired.is_set():
            return
        if e.event_type == 'down':
            name = (e.name or '').lower()
            if name:
                holder['v'] = name
                fired.set()

    def on_m(e):
        if fired.is_set():
            return
        if isinstance(e, mouse.ButtonEvent) and e.event_type in ('down', 'double'):
            holder['v'] = f'mouse:{e.button.lower()}'
            fired.set()

    keyboard.hook(on_kb)
    mouse.hook(on_m)
    triggered = fired.wait(timeout)
    try:
        keyboard.unhook(on_kb)
    except Exception:
        pass
    try:
        mouse.unhook(on_m)
    except Exception:
        pass
    return holder.get('v') if triggered else None


def _run_config_flow():
    print("\n=== Hotkey Configuration ===")
    print("For each action, press the new key or mouse button to rebind.")
    print("Wait 10 seconds to keep the current binding.\n")

    for action in ACTION_NAMES:
        current = HOTKEYS[action]
        print(f"  {action:<7}: current = {_display(current)}")
        print(f"           press new... ", end='', flush=True)
        # Brief drain so any in-flight events from the previous step
        # (e.g. the Enter that confirmed config mode) aren't captured.
        time.sleep(0.4)
        captured = _capture_one(10.0)
        if captured and captured != current:
            HOTKEYS[action] = captured
            print(f"-> {_display(captured)}")
        else:
            print("(kept)")

    seen = {}
    for action in ACTION_NAMES:
        b = HOTKEYS[action]
        if b in seen:
            print(f"[WARN] {action!r} and {seen[b]!r} both bound to {_display(b)} — last write wins.")
        seen[b] = action

    _save_overrides()
    print("=============================\n")


# === Recording hooks ===
def on_key_event(event):
    global recording, macro_events, start_time
    if not recording:
        return

    name = event.name.lower() if event.name else ''
    if name in _KB_HOTKEY_NAMES:
        return

    if event.event_type == 'down' and macro_events:
        last = next((e for e in reversed(macro_events)
                     if e['device'] == 'keyboard' and e['key'] == name), None)
        if last and last['event_type'] == 'down':
            return

    macro_events.append({
        'time': time.time() - start_time,
        'device': 'keyboard',
        'key': name,
        'event_type': event.event_type,
    })


def on_mouse_event(event):
    global recording, macro_events, start_time
    if not isinstance(event, mouse.ButtonEvent):
        return

    btn = event.button.lower()

    if event.event_type in ('down', 'double'):
        for action_name, fn in ACTIONS.items():
            binding = HOTKEYS.get(action_name)
            if binding and _is_mouse(binding) and _mouse_button(binding) == btn:
                fn()

    if not recording:
        return
    if btn in _MOUSE_HOTKEY_BUTTONS:
        return

    ev_type = 'down' if event.event_type == 'double' else event.event_type
    macro_events.append({
        'time': time.time() - start_time,
        'device': 'mouse',
        'key': btn,
        'event_type': ev_type,
    })


# === Playback ===
def play_macro():
    global playing, arduino
    if playing or not macro_events:
        return

    playing = True
    print("\n[MACRO] Playing macro...")

    start_play_time = time.time()

    for event in macro_events:
        if not playing:
            break
        target_time = start_play_time + event['time']
        sleep_time = target_time - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)

        if event.get('device', 'keyboard') == 'keyboard':
            if event['event_type'] == 'down':
                arduino.key_down(event['key'])
            elif event['event_type'] == 'up':
                arduino.key_up(event['key'])
        elif event.get('device') == 'mouse':
            if event['event_type'] in ('down', 'double'):
                arduino.mouse_down(event['key'])
            elif event['event_type'] == 'up':
                arduino.mouse_up(event['key'])

    for k in KEYS:
        arduino.key_up(k)
    arduino.mouse_up('left')
    arduino.mouse_up('right')

    playing = False
    print("[MACRO] Playback finished.")
    print_menu()


def toggle_recording():
    global recording, macro_events, start_time
    if playing:
        print("\n[ERROR] Cannot record while playing.")
        return

    if recording:
        recording = False
        print(f"\n[MACRO] Stopped recording. Recorded {len(macro_events)} events.")
        print_menu()
    else:
        macro_events = []
        start_time = time.time()
        recording = True
        print(f"\n[MACRO] 🔴 Started recording... Press {_display(HOTKEYS['record'])} to stop.")


def start_playback():
    """Idempotent — a press while a macro is already running is a no-op,
    so the play key can be spammed safely without cancelling the run.
    """
    if recording:
        print("\n[ERROR] Cannot play while recording.")
        return
    if playing:
        return
    if not macro_events:
        print("\n[ERROR] No macro recorded to play.")
        return
    threading.Thread(target=play_macro, daemon=True).start()


def stop_playback():
    global playing
    if playing:
        playing = False
        print("\n[MACRO] ⏹️ Playback stopped.")


# === Slot save/load ===
def _save_slot(slot):
    path = _slot_path(slot)
    try:
        with open(path, 'w') as f:
            json.dump(macro_events, f, indent=4)
        print(f"\n[MACRO] 💾 Saved to slot {slot} -> {os.path.basename(path)}")
    except Exception as e:
        print(f"\n[ERROR] Failed to save slot {slot}: {e}")
    print_menu()


def _load_slot(slot):
    global macro_events
    path = _slot_path(slot)
    if not os.path.exists(path):
        if slot == 1 and os.path.exists(_legacy_path()):
            path = _legacy_path()
        else:
            print(f"\n[ERROR] Slot {slot} is empty.")
            print_menu()
            return

    try:
        with open(path, 'r') as f:
            macro_events = json.load(f)
        print(f"\n[MACRO] 📂 Loaded slot {slot} ({len(macro_events)} events from {os.path.basename(path)})")
    except Exception as e:
        print(f"\n[ERROR] Failed to load slot {slot}: {e}")
    print_menu()


def _begin_save():
    if recording or playing:
        print("\n[ERROR] Cannot save while recording or playing.")
        return
    if not macro_events:
        print("\n[ERROR] No macro to save.")
        return
    occupied = _list_slots()
    if occupied:
        print(f"[MACRO] Save: occupied slots {', '.join(map(str, occupied))} (will overwrite)")
    else:
        print("[MACRO] Save: all slots empty")
    _set_pending('save')


def _begin_load():
    if recording or playing:
        print("\n[ERROR] Cannot load while recording or playing.")
        return
    occupied = _list_slots()
    legacy = os.path.exists(_legacy_path())
    if not occupied and not legacy:
        print("\n[ERROR] No saved slots.")
        return
    if occupied:
        print(f"[MACRO] Load: saved slots {', '.join(map(str, occupied))}")
    elif legacy:
        print("[MACRO] Load: legacy macro.json available as slot 1")
    _set_pending('load')


def _set_pending(action):
    global _pending_action, _slot_timer
    with _slot_lock:
        _clear_pending_locked()
        _pending_action = action
        for d in range(1, 10):
            try:
                h = keyboard.add_hotkey(str(d), _slot_chosen,
                                        args=(d,), suppress=True)
                _slot_hotkey_handles.append(h)
            except Exception as e:
                print(f"[ERROR] register slot hotkey {d}: {e}")
        try:
            h = keyboard.add_hotkey('esc', _slot_cancel_user, suppress=True)
            _slot_hotkey_handles.append(h)
        except Exception:
            pass
        _slot_timer = threading.Timer(SLOT_TIMEOUT_S, _slot_timeout_fired)
        _slot_timer.daemon = True
        _slot_timer.start()
    print(f"[MACRO] {action.title()}: press 1-9 to choose slot, ESC to cancel "
          f"(timeout {SLOT_TIMEOUT_S:.0f}s)")


def _clear_pending_locked():
    global _pending_action, _slot_timer
    for h in _slot_hotkey_handles:
        try:
            keyboard.remove_hotkey(h)
        except Exception:
            pass
    _slot_hotkey_handles.clear()
    if _slot_timer is not None:
        _slot_timer.cancel()
        _slot_timer = None
    _pending_action = None


def _slot_chosen(slot):
    threading.Thread(target=_handle_slot_choice,
                     args=(slot,), daemon=True).start()


def _handle_slot_choice(slot):
    with _slot_lock:
        action = _pending_action
        _clear_pending_locked()
    if action == 'save':
        _save_slot(slot)
    elif action == 'load':
        _load_slot(slot)


def _slot_cancel_user():
    threading.Thread(target=_handle_slot_cancel, daemon=True).start()


def _handle_slot_cancel():
    with _slot_lock:
        action = _pending_action
        _clear_pending_locked()
    if action:
        print(f"\n[MACRO] {action.title()} cancelled.")
        print_menu()


def _slot_timeout_fired():
    with _slot_lock:
        action = _pending_action
        if action is None:
            return
        _clear_pending_locked()
    print(f"\n[MACRO] {action.title()} timed out.")
    print_menu()


def request_exit():
    _exit_event.set()


ACTIONS = {
    'record': toggle_recording,
    'play':   start_playback,
    'stop':   stop_playback,
    'save':   _begin_save,
    'load':   _begin_load,
    'exit':   request_exit,
}


def print_menu():
    print("\n--- Macro Tool ---")
    rows = [
        ('Toggle Record',  'record'),
        ('Play',           'play'),
        ('Stop',           'stop'),
        ('Save (1-9)',     'save'),
        ('Load (1-9)',     'load'),
        ('Exit script',    'exit'),
    ]
    for label, key in rows:
        binding = HOTKEYS.get(key, '<unbound>')
        print(f"{_display(binding):<14} : {label}")
    occupied = _list_slots()
    if occupied:
        print(f"Saved slots    : {', '.join(map(str, occupied))}")
    elif os.path.exists(_legacy_path()):
        print("Saved slots    : 1 (legacy macro.json)")
    print("------------------")


def _validate_hotkeys():
    missing = set(ACTION_NAMES) - set(HOTKEYS.keys())
    if missing:
        print(f"[ERROR] Hotkeys missing: {sorted(missing)}")
        sys.exit(1)


def main():
    global arduino, _KB_HOTKEY_NAMES, _MOUSE_HOTKEY_BUTTONS
    print("Initializing Macro Tool...")

    import ctypes
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False

    if not is_admin:
        print("\n" + "=" * 60)
        print("🚨 WARNING: NOT RUNNING AS ADMINISTRATOR 🚨")
        print("Genshin Impact's anti-cheat blocks keyboard listeners from")
        print("normal programs. If you don't run your terminal/VS Code as")
        print("Administrator, hotkeys and recording will NOT work inside the game!")
        print("=" * 60 + "\n")

    try:
        arduino = ArduinoHIDController()
    except Exception as e:
        print(f"Failed to connect to Arduino: {e}")
        sys.exit(1)

    # Apply persisted overrides on top of config defaults.
    _load_overrides()

    # Optional config step.
    print("\n[c] Configure hotkeys   [Enter] Start")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        choice = ''
    if choice == 'c':
        _run_config_flow()

    _validate_hotkeys()

    _KB_HOTKEY_NAMES = {
        b.lower() for b in HOTKEYS.values() if not _is_mouse(b)
    }
    _MOUSE_HOTKEY_BUTTONS = {
        _mouse_button(b) for b in HOTKEYS.values() if _is_mouse(b)
    }

    keyboard.hook(on_key_event)
    mouse.hook(on_mouse_event)

    for name, fn in ACTIONS.items():
        binding = HOTKEYS[name]
        if _is_mouse(binding):
            continue
        try:
            keyboard.add_hotkey(binding, fn, suppress=True)
        except Exception as e:
            print(f"[ERROR] Failed to bind {name!r} -> {binding!r}: {e}")

    print_menu()

    _exit_event.wait()

    print("\nExiting Macro Tool...")
    arduino.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
