# Genshin Rhythm Bot

Automates Genshin Impact's **Repertoire of Myriad Melodies** rhythm minigame. It watches the screen for notes and presses the keys for you, either straight through Windows (no hardware needed) or through an Arduino Leonardo acting as a real USB keyboard and mouse.

- **Standalone** plays whatever song you have open until you stop it.
- **Album** plays a whole country album page (up to 12 songs) at the difficulties you pick. It can skip songs already at Canorus and shows a live progress bar.
- **Macros** records key presses and mouse-button clicks in-game and replays them. You get 18 named save slots, three "active macro" hotkeys and an inline event editor.

The bot only presses keys while Genshin is the focused window. If you alt-tab away it releases every lane key and waits until you click back into the game.

## Requirements

- **Windows 10 or 11, run as administrator.** Genshin's anti-cheat blocks keyboard hooks from non-elevated apps. The `.exe` asks through UAC. From source, start your terminal as admin.
- **Genshin at 1080p**, fullscreen or windowed-fullscreen. Other resolutions are scaled automatically but were only tested at 1080p. A windowed game can be moved or resized mid-run.
- **Hu Tao theme** equipped in the rhythm minigame (cosmetic, ~600 in-game currency). The detector relies on its solid hit-line background and does not work with the default theme.
- **English game text** for Album mode, because it finds the album buttons by their English labels.
- *Optional:* an **Arduino Leonardo** or another ATmega32u4 board (Pro Micro, Beetle, ...) plus a USB cable. You only need it for the Arduino input method. An Uno can't act as a USB keyboard.

## Install

1. Download `GenshinRhythmBot-vX.Y.Z.zip` from [Releases](https://github.com/PietroFilippo/Genshin-Repertoire-of-Myriad-Melodies/releases) and extract it anywhere.
2. Run `Genshin Rhythm Bot\Genshin Rhythm Bot.exe` and accept the UAC prompt.
3. In the game, open the rhythm minigame and equip the Hu Tao theme.

Settings and saved macros are stored in `%APPDATA%\GenshinRhythmBot`: `ui_settings.json` holds hotkeys, input method, Main-tab options, theme and window size, and `macros\` holds the saved macros. Because they live there, they carry over when you upgrade: extract the new zip and run it.

## Input method: Software or Arduino

Choose with the **Input** dropdown at the top of the window. You can switch at any time; if the bot or a macro is running, it asks to stop them first. **Software** is the default.

| | Software (synthetic Windows input) | Arduino HID |
|---|---|---|
| Hardware | None | Leonardo / ATmega32u4 + USB cable, flashed once |
| Rhythm keys and Album menu clicks | Works | Works |
| In-combat camera, aim and attacks | **Blocked by anti-cheat** | Works |
| Macro mouse buttons | All five | Left / right / middle (Mouse 4/5 skipped) |
| Replayed keys re-triggering your own macro hotkeys | Can happen, so bind hotkeys to keys the macro doesn't use | Not affected |
| If Genshin ever filters synthetic menu input | Would break | Real USB input still works |

Software is fine for the rhythm bot and for macros that only use menus or minigame keys. Use the Arduino for macros that need combat input, or as the safer bet against future anti-cheat changes. (The Arduino path exists because I had one lying around and wanted to learn USB HID end to end. Software mode came later, so nobody needs the hardware.)

### Arduino setup and notes

1. Open the firmware in the Arduino IDE. In the release zip it's `Arduino Firmware\rhythm_controller\rhythm_controller.ino`; in the repo it's `arduino/rhythm_controller/`.
2. Pick **Tools → Board → Arduino Leonardo** (or your 32u4 board) and the board's port under **Tools → Port**, then click **Upload**.
3. Plug the board in and set **Input** to **Arduino HID**. The port is detected automatically. If several boards are connected, unplug the others. From source you can instead set `SERIAL_PORT = 'COM7'` in `pc_client/config.py`.

- **Firmware version:** the current firmware lets the board time macro playback itself, which is more precise. Older firmware still works; the app then times playback on the PC. Coming from v1.1.1 or earlier, reflash once to get board-timed playback.
- **Keys it can type:** letters, digits and US-layout punctuation, Shift / Ctrl / Alt / Windows, Enter / Esc / Tab / Space / Backspace, arrows, **F1–F24**, Insert / Home / End / Page Up / Page Down / Delete, Caps / Num / Scroll Lock, Print Screen, Pause and Menu. Any other key is skipped and logged in the Logs tab instead of being sent as the wrong key.
- **Mouse 4 / Mouse 5 can't be sent**, because the Arduino mouse only has three buttons. They're skipped during playback. Binding them as *hotkeys* still works with either input method.
- **If the board is unplugged mid-run**, the Main tab shows "Arduino disconnected". Plug it back in, then press **Stop** and **Start**. For a macro, press Play again.

## Using it

Launch the app, press **Start** (or `F8`) and switch to Genshin. The strip at the top of every tab shows what can block a run: admin rights, whether the game window was found, and the input method. If a run can't start or has to stop, a notice explains what to fix and links to the logs (**View logs**). Common causes are the game not found or minimized, not being on an album page, a click that didn't land, or no Arduino.

### Main tab

- **Standalone / Album** switch, plus the Album options (dimmed in Standalone): **Songs** (1–12), **Difficulty** chips (any combination, at least one) and **Canorus** (replay songs already at Canorus rank, or skip them).
- **Status**: Start/Stop and Pause/Resume, plus the current song, detector FPS and elapsed time. A lane strip lights up A S D J K L while the bot holds them, and in Album mode a progress bar shows each song as played, skipped (Canorus), failed or up next.
- **Debug overlay** switch and the bot **Hotkeys**.

Mode, songs, difficulties and the Canorus choice are remembered between launches. On first launch a one-time tip reminds you about the Hu Tao theme and English text; close it with ×.

### Album mode

Open a **country album page** (Mondstadt, Liyue, Inazuma, Fontaine, ...) so the "Go Perform" button shows bottom-right. The All Albums grid won't work. Then press Start.

- **One difficulty:** each song is played once.
- **Several difficulties:** each song is played at every chosen difficulty before the wheel moves on. The order is always Normal → Hard → Pro → Legendary, so a full album at all four difficulties is 48 plays.
- **Canorus skipping is per difficulty.** A song at Canorus on Normal still plays on Hard if it isn't Canorus there.
- **Song wheel:** a 12-song run rolls it back to song 1 at the end. Shorter runs leave it on the last song played.
- **Pause** works at any point. Mid-song it releases the keys; during the menu clicks it waits before the next one.
- **Switching modes mid-run:** leaving Album mid-song asks first, because the current song is aborted. Switching a running Standalone into Album picks up from the song that's playing; press Stop first to start from song 1.
- **Pointer precision:** Windows *Enhance pointer precision* is turned off during an Album run and restored afterwards.

### Macros tab

Use it for repetitive in-game routines the bot doesn't cover, such as dialogue mashing, turn-ins or fishing casts.

- **Record** captures key presses and mouse-button clicks while Genshin is focused. It doesn't record cursor movement or the scroll wheel. Alt-tabbing pauses capture, though keys you were holding still record their release. The event count updates live while you record. Press Record (or its hotkey) again to stop.
- **Play** switches to Genshin first, then replays the events with their original timing. If the game isn't running or can't be focused, it refuses and shows a message. **Stop** ends playback.
- **Saved macros (slots 1–18):** hover a slot for Load / Save / Rename / Clear, or use **Save to slot…** to pick a slot and name it. It warns before overwriting, including when an active macro plays that slot. A gold ring marks the slot the current macro came from, and A/B/C marks show which slots are used by active macros.
- **Active macros:** give up to three saved slots their own in-game hotkeys (unbound by default). Only one macro plays at a time. Choose whether another active hotkey is **ignored** or **interrupts** the current macro.
- **Events editor:** **Show** lists every event; edit time, device, key and type inline, add rows with **+ Add** and delete them with **×**. **Save changes** sorts by time and writes back to the loaded slot. It refuses if a different macro was loaded or recorded in the meantime, and drops rows with an unknown key or button. **Discard** reverts your edits.
- **Unsaved work is protected.** Recording, loading or closing the app over an unsaved macro asks first. The in-game Record and Load hotkeys can't show a dialog, so the first press only beeps; press the same hotkey again within 4 seconds to discard the macro.
- **One at a time:** the bot and macros never run together. Starting one asks to stop the other.

### Logs tab

Shows timestamped output from the bot, Album mode, macros and the Arduino. You can filter by source and use **Follow**, **Copy** and **Clear**. Check it first when something fails.

### Hotkeys

| Action | Default |
|---|---|
| Bot start / stop | `F8` |
| Bot pause / resume | `F9` |
| Debug overlay | `F10` |
| Macro record / stop | `Y` |
| Macro play | `Mouse 4` |
| Macro stop playback | `Mouse 5` |
| Macro save to slot | `U` |
| Macro load slot | `F11` |

- **Where they work:** bot hotkeys work while Genshin **or** the bot window is focused. Macro hotkeys, including active macros, only work inside Genshin.
- **Held keys:** hotkeys work while you're holding other keys, such as walking with W or while the bot holds a lane key. Each press triggers **one** action, and holding the key doesn't repeat it.
- **Save / Load picker:** press the hotkey, then `1`–`9` for slots 1–9, or `0` followed by `1`–`9` for slots 10–18. Esc, leaving the game or waiting 4 seconds cancels. The digit isn't passed on to the game, so it won't also switch party members.
- **Rebinding:** click a key cap, then press a key or a mouse button (right, middle, Mouse 4 or Mouse 5). The bot doesn't need to be running. **Esc** or a **left click** cancels, and all hotkeys are paused while the prompt is open.
- **Keys that can't be bound:** left click (it's every attack and every album click), and `A S D J K L` on their own for the bot controls (the bot presses them itself). Any other key the app can't use is refused with the reason, and your previous binding stays.
- **No system-wide reservation:** hotkeys don't reserve keys for the whole system. For example, `F` can trigger a macro inside Genshin and still type normally in other apps while the bot is open.

### Debug overlay

Turn on **Debug overlay** (or press `F10`) to get a preview window while the bot runs. It shows the hit line, the sample dots, a blue-channel reading per lane, FPS and a live key log. It stays on top without taking focus from the game, so the bot keeps playing. Close it with its **X**, the switch or `F10`. Closing it only turns debug off; the run continues.

### Themes and window size

- **Themes:** **Score sheet** (the default), **Light**, **Dark** or **Match Windows** (follows Windows' light/dark mode). Pick one with the **Theme** dropdown in the strip at the top. Your choice is remembered.
- **Resizing:** drag any edge or corner of the window (minimum 640 × 600). Double-click the title bar to maximize or restore; maximized keeps the taskbar visible. Size, position and the maximized state are remembered. If the saved position is off-screen, for example after unplugging a monitor, the window opens centred at its default size.
- Wider windows use the extra space: Album options sit in one row and the Macros tab gets two columns with a full-height events editor.

## How detection works

With the Hu Tao theme, the hit line has a bright, solid background (blue channel around 230 or higher). The bot samples a few pixels on the hit line of each of the six lanes every 5 ms. When a note covers them (blue drops below 220), it holds the key; when the background shows again, it releases it. That one rule covers taps, holds and back-to-back holds.

**Hard Notes modifier:** the official album charts work at every difficulty. Dense community maps with Hard Notes can confuse the detector, though: their longer hold tails can merge two quick presses into one hold. Raise the in-game **note speed to 2x–3x**. That also helps if the bot misses notes on official charts.

## Troubleshooting

- **Hotkeys do nothing in Genshin.** The app must run as administrator. The `.exe` always does; from source, use an admin terminal. Hotkeys are also paused while a rebind prompt is open. If a single key still fails, look for a bind error in the Logs tab.
- **The bot runs but presses nothing.** It only presses keys while Genshin is focused, so click into the game. The Logs tab shows `game lost focus — pausing input` / `regained focus` when this happens.
- **It misses notes or presses randomly.** Make sure the **Hu Tao theme** is equipped; the default theme never works. Turn on the debug overlay (or run `calibrate.py` from source): the dots should sit on the hit line and empty lanes should read 230 or higher. With Hard Notes, raise the note speed (see above).
- **"Input backend unavailable", `Couldn't open COMx` or "No Arduino found".** Check that the board is plugged in and flashed. Close the Arduino IDE's serial monitor or anything else using the port. Or switch **Input** to **Software**.
- **Album mode stops with "Not on a country album page" or a message about a click or the game window.** Open a country album page (not the All Albums grid) with the game text in English. Keep the game window fully on screen and not minimized, then press Start again. **View logs** shows which step failed.
- **pythonnet / .NET error when the `.exe` starts.** Windows marks files from downloaded zips, and the .NET loader refuses them. The app clears that mark from its own files at startup. If the error persists, right-click the zip, choose **Properties → Unblock**, and extract it again.

## Running from source

Run these in an **administrator** terminal with Python 3. Source runs keep settings and macros in `pc_client\` (`ui_settings.json`, `macros\`) instead of `%APPDATA%`.

```powershell
git clone https://github.com/PietroFilippo/Genshin-Repertoire-of-Myriad-Melodies.git
cd Genshin-Repertoire-of-Myriad-Melodies
python -m venv venv
venv\Scripts\activate
pip install opencv-python mss numpy pyserial pywebview pythonnet keyboard mouse pyinstaller
python pc_client\ui.py
```

**Calibration check:** `python pc_client\calibrate.py` finds the game window, grabs one frame and draws the hit line with green sample dots. The dots should sit on the hit line of an empty lane. Press any key to close it. Thresholds, `SERIAL_PORT` and the default hotkeys live in `pc_client/config.py`.

**Tests:** the Node.js test checks the UI's hotkey-capture logic, and the Python suite also runs it automatically when `node` is on PATH. Use `-p "test_ux_*.py"` to run only the UI, bot, macro and backend tests.

```powershell
venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
node tests\capture_guard.test.js
```

**Build the release folder.** Output goes to `dist\Genshin Rhythm Bot\` (the `.exe` plus `_internal\`). The release zip contains that folder plus the firmware as `Arduino Firmware\rhythm_controller\`.

```powershell
python -m PyInstaller pc_client\ui.py `
  --name "Genshin Rhythm Bot" --windowed --icon pc_client\assets\icon.ico --uac-admin `
  --add-data "pc_client\assets;assets" --add-data "pc_client\web;web" `
  --noconfirm --clean
```

### Project layout

```
arduino/rhythm_controller/              Arduino firmware (serial → USB HID)
pc_client/
  ui.py, ui_core.py, web/               the app: window, bot / macro / hotkey control, HTML UI
  detector.py                           pixel-polling note detector
  album.py, song_player.py              Album mode (album.py also runs as a CLI)
  screen_watcher.py, game_window.py     screen capture, button matching, game-window detection
  controller.py, software_input.py      Arduino and Software input methods
  macro_engine.py                       macro format, playback and slot files
  config.py                             tunable settings
  main.py, macro_tool.py, calibrate.py  CLI bot, CLI macro recorder, calibration overlay
  assets/                               icon + 1080p button templates for Album mode
tests/                                  unittest suite + capture_guard.test.js (Node)
```
