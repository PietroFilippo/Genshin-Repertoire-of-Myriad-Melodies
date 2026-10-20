// Node-run regression test for the hotkey-capture guard in
// pc_client/web/app.js (HK1 / HK2). Loads the real app.js into a vm with a
// minimal fake DOM + fake pywebview bridge, then drives the capture flow.
//
// Run:  node tests/capture_guard.test.js      (exit code 0 = pass)
// Also run by tests/test_ux_hotkeys.py when node is on PATH.
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// APP_JS env override lets a reviewer point the test at a mutated copy.
const APP_JS = process.env.APP_JS
    || path.join(__dirname, '..', 'pc_client', 'web', 'app.js');

// --- fake DOM ---------------------------------------------------------------

// Anything app.js touches that this test doesn't care about: callable,
// indexable, coerces to '' / 0, not a thenable, iterates as empty.
function universal() {
    const fn = function () { return universal(); };
    return new Proxy(fn, {
        get(_t, prop) {
            if (prop === Symbol.toPrimitive) return () => '';
            if (prop === Symbol.iterator) return function* () {};
            if (prop === 'then') return undefined;
            if (prop === 'length') return 0;
            return universal();
        },
        apply() { return universal(); },
        set() { return true; },
    });
}

function makeEl(label) {
    const listeners = {};
    const children = {};
    const closestCache = {};
    const base = {
        __label: label,
        __listeners: listeners,
        dataset: {},
        textContent: '',
        title: '',
        value: '',
        checked: false,
        disabled: false,
        innerHTML: '',
        style: {},
        classList: {
            toggle() {}, add() {}, remove() {}, contains() { return false; },
        },
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener() {},
        dispatchEvent() { return true; },
        querySelector(sel) { return (children[sel] = children[sel] || makeEl(`${label} ${sel}`)); },
        querySelectorAll() { return []; },
        closest(sel) { return (closestCache[sel] = closestCache[sel] || makeEl(`${label}^${sel}`)); },
        appendChild(c) { return c; },
        removeChild() {},
        setAttribute() {},
        getAttribute() { return null; },
        getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
        focus() {}, blur() {}, click() {}, scrollIntoView() {},
    };
    return new Proxy(base, {
        get(t, prop) { return prop in t ? t[prop] : universal(); },
        set(t, prop, v) { t[prop] = v; return true; },
    });
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function makeEnv() {
    const byId = {};
    const calls = [];            // [name, args]
    const pending = {};          // name -> [deferred, ...] (FIFO)
    const autoReply = {};        // name -> value for calls nobody awaits
    const windowListeners = {};

    const document = {
        querySelector(sel) { return (byId[sel] = byId[sel] || makeEl(sel)); },
        querySelectorAll() { return []; },
        getElementById(id) { return this.querySelector(`#${id}`); },
        createElement(tag) { return makeEl(`<${tag}>`); },
        addEventListener() {},
        body: makeEl('body'),
        documentElement: makeEl('html'),
    };

    const api = new Proxy({}, {
        get(_t, name) {
            if (typeof name !== 'string') return undefined;
            return (...args) => {
                calls.push([name, args]);
                if (pending[name] && pending[name].length) {
                    return pending[name].shift().promise;
                }
                return autoReply[name] !== undefined ? autoReply[name] : null;
            };
        },
    });

    const timers = [];
    const sandbox = {
        console,
        document,
        Promise,
        Date,
        Math,
        JSON,
        Object,
        Array,
        Set,
        Map,
        Number,
        String,
        Boolean,
        Symbol,
        Proxy,
        Error,
        parseInt,
        parseFloat,
        isNaN,
        performance: { now: () => Date.now() },
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
        requestAnimationFrame: () => 0,
        getComputedStyle: () => universal(),
        confirm: () => false,
        alert: () => {},
        Event: function Event(type) { this.type = type; },
        CustomEvent: function CustomEvent(type) { this.type = type; },
        MutationObserver: function () { return { observe() {}, disconnect() {} }; },
        ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        navigator: { userAgent: 'node' },
        location: { reload() {} },
        addEventListener(type, fn) {
            (windowListeners[type] = windowListeners[type] || []).push(fn);
        },
        removeEventListener() {},
        pywebview: { api },
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox, { filename: 'app.js' });

    function fire(type, init) {
        const ev = Object.assign({
            preventDefault() {}, stopPropagation() {}, repeat: false,
            target: { closest: () => null },
        }, init);
        (windowListeners[type] || []).forEach((fn) => fn(ev));
        return ev;
    }

    return {
        sandbox,
        calls,
        names: () => calls.map((c) => c[0]),
        callsOf: (name) => calls.filter((c) => c[0] === name),
        hold(name) {
            const d = deferred();
            (pending[name] = pending[name] || []).push(d);
            return d;
        },
        autoReply,
        key: (key, extra) => fire('keydown', Object.assign({ key, code: key }, extra)),
        mouse: (button, target) => fire('mousedown', { button, target: target || { closest: () => null } }),
        blur: () => fire('blur', {}),
        msg: (sel) => document.querySelector(sel).textContent,
        el: (sel) => document.querySelector(sel),
    };
}

const flush = () => new Promise((r) => setImmediate(r));

// --- tests --------------------------------------------------------------------

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

test('HK1: active-macro Bind suspends hotkeys and waits for confirmation', async () => {
    const env = makeEnv();
    const begin = env.hold('capture_begin');
    env.sandbox.beginActiveCapture(1);
    const begins = env.callsOf('capture_begin');
    assert.strictEqual(begins.length, 1, 'active capture must call capture_begin');
    const token = begins[0][1][0];
    assert.ok(Number.isFinite(token) && token > 0, 'token passed');

    env.key('F8');                                   // before Python confirms
    await flush();
    assert.strictEqual(env.callsOf('macro_set_active_hotkey').length, 0,
        'key accepted before suspension was confirmed');

    begin.resolve(true);
    await flush();
    const bind = env.hold('macro_set_active_hotkey');
    env.key('F8');
    assert.strictEqual(env.callsOf('macro_set_active_hotkey').length, 1);
    assert.deepStrictEqual(env.callsOf('macro_set_active_hotkey')[0][1][0], 1);

    bind.resolve({ ok: true, hotkey: 'f8' });
    await flush();
    const ends = env.callsOf('capture_end');
    assert.strictEqual(ends.length, 1, 'capture_end on success');
    assert.strictEqual(ends[0][1][0], token, 'capture_end carries the token');
});

test('HK1: active capture releases on Esc, blur and left click', async () => {
    for (const exit of ['esc', 'blur', 'left']) {
        const env = makeEnv();
        env.autoReply.capture_begin = true;
        env.sandbox.beginActiveCapture(2);
        await flush();
        if (exit === 'esc') env.key('Escape');
        if (exit === 'blur') env.blur();
        if (exit === 'left') env.mouse(0);
        assert.strictEqual(env.callsOf('capture_end').length, 1, `capture_end on ${exit}`);
        env.key('F8');
        await flush();
        assert.strictEqual(env.callsOf('macro_set_active_hotkey').length, 0,
            `no bind after ${exit}`);
    }
});

test('HK2: standard capture accepts no key until capture_begin resolves', async () => {
    const env = makeEnv();
    const begin = env.hold('capture_begin');
    env.sandbox.beginCapture('start_stop');
    env.key('F8');
    env.mouse(4);                                     // Mouse 5 while pending
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 0, 'bound before suspension');

    begin.resolve(true);
    await flush();
    env.autoReply.rebind = 'f7';
    env.key('F7');
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 1);
    assert.strictEqual(env.callsOf('capture_end').length, 1);
});

test('HK2: cancel while capture_begin is in flight never becomes ready', async () => {
    const env = makeEnv();
    const begin = env.hold('capture_begin');
    env.sandbox.beginCapture('pause');
    env.key('Escape');                               // cancel during the await
    assert.strictEqual(env.callsOf('capture_end').length, 1);
    begin.resolve(true);                             // late confirmation
    await flush();
    env.key('F9');
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 0, 'late begin re-opened capture');
});

test('HK2: a superseded arming does not make the new prompt ready', async () => {
    const env = makeEnv();
    const first = env.hold('capture_begin');
    env.sandbox.beginCapture('debug');
    env.key('Escape');
    const second = env.hold('capture_begin');
    env.sandbox.beginCapture('debug');
    first.resolve(true);                             // stale token resolves
    await flush();
    env.key('F10');
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 0, 'stale confirmation accepted');
    second.resolve(true);
    await flush();
    env.autoReply.rebind = 'f10';
    env.key('F10');
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 1);
});

test('HK2: failed capture_begin cancels the prompt', async () => {
    const env = makeEnv();
    env.autoReply.capture_begin = false;
    env.sandbox.beginCapture('start_stop');
    await flush();
    assert.strictEqual(env.callsOf('capture_end').length, 1, 'released after failure');
    env.key('F8');
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 0);
});

test('refusal re-arms and blocks input until re-confirmed; one submit at a time', async () => {
    const env = makeEnv();
    env.autoReply.capture_begin = true;
    env.sandbox.beginCapture('pause');
    await flush();
    const refused = env.hold('rebind');
    env.key('a');
    env.key('b');                                    // second key while in flight
    assert.strictEqual(env.callsOf('rebind').length, 1, 'double submission');
    const rearm = env.hold('capture_begin');
    refused.resolve({ ok: false, reason: 'rhythm_key' });
    await flush();
    assert.strictEqual(env.callsOf('capture_begin').length, 2, 're-armed after refusal');
    env.key('F9');
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 1, 'accepted before re-arm confirmed');
    rearm.resolve(true);
    await flush();
    env.autoReply.rebind = 'f9';
    env.key('F9');
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 2);
});

test('U1: left click cancels capture and never binds', async () => {
    const env = makeEnv();
    env.autoReply.capture_begin = true;
    env.sandbox.beginCapture('start_stop');
    await flush();
    env.mouse(0);
    await flush();
    assert.strictEqual(env.callsOf('rebind').length, 0);
    assert.strictEqual(env.callsOf('capture_end').length, 1);
});

(async () => {
    let failed = 0;
    for (const t of tests) {
        try {
            await t.fn();
            console.log(`ok   ${t.name}`);
        } catch (e) {
            failed += 1;
            console.log(`FAIL ${t.name}\n     ${e && e.stack ? e.stack : e}`);
        }
    }
    console.log(`${tests.length - failed}/${tests.length} passed`);
    process.exit(failed ? 1 : 0);
})();
