# Genshin Rhythm Bot

Automates Genshin Impact's **Repertoire of Myriad Melodies** rhythm minigame. A Python host watches the screen for falling notes and sends the keystrokes to the game — either directly through Windows (no hardware needed) or through an Arduino Leonardo, which emits them as USB HID input the game accepts.

Two rhythm modes plus a built-in macro tool:
- **Standalone** — runs the rhythm detector indefinitely. Start it on any song; stop it whenever.
- **Album** — auto-plays an entire country album page (12 songs) at your chosen difficulties, with optional skip-on-Canorus and a live progress bar.
- **Macros** — separate UI tab to record keyboard keys + mouse-button clicks in-game and replay them through the selected input method. 18 named save slots, three active-slot hotkeys, inline event editor, auto-focus to the game on Play.

The rhythm bot only presses keys while Genshin is the focused window — alt-tab away and it lets go of every lane key and waits until you click back into the game.

---

## Why an Arduino?

Mainly because I had an Arduino sitting around and wanted a project to use it for, plus an excuse to actually learn the platform end-to-end (firmware, HID, serial). The rhythm-bot use case fit perfectly.

But because the game accepts SendInput-style input fine for the rhythm minigame and menus, a **software-only mode** was also added as a parallel option that needs no hardware at all. You only need the Arduino if you want the extra ceiling it gives (combat input, hardware-HID resilience).

Pick whichever fits, you can switch at any time from the **Input** dropdown at the top of the window.

### Backend trade-offs

| Aspect | Arduino HID | Software (Win32 SendInput) |
|---|---|---|
| Hardware required | Leonardo + USB cable | None |
| Setup | Flash sketch, plug in board, find COM port | Just install the bot |
| Rhythm minigame keys (`a s d j k l`) | Works | Works |
| Album-mode menu clicks | Works (closed-loop HID convergence, ~75 ms per click target) | Works (single absolute `mouse_event` call, ~instant) |
| In-combat camera / aim / clicks | Works (what the Arduino was originally for) | **Blocked by anti-cheat** — synthetic mouse moves don't drive the in-game camera |
| Macro recording (capture) | Same — uses Win32 hooks regardless of backend | Same |
| Macro playback — keyboard | Works, fully external to Python (see [Arduino notes](#arduino-notes) for the supported keys) | Works, but synthetic events are also seen by our own keyboard listener, so binding a UI hotkey to a key the macro itself uses can re-fire that hotkey during playback. The Arduino backend doesn't have this issue because events come from outside the Python process. |
| Macro playback — mouse buttons | Left / right / middle (Mouse 4 / Mouse 5 are skipped) | All five |
| Latency | +1 USB serial round-trip per command (sub-ms but real) | Sub-ms, in-process |
| Reliability if Genshin tightens menu-input filtering | Hardware HID still goes through | Would break |
| Portability | Tied to a specific COM port; needs the board | Pure software, runs anywhere admin keyboard hooks work |

Short version:

- **Rhythm bot (Standalone / Album) + macros that only drive menus or rhythm-minigame keys**: software mode is fine and simpler.
- **Macros that need anything inside combat** (camera turns, aim, attack clicks): stay on the Arduino backend.
- **Belt-and-suspenders against future anti-cheat changes**: Arduino backend has the safer ceiling — real HID is the hardest path to detect.

### Arduino notes

- **Keys it can type:** letters, digits and US-layout punctuation, Shift / Ctrl / Alt / Windows, Enter / Esc / Tab / Space / Backspace, arrows, **F1–F24**, Insert / Home / End / Page Up / Page Down / Delete, Caps / Num / Scroll Lock, Print Screen, Pause and Menu. Anything else is skipped with one line in the Logs tab instead of being sent as a wrong key.
- **Mouse 4 / Mouse 5 can't be sent** (the Arduino mouse only has left, right and middle). Those events are skipped during macro playback — use the Software backend for macros that need side buttons. Binding Mouse 4 / 5 as *hotkeys* works on both backends.
- **Unplugged mid-run?** The Main tab says "Arduino disconnected". Plug it back in, press **Stop**, then **Start** to reconnect (for macros, just press Play again).

---

## Requirements

### Hardware
- **Arduino Leonardo** *(only if you want the Arduino backend)* — or any ATmega32u4 board (Pro Micro, Beetle, etc.). Regular Unos cannot do HID. **Software mode requires no hardware.**
- USB cable to connect the board to the PC (Arduino backend only).

### Software
- Windows 10 or 11.
- Genshin Impact at **1080p windowed-fullscreen or fullscreen**. Other resolutions are auto-scaled but were tested at 1080p. A windowed game can be moved or resized mid-run; the bot follows it.
- The **Hu Tao theme** equipped in the rhythm minigame. *(Cosmetic, ~600 in-game currency.)* The detector relies on this theme's opaque hit-line background — see [How detection works](#how-detection-works).
- **English game text** for Album mode (it finds the album buttons by their English labels).
- Administrator rights when running the bot — Genshin's anti-cheat blocks keyboard hooks from non-elevated processes. The bundled `.exe` self-elevates via UAC; dev launches need to be started as admin manually.

---

## Setup

### 1. Flash the Arduino *(optional — software mode is the default)*

The bot ships with the **software** input backend selected by default; you don't need to flash anything to get the rhythm bot, album auto-clicker, or menu-driven macros working.

Flash the Leonardo only if you want the Arduino backend (combat-input macros, or hardware-HID resilience):

1. Open `arduino/rhythm_controller/rhythm_controller.ino` in the Arduino IDE.
2. Select **Tools → Board → Arduino Leonardo** (or your specific ATmega32u4 board).
3. Select the right COM port under **Tools → Port**.
4. Click **Upload**.

The sketch parses three line-based commands from serial: `K:<code>:DOWN|UP` (keyboard), `M:<code>:DOWN|UP` (mouse button), `P:<dx>:<dy>` (relative mouse move), plus an optional upload-then-play protocol that lets the board time macro playback itself.

### 2. Equip the Hu Tao theme

In Genshin's main menu, open the rhythm minigame ("Repertoire of Myriad Melodies") → equip the **Hu Tao theme**. The detector pixel-polls the blue channel at the hit line — the Hu Tao theme's high-blue background makes any note that crosses the hit line trivially detectable.

### 3. Install the bot

**Option A — Pre-built `.exe`** *(recommended for non-developers)*

1. Download `GenshinRhythmBot-vX.Y.Z.zip` from [Releases](https://github.com/PietroFilippo/genshin-Repertoire-of-Myriad-Melodies/releases).
2. Extract anywhere.
3. Double-click `Genshin Rhythm Bot.exe`. Accept the UAC prompt.

User settings (hotkeys, input method, the Main-tab options) persist in `%APPDATA%\GenshinRhythmBot\ui_settings.json`. Saved macros live in `%APPDATA%\GenshinRhythmBot\macros\macro_<n>.json` (frozen build) or `pc_client/macros/` (dev).

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

The strip at the top of every tab shows the three things that block a run: admin rights, whether the game window was found, and the **Input** method. On first launch the Main tab also shows a one-time tip (equip the Hu Tao theme; Album mode needs English game text) — dismiss it with ×.

### Main tab

- **Mode** — Standalone or Album (see below). The album options dim while Standalone is selected.
- **Status** — Ready / Playing / Paused / Stopping, with **Start** (becomes **Stop** while running) and **Pause / Resume**. While running you also see the song, detector FPS, elapsed time and a lane strip where A S D J K L light up while the bot holds that key.
- **Problems** — if a run can't start or has to stop (game not found or minimized, not on an album page, a click that didn't land, Arduino not connected, ...), a notice explains what to fix, with a **View logs** link.
- **Debug overlay** — opens the [debug preview](#debug-visualization).
- **Hotkeys** — click a key cap to rebind it (see [Hotkeys](#hotkeys)).

Your mode, song count, difficulties and Canorus choice are remembered between launches.

### Modes

- **Standalone** - starts the rhythm detector, runs until you stop it. Use it on any song you've manually started.
- **Album** - clicks through one country album page (Mondstadt, Liyue, Inazuma, Fontaine, ...) for you. Pick any subset of difficulties with the four difficulty chips (at least one stays selected) + song count, and choose whether to replay songs already at Canorus rank.

  Open the country album page (not the All Albums grid), then press Start.

  **Difficulty subset behavior:**
  - **One selected** — classic single-difficulty run, plays each song once at that difficulty (12 songs total at default song count).
  - **Two or three selected** — each song-slot is played once per chosen difficulty before the wheel advances. Example: Normal + Hard → song 1 Normal → song 1 Hard → song 2 Normal → song 2 Hard → ... Order is always normal → hard → pro → legendary regardless of click order.
  - **All four selected** — equivalent to an "All" option (4 × 12 = 48 plays at default song count).
  - Canorus skip is always per-difficulty: a song already at Canorus on Normal is skipped just for that pass; the same song still plays on Hard if Hard is selected and not yet Canorus there.

  At 12 songs the wheel rolls back to song 1 when the run finishes; shorter runs leave it on the last song played. A progress bar shows each song as played, skipped (Canorus), failed or up next. Pause works at any point — mid-song it lets go of the keys, and during the menu clicks before a song it waits before the next click.

You can switch modes mid-run - the bot stops the current worker and starts a fresh one in the new mode. Switching out of Album mid-song asks for confirmation first because the current song progress is lost. Switching from a running Standalone into Album picks up from the song already playing; press Stop first to start from song 1.

### Macros tab

Separate tab for recording/playing input sequences through the selected input method. Use cases: any repetitive in-game routine the bot doesn't natively cover (commission turn-ins, shop dialogue mashing, fishing-cast loops, ...).

- **Record** captures key presses and mouse-button clicks while Genshin is the foreground window — **not** cursor movement or the scroll wheel. Alt-tabbing pauses capture (a key you were already holding still records its release). The event count updates live. Stop with the same hotkey or button.
- **Play** switches to Genshin first and refuses (with a message) if the game isn't running or can't be brought to the front, then replays the events with their original timing.
- **Saved macros (slots 1-18)** — hover a slot for **Load / Save / Rename / Clear**, or use **Save to slot…** to pick a slot and name it (it warns before overwriting, and if an active macro uses that slot). The slot your current macro came from gets a gold ring; A/B/C marks show which slots are assigned to active macros.
- **Active Macros** assigns up to three saved slots to independent Genshin-only playback hotkeys. Playback remains one-at-a-time; choose whether another active hotkey is ignored or interrupts the current macro.
- **Events editor** — Show/hide toggles a table of every event in the current macro. Edit time / device / key / event-type inline, `+ Add` a row, `×` delete a row. **Save changes** validates + sorts the events and (if a slot is loaded) writes back to it in the same click; it refuses instead of overwriting if a different macro was loaded or recorded in the meantime. Rows with an unknown key or button are dropped on save. `Discard` reverts your edits.
- **Unsaved work is protected.** Recording, loading or closing the app over a macro you haven't saved asks first. The in-game Record and Load hotkeys can't show a dialog, so over an unsaved macro the first press only warns you (with a short system sound) — press the same hotkey again within 4 seconds to discard it.
- The bot and macros never run at the same time — starting one while the other is running asks to stop the other first.

### Default hotkeys

| Action | Key |
|---|---|
| Bot start / stop | `F8` |
| Bot pause / resume | `F9` |
| Toggle debug viz | `F10` |
| Macro record / stop | `Y` |
| Macro play | `Mouse 4` |
| Macro stop playback | `Mouse 5` |
| Macro save (1-18 picker) | `U` |
| Macro load (1-18 picker) | `F11` |

### Hotkeys

Bot hotkeys fire while the **game window or the bot UI** is in the foreground. Macro hotkeys are stricter: **Genshin only**, so the macro tool can't accidentally arm/record while you're typing in another app.

- Hotkeys work while you're holding other keys (walking with W, or while the bot holds a lane key), and each press triggers **one** action — holding the key down doesn't repeat it.
- To rebind, click the key cap and press the new key or mouse button (right / middle / Mouse 4 / Mouse 5); you don't need to start the bot first. **Esc** or a **left click** cancels. All hotkeys are paused while the prompt is open, so trying F8 as a binding won't also stop the bot.
- **Left click can't be bound** (it's every attack and every album click), and `A S D J K L` on their own are refused for the bot controls because the bot presses them itself. A key the prompt can't use is refused with the reason, and your previous binding stays.

Since v1.5.5, keyboard hotkeys use non-suppressing listeners rather than exclusive Windows global registrations. For example, `F` may activate an assigned macro while Genshin is focused but remains usable in another application while the bot stays open. When upgrading from v1.5.4, close that old process once so Windows releases the key it registered globally; v1.5.5 will not claim it again.

For the save/load hotkey picker, press `1`–`9` for slots 1–9. Press `0`
first, then `1`–`9`, for slots 10–18. Esc or leaving the game cancels; the
digit you pick isn't passed on to the game (so it won't also switch party
members).

### Debug visualization

Toggling debug opens an OpenCV preview window with the hit-line, sample dots, blue-channel readout per column, FPS and a live key log. Use it while tuning thresholds. It stays on top but never takes focus from the game, so the bot keeps playing while it's open. Close it with its **X** button or by turning **Debug overlay** off (or `F10`); closing it only turns debug off, the bot keeps running.

---

## How detection works

The bot polls a single pixel (well, an asymmetric vertical strip of 4 pixels) at the hit line of each of the 6 lanes, every 5 ms, in a dedicated thread per lane. The Hu Tao theme's hit-line background reads ≥230 on the blue channel. Any falling note (tap or hold) is opaque enough to drop the blue channel below the configured threshold (default 220).

- All 4 strip samples dark → key down.
- Any sample bright → key up.

Holds sustain the dark pixel; consecutive holds create a dark-bright-dark sequence. Taps are a brief flash. One rule covers every note type — no shape recognition, no contour analysis, no per-tap heuristics.

A separate check pauses all key presses while Genshin isn't the foreground window, so notes never get typed into another app (a game on a second monitor is still visible to the detector).

The previous engine used HSV masking + contour detection on the default (water/blue) theme. It worked, but burned ~400 lines of edge-case heuristics on flickering glow effects. The Hu Tao theme reduced the whole problem to a binary sensor.

### Known limitation: the "Hard Notes" modifier

Hard Notes shrinks every note (smaller flowers, longer hold tails). The bot handles the official album charts at every difficulty (Normal / Hard / Pro / Legendary) reliably, but **community-made hard maps** that combine Hard Notes with very dense note charts can confuse the detector — the longer hold tails leave the hit-line strip dark for longer than the gap between notes, so consecutive presses occasionally read as one sustained hold and the bot drops the second tap.

**Workaround**: bump the in-game **note speed to 2x–3x** when playing community hard maps. Faster notes spend less time crossing the hit-line strip, which shrinks the merge window and noticeably cuts misses. If you're farming the official albums (which is what Album mode is designed for), faster speed should also help if the program is missing.

---

## Troubleshooting

**Hotkeys do nothing while Genshin is focused.**
The packaged app always self-elevates because Genshin can block low-level keyboard hooks from non-elevated processes. If you launched from source, restart the terminal as administrator. Keyboard bindings are deliberately non-suppressing and foreground-gated: a key such as `F` can trigger a macro in Genshin without being reserved from other applications while the bot stays open. Hotkeys are also paused while a rebind prompt is open. Check the Logs tab for bind errors if one particular key still fails.

**The bot is running but presses nothing.**
It only sends keys while Genshin is the focused window — click into the game. The Logs tab shows `game lost focus — pausing input` / `regained focus` when this happens.

**The bot misses notes / fires randomly.**
Re-check that the **Hu Tao theme** is equipped on the rhythm minigame. The water/blue default theme will never work with this detector. Run `calibrate.py` and check the per-column blue readouts — they should be ≥230 when no note is present. If not, check if you're using the hard notes modifier, if so, try bumping the in-game note speed to 2x-3x.

**"Input backend unavailable" / `Couldn't open COMx` / `No Arduino found`.**
Verify the board is plugged in and the sketch is flashed, and close the Arduino IDE's serial monitor (or anything else holding the port). If multiple boards are connected, set `SERIAL_PORT` explicitly in `pc_client/config.py` instead of relying on auto-detect. Or switch the **Input** dropdown to **Software** to skip the hardware path entirely.

**"Arduino disconnected" in the middle of a run.**
The USB link dropped. Reconnect the board, then press **Stop** and **Start** — the bot reconnects on the next start.

**The debug preview won't close with `q`.**
The preview no longer takes keyboard focus (so it can't pause the bot), which means it usually doesn't see `q`. Close it with its **X** button or the **Debug overlay** toggle / `F10`.

**`pythonnet` / WinForms error on `.exe` first launch.**
The bundled `.exe` strips Mark-of-the-Web from its DLLs on first launch (downloaded zips inherit MOTW, which the .NET CLR refuses to load assemblies from). If you see a CLR error on the very first run, try running the `.exe` once more — the strip happens before the second launch.

**Album mode says "Not on a country album page".**
You're probably on the *All Albums* grid, not inside a country album page. Click into one (Mondstadt / Liyue / Inazuma / Fontaine / ...) so the "Go Perform" pill is visible bottom-right, then press Start. Album mode also needs the game text set to English.

**Album mode stopped with a message about a click or the game window.**
Keep the game window fully on screen and not minimized while Album mode runs, open a country album page again, and press Start. **View logs** shows exactly which step failed.

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

Run the regression suite before packaging:

```powershell
venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
node tests\capture_guard.test.js
```

The second line (needs Node.js) checks the UI's hotkey-capture logic; the Python suite also runs it automatically when `node` is on PATH. To run only the UI / bot / macro / backend regression tests, use `-p "test_ux_*.py"`.

---

## Project layout

```
arduino/
  rhythm_controller/        Serial → HID firmware (Leonardo)
pc_client/
  ui.py                     PyWebView front-end (.exe entrypoint)
  ui_core.py                BotController, MacroController, KeybindManager, settings
  main.py                   Standalone-mode CLI entrypoint
  standalone_runner.py      Rhythm-runner orchestrator (NoteDetector + viz)
  album.py                  Album cycle orchestrator + CLI
  song_player.py            Per-song state machine (intro click chain + detector + end-watcher + pause/resume)
  screen_watcher.py         UI automation (grab, template match, click, cancellable waits)
  detector.py               Per-key pixel-polling rhythm detector
  game_window.py            Genshin window detection + 1080p coord scaling
  system_setup.py           Process-wide DPI awareness + 1ms multimedia timer
  controller.py             Arduino serial wrapper (Arduino backend)
  software_input.py         Win32 SendInput / mouse_event wrapper (software backend)
  macro_engine.py           Shared macro core (event buffer, auto-repeat, playback, slot I/O)
  macro_tool.py             Standalone CLI macro recorder
  config.py                 All tunable knobs
  calibrate.py              One-frame calibration overlay
  assets/                   Icon + 1080p UI templates for album mode
  web/                      HTML/CSS/JS for the UI
tests/
  test_ux_*.py              Regression tests for the backends, bot, hotkeys, macros and rhythm/album engine
  test_*.py                 Scheduled-macro transport, slot persistence, macro builders
  capture_guard.test.js     Node test for the hotkey-capture guard in web/app.js
```
