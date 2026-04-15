# pc_client/controller.py
import serial
import time
import threading
import sys
from config import (SERIAL_PORT, BAUD_RATE, HOLD_RESTART_GAP_MS,
                    TAP_MULTI_INTERNAL_GAP_MS)

class ArduinoHIDController:
    def __init__(self):
        self.port = SERIAL_PORT
        self.baud = BAUD_RATE
        self.ser = None
        # Serialize all writes from worker threads. Concurrent writes to the
        # same Windows COM port cause race conditions and Write timeouts.
        self._write_lock = threading.Lock()
        # Per-key UP epoch. hold_start/hold_restart bump it to invalidate
        # any tap UPs that were scheduled before the hold began. Without
        # this, a tap's delayed UP daemon would release a key that has
        # since been promoted to a held key — breaking the hold.
        self._up_epoch = {k: 0 for k in ['a', 's', 'd', 'j', 'k', 'l']}
        self._epoch_lock = threading.Lock()

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

    def _threaded_action(self, cmd, delay_ms):
        """Sleep in a separate thread, then send the command"""
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        self._send_command(cmd)

    def _delayed_up(self, key, delay_ms, expected_epoch):
        """Send KEY_UP after a delay, but only if the epoch hasn't moved.

        If hold_start/hold_restart bumped the epoch in the meantime, this
        scheduled release was cancelled — sending it would release a key
        that has since become a held key.
        """
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        with self._epoch_lock:
            if self._up_epoch[key.lower()] != expected_epoch:
                return  # cancelled by hold_start
        self._send_command(f"{key.upper()}_UP")

    def tap_key(self, key):
        """Simulate a fast key tap (press and immediate release)"""
        with self._epoch_lock:
            epoch = self._up_epoch[key.lower()]
        self._send_command(f"{key.upper()}_DOWN")
        threading.Thread(target=self._delayed_up,
                         args=(key, 50, epoch), daemon=True).start()

    def tap_multi(self, key, count, gap_ms=None):
        """Fire `count` taps in quick succession on the same key.

        First tap fires immediately. Each subsequent tap is scheduled in its
        own daemon thread at i*gap_ms (DOWN) and i*gap_ms+50 (UP). Used for
        consecutive tap notes that visually merge into one tall contour.

        All UPs share a single epoch snapshot taken at the start of the
        burst. If a hold_start fires before any UP runs, every UP in the
        burst is cancelled together.
        """
        if gap_ms is None:
            gap_ms = TAP_MULTI_INTERNAL_GAP_MS
        if count < 1:
            return
        with self._epoch_lock:
            epoch = self._up_epoch[key.lower()]
        # Tap #0 fires now
        self._send_command(f"{key.upper()}_DOWN")
        threading.Thread(target=self._delayed_up,
                         args=(key, 50, epoch), daemon=True).start()
        # Taps #1..N-1 are scheduled with increasing offsets
        for i in range(1, count):
            down_delay = i * gap_ms
            up_delay = down_delay + 50
            threading.Thread(target=self._threaded_action,
                             args=(f"{key.upper()}_DOWN", down_delay),
                             daemon=True).start()
            threading.Thread(target=self._delayed_up,
                             args=(key, up_delay, epoch),
                             daemon=True).start()

    def hold_start(self, key):
        """Start holding a key down immediately.

        Bumps the per-key UP epoch so any in-flight tap UP daemons skip
        their write — preventing them from releasing this newly-held key.
        """
        with self._epoch_lock:
            self._up_epoch[key.lower()] += 1
        self._send_command(f"{key.upper()}_DOWN")

    def hold_end(self, key):
        """Release a held key immediately"""
        self._send_command(f"{key.upper()}_UP")

    def hold_restart(self, key):
        """Release and immediately re-press for consecutive hold notes.

        Also bumps the epoch — any pending tap UPs from before the restart
        are cancelled. The scheduled DOWN is not epoch-checked because
        re-pressing is the intended behavior.
        """
        with self._epoch_lock:
            self._up_epoch[key.lower()] += 1
        self._send_command(f"{key.upper()}_UP")
        threading.Thread(target=self._threaded_action,
                         args=(f"{key.upper()}_DOWN", HOLD_RESTART_GAP_MS),
                         daemon=True).start()

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
