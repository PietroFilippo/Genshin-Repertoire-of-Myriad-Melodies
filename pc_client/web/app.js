// Frontend logic. Backend is `BridgeApi` in pc_client/ui.py exposed as
// `pywebview.api`. Two globals (updateStatus, appendLog) are called from
// Python via window.evaluate_js on the 50ms drain tick.
'use strict';

const STATE = {
    capturing: null,           // action name while waiting for keypress
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
$('#btn-max').addEventListener('click', () => callApi('toggle_maximize'));
$('#btn-close').addEventListener('click', () => callApi('close'));

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

$('#songs').addEventListener('change', () => {
    const v = parseInt($('#songs').value, 10);
    if (!Number.isNaN(v)) callApi('set_songs', v);
});

$('#difficulty').addEventListener('change', () => {
    callApi('set_difficulty', $('#difficulty').value);
});

$('#replay-canorus').addEventListener('change', () => {
    callApi('set_replay_canorus', $('#replay-canorus').checked);
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

$('#btn-start').addEventListener('click', () => {
    // If a macro is busy and the bot is currently idle, this click would
    // start the bot. Confirm so the user knows the macro will be killed.
    if (!botIsRunning() && macroIsBusy()) {
        const action = (STATE.lastStatus || {}).macro_state;
        const verb = action === 'recording' ? 'recording' : 'macro playback';
        if (!confirm(`A macro is ${verb}. Stop it and start the bot?`)) return;
        callApi('start_stop_force');
        return;
    }
    callApi('start_stop');
});
$('#btn-pause').addEventListener('click', () => callApi('pause'));

// --- hotkey rebind ---------------------------------------------------------

$$('.hk-row').forEach((row) => {
    const action = row.dataset.action;
    const btn = row.querySelector('.rebind');
    btn.addEventListener('click', () => beginCapture(action));
});

function captureMsgFor(action) {
    // Each card has its own capture message line — find it via the row.
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    if (!row) return null;
    const card = row.closest('.card');
    return card ? card.querySelector('.hk-capture-msg') : null;
}

function beginCapture(action) {
    if (STATE.capturing) return;
    STATE.capturing = action;
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    row.querySelector('.rebind').textContent = '…';
    const msg = captureMsgFor(action);
    if (msg) {
        msg.textContent =
            `Press a key or mouse button to bind ${labelFor(action)} (Esc to cancel)…`;
    }
}

function endCapture() {
    if (!STATE.capturing) return;
    const action = STATE.capturing;
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    if (row) row.querySelector('.rebind').textContent = 'Rebind';
    const msg = captureMsgFor(action);
    if (msg) msg.textContent = '';
    STATE.capturing = null;
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
    // Capture mode: any non-modifier key gets sent to Python.
    if (STATE.capturing) {
        ev.preventDefault();
        ev.stopPropagation();
        if (ev.key === 'Escape') {
            endCapture();
            return;
        }
        if (_MODIFIER_KEYS.has(ev.key)) return;
        const payload = {
            key: ev.key,
            code: ev.code,
            ctrlKey: ev.ctrlKey,
            altKey: ev.altKey,
            shiftKey: ev.shiftKey,
            metaKey: ev.metaKey,
        };
        callApi('rebind', STATE.capturing, payload).then(() => endCapture());
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
    if (!STATE.capturing) return;
    // Don't swallow clicks on the rebind button itself — the click that
    // started capture also bubbles a mousedown here. Detect via the
    // active capturing row's button.
    const row = document.querySelector(
        `.hk-row[data-action="${STATE.capturing}"]`);
    if (row && ev.target.closest('.rebind') === row.querySelector('.rebind')) {
        return;
    }
    const name = _BROWSER_BUTTON_TO_NAME[ev.button];
    if (!name) return;
    ev.preventDefault();
    ev.stopPropagation();
    callApi('rebind', STATE.capturing, { mouseButton: name })
        .then(() => endCapture());
}, true);
// Also block contextmenu while capturing so right-click capture doesn't
// pop the native menu.
window.addEventListener('contextmenu', (ev) => {
    if (STATE.capturing) ev.preventDefault();
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

$$('.slot').forEach((btn) => {
    btn.addEventListener('click', () => {
        const n = parseInt(btn.dataset.slot, 10);
        if (Number.isNaN(n)) return;
        const occupied = btn.classList.contains('slot-occupied');
        if (macroSlotMode === 'load') {
            if (!occupied) return;
            callApi('macro_load_slot', n);
        } else if (macroSlotMode === 'save') {
            const existing = slotNameOf(n);
            if (occupied) {
                const label = existing ? `"${existing}"` : 'a saved macro';
                if (!confirm(
                    `Slot ${n} already contains ${label}. ` +
                    `Overwrite with the current macro?`)) {
                    return;
                }
            }
            const promptMsg = occupied
                ? `Overwrite slot ${n} — name (leave blank for none):`
                : `Save to slot ${n} — name (leave blank for none):`;
            const name = window.prompt(promptMsg, existing);
            if (name === null) return;   // cancelled at name prompt
            callApi('macro_save_slot', n, name);
        } else if (macroSlotMode === 'rename') {
            if (!occupied) return;
            const existing = slotNameOf(n);
            const name = window.prompt(`Rename slot ${n}:`, existing);
            if (name === null) return;
            callApi('macro_rename_slot', n, name);
        } else if (macroSlotMode === 'clear') {
            if (!occupied) return;
            if (confirm(`Clear macro slot ${n}? This cannot be undone.`)) {
                callApi('macro_clear_slot', n);
            }
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
    callApi('macro_toggle_record', false).then((res) => {
        if (res && res.ok === false && res.reason === 'bot_running') {
            if (confirm('The bot is running. Stop it and start recording?')) {
                callApi('macro_toggle_record', true);
            }
        }
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

function renderMacroSlots(slots, namesMap, loadedSlot) {
    const occupied = new Set(slots || []);
    const names = namesMap || {};
    const loaded = parseInt(loadedSlot, 10) || 0;
    $$('.slot').forEach((btn) => {
        const n = parseInt(btn.dataset.slot, 10);
        const isOcc = occupied.has(n);
        btn.classList.toggle('slot-occupied', isOcc);
        btn.classList.toggle('slot-loaded', isOcc && n === loaded);
        const nameEl = btn.querySelector('.slot-name');
        if (!nameEl) return;
        if (!isOcc) {
            nameEl.textContent = 'empty';
            return;
        }
        const name = names[n] || names[String(n)] || '';
        nameEl.textContent = name || 'saved';
    });
}

function renderMacroState(macroState, eventCount, loadedSlot, slotNames, dirty) {
    const statusEl = $('#macro-status');
    const recBtn = $('#macro-record');
    const playBtn = $('#macro-play');
    const stopBtn = $('#macro-stop-play');
    const events = eventCount || 0;
    const stateLabel = macroState || 'idle';
    const loaded = parseInt(loadedSlot, 10) || 0;
    const isDirty = !!dirty;
    let suffix = '';
    if (loaded > 0) {
        const names = slotNames || {};
        const name = names[loaded] || names[String(loaded)] || '';
        const tag = isDirty ? '*' : '';
        suffix = name
            ? ` · slot ${loaded}${tag} (${name})`
            : ` · slot ${loaded}${tag}`;
    } else if (isDirty && events > 0) {
        suffix = ' · unsaved';
    }
    const fullText =
        `${stateLabel} · ${events} event${events === 1 ? '' : 's'}${suffix}`;
    statusEl.textContent = fullText;
    // CSS truncates long slot names with ellipsis; mirror the full
    // string on `title` so hover still reveals what got cut.
    statusEl.title = fullText;
    statusEl.classList.toggle('macro-status-recording', stateLabel === 'recording');
    statusEl.classList.toggle('macro-status-playing', stateLabel === 'playing');
    recBtn.textContent = stateLabel === 'recording' ? 'Stop Recording' : 'Record';
    recBtn.disabled = stateLabel === 'playing';
    playBtn.disabled = stateLabel !== 'idle' || events === 0;
    stopBtn.disabled = stateLabel !== 'playing';
}

function renderMacroPending(pending) {
    const el = $('#macro-pending');
    if (pending === 'save') {
        el.textContent = 'Save: press 1-9 inside Genshin to choose a slot (Esc to cancel)';
    } else if (pending === 'load') {
        el.textContent = 'Load: press 1-9 inside Genshin to choose a slot (Esc to cancel)';
    } else {
        el.textContent = '';
    }
}

// --- events editor ---------------------------------------------------------

const evtState = {
    visible: false,
    working: [],         // local edit copy
    jsDirty: false,      // local edits not yet pushed to Python
    lastFetchedCount: 0, // events count at the moment we last fetched
};

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
                updateEvtButtons();
            });
        });
        tr.querySelector('.evt-del').addEventListener('click', () => {
            evtState.working.splice(i, 1);
            evtState.jsDirty = true;
            renderEventsTable();
            updateEvtButtons();
        });
        tbody.appendChild(tr);
    });
    $('#evt-count').textContent = `(${evtState.working.length})`;
}

function updateEvtButtons() {
    const visible = evtState.visible;
    $('#evt-add').disabled = !visible;
    $('#evt-save').disabled = !visible || !evtState.jsDirty;
    $('#evt-discard').disabled = !visible || !evtState.jsDirty;
    const tag = $('#evt-dirty-tag');
    tag.textContent = (visible && evtState.jsDirty) ? 'unsaved edits' : '';
}

function fetchAndShowEvents() {
    return callApi('macro_get_events').then((events) => {
        evtState.working = Array.isArray(events) ? events : [];
        evtState.lastFetchedCount = evtState.working.length;
        evtState.jsDirty = false;
        renderEventsTable();
        updateEvtButtons();
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
    renderEventsTable();
    updateEvtButtons();
});

$('#evt-discard').addEventListener('click', () => {
    if (!confirm('Discard your edits and reload from the buffer?')) return;
    fetchAndShowEvents();
});

$('#evt-save').addEventListener('click', () => {
    // Coerce types one more time before sending — the inputs may have
    // produced strings even after our `input` handler ran.
    const sanitized = evtState.working.map((ev) => ({
        time: parseFloat(ev.time) || 0,
        device: (ev.device || 'keyboard').toLowerCase(),
        key: String(ev.key || '').trim().toLowerCase(),
        event_type: (ev.event_type || 'down').toLowerCase(),
    }));
    callApi('macro_set_events', sanitized).then((res) => {
        if (!res || res.ok === false) {
            alert('Could not apply edits — see the log for details.');
            return;
        }
        // If a slot is currently loaded, persist back to it so the
        // user doesn't have to hop over to the slot grid for a
        // round-trip Save.
        const loaded = (STATE.lastStatus || {}).macro_loaded || 0;
        if (loaded > 0) {
            const name = slotNameOf(loaded);
            callApi('macro_save_slot', loaded, name);
        }
        evtState.jsDirty = false;
        // Refresh the working copy from Python so any sort/cleanup
        // applied in set_events shows up in the table.
        fetchAndShowEvents();
    });
});

function maybeAutoRefreshEditor(eventsCount) {
    if (!evtState.visible) return;
    if (eventsCount === evtState.lastFetchedCount) return;
    if (evtState.jsDirty) return;   // user has local edits, leave alone
    fetchAndShowEvents();
}

// --- log pane --------------------------------------------------------------

$('#btn-clear-log').addEventListener('click', () => {
    $('#log-pane').textContent = '';
    callApi('clear_log');
});

// --- globals called from Python -------------------------------------------

window.appendLog = function appendLog(text) {
    if (!text) return;
    const pane = $('#log-pane');
    pane.textContent += text;
    if (pane.textContent.length > LOG_MAX_CHARS) {
        const trimTo = Math.floor(LOG_MAX_CHARS * 0.8);
        pane.textContent = pane.textContent.slice(-trimTo);
    }
    pane.scrollTop = pane.scrollHeight;
};

window.updateStatus = function updateStatus(data) {
    if (!data) return;
    STATE.lastStatus = data;

    if ('state' in data) $('#st-state').textContent = data.state || 'idle';
    if ('song' in data) $('#st-song').textContent = data.song || '—';
    if ('fps' in data) {
        const f = parseFloat(data.fps) || 0;
        $('#st-fps').textContent = f ? f.toFixed(1) : '—';
    }
    if ('debug' in data) $('#st-debug').textContent = data.debug ? 'on' : 'off';
    if ('admin' in data) {
        const el = $('#st-admin');
        if (data.admin) {
            el.textContent = 'yes';
            el.classList.add('ok');
            el.classList.remove('warn');
        } else {
            el.textContent = 'NO — hotkeys blocked in-game';
            el.classList.add('warn');
            el.classList.remove('ok');
        }
    }
    if ('keybinds' in data) renderKeybinds(data.keybinds);

    if ('macro_state' in data || 'macro_events' in data
        || 'macro_loaded' in data || 'macro_slot_names' in data
        || 'macro_dirty' in data) {
        renderMacroState(data.macro_state, data.macro_events,
                         data.macro_loaded, data.macro_slot_names,
                         data.macro_dirty);
    }
    if ('macro_events' in data) {
        // Buffer changed externally (record finished, slot loaded,
        // edits applied) — pull a fresh copy into the editor unless
        // the user has unsaved local changes.
        maybeAutoRefreshEditor(data.macro_events);
    }
    if ('macro_slots' in data || 'macro_slot_names' in data
        || 'macro_loaded' in data) {
        // Slot names + loaded indicator may arrive in a later snapshot
        // than the slot list itself, so always re-render with the most
        // recent values from STATE.lastStatus rather than just `data`.
        const last = STATE.lastStatus || {};
        renderMacroSlots(
            data.macro_slots || last.macro_slots || [],
            data.macro_slot_names || last.macro_slot_names || {},
            data.macro_loaded || last.macro_loaded || 0);
    }
    if ('macro_pending' in data) renderMacroPending(data.macro_pending);

    // Sync debug checkbox without echoing back to Python.
    if ('debug' in data) {
        const cb = $('#debug');
        if (cb.checked !== !!data.debug) {
            cb.checked = !!data.debug;
        }
    }

    // Button states from the live state machine.
    const running = data.state === 'running' || data.state === 'paused';
    const stopping = data.state === 'stopping';
    const startBtn = $('#btn-start');
    const pauseBtn = $('#btn-pause');
    if (stopping) {
        startBtn.textContent = 'Stopping…';
        startBtn.disabled = true;
    } else {
        startBtn.textContent = running ? 'Stop' : 'Start';
        startBtn.disabled = false;
    }
    pauseBtn.disabled = !running || stopping;
    pauseBtn.textContent = data.state === 'paused' ? 'Resume' : 'Pause';

    // Debug controls only meaningful while bot runs.
    const debugCb = $('#debug');
    debugCb.disabled = !running;
    if (!running && debugCb.checked) debugCb.checked = false;

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
        row.querySelector('.hk-binding').textContent = displayBinding(hotkey);
    });
}

// --- init ------------------------------------------------------------------

function init() {
    if (STATE.initialized) return;
    STATE.initialized = true;
    callApi('get_initial_state').then((s) => {
        if (!s) return;
        if (s.mode) {
            const r = document.querySelector(`input[name="mode"][value="${s.mode}"]`);
            if (r) r.checked = true;
        }
        if (typeof s.songs === 'number') $('#songs').value = s.songs;
        if (s.difficulty) $('#difficulty').value = s.difficulty;
        if (typeof s.replay_canorus === 'boolean') $('#replay-canorus').checked = s.replay_canorus;
        if (typeof s.debug === 'boolean') $('#debug').checked = s.debug;
        if (s.keybinds) renderKeybinds(s.keybinds);
        applyModeUi();
        // Initial status payload — fold macro snapshot in so the
        // Macros tab renders correctly without waiting for the first
        // drain tick.
        const macroSnap = s.macro || {};
        window.updateStatus({
            state: 'idle',
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
        });
    });
}

window.addEventListener('pywebviewready', init);
// pywebviewready may have fired before this script ran in some loads.
if (window.pywebview && window.pywebview.api) init();
