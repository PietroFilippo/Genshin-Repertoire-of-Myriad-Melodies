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

# === Multi-tap detection ===
# Two taps in quick succession visually merge into one taller contour. We
# classify primarily by ASPECT RATIO (h/w) since width stays the same as one
# tap regardless of how many are stacked. Absolute height is a secondary
# fallback. Single coins have aspect ≈ 1.0.
TAP_MAX_H = 280                     # absolute upper height limit (filters junk)
TAP_DOUBLE_ASPECT = 1.18            # h/w above this → 2 stacked taps
TAP_TRIPLE_ASPECT = 2.00            # h/w above this → 3 stacked taps
TAP_DOUBLE_MIN_H = 105              # absolute h fallback for 2 stacked taps
TAP_TRIPLE_MIN_H = 195              # absolute h fallback for 3 stacked taps
# Pixel-based fallback: when single coins are nearly square (h ≈ w) and a
# merged double only adds a few pixels of vertical extent, the aspect ratio
# barely moves. This catches that case directly: h must be at least this many
# pixels taller than w to count as a stacked double.
TAP_DOUBLE_MIN_EXTRA_H = 3
# Neighbor search: when a TAP fires, look for another TAP contour above it in
# the same column within this vertical range. If found, treat the pair as a
# TAP_DOUBLE so the second one doesn't get blocked by the single-tap lockout.
TAP_NEIGHBOR_MIN_GAP_PX = 20        # ignore neighbors closer than this (self)
TAP_NEIGHBOR_MAX_GAP_PX = 150       # ignore neighbors farther than this
# Approximate note fall speed used to compute the right inter-tap gap from
# the visual stack height. ~300 px/sec at 60 fps ≈ 5 px/frame.
TAP_FALL_SPEED_PX_PER_S = 300
TAP_MULTI_GAP_MIN_MS = 60           # clamp lower bound on dynamic gap
TAP_MULTI_GAP_MAX_MS = 280          # clamp upper bound on dynamic gap
TAP_MULTI_INTERNAL_GAP_MS = 130     # default gap if dynamic calc unavailable
TAP_MULTI_LOCKOUT_S = 0.45          # cooldown after firing a multi-tap
TAP_SINGLE_LOCKOUT_S = 0.25         # cooldown after firing a single tap

# Show OpenCV preview window
# Set to False to increase performance once configured
DEBUG_MODE = False
