# pc_client/config.py

# Hardware config
# The COM port the arduino is connected to
SERIAL_PORT = 'COM5'
# Ensure this matches the arduino sketch setup
BAUD_RATE = 115200 

# Screen capture config (1920x1080)
# Adjusted region to cut out the top UI and focus purely on the note dropping area
CAPTURE_REGION = {
    "top": 43,    
    "left": 239,   
    "width": 1367, 
    "height": 1025  
}

# Gameplay config
# The keys assigned to the 6 columns from left to right
KEYS = ['a', 's', 'd', 'j', 'k', 'l']

# Calibrated X-coordinates relative to the left edge of CAPTURE_REGION (which is at X=450)
COLUMN_CENTERS = [
    181,
    398,
    614,
    830,
    1049,
    1265
]

# The Y-coordinate (relative to the CAPTURE_REGION's top edge) of the "Hit Line"
# Absolute Y is ~875. Relative to top=300 is 575.
HIT_LINE_Y = 877

# opencv config
# Thresholding (HSV) lower and upper bounds for notes
# OpenCV HSV ranges: H is 0-179, S is 0-255, V is 0-255

# Yellow notes (Tap)
TAP_COLOR_LOWER = (10, 138, 189)
TAP_COLOR_UPPER = (30, 238, 255)

# Purple notes (Hold)
HOLD_COLOR_LOWER = (117, 37, 200)
HOLD_COLOR_UPPER = (137, 137, 255)

# Delay introduced manually to simulate human reaction time variance (milliseconds)
HUMANIZATION_MIN_LATENCY = 10 
HUMANIZATION_MAX_LATENCY = 30

# Show OpenCV preview window
# Set to False to increase performance once configured
DEBUG_MODE = True
