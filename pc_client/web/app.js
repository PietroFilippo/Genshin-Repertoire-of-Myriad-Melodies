// Frontend logic. Backend is `BridgeApi` in pc_client/ui.py exposed as
// `pywebview.api`. Two globals (updateStatus, appendLog) are called from
// Python via window.evaluate_js on the 50ms drain tick.
//
// Layout is Direction A ("Score sheet") from genshin-ui-mocks/design-notes.md;
// the Python <-> UI field names follow genshin-ui-mocks/ui-data-contract.md.
// Every new status field degrades gracefully when absent (older Python).
'use strict';

const STATE = {
    capturing: null,           // hotkey action name while waiting for a key
    activeCapturing: null,     // active-macro position while binding its key
    activeRenderSig: '',
    slotsRenderSig: '',
    lastStatus: null,          // most recent merged status snapshot
    initialized: false,
    tab: 'main',
};

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

// Write-if-changed setters: the 50 ms status tick must not touch the DOM
// (and trigger style/layout work) when nothing moved.
function setText(el, text) {
    if (!el) return;
    const t = text == null ? '' : String(text);
    if (el.textContent !== t) el.textContent = t;
}

function setTitle(el, text) {
    if (!el) return;
    const t = text == null ? '' : String(text);
    if (el.title !== t) el.title = t;
}

function setHidden(el, hidden) {
    if (!el) return;
    const h = !!hidden;
    if (el.hidden !== h) el.hidden = h;
}

function setDisabled(el, disabled) {
    if (!el) return;
    const d = !!disabled;
    if (el.disabled !== d) el.disabled = d;
}

function setClass(el, cls, on) {
    if (!el) return;
    el.classList.toggle(cls, !!on);
}

function setSwitch(el, on) {
    if (!el) return;
    setClass(el, 'on', on);
    const v = on ? 'true' : 'false';
    if (el.getAttribute('aria-checked') !== v) el.setAttribute('aria-checked', v);
}

function plural(n, word) {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
}

function activeEl() {
    return document.activeElement || null;
}

// --- in-app dialog -----------------------------------------------------------

// One dialog at a time; requests queue behind the open one. Escape cancels,
// Enter confirms (unless a dialog button has focus — its native click wins),
// Tab is trapped inside the box, and focus returns to where it was.
const DIALOG = {
    open: false,
    done: null,
    onEnter: null,
    cancelValue: null,
    prevFocus: null,
    chain: Promise.resolve(),
};

const _FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusablesIn(box) {
    return Array.from(box.querySelectorAll(_FOCUSABLE));
}

function showDialog(build, cancelValue) {
    const run = () => new Promise((resolve) => {
        if (captureArmed()) cancelCapture();
        const scrim = $('#dialog-scrim');
        const box = $('#dialog-box');
        box.innerHTML = '';
        box.classList.remove('wide');
        DIALOG.prevFocus = activeEl();
        let settled = false;
        const done = (result) => {
            if (settled) return;
            settled = true;
            DIALOG.open = false;
            DIALOG.done = null;
            DIALOG.onEnter = null;
            scrim.hidden = true;
            box.innerHTML = '';
            const pf = DIALOG.prevFocus;
            DIALOG.prevFocus = null;
            if (pf && typeof pf.focus === 'function') {
                try { pf.focus(); } catch (e) { /* element may be gone */ }
            }
            resolve(result);
        };
        DIALOG.open = true;
        DIALOG.done = done;
        DIALOG.cancelValue = cancelValue;
        const spec = build(box, done) || {};
        DIALOG.onEnter = spec.onEnter || null;
        scrim.hidden = false;
        const target = spec.initialFocus || focusablesIn(box)[0];
        if (target && typeof target.focus === 'function') target.focus();
    });
    const p = DIALOG.chain.then(run, run);
    DIALOG.chain = p.then(() => {}, () => {});
    return p;
}

function dlgEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
}

function dlgButton(label, cls, onClick) {
    const b = dlgEl('button', `btn sm${cls ? ` ${cls}` : ''}`, label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
}

// Yes/No question. Resolves true / false.
function dlgConfirm(title, body, opts = {}) {
    return showDialog((box, done) => {
        box.appendChild(dlgEl('h2', '', title)).id = 'dialog-title';
        if (body) box.appendChild(dlgEl('p', '', body));
        if (opts.note) box.appendChild(dlgEl('p', 'note', opts.note));
        const row = dlgEl('div', 'row');
        const cancel = dlgButton(opts.cancel || 'Cancel', '', () => done(false));
        const ok = dlgButton(opts.ok || 'OK', opts.danger ? 'stop' : 'go', () => done(true));
        row.appendChild(cancel);
        row.appendChild(ok);
        box.appendChild(row);
        return { initialFocus: opts.focusCancel ? cancel : ok, onEnter: () => done(true) };
    }, false);
}

// Information only. Resolves when dismissed.
function dlgAlert(title, body) {
    return showDialog((box, done) => {
        box.appendChild(dlgEl('h2', '', title)).id = 'dialog-title';
        if (body) box.appendChild(dlgEl('p', '', body));
        const row = dlgEl('div', 'row');
        const ok = dlgButton('OK', 'go', () => done(true));
        row.appendChild(ok);
        box.appendChild(row);
        return { initialFocus: ok, onEnter: () => done(true) };
    }, true);
}

// One text field. Resolves the string, or null when cancelled.
function dlgPrompt(title, body, opts = {}) {
    return showDialog((box, done) => {
        box.appendChild(dlgEl('h2', '', title)).id = 'dialog-title';
        if (body) box.appendChild(dlgEl('p', '', body));
        const label = dlgEl('label', 'f', opts.label || 'Name');
        label.htmlFor = 'dialog-text';
        box.appendChild(label);
        const input = dlgEl('input', 'text');
        input.type = 'text';
        input.id = 'dialog-text';
        input.value = opts.value || '';
        input.spellcheck = false;
        if (opts.placeholder) input.placeholder = opts.placeholder;
        box.appendChild(input);
        const row = dlgEl('div', 'row');
        row.appendChild(dlgButton('Cancel', '', () => done(null)));
        row.appendChild(dlgButton(opts.ok || 'OK', 'go', () => done(input.value)));
        box.appendChild(row);
        setTimeout(() => { try { input.select(); } catch (e) { /* no-op */ } }, 0);
        return { initialFocus: input, onEnter: () => done(input.value) };
    }, null);
}

// --- titlebar buttons ------------------------------------------------------

$('#btn-min').addEventListener('click', () => callApi('minimize'));
$('#btn-max').addEventListener('click', () => {
    // Python snaps to the monitor work area (taskbar stays visible) and
    // reports the new state; swap glyph + tooltip to match.
    callApi('toggle_maximize').then((res) => {
        if (!res || typeof res.maximized !== 'boolean') return;
        const btn = $('#btn-max');
        btn.textContent = res.maximized ? '\uE923' : '\uE922';
        btn.title = res.maximized ? 'Restore' : 'Maximize';
    });
});
$('#btn-close').addEventListener('click', async () => {
    // Unsaved macro work (recording, unsaved buffer, editor edits) only
    // lives in memory — ask before throwing it away. The rule must match
    // Python's allow_close() (`recording or dirty`), otherwise a case the
    // page skips is asked again natively after close().
    const loss = macroUnsavedSummary(true);
    if (loss) {
        const ok = await dlgConfirm('Close Genshin Rhythm Bot?',
            `You have ${loss} that will be lost.`,
            { ok: 'Discard and close', danger: true, focusCancel: true });
        if (!ok) return;
    }
    // The user already answered here; Python's `closing` handler must
    // not ask again. Mark it on the window (readable via evaluate_js)
    // and pass it as an argument; fall back to the no-argument close()
    // for a Python side that predates the flag.
    window.__closeConfirmed = true;
    callApi('close', true).catch(() => callApi('close'));
});

// --- tab switching ---------------------------------------------------------

function showTab(name) {
    STATE.tab = name;
    $$('.tab').forEach((t) => t.classList.toggle('tab-active', t.dataset.tab === name));
    $$('.panel').forEach((p) => p.classList.toggle('panel-active', p.dataset.panel === name));
    if (name === 'logs') logCatchUp();
}

$$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => showTab(btn.dataset.tab));
});

$$('[data-goto]').forEach((btn) => {
    btn.addEventListener('click', () => showTab(btn.dataset.goto));
});

// --- health strip ------------------------------------------------------------

function renderHealthAdmin(admin) {
    const el = $('#hs-admin');
    const dot = el.querySelector('.dot');
    const label = el.querySelector('span');
    if (admin) {
        setClass(dot, 'bad', false);
        setClass(dot, 'neutral', false);
        setClass(el, 'bad', false);
        setText(label, 'Running as admin');
        setTitle(el, '');
    } else {
        setClass(dot, 'neutral', false);
        setClass(dot, 'bad', true);
        setClass(el, 'bad', true);
        setText(label, 'Not running as admin');
        setTitle(el, 'The game runs elevated, so Windows blocks this app\'s '
            + 'hotkeys and Software-input keypresses from reaching it. '
            + 'Relaunch as administrator.');
    }
}

function renderHealthGame(found) {
    const el = $('#hs-game');
    const dot = el.querySelector('.dot');
    const label = el.querySelector('span');
    if (found === true) {
        setClass(dot, 'neutral', false);
        setClass(dot, 'warn', false);
        setText(label, 'Game window found');
        setTitle(el, '');
    } else if (found === false) {
        setClass(dot, 'neutral', false);
        setClass(dot, 'warn', true);
        setText(label, 'Game window not found');
        setTitle(el, 'Genshin Impact is not running (or its window has a '
            + 'different title). Start the game before pressing Start.');
    } else {
        setClass(dot, 'warn', false);
        setClass(dot, 'neutral', true);
        setText(label, 'Looking for the game…');
        setTitle(el, '');
    }
}

// --- input backend selector ------------------------------------------------

const _BACKEND_LABEL = { software: 'Software', arduino: 'Arduino HID' };

function currentBackend() {
    return (STATE.lastStatus || {}).input_backend
        || $('#input-backend').value
        || 'arduino';
}

$('#input-backend').addEventListener('change', async () => {
    const sel = $('#input-backend');
    const desired = sel.value;
    const current = currentBackend();
    if (desired === current) return;
    const trySwitch = (force) => callApi('set_input_backend', desired, !!force);
    const res = await trySwitch(false);
    if (res && res.ok === false && res.reason === 'busy') {
        const ok = await dlgConfirm('Stop the bot or macro?',
            'The bot or a macro is currently running. Stop them and switch '
            + `the input method to ${_BACKEND_LABEL[desired] || desired}?`,
            { ok: 'Stop and switch' });
        if (!ok) {
            sel.value = current;          // revert without re-firing change
            return;
        }
        const res2 = await trySwitch(true);
        if (res2 && res2.ok === false) {
            sel.value = current;
            if (res2.reason === 'still_busy') {
                await dlgAlert('Input method not switched',
                    'The bot or macro did not stop in time, so the input '
                    + 'method was not switched. Try again.');
            }
        }
    } else if (res && res.ok === false) {
        sel.value = current;
    }
});

// --- mode + album options --------------------------------------------------

// Main-tab-only UI state (kept out of the shared STATE object).
const MAIN = {
    mode: 'standalone',   // mode shown by the segmented control
    lastSongs: parseInt($('#songs').value, 10) || 12, // last valid Songs value
    startPending: null,   // {state, lastError, minUntil, timer} while Start/Stop is in flight
    clickGuardUntil: 0,   // Start/Stop clicks are ignored until this time
    suppressError: null,  // last_error string hidden since the last Start click
    modeRequest: false,   // a mode-switch confirm is open
    tipDismissed: false,
    runStartedAt: 0,      // epoch seconds from Python
    runEndedAt: 0,        // local epoch seconds when the run went idle
    lanesSig: '',
    progSig: '',
    debug: false,
};

const _MODE_HINT = {
    standalone: 'Open any song in the game, then press Start. Plays until you stop it.',
    album: 'Open a country album page in the game, then press Start.',
};

const _DIFF_ORDER = ['normal', 'hard', 'pro', 'legendary'];
const _DIFF_LABEL = { normal: 'Normal', hard: 'Hard', pro: 'Pro', legendary: 'Legendary' };

function modeIsAlbum() {
    return MAIN.mode === 'album';
}

function setModeUi(mode) {
    MAIN.mode = mode === 'album' ? 'album' : 'standalone';
    $$('#mode-seg button').forEach((b) => {
        const on = b.dataset.mode === MAIN.mode;
        b.classList.toggle('on', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    setText($('#mode-hint'), _MODE_HINT[MAIN.mode]);
    applyAlbumDisabledState();
    renderStageText();
}

function applyAlbumDisabledState() {
    const album = modeIsAlbum();
    const card = $('#album-card');
    card.classList.toggle('dim', !album);
    card.setAttribute('aria-disabled', album ? 'false' : 'true');
    $$('#album-card input, #album-card select, #album-card button').forEach((el) => {
        el.disabled = !album;
    });
}

function applyModeUi() {
    applyAlbumDisabledState();
    callApi('set_mode', modeIsAlbum() ? 'album' : 'standalone');
}

async function requestMode(newMode) {
    if (newMode === MAIN.mode || MAIN.modeRequest) return;
    const status = STATE.lastStatus || {};
    const running = status.state === 'running' || status.state === 'paused';
    const oldMode = status.mode;

    // Confirm only when leaving album mid-run — that's the case where
    // the user loses song progress. Standalone has no per-song state to
    // protect, and switching INTO album from idle/standalone is cheap.
    if (running && oldMode === 'album' && newMode !== 'album') {
        MAIN.modeRequest = true;
        const ok = await dlgConfirm('Leave Album mode?',
            'Switching modes will abort the current album song.',
            { ok: 'Switch to Standalone', danger: true, focusCancel: true });
        MAIN.modeRequest = false;
        if (!ok) return;      // the control still shows the old mode
    }

    setModeUi(newMode);
    if (running) {
        callApi('restart_in_mode', newMode);
    } else {
        callApi('set_mode', newMode);
    }
}

$$('#mode-seg button').forEach((b) => {
    b.addEventListener('click', () => requestMode(b.dataset.mode));
});

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
    renderSongsSub(v);
    if (v !== MAIN.lastSongs) {
        MAIN.lastSongs = v;
        callApi('set_songs', v);
        renderStageText();
    }
}

function renderSongsSub(v) {
    setText($('#songs-sub'), v >= 12
        ? 'whole album · wheel returns to song 1 when done'
        : `${v} of 12 · wheel stays at the last-played song`);
}

$('#songs').addEventListener('change', commitSongs);
$('#songs').addEventListener('blur', commitSongs);

// Stepper buttons flank the number; each step commits immediately.
$$('.stepper .step-up, .stepper .step-down').forEach((btn) => {
    btn.addEventListener('click', () => {
        const target = $('#songs');
        if (!target || target.disabled) return;
        const step = btn.classList.contains('step-up') ? 1 : -1;
        const v = parseInt(target.value, 10);
        const base = Number.isNaN(v) ? MAIN.lastSongs : v;
        target.value = String(base + step);
        commitSongs();
    });
});

function selectedDifficulties() {
    const on = new Set($$('#diff-chips .chip.on').map((c) => c.dataset.diff));
    return _DIFF_ORDER.filter((d) => on.has(d));
}

function applyDifficultiesToChips(value) {
    // Accept either an array of names, the literal string 'all', or a
    // single difficulty name — JS init may receive any of those
    // depending on how the setting was saved.
    let wanted;
    if (Array.isArray(value)) wanted = value;
    else if (value === 'all') wanted = _DIFF_ORDER;
    else if (typeof value === 'string' && _DIFF_ORDER.includes(value)) wanted = [value];
    else return;
    const wantedSet = new Set(wanted);
    $$('#diff-chips .chip').forEach((c) => {
        const on = wantedSet.has(c.dataset.diff);
        c.classList.toggle('on', on);
        c.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
}

$$('#diff-chips .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
        const on = chip.classList.contains('on');
        // Refuse to leave the user in a no-difficulty state — the last
        // lit chip simply stays on.
        if (on && selectedDifficulties().length === 1) return;
        chip.classList.toggle('on', !on);
        chip.setAttribute('aria-pressed', on ? 'false' : 'true');
        callApi('set_difficulty', selectedDifficulties());
        renderStageText();
    });
});

$('#replay-canorus').addEventListener('click', () => {
    const el = $('#replay-canorus');
    const next = !el.classList.contains('on');
    setSwitch(el, next);
    callApi('set_replay_canorus', next);
});

$('#debug').addEventListener('click', () => {
    // Python owns the value; the status sync mirrors it back. While idle
    // the switch is the preset the next Start opens the viz window with.
    const next = !MAIN.debug;
    MAIN.debug = next;
    renderDebugSwitch();
    callApi('set_debug', next);
});

function renderDebugSwitch() {
    const s = STATE.lastStatus || {};
    const live = s.state === 'running' || s.state === 'paused';
    setSwitch($('#debug'), MAIN.debug);
    setText($('#debug-label'), (MAIN.debug && !live)
        ? 'Debug overlay · on at next start' : 'Debug overlay');
}

// --- start/stop/pause ------------------------------------------------------

// Start/Stop debounce, two layers (BU7):
//  - a fixed click guard: for START_CLICK_GUARD_MS after a click the
//    button ignores clicks and stays disabled, whatever the status says.
//    The first `running` push used to arrive within ~50 ms and re-enable
//    the button in time for the second half of a double-click to Stop.
//  - the pending guard: beyond that, the button stays disabled until a
//    status push shows the state (or last_error) changed, with a
//    START_PENDING_MAX_MS safety re-enable in case no push ever comes.
const START_CLICK_GUARD_MS = 600;
const START_PENDING_MAX_MS = 1500;

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
        minUntil: Date.now() + START_CLICK_GUARD_MS,
        settling: false,
        timer: setTimeout(endStartPendingNow, START_PENDING_MAX_MS),
    };
    $('#btn-start').disabled = true;
}

// A status push showed the state moved: release the button now if the
// click guard has elapsed, otherwise when it does.
function settleStartPending() {
    const p = MAIN.startPending;
    if (!p || p.settling) return;
    const wait = p.minUntil - Date.now();
    if (wait <= 0) {
        endStartPendingNow();
        return;
    }
    p.settling = true;
    clearTimeout(p.timer);
    p.timer = setTimeout(endStartPendingNow, wait);
}

function endStartPendingNow() {
    clearStartPending();
    renderStartButtons(STATE.lastStatus || {});
}

// True (and arms the guard) when a Start/Stop click may go through.
function acceptStartClick() {
    const now = Date.now();
    if (MAIN.startPending || now < MAIN.clickGuardUntil) return false;
    MAIN.clickGuardUntil = now + START_CLICK_GUARD_MS;
    return true;
}

function botIsRunning() {
    const s = STATE.lastStatus || {};
    return s.state === 'running' || s.state === 'paused';
}

function macroIsBusy() {
    const s = STATE.lastStatus || {};
    return s.macro_state === 'recording' || s.macro_state === 'playing';
}

$('#btn-start').addEventListener('click', async () => {
    if (!acceptStartClick()) return;
    const s = STATE.lastStatus || {};
    if (!botIsRunning()) {
        // A retry must never show the previous failure next to "Playing":
        // hide the notice now and keep it hidden until last_error moves.
        MAIN.suppressError = s.last_error || '';
        renderBotNotice();
        // If a macro is busy and the bot is currently idle, this click
        // would start the bot. Confirm so the user knows the macro will
        // be killed.
        if (macroIsBusy()) {
            const verb = s.macro_state === 'recording' ? 'recording' : 'macro playback';
            const ok = await dlgConfirm('Stop the macro?',
                `A macro is ${verb}. Stop it and start the bot?`,
                { ok: 'Stop macro and start' });
            if (!ok) return;
            beginStartPending();
            const res = await callApi('start_stop_force');
            if (res && res.ok === false) {
                endStartPendingNow();
                if (res.reason === 'busy') {
                    dlgAlert('Bot not started',
                        'The macro did not stop in time, so the bot was '
                        + 'not started. Try again.');
                }
            }
            return;
        }
    }
    beginStartPending();
    const ok = await callApi('start_stop');
    if (ok === false) endStartPendingNow();
});
$('#btn-pause').addEventListener('click', () => callApi('pause'));

// --- stage: state word, notice, tip, progress, lanes, stats ----------------

const _STATE_WORD = {
    idle: 'Ready', running: 'Playing', paused: 'Paused', stopping: 'Stopping…',
};

function difficultyPhrase(list) {
    const names = (Array.isArray(list) ? list : [])
        .map((d) => _DIFF_LABEL[d] || d);
    return names.length ? names.join(' + ') : '';
}

function renderStageText() {
    const s = STATE.lastStatus || {};
    const state = s.state || 'idle';
    const live = state === 'running' || state === 'paused';
    const prog = s.album_progress;
    const h1 = $('#st-state');
    setText(h1, _STATE_WORD[state] || state);
    const pulse = $('#st-pulse');
    setClass(pulse, 'live', state === 'running');
    setClass(pulse, 'paused', state === 'paused');
    setClass(pulse, 'stopping', state === 'stopping');

    let sub;
    if (state === 'stopping') {
        sub = 'Releasing keys and closing the detector…';
    } else if (live) {
        const runMode = s.mode || MAIN.mode;
        if (runMode === 'album') {
            if (prog && typeof prog === 'object') {
                const total = parseInt(prog.total, 10) || 0;
                const index = parseInt(prog.index, 10) || 0;
                const results = Array.isArray(prog.results) ? prog.results : [];
                const skipped = results.filter((r) => r === 'skipped').length;
                const failed = results.filter((r) => r === 'failed').length;
                if (index > 0) {
                    sub = `Song ${index} of ${total}`;
                    if (prog.difficulty) sub += ` · ${_DIFF_LABEL[prog.difficulty] || prog.difficulty}`;
                } else {
                    sub = `Starting · ${plural(total, 'song')}`;
                }
                if (skipped) sub += ` · ${skipped} skipped at Canorus`;
                if (failed) sub += ` · ${failed} failed`;
            } else {
                sub = s.song ? `Album · song ${s.song}` : 'Album · starting…';
            }
        } else {
            sub = state === 'paused'
                ? 'Standalone · detector paused'
                : 'Standalone · detector running';
        }
    } else if (modeIsAlbum()) {
        const diffs = difficultyPhrase(selectedDifficulties());
        sub = `Album · ${plural(MAIN.lastSongs, 'song')}${diffs ? ` · ${diffs}` : ''}`;
    } else {
        sub = 'Standalone · plays the open song until you stop it';
    }
    setText($('#st-sub'), sub);
    renderLaneHint();
}

// Bot error notice (last_error). Hidden since the last Start click until
// Python reports a different value.
function renderBotNotice() {
    const s = STATE.lastStatus || {};
    const err = String(s.last_error || '').trim();
    if (MAIN.suppressError !== null && err !== MAIN.suppressError) {
        MAIN.suppressError = null;
    }
    const show = !!err && MAIN.suppressError === null;
    setText($('#bot-notice-msg'), err);
    setHidden($('#bot-notice'), !show);
    renderPrereqTip();
}

function renderPrereqTip() {
    const s = STATE.lastStatus || {};
    const idle = !(s.state === 'running' || s.state === 'paused' || s.state === 'stopping');
    const noticeShown = !$('#bot-notice').hidden;
    setHidden($('#prereq-tip'), MAIN.tipDismissed || noticeShown || !idle);
}

$('#prereq-tip-close').addEventListener('click', () => {
    MAIN.tipDismissed = true;
    renderPrereqTip();
    callApi('dismiss_prereq_tip');
});

// Album progress bar: one segment per song position. Segments are only
// rebuilt when the total changes; classes update when the data moves.
function renderAlbumProgress(prog, state) {
    const bar = $('#album-prog');
    const legend = $('#album-legend');
    if (!prog || typeof prog !== 'object') {
        if (MAIN.progSig !== '') {
            MAIN.progSig = '';
            bar.innerHTML = '';
        }
        setHidden(bar, true);
        setHidden(legend, true);
        return;
    }
    const total = Math.max(0, Math.min(12, parseInt(prog.total, 10) || 0));
    const index = parseInt(prog.index, 10) || 0;
    const results = Array.isArray(prog.results) ? prog.results : [];
    const live = state === 'running' || state === 'paused';
    const sig = `${total}|${index}|${results.join(',')}|${live ? 1 : 0}`;
    if (sig === MAIN.progSig) return;
    MAIN.progSig = sig;
    if (bar.children.length !== total) {
        bar.innerHTML = '';
        for (let i = 0; i < total; i += 1) bar.appendChild(document.createElement('i'));
    }
    const segs = Array.from(bar.children);
    segs.forEach((seg, i) => {
        const r = results[i];
        let cls = '';
        if (r === 'played') cls = 'done';
        else if (r === 'skipped') cls = 'skip';
        else if (r === 'failed') cls = 'fail';
        else if (live && index === i + 1) cls = 'now';
        if (seg.className !== cls) seg.className = cls;
    });
    setClass(bar, 'stale', !live);
    setHidden(bar, total === 0);
    setHidden(legend, total === 0);
    setClass(legend, 'stale', !live);
}

function renderLanes(lanes) {
    const list = Array.isArray(lanes) ? lanes : [];
    const sig = list.map((v) => (v ? '1' : '0')).join('');
    if (sig === MAIN.lanesSig) return;
    MAIN.lanesSig = sig;
    $$('#lane-row .lane').forEach((lane, i) => {
        lane.classList.toggle('on', !!list[i]);
    });
}

function renderLaneHint() {
    const s = STATE.lastStatus || {};
    const live = s.state === 'running' || s.state === 'paused';
    setHidden($('#lane-hint'), live);
}

function fmtElapsed(secs) {
    const t = Math.max(0, Math.floor(secs));
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const sec = t % 60;
    const mm = String(m).padStart(2, '0');
    const ss = String(sec).padStart(2, '0');
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function renderElapsed() {
    const el = $('#st-elapsed');
    if (!MAIN.runStartedAt) {
        setText(el, '—');
        return;
    }
    const end = MAIN.runEndedAt || Date.now() / 1000;
    setText(el, fmtElapsed(end - MAIN.runStartedAt));
}

setInterval(() => {
    if (MAIN.runStartedAt && !MAIN.runEndedAt) renderElapsed();
}, 1000);

// Start / Pause buttons from the live state machine (+ the Start/Stop
// click debounce in MAIN.startPending).
function renderStartButtons(data) {
    const running = data.state === 'running' || data.state === 'paused';
    const stopping = data.state === 'stopping';
    const startBtn = $('#btn-start');
    const pauseBtn = $('#btn-pause');
    if (stopping) {
        setText(startBtn, 'Stopping…');
        setDisabled(startBtn, true);
    } else {
        setText(startBtn, running ? 'Stop' : 'Start');
        setDisabled(startBtn, !!MAIN.startPending);
    }
    setClass(startBtn, 'go', !running && !stopping);
    setClass(startBtn, 'stop', running || stopping);
    setDisabled(pauseBtn, !running || stopping);
    setText(pauseBtn, data.state === 'paused' ? 'Resume' : 'Pause');
}

// --- hotkey rebind ---------------------------------------------------------

$$('.hk-row').forEach((row) => {
    const action = row.dataset.action;
    const btn = row.querySelector('.key');
    if (btn) btn.addEventListener('click', () => beginCapture(action));
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
const CAPTURE_MSG = 'Press a key or mouse button · Esc or click the key to cancel · left click can’t be bound';
const CAPTURE_PAUSING = 'Pausing hotkeys…';
let _captureToken = 0;
let _captureTimer = null;
let _captureReady = false;
// Key cap whose own left-click just cancelled its capture: the click
// event that follows that mousedown must not re-arm it.
let _captureCancelBtn = null;
// Right-click capture ends before the button is released; keep blocking
// the native context menu that the release would open.
let _captureCtxGuardUntil = 0;

function captureArmed() {
    return !!(STATE.capturing || STATE.activeCapturing);
}

// The card subtitle that hosts the capture message (#28): the hotkey
// card's for a bot/macro hotkey, the Active macros card's for a binding.
function captureMsgFor(action) {
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    if (!row) return null;
    const card = row.closest('.card');
    return card ? card.querySelector('.card-sub') : null;
}

function activeCaptureMsg() {
    const card = $('#active-macros-card');
    return card ? card.querySelector('.card-sub') : null;
}

function currentCaptureMsg() {
    if (STATE.capturing) return captureMsgFor(STATE.capturing);
    if (STATE.activeCapturing) return activeCaptureMsg();
    return null;
}

function setCaptureMsg(el, text) {
    if (!el) return;
    el.textContent = text;
    el.classList.add('cap-msg');
    el.title = text;
}

function restoreCardSub(el) {
    if (!el) return;
    const def = (el.dataset && el.dataset.default) || '';
    el.textContent = def;
    el.title = '';
    el.classList.remove('cap-msg');
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
                    setCaptureMsg(msg,
                        'Could not pause the hotkeys for capture — try again.');
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
        return row ? row.querySelector('.key') : null;
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
    // cancelled capture on this same key cap: clicking the cap again
    // while it reads "press a key…" cancels instead of re-arming.
    const hit = !!btn && _captureCancelBtn === btn;
    _captureCancelBtn = null;
    return hit;
}

const _CAPTURE_REFUSALS = {
    duplicate_hotkey: 'That binding is already in use.',
    left_click_reserved:
        'Left Click cannot be a hotkey — it is every attack in Genshin.',
    unsupported_key: 'That key cannot be used as a hotkey.',
    rhythm_key: 'A S D J K L are the keys the bot presses itself.',
    install_failed: 'Could not install that hotkey (see Logs).',
};

function showCaptureRefusal(msg, res) {
    if (!msg) return;
    const why = _CAPTURE_REFUSALS[res && res.reason] || 'Could not bind that.';
    setCaptureMsg(msg, `${why} Try another key or button (Esc cancels)…`);
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
            showCaptureRefusal(activeCaptureMsg(), res);
            armCaptureGuard();
        }, () => {
            if (token !== _captureToken || STATE.activeCapturing !== pos) return;
            showCaptureRefusal(activeCaptureMsg(), null);
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

function setCapCapturing(btn) {
    if (!btn) return;
    btn.classList.add('cap');
    btn.classList.remove('unset');
    btn.textContent = 'press a key…';
}

function beginCapture(action) {
    const row = document.querySelector(`.hk-row[data-action="${action}"]`);
    const btn = row ? row.querySelector('.key') : null;
    if (consumeCancelClick(btn) || captureArmed() || !btn || DIALOG.open) return;
    STATE.capturing = action;
    setCapCapturing(btn);
    const msg = captureMsgFor(action);
    if (msg) setCaptureMsg(msg, CAPTURE_PAUSING);
    armCaptureGuard(() => {
        if (msg) setCaptureMsg(msg, CAPTURE_MSG);
    });
}

function endCapture() {
    if (!STATE.capturing) return;
    const action = STATE.capturing;
    STATE.capturing = null;
    const msg = captureMsgFor(action);
    restoreCardSub(msg);
    // Put the current binding back on the cap (or an unset cap if Python
    // never sent one for this action).
    const kb = (STATE.lastStatus || {}).keybinds || {};
    if (kb[action]) {
        renderKeybinds(kb, action);
    } else {
        const row = document.querySelector(`.hk-row[data-action="${action}"]`);
        const btn = row ? row.querySelector('.key') : null;
        if (btn) {
            btn.textContent = 'Bind…';
            btn.classList.remove('cap');
            btn.classList.add('unset');
        }
    }
    if (!captureArmed()) releaseCaptureGuard();
}

function beginActiveCapture(pos) {
    const row = document.querySelector(`.active-macro-row[data-active="${pos}"]`);
    const btn = row ? row.querySelector('.active-bind') : null;
    if (consumeCancelClick(btn) || captureArmed() || DIALOG.open) return;
    STATE.activeCapturing = pos;
    setCapCapturing(btn);
    const msg = activeCaptureMsg();
    if (msg) setCaptureMsg(msg, CAPTURE_PAUSING);
    // Same guard as the hotkey rows: global hotkeys are suspended (and
    // the prompt times out) while an active-macro key is being picked.
    armCaptureGuard(() => {
        if (msg) setCaptureMsg(msg, CAPTURE_MSG);
    });
}

function endActiveCapture() {
    if (!STATE.activeCapturing) return;
    STATE.activeCapturing = null;
    restoreCardSub(activeCaptureMsg());
    STATE.activeRenderSig = '';
    renderActiveMacroRows(STATE.lastStatus || {});
    if (!captureArmed()) releaseCaptureGuard();
}

const _MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'OS']);

// Capture phase so reserved-key swallow happens before browser default.
window.addEventListener('keydown', (ev) => {
    // A modal dialog owns the keyboard: Escape cancels, Enter confirms,
    // Tab stays inside the box. Nothing leaks to the page behind it.
    if (DIALOG.open) {
        const box = $('#dialog-box');
        if (ev.key === 'Escape') {
            ev.preventDefault();
            ev.stopPropagation();
            if (DIALOG.done) DIALOG.done(DIALOG.cancelValue);
        } else if (ev.key === 'Enter') {
            const ae = activeEl();
            const onButton = ae && ae.tagName === 'BUTTON' && box.contains(ae);
            if (onButton || (ae && ae.tagName === 'TEXTAREA')) return;
            ev.preventDefault();
            ev.stopPropagation();
            if (DIALOG.onEnter) DIALOG.onEnter();
        } else if (ev.key === 'Tab') {
            const items = focusablesIn(box);
            if (!items.length) { ev.preventDefault(); return; }
            const ae = activeEl();
            const idx = items.indexOf(ae);
            if (ev.shiftKey && (idx <= 0)) {
                ev.preventDefault();
                items[items.length - 1].focus();
            } else if (!ev.shiftKey && (idx === -1 || idx === items.length - 1)) {
                ev.preventDefault();
                items[0].focus();
            }
        } else if (ev.key === 'F5' || ev.key === 'F11' || ev.key === 'F12') {
            ev.preventDefault();
        }
        return;
    }
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
    if (DIALOG.open) {
        // Clicking the dark backdrop (not the box) cancels the dialog.
        const t = ev.target;
        if (t && t.id === 'dialog-scrim' && ev.button === 0 && DIALOG.done) {
            DIALOG.done(DIALOG.cancelValue);
        }
        return;
    }
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
        // on the capturing key cap itself just cancels (see
        // consumeCancelClick).
        const btn = captureButton();
        const hit = ev.target && ev.target.closest
            ? ev.target.closest('button.key') : null;
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

// --- bindings display --------------------------------------------------------

const _MOUSE_DISPLAY = {
    left: 'Left Click', right: 'Right Click', middle: 'Middle Click',
    x: 'Mouse 4', x2: 'Mouse 5',
};

const _KEY_DISPLAY = {
    esc: 'Esc', escape: 'Esc', enter: 'Enter', space: 'Space', tab: 'Tab',
    backspace: 'Backspace', delete: 'Delete', insert: 'Insert', home: 'Home',
    end: 'End', 'page up': 'Page Up', 'page down': 'Page Down', up: 'Up',
    down: 'Down', left: 'Left', right: 'Right', ctrl: 'Ctrl', alt: 'Alt',
    shift: 'Shift', windows: 'Win', 'caps lock': 'Caps Lock',
    'num lock': 'Num Lock', 'scroll lock': 'Scroll Lock',
    'print screen': 'PrtSc', pause: 'Pause',
};

function prettyKeyPart(part) {
    const p = String(part || '').trim();
    if (!p) return p;
    const lower = p.toLowerCase();
    if (_KEY_DISPLAY[lower]) return _KEY_DISPLAY[lower];
    if (/^f\d{1,2}$/.test(lower)) return lower.toUpperCase();
    if (lower.length === 1) return lower.toUpperCase();
    return lower.charAt(0).toUpperCase() + lower.slice(1);
}

function displayBinding(binding) {
    if (!binding) return '<unset>';
    const m = /^mouse:(.+)$/i.exec(binding);
    if (m) {
        const name = m[1].toLowerCase();
        return _MOUSE_DISPLAY[name] || `Mouse ${m[1]}`;
    }
    return String(binding).split('+').map(prettyKeyPart).join('+');
}

// Key caps of the hotkey rows. A capturing cap keeps its "press a key…"
// text; `only` limits the refresh to one action (end of its capture).
function renderKeybinds(kb, only) {
    Object.entries(kb || {}).forEach(([action, hotkey]) => {
        if (only && action !== only) return;
        const row = document.querySelector(`.hk-row[data-action="${action}"]`);
        if (!row) return;
        const btn = row.querySelector('.key');
        if (!btn) return;
        if (STATE.capturing === action) return;
        const hk = String(hotkey || '').toLowerCase();
        const text = hotkey ? displayBinding(hotkey) : 'Bind…';
        setText(btn, text);
        setClass(btn, 'cap', false);
        setClass(btn, 'unset', !hotkey);
        setClass(btn, 'warnkey', hk === 'mouse:left');
        // Mouse hotkeys see every click of that button, including clicks a
        // playing macro replays. A Left Click saved by an older build is
        // refused at install time (it is every attack), so say so.
        setTitle(btn, hk === 'mouse:left'
            ? 'Left Click is not active as a hotkey — click to rebind this.'
            : (hk.startsWith('mouse:')
                ? `${text} — mouse hotkeys also fire when a macro replays this button. Click to change.`
                : (hotkey ? `${text} — click to change` : 'Click to bind')));
    });
}

// --- macros panel ----------------------------------------------------------

const MACRO = {
    lastAction: '',        // JS-initiated action, for the notice lead
    lastActionAt: 0,
    noticeLead: '',
    noticeErr: '',
    localErr: null,        // {lead, msg, until} shown from a bridge refusal
    localErrTimer: null,
    hint: { text: '', until: 0, timer: null },
};

const LOCAL_ERR_MS = 10000;

// Fallback wording per refusal reason, for a Python side that returns
// the reason without a `message` (the message wins when present).
const _MACRO_REFUSAL_TEXT = {
    game_not_found: 'Genshin isn’t running — start the game, then try again.',
    focus_failed: 'Couldn’t bring Genshin to the front — click into the game '
        + 'and use the macro hotkey instead.',
    not_foreground: 'Genshin isn’t the active window — playback was not started.',
    bot_stop_timeout: 'The bot didn’t stop in time — macro not started. Try again.',
    no_backend: 'The input backend is unavailable — see the Logs tab.',
    busy: 'Another macro command is still running — try again.',
    empty_buffer: 'Nothing to play — record or load a macro first.',
    empty_slot: 'That slot is empty.',
};

// Show a bridge refusal in the macro notice right away instead of
// waiting for the status tick (and for refusals Python does not publish
// as macro_error). Returns true when something was shown. `bot_running`
// is handled by the caller's confirm; `closing` needs no notice.
function explainMacroRefusal(action, res) {
    if (!res || res.ok !== false) return false;
    const reason = String(res.reason || '');
    if (reason === 'bot_running' || reason === 'closing' || reason === 'stale') return false;
    const msg = String(res.message || '').trim()
        || _MACRO_REFUSAL_TEXT[reason]
        || (reason ? `Refused (${reason}) — see the Logs tab.` : 'See the Logs tab.');
    showMacroError(_MACRO_LEADS[action] || 'Couldn’t do that.', msg);
    return true;
}

function showMacroError(lead, msg) {
    MACRO.localErr = { lead, msg, until: Date.now() + LOCAL_ERR_MS };
    clearTimeout(MACRO.localErrTimer);
    MACRO.localErrTimer = setTimeout(() => {
        MACRO.localErr = null;
        renderMacroNotice(STATE.lastStatus || {});
    }, LOCAL_ERR_MS + 50);
    renderMacroNotice(STATE.lastStatus || {});
}

const _MACRO_LEADS = {
    record: 'Record didn’t start.',
    play: 'Play didn’t start.',
    save: 'Save failed.',
    load: 'Load failed.',
    rename: 'Rename failed.',
    clear: 'Clear failed.',
    edit: 'Edits weren’t saved.',
};

function noteMacroAction(name) {
    MACRO.lastAction = name;
    MACRO.lastActionAt = Date.now();
}

function slotNameOf(n) {
    const map = (STATE.lastStatus && STATE.lastStatus.macro_slot_names) || {};
    // pywebview JSONifies dict keys as strings; normalize lookup.
    return map[n] || map[String(n)] || '';
}

function slotLabel(n) {
    const name = slotNameOf(n);
    return name ? `slot ${n} ("${name}")` : `slot ${n}`;
}

// Transient explanation in the Saved macros card subtitle, for refusals
// the UI can explain without a round-trip (empty slot on Load, nothing
// to save, ...). Python-side failures show in the notice (macro_error).
function showMacroHint(text, ms = 6000) {
    MACRO.hint.text = text || '';
    MACRO.hint.until = Date.now() + ms;
    renderSlotsSub();
    clearTimeout(MACRO.hint.timer);
    MACRO.hint.timer = setTimeout(renderSlotsSub, ms + 50);
}

function renderSlotsSub() {
    const s = STATE.lastStatus || {};
    const el = $('#slots-sub');
    if (!el) return;
    const busy = s.macro_state === 'recording' || s.macro_state === 'playing';
    let text;
    let live = false;
    if (MACRO.hint.text && Date.now() < MACRO.hint.until) {
        text = MACRO.hint.text;
        live = true;
    } else if (busy) {
        text = `unavailable while ${s.macro_state}`;
    } else {
        text = (el.dataset && el.dataset.default) || '';
    }
    setText(el, text);
    setTitle(el, live ? text : '');
    setClass(el, 'hint-msg', live);
}

// Row letters (A/B/C) of the active macros that play slot `n`.
function activeUsersOfSlot(n) {
    return activeMacrosOf().map((item, i) => (
        item.slot === n ? 'ABC'[i] || String(i + 1) : null
    )).filter(Boolean);
}

// What replacing the working macro right now would throw away, as a
// phrase ('' = nothing). Shared by the Load / Record / Close confirms.
// Load / Record count a dirty buffer only when it has events (an empty
// one has nothing to lose); `forClose` follows Python's close rule,
// `macro_state === 'recording' || macro_dirty`, exactly.
function macroUnsavedSummary(forClose) {
    const s = STATE.lastStatus || {};
    const parts = [];
    const n = s.macro_events || 0;
    const evs = plural(n, 'event');
    if (s.macro_state === 'recording') {
        parts.push(`a recording in progress (${evs})`);
    } else if (s.macro_dirty && (n > 0 || forClose)) {
        const loaded = parseInt(s.macro_loaded, 10) || 0;
        if (loaded) parts.push(`unsaved changes to slot ${loaded} (${evs})`);
        else if (n > 0) parts.push(`an unsaved macro (${evs})`);
        else parts.push('unsaved macro changes');
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
    return [1, 2, 3].map((pos) => {
        const item = active[pos - 1] || {};
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
    const playingActive = s.macro_state === 'playing' && s.macro_playing_source === 'active';
    const sig = JSON.stringify({
        active,
        slots: s.macro_slots || [],
        names,
        playingSlot: playingActive ? playingSlot : 0,
        state: s.macro_state || '',
    });
    if (sig === STATE.activeRenderSig) return;
    STATE.activeRenderSig = sig;
    const ae = activeEl();
    $$('.active-macro-row').forEach((row) => {
        const pos = parseInt(row.dataset.active, 10);
        const item = active[pos - 1] || { slot: 0, hotkey: '' };
        const select = row.querySelector('.active-slot');
        if (select === ae) {
            // Don't rebuild a dropdown the user has open; retry next tick.
            STATE.activeRenderSig = '';
        } else {
            const previous = String(item.slot || 0);
            select.innerHTML = '<option value="0">No slot</option>';
            (s.macro_slots || []).forEach((slot) => {
                const opt = document.createElement('option');
                opt.value = String(slot);
                const name = names[slot] || names[String(slot)] || '';
                opt.textContent = name ? `Slot ${slot} · ${name}` : `Slot ${slot}`;
                select.appendChild(opt);
            });
            if (item.slot && !occupied.has(item.slot)) {
                const opt = document.createElement('option');
                opt.value = String(item.slot);
                opt.textContent = `Missing slot ${item.slot}`;
                select.appendChild(opt);
            }
            select.value = previous;
        }

        const cap = row.querySelector('.active-bind');
        if (STATE.activeCapturing !== pos) {
            const hk = String(item.hotkey || '').toLowerCase();
            setText(cap, item.hotkey ? displayBinding(item.hotkey) : 'Bind…');
            setClass(cap, 'cap', false);
            setClass(cap, 'unset', !item.hotkey);
            setClass(cap, 'warnkey', hk === 'mouse:left');
            // Same caveat renderKeybinds shows: a mouse hotkey sees every
            // click of that button, including clicks a playing macro replays.
            setTitle(cap, hk.startsWith('mouse:')
                ? `${displayBinding(item.hotkey)} — mouse hotkeys also fire when `
                  + 'a macro replays this button. Click to change.'
                : (item.hotkey ? `${displayBinding(item.hotkey)} — click to change` : 'Click to bind'));
        }

        const slotReady = !!item.slot && occupied.has(item.slot);
        const isRecording = s.macro_state === 'recording';
        const isPlayingThis = playingActive && playingSlot && playingSlot === item.slot;
        setClass(row, 'active-playing', !!isPlayingThis);
        setClass(row, 'assigned', !!item.slot || !!item.hotkey);
        setDisabled(row.querySelector('.active-play'), !slotReady || isRecording);
        setDisabled(cap, isRecording);
        setDisabled(row.querySelector('.active-clear'), !item.slot && !item.hotkey);
    });
}

function renderActivePolicy(policy) {
    const normalized = policy === 'interrupt' ? 'interrupt' : 'ignore';
    $$('#policy-seg button').forEach((btn) => {
        const on = btn.dataset.policy === normalized;
        btn.classList.toggle('on', on);
        btn.setAttribute('aria-checked', on ? 'true' : 'false');
    });
}

$$('#policy-seg button').forEach((btn) => {
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
                    if (res.reason === 'duplicate_slot' || res.reason === 'empty_slot') {
                        showMacroHint(res.message || 'That slot cannot be assigned here.');
                    }
                }
            });
    });
    row.querySelector('.active-bind').addEventListener('click', () => {
        beginActiveCapture(pos);
    });
    row.querySelector('.active-play').addEventListener('click', async () => {
        noteMacroAction('play');
        const res = await callApi('macro_play_active', pos, false);
        if (res && res.ok === false && res.reason === 'bot_running') {
            const ok = await dlgConfirm('Stop the bot?',
                'The bot is running. Stop it and play this active macro?',
                { ok: 'Stop bot and play' });
            if (ok) {
                noteMacroAction('play');
                explainMacroRefusal('play', await callApi('macro_play_active', pos, true));
            }
            return;
        }
        explainMacroRefusal('play', res);
    });
    row.querySelector('.active-clear').addEventListener('click', () => {
        callApi('macro_clear_active', pos);
    });
});

// --- slot grid ---------------------------------------------------------------

function slotIsOccupied(n) {
    const s = STATE.lastStatus || {};
    return (s.macro_slots || []).map((x) => parseInt(x, 10)).includes(n);
}

const _SLOT_ACTS_FILLED =
    '<button type="button" data-act="load">Load</button>'
    + '<button type="button" data-act="save">Save</button>'
    + '<button type="button" data-act="rename">Rename</button>'
    + '<button type="button" data-act="clear" class="danger">Clear</button>';
const _SLOT_ACTS_EMPTY =
    '<button type="button" data-act="save" class="wide">Save here</button>';

function renderMacroSlots(s) {
    const slots = (s.macro_slots || []).map((n) => parseInt(n, 10));
    const occupied = new Set(slots);
    const names = s.macro_slot_names || {};
    const loaded = parseInt(s.macro_loaded, 10) || 0;
    const macroState = s.macro_state || 'idle';
    const active = activeMacrosOf(s);
    const sig = JSON.stringify({ slots, names, loaded, macroState, active });
    if (sig === STATE.slotsRenderSig) return;
    STATE.slotsRenderSig = sig;
    // Slots can't load / save / rename / clear while recording or
    // playing — the whole card locks instead of refusing silently.
    const busy = macroState === 'recording' || macroState === 'playing';
    const card = $('#slots-card');
    setClass(card, 'locked', busy);
    card.setAttribute('aria-disabled', busy ? 'true' : 'false');
    renderSlotsSub();
    $$('#slot-grid .slot').forEach((tile) => {
        const n = parseInt(tile.dataset.slot, 10);
        const isOcc = occupied.has(n);
        setClass(tile, 'filled', isOcc);
        setClass(tile, 'empty', !isOcc);
        setClass(tile, 'loaded', isOcc && n === loaded);
        const nameEl = tile.querySelector('.name');
        const name = isOcc ? (names[n] || names[String(n)] || '') : '';
        setText(nameEl, isOcc ? (name || 'Saved macro') : 'Empty');
        // Badges: which active rows play this slot.
        const letters = active.map((it, i) => (it.slot === n ? 'ABC'[i] : null)).filter(Boolean);
        const ab = tile.querySelector('.ab');
        const abHtml = letters.map((l) => `<i>${l}</i>`).join('');
        if (ab.innerHTML !== abHtml) ab.innerHTML = abHtml;
        // Hover actions depend on occupancy only.
        const acts = tile.querySelector('.acts');
        const occKey = isOcc ? 'filled' : 'empty';
        if (acts.dataset.occ !== occKey) {
            acts.dataset.occ = occKey;
            acts.innerHTML = isOcc ? _SLOT_ACTS_FILLED : _SLOT_ACTS_EMPTY;
        }
        // The 12 px name is ellipsized; the tooltip carries the full one.
        const busyNote = busy ? ` (stop ${macroState} to use the slots)` : '';
        const extra = letters.length ? ` · active macro ${letters.join(', ')}` : '';
        setTitle(tile, isOcc
            ? `Slot ${n}: ${name || '(unnamed)'}${n === loaded ? ' · loaded' : ''}${extra}${busyNote}`
            : `Slot ${n}: empty${busyNote}`);
    });
}

async function slotAction(act, n) {
    if (Number.isNaN(n)) return;
    const s = STATE.lastStatus || {};
    const occupied = slotIsOccupied(n);
    const existing = slotNameOf(n);
    const label = existing ? ` ("${existing}")` : '';
    if (macroIsBusy()) {
        // The grid is locked while busy; covers a click that raced
        // the status push.
        showMacroHint(`Stop ${s.macro_state === 'recording'
            ? 'recording' : 'playback'} before using the slots.`);
        return;
    }
    if (act === 'load') {
        if (!occupied) {
            showMacroHint(`Slot ${n} is empty — use "Save here" to store the current macro there.`);
            return;
        }
        const loss = macroUnsavedSummary();
        if (loss) {
            const ok = await dlgConfirm(`Load slot ${n}${label}?`,
                `This replaces the current macro, and you have ${loss} that will be lost.`,
                { ok: 'Load and discard', danger: true, focusCancel: true });
            if (!ok) return;
        }
        noteMacroAction('load');
        const res = await callApi('macro_load_slot', n);
        if (res && res.ok) discardEditorEdits();
        else explainMacroRefusal('load', res);
    } else if (act === 'save') {
        openSaveDialog(n);
    } else if (act === 'rename') {
        if (!occupied) {
            showMacroHint(`Slot ${n} is empty — nothing to rename.`);
            return;
        }
        const name = await dlgPrompt(`Rename slot ${n}`, '',
            { label: 'Name', value: existing, ok: 'Rename' });
        if (name === null) return;
        noteMacroAction('rename');
        callApi('macro_rename_slot', n, name);
    } else if (act === 'clear') {
        if (!occupied) {
            showMacroHint(`Slot ${n} is already empty.`);
            return;
        }
        const users = activeUsersOfSlot(n);
        const loaded = parseInt(s.macro_loaded, 10) || 0;
        let body = existing
            ? `"${existing}" will be deleted from disk. This can’t be undone.`
            : 'The saved macro will be deleted from disk. This can’t be undone.';
        if (users.length) {
            body += `\n\nActive macro ${users.join(', ')} uses this slot and will be `
                + 'unassigned (its hotkey stays bound but does nothing until you '
                + 'pick another slot).';
        }
        if (loaded === n && (s.macro_events || 0) > 0) {
            body += '\n\nThe events loaded from it stay in the current macro as unsaved.';
        }
        const ok = await dlgConfirm(`Clear slot ${n}?`, body,
            { ok: 'Clear slot', cancel: 'Keep it', danger: true, focusCancel: true });
        if (!ok) return;
        noteMacroAction('clear');
        const res = await callApi('macro_clear_slot', n);
        if (res && res.ok) noteUnassigned(n, res);
        else explainMacroRefusal('clear', res);
    }
}

// `macro_clear_slot` reports the active-macro letters it unassigned.
function unassignedPhrase(res) {
    const letters = (res && Array.isArray(res.unassigned) ? res.unassigned : [])
        .map((l) => String(l)).filter(Boolean);
    if (!letters.length) return '';
    const one = letters.length === 1;
    return `Active macro ${letters.join(', ')} ${one ? 'is' : 'are'} now `
        + `unassigned — pick another slot for ${one ? 'it' : 'them'}.`;
}

function noteUnassigned(n, res) {
    const phrase = unassignedPhrase(res);
    if (phrase) showMacroHint(`Slot ${n} cleared. ${phrase}`, 9000);
}

$('#slot-grid').addEventListener('click', (ev) => {
    const act = ev.target && ev.target.closest ? ev.target.closest('[data-act]') : null;
    if (!act) return;
    const tile = act.closest('.slot');
    if (!tile) return;
    slotAction(act.dataset.act, parseInt(tile.dataset.slot, 10));
});

// Keyboard: Enter on a focused tile = Load (filled) / Save here (empty).
$('#slot-grid').addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter') return;
    const t = ev.target;
    if (!t || !t.classList || !t.classList.contains('slot')) return;
    ev.preventDefault();
    const n = parseInt(t.dataset.slot, 10);
    slotAction(slotIsOccupied(n) ? 'load' : 'save', n);
});

// Save-to-slot picker (#25): name + 18 mini slots + overwrite warning.
// Serves "Save here" on an empty tile, "Save" on a filled tile and the
// transport's "Save to slot…" button. The buffer revision is pinned when
// the dialog opens so a macro recorded meanwhile (in-game hotkey) can't
// be saved under the name the user typed for the old one.
async function openSaveDialog(preSlot) {
    const s = STATE.lastStatus || {};
    if (macroIsBusy()) {
        showMacroHint(`Stop ${s.macro_state} before saving.`);
        return;
    }
    const events = s.macro_events || 0;
    if (events === 0) {
        showMacroHint('Nothing to save — record or load a macro first.');
        return;
    }
    if (evtState.visible && evtState.jsDirty) {
        const ok = await dlgConfirm('Save without the editor edits?',
            'The events editor has unsaved edits that are not in the macro '
            + 'yet (press Save changes in the editor to apply them).\n\n'
            + 'Save the macro without those edits?',
            { ok: 'Save without them', focusCancel: true });
        if (!ok) return;
    }
    const rev = s.macro_rev;
    const loaded = parseInt(s.macro_loaded, 10) || 0;
    const slots = new Set((s.macro_slots || []).map((n) => parseInt(n, 10)));
    const names = s.macro_slot_names || {};
    const nameOf = (n) => names[n] || names[String(n)] || '';
    const pre = preSlot || loaded || 0;
    const picked = await showDialog((box, done) => {
        box.classList.add('wide');
        box.appendChild(dlgEl('h2', '', 'Save to a slot')).id = 'dialog-title';
        box.appendChild(dlgEl('p', '',
            `${plural(events, 'event')} in the working macro. Pick a slot and give it a name.`));
        const label = dlgEl('label', 'f', 'Name');
        label.htmlFor = 'dialog-text';
        box.appendChild(label);
        const input = dlgEl('input', 'text');
        input.type = 'text';
        input.id = 'dialog-text';
        input.spellcheck = false;
        input.placeholder = 'leave blank for no name';
        input.value = pre ? nameOf(pre) : '';
        box.appendChild(input);
        let touched = false;
        input.addEventListener('input', () => { touched = true; });
        const grid = dlgEl('div', 'mslots');
        const tiles = [];
        let current = 0;
        const msg = dlgEl('span', 'left', 'Pick a slot.');
        const ok = dlgButton('Save', 'go', () => {
            if (current) done({ slot: current, name: input.value });
        });
        ok.disabled = true;
        const pick = (n) => {
            current = n;
            tiles.forEach((t) => t.classList.toggle('pick', parseInt(t.dataset.slot, 10) === n));
            const filled = slots.has(n);
            const nm = nameOf(n);
            if (!touched) input.value = nm;
            const users = activeUsersOfSlot(n);
            let text;
            if (!filled) {
                text = `Slot ${n} is empty.`;
            } else {
                text = `Slot ${n} · "${nm || 'unnamed'}" will be overwritten.`;
                if (users.length) {
                    text += ` Active macro ${users.join(', ')} plays this slot and will play the new macro.`;
                }
            }
            msg.textContent = text;
            msg.classList.toggle('warn', filled);
            ok.textContent = filled ? `Overwrite slot ${n}` : `Save to slot ${n}`;
            ok.disabled = false;
        };
        for (let n = 1; n <= 18; n += 1) {
            const filled = slots.has(n);
            const t = dlgEl('button', `mslot${filled ? '' : ' empty'}`);
            t.type = 'button';
            t.dataset.slot = String(n);
            t.appendChild(dlgEl('span', 'n', String(n).padStart(2, '0')));
            t.appendChild(dlgEl('span', 'name', filled ? (nameOf(n) || 'Saved macro') : 'Empty'));
            t.title = filled ? `Slot ${n}: ${nameOf(n) || '(unnamed)'}` : `Slot ${n}: empty`;
            t.addEventListener('click', () => pick(n));
            grid.appendChild(t);
            tiles.push(t);
        }
        box.appendChild(grid);
        const row = dlgEl('div', 'row');
        row.appendChild(msg);
        row.appendChild(dlgButton('Cancel', '', () => done(null)));
        row.appendChild(ok);
        box.appendChild(row);
        if (pre) pick(pre);
        setTimeout(() => { try { input.select(); } catch (e) { /* no-op */ } }, 0);
        return {
            initialFocus: input,
            onEnter: () => { if (current) done({ slot: current, name: input.value }); },
        };
    }, null);
    if (!picked) return;
    noteMacroAction('save');
    const pinned = typeof rev === 'number' ? rev : null;
    const res = await callApi('macro_save_slot', picked.slot, picked.name, pinned);
    if (res && res.ok === false && res.reason === 'stale') {
        showMacroHint('The macro changed while you were naming it — nothing was saved. Try again.');
        return;
    }
    if (!res || res.ok === false) {
        explainMacroRefusal('save', res);
        return;
    }
    // Saving to a new slot changes the buffer's identity (rev + loaded
    // slot) without touching its events. An editor that fetched this
    // same revision can simply follow: its local edits still apply, and
    // Save writes them back to the slot the macro now lives in. Any
    // other state re-fetches through the normal rev check.
    if (evtState.visible && evtState.jsDirty && pinned !== null
            && evtState.rev === pinned && typeof res.rev === 'number') {
        evtState.rev = res.rev;
        evtState.slot = parseInt(res.slot, 10) || picked.slot;
        updateEvtButtons();
    }
}

$('#macro-save-to').addEventListener('click', () => openSaveDialog(0));

// --- transport -------------------------------------------------------------------

$('#macro-record').addEventListener('click', async () => {
    const starting = (STATE.lastStatus || {}).macro_state !== 'recording';
    if (starting) {
        const loss = macroUnsavedSummary();
        if (loss) {
            const ok = await dlgConfirm('Start a new recording?',
                `Recording replaces the current macro, and you have ${loss} that will be lost.`,
                { ok: 'Record', danger: true, focusCancel: true });
            if (!ok) return;
        }
        noteMacroAction('record');
    }
    const after = (res) => {
        if (starting && res && res.ok) discardEditorEdits();
        if (starting) explainMacroRefusal('record', res);
    };
    const res = await callApi('macro_toggle_record', false);
    if (res && res.ok === false && res.reason === 'bot_running') {
        const ok = await dlgConfirm('Stop the bot?',
            'The bot is running. Stop it and start recording?',
            { ok: 'Stop bot and record' });
        if (ok) {
            noteMacroAction('record');
            callApi('macro_toggle_record', true).then(after);
        }
        return;
    }
    after(res);
});

$('#macro-play').addEventListener('click', async () => {
    noteMacroAction('play');
    const res = await callApi('macro_play', false);
    if (res && res.ok === false && res.reason === 'bot_running') {
        const ok = await dlgConfirm('Stop the bot?',
            'The bot is running. Stop it and play the macro?',
            { ok: 'Stop bot and play' });
        if (ok) {
            noteMacroAction('play');
            explainMacroRefusal('play', await callApi('macro_play', true));
        }
        return;
    }
    // game_not_found / focus_failed / not_foreground / bot_stop_timeout /
    // no_backend ... each come with a `message`.
    explainMacroRefusal('play', res);
});

$('#macro-stop-play').addEventListener('click', () => callApi('macro_stop'));

function renderMacroNotice(s) {
    const err = String(s.macro_error || '').trim();
    const local = (MACRO.localErr && Date.now() < MACRO.localErr.until)
        ? MACRO.localErr : null;
    if (!err && !local) {
        MACRO.noticeErr = '';
        setHidden($('#macro-notice'), true);
        return;
    }
    if (!err) {
        // Python has not (or not yet) published this one: show what the
        // bridge call returned until the status catches up.
        setText($('#macro-notice-lead'), local.lead);
        setText($('#macro-notice-msg'), local.msg);
        setHidden($('#macro-notice'), false);
        return;
    }
    if (err !== MACRO.noticeErr) {
        MACRO.noticeErr = err;
        const recent = MACRO.lastAction && (Date.now() - MACRO.lastActionAt) < 15000;
        MACRO.noticeLead = (recent && _MACRO_LEADS[MACRO.lastAction]) || 'Couldn’t do that.';
    }
    setText($('#macro-notice-lead'), MACRO.noticeLead);
    setText($('#macro-notice-msg'), err);
    setHidden($('#macro-notice'), false);
}

function renderMacroState(s) {
    s = s || {};
    const stateLabel = s.macro_state || 'idle';
    const bufferEvents = s.macro_events || 0;
    // While playing, count what is actually playing — an active slot's
    // event count differs from the working buffer's.
    const playingEvents = parseInt(s.macro_playing_events, 10) || 0;
    const events = (stateLabel === 'playing' && playingEvents)
        ? playingEvents : bufferEvents;
    const loaded = parseInt(s.macro_loaded, 10) || 0;
    const isDirty = !!s.macro_dirty;
    const names = s.macro_slot_names || {};
    const recording = stateLabel === 'recording';
    const playing = stateLabel === 'playing';

    // Status pill: state + live event count (#16, #26).
    const pill = $('#macro-pill');
    setClass(pill, 'rec', recording);
    setClass(pill, 'play', playing);
    setHidden($('#macro-pill-dot'), !recording);
    setText($('#macro-pill-state'),
        recording ? 'Recording' : (playing ? 'Playing' : 'Idle'));
    setText($('#macro-pill-events'), plural(events, 'event'));

    // Slot pill: where the working macro came from, or "Not saved yet".
    const slotPill = $('#macro-slot-pill');
    const slotText = $('#macro-slot-text');
    const slotTag = $('#macro-slot-tag');
    let showSlotPill = false;
    let unsavedPill = false;
    let text = '';
    let tag = '';
    if (playing && s.macro_playing_source === 'active') {
        const slot = parseInt(s.macro_playing_slot, 10) || 0;
        const name = s.macro_playing_name || '';
        text = name ? `Active · slot ${slot} · ${name}` : `Active · slot ${slot}`;
        showSlotPill = true;
    } else if (loaded > 0) {
        const name = names[loaded] || names[String(loaded)] || '';
        text = name ? `Slot ${loaded} · ${name}` : `Slot ${loaded}`;
        tag = isDirty ? 'unsaved' : '';
        showSlotPill = true;
    } else if (isDirty && bufferEvents > 0 && !recording) {
        text = 'Not saved yet';
        unsavedPill = true;
        showSlotPill = true;
    }
    setText(slotText, text);
    setTitle(slotPill, text);
    setText(slotTag, tag);
    setHidden(slotTag, !tag);
    setClass(slotPill, 'unsaved', unsavedPill);
    setHidden(slotPill, !showSlotPill);
    const canSave = stateLabel === 'idle' && bufferEvents > 0 && isDirty;
    setHidden($('#macro-save-to'), !canSave);
    setHidden($('#macro-rec-note'), !recording);

    // Transport buttons.
    const recBtn = $('#macro-record');
    setText($('#macro-record-label'), recording ? 'Stop recording' : 'Record');
    setClass(recBtn, 'rec-on', recording);
    setClass(recBtn.querySelector('.rec-dot'), 'blink', recording);
    setDisabled(recBtn, playing);
    setDisabled($('#macro-play'), stateLabel !== 'idle' || bufferEvents === 0);
    setDisabled($('#macro-stop-play'), !playing);

    // Locks (#26): saved + active cards while recording or playing.
    const busy = recording || playing;
    const activeCard = $('#active-macros-card');
    setClass(activeCard, 'locked', busy);
    activeCard.setAttribute('aria-disabled', busy ? 'true' : 'false');
    const activeSub = activeCard.querySelector('.card-sub');
    if (activeSub && STATE.activeCapturing === null) {
        setText(activeSub, busy ? `unavailable while ${stateLabel}`
            : ((activeSub.dataset && activeSub.dataset.default) || ''));
    }
    if (busy && captureArmed()) cancelCapture();

    renderMacroNotice(s);
    renderEventsCardState();
}

// Fallback wording for a Python side without `macro_pending_hint`.
const _PENDING_TEXT = {
    save: 'Save (in Genshin): press 1–9 for slots 1–9, or 0 then 1–9 for slots 10–18. Esc or leaving the game cancels.',
    load: 'Load (in Genshin): press 1–9 for slots 1–9, or 0 then 1–9 for slots 10–18. Esc or leaving the game cancels.',
    confirm_record: 'Unsaved macro — press the Record hotkey again within 3 s to discard it and record.',
    confirm_load: 'Unsaved macro — press the Load hotkey again within 3 s to discard it and load a slot.',
};
const _PENDING_IDLE_TEXT = 'Save / Load: press the key, then 1–9 for slots 1–9, or 0 then 1–9 for slots 10–18. Esc cancels.';

// `macro_pending`: '' | 'save' | 'load' (slot picker armed) |
// 'confirm_record' | 'confirm_load' (in-game Record / Load over unsaved
// work needs a second press within 3 s). `macro_pending_hint` is the
// ready-made sentence for whichever is armed and is shown verbatim.
function renderMacroPending(s) {
    s = s || {};
    const el = $('#macro-pending');
    const pending = String(s.macro_pending || '');
    const hint = String(s.macro_pending_hint || '').trim();
    const confirm = pending.indexOf('confirm_') === 0;
    const live = !!pending;
    let text;
    if (live && hint) text = hint;
    else if (live) text = _PENDING_TEXT[pending] || 'Press the hotkey again to confirm.';
    else text = _PENDING_IDLE_TEXT;
    setText(el, text);
    setClass(el, 'live', live);
    setClass(el, 'confirm', confirm);
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
    saving: false,       // Save / Clear-instead in flight
    locked: false,       // row inputs disabled (mirrors `saving`)
};

// While a Save or "Clear slot instead" is in flight the rows and Add are
// disabled (MR8): an edit made meanwhile used to be wiped when the
// completion cleared `jsDirty` and re-fetched. The edit-generation check
// at completion stays as a second line of defence.
function setEditorLocked(locked) {
    const on = !!locked;
    if (evtState.locked === on) return;
    evtState.locked = on;
    const tbody = $('#evt-tbody');
    if (!tbody) return;
    tbody.querySelectorAll('input, select, button').forEach((el) => {
        el.disabled = on;
    });
    setClass($('#evt-list-wrapper'), 'busy', on);
}

function beginEditorSave() {
    evtState.saving = true;
    setEditorLocked(true);
    updateEvtButtons();
}

function endEditorSave() {
    evtState.saving = false;
    setEditorLocked(false);
    updateEvtButtons();
}

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
        const t = (typeof ev.time === 'number' ? ev.time : parseFloat(ev.time) || 0).toFixed(3);
        const dev = (ev.device || 'keyboard').toLowerCase();
        const evt = (ev.event_type || 'down').toLowerCase();
        const tr = document.createElement('tr');
        if (ev.__edited) tr.className = 'edited';
        tr.innerHTML = `
            <td class="i">${i + 1}</td>
            <td><input type="number" step="0.001" min="0" value="${t}" data-field="time" aria-label="Time"></td>
            <td><span class="sel"><select data-field="device" aria-label="Device">
                    <option value="keyboard"${dev === 'keyboard' ? ' selected' : ''}>keyboard</option>
                    <option value="mouse"${dev === 'mouse' ? ' selected' : ''}>mouse</option>
                </select></span></td>
            <td><input type="text" value="${escapeAttr(ev.key || '')}" data-field="key" spellcheck="false" aria-label="Key"></td>
            <td><span class="sel"><select data-field="event_type" aria-label="Event">
                    <option value="down"${evt === 'down' ? ' selected' : ''}>down</option>
                    <option value="up"${evt === 'up' ? ' selected' : ''}>up</option>
                    <option value="double"${evt === 'double' ? ' selected' : ''}>double</option>
                </select></span></td>
            <td><button type="button" class="del" title="Delete row">&times;</button></td>
        `;
        tr.querySelectorAll('[data-field]').forEach((el) => {
            el.addEventListener('input', () => {
                const field = el.dataset.field;
                let val = el.value;
                if (field === 'time') val = parseFloat(val) || 0;
                evtState.working[i][field] = val;
                evtState.working[i].__edited = true;
                tr.classList.add('edited');
                evtState.jsDirty = true;
                evtState.editRev += 1;
                updateEvtButtons();
            });
        });
        tr.querySelector('.del').addEventListener('click', () => {
            evtState.working.splice(i, 1);
            evtState.jsDirty = true;
            evtState.editRev += 1;
            renderEventsTable();
            updateEvtButtons();
        });
        tbody.appendChild(tr);
    });
    if (evtState.locked) {
        tbody.querySelectorAll('input, select, button').forEach((el) => {
            el.disabled = true;
        });
    }
    renderEvtCount();
}

function renderEvtCount() {
    const s = STATE.lastStatus || {};
    const n = evtState.visible ? evtState.working.length : (s.macro_events || 0);
    setText($('#evt-count'), `${n} in the working macro`);
}

function updateEvtButtons() {
    const visible = evtState.visible;
    const s = STATE.lastStatus || {};
    const busy = s.macro_state === 'recording' || s.macro_state === 'playing';
    setDisabled($('#evt-add'), !visible || evtState.rev < 0 || busy || evtState.saving);
    const saveBtn = $('#evt-save');
    setDisabled(saveBtn, !visible || !evtState.jsDirty || busy || evtState.saving);
    setTitle(saveBtn, (visible && busy)
        ? `Stop ${s.macro_state} before saving edits.` : '');
    setDisabled($('#evt-discard'), !visible || !evtState.jsDirty || evtState.saving);
    const tag = $('#evt-dirty-tag');
    let text = '';
    let stale = false;
    if (visible && evtState.jsDirty) {
        stale = editorIsStale();
        text = stale ? 'unsaved edits — the macro changed meanwhile' : 'unsaved edits';
    }
    setText(tag, text);
    setClass(tag, 'stale', stale);
    setHidden(tag, !text);
}

// Card body: table when shown, a one-line placeholder while recording.
function renderEventsCardState() {
    const s = STATE.lastStatus || {};
    const recording = s.macro_state === 'recording';
    setHidden($('#evt-placeholder'), !recording);
    setHidden($('#evt-list-wrapper'), !evtState.visible || recording);
    setDisabled($('#evt-toggle'), recording);
    renderEvtCount();
    updateEvtButtons();
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

$('#evt-toggle').addEventListener('click', async () => {
    if (evtState.visible) {
        if (evtState.jsDirty) {
            const ok = await dlgConfirm('Discard your edits?',
                'Your unsaved event edits will be lost.',
                { ok: 'Discard', danger: true, focusCancel: true });
            if (!ok) return;
        }
        evtState.visible = false;
        evtState.jsDirty = false;
        evtState.working = [];
        evtState.rev = -1;
        evtState.slot = 0;
        evtState.fetchGen += 1;     // drop any response still in flight
        $('#evt-toggle').textContent = 'Show';
        renderEventsCardState();
    } else {
        evtState.visible = true;
        $('#evt-toggle').textContent = 'Hide';
        renderEventsCardState();
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
        __edited: true,
    });
    evtState.jsDirty = true;
    evtState.editRev += 1;
    renderEventsTable();
    updateEvtButtons();
    const wrap = $('#evt-list-wrapper');
    if (wrap) wrap.scrollTop = wrap.scrollHeight;
});

$('#evt-discard').addEventListener('click', async () => {
    const ok = await dlgConfirm('Discard your edits?',
        'Your edits are thrown away and the list reloads from the macro.',
        { ok: 'Discard', danger: true, focusCancel: true });
    if (!ok) return;
    evtState.jsDirty = false;
    evtState.editRev += 1;          // an in-flight fetch must not be skipped
    fetchAndShowEvents();
});

// The buffer changed under the editor: never write these edits into
// whatever is loaded now — offer to throw them away instead.
async function offerEditorReload(res) {
    const s = STATE.lastStatus || {};
    const loaded = (res && typeof res.loaded === 'number')
        ? res.loaded : (parseInt(s.macro_loaded, 10) || 0);
    const was = evtState.slot ? slotLabel(evtState.slot) : 'the previous macro';
    const now = loaded ? slotLabel(loaded) : 'a different macro';
    const ok = await dlgConfirm('The macro changed',
        `The macro changed since you started editing (now ${now}).\n\n`
        + `Your edits were made to ${was} and were NOT saved, so they `
        + `can’t overwrite ${now}.\n\nDiscard your edits and show the `
        + 'current macro?',
        { ok: 'Discard edits', cancel: 'Keep editing', danger: true, focusCancel: true });
    if (ok) {
        evtState.jsDirty = false;
        evtState.editRev += 1;
        fetchAndShowEvents();
    }
    updateEvtButtons();
}

// Every row of a loaded slot was deleted. An empty macro can't be saved,
// so offer Clear slot instead (empties the buffer, then deletes the slot).
async function offerClearInstead(slot) {
    const users = activeUsersOfSlot(slot);
    let body = `You removed every event. An empty macro can’t be saved to `
        + `${slotLabel(slot)}.\n\nClear slot ${slot} instead? This cannot be undone.`;
    if (users.length) {
        body += `\n\nActive macro ${users.join(', ')} uses this slot and will be unassigned.`;
    }
    const ok = await dlgConfirm('Clear the slot instead?', body,
        { ok: `Clear slot ${slot}`, cancel: 'Keep editing', danger: true, focusCancel: true });
    if (!ok) return;
    if (evtState.saving || !evtState.visible) return;   // raced another Save
    const editRevAtClear = evtState.editRev;
    beginEditorSave();
    noteMacroAction('clear');
    callApi('macro_set_events', [], evtState.rev, slot, false).then((res) => {
        if (!res || res.ok === false) {
            if (res && res.reason === 'stale') {
                offerEditorReload(res);
            } else {
                dlgAlert('Could not clear the macro',
                    (res && res.message) || 'See the Logs tab.');
            }
            return null;
        }
        if (typeof res.rev === 'number') evtState.rev = res.rev;
        return callApi('macro_clear_slot', slot).then((r2) => {
            if (!r2 || r2.ok === false) {
                dlgAlert('Slot not cleared',
                    `The macro is now empty, but slot ${slot} could not be cleared: `
                    + ((r2 && r2.message) || 'see the Logs tab.'));
            } else {
                noteUnassigned(slot, r2);
            }
            if (evtState.editRev === editRevAtClear) {
                evtState.jsDirty = false;
                evtState.editRev += 1;
                fetchAndShowEvents();
            } else {
                // Edits arrived while this ran (the rows are locked, so
                // only a race can get here): keep them as unsaved edits
                // to the now-unassigned buffer rather than wiping them.
                evtState.slot = 0;
                updateEvtButtons();
            }
        });
    }).catch((e) => {
        console.error('[evt] clear failed', e);
    }).finally(endEditorSave);
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
    beginEditorSave();
    noteMacroAction('edit');
    // expected_slot is ALWAYS sent — 0 included, so an editor opened on an
    // unassigned buffer can't write persist=false over a slot loaded
    // meanwhile (null would disable Python's slot check).
    callApi('macro_set_events', sanitized, evtState.rev, slot, slot > 0)
        .then((res) => {
            if (!res) {
                dlgAlert('Could not apply edits', 'See the Logs tab.');
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
                // also in the macro notice (macro_error).
                dlgAlert('Could not save your edits',
                    res.message || res.reason || 'See the Logs tab.');
                return;
            }
            const dropped = parseInt(res.dropped, 10) || 0;
            if (dropped > 0) {
                dlgAlert('Some rows were dropped',
                    `${dropped} row${dropped === 1 ? ' was' : 's were'} `
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
        .finally(endEditorSave);
});

// Called on every status push. Re-fetches when the buffer revision
// moved (record finished, slot loaded, edits applied, slot cleared) and
// the user has no local edits; with local edits the dirty tag warns.
function maybeAutoRefreshEditor() {
    updateEvtButtons();
    if (!evtState.visible || evtState.jsDirty) return;
    const s = STATE.lastStatus || {};
    if (s.macro_state === 'recording') return;   // the list fills in on stop
    if (typeof s.macro_rev === 'number') {
        if (s.macro_rev === evtState.rev) return;
    } else if ((s.macro_events || 0) === evtState.working.length) {
        return;   // legacy fallback: no revision in the status
    }
    fetchAndShowEvents();
}

// --- log pane --------------------------------------------------------------

// Each log LINE is one element: time / [tag] / message (contract: the
// Python QueueWriter stamps `HH:MM:SS ` at the start of every line).
// Append-only; the oldest lines are dropped in one batch once past
// LOG_MAX_LINES; the view only follows new output while Follow is on,
// and Follow turns itself off when the user scrolls up — the detector
// logs every key DOWN/UP, and force-scrolling made it impossible to
// scroll up or select text during a song.
const LOG_MAX_LINES = 4000;
const LOG_TRIM_TO = 3200;
const LOG_STICK_PX = 24;
const LOG_LINE_RE = /^(\d\d:\d\d:\d\d) (?:\[([^\]]+)\]\s*)?(.*)$/;
const LOG_TAG_RE = /^\[([^\]]+)\]\s*(.*)$/;
const LOG_WARN_RE = /\b(failed|error|exception|traceback|refused|not found)\b/i;
const LOG_KNOWN_TAGS = new Set(['album', 'bot', 'macro', 'serial', 'ui']);
const LOG = {
    lines: 0,       // line elements in the pane
    stick: true,    // follow new output (last known, survives tab hides)
    partial: null,  // trailing line element not yet terminated by '\n'
    partialText: '',
    filter: '',
};

function logPaneAtBottom(pane) {
    return pane.scrollHeight - pane.scrollTop - pane.clientHeight <= LOG_STICK_PX;
}

function setLogFollow(on) {
    LOG.stick = !!on;
    setSwitch($('#log-follow'), LOG.stick);
}

function logCatchUp() {
    if (!LOG.stick) return;
    requestAnimationFrame(() => {
        const pane = $('#log-pane');
        pane.scrollTop = pane.scrollHeight;
    });
}

function makeLogLine(text) {
    let time = '';
    let tag = '';
    let msg = text;
    const m = LOG_LINE_RE.exec(text);
    if (m) {
        time = m[1];
        tag = (m[2] || '').toLowerCase();
        msg = m[3];
    } else {
        const t = LOG_TAG_RE.exec(text);
        if (t) {
            tag = t[1].toLowerCase();
            msg = t[2];
        }
    }
    const group = LOG_KNOWN_TAGS.has(tag) ? tag : 'other';
    const div = document.createElement('div');
    div.className = LOG_WARN_RE.test(msg) ? 'ln warnline' : 'ln';
    div.dataset.g = group;
    const tEl = document.createElement('span');
    tEl.className = 't';
    tEl.textContent = time;
    const gEl = document.createElement('span');
    gEl.className = tag ? `g ${tag.replace(/[^a-z0-9_-]/g, '')}` : 'g';
    gEl.textContent = tag ? `[${tag}]` : '';
    const mEl = document.createElement('span');
    mEl.className = 'm';
    mEl.textContent = msg;
    div.appendChild(tEl);
    div.appendChild(gEl);
    div.appendChild(mEl);
    return div;
}

$('#btn-clear-log').addEventListener('click', () => {
    const pane = $('#log-pane');
    pane.textContent = '';
    LOG.lines = 0;
    LOG.partial = null;
    LOG.partialText = '';
    setLogFollow(true);
    callApi('clear_log');
});

$('#log-pane').addEventListener('scroll', () => {
    const pane = $('#log-pane');
    if (pane.clientHeight === 0) return;   // hidden tab — keep last value
    const at = logPaneAtBottom(pane);
    if (at !== LOG.stick) setLogFollow(at);
});

$('#log-follow').addEventListener('click', () => {
    const next = !LOG.stick;
    setLogFollow(next);
    if (next) {
        const pane = $('#log-pane');
        pane.scrollTop = pane.scrollHeight;
    }
});

$$('#log-filters .fchip').forEach((chip) => {
    chip.addEventListener('click', () => {
        LOG.filter = chip.dataset.f || '';
        $$('#log-filters .fchip').forEach((c) => {
            const on = c === chip;
            c.classList.toggle('on', on);
            c.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        const pane = $('#log-pane');
        pane.dataset.filter = LOG.filter;
        logCatchUp();
    });
});

function visibleLogText() {
    const pane = $('#log-pane');
    const out = [];
    Array.from(pane.children).forEach((ln) => {
        if (LOG.filter && ln.dataset.g !== LOG.filter) return;
        const parts = Array.from(ln.children).map((c) => c.textContent).filter(Boolean);
        out.push(parts.join(' '));
    });
    return out.join('\n');
}

function copyText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        return navigator.clipboard.writeText(text).then(() => true, () => copyTextFallback(text));
    }
    return Promise.resolve(copyTextFallback(text));
}

function copyTextFallback(text) {
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-1000px';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand && document.execCommand('copy');
        ta.remove();
        return !!ok;
    } catch (e) {
        return false;
    }
}

$('#btn-copy-log').addEventListener('click', () => {
    const btn = $('#btn-copy-log');
    copyText(visibleLogText()).then((ok) => {
        btn.textContent = ok ? 'Copied' : 'Copy failed';
        setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
    });
});

// --- globals called from Python -------------------------------------------

window.appendLog = function appendLog(text) {
    if (!text) return;
    const pane = $('#log-pane');
    const stick = pane.clientHeight > 0 ? logPaneAtBottom(pane) : LOG.stick;
    // A chunk may end mid-line; the open line is re-rendered when the
    // rest arrives so its time / tag columns parse correctly.
    let buf = LOG.partialText + text;
    if (LOG.partial) {
        pane.removeChild(LOG.partial);
        LOG.partial = null;
        LOG.lines -= 1;
    }
    LOG.partialText = '';
    const frag = document.createDocumentFragment();
    let start = 0;
    let added = 0;
    for (;;) {
        const nl = buf.indexOf('\n', start);
        if (nl < 0) break;
        const line = buf.slice(start, nl).replace(/\r$/, '');
        if (line) {
            frag.appendChild(makeLogLine(line));
            added += 1;
        }
        start = nl + 1;
    }
    const rest = buf.slice(start);
    if (rest) {
        const el = makeLogLine(rest);
        el.classList.add('partial');
        frag.appendChild(el);
        LOG.partial = el;
        LOG.partialText = rest;
        added += 1;
    }
    buf = null;
    pane.appendChild(frag);
    LOG.lines += added;
    if (LOG.lines > LOG_MAX_LINES) {
        let drop = LOG.lines - LOG_TRIM_TO;
        while (drop > 0 && pane.firstChild && pane.firstChild !== LOG.partial) {
            pane.removeChild(pane.firstChild);
            drop -= 1;
            LOG.lines -= 1;
        }
    }
    if (stick) {
        pane.scrollTop = pane.scrollHeight;
        if (!LOG.stick) setLogFollow(true);
    }
};

window.updateStatus = function updateStatus(data) {
    if (!data) return;
    const prev = STATE.lastStatus || {};
    STATE.lastStatus = Object.assign({}, prev, data);
    const s = STATE.lastStatus;

    // Main-tab bot status. Start/Stop debounce ends as soon as a push
    // shows the state (or last_error) moved.
    const botState = s.state || 'idle';
    const botLive = botState === 'running' || botState === 'paused';
    if (MAIN.startPending && (botState !== MAIN.startPending.state
            || (s.last_error || '') !== MAIN.startPending.lastError)) {
        settleStartPending();
    }

    if ('run_started_at' in data) {
        const started = parseFloat(data.run_started_at) || 0;
        if (started !== MAIN.runStartedAt) {
            MAIN.runStartedAt = started;
            MAIN.runEndedAt = 0;
        }
    }
    const wasLive = prev.state === 'running' || prev.state === 'paused';
    if (wasLive && !botLive && MAIN.runStartedAt && !MAIN.runEndedAt) {
        MAIN.runEndedAt = Date.now() / 1000;   // freeze the final elapsed
    }
    if (!botLive && !MAIN.runStartedAt) MAIN.runEndedAt = 0;

    if ('state' in data || 'mode' in data || 'song' in data || 'album_progress' in data) {
        renderStageText();
        renderStartButtons(s);
        renderDebugSwitch();
        renderAlbumProgress(s.album_progress, botState);
    }
    if ('state' in data || 'last_error' in data) {
        renderBotNotice();
    }
    if ('prereq_tip_dismissed' in data) {
        MAIN.tipDismissed = !!data.prereq_tip_dismissed;
        renderPrereqTip();
    }
    if ('song' in data || 'album_progress' in data || 'state' in data) {
        const prog = s.album_progress;
        let songText = '—';
        if (prog && typeof prog === 'object' && (parseInt(prog.total, 10) || 0) > 0) {
            const idx = parseInt(prog.index, 10) || 0;
            songText = `${idx} / ${prog.total}`;
            if (prog.difficulty) songText += ` · ${_DIFF_LABEL[prog.difficulty] || prog.difficulty}`;
        } else if (s.song) {
            songText = String(s.song);
        }
        setText($('#st-song'), songText);
    }
    if ('fps' in data) {
        const f = parseFloat(data.fps) || 0;
        setText($('#st-fps'), f ? f.toFixed(1) : '—');
    }
    renderElapsed();
    if ('lanes' in data || 'state' in data) {
        renderLanes(botLive ? s.lanes : null);
    }
    if ('debug' in data) {
        // Sync the switch without echoing back to Python. While idle
        // `debug` is the preset for the next Start, not a live viz window.
        MAIN.debug = !!data.debug;
        renderDebugSwitch();
    }
    if ('admin' in data) renderHealthAdmin(!!data.admin);
    if ('game_window_found' in data) {
        renderHealthGame(typeof data.game_window_found === 'boolean'
            ? data.game_window_found : undefined);
    }
    if ('keybinds' in data) renderKeybinds(data.keybinds);

    if ('input_backend' in data) {
        const sel = $('#input-backend');
        if (sel && sel.value !== data.input_backend && sel !== activeEl()) {
            sel.value = data.input_backend;
        }
    }

    if ('macro_state' in data || 'macro_events' in data
        || 'macro_loaded' in data || 'macro_slot_names' in data
        || 'macro_dirty' in data || 'macro_playing_slot' in data
        || 'macro_playing_source' in data || 'macro_error' in data
        || 'macro_playing_events' in data || 'macro_playing_name' in data) {
        renderMacroState(s);
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
        || 'macro_loaded' in data || 'macro_state' in data
        || 'macro_active_macros' in data) {
        // Slot names + loaded indicator may arrive in a later snapshot
        // than the slot list itself, so always render from the merged
        // snapshot rather than just `data`.
        renderMacroSlots(s);
    }
    if ('macro_pending' in data || 'macro_pending_hint' in data) renderMacroPending(s);
    if ('macro_active_macros' in data || 'macro_conflict_policy' in data
        || 'macro_slots' in data || 'macro_slot_names' in data
        || 'macro_state' in data || 'macro_playing_slot' in data
        || 'macro_playing_source' in data) {
        renderActiveMacroRows(s);
        renderActivePolicy(s.macro_conflict_policy);
    }

    // Button states from the live state machine.
    renderStartButtons(s);

    // Key caps stay enabled regardless of bot state — the user can
    // configure hotkeys without first starting the bot. The hotkeys
    // themselves are still inert when the bot is off (runtime gate in
    // ui.py:_hotkey_pause / _hotkey_debug).
};

// --- help-th portal tooltips ----------------------------------------------

// `.help-th` icons sit inside `.evt-wrap` which has `overflow: auto`
// (scrollable event list). Auto-overflow clips the standard
// `.help::after` pseudo-element tooltip against the wrapper's box on
// both axes, so when the wrapper is short (few or no events) the tooltip
// is invisible. Render the tooltip in `document.body` instead,
// positioned via getBoundingClientRect — it lives outside the overflow
// context and stays visible regardless of wrapper size.
function setupHelpThPortals() {
    document.querySelectorAll('.help-th').forEach((icon) => {
        const text = icon.getAttribute('data-help');
        if (!text) return;
        let tip = null;

        const position = () => {
            if (!tip) return;
            const r = icon.getBoundingClientRect();
            tip.style.top = (r.bottom + 8) + 'px';
            // Clamp left so the 300px-max tooltip doesn't overflow
            // the right edge of the viewport on narrow windows.
            const maxLeft = (window.innerWidth || 720) - 300 - 8;
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

// --- init ------------------------------------------------------------------

function init() {
    if (STATE.initialized) return;
    STATE.initialized = true;
    callApi('get_initial_state').then((s) => {
        if (!s) return;
        if (typeof s.songs === 'number') {
            $('#songs').value = s.songs;
            MAIN.lastSongs = s.songs;
            renderSongsSub(s.songs);
        }
        if (s.difficulty !== undefined) applyDifficultiesToChips(s.difficulty);
        if (typeof s.replay_canorus === 'boolean') setSwitch($('#replay-canorus'), s.replay_canorus);
        if (typeof s.debug === 'boolean') MAIN.debug = s.debug;
        if (s.input_backend) $('#input-backend').value = s.input_backend;
        if (s.keybinds) renderKeybinds(s.keybinds);
        if (typeof s.prereq_tip_dismissed === 'boolean') MAIN.tipDismissed = s.prereq_tip_dismissed;
        setModeUi(s.mode === 'album' ? 'album' : 'standalone');
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
            prereq_tip_dismissed: !!s.prereq_tip_dismissed,
            macro_state: macroSnap.macro_state || 'idle',
            macro_events: macroSnap.macro_events || 0,
            macro_slots: macroSnap.macro_slots || [],
            macro_slot_names: macroSnap.macro_slot_names || {},
            macro_loaded: macroSnap.macro_loaded || 0,
            macro_dirty: !!macroSnap.macro_dirty,
            macro_pending: macroSnap.macro_pending || '',
            macro_pending_hint: macroSnap.macro_pending_hint || '',
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

// Pre-init render so the page is coherent before Python answers.
renderMacroPending({});
renderStageText();

window.addEventListener('pywebviewready', init);
// pywebviewready may have fired before this script ran in some loads.
if (window.pywebview && window.pywebview.api) init();
