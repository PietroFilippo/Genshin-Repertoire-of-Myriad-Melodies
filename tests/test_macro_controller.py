import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc_client'))

import ui_core  # noqa: E402


class BlockingCleanupController:
    def __init__(self):
        self.calls = []
        self.cleanup_started = threading.Event()
        self.allow_cleanup = threading.Event()
        self._block_once = True

    def key_down(self, key):
        self.calls.append(('key_down', key))

    def key_up(self, key):
        if key == 'x' and self._block_once:
            self._block_once = False
            self.cleanup_started.set()
            self.allow_cleanup.wait(timeout=2.0)
        self.calls.append(('key_up', key))

    def mouse_down(self, button):
        self.calls.append(('mouse_down', button))

    def mouse_up(self, button):
        self.calls.append(('mouse_up', button))


class MacroControllerConcurrencyTests(unittest.TestCase):
    def test_stop_after_active_interrupt_targets_new_playback(self):
        with tempfile.TemporaryDirectory() as td:
            macro_dir = Path(td)
            original_macros_dir = ui_core.macros_dir
            ui_core.macros_dir = lambda: macro_dir
            try:
                self._write_slot(macro_dir, 1, [
                    self._ev(0.0, 'keyboard', 'x', 'down'),
                    self._ev(5.0, 'keyboard', 'x', 'up'),
                ])
                self._write_slot(macro_dir, 2, [
                    self._ev(0.0, 'keyboard', 'y', 'down'),
                    self._ev(5.0, 'keyboard', 'y', 'up'),
                ])

                controller = BlockingCleanupController()
                macro = ui_core.MacroController(
                    controller_provider=lambda: controller,
                    on_status=lambda _payload: None,
                )

                self.assertTrue(macro.play_slot(1))
                self._wait_for_call(controller, ('key_down', 'x'))

                result = {}

                def play_second():
                    result['ok'] = macro.play_slot(
                        2, conflict_policy=ui_core.MACRO_CONFLICT_INTERRUPT)

                t = threading.Thread(target=play_second)
                t.start()
                self.assertTrue(controller.cleanup_started.wait(timeout=1.0))

                stop_result = {}

                def stop_playback():
                    stop_result['ok'] = macro.stop_play()

                stop_thread = threading.Thread(target=stop_playback)
                stop_thread.start()
                controller.allow_cleanup.set()
                t.join(timeout=1.0)
                stop_thread.join(timeout=1.0)
                self.assertFalse(t.is_alive())
                self.assertFalse(stop_thread.is_alive())
                self.assertTrue(result.get('ok'))
                self.assertTrue(stop_result.get('ok'))

                deadline = time.time() + 1.0
                while time.time() < deadline:
                    if macro.state() == ui_core.MacroController.STATE_IDLE:
                        break
                    time.sleep(0.01)

                self.assertEqual(ui_core.MacroController.STATE_IDLE,
                                 macro.state())
            finally:
                ui_core.macros_dir = original_macros_dir

    def test_repeated_same_active_trigger_does_not_restart_after_finish(self):
        with tempfile.TemporaryDirectory() as td:
            macro_dir = Path(td)
            original_macros_dir = ui_core.macros_dir
            ui_core.macros_dir = lambda: macro_dir
            try:
                self._write_slot(macro_dir, 1, [
                    self._ev(0.0, 'keyboard', 'z', 'down'),
                    self._ev(0.05, 'keyboard', 'z', 'up'),
                ])

                controller = BlockingCleanupController()
                macro = ui_core.MacroController(
                    controller_provider=lambda: controller,
                    on_status=lambda _payload: None,
                )

                self.assertTrue(macro.play_slot(1))
                self._wait_for_call(controller, ('key_down', 'z'))
                time.sleep(0.03)
                self.assertFalse(macro.play_slot(
                    1, conflict_policy=ui_core.MACRO_CONFLICT_INTERRUPT))
                self._wait_for_state(macro, ui_core.MacroController.STATE_IDLE)

                self.assertFalse(macro.play_slot(
                    1, conflict_policy=ui_core.MACRO_CONFLICT_INTERRUPT))
                self.assertEqual(1, self._count_call(
                    controller, ('key_down', 'z')))

                time.sleep(macro.TRIGGER_DEBOUNCE_S + 0.05)
                self.assertTrue(macro.play_slot(1))
                self._wait_for_count(controller, ('key_down', 'z'), 2)
            finally:
                ui_core.macros_dir = original_macros_dir

    @staticmethod
    def _ev(t, device, key, event_type):
        return {
            'time': t,
            'device': device,
            'key': key,
            'event_type': event_type,
        }

    @staticmethod
    def _write_slot(macro_dir, n, events):
        path = macro_dir / f'macro_{n}.json'
        with path.open('w', encoding='utf-8') as f:
            json.dump({'name': f'slot {n}', 'events': events}, f)

    @staticmethod
    def _count_call(controller, expected):
        return sum(1 for call in controller.calls if call == expected)

    @classmethod
    def _wait_for_count(cls, controller, expected, count):
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if cls._count_call(controller, expected) >= count:
                return
            time.sleep(0.01)
        raise AssertionError(f'{expected!r} was not called {count} times')

    @staticmethod
    def _wait_for_state(macro, state):
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if macro.state() == state:
                return
            time.sleep(0.01)
        raise AssertionError(f'macro did not reach state {state!r}')

    @staticmethod
    def _wait_for_call(controller, expected):
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if expected in controller.calls:
                return
            time.sleep(0.01)
        raise AssertionError(f'{expected!r} was not called')


if __name__ == '__main__':
    unittest.main()
