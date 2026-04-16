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
# Kept at 0.35 (100ms safety margin over the animation). Fast hold→tap and
# tap→hold→tap sequences are handled separately via HOLD_END_THEN_TAP, so
# we don't need to shrink this — and shrinking it caused long holds to
# release prematurely when the animation ran slightly long.
HOLD_MIN_TIME = 0.35                # seconds before any release check activates

# Hold restart gap timing (used by controller.py)
HOLD_RESTART_GAP_MS = 20
# Maximum pixel distance (above hit line) at which a next-hold contour
# triggers HOLD_RESTART instead of HOLD_END. 150px ≈ 326ms at 460px/s.
# Too large → false restarts on distant notes; too small → missed restarts.
HOLD_RESTART_MAX_DIST_PX = 150

# Post-tap fallback detection (hold note after tap note)
POST_TAP_WATCH_DURATION = 0.35      # seconds after tap to apply fallback logic
POST_TAP_PIXEL_THRESHOLD = 100      # minimum purple pixels to detect approaching hold
POST_TAP_CONFIRM_FRAMES = 4         # consecutive frames required
POST_TAP_EXPANDED_WINDOW = 30       # expanded hit line window (pixels above) — equal
                                    # to normal margin; expansion path is now neutralized
                                    # so slow follow-up holds aren't caught early.
# Post-tap fallback ROI bounds (pixels above hit line). Tighter ROI fires
# the hold closer to the actual hit line, avoiding "good" instead of "perfect".
POST_TAP_FALLBACK_ABOVE_HIGH = 60   # was hardcoded 100
POST_TAP_FALLBACK_ABOVE_LOW = 10    # was hardcoded 20

# === Multi-tap detection ===
# Two taps in quick succession visually merge into one taller contour. We
# classify primarily by ASPECT RATIO (h/w) since width stays the same as one
# tap regardless of how many are stacked. Absolute height is a secondary
# fallback. Single coins have aspect ≈ 1.0.
TAP_MAX_H = 280                     # absolute upper height limit (filters junk)
TAP_DOUBLE_ASPECT = 1.45            # h/w above this -> 2 stacked taps
TAP_TRIPLE_ASPECT = 2.00            # h/w above this -> 3 stacked taps
TAP_DOUBLE_MIN_H = 90               # absolute min h for 2 stacked taps
TAP_TRIPLE_MIN_H = 140              # absolute min h for 3 stacked taps
# To avoid firing on single coins with explosion glow, we only trust shape
# detection when the width remains coin-sized (~55-65px). Glow blobs are wider.
TAP_COIN_MIN_W = 50
TAP_COIN_MAX_W = 90
# Merged doubles have a peanut shape (low circularity ~0.60-0.80) while
# single coins with glow are elliptical (high circularity ~0.95+). This
# replaces the height check as the main discriminator for shape-based doubles.
TAP_DOUBLE_MAX_CIRC = 0.88
# Very-fast-double detection (merged contour, sub-aspect-threshold).
# When two coins overlap by more than ~half a coin, the merged blob's
# aspect stays below TAP_DOUBLE_ASPECT so the primary classifier misses
# it. Secondary check: contour is coin-width, moderately tall, non-
# circular, AND its top third carries coin-level pixel density. Single
# coins with motion trail fail the density test (trails taper).
# MIN_H=70 covers overlap down to ~50px (near full stack); below that
# the two coins are visually indistinguishable.
TAP_FAST_DOUBLE_MIN_H = 70
TAP_FAST_DOUBLE_MIN_ASPECT = 1.03   # real doubles asp~1.05; singles asp~1.00 even w/ halo
TAP_FAST_DOUBLE_MAX_CIRC = 0.85     # real doubles circ~0.82; singles circ~0.89
TAP_FAST_DOUBLE_TOP_DENSITY = 0.55  # top-third pixel density cutoff
# Neighbor search: when a TAP fires, look for another TAP contour above it in
# the same column within this vertical range. If found, treat the pair as a
# TAP_DOUBLE so the second one doesn't get blocked by the single-tap lockout.
TAP_NEIGHBOR_MIN_GAP_PX = 20        # ignore neighbors closer than this (self)
TAP_NEIGHBOR_MAX_GAP_PX = 140       # ignore neighbors farther than this (~304ms at 460px/s)
TAP_NEIGHBOR_MIN_H = 40
# Reject neighbor contours whose bottom edge is within this many pixels of
# the hit line. Explosion artifacts linger ~30px around the hit line; real
# stacked-double second taps sit 60-100px above.
TAP_NEIGHBOR_HIT_LINE_EXCLUSION = 40
# Note fall speed used to compute the right inter-tap gap from the visual
# stack height. Measured empirically: 234 px traversed in 0.508 s ≈ 460 px/s.
# Only affects multi-tap gap calculation (detector.py inside the TAP firing
# branch); single taps, holds, and hit-line tolerances are unaffected.
TAP_FALL_SPEED_PX_PER_S = 460
TAP_MULTI_GAP_MIN_MS = 45           # clamp lower bound on dynamic gap.
                                    # Must stay > TAP_UP_DELAY_MS + ~1 game
                                    # frame (16.7ms at 60 Hz) so the release
                                    # window between DOWN/UP/DOWN/UP is wide
                                    # enough for the game to see two taps,
                                    # not one held press.
TAP_MULTI_GAP_MAX_MS = 280          # clamp upper bound on dynamic gap
TAP_MULTI_INTERNAL_GAP_MS = 130     # default gap if dynamic calc unavailable
# Duration a tap key is held DOWN before UP fires. Shorter = wider release
# window between consecutive taps in tap_multi (critical for the game to
# register both keystrokes instead of merging into one long press). 25ms
# is longer than a 60 Hz game frame so single taps register reliably,
# while leaving >16ms release gap at TAP_MULTI_GAP_MIN_MS=45.
TAP_UP_DELAY_MS = 25
TAP_MULTI_LOCKOUT_S = 0.25          # cooldown after firing a multi-tap
TAP_SINGLE_LOCKOUT_S = 0.32         # cooldown after firing a single tap; must
                                    # outlast the yellow explosion effect (~270ms)
# Pending-double window: after a single TAP fires, briefly allow a SECOND
# coin-shaped contour arriving at hit line to bypass the single-tap lockout
# (fast doubles whose gap is too wide for in-frame neighbor detection but
# whose second coin reaches hit line well before the 320ms lockout expires).
# Self-retrigger from the fired coin's own descending trail is blocked via
# trail-position check (see detector.py).
PENDING_DOUBLE_WINDOW_S = 0.30
# Shape gates for the bypass-fire contour. Tight on purpose — explosion
# artifacts must not masquerade as fresh coins.
PENDING_DOUBLE_MIN_CIRC = 0.75
PENDING_DOUBLE_TRAIL_TOLERANCE_PX = 30
POST_HOLD_END_TAP_LOCKOUT_S = 0.05  # just enough for serial flush; tap-after-hold
                                    # is now mostly handled by HOLD_END_THEN_TAP

# Show OpenCV preview window
# Set to False to increase performance once configured
DEBUG_MODE = True
