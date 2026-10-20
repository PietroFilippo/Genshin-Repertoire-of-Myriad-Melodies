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

Geometry: region + scales live in one tuple swapped atomically by
`update_geometry()`, so the end-of-song watcher thread never reads a
new `scale_x` paired with an old `region` while the album thread is
tracking a windowed game that was moved or resized. Each swap bumps
`generation`; a caller that matched on a frame and clicks later passes
the generation it captured at, and the click is refused if the
geometry changed in between (its local coords would be for the old
window — e.g. landing outside a game that just shrank).
"""
import ctypes
import ctypes.wintypes
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
                 templates, match_threshold=DEFAULT_MATCH_THRESHOLD,
                 live_geometry_fn=None, window_gone_fn=None):
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
            live_geometry_fn: optional `callable() -> (region, scale_x,
                scale_y) | None` reading the game window's CURRENT client
                rect (e.g. `game_window.read_game_geometry(hwnd)` — a few
                non-messaging Win32 reads). Checked right before every
                mouse-down so a window change the caller's periodic poll
                hasn't published yet still stops the click. None = no
                live check (caller-published geometry only).
            window_gone_fn: optional `callable() -> bool`, True once the
                game window is no longer usable — game closed or window
                changed (AlbumRunner passes its `_window_gone`, which also
                reports it). `wait_for` gives up as soon as it says so
                instead of polling until the timeout. None = no check.
                (The pre-click check is `live_geometry_fn`'s job: return
                None once the window isn't the game's.)
        """
        if stop_evt is None:
            raise ValueError("ScreenWatcher requires a stop_evt — "
                             "use threading.Event() if there's no "
                             "external one to thread through")
        # (region, scale_x, scale_y, generation) — see module docstring.
        self._geom = (dict(region), scale_x, scale_y, 0)
        # Serializes generation bumps: update_geometry can be called from
        # the album thread (periodic poll) and from a clicking thread
        # (live check) at the same time.
        self._geom_lock = threading.Lock()
        self._live_geometry_fn = live_geometry_fn
        self._window_gone_fn = window_gone_fn
        self.controller = controller
        self.stop_evt = stop_evt
        self.templates = templates
        self._match_threshold = match_threshold
        # mss handles are thread-local in mss>=6 — each thread that
        # calls grab() needs its own mss.mss().
        self._sct_local = threading.local()

    # --- geometry -----------------------------------------------------------

    @property
    def region(self):
        return self._geom[0]

    @property
    def scale_x(self):
        return self._geom[1]

    @property
    def scale_y(self):
        return self._geom[2]

    @property
    def generation(self):
        """Bumped by every `update_geometry`. Read it BEFORE `grab()` and
        pass it to `click_local(..., generation=)` so a click computed
        from a frame of the old geometry is refused."""
        return self._geom[3]

    def update_geometry(self, region, scale_x, scale_y):
        """Swap in a new capture region + scales (game window moved or
        resized). Single tuple assignment, so concurrent readers see
        either the old geometry or the new one, never a mix. Templates
        are caller-owned: on a resolution change the caller rescales its
        `templates` dict in place."""
        with self._geom_lock:
            self._geom = (dict(region), scale_x, scale_y, self._geom[3] + 1)

    # --- capture + coord helpers -------------------------------------------

    def grab(self):
        """Capture a BGR frame of the configured region. Thread-safe
        (each thread lazily creates its own mss instance)."""
        sct = getattr(self._sct_local, 'sct', None)
        if sct is None:
            sct = mss.mss()
            self._sct_local.sct = sct
        shot = sct.grab(self._geom[0])
        return np.ascontiguousarray(np.array(shot)[:, :, :3])

    def ref_to_screen(self, x, y):
        """Convert 1080p reference (x, y) to absolute screen coords
        (suitable for `controller.move_to`)."""
        region, sx, sy, _ = self._geom
        return (int(x * sx) + region['left'],
                int(y * sy) + region['top'])

    def ref_rect_to_local(self, ref_x, ref_y, ref_w, ref_h):
        """Convert a 1080p reference rectangle to capture-region-
        local coords (suitable as a ROI for `match()`)."""
        _, sx, sy, _ = self._geom
        return (int(ref_x * sx), int(ref_y * sy),
                int(ref_w * sx), int(ref_h * sy))

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

    def click_screen(self, sx, sy, generation=None):
        """Move cursor to absolute screen coords (sx, sy), then click.
        Cancellable: bails after `move_to` if stop_evt is set so the
        Arduino backend's iterative convergence can abort cleanly.

        Returns True if the click was issued, False if it wasn't — stop
        fired, `move_to` didn't converge (clicking wherever the cursor
        happened to stop could hit the wrong UI element), `generation`
        was given and the geometry changed before the button went down
        (target computed for the old window), or the live window check
        failed (see `_live_target_ok`; that one also publishes the new
        geometry, bumping `generation`). Callers treat False like a
        missed click; check `stop_evt.is_set()` / `generation` to tell
        the cases apart (same escape hatch as `move_to`)."""
        if self._stale(generation):
            return False
        converged = self.controller.move_to(int(sx), int(sy),
                                            stop_evt=self.stop_evt)
        if self.stop_evt.is_set():
            return False
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        if not converged:
            print(f"    click SKIPPED target=({int(sx)},{int(sy)}), "
                  f"final=({pt.x},{pt.y}) — cursor didn't reach the target")
            return False
        print(f"    click target=({int(sx)},{int(sy)}), "
              f"final=({pt.x},{pt.y}), converged={converged}")
        # If stop fires after mouse_down we still issue mouse_up so
        # the Arduino doesn't end the run with the button held.
        if self.stop_wait(CLICK_HOVER_S):
            return False
        # Last chance to notice the window moved / resized while the
        # cursor travelled (Arduino move_to can take ~600 ms): first the
        # caller-published geometry, then the LIVE client rect — the
        # periodic poll may not have seen a change that just happened.
        if self._stale(generation):
            return False
        if not self._live_target_ok(sx, sy):
            return False
        self.controller.mouse_down('left')
        self.stop_wait(CLICK_HOLD_S)
        self.controller.mouse_up('left')
        self.stop_wait(CLICK_SETTLE_S)
        return True

    def _live_target_ok(self, sx, sy):
        """Read the game's current client rect (cheap, non-messaging) and
        confirm it still matches our geometry and contains (sx, sy).
        On a mismatch publish the live geometry (bumps `generation`, so
        callers re-compute / re-capture) and refuse the click. If the
        window isn't usable right now (minimized / gone), refuse too —
        any click would land on whatever is behind it."""
        if self._live_geometry_fn is None:
            return True
        try:
            live = self._live_geometry_fn()
        except Exception as e:
            print(f"    [watcher] live window check failed: {e}")
            return True
        if live is None:
            print("    click SKIPPED — game window isn't usable right now "
                  "(minimized / closed / off-screen)")
            return False
        region, sx_scale, sy_scale = live
        cur_region, cur_sx, cur_sy, _ = self._geom
        inside = (region['left'] <= sx < region['left'] + region['width']
                  and region['top'] <= sy < region['top'] + region['height'])
        if (region == cur_region and sx_scale == cur_sx
                and sy_scale == cur_sy and inside):
            return True
        if region != cur_region or sx_scale != cur_sx or sy_scale != cur_sy:
            self.update_geometry(region, sx_scale, sy_scale)
        print(f"    click SKIPPED — game window is now "
              f"{region['width']}x{region['height']} @ "
              f"({region['left']},{region['top']}); target "
              f"({int(sx)},{int(sy)}) was for the old window")
        return False

    def click_ref(self, ref_x, ref_y):
        """Click at a 1080p reference position. Returns `click_screen`'s
        result. If the window moved / resized under the click (the
        generation changed), the target is re-computed from the new
        geometry and the click retried once — unlike `click_local`, a
        reference coordinate stays valid across a geometry change."""
        for _ in range(2):
            gen = self.generation
            if self.click_screen(*self.ref_to_screen(ref_x, ref_y),
                                 generation=gen):
                return True
            if self.stop_evt.is_set() or self.generation == gen:
                return False
        return False

    def click_local(self, lx, ly, generation=None):
        """Click at a capture-region-local position (e.g. the (cx, cy)
        a `match()` call returned). Pass the `generation` read before
        the `grab()` that produced (lx, ly): if the geometry has changed
        since, the click is refused (returns False) and the caller
        should re-capture and re-match. Returns `click_screen`'s result."""
        region, _, _, current = self._geom
        if generation is not None and generation != current:
            self._stale(generation)
            return False
        return self.click_screen(lx + region['left'],
                                 ly + region['top'],
                                 generation=generation)

    def _stale(self, generation):
        if generation is None or generation == self._geom[3]:
            return False
        print("    click SKIPPED — game window moved/resized since the "
              "frame was captured")
        return True

    # --- waits --------------------------------------------------------------

    def wait_for(self, tpl_name, ref_roi, timeout=5.0, present=True):
        """Poll until `templates[tpl_name]` is present (or absent if
        present=False) inside `ref_roi` (1080p reference coords).
        Returns True iff the desired state is reached within timeout.
        Stop-aware: bails immediately on stop_evt (returns False), and on
        a closed game when `window_gone_fn` is set (returns False too —
        the caller tells the cases apart)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_evt.is_set():
                return False
            if self._window_gone():
                return False
            frame = self.grab()
            roi = self.ref_rect_to_local(*ref_roi)
            found = self.match(frame, tpl_name, roi=roi) is not None
            if found == present:
                return True
            if self.stop_wait(0.2):
                return False
        return False

    def _window_gone(self):
        fn = self._window_gone_fn
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception as e:
            print(f"    [watcher] window check failed: {e}")
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
