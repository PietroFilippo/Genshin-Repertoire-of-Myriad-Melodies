import cv2
import numpy as np
import time
from config import COLUMN_CENTERS, KEYS, TAP_COLOR_LOWER, TAP_COLOR_UPPER, HOLD_COLOR_LOWER, HOLD_COLOR_UPPER, HIT_LINE_Y

class NoteDetector:
    def __init__(self):
        # Configuration
        self.column_centers = COLUMN_CENTERS
        self.hit_line_y = HIT_LINE_Y
        self.col_width = 120 # Widened width to dramatically increase leniENCY for off-center columns
        
        # State tracking
        self.holding_state = {key: False for key in KEYS}
        self.last_tap_time = {key: 0.0 for key in KEYS}
        self.last_hold_time = {key: 0.0 for key in KEYS}
        # Consecutive empty frame counter per key — requires multiple blank frames before releasing
        self.hold_empty_frames = {key: 0 for key in KEYS}

        
    def process_frame(self, frame_bgr):
        """
        Process the screen capture frame. 
        Returns a dictionary of actions to take in format: { 'key_name': 'ACTION' }
        ACTION can be 'TAP', 'HOLD_START', or 'HOLD_END'.
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
        
        # Bridge gaps in hold trail so head+body+tail stay as one contour
        # without this, the translucent trail gets eroded away and the head
        # circle becomes an orphaned small contour that fails size filters
        close_kernel = np.ones((15, 15), np.uint8)
        mask_hold = cv2.morphologyEx(mask_hold, cv2.MORPH_CLOSE, close_kernel)
        
        
        # Combined mask for debugging display
        debug_mask = cv2.bitwise_or(mask_tap, mask_hold)
        
        # Detect contours
        contours_tap, _ = cv2.findContours(mask_tap, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours_hold, _ = cv2.findContours(mask_hold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # Filter and group contours by column
        col_notes = {i: [] for i in range(len(self.column_centers))}
        
        # Helper to categorize notes into columns with strict size filtering
        def categorize_contours(contours_list, note_type, mask):
            for contour in contours_list:
                x, y, w, h = cv2.boundingRect(contour)
                
                # Explicitly destroys hollow explosion rings which have very few solid pixels inside them
                roi = mask[y:y+h, x:x+w]
                pixel_area = cv2.countNonZero(roi)
                area = cv2.contourArea(contour)
                fill_ratio = pixel_area / area if area > 0 else 0
                
                if note_type == 'TAP':
                    if not (50 < w < 220 and 15 < h < 150):
                        continue
                    # A perfect dense tap note has a fill_ratio near 0.8+. The hollow explosion ring is typically < 0.6
                    if fill_ratio < 0.70:
                        continue
                    
                    # Circularity filter
                    # A real tap note is a near-perfect circle (circularity ~0.75-1.0)
                    # The explosion starburst has protruding rays, dropping circularity to ~0.3-0.5
                    # Threshold at 0.45 to be lenient with notes partially clipped at edges
                    perimeter = cv2.arcLength(contour, True)
                    circularity = (4 * np.pi * area) / (perimeter ** 2) if perimeter > 0 else 0
                    if circularity < 0.45:
                        continue
                    
                    # Brightness/Saturation filter
                    # The explosion glow is much brighter (high V) and desaturated (low S)
                    # compared to the solid opaque yellow of a real note
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
                        col_notes[i].append((x, y, w, h, note_type))
                        break

        categorize_contours(contours_tap, 'TAP', mask_tap)
        categorize_contours(contours_hold, 'HOLD', mask_hold)
        
        # Process each column to decide actions
        for col_idx, notes_in_col in col_notes.items():
            key = KEYS[col_idx]
            current_t = time.time()
            
            # Sort notes by lowest y coordinate first (closest to bottom/hit line)
            notes_in_col.sort(key=lambda box: box[1] + box[3], reverse=True)
            
            for (x, y, w, h, note_type) in notes_in_col:
                bottom_y = y + h
                
                # Check if the note bottom is within the correct hit window
                if note_type == 'TAP':
                    # Wider window now that circularity/brightness/debounce filters handle explosion rejection.
                    # 65px window gives ~6-8 frames at 60fps for a falling note to register.
                    is_on_hit_line = (bottom_y >= self.hit_line_y - 30) and (bottom_y <= self.hit_line_y + 35)
                else:
                    is_on_hit_line = (bottom_y >= self.hit_line_y - 30) and (bottom_y <= self.hit_line_y + 35)
                
                if is_on_hit_line:
                    action_taken = False
                    
                    if note_type == 'TAP' and not self.holding_state[key]:
                        # 0.25s debounce for tap to outlast the ~200ms explosion animation
                        if current_t - self.last_tap_time[key] > 0.25:
                            actions[key] = 'TAP'
                            self.last_tap_time[key] = current_t
                            action_taken = True
                            
                    elif note_type == 'HOLD':
                        # Only trigger start from bounding boxes. End is tracked naturally by pixel trailing
                        if not self.holding_state[key]:
                            actions[key] = 'HOLD_START'
                            self.holding_state[key] = True
                            self.last_hold_time[key] = current_t
                            action_taken = True
                            
                    # Only skip remaining notes if its actually acted on this one
                    # A debounced/rejected tap must NOT block a hold note behind it
                    if action_taken:
                        break

            # Unconditionally verify if an active hold note has completely passed based on raw trailing pixels
            if self.holding_state[key]:
                # Enforce a minimum hold duration of 0.5s to prevent premature release
                # when the hold body is partially invisible in the mask
                if current_t - self.last_hold_time[key] < 0.5:
                    continue
                    
                cx = self.column_centers[col_idx]
                
                # Scan only a narrow band around the hit line for purple pixels
                # Scanning the full column would see the NEXT hold note descending
                # from above and never release the current hold. By limiting to
                # ~100px around the hit line, it detects when the current hold's
                # trail has fully passed through without interference from future notes
                look_x_start = max(0, cx - 50)
                look_x_end = min(mask_hold.shape[1], cx + 50)
                look_y_start = max(0, self.hit_line_y - 60)
                look_y_end = min(mask_hold.shape[0], self.hit_line_y + 40)
                
                roi = mask_hold[look_y_start:look_y_end, look_x_start:look_x_end]
                if cv2.countNonZero(roi) == 0:
                    # Require 5 consecutive empty frames before releasing
                    # A single glitchy frame won't prematurely drop the hold
                    self.hold_empty_frames[key] += 1
                    if self.hold_empty_frames[key] >= 5:
                        actions[key] = 'HOLD_END'
                        self.holding_state[key] = False
                        self.hold_empty_frames[key] = 0
                        # Reset hold time so a new hold at the hit line can
                        # start immediately on the very next frame
                        self.last_hold_time[key] = 0.0
                else:
                    # Reset counter when purple pixels are found
                    self.hold_empty_frames[key] = 0
                
        return actions, debug_mask
