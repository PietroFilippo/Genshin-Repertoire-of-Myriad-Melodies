# pc_client/controller.py
import serial
import time
import random
import threading
import sys
from config import SERIAL_PORT, BAUD_RATE, HUMANIZATION_MIN_LATENCY, HUMANIZATION_MAX_LATENCY

class ArduinoHIDController:
    def __init__(self):
        self.port = SERIAL_PORT
        self.baud = BAUD_RATE
        self.ser = None
        
        try:
            print(f"Connecting to Arduino on {self.port} at {self.baud} baud")
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
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
            self.ser.write(cmd_str.encode('utf-8'))
            self.ser.flush()

    def _threaded_action(self, cmd, delay_ms):
        """Sleep in a separate thread, then send the command"""
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        self._send_command(cmd)

    def get_human_delay(self):
        return random.uniform(HUMANIZATION_MIN_LATENCY, HUMANIZATION_MAX_LATENCY)

    def tap_key(self, key):
        """Simulate a fast key tap (press and immediate release) with delay"""
        delay = self.get_human_delay()
        threading.Thread(target=self._threaded_action, args=(f"{key.upper()}_DOWN", delay), daemon=True).start()
        # Release automatically after 50ms + the initial delay
        threading.Thread(target=self._threaded_action, args=(f"{key.upper()}_UP", delay + 50), daemon=True).start()

    def hold_start(self, key):
        """Start holding a key down with delay"""
        delay = self.get_human_delay()
        threading.Thread(target=self._threaded_action, args=(f"{key.upper()}_DOWN", delay), daemon=True).start()

    def hold_end(self, key):
        """Release a held key with delay"""
        delay = self.get_human_delay()
        threading.Thread(target=self._threaded_action, args=(f"{key.upper()}_UP", delay), daemon=True).start()

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
    ctrl.tap_key('a')
    time.sleep(1)
    
    print("Tapping S")
    ctrl.tap_key('s')
    time.sleep(1)
    
    ctrl.close()
