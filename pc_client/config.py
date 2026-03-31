# pc_client/config.py

# Hardware config
# The COM port the arduino is connected to
SERIAL_PORT = 'COM5'
# Ensure this matches the arduino sketch setup
BAUD_RATE = 115200 

# Screen capture config (1920x1080)
# Adjusted region to cut out the top UI and focus purely on the note dropping area
CAPTURE_REGION = {
    "top": 44,    
    "left": 201,   
    "width": 1421, 
    "height": 988  
}

# Gameplay config
# The keys assigned to the 6 columns from left to right
KEYS = ['a', 's', 'd', 'j', 'k', 'l']

# Calibrated X-coordinates relative to the left edge of CAPTURE_REGION (which is at X=450)
COLUMN_CENTERS = [
    219,
    434,
    651,
    867,
    1085,
    1301
]

# The Y-coordinate (relative to the CAPTURE_REGION's top edge) of the "Hit Line"
# Absolute Y is ~875. Relative to top=300 is 575.
HIT_LINE_Y = 877

# opencv config
# Thresholding (HSV) lower and upper bounds for notes
# OpenCV HSV ranges: H is 0-179, S is 0-255, V is 0-255

# Yellow notes (Tap)
TAP_COLOR_LOWER = (10, 138, 188)
TAP_COLOR_UPPER = (30, 238, 255)

# Purple notes (Hold)
HOLD_COLOR_LOWER = (116, 77, 197)
HOLD_COLOR_UPPER = (136, 177, 255)

# Delay introduced manually to simulate human reaction time variance (milliseconds)
HUMANIZATION_MIN_LATENCY = 10 
HUMANIZATION_MAX_LATENCY = 30

# Show OpenCV preview window
# Set to False to increase performance once configured
DEBUG_MODE = True
