import cv2
import numpy as np
import time
from config import (COLUMN_CENTERS, KEYS, TAP_COLOR_LOWER, TAP_COLOR_UPPER,
                     HOLD_COLOR_LOWER, HOLD_COLOR_UPPER, HIT_LINE_Y,
                     HOLD_PROBE_HALF_WIDTH, HOLD_PROBE_ABOVE, HOLD_PROBE_BELOW,
                     HOLD_RELEASE_FRAMES, HOLD_MIN_TIME,
                     POST_TAP_WATCH_DURATION, POST_TAP_PIXEL_THRESHOLD,
                     POST_TAP_CONFIRM_FRAMES, POST_TAP_EXPANDED_WINDOW,
                     TAP_MAX_H, TAP_DOUBLE_ASPECT, TAP_TRIPLE_ASPECT,
                     TAP_DOUBLE_MIN_H, TAP_TRIPLE_MIN_H,
                     TAP_DOUBLE_MIN_EXTRA_H,
                     TAP_NEIGHBOR_MIN_GAP_PX, TAP_NEIGHBOR_MAX_GAP_PX,
                     TAP_FALL_SPEED_PX_PER_S, TAP_MULTI_GAP_MIN_MS,
                     TAP_MULTI_GAP_MAX_MS, TAP_MULTI_INTERNAL_GAP_MS,
                     TAP_SINGLE_LOCKOUT_S, TAP_MULTI_LOCKOUT_S)

class NoteDetector:
    def __init__(self):
        # Configuration
        self.column_centers = COLUMN_CENTERS
        self.hit_line_y = HIT_LINE_Y
        self.col_width = 120

        # State tracking
        self.holding_state = {key: False for key in KEYS}
        self.last_tap_time = {key: 0.0 for key in KEYS}
        # Absolute timestamp until which TAPs are blocked from re-firing.
        # Multi-tap actions push this further into the future than single taps.
        self.tap_lockout_until = {key: 0.0 for key in KEYS}
        # Per-key dynamic gap for the most recent multi-tap action. main.py
        # reads this when dispatching TAP_DOUBLE / TAP_TRIPLE so the inter-tap
        # delay matches the visual stack height.
        self.pending_tap_gap_ms = {key: TAP_MULTI_INTERNAL_GAP_MS for key in KEYS}
        self.last_hold_time = {key: 0.0 for key in KEYS}
        # Consecutive empty probe frames per key
        self.hold_empty_frames = {key: 0 for key in KEYS}
        # Post-tap fallback state
        self.post_tap_active_frames = {key: 0 for key in KEYS}

    def process_frame(self, frame_bgr):
        """
        Process the screen capture frame.
        Returns a dictionary of actions to take in format: { 'key_name': 'ACTION' }
        ACTION can be 'TAP', 'HOLD_START', 'HOLD_END', or 'HOLD_RESTART'.
        """
        actions = {}

        # Convert to HSV for better color thresholding
        hsv_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

        # Create masks for yellow (tap) and purple (hold)
        mask_tap = cv2.inRange(hsv_frame, TAP_COLOR_LOWER, TAP_COLOR_UPPER)
        mask_hold = cv2.inRange(hsv_frame, HOLD_COLOR_LOWER, HOLD_COLOR_UPPER)

        # Standard morphological opening to remove noise
        kernel = np.ones((5, 5), np.uint8)
        mask_tap = cv2.morphologyEx(mask_tap, cv2.MORPH_OPEN, kernel)
        mask_hold = cv2.morphologyEx(mask_hold, cv2.MORPH_OPEN, kernel)

        # Preserve pre-close mask for post-tap fallback
        mask_hold_open = mask_hold.copy()

        # Bridge gaps in hold trail so head+body+tail stay as one contour
        close_kernel = np.ones((15, 15), np.uint8)
        mask_hold_closed = cv2.morphologyEx(mask_hold_open, cv2.MORPH_CLOSE, close_kernel)

        # Combined mask for debugging display
        debug_mask = cv2.bitwise_or(mask_tap, mask_hold_closed)

        # Detect contours
        # TAP uses standard mask. HOLD uses the OPEN mask (pre-MORPH_CLOSE) so that
        # consecutive hold notes remain separate contours — the gray ring between
        # them survives in the open mask but gets bridged by the close kernel.
        contours_tap, _ = cv2.findContours(mask_tap, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours_hold, _ = cv2.findContours(mask_hold_open, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Filter and group contours by column
        col_notes = {i: [] for i in range(len(self.column_centers))}

        # Helper to categorize notes into columns with strict size filtering.
        # Each appended tuple is (x, y, w, h, note_type, n_taps). For HOLD,
        # n_taps is always 1 and is unused — it just keeps the tuple shape
        # uniform so unpacking works for both note types.
        def categorize_contours(contours_list, note_type, mask):
            for contour in contours_list:
                x, y, w, h = cv2.boundingRect(contour)

                roi = mask[y:y+h, x:x+w]
                pixel_area = cv2.countNonZero(roi)
                area = cv2.contourArea(contour)
                fill_ratio = pixel_area / area if area > 0 else 0

                n_taps = 1

                if note_type == 'TAP':
                    if not (50 < w < 220 and 15 < h < TAP_MAX_H):
                        continue
                    if fill_ratio < 0.55:
                        continue

                    # Classify by shape. A single coin is roughly square
                    # (h ≈ w). A merged double adds vertical extent without
                    # changing width — even when the overlap is so heavy that
                    # the aspect ratio barely moves, h-w is still a few px
                    # bigger than for a single. Use three signals (any wins):
                    #   - aspect ratio threshold (catches obvious tall stacks)
                    #   - absolute height threshold (catches unusual zooms)
                    #   - h-w pixel delta (catches very heavy overlaps)
                    aspect = h / w if w > 0 else 1.0
                    extra_h = h - w
                    if aspect >= TAP_TRIPLE_ASPECT or h >= TAP_TRIPLE_MIN_H:
                        n_taps = 3
                    elif (aspect >= TAP_DOUBLE_ASPECT
                          or h >= TAP_DOUBLE_MIN_H
                          or extra_h >= TAP_DOUBLE_MIN_EXTRA_H):
                        n_taps = 2
                    else:
                        n_taps = 1

                    # Only enforce circularity on shapes that actually look
                    # like a circle (aspect close to 1). Merged double/triple
                    # contours are peanut-shaped and would otherwise be
                    # rejected here, which is exactly the bug we just hit.
                    if n_taps == 1 and aspect < 1.15:
                        perimeter = cv2.arcLength(contour, True)
                        circularity = (4 * np.pi * area) / (perimeter ** 2) if perimeter > 0 else 0
                        if circularity < 0.45:
                            continue

                    hsv_roi = hsv_frame[y:y+h, x:x+w]
                    mean_hsv = cv2.mean(hsv_roi, mask=roi)
                    if mean_hsv[2] > 245 and mean_hsv[1] < 130:
                        continue

                elif note_type == 'HOLD':
                    if not (w > 30 and h > 30):
                        continue

                center_x = x + (w // 2)
                for i, cx in enumerate(self.column_centers):
                    if abs(center_x - cx) < self.col_width:
                        col_notes[i].append((x, y, w, h, note_type, n_taps))
                        break

        categorize_contours(contours_tap, 'TAP', mask_tap)
        categorize_contours(contours_hold, 'HOLD', mask_hold_open)

        # Process each column to decide actions
        for col_idx, notes_in_col in col_notes.items():
            key = KEYS[col_idx]
            current_t = time.time()

            # Sort notes by lowest y coordinate first (closest to bottom/hit line)
            notes_in_col.sort(key=lambda box: box[1] + box[3], reverse=True)

            for (x, y, w, h, note_type, n_from_shape) in notes_in_col:
                bottom_y = y + h

                # Check if the note bottom is within the correct hit window
                if note_type == 'TAP':
                    is_on_hit_line = (bottom_y >= self.hit_line_y - 30) and (bottom_y <= self.hit_line_y + 35)
                else:
                    post_tap_active = current_t - self.last_tap_time[key] < POST_TAP_WATCH_DURATION
                    upper_margin = POST_TAP_EXPANDED_WINDOW if post_tap_active else 30
                    is_on_hit_line = (bottom_y >= self.hit_line_y - upper_margin) and (bottom_y <= self.hit_line_y + 35)

                if is_on_hit_line:
                    action_taken = False

                    if note_type == 'TAP' and not self.holding_state[key]:
                        if current_t >= self.tap_lockout_until[key]:
                            # Look for SEPARATE TAP contours above this one in
                            # the same column. If a second coin is sitting up
                            # there waiting, it's a double — we can't rely on
                            # the single-tap cooldown to fire it later because
                            # the gap is often shorter than the cooldown.
                            my_bottom = bottom_y
                            neighbors_above = []
                            for (ox, oy, ow, oh, ot, _) in notes_in_col:
                                if ot != 'TAP':
                                    continue
                                if (ox, oy, ow, oh) == (x, y, w, h):
                                    continue  # same contour as 'me'
                                o_bottom = oy + oh
                                gap_px = my_bottom - o_bottom
                                if TAP_NEIGHBOR_MIN_GAP_PX < gap_px < TAP_NEIGHBOR_MAX_GAP_PX:
                                    neighbors_above.append(o_bottom)
                            neighbors_above.sort(reverse=True)  # closest first

                            n_from_neighbors = 1 + len(neighbors_above)
                            n_taps = min(3, max(n_from_shape, n_from_neighbors))

                            # Dynamic gap. Prefer the actual visual distance
                            # to the closest neighbor; fall back to the
                            # current contour's extra height (merged blob).
                            if n_taps >= 2 and neighbors_above:
                                visual_gap = my_bottom - neighbors_above[0]
                            elif n_taps >= 2:
                                visual_gap = max(0, h - w)
                            else:
                                visual_gap = 0

                            gap_ms = int(visual_gap * 1000.0 / TAP_FALL_SPEED_PX_PER_S)
                            if n_taps >= 2:
                                gap_ms = max(TAP_MULTI_GAP_MIN_MS,
                                             min(TAP_MULTI_GAP_MAX_MS, gap_ms))
                            else:
                                gap_ms = 0

                            aspect = h / w if w > 0 else 1.0
                            print(f"  [tap {key}] w={w} h={h} asp={aspect:.2f} "
                                  f"neigh={len(neighbors_above)} n_shape={n_from_shape} "
                                  f"n={n_taps} gap={gap_ms}ms")

                            if n_taps >= 3:
                                actions[key] = 'TAP_TRIPLE'
                                self.tap_lockout_until[key] = current_t + TAP_MULTI_LOCKOUT_S
                            elif n_taps == 2:
                                actions[key] = 'TAP_DOUBLE'
                                self.tap_lockout_until[key] = current_t + TAP_MULTI_LOCKOUT_S
                            else:
                                actions[key] = 'TAP'
                                self.tap_lockout_until[key] = current_t + TAP_SINGLE_LOCKOUT_S
                            self.pending_tap_gap_ms[key] = gap_ms
                            self.last_tap_time[key] = current_t
                            action_taken = True

                    elif note_type == 'HOLD':
                        if not self.holding_state[key]:
                            actions[key] = 'HOLD_START'
                            self.holding_state[key] = True
                            self.last_hold_time[key] = current_t
                            self.hold_empty_frames[key] = 0
                            action_taken = True

                    if action_taken:
                        break

            # Post-tap fallback: detect hold notes obscured by tap explosion
            if not self.holding_state[key] and key not in actions:
                if current_t - self.last_tap_time[key] < POST_TAP_WATCH_DURATION:
                    cx = self.column_centers[col_idx]
                    fb_x_start = max(0, cx - 50)
                    fb_x_end = min(mask_hold_open.shape[1], cx + 50)
                    fb_y_start = max(0, self.hit_line_y - 100)
                    fb_y_end = max(0, self.hit_line_y - 20)

                    if fb_y_end > fb_y_start:
                        fb_roi = mask_hold_open[fb_y_start:fb_y_end, fb_x_start:fb_x_end]
                        purple_pixels = cv2.countNonZero(fb_roi)
                        if purple_pixels > POST_TAP_PIXEL_THRESHOLD:
                            self.post_tap_active_frames[key] += 1
                            if self.post_tap_active_frames[key] >= POST_TAP_CONFIRM_FRAMES:
                                actions[key] = 'HOLD_START'
                                self.holding_state[key] = True
                                self.last_hold_time[key] = current_t
                                self.hold_empty_frames[key] = 0
                                self.post_tap_active_frames[key] = 0
                        else:
                            self.post_tap_active_frames[key] = 0
                else:
                    self.post_tap_active_frames[key] = 0

            # === Hold maintenance ===
            #
            # Closed-mask probe at the hit line determines whether the trail is
            # still present. When it empties for HOLD_RELEASE_FRAMES, the current
            # note has ended — at that point we look at the OPEN-mask contours
            # in this column. If a separate next-hold contour is sitting at the
            # hit line (its head is right at the line, body extends well above),
            # fire HOLD_RESTART. Otherwise the column is truly empty → HOLD_END.
            #
            if self.holding_state[key] and key not in actions:
                hold_age = current_t - self.last_hold_time[key]

                if hold_age < HOLD_MIN_TIME:
                    continue

                cx = self.column_centers[col_idx]
                img_h, img_w = mask_hold_closed.shape[:2]

                probe_x1 = max(0, cx - HOLD_PROBE_HALF_WIDTH)
                probe_x2 = min(img_w, cx + HOLD_PROBE_HALF_WIDTH)
                probe_y1 = max(0, self.hit_line_y - HOLD_PROBE_ABOVE)
                probe_y2 = min(img_h, self.hit_line_y + HOLD_PROBE_BELOW)

                closed_roi = mask_hold_closed[probe_y1:probe_y2, probe_x1:probe_x2]

                if cv2.countNonZero(closed_roi) == 0:
                    self.hold_empty_frames[key] += 1

                    if self.hold_empty_frames[key] >= HOLD_RELEASE_FRAMES:
                        # Probe is empty → no purple in [hit_line-60, hit_line+40].
                        # If a next-hold note is approaching, its head (bottom of
                        # contour) sits ABOVE the probe area (above hit_line-60),
                        # within visible range. Detect that contour to fire RESTART.
                        next_hold = None
                        for (nx, ny, nw, nh, ntype, _ntaps) in notes_in_col:
                            if ntype != 'HOLD':
                                continue
                            n_bottom = ny + nh
                            # Head must be above the probe (probe is empty)
                            # but not too far up — within ~250px of hit line.
                            if (self.hit_line_y - 250) <= n_bottom <= (self.hit_line_y - 20):
                                next_hold = (nx, ny, nw, nh)
                                break

                        if next_hold is not None:
                            actions[key] = 'HOLD_RESTART'
                            self.hold_empty_frames[key] = 0
                            self.last_hold_time[key] = current_t
                        else:
                            actions[key] = 'HOLD_END'
                            self.holding_state[key] = False
                            self.hold_empty_frames[key] = 0
                            self.post_tap_active_frames[key] = 0
                            self.last_hold_time[key] = 0.0
                else:
                    self.hold_empty_frames[key] = 0

        return actions, debug_mask
