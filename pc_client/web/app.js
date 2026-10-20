// Frontend logic. Backend is `BridgeApi` in pc_client/ui.py exposed as
// `pywebview.api`. Two globals (updateStatus, appendLog) are called from
// Python via window.evaluate_js on the 50ms drain tick.
'use strict';

const STATE = {
    capturing: null,           // action name while waiting for keypress
    activeCapturing: null,     // active macro position while binding hotkey
    activeRenderSig: '',
    lastStatus: null,          // most recent status snapshot
    initialized: false,
};

const LOG_MAX_CHARS = 200_000; // soft cap so long sessions don't blow memory

// --- helpers ---------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function api() {
    return (window.pywebview && window.pywebview.api) || null;
}

function callApi(name, ...args) {
    const a = api();
    if (!a || typeof a[name] !== 'function') {
        return Promise.resolve(null);
    }
    try {
        const ret = a[name](...args);
        return Promise.resolve(ret);
    } catch (e) {
        console.error('[api] call failed', name, e);
        return Promise.reject(e);
    }
}

// --- titlebar buttons ------------------------------------------------------

$('#btn-min').addEventListener('click', () => callApi('minimize'));
$('#btn-max').addEventListener('click', () => {
    // Python snaps to the monitor work area (taskbar stays visible) and
    // reports the new state; swap glyph + tooltip to match.
    callApi('toggle_maximize').then((res) => {
        if (!res || typeof res.maximized !== 'boolean') return;
        const btn = $('#btn-max');
        btn.textContent = res.maximized ? '' : '';
        btn.title = res.maximized ? 'Restore' : 'Maximize';
    });
});
$('#btn-close').addEventListener('click', () => {
    // Unsaved macro work (recording, unsaved buffer, editor edits) only
    // lives in memory — ask before throwing it away.
    const loss = macroUnsavedSummary();
    if (loss && !confirm(
        `Close Genshin Rhythm Bot?\n\nYou have ${loss} that will be lost.`)) {
        return;
    }
    callApi('close');
});

// --- tab switching ---------------------------------------------------------

$$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
        const target = btn.dataset.tab;
        $$('.tab').forEach((t) => t.classList.toggle('tab-active', t === btn));
        $$('.panel').forEach((p) => {
            p.classList.toggle('panel-active', p.dataset.panel === target);
        });
    });
});

// --- mode + album options --------------------------------------------------

function modeIsAlbum() {
    return $('input[name="mode"][value="album"]').checked;
}

function applyAlbumDisabledState() {
    const album = modeIsAlbum();
    const card = $('#album-card');
    card.classList.toggle('disabled', !album);
    $$('#album-card input, #album-card select, #album-card button').forEach((el) => {
        el.disabled = !album;
    });
    // Replay-Canorus sits outside the album card but is album-only too.
    const rc = $('#replay-canorus');
    if (rc) rc.disabled = !album;
}

function applyModeUi() {
    applyAlbumDisabledState();
    callApi('set_mode', modeIsAlbum() ? 'album' : 'standalone');
}

function onModeRadioChange() {
    const newMode = modeIsAlbum() ? 'album' : 'standalone';
    const status = STATE.lastStatus || {};
    const running = status.state === 'running' || status.state === 'paused';
    const oldMode = status.mode;

    // Confirm only when leaving album mid-run — that's the case where
    // the user loses song progress. Standalone has no per-song state to
    // protect, and switching INTO album from idle/standalone is cheap.
    if (running && oldMode === 'album' && newMode !== 'album') {
        const ok = window.confirm(
            'Switching modes will abort the current album song. Continue?');
        if (!ok) {
            // Revert the radio to album without re-triggering this handler.
            const r = $('input[name="mode"][value="album"]');
            if (r) r.checked = true;
            applyAlbumDisabledState();
            return;
        }
    }

    applyAlbumDisabledState();
    if (running) {
        callApi('restart_in_mode', newMode);
    } else {
        callApi('set_mode', newMode);
    }
}

$$('input[name="mode"]').forEach((r) => {
    r.addEventListener('change', onModeRadioChange);
});

// Main-tab-only UI state (kept out of the shared STATE object).
const MAIN = {
    lastSongs: parseInt($('#songs').value, 10) || 12, // last valid Songs value
    startPending: null,   // {state, lastError, timer} while Start/Stop is in flight
};

// Clamp the Songs box to its min/max and write the clamped value back so
// what the user sees is what Python runs; garbage (empty / text) restores
// the last valid value instead of being silently ignored.
function commitSongs() {
    const el = $('#songs');
    const min = parseInt(el.min, 10) || 1;
    const max = parseInt(el.max, 10) || 12;
    let v = parseInt(el.value, 10);
    if (Number.isNaN(v)) v = MAIN.lastSongs;
    v = Math.max(min, Math.min(max, v));
    if (el.value !== String(v)) el.value = String(v);
    if (v !== MAIN.lastSongs) {
        MAIN.lastSongs = v;
        callApi('set_songs', v);
    }
}

$('#songs').addEventListener('change', commitSongs);
$('#songs').addEventListener('blur', commitSongs);

function selectedDifficulties() {
    return Array.from($$('input[name="difficulty"]:checked'))
        .map((cb) => cb.value);
}

function applyDifficultiesToCheckboxes(value) {
    // Accept either an array of names, the literal string 'all', or a
    // single difficulty name — JS init may receive any of those
    // depending on how the setting was saved.
    const all = ['normal', 'hard', 'pro', 'legendary'];
    let wanted;
    if (Array.isArray(value)) wanted = value;
    else if (value === 'all') wanted = all;
    else if (typeof value === 'string' && all.includes(value)) wanted = [value];
    else return;
    const wantedSet = new Set(wanted);
    $$('input[name="difficulty"]').forEach((cb) => {
        cb.checked = wantedSet.has(cb.value);
    });
}

$$('input[name="difficulty"]').forEach((cb) => {
    cb.addEventListener('change', () => {
        const chosen = selectedDifficulties();
        if (chosen.length === 0) {
            // Refuse to leave the user in a no-difficulty state — re-
            // check the box they just unchecked.
            cb.checked = true;
            return;
        }
        callApi('set_difficulty', chosen);
    });
});

$('#replay-canorus').addEventListener('change', () => {
    callApi('set_replay_canorus', $('#replay-canorus').checked);
});

// --- input backend selector ------------------------------------------------

function currentBackend() {
    return (STATE.lastStatus || {}).input_backend
        || $('#input-backend').value
        || 'arduino';
}

$('#input-backend').addEventListener('change', () => {
    const desired = $('#input-backend').value;
    const current = currentBackend();
    if (desired === current) return;
    const trySwitch = (force) => callApi('set_input_backend', desired, !!force);
    trySwitch(false).then((res) => {
        if (res && res.ok === false && res.reason === 'busy') {
            const ok = confirm(
                'Bot or macro is currently running. Stop them and switch '
                + 'to ' + desired + '?');
            if (!ok) {
                // Revert dropdown without re-firing change.
                $('#input-backend').value = current;
                return;
            }
            trySwitch(true).then((res2) => {
                if (res2 && res2.ok === false) {
                    $('#input-backend').value = current;
                    if (res2.reason === 'still_busy') {
                        alert('The bot or macro did not stop in time, so the '
                            + 'input method was not switched. Try again.');
                    }
                }
            });
        } else if (res && res.ok === false) {
            $('#input-backend').value = current;
        }
    });
});

$('#debug').addEventListener('change', () => {
    callApi('set_debug', $('#debug').checked);
});

// Custom spinbox up/down — the native number input arrow buttons are
// hidden in CSS so we re-implement via the styled buttons.
$$('.spinbox-up, .spinbox-down').forEach((btn) => {
    btn.addEventListener('click', () => {
        const target = $(`#${btn.dataset.target}`);
        if (!target || target.disabled) return;
        const step = btn.classList.contains('spinbox-up') ? 1 : -1;
        const v = parseInt(target.value, 10) || 0;
        const min = parseInt(target.min, 10);
        const max = parseInt(target.max, 10);
        let next = v + step;
        if (!Number.isNaN(min)) next = Math.max(min, next);
        if (!Number.isNaN(max)) next = Math.min(max, next);
        target.value = next;
        target.dispatchEvent(new Event('change'));
    });
});

// --- start/stop/pause ------------------------------------------------------

// Start/Stop debounce: after a click the button stays disabled until a
// status push shows the state (or last_error) changed, with a 1.5 s
// safety re-enable — so a double-click can't Start and then immediately
// Stop the run.
function clearStartPending() {
    if (MAIN.startPending && MAIN.startPending.timer) {
        clearTimeout(MAIN.startPending.timer);
    }
    MAIN.startPending = null;
}

function beginStartPending() {
    clearStartPending();
    const s = STATE.lastStatus || {};
    MAIN.startPending = {
        state: s.state || 'idle',
        lastError: s.last_error || '',
        timer: setTimeout(() => {
            MAIN.startPending = null;
            renderStartButtons(STATE.lastStatus || {});
        }, 1500),
    };
    $('#btn-start').disabled = true;
}

function endStartPendingNow() {
    clearStartPending();
    renderStartButtons(STATE.lastStatus || {});
}

$('#btn-start').addEventListener('click', () => {
    if (MAIN.startPending) return;
    // If a macro is busy and the bot is currently idle, this click would
    // start the bot. Confirm so the user knows the macro will be killed.
    if (!botIsRunning() && macroIsBusy()) {
        const action = (STATE.lastStatus || {}).macro_state;
        const verb = action === 'recording' ? 'recording' : 'macro playback';
        if (!confirm(`A macro is ${verb}. Stop it and start the bot?`)) return;
        beginStartPending();
        callApi('start_stop_force').then((res) => {
            if (res && res.ok === false) {
                endStartPendingNow();
                if (res.reason === 'busy') {
                    alert('The macro did not stop in time, so the bot was '
                        + 'not started. Try again.');
                }
            }
        });
        return;
    }
    beginStartPending();
    callApi('start_stop').then((ok) => {
        if (ok === false) endStartPendingNow();
    });
});
$('#btn-pause').addEventListener('click', () => callApi('pause'));

// --- hotkey rebind ---------------------------------------------------------

$$('.hk-row').forEach((row) => {
    const action = row.dataset.action;
    const btn = row.querySelector('.rebind');
    btn.addEventListener('click', () => beginCapture(action));
});

// While a capture prompt is open Python DROPS every global hotkey press
// (capture_begin), so pressing F8 to try it as a binding can't also stop
// the bot or F10 toggle the debug window. Python auto-expires that after
// 15 s in case capture_end is lost; the prompt gives up a little earlier
// so the two never disagree. Tokens grow per prompt (seeded from the
// clock so they keep growing across a reload) because every bridge call
// runs on its own Python thread: "cancel A, arm B" can arrive reversed.
// The prompt only accepts a key once Python has CONFIRMED the suspension
// for the current token (`_captureReady`); until then a press is
// swallowed, never submitted -- otherwise a slow capture_begin let F8
// both bind and fire Start/Stop.
const CAPTURE_TIMEOUT_MS = 14500;
let _captureToken = 0;
let _captureTimer = null;
let _captureReady = false;
// Rebind/Bind button whose own left-click just cancelled its capture: the
// click event that follows that mousedown must not re-arm it.
let _captureCancelBtn = null;
// Right-click capture ends before the button is released; keep blocking
// the native context menu that the release would open.
let _captureCtxGuardUntil = 0;

function captureArmed() {
    return !!(STATE.capturing || STATE.activeCapturing);
}

function currentCaptureMsg() {
    if (STATE.capturing) return captureMsgFor(STATE.capturing);
    if (STATE.activeCapturing) return $('.active-capture-msg');
    return null;
}

// Ask Python to suspend hotkeys for a fresh token; input stays blocked
// until it confirms. `onArmed` runs only if this arming is still the
// current one (not cancelled / re-armed while the call was in flight).
function armCaptureGuard(onArmed) {
    _captureToken = Math.max(_captureToken + 1, Date.now());
    const token = _captureToken;
    _captureReady = false;
    if (_captureTimer) clearTimeout(_captureTimer);
    _captureTimer = setTimeout(cancelCapture, CAPTURE_TIMEOUT_MS);
    callApi('capture_begin', token)
        .then((ok) => ok === true, () => false)
        .then((ok) => {
            if (token !== _captureToken || !captureArmed()) return;
            if (!ok) {
                const msg = currentCaptureMsg();
                cancelCapture();
                if (msg) {
                    msg.textContent =
                        'Could not pause the hotkeys for capture - try again.';
                }
                return;
            }
            _captureReady = true;
            if (onArmed) onArmed();
        });
}

function releaseCaptureGuard() {
    _captureReady = false;
    if (_captureTimer) {
        clearTimeout(_captureTimer);
        _captureTimer = null;
    }
    callApi('capture_end', _captureToken);
}

function cancelCapture() {
    endCapture();
    endActiveCapture();
}

function captureButton() {
    if (STATE.capturing) {
        const row = document.querySelector(
            `.hk-row[data-action="${STATE.capturing}"]`);
        return row ? row.querySelector('.rebind') : null;
    }
    if (STATE.activeCapturing) {
        const row = document.querySelector(
            `.active-macro-row[data-active="${STATE.activeCapturing}"]`);
        return row ? row.querySelector('.active-bind') : null;
    }
    return null;
}

function consumeCancelClick(btn) {
    // True when this click is the tail of the left-click that just
    // cancelled capture on this same button: clicking Rebind/Bind again
    // while it shows "..." cancels instead of re-arming.
    const hit = !!btn && _captureCancelBtn === btn;
    _captureCancelBtn = null;
    return hit;
}

const _CAPTURE_REFUSALS = {
    duplicate_hotkey: 'That binding is already in use.',
    left_click_reserved:
        'Left Click cannot be a hotkey - it is every attack in Genshin.',
    unsupported_key: 'That key cannot be used as a hotkey.',
    rhythm_key: 'A S D J K L are the keys the bot presses itself.',
    install_failed: 'Could not install that hotkey (see Logs).',
};

function showCaptureRefusal(msg, res) {
    if (!msg) return;
    const why = _CAPTURE_REFUSALS[res && res.reason] || 'Could not bind that.';
    msg.textContent = `${why} Try another key or button (Esc cancels)...`;
}

function submitCapture(payload) {
    if (!_captureReady) return;          // suspension not confirmed yet
    // One submission at a time: a second quick key must not bind too.
    _captureReady = false;
    const token = _captureToken;
    if (STATE.activeCapturing) {
        const pos = STATE.activeCapturing;
        callApi('macro_set_active_hotkey', pos, payload).then((res) => {
            // Cancelled (or cancelled and re-armed) meanwhile.
            if (token !== _captureToken || STATE.activeCapturing !== pos) return;
            if (res && res.ok !== false) {
                endActiveCapture();
                return;
            }
            showCaptureRefusal($('.active-capture-msg'), res);
            armCaptureGuard();
        }, () => {
            if (token !== _captureToken || STATE.activeCapturing !== pos) return;
            showCaptureRefusal($('.active-capture-msg'), null);
            armCaptureGuard();
        });
        return;
    }
    if (STATE.capturing) {
        const action = STATE.capturing;
        callApi('rebind', action, payload).then((res) => {
            if (token !== _captureToken || STATE.capturing !== action) return;
            // Success returns the new hotkey string; refusals are
            // {ok: false, reason}.
            if (typeof res === 'string' && res) {
                endCapture();
                return;
            }
            showCaptureRefusal(captureMsgFor(action), res);
            armCaptureGuard();
        }, () => {
            if (token !== _captureToken || STATE.capturing !== action) return;
            showCaptureRefusal(captureMsgFor(action), null);
            armCaptureGuard();
        });
    }
}

function captureMsgFor(action) {
    // Each card has its own capture message line — find it via the row.
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    if (!row) return null;
    const card = row.closest('.card');
    return card ? card.querySelector('.hk-capture-msg') : null;
}

function beginCapture(action) {
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    const btn = row ? row.querySelector('.rebind') : null;
    if (consumeCancelClick(btn) || captureArmed() || !btn) return;
    STATE.capturing = action;
    btn.textContent = '...';
    const msg = captureMsgFor(action);
    if (msg) msg.textContent = 'Pausing hotkeys...';
    armCaptureGuard(() => {
        if (msg) {
            msg.textContent =
                `Press a key or mouse button to bind ${labelFor(action)} (Esc or left-click cancels)...`;
        }
    });
}

function endCapture() {
    if (!STATE.capturing) return;
    const action = STATE.capturing;
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    if (row) row.querySelector('.rebind').textContent = 'Rebind';
    const msg = captureMsgFor(action);
    if (msg) msg.textContent = '';
    STATE.capturing = null;
    if (!captureArmed()) releaseCaptureGuard();
}

function beginActiveCapture(pos) {
    const row = document.querySelector(`.active-macro-row[data-active="${pos}"]`);
    const btn = row ? row.querySelector('.active-bind') : null;
    if (consumeCancelClick(btn) || captureArmed()) return;
    STATE.activeCapturing = pos;
    if (btn) btn.textContent = '...';
    const label = row && row.querySelector('.active-label');
    const name = (label && label.textContent.trim()) || pos;
    const msg = $('.active-capture-msg');
    if (msg) msg.textContent = 'Pausing hotkeys...';
    // Same guard as the hotkey rows: global hotkeys are suspended (and
    // the prompt times out) while an active-macro key is being picked.
    armCaptureGuard(() => {
        if (msg) {
            msg.textContent =
                `Press a key or mouse button to bind active macro ${name} (Esc or left-click cancels)...`;
        }
    });
}

function endActiveCapture() {
    if (!STATE.activeCapturing) return;
    const pos = STATE.activeCapturing;
    const row = document.querySelector(`.active-macro-row[data-active="${pos}"]`);
    if (row) row.querySelector('.active-bind').textContent = 'Bind';
    const msg = $('.active-capture-msg');
    if (msg) msg.textContent = '';
    STATE.activeCapturing = null;
    if (!captureArmed()) releaseCaptureGuard();
}

function labelFor(action) {
    return ({
        start_stop:   'Start/Stop',
        pause:        'Pause/Resume',
        debug:        'Toggle Debug',
        macro_record: 'Record/Stop',
        macro_play:   'Play Macro',
        macro_stop:   'Stop Playback',
        macro_save:   'Save Slot',
        macro_load:   'Load Slot',
    })[action] || action;
}

const _MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'OS']);

// Capture phase so reserved-key swallow happens before browser default.
window.addEventListener('keydown', (ev) => {
    // Capture mode (either tab): any non-modifier key gets sent to Python.
    if (captureArmed()) {
        ev.preventDefault();
        ev.stopPropagation();
        if (ev.key === 'Escape') {
            cancelCapture();
            return;
        }
        if (_MODIFIER_KEYS.has(ev.key)) return;
        // Holding a key auto-repeats keydown; submit the press once. Until
        // Python confirms the suspension the press is swallowed.
        if (ev.repeat || !_captureReady) return;
        submitCapture({
            key: ev.key,
            code: ev.code,
            ctrlKey: ev.ctrlKey,
            altKey: ev.altKey,
            shiftKey: ev.shiftKey,
            metaKey: ev.metaKey,
        });
        return;
    }
    // Suppress webview-default shortcuts that would reload / leave the
    // page or open native dialogs the user can't return from.
    if (ev.key === 'F5' || ev.key === 'F11' || ev.key === 'F12'
        || (ev.ctrlKey && (ev.key === 'r' || ev.key === 'R'
                            || ev.key === 'p' || ev.key === 'P'
                            || ev.key === 'f' || ev.key === 'F'
                            || ev.key === 'g' || ev.key === 'G'))) {
        ev.preventDefault();
    }
}, true);

// Mouse-button capture for hotkey rebind. Browser button index → mouse
// package name: 0=left, 1=middle, 2=right, 3=x (back), 4=x2 (forward).
// Edge/Chromium fires button 3 / 4 for the side buttons; if a mouse
// doesn't expose them, the user can still rebind via keyboard.
const _BROWSER_BUTTON_TO_NAME = ['left', 'middle', 'right', 'x', 'x2'];
window.addEventListener('mousedown', (ev) => {
    if (!captureArmed()) {
        _captureCancelBtn = null;
        return;
    }
    if (ev.button === 0) {
        // Left click never becomes a binding (Python refuses it too): it is
        // every attack in Genshin and every album click, and it is what
        // people press "somewhere else" to back out of the prompt -- which
        // is exactly how Play ended up on Left Click. Cancel the capture
        // and let the click carry on to whatever it was aimed at. A click
        // on the capturing button itself just cancels (see
        // consumeCancelClick).
        const btn = captureButton();
        const hit = ev.target && ev.target.closest
            ? ev.target.closest('.rebind, .active-bind') : null;
        _captureCancelBtn = btn && hit === btn ? btn : null;
        cancelCapture();
        return;
    }
    const name = _BROWSER_BUTTON_TO_NAME[ev.button];
    if (!name) return;
    ev.preventDefault();
    ev.stopPropagation();
    _captureCtxGuardUntil = performance.now() + 1500;
    if (!_captureReady) return;          // suspension not confirmed yet
    submitCapture({ mouseButton: name });
}, true);
// Leaving the window (alt-tab, clicking into the game) abandons the
// prompt, so hotkeys are live again as soon as the user is in-game.
window.addEventListener('blur', () => {
    if (captureArmed()) cancelCapture();
});
// Also block contextmenu while capturing so right-click capture doesn't
// pop the native menu.
window.addEventListener('contextmenu', (ev) => {
    if (captureArmed() || performance.now() < _captureCtxGuardUntil) {
        ev.preventDefault();
    }
}, true);

// --- macros panel ----------------------------------------------------------

let macroSlotMode = 'load';   // 'load' | 'save' | 'clear'

$$('.macro-mode-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
        macroSlotMode = btn.dataset.mode;
        $$('.macro-mode-btn').forEach((b) =>
            b.classList.toggle('macro-mode-active', b === btn));
    });
});

function slotNameOf(n) {
    const map = (STATE.lastStatus && STATE.lastStatus.macro_slot_names) || {};
    // pywebview JSONifies dict keys as strings; normalize lookup.
    return map[n] || map[String(n)] || '';
}

// Transient explanation in the Slots card's #macro-pending line, for
// refusals the UI can explain without a round-trip (empty slot in Load
// mode, nothing to save, ...). A hotkey slot-picker prompt wins while
// one is armed. Python-side failures show in the status pill instead
// (macro_error).
const macroHint = { text: '', until: 0, timer: null };

function showMacroHint(text, ms = 6000) {
    macroHint.text = text || '';
    macroHint.until = Date.now() + ms;
    renderMacroPending((STATE.lastStatus || {}).macro_pending);
    clearTimeout(macroHint.timer);
    macroHint.timer = setTimeout(
        () => renderMacroPending((STATE.lastStatus || {}).macro_pending),
        ms + 50);
}

// Row letters (A/B/C) of the active macros that play slot `n`.
function activeUsersOfSlot(n) {
    const rows = $$('.active-macro-row');
    return activeMacrosOf().map((item, i) => {
        if (item.slot !== n) return null;
        const label = rows[i] && rows[i].querySelector('.active-label');
        return (label && label.textContent.trim()) || String(i + 1);
    }).filter(Boolean);
}

// What replacing the working macro right now would throw away, as a
// phrase ('' = nothing). Shared by the Load / Record / Close confirms.
function macroUnsavedSummary() {
    const s = STATE.lastStatus || {};
    const parts = [];
    const n = s.macro_events || 0;
    const evs = `${n} event${n === 1 ? '' : 's'}`;
    if (s.macro_state === 'recording') {
        parts.push(`a recording in progress (${evs})`);
    } else if (s.macro_dirty && n > 0) {
        const loaded = parseInt(s.macro_loaded, 10) || 0;
        parts.push(loaded
            ? `unsaved changes to slot ${loaded} (${evs})`
            : `an unsaved macro (${evs})`);
    }
    if (evtState.visible && evtState.jsDirty) {
        parts.push('unsaved edits in the events editor');
    }
    return parts.join(' and ');
}

// The user accepted losing the editor's local edits (Load / Record
// replaced the macro): let the editor follow the new buffer.
function discardEditorEdits() {
    if (!evtState.jsDirty) return;
    evtState.jsDirty = false;
    evtState.editRev += 1;
    maybeAutoRefreshEditor();
}

function activeMacrosOf(status) {
    const s = status || STATE.lastStatus || {};
    const active = Array.isArray(s.macro_active_macros)
        ? s.macro_active_macros : [];
    return $$('.active-macro-row').map((_row, i) => {
        const item = active[i] || {};
        return {
            slot: parseInt(item.slot, 10) || 0,
            hotkey: item.hotkey || '',
        };
    });
}

function renderActiveMacroRows(status) {
    const s = status || STATE.lastStatus || {};
    const active = activeMacrosOf(s);
    const occupied = new Set((s.macro_slots || []).map((n) => parseInt(n, 10)));
    const names = s.macro_slot_names || {};
    const playingSlot = parseInt(s.macro_playing_slot, 10) || 0;
    const sig = JSON.stringify({
        active,
        slots: s.macro_slots || [],
        names,
        playingSlot,
        state: s.macro_state || '',
    });
    if (sig === STATE.activeRenderSig) return;
    STATE.activeRenderSig = sig;
    $$('.active-macro-row').forEach((row) => {
        const pos = parseInt(row.dataset.active, 10);
        const item = active[pos - 1] || { slot: 0, hotkey: '' };
        const select = row.querySelector('.active-slot');
        const previous = String(item.slot || 0);
        select.innerHTML = '<option value="0">No slot</option>';
        (s.macro_slots || []).forEach((slot) => {
            const opt = document.createElement('option');
            opt.value = String(slot);
            const name = names[slot] || names[String(slot)] || '';
            opt.textContent = name ? `Slot ${slot} - ${name}` : `Slot ${slot}`;
            select.appendChild(opt);
        });
        if (item.slot && !occupied.has(item.slot)) {
            const opt = document.createElement('option');
            opt.value = String(item.slot);
            opt.textContent = `Missing slot ${item.slot}`;
            select.appendChild(opt);
        }
        select.value = previous;

        const binding = row.querySelector('.active-binding');
        binding.textContent = displayBinding(item.hotkey);
        // Same caveat renderKeybinds shows: a mouse hotkey sees every
        // click of that button, including clicks a playing macro replays.
        binding.title = String(item.hotkey || '').toLowerCase().startsWith('mouse:')
            ? `${displayBinding(item.hotkey)} - mouse hotkeys also fire when `
              + 'a macro replays this button.'
            : (item.hotkey ? displayBinding(item.hotkey) : '');

        const slotReady = !!item.slot && occupied.has(item.slot);
        const isRecording = s.macro_state === 'recording';
        const isPlayingThis = playingSlot && playingSlot === item.slot;
        row.classList.toggle('active-playing', !!isPlayingThis);
        row.querySelector('.active-play').disabled = !slotReady || isRecording;
        row.querySelector('.active-bind').disabled = isRecording;
        row.querySelector('.active-clear').disabled = !item.slot && !item.hotkey;
    });
}

function renderActivePolicy(policy) {
    const normalized = policy === 'interrupt' ? 'interrupt' : 'ignore';
    $$('.active-policy-btn').forEach((btn) => {
        btn.classList.toggle('macro-mode-active',
            btn.dataset.policy === normalized);
    });
}

$$('.active-policy-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
        callApi('macro_set_conflict_policy', btn.dataset.policy);
    });
});

$$('.active-macro-row').forEach((row) => {
    const pos = parseInt(row.dataset.active, 10);
    row.querySelector('.active-slot').addEventListener('change', (ev) => {
        callApi('macro_set_active_slot', pos, parseInt(ev.target.value, 10))
            .then((res) => {
                if (res && res.ok === false) {
                    STATE.activeRenderSig = '';
                    renderActiveMacroRows(STATE.lastStatus || {});
                }
            });
    });
    row.querySelector('.active-bind').addEventListener('click', () => {
        beginActiveCapture(pos);
    });
    row.querySelector('.active-play').addEventListener('click', () => {
        callApi('macro_play_active', pos, false).then((res) => {
            if (res && res.ok === false && res.reason === 'bot_running') {
                if (confirm('The bot is running. Stop it and play this active macro?')) {
                    callApi('macro_play_active', pos, true);
                }
            }
        });
    });
    row.querySelector('.active-clear').addEventListener('click', () => {
        callApi('macro_clear_active', pos);
    });
});

$$('.slot').forEach((btn) => {
    btn.addEventListener('click', () => {
        const n = parseInt(btn.dataset.slot, 10);
        if (Number.isNaN(n)) return;
        const s = STATE.lastStatus || {};
        const occupied = btn.classList.contains('slot-occupied');
        const existing = slotNameOf(n);
        const label = existing ? ` ("${existing}")` : '';
        if (macroIsBusy()) {
            // The grid is disabled while busy; covers a click that raced
            // the status push.
            showMacroHint(`Stop ${s.macro_state === 'recording'
                ? 'recording' : 'playback'} before using the slots.`);
            return;
        }
        if (macroSlotMode === 'load') {
            if (!occupied) {
                showMacroHint(`Slot ${n} is empty — switch to Save to `
                    + 'store the current macro there.');
                return;
            }
            const loss = macroUnsavedSummary();
            if (loss && !confirm(
                `Load slot ${n}${label}?\n\nThis replaces the current macro, `
                + `and you have ${loss} that will be lost.`)) {
                return;
            }
            callApi('macro_load_slot', n).then((res) => {
                if (res && res.ok) discardEditorEdits();
            });
        } else if (macroSlotMode === 'save') {
            if ((s.macro_events || 0) === 0) {
                showMacroHint('Nothing to save — record or load a macro first.');
                return;
            }
            if (evtState.visible && evtState.jsDirty && !confirm(
                'The events editor has unsaved edits that are not in the '
                + 'macro yet (press Save in the editor to apply them).\n\n'
                + `Save the macro to slot ${n} without those edits?`)) {
                return;
            }
            if (occupied) {
                const what = existing ? `"${existing}"` : 'a saved macro';
                const users = activeUsersOfSlot(n);
                if (!confirm(
                    `Slot ${n} already contains ${what}. ` +
                    `Overwrite with the current macro?` +
                    (users.length ? `\n\nActive macro ${users.join(', ')} `
                        + 'plays this slot and will play the new macro.' : ''))) {
                    return;
                }
            }
            // Pin the buffer the user is naming: if it changes while the
            // prompt is open (in-game record hotkey), Python refuses.
            const rev = s.macro_rev;
            const promptMsg = occupied
                ? `Overwrite slot ${n} — name (leave blank for none):`
                : `Save to slot ${n} — name (leave blank for none):`;
            const name = window.prompt(promptMsg, existing);
            if (name === null) return;   // cancelled at name prompt
            callApi('macro_save_slot', n, name,
                    typeof rev === 'number' ? rev : null).then((res) => {
                if (res && res.ok === false && res.reason === 'stale') {
                    showMacroHint('The macro changed while you were naming it '
                        + '— nothing was saved. Try again.');
                }
            });
        } else if (macroSlotMode === 'rename') {
            if (!occupied) {
                showMacroHint(`Slot ${n} is empty — nothing to rename.`);
                return;
            }
            const name = window.prompt(`Rename slot ${n}:`, existing);
            if (name === null) return;
            callApi('macro_rename_slot', n, name);
        } else if (macroSlotMode === 'clear') {
            if (!occupied) {
                showMacroHint(`Slot ${n} is already empty.`);
                return;
            }
            const users = activeUsersOfSlot(n);
            const loaded = parseInt(s.macro_loaded, 10) || 0;
            let msg = `Clear macro slot ${n}${label}? This cannot be undone.`;
            if (users.length) {
                msg += `\n\nActive macro ${users.join(', ')} uses this slot `
                    + 'and will be unassigned (its hotkey stays bound but '
                    + 'does nothing until you pick another slot).';
            }
            if (loaded === n && (s.macro_events || 0) > 0) {
                msg += '\n\nThe events loaded from it stay in the current '
                    + 'macro as unsaved.';
            }
            if (confirm(msg)) callApi('macro_clear_slot', n);
        }
    });
});

function botIsRunning() {
    const s = STATE.lastStatus || {};
    return s.state === 'running' || s.state === 'paused';
}

function macroIsBusy() {
    const s = STATE.lastStatus || {};
    return s.macro_state === 'recording' || s.macro_state === 'playing';
}

$('#macro-record').addEventListener('click', () => {
    const starting = (STATE.lastStatus || {}).macro_state !== 'recording';
    if (starting) {
        const loss = macroUnsavedSummary();
        if (loss && !confirm(
            'Start a new recording?\n\nRecording replaces the current '
            + `macro, and you have ${loss} that will be lost.`)) {
            return;
        }
    }
    const after = (res) => {
        if (starting && res && res.ok) discardEditorEdits();
    };
    callApi('macro_toggle_record', false).then((res) => {
        if (res && res.ok === false && res.reason === 'bot_running') {
            if (confirm('The bot is running. Stop it and start recording?')) {
                callApi('macro_toggle_record', true).then(after);
            }
            return;
        }
        after(res);
    });
});

$('#macro-play').addEventListener('click', () => {
    callApi('macro_play', false).then((res) => {
        if (res && res.ok === false && res.reason === 'bot_running') {
            if (confirm('The bot is running. Stop it and play the macro?')) {
                callApi('macro_play', true);
            }
        }
    });
});

$('#macro-stop-play').addEventListener('click', () => callApi('macro_stop'));

function renderMacroSlots(slots, namesMap, loadedSlot, macroState) {
    const occupied = new Set(slots || []);
    const names = namesMap || {};
    const loaded = parseInt(loadedSlot, 10) || 0;
    // Slots can't load / save / rename / clear while recording or
    // playing — disable them instead of refusing silently.
    const busy = macroState === 'recording' || macroState === 'playing';
    $$('.slot').forEach((btn) => {
        const n = parseInt(btn.dataset.slot, 10);
        const isOcc = occupied.has(n);
        btn.classList.toggle('slot-occupied', isOcc);
        btn.classList.toggle('slot-loaded', isOcc && n === loaded);
        btn.disabled = busy;
        const nameEl = btn.querySelector('.slot-name');
        if (!nameEl) return;
        // The 9 px name is ellipsized; the tooltip carries the full one.
        const busyNote = busy ? ` (stop ${macroState} to use the slots)` : '';
        if (!isOcc) {
            nameEl.textContent = 'empty';
            btn.title = `Slot ${n}: empty${busyNote}`;
            return;
        }
        const name = names[n] || names[String(n)] || '';
        nameEl.textContent = name || 'saved';
        btn.title = `Slot ${n}: ${name || '(unnamed)'}`
            + (n === loaded ? ' · loaded' : '') + busyNote;
    });
}

function renderMacroState(s) {
    s = s || {};
    const statusEl = $('#macro-status');
    const recBtn = $('#macro-record');
    const playBtn = $('#macro-play');
    const stopBtn = $('#macro-stop-play');
    const stateLabel = s.macro_state || 'idle';
    const bufferEvents = s.macro_events || 0;
    // While playing, count what is actually playing — an active slot's
    // event count differs from the working buffer's.
    const playingEvents = parseInt(s.macro_playing_events, 10) || 0;
    const events = (stateLabel === 'playing' && playingEvents)
        ? playingEvents : bufferEvents;
    const loaded = parseInt(s.macro_loaded, 10) || 0;
    const isDirty = !!s.macro_dirty;
    let suffix = '';
    if (stateLabel === 'playing' && s.macro_playing_source === 'active') {
        const slot = parseInt(s.macro_playing_slot, 10) || 0;
        const name = s.macro_playing_name || '';
        suffix = name
            ? ` · active slot ${slot} (${name})`
            : ` · active slot ${slot}`;
    } else if (loaded > 0) {
        const names = s.macro_slot_names || {};
        const name = names[loaded] || names[String(loaded)] || '';
        const tag = isDirty ? '*' : '';
        suffix = name
            ? ` · slot ${loaded}${tag} (${name})`
            : ` · slot ${loaded}${tag}`;
    } else if (isDirty && bufferEvents > 0) {
        suffix = ' · unsaved';
    }
    const base =
        `${stateLabel} · ${events} event${events === 1 ? '' : 's'}${suffix}`;
    // Last macro failure (game not found, save failed, ...) leads the
    // pill so the ellipsis can't hide it; cleared by the next success.
    const err = String(s.macro_error || '').trim();
    statusEl.textContent = err ? `${err} · ${base}` : base;
    // CSS truncates long slot names with ellipsis; mirror the full
    // string on `title` so hover still reveals what got cut.
    statusEl.title = err ? `${err}\n${base}` : base;
    statusEl.classList.toggle('macro-status-recording',
        stateLabel === 'recording' || (!!err && stateLabel === 'idle'));
    statusEl.classList.toggle('macro-status-playing', stateLabel === 'playing');
    recBtn.textContent = stateLabel === 'recording' ? 'Stop Recording' : 'Record';
    recBtn.disabled = stateLabel === 'playing';
    playBtn.disabled = stateLabel !== 'idle' || bufferEvents === 0;
    stopBtn.disabled = stateLabel !== 'playing';
}

function renderMacroPending(pending) {
    const el = $('#macro-pending');
    if (pending === 'save') {
        el.textContent = 'Save (in Genshin): 1-9 picks slots 1-9; 0 then 1-9 picks 10-18. Esc or leaving the game cancels.';
    } else if (pending === 'load') {
        el.textContent = 'Load (in Genshin): 1-9 picks slots 1-9; 0 then 1-9 picks 10-18. Esc or leaving the game cancels.';
    } else if (macroHint.text && Date.now() < macroHint.until) {
        el.textContent = macroHint.text;
    } else {
        el.textContent = '';
    }
}

// --- events editor ---------------------------------------------------------

// The editor pins the buffer REVISION (+ loaded slot) it fetched.
// Python bumps `macro_rev` on every buffer change (record, load, apply,
// loaded slot cleared), so loading another slot with the same event
// count still refreshes the editor, and a Save made against slot A is
// refused once slot B is loaded instead of being written into B.
const evtState = {
    visible: false,
    working: [],         // local edit copy
    jsDirty: false,      // local edits not yet pushed to Python
    rev: -1,             // buffer revision of `working` (-1 = not loaded yet)
    slot: 0,             // slot loaded when `working` was fetched (0 = none)
    editRev: 0,          // bumped on every local edit
    fetchGen: 0,         // bumped per fetch / hide — stale responses dropped
    fetching: false,     // one fetch in flight at a time
    refetch: false,      // another fetch was requested meanwhile
    saving: false,
};

// True when the buffer moved on since `working` was fetched.
function editorIsStale() {
    if (evtState.rev < 0) return false;
    const s = STATE.lastStatus || {};
    if (typeof s.macro_rev === 'number' && s.macro_rev !== evtState.rev) {
        return true;
    }
    const loaded = parseInt(s.macro_loaded, 10) || 0;
    return evtState.slot > 0 && loaded !== evtState.slot;
}

function escapeAttr(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

function renderEventsTable() {
    const tbody = $('#evt-tbody');
    tbody.innerHTML = '';
    evtState.working.forEach((ev, i) => {
        const t = (typeof ev.time === 'number' ? ev.time : 0).toFixed(3);
        const dev = (ev.device || 'keyboard').toLowerCase();
        const evt = (ev.event_type || 'down').toLowerCase();
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="evt-idx">${i + 1}</td>
            <td><input type="number" step="0.001" min="0" value="${t}" data-field="time"></td>
            <td>
                <select data-field="device">
                    <option value="keyboard"${dev === 'keyboard' ? ' selected' : ''}>keyboard</option>
                    <option value="mouse"${dev === 'mouse' ? ' selected' : ''}>mouse</option>
                </select>
            </td>
            <td><input type="text" value="${escapeAttr(ev.key || '')}" data-field="key" spellcheck="false"></td>
            <td>
                <select data-field="event_type">
                    <option value="down"${evt === 'down' ? ' selected' : ''}>down</option>
                    <option value="up"${evt === 'up' ? ' selected' : ''}>up</option>
                    <option value="double"${evt === 'double' ? ' selected' : ''}>double</option>
                </select>
            </td>
            <td><button type="button" class="evt-del" title="Delete row">&times;</button></td>
        `;
        tr.querySelectorAll('[data-field]').forEach((el) => {
            el.addEventListener('input', () => {
                const field = el.dataset.field;
                let val = el.value;
                if (field === 'time') val = parseFloat(val) || 0;
                evtState.working[i][field] = val;
                evtState.jsDirty = true;
                evtState.editRev += 1;
                updateEvtButtons();
            });
        });
        tr.querySelector('.evt-del').addEventListener('click', () => {
            evtState.working.splice(i, 1);
            evtState.jsDirty = true;
            evtState.editRev += 1;
            renderEventsTable();
            updateEvtButtons();
        });
        tbody.appendChild(tr);
    });
    $('#evt-count').textContent = `(${evtState.working.length})`;
}

function updateEvtButtons() {
    const visible = evtState.visible;
    const s = STATE.lastStatus || {};
    const busy = s.macro_state === 'recording' || s.macro_state === 'playing';
    $('#evt-add').disabled = !visible || evtState.rev < 0 || busy;
    const saveBtn = $('#evt-save');
    saveBtn.disabled = !visible || !evtState.jsDirty || busy || evtState.saving;
    saveBtn.title = (visible && busy)
        ? `Stop ${s.macro_state} before saving edits.` : '';
    $('#evt-discard').disabled = !visible || !evtState.jsDirty || evtState.saving;
    const tag = $('#evt-dirty-tag');
    let text = '';
    if (visible && evtState.jsDirty) {
        text = editorIsStale()
            ? 'unsaved edits — the macro changed meanwhile'
            : 'unsaved edits';
    }
    tag.textContent = text;
}

// `macro_get_events` returns {events, rev, loaded, ...}; tolerate the
// legacy bare list.
function normalizeEventsPayload(res) {
    if (Array.isArray(res)) {
        const s = STATE.lastStatus || {};
        return {
            events: res,
            rev: typeof s.macro_rev === 'number' ? s.macro_rev : 0,
            loaded: parseInt(s.macro_loaded, 10) || 0,
        };
    }
    if (res && Array.isArray(res.events)) {
        return {
            events: res.events,
            rev: typeof res.rev === 'number' ? res.rev : 0,
            loaded: parseInt(res.loaded, 10) || 0,
        };
    }
    return null;
}

// One request in flight at a time. A response is dropped if the editor
// was hidden / re-fetched meanwhile, or if the user typed after the
// request went out (their edits win; Save's revision check catches any
// mismatch).
function fetchAndShowEvents() {
    if (evtState.fetching) {
        evtState.refetch = true;
        return Promise.resolve();
    }
    const gen = ++evtState.fetchGen;
    const editRevAtStart = evtState.editRev;
    evtState.fetching = true;
    return callApi('macro_get_events').then((res) => {
        if (gen !== evtState.fetchGen || !evtState.visible) return;
        if (evtState.rev >= 0 && evtState.editRev !== editRevAtStart) return;
        const p = normalizeEventsPayload(res);
        if (!p) return;
        evtState.working = p.events;
        evtState.rev = p.rev;
        evtState.slot = p.loaded;
        evtState.jsDirty = false;
        renderEventsTable();
        updateEvtButtons();
    }).catch((e) => {
        console.error('[evt] fetch failed', e);
    }).finally(() => {
        evtState.fetching = false;
        const again = evtState.refetch;
        evtState.refetch = false;
        if (again && evtState.visible && !evtState.jsDirty) {
            fetchAndShowEvents();
        }
    });
}

$('#evt-toggle').addEventListener('click', () => {
    if (evtState.visible) {
        if (evtState.jsDirty
            && !confirm('Discard your unsaved event edits?')) {
            return;
        }
        evtState.visible = false;
        evtState.jsDirty = false;
        evtState.working = [];
        evtState.rev = -1;
        evtState.slot = 0;
        evtState.fetchGen += 1;     // drop any response still in flight
        $('#evt-list-wrapper').hidden = true;
        $('#evt-toggle').textContent = 'Show';
        updateEvtButtons();
    } else {
        evtState.visible = true;
        $('#evt-list-wrapper').hidden = false;
        $('#evt-toggle').textContent = 'Hide';
        fetchAndShowEvents();
    }
});

$('#evt-add').addEventListener('click', () => {
    const lastT = evtState.working.length
        ? (parseFloat(evtState.working[evtState.working.length - 1].time) || 0)
        : 0;
    evtState.working.push({
        time: +(lastT + 0.05).toFixed(3),
        device: 'keyboard',
        key: 'a',
        event_type: 'down',
    });
    evtState.jsDirty = true;
    evtState.editRev += 1;
    renderEventsTable();
    updateEvtButtons();
});

$('#evt-discard').addEventListener('click', () => {
    if (!confirm('Discard your edits and reload from the buffer?')) return;
    evtState.jsDirty = false;
    evtState.editRev += 1;          // an in-flight fetch must not be skipped
    fetchAndShowEvents();
});

function slotLabel(n) {
    const name = slotNameOf(n);
    return name ? `slot ${n} ("${name}")` : `slot ${n}`;
}

// The buffer changed under the editor: never write these edits into
// whatever is loaded now — offer to throw them away instead.
function offerEditorReload(res) {
    const s = STATE.lastStatus || {};
    const loaded = (res && typeof res.loaded === 'number')
        ? res.loaded : (parseInt(s.macro_loaded, 10) || 0);
    const was = evtState.slot ? slotLabel(evtState.slot) : 'the previous macro';
    const now = loaded ? slotLabel(loaded) : 'a different macro';
    if (confirm(
        `The macro changed since you started editing (now ${now}).\n\n`
        + `Your edits were made to ${was} and were NOT saved, so they `
        + `can't overwrite ${now}.\n\nDiscard your edits and show the `
        + 'current macro?')) {
        evtState.jsDirty = false;
        evtState.editRev += 1;
        fetchAndShowEvents();
    }
    updateEvtButtons();
}

// Every row of a loaded slot was deleted. An empty macro can't be saved,
// so offer Clear slot instead (empties the buffer, then deletes the slot).
function offerClearInstead(slot) {
    const users = activeUsersOfSlot(slot);
    let msg = `You removed every event. An empty macro can't be saved to `
        + `${slotLabel(slot)}.\n\nClear slot ${slot} instead? This cannot `
        + 'be undone.';
    if (users.length) {
        msg += `\n\nActive macro ${users.join(', ')} uses this slot and `
            + 'will be unassigned.';
    }
    msg += '\n\n(Cancel keeps editing.)';
    if (!confirm(msg)) return;
    evtState.saving = true;
    updateEvtButtons();
    callApi('macro_set_events', [], evtState.rev, slot, false).then((res) => {
        if (!res || res.ok === false) {
            if (res && res.reason === 'stale') {
                offerEditorReload(res);
            } else {
                alert('Could not clear the macro: '
                    + ((res && res.message) || 'see the Logs tab.'));
            }
            return null;
        }
        evtState.rev = res.rev;
        return callApi('macro_clear_slot', slot).then((r2) => {
            if (!r2 || r2.ok === false) {
                alert(`The macro is now empty, but slot ${slot} could not `
                    + 'be cleared: '
                    + ((r2 && r2.message) || 'see the Logs tab.'));
            }
            evtState.jsDirty = false;
            evtState.editRev += 1;
            fetchAndShowEvents();
        });
    }).catch((e) => {
        console.error('[evt] clear failed', e);
    }).finally(() => {
        evtState.saving = false;
        updateEvtButtons();
    });
}

$('#evt-save').addEventListener('click', () => {
    if (evtState.saving || macroIsBusy()) return;
    if (editorIsStale()) {
        offerEditorReload(null);
        return;
    }
    // Coerce types one more time before sending — the inputs may have
    // produced strings even after our `input` handler ran.
    const sanitized = evtState.working.map((ev) => ({
        time: parseFloat(ev.time) || 0,
        device: (ev.device || 'keyboard').toLowerCase(),
        key: String(ev.key || '').trim().toLowerCase(),
        event_type: (ev.event_type || 'down').toLowerCase(),
    }));
    // The slot the edits were made to (pinned at fetch). Python applies
    // + writes back to it in ONE command and refuses if the revision or
    // loaded slot moved on in the meantime.
    const slot = evtState.slot;
    if (sanitized.length === 0 && slot > 0) {
        offerClearInstead(slot);
        return;
    }
    const editRevAtSave = evtState.editRev;
    evtState.saving = true;
    updateEvtButtons();
    callApi('macro_set_events', sanitized, evtState.rev, slot, slot > 0)
        .then((res) => {
            if (!res) {
                alert('Could not apply edits — see the Logs tab.');
                return;
            }
            if (res.ok === false && res.reason === 'stale') {
                offerEditorReload(res);
                return;
            }
            if (res.ok === false && res.reason === 'empty') {
                offerClearInstead(slot);
                return;
            }
            if (res.applied && typeof res.rev === 'number') {
                // Python now holds these edits (sorted, canonical names).
                evtState.rev = res.rev;
            }
            if (res.ok === false) {
                // Keep "unsaved edits" so Save can retry; the reason is
                // also in the status pill (macro_error).
                alert('Could not save your edits: '
                    + (res.message || res.reason || 'see the Logs tab.'));
                return;
            }
            const dropped = parseInt(res.dropped, 10) || 0;
            if (dropped > 0) {
                alert(`${dropped} row${dropped === 1 ? ' was' : 's were'} `
                    + 'dropped (unknown key, mouse button or event type, or a '
                    + 'key combination) — see the Logs tab. The table '
                    + 'now shows what was kept.');
            }
            if (evtState.editRev === editRevAtSave) {
                evtState.jsDirty = false;
                // Show what was actually stored (sorted, canonicalized,
                // invalid rows removed).
                fetchAndShowEvents();
            }
        })
        .catch((e) => {
            console.error('[evt] save failed', e);
        })
        .finally(() => {
            evtState.saving = false;
            updateEvtButtons();
        });
});

// Called on every status push. Re-fetches when the buffer revision
// moved (record finished, slot loaded, edits applied, slot cleared) and
// the user has no local edits; with local edits the dirty tag warns.
function maybeAutoRefreshEditor() {
    updateEvtButtons();
    if (!evtState.visible || evtState.jsDirty) return;
    const s = STATE.lastStatus || {};
    if (typeof s.macro_rev === 'number') {
        if (s.macro_rev === evtState.rev) return;
    } else if ((s.macro_events || 0) === evtState.working.length) {
        return;   // legacy fallback: no revision in the status
    }
    fetchAndShowEvents();
}

// --- log pane --------------------------------------------------------------

// Auto-scroll only "sticks" while the user is at (or within this many px
// of) the bottom — scrolling up to read / select pauses it.
const LOG_STICK_PX = 24;
const LOG_PANE = {
    chars: 0,       // characters currently in the pane
    stick: true,    // follow new output (last known, survives tab hides)
};

function logPaneAtBottom(pane) {
    return pane.scrollHeight - pane.scrollTop - pane.clientHeight <= LOG_STICK_PX;
}

$('#btn-clear-log').addEventListener('click', () => {
    $('#log-pane').textContent = '';
    LOG_PANE.chars = 0;
    LOG_PANE.stick = true;
    callApi('clear_log');
});

$('#log-pane').addEventListener('scroll', () => {
    const pane = $('#log-pane');
    if (pane.clientHeight === 0) return;   // hidden tab — keep last value
    LOG_PANE.stick = logPaneAtBottom(pane);
});

// The pane can't scroll while its tab is hidden; catch up on show.
$$('.tab[data-tab="logs"]').forEach((btn) => {
    btn.addEventListener('click', () => {
        if (!LOG_PANE.stick) return;
        requestAnimationFrame(() => {
            const pane = $('#log-pane');
            pane.scrollTop = pane.scrollHeight;
        });
    });
});

// --- globals called from Python -------------------------------------------

// Append-only: each chunk becomes a new text node (no rewrite of the whole
// 200k-char buffer per chunk), the front is trimmed in one batch once past
// LOG_MAX_CHARS, and the view only follows new output when it was already
// at the bottom — the detector logs every key DOWN/UP, and force-scrolling
// made it impossible to scroll up or select text during a song.
window.appendLog = function appendLog(text) {
    if (!text) return;
    const pane = $('#log-pane');
    const stick = pane.clientHeight > 0 ? logPaneAtBottom(pane) : LOG_PANE.stick;
    pane.appendChild(document.createTextNode(text));
    LOG_PANE.chars += text.length;
    if (LOG_PANE.chars > LOG_MAX_CHARS) {
        const trimTo = Math.floor(LOG_MAX_CHARS * 0.8);
        while (LOG_PANE.chars > trimTo && pane.firstChild) {
            const node = pane.firstChild;
            const len = (node.textContent || '').length;
            const excess = LOG_PANE.chars - trimTo;
            if (len <= excess) {
                pane.removeChild(node);
                LOG_PANE.chars -= len;
            } else {
                node.textContent = node.textContent.slice(excess);
                LOG_PANE.chars -= excess;
            }
        }
    }
    if (stick) pane.scrollTop = pane.scrollHeight;
};

// Status-card State cell: "<state>" or "<state> — <short error>" (warn
// colour) with the full message + a Logs pointer in the tooltip.
const STATE_ERROR_MAX = 28;
function renderStateCell(state, lastError) {
    const el = $('#st-state');
    const err = (lastError || '').trim();
    if (err) {
        const short = err.length > STATE_ERROR_MAX
            ? err.slice(0, STATE_ERROR_MAX - 1) + '…' : err;
        el.textContent = `${state} — ${short}`;
        el.title = `${err}\n(see the Logs tab for details)`;
        el.classList.add('warn');
    } else {
        el.textContent = state;
        el.title = '';
        el.classList.remove('warn');
    }
}

// Start / Pause buttons from the live state machine (+ the Start/Stop
// click debounce in MAIN.startPending).
function renderStartButtons(data) {
    const running = data.state === 'running' || data.state === 'paused';
    const stopping = data.state === 'stopping';
    const startBtn = $('#btn-start');
    const pauseBtn = $('#btn-pause');
    if (stopping) {
        startBtn.textContent = 'Stopping…';
        startBtn.disabled = true;
    } else {
        startBtn.textContent = running ? 'Stop' : 'Start';
        startBtn.disabled = !!MAIN.startPending;
    }
    pauseBtn.disabled = !running || stopping;
    pauseBtn.textContent = data.state === 'paused' ? 'Resume' : 'Pause';
}

window.updateStatus = function updateStatus(data) {
    if (!data) return;
    STATE.lastStatus = data;

    // Main-tab bot status. Start/Stop debounce ends as soon as a push
    // shows the state (or last_error) moved.
    const botState = data.state || 'idle';
    const botLive = botState === 'running' || botState === 'paused';
    if (MAIN.startPending && (botState !== MAIN.startPending.state
            || (data.last_error || '') !== MAIN.startPending.lastError)) {
        clearStartPending();
    }
    if ('state' in data || 'last_error' in data) {
        renderStateCell(botState, data.last_error);
    }
    if ('song' in data) $('#st-song').textContent = data.song || '—';
    if ('fps' in data) {
        const f = parseFloat(data.fps) || 0;
        $('#st-fps').textContent = f ? f.toFixed(1) : '—';
    }
    if ('debug' in data) {
        // While idle `debug` is the preset for the next Start, not a live
        // viz window — say so instead of a stale "on".
        $('#st-debug').textContent = !data.debug ? 'off'
            : (botLive ? 'on' : 'on at next start');
    }
    if ('admin' in data) {
        const el = $('#st-admin');
        if (data.admin) {
            el.textContent = 'yes';
            el.title = '';
            el.classList.add('ok');
            el.classList.remove('warn');
        } else {
            el.textContent = 'NO — input blocked in-game';
            el.title = 'Not running as administrator. The game runs '
                + 'elevated, so Windows blocks this app\'s hotkeys and '
                + 'Software-input keypresses/clicks from reaching it. '
                + 'Relaunch as administrator.';
            el.classList.add('warn');
            el.classList.remove('ok');
        }
    }
    if ('keybinds' in data) renderKeybinds(data.keybinds);

    if ('input_backend' in data) {
        const sel = $('#input-backend');
        if (sel && sel.value !== data.input_backend) {
            sel.value = data.input_backend;
        }
    }

    if ('macro_state' in data || 'macro_events' in data
        || 'macro_loaded' in data || 'macro_slot_names' in data
        || 'macro_dirty' in data || 'macro_playing_slot' in data
        || 'macro_playing_source' in data || 'macro_error' in data
        || 'macro_playing_events' in data) {
        renderMacroState(STATE.lastStatus || data);
    }
    if ('macro_events' in data || 'macro_rev' in data
        || 'macro_state' in data || 'macro_loaded' in data) {
        // Buffer changed externally (record finished, slot loaded,
        // edits applied) — pull a fresh copy into the editor unless
        // the user has unsaved local changes (revision-based, see
        // evtState).
        maybeAutoRefreshEditor();
    }
    if ('macro_slots' in data || 'macro_slot_names' in data
        || 'macro_loaded' in data || 'macro_state' in data) {
        // Slot names + loaded indicator may arrive in a later snapshot
        // than the slot list itself, so always re-render with the most
        // recent values from STATE.lastStatus rather than just `data`.
        const last = STATE.lastStatus || {};
        renderMacroSlots(
            data.macro_slots || last.macro_slots || [],
            data.macro_slot_names || last.macro_slot_names || {},
            data.macro_loaded || last.macro_loaded || 0,
            data.macro_state || last.macro_state || 'idle');
    }
    if ('macro_pending' in data) renderMacroPending(data.macro_pending);
    if ('macro_active_macros' in data || 'macro_conflict_policy' in data
        || 'macro_slots' in data || 'macro_slot_names' in data
        || 'macro_state' in data || 'macro_playing_slot' in data) {
        renderActiveMacroRows(STATE.lastStatus || {});
        renderActivePolicy((STATE.lastStatus || {}).macro_conflict_policy);
    }

    // Sync debug checkbox without echoing back to Python.
    if ('debug' in data) {
        const cb = $('#debug');
        if (cb.checked !== !!data.debug) {
            cb.checked = !!data.debug;
        }
    }

    // Button states from the live state machine.
    renderStartButtons(data);

    // The debug checkbox stays enabled while idle: it is the preset the
    // next Start opens the viz window with (Python owns the value, the
    // sync above mirrors it).

    // Rebind buttons stay enabled regardless of bot state — user can
    // configure hotkeys without first starting the bot. The hotkeys
    // themselves are still inert when the bot is off (runtime gate in
    // ui.py:_hotkey_pause / _hotkey_debug).
};

const _MOUSE_DISPLAY = {
    left: 'Left Click', right: 'Right Click', middle: 'Middle Click',
    x: 'Mouse 4', x2: 'Mouse 5',
};

function displayBinding(binding) {
    if (!binding) return '<unset>';
    const m = /^mouse:(.+)$/i.exec(binding);
    if (m) {
        const name = m[1].toLowerCase();
        return _MOUSE_DISPLAY[name] || `Mouse ${m[1]}`;
    }
    return binding;
}

function renderKeybinds(kb) {
    Object.entries(kb || {}).forEach(([action, hotkey]) => {
        const row = document.querySelector(`.hk-row[data-action="${action}"]`);
        if (!row) return;
        const el = row.querySelector('.hk-binding');
        el.textContent = displayBinding(hotkey);
        // Mouse hotkeys see every click of that button, including clicks a
        // playing macro replays. A Left Click saved by an older build is
        // refused at install time (it is every attack), so say so.
        const hk = String(hotkey || '').toLowerCase();
        el.title = hk === 'mouse:left'
            ? 'Left Click is not active as a hotkey - rebind this.'
            : (hk.startsWith('mouse:')
                ? 'Mouse hotkeys also fire when a macro replays this button.'
                : '');
    });
}

// --- init ------------------------------------------------------------------

// --- help-th portal tooltips ----------------------------------------------

// `.help-th` icons sit inside `.evt-list-wrapper` which has
// `overflow-y: auto` (scrollable event list). Auto-overflow clips the
// standard `.help::after` pseudo-element tooltip against the wrapper's
// box on both axes, so when the wrapper is short (few or no events)
// the tooltip is invisible. Render the tooltip in `document.body`
// instead, positioned via getBoundingClientRect — it lives outside the
// overflow context and stays visible regardless of wrapper size.
function setupHelpThPortals() {
    document.querySelectorAll('.help-th').forEach((icon) => {
        const text = icon.getAttribute('data-help');
        if (!text) return;
        let tip = null;

        const position = () => {
            if (!tip) return;
            const r = icon.getBoundingClientRect();
            tip.style.top = (r.bottom + 8) + 'px';
            // Clamp left so the 320px-max tooltip doesn't overflow
            // the right edge of the viewport on narrow windows.
            const maxLeft = window.innerWidth - 320 - 8;
            tip.style.left = Math.max(8, Math.min(r.left, maxLeft)) + 'px';
        };

        const show = () => {
            if (tip) return;
            tip = document.createElement('div');
            tip.className = 'help-th-tooltip';
            tip.textContent = text;
            document.body.appendChild(tip);
            position();
        };

        const hide = () => {
            if (tip) {
                tip.remove();
                tip = null;
            }
        };

        icon.addEventListener('mouseenter', show);
        icon.addEventListener('mouseleave', hide);
        icon.addEventListener('focus', show);
        icon.addEventListener('blur', hide);
    });
}

// Help icons live in static HTML so the DOM is ready by the time this
// script (at end of body) executes. Wiring is independent of the
// pywebview API readiness — don't gate on init().
setupHelpThPortals();


function init() {
    if (STATE.initialized) return;
    STATE.initialized = true;
    callApi('get_initial_state').then((s) => {
        if (!s) return;
        if (s.mode) {
            const r = document.querySelector(`input[name="mode"][value="${s.mode}"]`);
            if (r) r.checked = true;
        }
        if (typeof s.songs === 'number') {
            $('#songs').value = s.songs;
            MAIN.lastSongs = s.songs;
        }
        if (s.difficulty !== undefined) applyDifficultiesToCheckboxes(s.difficulty);
        if (typeof s.replay_canorus === 'boolean') $('#replay-canorus').checked = s.replay_canorus;
        if (typeof s.debug === 'boolean') $('#debug').checked = s.debug;
        if (s.input_backend) $('#input-backend').value = s.input_backend;
        if (s.keybinds) renderKeybinds(s.keybinds);
        applyModeUi();
        // Initial status payload — fold macro snapshot in so the
        // Macros tab renders correctly without waiting for the first
        // drain tick.
        const macroSnap = s.macro || {};
        window.updateStatus({
            state: 'idle',
            last_error: s.last_error || '',
            song: '',
            fps: 0,
            debug: !!s.debug,
            admin: !!s.admin,
            keybinds: s.keybinds || {},
            macro_state: macroSnap.macro_state || 'idle',
            macro_events: macroSnap.macro_events || 0,
            macro_slots: macroSnap.macro_slots || [],
            macro_slot_names: macroSnap.macro_slot_names || {},
            macro_loaded: macroSnap.macro_loaded || 0,
            macro_dirty: !!macroSnap.macro_dirty,
            macro_pending: macroSnap.macro_pending || '',
            macro_playing_slot: macroSnap.macro_playing_slot || 0,
            macro_playing_name: macroSnap.macro_playing_name || '',
            macro_playing_source: macroSnap.macro_playing_source || '',
            macro_playing_events: macroSnap.macro_playing_events || 0,
            macro_rev: typeof macroSnap.macro_rev === 'number'
                ? macroSnap.macro_rev : 0,
            macro_error: macroSnap.macro_error || '',
            macro_active_macros: s.macro_active_macros || [],
            macro_conflict_policy: s.macro_conflict_policy || 'ignore',
            input_backend: s.input_backend || 'arduino',
        });
    });
}

window.addEventListener('pywebviewready', init);
// pywebviewready may have fired before this script ran in some loads.
if (window.pywebview && window.pywebview.api) init();
