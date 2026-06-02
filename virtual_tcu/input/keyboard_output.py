import time
from concurrent.futures import ThreadPoolExecutor

import keyboard

from virtual_tcu.config.constants import Cfg
from virtual_tcu.config.store import ConfigStore
from virtual_tcu.input.interface import OutputInterface


class KeyboardOutput(OutputInterface):
    """Inject shift commands as keyboard key-presses (E / Q by default).

    When ``feat_clutch_assist`` is enabled, each shift press is wrapped in a
    configurable clutch-key sequence. This is intentionally contained here so
    the TCU learning and shift-decision logic can stay unchanged.
    """

    def __init__(self, config: ConfigStore):
        self._config = config
        self._self_press_until: dict[str, float] = {}
        self.SELF_PRESS_WINDOW_S = 0.15
        # Single worker ensures keystrokes are executed sequentially without thread leaks
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="KB_Worker")

    @property
    def key_up(self) -> str:
        return str(self._config.get("shift_key_up", "e")).lower()

    @property
    def key_down(self) -> str:
        return str(self._config.get("shift_key_down", "q")).lower()

    @property
    def clutch_key(self) -> str:
        return str(self._config.get("clutch_key", "shift")).lower()

    @property
    def use_clutch(self) -> bool:
        return bool(self._config.get("feat_clutch_assist", False))

    @property
    def clutch_pre_ms(self) -> int:
        return int(self._config.get("clutch_pre_ms", 20))

    @property
    def clutch_overlap_ms(self) -> int:
        return int(self._config.get("clutch_overlap_ms", 55))

    @property
    def clutch_release_ms(self) -> int:
        return int(self._config.get("clutch_release_ms", 25))

    def is_self_press(self, key: str) -> bool:
        return time.time() < self._self_press_until.get(key.lower(), 0.0)

    def _press_release(self, key: str):
        try:
            key = key.lower()
            self._self_press_until[key] = time.time() + self.SELF_PRESS_WINDOW_S
            keyboard.press(key)
            time.sleep(Cfg.KEY_HOLD_S)
            keyboard.release(key)
        except Exception as e:
            print(f"[KB] Input simulation failed: {e}")

    def _press_release_with_clutch(self, key: str):
        ck = self.clutch_key
        k = key.lower()
        pressed_ck = False
        pressed_k = False
        try:
            pre_s = max(0, self.clutch_pre_ms) / 1000.0
            overlap_s = max(1, self.clutch_overlap_ms) / 1000.0
            release_s = max(0, self.clutch_release_ms) / 1000.0
            deadline = time.time() + pre_s + overlap_s + release_s + self.SELF_PRESS_WINDOW_S
            self._self_press_until[k] = deadline
            self._self_press_until[ck] = deadline

            keyboard.press(ck)
            pressed_ck = True
            time.sleep(pre_s)
            keyboard.press(k)
            pressed_k = True
            time.sleep(overlap_s)
            keyboard.release(k)
            pressed_k = False
            time.sleep(release_s)
            keyboard.release(ck)
            pressed_ck = False
        except Exception as e:
            print(f"[KB] Clutch-assisted shift failed: {e}")
        finally:
            try:
                if pressed_k:
                    keyboard.release(k)
                if pressed_ck:
                    keyboard.release(ck)
            except Exception:
                pass

    def _shift_key(self, key: str):
        if self.use_clutch:
            self._press_release_with_clutch(key)
        else:
            self._press_release(key)

    def shift_up(self):
        self._executor.submit(self._shift_key, self.key_up)

    def shift_down(self):
        self._executor.submit(self._shift_key, self.key_down)

    def shift_down_double(self):
        def _double():
            self._shift_key(self.key_down)
            time.sleep(0.06)
            self._shift_key(self.key_down)

        self._executor.submit(_double)

    def shutdown(self):
        self._executor.shutdown(wait=False)
