# pc_client/config.py

# Hardware config
# The COM port the arduino is connected to. Set to None for auto-detection
# (matches by USB VID for Arduino Leonardo / Pro Micro / common clones, then
# by description containing "Arduino"/"Leonardo"). Set to e.g. 'COM7' to
# force a specific port — useful if multiple Arduinos are connected.
SERIAL_PORT = None
# Ensure this matches the arduino sketch setup
BAUD_RATE = 115200

# Default input backend for first-launch / unset settings. 'software' uses
# SoftwareInputController which synthesizes input via Win32 SendInput /
# mouse_event — no hardware required, works for rhythm minigame + menu
# clicks (the only combat path it can't drive is in-combat camera/aim,
# blocked by anti-cheat). 'arduino' uses ArduinoHIDController (real USB
# HID via the Leonardo) and is the right pick for users who want combat
# macros or already have the board wired up. The UI persists this per-user
# in ui_settings.json — existing users keep whatever they last selected;
# only fresh installs see this default.
INPUT_BACKEND_DEFAULT = 'software'

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
# Difficulty target. Genshin's English UI uses:
#   'normal'    →  card center (465, 530)
#   'hard'      →  card center (790, 530)
#   'pro'       →  card center (1115, 530)
#   'legendary' →  card center (1440, 530)   ← highest tier
ALBUM_DIFFICULTY = 'legendary'

# 1080p click coords for the 4-card difficulty selection screen
# (post "Go Perform"). All at y=530 (card vertical center).
ALBUM_DIFFICULTY_COORDS = {
    'normal':    (465,  530),
    'hard':      (790,  530),
    'pro':       (1115, 530),
    'legendary': (1440, 530),
}

# 1080p click coord for advancing to the next song. Genshin's UI rotates
# the song wheel so the same on-screen slot always shows the next-up song;
# clicking this stable coord 12× iterates the whole album.
ALBUM_NEXT_SONG_XY = (280, 480)

# 1080p click coords for action buttons (bottom-bar pills).
ALBUM_GO_PERFORM_XY        = (1714, 1018)   # album page → diff selection
ALBUM_BEGIN_PERFORMANCE_XY = (1130, 1018)   # diff selection → rhythm

# Songs per album. Currently 12 across every album in-game.
ALBUM_SONG_COUNT = 12

# Template-match score threshold (cv2.TM_CCOEFF_NORMED). 0.85 is solid for
# clean UI text. Drop to 0.75 if matches misfire after a UI patch.
ALBUM_MATCH_THRESHOLD = 0.85

# End-of-song watcher poll interval (seconds). BGI uses 5s.
ALBUM_END_POLL_S = 5.0

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

# === UI hotkeys (ui.py) ===
# Defaults loaded when no ui_settings.json override is present. Names follow
# the `keyboard` package convention ('f8', 'ctrl+shift+a', etc.); mouse
# buttons use the 'mouse:<btn>' prefix where <btn> is one of left / right /
# middle / x / x2. Bot hotkeys (start_stop / pause / debug) fire when either
# Genshin or the UI is focused. Macro hotkeys fire only when Genshin is
# focused — the macro tool also captures input only inside the game.
UI_KEYBINDS_DEFAULT = {
    'start_stop': 'f8',   # toggle bot run / stop
    'pause':      'f9',   # pause / resume (releases keys, halts album clicker)
    'debug':      'f10',  # toggle OpenCV viz window in real time
    'macro_record': 'y',         # toggle record / stop-record
    'macro_play':   'mouse:x',   # start playback (idempotent while playing)
    'macro_stop':   'mouse:x2',  # stop playback
    'macro_save':   'u',         # arms 1-9 slot picker (4s) for save
    'macro_load':   'f11',       # arms 1-9 slot picker (4s) for load
}
