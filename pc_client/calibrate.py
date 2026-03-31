import cv2
import mss
import numpy as np
import time
import re
import os

# States
STATE_TOP_LEFT = 0
STATE_BOTTOM_RIGHT = 1
STATE_HIT_LINE = 2
STATE_COLUMNS = 3  
STATE_TAP_COLOR = 4
STATE_HOLD_COLOR = 5
STATE_DONE = 6

state = STATE_TOP_LEFT
clicks_columns = []
top_left = None
bottom_right = None
hit_line_y = None
tap_color_hsv = None
hold_color_hsv = None

frame_hsv = None
original_frame = None

def mouse_callback(event, x, y, flags, param):
    global state, top_left, bottom_right, hit_line_y, clicks_columns, tap_color_hsv, hold_color_hsv
    if event == cv2.EVENT_LBUTTONDOWN:
        if state == STATE_TOP_LEFT:
            top_left = (x, y)
            state = STATE_BOTTOM_RIGHT
            
        elif state == STATE_BOTTOM_RIGHT:
            bottom_right = (x, y)
            state = STATE_HIT_LINE
            
        elif state == STATE_HIT_LINE:
            hit_line_y = y
            state = STATE_COLUMNS
            
        elif state == STATE_COLUMNS:
            clicks_columns.append(x)
            if len(clicks_columns) == 6:
                state = STATE_TAP_COLOR
                
        elif state == STATE_TAP_COLOR:
            tap_color_hsv = frame_hsv[y, x]
            state = STATE_HOLD_COLOR
            
        elif state == STATE_HOLD_COLOR:
            hold_color_hsv = frame_hsv[y, x]
            state = STATE_DONE

def update_config_file():
    # Calculate relative values based on absolute clicks
    top = min(top_left[1], bottom_right[1])
    left = min(top_left[0], bottom_right[0])
    width = abs(bottom_right[0] - top_left[0])
    height = abs(bottom_right[1] - top_left[1])
    
    rel_hit_line_y = hit_line_y - top
    rel_columns = [cx - left for cx in sorted(clicks_columns)]
    
    config_path = os.path.join(os.path.dirname(__file__), "config.py")
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    # Replace CAPTURE_REGION
    new_capture_region = f'''CAPTURE_REGION = {{
    "top": {top},    
    "left": {left},   
    "width": {width}, 
    "height": {height}  
}}'''
    content = re.sub(r'CAPTURE_REGION\s*=\s*\{.*?\}', new_capture_region, content, flags=re.DOTALL)
    
    # Replace COLUMN_CENTERS
    cols_str = ",\n    ".join([str(c) for c in rel_columns])
    new_column_centers = f'COLUMN_CENTERS = [\n    {cols_str}\n]'
    content = re.sub(r'COLUMN_CENTERS\s*=\s*\[.*?\]', new_column_centers, content, flags=re.DOTALL)
    
    # Replace HIT_LINE_Y
    content = re.sub(r'HIT_LINE_Y\s*=\s*\d+', f'HIT_LINE_Y = {rel_hit_line_y}', content)
    
    # Replace HSV Colors if captured
    if tap_color_hsv is not None:
        h, s, v = int(tap_color_hsv[0]), int(tap_color_hsv[1]), int(tap_color_hsv[2])
        t_low = f'TAP_COLOR_LOWER = ({max(0, h-10)}, {max(0, s-50)}, {max(0, v-50)})'
        t_up = f'TAP_COLOR_UPPER = ({min(179, h+10)}, {min(255, s+50)}, {min(255, v+50)})'
        content = re.sub(r'TAP_COLOR_LOWER\s*=\s*\([^)]+\)', t_low, content)
        content = re.sub(r'TAP_COLOR_UPPER\s*=\s*\([^)]+\)', t_up, content)
        
    if hold_color_hsv is not None:
        h, s, v = int(hold_color_hsv[0]), int(hold_color_hsv[1]), int(hold_color_hsv[2])
        h_low = f'HOLD_COLOR_LOWER = ({max(0, h-10)}, {max(0, s-50)}, {max(0, v-50)})'
        h_up = f'HOLD_COLOR_UPPER = ({min(179, h+10)}, {min(255, s+50)}, {min(255, v+50)})'
        content = re.sub(r'HOLD_COLOR_LOWER\s*=\s*\([^)]+\)', h_low, content)
        content = re.sub(r'HOLD_COLOR_UPPER\s*=\s*\([^)]+\)', h_up, content)
        
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    print("\nconfig.py updated successfully!")

def main():
    global original_frame, frame_hsv, state, clicks_columns, top_left, bottom_right, hit_line_y, tap_color_hsv, hold_color_hsv
    
    print("========================================")
    print("Genshin Repertoire of Myriad Melodies - Calibration")
    print("========================================")
    print("Please bring up Genshin Impact to the screen where the rhythm notes are visible.")
    print("To make it easier, open the interface styles in the settings.")
    print("Taking full-screen capture in:")
    for i in range(5, 0, -1):
        print(f"{i}...")
        time.sleep(1)
        
    with mss.mss() as sct:
        monitor = sct.monitors[1] # Primary monitor
        screenshot = sct.grab(monitor)
        # Convert to BGR array for OpenCV
        original_frame = np.ascontiguousarray(np.array(screenshot)[:, :, :3])
        frame_hsv = cv2.cvtColor(original_frame, cv2.COLOR_BGR2HSV)
        
    window_name = "Calibration - Press 's' to skip color selection, 'r' to restart, 'q' to quit"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cv2.setMouseCallback(window_name, mouse_callback)
    
    while True:
        display = original_frame.copy()
        
        # Draw current instructions
        msg = ""
        if state == STATE_TOP_LEFT:
            msg = "STEP 1: Click the Top-Left corner of the gameplay area."
        elif state == STATE_BOTTOM_RIGHT:
            msg = "STEP 2: Click the Bottom-Right corner of the gameplay area."
        elif state == STATE_HIT_LINE:
            msg = "STEP 3: Click anywhere on the Hit Line."
        elif state == STATE_COLUMNS:
            msg = f"STEP 4: Click the centers of the 6 columns ({len(clicks_columns)}/6 done)."
        elif state == STATE_TAP_COLOR:
            msg = "STEP 5: Click a yellow TAP NOTE to grab its color (Press 's' to skip)."
        elif state == STATE_HOLD_COLOR:
            msg = "STEP 6: Click a purple HOLD NOTE to grab its color (Press 's' to skip)."
        elif state == STATE_DONE:
            msg = " Press ENTER to save or 'r' to restart."
            
        # Add instruction background text
        cv2.rectangle(display, (10, 10), (1200, 70), (0, 0, 0), -1)
        cv2.putText(display, msg, (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        
        # Draw markings
        if top_left is not None:
            cv2.circle(display, top_left, 5, (0, 0, 255), -1)
        if bottom_right is not None:
            cv2.circle(display, bottom_right, 5, (0, 0, 255), -1)
        if top_left is not None and bottom_right is not None:
            cv2.rectangle(display, top_left, bottom_right, (0, 255, 0), 2)
            
        if hit_line_y is not None:
            # draw line across the screen at that Y
            cv2.line(display, (0, hit_line_y), (display.shape[1], hit_line_y), (0, 255, 255), 2)
            
        for cx in clicks_columns:
            cv2.line(display, (cx, 0), (cx, display.shape[0]), (255, 0, 0), 2)
            
        cv2.imshow(window_name, display)
        key = cv2.waitKey(10) & 0xFF
        
        if key == ord('q'):
            break
        elif key == ord('r'):
            # Reset state
            state = STATE_TOP_LEFT
            clicks_columns = []
            top_left = None
            bottom_right = None
            hit_line_y = None
            tap_color_hsv = None
            hold_color_hsv = None
        elif key == ord('s'):
            if state == STATE_TAP_COLOR:
                state = STATE_HOLD_COLOR
            elif state == STATE_HOLD_COLOR:
                state = STATE_DONE
        elif key == 13: # Enter
            if state == STATE_DONE:
                update_config_file()
                break

    cv2.destroyAllWindows()

if __name__ == '__main__':
    main()
