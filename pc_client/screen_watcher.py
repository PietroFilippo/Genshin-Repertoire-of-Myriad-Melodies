# pc_client/screen_watcher.py
"""Capture-region-aware screen automation: grab a frame, scale 1080p
reference coords to the live resolution, match a named template inside
a ROI, click at ref-or-local coords with hover/hold timing, poll until
a template appears or disappears. Every wait routes through the
caller-provided `stop_evt` so a UI Stop button can abort any blocking
call within ~200ms.

The watcher does NOT own:
- Window handle resolution (caller passes the capture region + scale).
- Template loading / asset paths (caller hands in a pre-rescaled
  `{name: cv2 image}` dict; templates are application-specific assets).
- Enhance Pointer Precision toggling. EPP must be disabled when clicks
  go through the Arduino backend's iterative `move_to` (otherwise it
  overshoots). The album runner owns that toggle for its run; future
  callers must do the same (see CLAUDE.md → Input backend contract).
- Application-specific diagnostics. The album runner keeps its own
  `_diagnose_album_page` because it needs the raw `max_val` even
  below threshold, which `match()` filters out by design.

Threading: `grab()` uses a thread-local `mss.mss()` because mss>=6
handles are thread-local (otherwise `_thread._local has no attribute
'srcdc'`). Other methods are caller-synchronized — `_write_lock` on
the controller serializes simultaneous click bursts from worker
threads.
"""
import ctypes
import threading
import time
from pathlib import Path

import cv2
import mss
import numpy as np


# Click choreography — hardcoded because synthetic clicks faster than
# these tend to be debounced as tap-cancel by Genshin's UI. Hover
# settles the cursor before the down event so EPP-induced overshoot
# (Arduino backend) gets a chance to converge. Hold gives the game's
# input thread a frame to register the press. Settle absorbs the
# UI's animation start before the next click.
CLICK_HOVER_S = 0.15
CLICK_HOLD_S = 0.10
CLICK_SETTLE_S = 0.15


# Default match threshold when caller doesn't pass one at construction.
# 0.85 picks up the typical UI-pill template against a clean
# background; lower for pixelated assets or noisy backgrounds.
DEFAULT_MATCH_THRESHOLD = 0.85


class ScreenWatcher:
    def __init__(self, region, scale_x, scale_y, controller, stop_evt,
                 templates, match_threshold=DEFAULT_MATCH_THRESHOLD):
        """
        Args:
            region: dict with keys top/left/width/height — the mss
                capture region (already in screen coords).
            scale_x, scale_y: float multipliers from 1080p reference
                coords to the live game-window resolution.
            controller: an `InputBackend` (Arduino or Software). Used
                for `move_to` + mouse down/up during clicks.
            stop_evt: REQUIRED. Set this to cancel any blocking call
                in this watcher. `wait_for` polls it every 200ms;
                `stop_wait` returns True on set.
            templates: dict[str, np.ndarray] of pre-loaded, already-
                rescaled-to-asset-scale templates. Caller owns asset
                path resolution.
            match_threshold: default minimum `cv2.matchTemplate`
                score for `match()` to count as a hit. Per-call
                override available via the method's `threshold` arg.
        """
        if stop_evt is None:
            raise ValueError("ScreenWatcher requires a stop_evt — "
                             "use threading.Event() if there's no "
                             "external one to thread through")
        self.region = region
        self.scale_x = scale_x
        self.scale_y = scale_y
        self.controller = controller
        self.stop_evt = stop_evt
        self.templates = templates
        self._match_threshold = match_threshold
        # mss handles are thread-local in mss>=6 — each thread that
        # calls grab() needs its own mss.mss().
        self._sct_local = threading.local()

    # --- capture + coord helpers -------------------------------------------

    def grab(self):
        """Capture a BGR frame of the configured region. Thread-safe
        (each thread lazily creates its own mss instance)."""
        sct = getattr(self._sct_local, 'sct', None)
        if sct is None:
            sct = mss.mss()
            self._sct_local.sct = sct
        shot = sct.grab(self.region)
        return np.ascontiguousarray(np.array(shot)[:, :, :3])

    def ref_to_screen(self, x, y):
        """Convert 1080p reference (x, y) to absolute screen coords
        (suitable for `controller.move_to`)."""
        return (int(x * self.scale_x) + self.region['left'],
                int(y * self.scale_y) + self.region['top'])

    def ref_rect_to_local(self, ref_x, ref_y, ref_w, ref_h):
        """Convert a 1080p reference rectangle to capture-region-
        local coords (suitable as a ROI for `match()`)."""
        return (int(ref_x * self.scale_x), int(ref_y * self.scale_y),
                int(ref_w * self.scale_x), int(ref_h * self.scale_y))

    # --- template matching --------------------------------------------------

    def match(self, frame, tpl_name, roi=None, threshold=None):
        """Best match of templates[tpl_name] inside `roi` (or the full
        frame). Returns (score, (cx, cy)) in capture-region-local
        coords, or None if no match meets the threshold. ROI is
        expressed in capture-region-local coords too — use
        `ref_rect_to_local` to convert from 1080p reference."""
        if threshold is None:
            threshold = self._match_threshold
        tpl = self.templates[tpl_name]
        if roi is None:
            haystack = frame
            ox, oy = 0, 0
        else:
            x, y, w, h = roi
            x = max(0, x)
            y = max(0, y)
            x2 = min(frame.shape[1], x + w)
            y2 = min(frame.shape[0], y + h)
            haystack = frame[y:y2, x:x2]
            ox, oy = x, y
        if (haystack.shape[0] < tpl.shape[0]
                or haystack.shape[1] < tpl.shape[1]):
            return None
        result = cv2.matchTemplate(haystack, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return None
        cx = ox + max_loc[0] + tpl.shape[1] // 2
        cy = oy + max_loc[1] + tpl.shape[0] // 2
        return float(max_val), (cx, cy)

    # --- clicks -------------------------------------------------------------

    def click_screen(self, sx, sy):
        """Move cursor to absolute screen coords (sx, sy), then click.
        Cancellable: bails after `move_to` if stop_evt is set so the
        Arduino backend's iterative convergence can abort cleanly."""
        converged = self.controller.move_to(int(sx), int(sy),
                                            stop_evt=self.stop_evt)
        if self.stop_evt.is_set():
            return
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        print(f"    click target=({int(sx)},{int(sy)}), "
              f"final=({pt.x},{pt.y}), converged={converged}")
        # If stop fires after mouse_down we still issue mouse_up so
        # the Arduino doesn't end the run with the button held.
        if self.stop_wait(CLICK_HOVER_S):
            return
        self.controller.mouse_down('left')
        self.stop_wait(CLICK_HOLD_S)
        self.controller.mouse_up('left')
        self.stop_wait(CLICK_SETTLE_S)

    def click_ref(self, ref_x, ref_y):
        """Click at a 1080p reference position."""
        self.click_screen(*self.ref_to_screen(ref_x, ref_y))

    def click_local(self, lx, ly):
        """Click at a capture-region-local position (e.g. the (cx, cy)
        a `match()` call returned)."""
        self.click_screen(lx + self.region['left'],
                          ly + self.region['top'])

    # --- waits --------------------------------------------------------------

    def wait_for(self, tpl_name, ref_roi, timeout=5.0, present=True):
        """Poll until `templates[tpl_name]` is present (or absent if
        present=False) inside `ref_roi` (1080p reference coords).
        Returns True iff the desired state is reached within timeout.
        Stop-aware: bails immediately on stop_evt (returns False)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_evt.is_set():
                return False
            frame = self.grab()
            roi = self.ref_rect_to_local(*ref_roi)
            found = self.match(frame, tpl_name, roi=roi) is not None
            if found == present:
                return True
            if self.stop_wait(0.2):
                return False
        return False

    def stop_wait(self, secs):
        """Cancellable sleep. Blocks up to `secs` and returns True if
        `stop_evt` fired during the wait. Use everywhere a plain
        `time.sleep` would otherwise stall the caller after stop_evt
        is set."""
        if secs <= 0:
            return self.stop_evt.is_set()
        return self.stop_evt.wait(secs)

    # --- diagnostics --------------------------------------------------------

    def dump_frame(self, path):
        """Capture the current frame and write it to `path` (must be a
        pathlib.Path). Returns the path for caller logging
        convenience. Caller owns path naming + cleanup."""
        path = Path(path)
        cv2.imwrite(str(path), self.grab())
        return path
