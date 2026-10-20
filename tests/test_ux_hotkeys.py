"""Regression tests for the hotkey UX fixes (R2, M3/R16, M2/R17, U1, U6/R18,
R15, U10).

Headless: no real keyboard / mouse hook is installed. `keyboard.hook` is
patched out and key events are fed straight into
KeybindManager._on_kb_event, the matcher the real hook would call.

Run:  python -m unittest tests.test_ux_hotkeys -v
"""
import functools
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc_client'))

import keyboard                                         # noqa: E402
import mouse                                            # noqa: E402
import ui_core                                          # noqa: E402
from ui_core import (KeybindManager, js_event_to_hotkey,  # noqa: E402
                     normalize_hotkey, validate_hotkey)

try:                                                    # BridgeApi lives here
    import ui as ui_mod                                 # noqa: E402
except Exception:                                       # pragma: no cover
    ui_mod = None


def _sc(name):
    return keyboard.key_to_scan_codes(name)[0]


class _Harness(unittest.TestCase):
    """KeybindManager with the OS hooks and foreground gates stubbed."""

    def setUp(self):
        for target, attr, kwargs in (
                (ui_core.keyboard, 'hook', {'return_value': 'kb-hook'}),
                (ui_core, 'is_allowed_foreground', {'return_value': True}),
                (ui_core, 'is_allowed_game_only', {'return_value': True})):
            p = mock.patch.object(target, attr, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        # Windows' view of the modifiers (GetAsyncKeyState): by default it
        # agrees with the event stream; HK5 tests override it.
        p = mock.patch.object(ui_core, '_async_key_down', return_value=True)
        self.async_down = p.start()
        self.addCleanup(p.stop)
        self.km = KeybindManager(lambda: 0)
        self.fired = []
        self.clock = 1000.0

    def bind(self, action, hotkey, scope='app'):
        return self.km.set(action, hotkey,
                           lambda a=action: self.fired.append(a), scope)

    def _event(self, kind, name, t):
        if t is None:
            self.clock += 0.25
            t = self.clock
        return keyboard.KeyboardEvent(kind, _sc(name), name, time=t)

    def down(self, name, t=None):
        return self.km._on_kb_event(self._event(keyboard.KEY_DOWN, name, t))

    def up(self, name, t=None):
        return self.km._on_kb_event(self._event(keyboard.KEY_UP, name, t))

    def tap(self, name):
        self.down(name)
        self.up(name)

    def settle(self):
        self.km._dispatch_q.join()     # dispatcher has run every callback
        return list(self.fired)


class HeldKeysDoNotBlockHotkeys(_Harness):
    """R2: keyboard.add_hotkey matched the ENTIRE pressed-key set, so while
    the bot held a rhythm key F8 was {a, f8} and never fired."""

    def test_start_stop_fires_while_bot_holds_a_rhythm_key(self):
        self.bind('start_stop', 'f8')
        self.down('a')                      # bot holding an A note
        self.tap('f8')
        self.assertEqual(self.settle(), ['start_stop'])

    def test_macro_hotkey_fires_while_macro_holds_w_and_shift(self):
        self.bind('macro_record', 'y', scope='game')
        self.down('w')
        self.down('shift')
        self.tap('y')
        self.assertEqual(self.settle(), ['macro_record'])

    def test_named_modifier_is_required(self):
        self.bind('start_stop', 'ctrl+f8')
        self.tap('f8')
        self.assertEqual(self.settle(), [])
        self.down('ctrl')
        self.tap('f8')
        self.assertEqual(self.settle(), ['start_stop'])

    def test_most_specific_binding_wins(self):
        self.bind('start_stop', 'f8')
        self.bind('pause', 'shift+f8')
        self.down('shift')
        self.tap('f8')
        self.assertEqual(self.settle(), ['pause'])
        self.up('shift')
        self.tap('f8')
        self.assertEqual(self.settle(), ['pause', 'start_stop'])

    def test_extra_modifier_tolerated_without_a_more_specific_binding(self):
        self.bind('start_stop', 'f8')
        self.down('shift')                  # sprinting in Genshin
        self.tap('f8')
        self.assertEqual(self.settle(), ['start_stop'])

    def test_right_side_modifier_satisfies_unsided_binding(self):
        self.bind('pause', 'shift+f9')
        self.down('right shift')
        self.tap('f9')
        self.assertEqual(self.settle(), ['pause'])

    def test_aliases_normalize_like_the_keyboard_package(self):
        self.assertTrue(self.bind('debug', 'Control+F10'))
        self.assertEqual(self.km.get_bindings()['debug'], 'ctrl+f10')
        self.down('ctrl')
        self.tap('f10')
        self.assertEqual(self.settle(), ['debug'])

    def test_matcher_never_returns_truthy(self):
        """GenericListener.invoke_handlers stops at the first truthy
        handler -- that would starve MacroController's recording hook."""
        self.bind('start_stop', 'f8')
        self.assertFalse(self.down('f8'))
        self.assertFalse(self.up('f8'))
        self.assertFalse(self.down('q'))


class AutoRepeatLatch(_Harness):
    """M3 / R16: holding Y re-toggled record (wiping the buffer), holding
    F9 flapped Pause/Resume."""

    def test_held_key_fires_once(self):
        self.bind('pause', 'f9')
        t = 2000.0
        for i in range(20):                 # 20 auto-repeat KEY_DOWNs
            self.down('f9', t + i * 0.033)
        self.assertEqual(self.settle(), ['pause'])
        self.up('f9', t + 1.0)
        self.down('f9', t + 1.2)
        self.assertEqual(self.settle(), ['pause', 'pause'])

    def test_key_up_rearms_not_time(self):
        """A long hold that keeps auto-repeating never re-fires, however
        long it lasts."""
        self.bind('macro_record', 'y', scope='game')
        t = 3000.0
        for i in range(400):                # ~13 s of auto-repeat
            self.down('y', t + i * 0.033)
        self.assertEqual(self.settle(), ['macro_record'])

    def test_lost_key_up_does_not_wedge_the_hotkey(self):
        self.bind('start_stop', 'f8')
        self.down('f8', 4000.0)
        # KEY_UP never arrives (secure desktop); much later a new press.
        self.down('f8', 4000.0 + KeybindManager._KB_STALE_HOLD_S + 1)
        self.assertEqual(self.settle(), ['start_stop', 'start_stop'])

    def test_rebind_while_held_does_not_fire_on_repeat(self):
        self.bind('start_stop', 'f8')
        self.down('f8', 5000.0)
        self.km.clear_all()                 # _install_keybinds path
        self.bind('start_stop', 'f8')
        self.down('f8', 5000.5)             # auto-repeat of the same press
        self.assertEqual(self.settle(), ['start_stop'])


class MouseDoubleIsAPress(_Harness):
    """M2 / R17: the mouse package reports a press within the
    double-click interval of ANY previous button event as 'double'
    instead of 'down', so Stop (Mouse 5) right after a click was lost."""

    def _bind_with_real_on_button(self, action, binding):
        handlers = []
        with mock.patch.object(ui_core.mouse._listener, 'add_handler',
                               side_effect=handlers.append):
            self.assertTrue(self.bind(action, binding, scope='game'))
        self.assertEqual(len(handlers), 1)
        return handlers[0]

    def test_double_typed_press_fires(self):
        handler = self._bind_with_real_on_button('macro_stop', 'mouse:x2')
        handler(mouse.ButtonEvent(mouse.DOUBLE, mouse.X2, time.time()))
        self.assertEqual(self.settle(), ['macro_stop'])

    def test_down_fires_and_release_does_not(self):
        handler = self._bind_with_real_on_button('macro_play', 'mouse:x')
        handler(mouse.ButtonEvent(mouse.DOWN, mouse.X, time.time()))
        handler(mouse.ButtonEvent(mouse.UP, mouse.X, time.time()))
        self.assertEqual(self.settle(), ['macro_play'])

    def test_other_buttons_ignored(self):
        handler = self._bind_with_real_on_button('macro_play', 'mouse:x')
        handler(mouse.ButtonEvent(mouse.DOUBLE, mouse.LEFT, time.time()))
        self.assertEqual(self.settle(), [])


class LeftClickIsNeverABinding(_Harness):
    """U1: a stray left click during capture bound Play to Left Click, so
    every attack replayed the macro."""

    ACTIONS = ('start_stop', 'pause', 'debug', 'macro_record', 'macro_play',
               'macro_stop', 'macro_save', 'macro_load', 'macro_active_1')

    def test_validate_rejects_left_click_for_every_action(self):
        for action in self.ACTIONS:
            self.assertEqual(validate_hotkey(action, 'mouse:left'),
                             (None, 'left_click_reserved'), action)
            self.assertEqual(validate_hotkey(action, ' MOUSE:Left '),
                             (None, 'left_click_reserved'), action)

    def test_set_refuses_left_click_and_keeps_old_binding(self):
        with mock.patch.object(ui_core.mouse, 'on_button',
                               return_value='mouse-hook') as on_button:
            self.assertTrue(self.bind('macro_play', 'mouse:x'))
            self.assertFalse(self.bind('macro_play', 'mouse:left'))
        on_button.assert_called_once()
        self.assertEqual(self.km.get_bindings()['macro_play'], 'mouse:x')

    def test_other_mouse_buttons_still_allowed(self):
        for btn in ('right', 'middle', 'x', 'x2'):
            self.assertEqual(validate_hotkey('macro_play', f'mouse:{btn}'),
                             (f'mouse:{btn}', None))


class UnsupportedKeysAreRefusedUpFront(_Harness):
    """U6 / R18: unbindable names were saved + displayed, and set() logged
    'failed to bind' only after the old binding was gone."""

    def test_unmappable_names_rejected(self):
        for key in ('Dead', 'Unidentified', 'Process', 'MediaPlayPause'):
            hk = js_event_to_hotkey({'key': key})
            self.assertEqual(validate_hotkey('macro_play', hk),
                             (None, 'unsupported_key'), key)

    def test_failed_rebind_keeps_previous_binding_working(self):
        self.assertTrue(self.bind('start_stop', 'f8'))
        self.assertFalse(self.bind('start_stop', 'dead'))
        self.assertEqual(self.km.get_bindings()['start_stop'], 'f8')
        self.tap('f8')
        self.assertEqual(self.settle(), ['start_stop'])

    def test_shift_plus_is_normalized(self):
        hk = js_event_to_hotkey({'key': '+', 'shiftKey': True})
        self.assertEqual(hk, 'shift+plus')
        self.assertEqual(validate_hotkey('macro_play', hk),
                         ('shift+plus', None))
        # What older builds saved for the same press.
        self.assertEqual(normalize_hotkey('shift++'), 'shift+plus')
        self.assertEqual(js_event_to_hotkey({'key': ','}), 'comma')

    def test_equivalent_spellings_compare_equal(self):
        self.assertEqual(normalize_hotkey('shift+ctrl+F8'),
                         normalize_hotkey('ctrl+shift+f8'))
        self.assertEqual(normalize_hotkey('escape'), 'esc')


class RhythmKeysCannotDriveTheBot(unittest.TestCase):
    """R15: the bot presses a s d j k l itself -- an A note toggled
    Pause."""

    def test_bare_rhythm_key_refused_for_bot_actions(self):
        for action in ('start_stop', 'pause', 'debug'):
            for key in ('a', 's', 'd', 'j', 'k', 'l', 'A'):
                self.assertEqual(validate_hotkey(action, key),
                                 (None, 'rhythm_key'), (action, key))

    def test_modified_or_macro_bindings_allowed(self):
        self.assertEqual(validate_hotkey('pause', 'ctrl+a'),
                         ('ctrl+a', None))
        self.assertEqual(validate_hotkey('macro_record', 'a'), ('a', None))


class CaptureSuspendsHotkeys(_Harness):
    """U10: pressing F8 inside the capture prompt (UI foreground, 'app'
    scope) stopped the bot; F10 toggled the debug window."""

    def test_presses_are_dropped_not_queued(self):
        self.bind('start_stop', 'f8')
        self.km.suspend(token=1)
        self.tap('f8')
        self.assertEqual(self.km._dispatch_q.qsize(), 0)
        self.km.resume(token=1)
        self.assertEqual(self.settle(), [])
        self.tap('f8')
        self.assertEqual(self.settle(), ['start_stop'])

    def test_mouse_bindings_are_suspended_too(self):
        wrapper = self.km._gate(lambda: self.fired.append('m'), 'game', 'm')
        self.km.suspend()
        wrapper()
        self.assertEqual(self.km._dispatch_q.qsize(), 0)
        self.km.resume()
        wrapper()
        self.assertEqual(self.settle(), ['m'])

    def test_key_held_through_capture_does_not_fire_after_resume(self):
        """The user presses F8 to bind it and is still holding it when
        the prompt closes: its auto-repeat must not start the bot."""
        self.bind('start_stop', 'f8')
        self.km.suspend(token=1)
        self.down('f8', 6000.0)
        self.km.resume(token=1)
        self.down('f8', 6000.5)
        self.assertEqual(self.settle(), [])

    def test_suspension_expires_on_its_own(self):
        self.bind('start_stop', 'f8')
        self.km.suspend(seconds=0.05)
        time.sleep(0.1)
        self.tap('f8')
        self.assertEqual(self.settle(), ['start_stop'])

    def test_suspension_is_capped(self):
        self.km.suspend(seconds=3600)
        remaining = self.km._suspended_until - time.monotonic()
        self.assertLessEqual(remaining, KeybindManager.CAPTURE_SUSPEND_MAX_S)

    def test_out_of_order_end_does_not_cancel_newer_capture(self):
        self.km.suspend(token=1)
        self.km.suspend(token=2)            # prompt B armed ...
        self.km.resume(token=1)             # ... before A's end arrives
        self.assertTrue(self.km.is_suspended())
        self.km.resume(token=2)
        self.assertFalse(self.km.is_suspended())

    def test_begin_arriving_after_its_end_is_ignored(self):
        self.km.resume(token=5)
        self.assertFalse(self.km.suspend(token=5))
        self.assertFalse(self.km.is_suspended())


class CaptureTokenAtomicity(unittest.TestCase):
    """HK3: resume(A) passed its token check, suspend(B) installed B's
    suspension, then A wrote 0.0 -- B's prompt open with hotkeys live."""

    def test_resume_check_and_write_cannot_interleave_with_suspend(self):
        entered = threading.Event()
        release = threading.Event()

        class Probe(KeybindManager):
            # Pause resume(A) exactly between its token check and its
            # write of 0.0 -- the window the race needed.
            @property
            def _suspended_until(self):
                return self._su

            @_suspended_until.setter
            def _suspended_until(self, value):
                if (value == 0.0 and
                        threading.current_thread().name == 'resume-A'):
                    entered.set()
                    release.wait(2.0)
                self._su = value

        km = Probe(lambda: 0)
        km.suspend(token=1)
        a = threading.Thread(target=km.resume, kwargs={'token': 1},
                             name='resume-A', daemon=True)
        a.start()
        self.assertTrue(entered.wait(2.0))
        b = threading.Thread(target=km.suspend, kwargs={'token': 2},
                             name='suspend-B', daemon=True)
        b.start()
        b.join(0.2)
        self.assertTrue(b.is_alive(),
                        'suspend(B) ran inside resume(A)\'s check-and-write')
        release.set()
        a.join(2.0)
        b.join(2.0)
        self.assertTrue(km.is_suspended(),
                        "B's prompt is open but hotkeys are live")

    def test_gate_reads_without_the_capture_lock(self):
        """The hook path must never wait on a bridge thread."""
        km = KeybindManager(lambda: 0)
        wrapper = km._gate(lambda: None, 'app', 'x')
        with km._capture_lock, mock.patch.object(
                ui_core, 'is_allowed_foreground', return_value=True):
            t0 = time.perf_counter()
            wrapper()
            self.assertLess(time.perf_counter() - t0, 0.05)


class LostModifierRelease(_Harness):
    """HK5: a Ctrl release swallowed by the secure desktop left Ctrl
    'held' forever, so plain F8 kept resolving to 'ctrl+f8'."""

    def setUp(self):
        super().setUp()
        self.bind('start_stop', 'f8')
        self.bind('pause', 'ctrl+f8')

    def test_released_per_windows_is_dropped(self):
        self.async_down.return_value = False      # Windows: Ctrl is up
        self.down('ctrl', 100.0)                  # its KEY_UP never came
        self.down('f8', 400.0)
        self.assertEqual(self.settle(), ['start_stop'])

    def test_legit_long_hold_is_kept(self):
        self.async_down.return_value = True       # really still held
        self.down('ctrl', 100.0)
        self.down('f8', 400.0)                    # 5 minutes later
        self.assertEqual(self.settle(), ['pause'])

    def test_recent_modifier_trusts_the_event_stream(self):
        """Ctrl went down 0.3 s before F8: its KEY_UP may still be queued
        behind F8 (delivery lag), so Windows' 'up' is not proof."""
        self.async_down.return_value = False
        self.down('ctrl', 100.0)
        self.down('f8', 100.3)
        self.assertEqual(self.settle(), ['pause'])

    def test_unknown_windows_state_keeps_modifier(self):
        self.async_down.return_value = None
        self.down('ctrl', 100.0)
        self.down('f8', 400.0)
        self.assertEqual(self.settle(), ['pause'])

    def test_windows_is_asked_only_on_the_candidate_path(self):
        self.bind('debug', 'f10')                 # bare-only candidate
        self.async_down.reset_mock()              # forget the bind-time prime
        self.down('ctrl', 100.0)
        self.down('q', 400.0)                     # unbound key
        self.down('f10', 401.0)                   # no modifier decision
        self.up('q', 401.5)
        self.async_down.assert_not_called()
        self.down('f8', 402.0)                    # candidate path
        calls = self.async_down.call_count
        self.assertGreater(calls, 0)
        self.down('f8', 402.03)                   # auto-repeat: no re-check
        self.assertEqual(self.async_down.call_count, calls)


class SetKeepsOldBindingOnInstallFailure(_Harness):
    """HK4 (manager side): a transient hook-install failure must not
    remove the binding that was working."""

    def test_mouse_install_failure_keeps_previous(self):
        def on_button(cb, buttons, types):
            if buttons == ['right']:
                raise RuntimeError('hook failed')
            return ('handle', tuple(buttons))
        with mock.patch.object(ui_core.mouse, 'on_button',
                               side_effect=on_button), \
                mock.patch.object(ui_core.mouse, 'unhook') as unhook:
            self.assertTrue(self.bind('macro_play', 'mouse:x'))
            self.assertFalse(self.bind('macro_play', 'mouse:right'))
        unhook.assert_not_called()
        self.assertEqual(self.km.get_bindings()['macro_play'], 'mouse:x')
        self.assertIn('macro_play', self.km._mouse_hooks)

    def test_keyboard_hook_failure_keeps_previous(self):
        with mock.patch.object(ui_core.mouse, 'on_button',
                               return_value='handle'), \
                mock.patch.object(ui_core.mouse, 'unhook'):
            self.assertTrue(self.bind('macro_play', 'mouse:x'))
        with mock.patch.object(ui_core.keyboard, 'hook',
                               side_effect=RuntimeError('hook failed')):
            self.assertFalse(self.bind('macro_play', 'f6'))
        self.assertEqual(self.km.get_bindings()['macro_play'], 'mouse:x')


@unittest.skipIf(ui_mod is None, 'ui.py not importable here')
class BridgeRebindValidatesBeforeSaving(unittest.TestCase):
    """BridgeApi.rebind / macro_set_active_hotkey must refuse BEFORE the
    settings change, so the old binding stays saved and installed."""

    def setUp(self):
        p = mock.patch.object(ui_core.keyboard, 'hook',
                              return_value='kb-hook')
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ui_mod, 'save_settings')
        self.save = p.start()
        self.addCleanup(p.stop)
        # No real WH_MOUSE_LL hook in tests; buttons listed in
        # `fail_buttons` simulate a transient install failure.
        self.fail_buttons = set()

        def on_button(cb, buttons, types):
            if set(buttons) & self.fail_buttons:
                raise RuntimeError('hook install failed')
            return ('mouse-handle', tuple(buttons))
        for attr, kwargs in (('on_button', {'side_effect': on_button}),
                             ('unhook', {})):
            p = mock.patch.object(ui_core.mouse, attr, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        api = mock.MagicMock()
        api._lock = threading.Lock()
        api._settings = {
            'keybinds': {'start_stop': 'f8', 'pause': 'f9', 'debug': 'f10',
                         'macro_record': 'y', 'macro_play': 'mouse:x',
                         'macro_stop': 'mouse:x2', 'macro_save': 'u',
                         'macro_load': 'f11'},
            'macro_active_macros': [
                {'slot': 3, 'hotkey': 'mouse:middle'},
                {'slot': 0, 'hotkey': ''},
                {'slot': 0, 'hotkey': ''},
            ],
        }
        api._keybinds = KeybindManager(lambda: 0)
        bridge = ui_mod.BridgeApi
        for name in ('_hotkey_in_use_locked', '_active_macros_locked',
                     '_active_macro_index'):
            setattr(api, name, functools.partial(getattr(bridge, name), api))
        api._capture_token = bridge._capture_token

        def install():                     # mirrors BridgeApi._install_keybinds
            api._keybinds.clear_all()
            for action, hk in api._settings['keybinds'].items():
                api._keybinds.set(action, hk, lambda: None)
            for pos, item in enumerate(api._settings['macro_active_macros'],
                                       start=1):
                if item.get('slot') and item.get('hotkey'):
                    api._keybinds.set(f'macro_active_{pos}', item['hotkey'],
                                      lambda: None, scope='game')
        api._install_keybinds.side_effect = install
        install()
        self.api = api
        self.bridge = bridge

    def rebind(self, action, payload):
        return self.bridge.rebind(self.api, action, payload)

    def test_left_click_refused(self):
        res = self.rebind('macro_play', {'mouseButton': 'left'})
        self.assertEqual(res, {'ok': False, 'reason': 'left_click_reserved'})
        self.assertEqual(self.api._settings['keybinds']['macro_play'],
                         'mouse:x')
        self.save.assert_not_called()

    def test_dead_key_refused_and_old_binding_kept(self):
        res = self.rebind('start_stop', {'key': 'Dead'})
        self.assertEqual(res, {'ok': False, 'reason': 'unsupported_key'})
        self.assertEqual(self.api._settings['keybinds']['start_stop'], 'f8')
        self.assertEqual(self.api._keybinds.get_bindings()['start_stop'], 'f8')
        self.save.assert_not_called()

    def test_rhythm_key_refused_for_bot_action(self):
        res = self.rebind('pause', {'key': 'a'})
        self.assertEqual(res, {'ok': False, 'reason': 'rhythm_key'})
        self.save.assert_not_called()

    def test_valid_rebind_saves_canonical_form(self):
        res = self.rebind('start_stop', {'key': 'F7', 'ctrlKey': True})
        self.assertEqual(res, 'ctrl+f7')
        self.assertEqual(self.api._settings['keybinds']['start_stop'],
                         'ctrl+f7')
        self.save.assert_called_once()

    def test_duplicate_detected_across_spellings(self):
        self.api._settings['keybinds']['macro_load'] = 'escape'
        res = self.rebind('macro_save', {'key': 'Escape'})
        self.assertEqual(res, {'ok': False, 'reason': 'duplicate_hotkey'})

    def test_active_macro_hotkey_refuses_left_click(self):
        res = self.bridge.macro_set_active_hotkey(
            self.api, 1, {'mouseButton': 'left'})
        self.assertEqual(res, {'ok': False, 'reason': 'left_click_reserved'})
        self.save.assert_not_called()

    # HK4: install + verify before saving; restore on failure.

    def test_rebind_install_failure_restores_previous(self):
        self.fail_buttons = {'right'}
        res = self.rebind('macro_play', {'mouseButton': 'right'})
        self.assertEqual(res, {'ok': False, 'reason': 'install_failed'})
        self.assertEqual(self.api._settings['keybinds']['macro_play'],
                         'mouse:x')
        self.assertEqual(self.api._keybinds.get_bindings()['macro_play'],
                         'mouse:x')
        self.save.assert_not_called()

    def test_active_hotkey_install_failure_restores_previous(self):
        self.fail_buttons = {'right'}
        res = self.bridge.macro_set_active_hotkey(
            self.api, 1, {'mouseButton': 'right'})
        self.assertEqual(res, {'ok': False, 'reason': 'install_failed'})
        self.assertEqual(
            self.api._settings['macro_active_macros'][0]['hotkey'],
            'mouse:middle')
        self.assertEqual(
            self.api._keybinds.get_bindings()['macro_active_1'],
            'mouse:middle')
        self.save.assert_not_called()

    def test_active_hotkey_success_installs_then_saves(self):
        res = self.bridge.macro_set_active_hotkey(self.api, 1, {'key': 'F6'})
        self.assertEqual(res, {'ok': True, 'hotkey': 'f6'})
        self.assertEqual(
            self.api._keybinds.get_bindings()['macro_active_1'], 'f6')
        self.save.assert_called_once()

    def test_active_hotkey_without_slot_is_saved_uninstalled(self):
        """Only slotted entries are installed; an unslotted one has nothing
        to verify and is just saved."""
        res = self.bridge.macro_set_active_hotkey(self.api, 2, {'key': 'F6'})
        self.assertEqual(res, {'ok': True, 'hotkey': 'f6'})
        self.assertNotIn('macro_active_2', self.api._keybinds.get_bindings())
        self.save.assert_called_once()

    def test_capture_bridge_methods_drive_suspension(self):
        self.assertTrue(self.bridge.capture_begin(self.api, 10))
        self.assertTrue(self.api._keybinds.is_suspended())
        self.assertTrue(self.bridge.capture_end(self.api, 10))
        self.assertFalse(self.api._keybinds.is_suspended())


_NODE = shutil.which('node')


@unittest.skipIf(_NODE is None, 'node not on PATH')
class CaptureGuardJs(unittest.TestCase):
    """HK1 / HK2: runs tests/capture_guard.test.js, which loads the real
    app.js in a vm with a fake DOM + bridge and drives the capture flow."""

    def test_capture_guard_js(self):
        script = Path(__file__).with_name('capture_guard.test.js')
        r = subprocess.run([_NODE, str(script)], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
