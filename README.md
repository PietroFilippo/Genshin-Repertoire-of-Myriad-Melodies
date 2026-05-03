# Genshin Rhythm Bot

Automates Genshin Impact's **Repertoire of Myriad Melodies** rhythm minigame. A Python host watches the screen for falling notes and streams keystrokes to an Arduino Leonardo, which emits them as USB HID input the game accepts.

Two modes:
- **Standalone** — runs the rhythm detector indefinitely. Start it on any song; stop it whenever.
- **Album** — auto-plays an entire country album page (12 songs) at a chosen difficulty, with optional skip-on-Canorus.

---

## Why an Arduino?

Mainly becauseI had an Arduino sitting around and wanted a project to use it for, plus an excuse to actually learn the platform end-to-end (firmware, HID, serial). The rhythm-bot use case fit perfectly.

Also, Genshin Impact's anti-cheat (mhyprot) ignores software-synthesized input — `SetCursorPos`, `SendInput`, and similar Windows APIs do nothing inside the game window. Real USB HID hardware gets through, so the Arduino acts as a hardware keyboard/mouse the bot drives over a serial link.

---

## Requirements

### Hardware
- **Arduino Leonardo** (or any ATmega32u4 board: Pro Micro, Beetle, etc.). Regular Unos cannot do HID.
- USB cable to connect the board to the PC.

### Software
- Windows 10 or 11.
- Genshin Impact at **1080p windowed-fullscreen or fullscreen**. Other resolutions are auto-scaled but were tested at 1080p.
- The **Hu Tao theme** equipped in the rhythm minigame. *(Cosmetic, ~600 in-game currency.)* The detector relies on this theme's opaque hit-line background — see [How detection works](#how-detection-works).
- Administrator rights when running the bot — Genshin's anti-cheat blocks keyboard hooks from non-elevated processes. The bundled `.exe` self-elevates via UAC; dev launches need to be started as admin manually.

---

## Setup

### 1. Flash the Arduino

1. Open `arduino/rhythm_controller/rhythm_controller.ino` in the Arduino IDE.
2. Select **Tools → Board → Arduino Leonardo** (or your specific ATmega32u4 board).
3. Select the right COM port under **Tools → Port**.
4. Click **Upload**.

The sketch parses three line-based commands from serial: `K:<code>:DOWN|UP` (keyboard), `M:<code>:DOWN|UP` (mouse button), `P:<dx>:<dy>` (relative mouse move).

### 2. Equip the Hu Tao theme

In Genshin's main menu, open the rhythm minigame ("Repertoire of Myriad Melodies") → equip the **Hu Tao theme**. The detector pixel-polls the blue channel at the hit line — the Hu Tao theme's high-blue background makes any note that crosses the hit line trivially detectable.

### 3. Install the bot

**Option A — Pre-built `.exe`** *(recommended for non-developers)*

1. Download `GenshinRhythmBot-vX.Y.Z.zip` from [Releases](https://github.com/PietroFilippo/genshin-Repertoire-of-Myriad-Melodies/releases).
2. Extract anywhere.
3. Double-click `Genshin Rhythm Bot.exe`. Accept the UAC prompt.

User settings (rebound hotkeys) persist in `%APPDATA%\GenshinRhythmBot\ui_settings.json`.

**Option B — From source**

```powershell
git clone https://github.com/PietroFilippo/genshin-Repertoire-of-Myriad-Melodies.git
cd genshin-Repertoire-of-Myriad-Melodies
python -m venv venv
venv\Scripts\activate
pip install opencv-python mss numpy pyserial pywebview pythonnet keyboard mouse pyinstaller
```

Run the UI:
```powershell
python pc_client\ui.py
```

(Run your terminal as Administrator first, or in-game hotkeys won't fire.)

### 4. Verify calibration *(optional)*

```powershell
python pc_client\calibrate.py
```

Auto-detects the game window, captures one frame, and shows an overlay with the hit-line and per-column sample dots. Confirm the green dots sit on the hit-line of an empty rhythm lane — if the layout looks right, you're calibrated. Press any key to close the window.

If your COM port is unusual (multiple Arduinos plugged in, etc.), set it explicitly in `pc_client/config.py`:

```python
SERIAL_PORT = 'COM7'   # or leave None for auto-detect
```

---

## Usage

Launch the bot, switch focus to Genshin, then either click **Start** in the UI or press the start hotkey.

### Modes

- **Standalone** - starts the rhythm detector, runs until you stop it. Use it on any song you've manually started.
- **Album** - clicks through one country album page (Mondstadt, Liyue, Inazuma, Fontaine, ...) for you. Pick a difficulty + song count and choose whether to replay songs already at Canorus rank.

  Open the country album page (not the All Albums grid), then press Start.

You can switch modes mid-run - the bot stops the current worker and starts a fresh one in the new mode. Switching out of Album mid-song asks for confirmation first because the current song progress is lost.

### Default hotkeys

| Action | Key |
|---|---|
| Start / Stop | `F8` |
| Pause / Resume | `F9` |
| Toggle debug viz | `F10` |

Hotkeys only fire while the **game window** or the **bot UI** is in the foreground. Rebind from the UI (you don't need to start the bot first).

### Debug visualization

Toggling debug opens an OpenCV preview window with the hit-line, sample dots, blue-channel readout per column, and FPS. Use it while tuning thresholds.

---

## How detection works

The bot polls a single pixel (well, an asymmetric vertical strip of 4 pixels) at the hit line of each of the 6 lanes, every 5 ms, in a dedicated thread per lane. The Hu Tao theme's hit-line background reads ≥230 on the blue channel. Any falling note (tap or hold) is opaque enough to drop the blue channel below the configured threshold (default 220).

- All 4 strip samples dark → key down.
- Any sample bright → key up.

Holds sustain the dark pixel; consecutive holds create a dark-bright-dark sequence. Taps are a brief flash. One rule covers every note type — no shape recognition, no contour analysis, no per-tap heuristics.

The previous engine used HSV masking + contour detection on the default (water/blue) theme. It worked, but burned ~400 lines of edge-case heuristics on flickering glow effects. The Hu Tao theme reduced the whole problem to a binary sensor.

### Known limitation: the "Hard Notes" modifier

Hard Notes shrinks every note (smaller flowers, longer hold tails). The bot handles the official album charts at every difficulty (Normal / Hard / Pro / Legendary) reliably, but **community-made hard maps** that combine Hard Notes with very dense note charts can confuse the detector — the longer hold tails leave the hit-line strip dark for longer than the gap between notes, so consecutive presses occasionally read as one sustained hold and the bot drops the second tap.

**Workaround**: bump the in-game **note speed to 2x–3x** when playing community hard maps. Faster notes spend less time crossing the hit-line strip, which shrinks the merge window and noticeably cuts misses. If you're farming the official albums (which is what Album mode is designed for), faster speed should also help if the program is missing.

---

## Troubleshooting

**Hotkeys do nothing while Genshin is focused.**
You're not running as administrator. Genshin's anti-cheat blocks keyboard hooks from non-elevated processes. The `.exe` always self-elevates; if you launched from a terminal, restart the terminal as administrator.

**The bot misses notes / fires randomly.**
Re-check that the **Hu Tao theme** is equipped on the rhythm minigame. The water/blue default theme will never work with this detector. Run `calibrate.py` and check the per-column blue readouts — they should be ≥230 when no note is present. If not, check if you're using the hard notes modifier, if so, try bumping the in-game note speed to 2x-3x.

**`Failed to connect to Arduino on COMx`.**
Verify the board is plugged in and the sketch is flashed. If multiple boards are connected, set `SERIAL_PORT` explicitly in `pc_client/config.py` instead of relying on auto-detect.

**`pythonnet` / WinForms error on `.exe` first launch.**
The bundled `.exe` strips Mark-of-the-Web from its DLLs on first launch (downloaded zips inherit MOTW, which the .NET CLR refuses to load assemblies from). If you see a CLR error on the very first run, try running the `.exe` once more — the strip happens before the second launch.

**Album mode sits doing nothing.**
You're probably on the *All Albums* grid, not inside a country album page. Click into one (Mondstadt / Liyue / Inazuma / Fontaine / ...) so the "Go Perform" pill is visible bottom-right, then press Start.

---

## Building releases

```powershell
venv\Scripts\activate
python -m PyInstaller pc_client\ui.py `
  --name "Genshin Rhythm Bot" `
  --windowed `
  --icon pc_client\assets\icon.ico `
  --uac-admin `
  --add-data "pc_client\assets;assets" `
  --add-data "pc_client\web;web" `
  --noconfirm --clean
```

Output: `dist\Genshin Rhythm Bot\` (`.exe` + bundled `_internal\`). Zip the onedir folder for distribution.

---

## Project layout

```
arduino/
  rhythm_controller/        Serial → HID firmware (Leonardo)
pc_client/
  ui.py                     PyWebView front-end (.exe entrypoint)
  ui_core.py                BotController, KeybindManager, settings
  main.py                   Standalone-mode engine + CLI
  album.py                  Album auto-runner + CLI
  detector.py               Per-key pixel-polling rhythm detector
  controller.py             Arduino serial wrapper
  config.py                 All tunable knobs
  calibrate.py              One-frame calibration overlay
  macro_tool.py             Standalone macro recorder (CLI only)
  assets/                   Icon + 1080p UI templates for album mode
  web/                      HTML/CSS/JS for the UI
```