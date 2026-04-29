# pc_client/controller.py
import serial
import time
import threading
import sys
from config import SERIAL_PORT, BAUD_RATE

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
        self.port = SERIAL_PORT
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
