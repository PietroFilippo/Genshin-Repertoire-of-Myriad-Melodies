import cv2
import mss
import numpy as np
import time
import ctypes

from config import CAPTURE_REGION, HIT_LINE_Y, COLUMN_CENTERS, DEBUG_MODE
from detector import NoteDetector
from controller import ArduinoHIDController

def main():
    print("Initializing program")
    
    # Initialize Serial Controller
    controller = ArduinoHIDController()
    
    # Initialize Vision pipeline
    detector = NoteDetector()
    
    # Store recent logs for on-screen display
    recent_logs = ["Waiting for notes"]
    
    # Frame time tracking for FPS calculation
    last_time = time.time()
    
    with mss.mss() as sct:
        monitor = CAPTURE_REGION
        
        print("Starting main loop. Press 'q' in the OpenCV window to exit.")
        
        if DEBUG_MODE:
            cv2.namedWindow("Vision Context", cv2.WINDOW_NORMAL)
            cv2.setWindowProperty("Vision Context", cv2.WND_PROP_TOPMOST, 1)
            cv2.moveWindow("Vision Context", 0, 0)
            
            cv2.namedWindow("Color Threshold Mask", cv2.WINDOW_NORMAL)
            cv2.setWindowProperty("Color Threshold Mask", cv2.WND_PROP_TOPMOST, 1)
            cv2.moveWindow("Color Threshold Mask", 0, 400)
        
        while True:
            # Force terminal console to stay on top regardless of debug mode
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 3)

            # Grab high-speed screen snippet
            screenshot = sct.grab(monitor)
            
            # Convert direct to OpenCV BGR format
            frame = np.ascontiguousarray(np.array(screenshot)[:, :, :3]) 

            # Process the image to find notes and necessary actions
            actions, debug_mask = detector.process_frame(frame)
            
            # Execute actions onto the arduino
            for key, action in actions.items():
                log_text = f"Key {key.upper()}: {action}"
                print(f"[{time.time():.3f}] {log_text}")
                
                recent_logs.append(log_text)
                if len(recent_logs) > 5:
                    recent_logs.pop(0)
                
                # Actually send to arduino here
                if action == 'TAP':
                    controller.tap_key(key)
                elif action == 'HOLD_START':
                    controller.hold_start(key)
                elif action == 'HOLD_END':
                    controller.hold_end(key)

            # Debug overlay
            if DEBUG_MODE:
                # Draw hit line
                cv2.line(frame, (0, HIT_LINE_Y), (monitor["width"], HIT_LINE_Y), (0, 0, 255), 2)
                
                # Draw column indicators
                for cx in COLUMN_CENTERS:
                    cv2.line(frame, (cx, 0), (cx, monitor["height"]), (255, 0, 0), 1)
                
                # Calculate FPS
                curr_time = time.time()
                fps = 1.0 / (curr_time - last_time) if curr_time - last_time > 0 else 0
                last_time = curr_time
                cv2.putText(frame, f"FPS: {fps:.1f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

                # Draw the logs natively onto the frame
                for i, log_str in enumerate(recent_logs):
                    cv2.putText(frame, log_str, (10, 80 + i * 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)

                # Resize to physically fit to the LEFT of the X=450 capture region to prevent mirror effect
                scale_factor = 400 / monitor["width"]
                target_height = int(monitor["height"] * scale_factor)
                
                cv2.setWindowProperty("Automata Vision Context", cv2.WND_PROP_TOPMOST, 1)
                preview = cv2.resize(frame, (400, target_height))
                cv2.imshow("Automata Vision Context", preview)
                
                cv2.setWindowProperty("Color Threshold Mask", cv2.WND_PROP_TOPMOST, 1)
                mask_preview = cv2.resize(debug_mask, (400, target_height))
                cv2.imshow("Color Threshold Mask", mask_preview)

                # Break conditions
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
    controller.close()
    cv2.destroyAllWindows()
    print("Shutdown complete")

if __name__ == '__main__':
    main()
