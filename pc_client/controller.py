# pc_client/controller.py
import ctypes
import ctypes.wintypes
import serial
from serial.tools import list_ports
import time
import threading
import sys
from config import SERIAL_PORT, BAUD_RATE

# Known USB VIDs for Arduino-compatible ATmega32u4 boards capable of HID.
# Match priority is list order: genuine Arduino first, then Sparkfun Pro
# Micro, then CH340-based clones (which advertise a generic Qinheng VID).
_KNOWN_VIDS = [
    0x2341,  # Arduino LLC (Leonardo, Micro)
    0x2A03,  # Arduino SRL (post-split clone of 0x2341)
    0x1B4F,  # Sparkfun Pro Micro
    0x1A86,  # QinHeng CH340 — common on cheap clones
    0x10C4,  # Silicon Labs CP210x — some clones
]

# Description-substring fallback when VID isn't recognized. Match is
# case-insensitive on the port's `description` (e.g. "Arduino Leonardo
# (COM7)" on Windows).
_DESCRIPTION_HINTS = ('arduino', 'leonardo', 'pro micro')


def _auto_detect_port():
    """Find the first plausible Arduino COM port. Returns device name
    (e.g. 'COM7') or None if no candidate found. Prefers VID match; falls
    back to description-substring match."""
    ports = list(list_ports.comports())
    # VID match — strongest signal.
    for vid in _KNOWN_VIDS:
        for p in ports:
            if p.vid == vid:
                print(f"Auto-detected Arduino on {p.device} "
                      f"(VID=0x{p.vid:04X}, '{p.description}')")
                return p.device
    # Description fallback for boards with unknown VIDs.
    for p in ports:
        desc = (p.description or '').lower()
        if any(hint in desc for hint in _DESCRIPTION_HINTS):
            print(f"Auto-detected Arduino on {p.device} "
                  f"(description match: '{p.description}')")
            return p.device
    if ports:
        print("No Arduino-like port matched. Available ports:")
        for p in ports:
            vid = f"0x{p.vid:04X}" if p.vid is not None else '?'
            print(f"  {p.device}  VID={vid}  '{p.description}'")
    else:
        print("No serial ports found at all.")
    return None

# Arduino Keyboard.h modifiers
KEY_MAP = {
    'ctrl': 128, 'left ctrl': 128, 'right ctrl': 128,
    'shift': 129, 'left shift': 129, 'right shift': 129,
    'alt': 130, 'left alt': 130, 'right alt': 130,
    'up': 218, 'down': 217, 'left': 216, 'right': 215,
    'backspace': 178, 'tab': 179, 'enter': 176, 'esc': 177,
    'space': 32
}

MOUSE_MAP = {
    'left': 1, 'right': 2, 'middle': 4
}


class ArduinoHIDController:
    def __init__(self):
        # Resolve port: explicit config value wins; None/empty triggers
        # auto-detection. Auto-detect failure exits — no port = no bot.
        if SERIAL_PORT:
            self.port = SERIAL_PORT
        else:
            detected = _auto_detect_port()
            if not detected:
                print("ERROR: SERIAL_PORT is None and auto-detection failed. "
                      "Plug in the Arduino or set SERIAL_PORT in config.py.")
                sys.exit(1)
            self.port = detected
        self.baud = BAUD_RATE
        self.ser = None
        # Serialize all writes from worker threads. Concurrent writes to the
        # same Windows COM port cause race conditions and Write timeouts.
        self._write_lock = threading.Lock()

        try:
            print(f"Connecting to Arduino on {self.port} at {self.baud} baud")
            # Explicit write_timeout so a stalled write fails fast instead of
            # blocking other commands behind it for unbounded time.
            self.ser = serial.Serial(self.port, self.baud,
                                     timeout=1, write_timeout=0.5)
            time.sleep(2) # Allow arduino leonardo time to reset and initialize serial
            print("Connected successfully")
        except Exception as e:
            print(f"Error connecting to Arduino on {self.port}: {e}")
            print("Please check your connection and the COM port in config.py")
            sys.exit(1)

    def _send_command(self, cmd):
        """Internal method to write string to serial"""
        if self.ser and self.ser.is_open:
            cmd_str = f"{cmd}\n"
            try:
                with self._write_lock:
                    self.ser.write(cmd_str.encode('utf-8'))
            except (serial.SerialTimeoutException, serial.SerialException, OSError) as e:
                # Don't let a transient write failure crash a worker thread.
                print(f"[serial] write failed for {cmd!r}: {e}")

    def _get_key_code(self, key):
        k = key.lower()
        if len(k) == 1:
            return ord(k)
        return KEY_MAP.get(k, 0)

    def key_down(self, key):
        """Press a key down"""
        code = self._get_key_code(key)
        if code:
            self._send_command(f"K:{code}:DOWN")

    def key_up(self, key):
        """Release a key"""
        code = self._get_key_code(key)
        if code:
            self._send_command(f"K:{code}:UP")
            
    def mouse_down(self, button):
        """Press a mouse button"""
        code = MOUSE_MAP.get(button.lower(), 1)
        self._send_command(f"M:{code}:DOWN")

    def mouse_up(self, button):
        """Release a mouse button"""
        code = MOUSE_MAP.get(button.lower(), 1)
        self._send_command(f"M:{code}:UP")

    def mouse_move(self, dx, dy):
        """Move the mouse pointer by (dx, dy) pixels via HID. Genshin's
        anti-cheat ignores SetCursorPos / synthetic SendInput, so use the
        Arduino's hardware-style relative move. The sketch chunks deltas
        larger than ±127 into multiple HID reports."""
        self._send_command(f"P:{int(dx)}:{int(dy)}")

    def move_to(self, x, y, stop_evt=None, max_iters=15, tol=2):
        """Closed-loop absolute move to screen-pixel (x, y). Genshin's
        anti-cheat ignores SetCursorPos and synthetic SendInput-style
        cursor moves, so we drive the cursor with real HID relative
        deltas and check via GetCursorPos until it lands within `tol`
        pixels (axis-wise) of the target. Iterating compensates for
        Windows pointer-precision (Enhance Pointer Precision) which
        makes a single delta over- or under-shoot — usually converges
        in 1-2 iters with EPP off (AlbumRunner disables it for the
        run). Sleep between iters is 40 ms — long enough that the
        Arduino round-trip + cursor update is observable on the next
        GetCursorPos. Returns True on convergence, False on stop_evt
        or running out of iterations."""
        user32 = ctypes.windll.user32
        pt = ctypes.wintypes.POINT()
        for _ in range(max_iters):
            if stop_evt is not None and stop_evt.is_set():
                return False
            user32.GetCursorPos(ctypes.byref(pt))
            dx = int(x) - pt.x
            dy = int(y) - pt.y
            if abs(dx) <= tol and abs(dy) <= tol:
                return True
            self.mouse_move(dx, dy)
            # Cancellable wait — stop_evt.wait returns True if set.
            if stop_evt is not None:
                if stop_evt.wait(0.04):
                    return False
            else:
                time.sleep(0.04)
        return False

    def close(self):
        if self.ser and self.ser.is_open:
            # Send UP to all standard rhythm keys just in case
            for k in ['a', 's', 'd', 'j', 'k', 'l']:
                self.key_up(k)
            # Release mouse just in case
            self.mouse_up('left')
            self.mouse_up('right')
            self.ser.close()
            print("Serial connection closed")

if __name__ == "__main__":
    # Test script if run directly
    ctrl = ArduinoHIDController()
    print("Testing keys. You should see keys being pressed in 3 seconds")
    time.sleep(3)
    
    print("Tapping A")
    ctrl.key_down('a')
    time.sleep(0.05)
    ctrl.key_up('a')
    time.sleep(1)
    
    print("Tapping Enter")
    ctrl.key_down('enter')
    time.sleep(0.05)
    ctrl.key_up('enter')
    time.sleep(1)
    
    ctrl.close()
