# pc_client/controller.py
import serial
import time
import threading
import sys
from config import SERIAL_PORT, BAUD_RATE

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
                    self.ser.flush()
            except (serial.SerialTimeoutException, serial.SerialException, OSError) as e:
                # Don't let a transient write failure crash a worker thread.
                print(f"[serial] write failed for {cmd!r}: {e}")

    def key_down(self, key):
        """Press a key down"""
        self._send_command(f"{key.upper()}_DOWN")

    def key_up(self, key):
        """Release a key"""
        self._send_command(f"{key.upper()}_UP")

    def close(self):
        if self.ser and self.ser.is_open:
            # Send UP to all keys just in case
            for k in ['A', 'S', 'D', 'J', 'K', 'L']:
                self._send_command(f"{k}_UP")
            self.ser.close()
            print("Serial connection closed")

if __name__ == "__main__":
    # Test script if run directly
    ctrl = ArduinoHIDController()
    print("Testing taps. You should see keys being pressed in 3 seconds")
    time.sleep(3)
    
    print("Tapping A")
    ctrl.key_down('a')
    time.sleep(0.05)
    ctrl.key_up('a')
    time.sleep(1)
    
    print("Tapping S")
    ctrl.key_down('s')
    time.sleep(0.05)
    ctrl.key_up('s')
    time.sleep(1)
    
    ctrl.close()
