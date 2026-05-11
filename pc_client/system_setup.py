# pc_client/system_setup.py
"""Process-wide Windows setup the rhythm runner needs before any
detector thread starts.

- DPI awareness: GDI `GetPixel` calls in the detector use raw pixel
  coords. Without `SetProcessDPIAware` the OS auto-scales them on
  high-DPI displays and the per-key polls land on the wrong pixels.
- Multimedia timer: the per-key polls use `time.sleep(0.005)`. The
  Windows scheduler quantum is ~15.6 ms by default, so without
  `timeBeginPeriod(1)` the 5 ms cadence collapses to ~16 ms and the
  detector misses notes. `timeBeginPeriod` / `timeEndPeriod` are
  reference-counted by the OS so calling them once per UI session is
  fine.
"""
import ctypes


def boost_timer():
    """Boost system timer to 1ms. Returns True if successful."""
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        return True
    except Exception as e:
        print(f"[warn] timeBeginPeriod failed: {e}")
        return False


def restore_timer():
    try:
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def set_dpi_aware():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        print(f"[warn] SetProcessDPIAware failed: {e}")
