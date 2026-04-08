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
HOLD_COLOR_LOWER = (115, 30, 190)
HOLD_COLOR_UPPER = (140, 150, 255)

# === Closed Mask Probe (release detection) ===
# Uses mask_hold_closed (with MORPH_CLOSE gap bridging) — proven in pc_client2
HOLD_PROBE_HALF_WIDTH = 50          # horizontal half-width in pixels
HOLD_PROBE_ABOVE = 60               # pixels above hit line
HOLD_PROBE_BELOW = 40               # pixels below hit line
HOLD_RELEASE_FRAMES = 2             # consecutive zero-pixel frames before release/restart

# Hit animation protection: the game plays a ~250ms visual effect on hold press
# that wipes purple from the probe area. Gate ALL checking until this clears.
HOLD_MIN_TIME = 0.35                # seconds before any release check activates

# Hold restart gap timing (used by controller.py)
HOLD_RESTART_GAP_MS = 20

# Post-tap fallback detection (hold note after tap note)
POST_TAP_WATCH_DURATION = 0.4       # seconds after tap to apply fallback logic
POST_TAP_PIXEL_THRESHOLD = 80       # minimum purple pixels to detect approaching hold
POST_TAP_CONFIRM_FRAMES = 3         # consecutive frames required
POST_TAP_EXPANDED_WINDOW = 60       # expanded hit line window (pixels above)

# Show OpenCV preview window
# Set to False to increase performance once configured
DEBUG_MODE = True
