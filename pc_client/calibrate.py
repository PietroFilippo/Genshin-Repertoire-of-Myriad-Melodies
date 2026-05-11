# pc_client/calibrate.py
#
# Verification tool for the auto-detected game positions.
# No manual clicking required - it finds the game window automatically,
# captures a frame, and overlays the computed sample points so you can
# visually confirm they're aligned with the hit line and column centers.

import cv2
import mss
import numpy as np
import time
import sys

from game_window import find_game_window, get_game_geometry, auto_detect
from config import KEYS, REF_COLUMN_X, REF_HIT_LINE_Y, PIXEL_THRESHOLD


def main():
    print("========================================")
    print("Genshin Repertoire of Myriad Melodies - Auto-Calibration Verification")
    print("========================================")
    print("Make sure Genshin Impact is running and visible on screen.")
    print()

    # Auto-detect game window
    capture_region, column_centers, hit_line_y = auto_detect()
    print()

    print("Capturing frame in 3 seconds... switch to the game.")
    for i in range(3, 0, -1):
        print(f"{i}...")
        time.sleep(1)

    # Capture the game area
    with mss.mss() as sct:
        screenshot = sct.grab(capture_region)
        frame = np.ascontiguousarray(np.array(screenshot)[:, :, :3])

    # Draw overlay
    display = frame.copy()

    # Hit line
    cv2.line(display, (0, hit_line_y), (display.shape[1], hit_line_y), (0, 255, 255), 2)
    cv2.putText(display, f"Hit Line Y={hit_line_y}", (10, hit_line_y - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

    # Column centers and sample points
    for i, cx in enumerate(column_centers):
        cv2.line(display, (cx, 0), (cx, display.shape[0]), (255, 0, 0), 1)

        # Sample point
        cv2.circle(display, (cx, hit_line_y), 10, (0, 255, 0), -1)
        cv2.circle(display, (cx, hit_line_y), 10, (255, 255, 255), 2)

        # Blue channel value at sample point
        if 0 <= hit_line_y < frame.shape[0] and 0 <= cx < frame.shape[1]:
            blue_val = int(frame[hit_line_y, cx, 0])
            status = "NOTE" if blue_val < PIXEL_THRESHOLD else "idle"
            cv2.putText(display, f"{KEYS[i].upper()} B:{blue_val} ({status})",
                        (cx - 40, hit_line_y + 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    # Info panel
    cv2.rectangle(display, (10, 10), (500, 90), (0, 0, 0), -1)
    cv2.putText(display, f"Resolution: {capture_region['width']}x{capture_region['height']}",
                (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(display, f"Threshold: B < {PIXEL_THRESHOLD}  |  Press any key to close",
                (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

    # Show
    window_name = "Calibration Verification"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.imshow(window_name, display)
    print("\nVerification window open. Check that the green dots are on the hit line.")
    print("Press any key in the window to close.")
    cv2.waitKey(0)
    cv2.destroyAllWindows()
    print("Done.")


if __name__ == '__main__':
    main()
