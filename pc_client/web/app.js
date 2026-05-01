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

function applyModeUi() {
    const album = modeIsAlbum();
    const card = $('#album-card');
    card.classList.toggle('disabled', !album);
    $$('#album-card input, #album-card select, #album-card button').forEach((el) => {
        el.disabled = !album;
    });
    callApi('set_mode', album ? 'album' : 'standalone');
}

$$('input[name="mode"]').forEach((r) => {
    r.addEventListener('change', applyModeUi);
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

$('#btn-start').addEventListener('click', () => callApi('start_stop'));
$('#btn-pause').addEventListener('click', () => callApi('pause'));

// --- hotkey rebind ---------------------------------------------------------

$$('.hk-row').forEach((row) => {
    const action = row.dataset.action;
    const btn = row.querySelector('.rebind');
    btn.addEventListener('click', () => beginCapture(action));
});

function beginCapture(action) {
    if (STATE.capturing) return;
    STATE.capturing = action;
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    row.querySelector('.rebind').textContent = '…';
    $('#capture-msg').textContent = `Press a key to bind ${labelFor(action)} (Esc to cancel)…`;
}

function endCapture() {
    if (!STATE.capturing) return;
    const row = document.querySelector(`.hk-row[data-action="${STATE.capturing}"]`);
    if (row) row.querySelector('.rebind').textContent = 'Rebind';
    STATE.capturing = null;
    $('#capture-msg').textContent = '';
}

function labelFor(action) {
    return ({
        start_stop: 'Start/Stop',
        pause: 'Pause/Resume',
        debug: 'Toggle Debug',
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

    // Pause/Debug rebind buttons follow the same gating.
    document.querySelectorAll('.hk-row[data-action="pause"] .rebind').forEach((b) => {
        b.disabled = !running;
    });
    document.querySelectorAll('.hk-row[data-action="debug"] .rebind').forEach((b) => {
        b.disabled = !running;
    });
};

function renderKeybinds(kb) {
    Object.entries(kb || {}).forEach(([action, hotkey]) => {
        const row = document.querySelector(`.hk-row[data-action="${action}"]`);
        if (!row) return;
        row.querySelector('.hk-binding').textContent = hotkey || '<unset>';
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
        // Initial status payload.
        window.updateStatus({
            state: 'idle',
            song: '',
            fps: 0,
            debug: !!s.debug,
            admin: !!s.admin,
            keybinds: s.keybinds || {},
        });
    });
}

window.addEventListener('pywebviewready', init);
// pywebviewready may have fired before this script ran in some loads.
if (window.pywebview && window.pywebview.api) init();
