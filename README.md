# Genshin Rhythm Bot

Automates Genshin Impact's **Repertoire of Myriad Melodies** rhythm minigame. A Python host watches the screen for falling notes and streams keystrokes to an Arduino Leonardo, which emits them as USB HID input the game accepts.

Two rhythm modes plus a built-in macro tool:
- **Standalone** — runs the rhythm detector indefinitely. Start it on any song; stop it whenever.
- **Album** — auto-plays an entire country album page (12 songs) at a chosen difficulty, with optional skip-on-Canorus.
- **Macros** — separate UI tab to record arbitrary keyboard + mouse sequences in-game and replay them through the same Arduino. 9 named save slots, inline event editor, auto-focus to the game on Play.

---

## Why an Arduino?

Mainly because I had an Arduino sitting around and wanted a project to use it for, plus an excuse to actually learn the platform end-to-end (firmware, HID, serial). The rhythm-bot use case fit perfectly.

But because the game accepts SendInput-style input fine, a **software-only mode** was also added as a parallel option that needs no hardware at all.

Pick whichever fits, you can switch at any time from the **Input** dropdown in the Main tab.

### Backend trade-offs

| Aspect | Arduino HID | Software (Win32 SendInput) |
|---|---|---|
| Hardware required | Leonardo + USB cable | None |
| Setup | Flash sketch, plug in board, find COM port | Just install the bot |
| Rhythm minigame keys (`a s d j k l`) | Works | Works |
| Album-mode menu clicks | Works (closed-loop HID convergence, ~75 ms per click target) | Works (single absolute `mouse_event` call, ~instant) |
| In-combat camera / aim / clicks | Works (what the Arduino was originally for) | **Blocked by anti-cheat** — synthetic mouse moves don't drive the in-game camera |
| Macro recording (capture) | Same — uses Win32 hooks regardless of backend | Same |
| Macro playback — keyboard | Works, fully external to Python | Works, but synthetic events are also seen by our own keyboard listener, so binding a UI hotkey to a key the macro itself uses can re-fire that hotkey during playback. The Arduino backend doesn't have this issue because events come from outside the Python process. |
| Macro playback — mouse buttons | Works | Works |
| Latency | +1 USB serial round-trip per command (sub-ms but real) | Sub-ms, in-process |
| Reliability if Genshin tightens menu-input filtering | Hardware HID still goes through | Would break |
| Portability | Tied to a specific COM port; needs the board | Pure software, runs anywhere admin keyboard hooks work |

Short version:

- **Rhythm bot (Standalone / Album) + macros that only drive menus or rhythm-minigame keys**: software mode is fine and simpler.
- **Macros that need anything inside combat** (camera turns, aim, attack clicks): stay on the Arduino backend.
- **Belt-and-suspenders against future anti-cheat changes**: Arduino backend has the safer ceiling — real HID is the hardest path to detect.

---

## Requirements

### Hardware
- **Arduino Leonardo** *(only if you want the Arduino backend)* — or any ATmega32u4 board (Pro Micro, Beetle, etc.). Regular Unos cannot do HID. **Software mode requires no hardware.**
- USB cable to connect the board to the PC (Arduino backend only).

### Software
- Windows 10 or 11.
- Genshin Impact at **1080p windowed-fullscreen or fullscreen**. Other resolutions are auto-scaled but were tested at 1080p.
- The **Hu Tao theme** equipped in the rhythm minigame. *(Cosmetic, ~600 in-game currency.)* The detector relies on this theme's opaque hit-line background — see [How detection works](#how-detection-works).
- Administrator rights when running the bot — Genshin's anti-cheat blocks keyboard hooks from non-elevated processes. The bundled `.exe` self-elevates via UAC; dev launches need to be started as admin manually.

---

## Setup

### 1. Flash the Arduino *(skip if you'll use software mode)*

If you only plan to use **software** input mode (set the Input dropdown to "Software (Win32 SendInput)" in the UI), skip this step entirely — no hardware is involved.

If you want the Arduino backend:

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

User settings (rebound hotkeys) persist in `%APPDATA%\GenshinRhythmBot\ui_settings.json`. Saved macros live in `%APPDATA%\GenshinRhythmBot\macros\macro_<n>.json` (frozen build) or `pc_client/macros/` (dev).

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

### Macros tab

Separate tab for recording/playing arbitrary input sequences through the Arduino. Use cases: any repetitive in-game routine the bot doesn't natively cover (commission turn-ins, shop dialogue mashing, fishing-cast loops, ...).

- **Record** captures every key press / mouse click while Genshin is the foreground window — alt-tabbing pauses capture. Stop with the same hotkey or button.
- **Play** focuses Genshin first (the bot already had focus, the macro tool didn't), waits 250 ms for the window switch, then streams events through HID with their original timing.
- **Slots 1-9** hold named saves. Click-mode toggle: `Load` reads a slot into the buffer, `Save` writes the buffer (prompts for a name; confirms before overwriting), `Rename` changes a slot's name in place, `Clear` deletes a slot. The slot whose macro is currently in memory gets a yellow ring.
- **Events editor** (card under hotkeys) — Show/hide toggles a table of every event in the current macro buffer. Edit time / device / key / event-type inline, `+ Add` a row, `×` delete a row. `Save` validates + sorts in Python and (if a slot is loaded) writes back to it in the same click. `Discard` reloads from the buffer.
- Bot mode and macro mode are mutually exclusive on the Arduino — starting one while the other is running prompts to stop the other first.

### Default hotkeys

| Action | Key |
|---|---|
| Bot start / stop | `F8` |
| Bot pause / resume | `F9` |
| Toggle debug viz | `F10` |
| Macro record / stop | `Y` |
| Macro play | `Mouse 4` |
| Macro stop playback | `Mouse 5` |
| Macro save (1-9 picker) | `U` |
| Macro load (1-9 picker) | `F11` |

Bot hotkeys fire while the **game window or the bot UI** is in the foreground. Macro hotkeys are stricter: **Genshin only**, so the macro tool can't accidentally arm/record while you're typing in another app. Rebind any of them from the UI (you don't need to start the bot first); rebind capture also accepts mouse buttons (left / right / middle / mouse 4 / mouse 5).

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
Verify the board is plugged in and the sketch is flashed. If multiple boards are connected, set `SERIAL_PORT` explicitly in `pc_client/config.py` instead of relying on auto-detect. Or switch the **Input** dropdown to **Software (Win32 SendInput)** to skip the hardware path entirely.

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
  controller.py             Arduino serial wrapper (Arduino backend)
  software_input.py         Win32 SendInput / mouse_event wrapper (software backend)
  config.py                 All tunable knobs
  calibrate.py              One-frame calibration overlay
  macro_tool.py             Standalone macro recorder (CLI only — UI has its own integrated macro tool)
  assets/                   Icon + 1080p UI templates for album mode
  web/                      HTML/CSS/JS for the UI
```