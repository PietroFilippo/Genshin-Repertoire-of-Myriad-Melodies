# pc_client/config.py

# Hardware config
# The COM port the arduino is connected to
SERIAL_PORT = 'COM7'
# Ensure this matches the arduino sketch setup
BAUD_RATE = 115200

# Gameplay config
# The keys assigned to the 6 columns from left to right
KEYS = ['a', 's', 'd', 'j', 'k', 'l']

# Reference positions for 1920x1080
# These are absolute pixel positions within the game client area at 1080p.
# At runtime they are scaled automatically to match the actual game resolution.
REF_WIDTH = 1920
REF_HEIGHT = 1080
REF_COLUMN_X = [417, 628, 844, 1061, 1277, 1493]
REF_HIT_LINE_Y = 921

# Game window title (used for auto-detection)
# Genshin uses "Genshin Impact"
GAME_WINDOW_TITLE = ["Genshin Impact"]

# Pixel-polling detection (Hu Tao theme)
# Blue channel threshold. With the Hu Tao theme, the hit-line background has
# high Blue values (~230-255) and notes passing through have low Blue (<200).
# A note is considered "present" when the sampled pixel's Blue channel drops
# below this value. Matches the BetterGenshinImpact C# check: c.B < 220.
PIXEL_THRESHOLD = 220

# Per-key polling interval. Each of the 6 lanes runs in its own thread and
# samples its pixel(s) every KEY_POLL_DELAY_S seconds. 5ms matches BGI.
# Note: Windows timer resolution must be boosted to 1ms (timeBeginPeriod) for
# sleeps below ~16ms to be accurate — main.py does this.
KEY_POLL_DELAY_S = 0.005

# Vertical strip of pixels sampled per key, as Y offsets relative to the hit
# line. Asymmetric thresholding:
#   KEY_DOWN fires when ALL samples are dark   (note covers full strip)
#   KEY_UP   fires when ANY sample  is bright  (gap touches strip anywhere)
# This widens the bright-window for inter-flower gaps, so very fast double
# taps are caught even when the visual gap between two stacked notes is brief.
#
# Center sample (dy=0) is intentionally skipped: the flower's bright
# yellow-white core has B near the threshold and reads "bright" while it
# crosses the hit line. With Hard Notes modifier (smaller flowers) that
# core-bright window is a large fraction of the note's transit time and
# can swallow every poll between dark-petal moments. Sampling petals only
# (dy in {-5,-3,3,5}) keeps every sample firmly under threshold for the
# entire time the note covers the strip. Setting this to [0] reverts to
# single-pixel (BGI-equivalent) behaviour.
Y_SAMPLE_OFFSETS = [-5, -3, 3, 5]

# Show OpenCV debug visualization window. Detection runs in threads and is
# independent of this flag — disable for max performance once tuned.
DEBUG_MODE = False

# === Album auto-runner (album.py) ===
# Difficulty for the rhythm task. One of:
#   'normal' (普通)  → click (480,  600)
#   'hard'   (困难)  → click (800,  600)
#   'master' (大师)  → click (1150, 600)
#   'pro'    (传说)  → click (1400, 600)   ← BGI's "Legend"; the highest tier
ALBUM_DIFFICULTY = 'pro'

# 1080p reference click coords for each difficulty button. Mirrored from
# BGI AutoAlbumTask.cs.
ALBUM_DIFFICULTY_COORDS = {
    'normal': (480,  600),
    'hard':   (800,  600),
    'master': (1150, 600),
    'pro':    (1400, 600),
}

# 1080p reference coord for the album-list "next song" arrow.
# Hardcoded by BGI as (310, 220).
ALBUM_NEXT_ARROW_XY = (310, 220)

# Template-match score threshold (cv2.TM_CCOEFF_NORMED) for static UI
# elements (album icon, list button, canorus, complete). 0.85 is solid.
ALBUM_MATCH_THRESHOLD = 0.85

# Looser threshold for the white Confirm button. The button's text and
# styling vary slightly between the song-detail and difficulty-confirm
# screens, so use a more permissive match.
ALBUM_WHITE_CONFIRM_THRESHOLD = 0.80

# End-of-song watcher poll interval (seconds). BGI uses 5s.
ALBUM_END_POLL_S = 5.0

# Skip-completed criterion. Mirrors BGI's MustCanorusLevel.
#   True  → skip song only if Canorus rank is achieved at the chosen
#           difficulty (per-difficulty canorus icon match).
#   False → skip song if all rewards are collected (album_music_complate),
#           regardless of difficulty.
ALBUM_USE_CANORUS_CHECK = True

# === Macro tool keybinds ===
# Keyboard bindings use `keyboard` package names ('y', 'f11', 'ctrl+m', ...).
# Mouse bindings use the 'mouse:<name>' prefix. Valid mouse names:
#   'left', 'right', 'middle', 'x' (Mouse 4), 'x2' (Mouse 5)
# Note: 'play' is idempotent — pressing it while a macro is already running
# is a no-op, so the play key can be safely spammed without cancelling the
# run. Use the 'stop' binding (or 'exit') to interrupt playback.
MACRO_HOTKEYS = {
    'record': 'y',
    'play':   'mouse:x',
    'stop':   'mouse:x2',
    'save':   'u',
    'load':   'f11',
    'exit':   'f12',
}
