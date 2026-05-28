from collections import deque

from virtual_tcu.learning.power_curve import W_PER_HP
from virtual_tcu.telemetry.model import Telemetry


class RevLimiterDetector:
    """Learns the real usable rev ceiling per car.

    Two signals are used:
    - limiter bounce: RPM plateaus and oscillates at full throttle;
    - fuel-cut cliff: high-RPM full-throttle power suddenly collapses.

    The second path fixes the previous self-blocking behavior where automatic
    upshifts protected the engine before the limiter learner could ever mature.
    """

    MIN_THROTTLE = 0.86
    POST_SHIFT_IGNORE_S = 0.45
    WINDOW = 18
    STABLE_FRAMES = 10
    MIN_PEAK_PCT = 0.82
    PEAK_EPS = 55.0
    MIN_OSCILLATION = 120.0
    POWER_DROP_RATIO = 0.42
    POWER_DROP_FRAMES = 2

    def __init__(self):
        self._redline: dict[tuple, float] = {}
        self._rpm_window: dict[tuple, deque[float]] = {}
        self._peak_hold: dict[tuple, tuple] = {}
        self._high_power_peak: dict[tuple, float] = {}
        self._drop_streak: dict[tuple, int] = {}

    def _reset_transient(self, car: tuple):
        self._rpm_window.pop(car, None)
        self._peak_hold.pop(car, None)
        self._drop_streak.pop(car, None)

    @staticmethod
    def _power_hp(td: Telemetry) -> float:
        if abs(td.power_w) > 1.0:
            return max(0.0, td.power_w / W_PER_HP)
        if td.torque_nm > 0 and td.current_rpm > 0:
            return td.torque_nm * td.current_rpm / 7127.0
        return 0.0

    def observe(self, td: Telemetry, last_downshift_time: float, now: float):
        car = td.car_key
        if (
            car[0] <= 0
            or td.is_shifting
            or td.engine_max_rpm <= 0
            or td.throttle < self.MIN_THROTTLE
            or now - last_downshift_time < self.POST_SHIFT_IGNORE_S
            or td.max_combined_slip > 1.2
            or not td.is_grounded
            or td.any_puddle
        ):
            self._reset_transient(car)
            return

        rpm = td.current_rpm
        high_rpm = rpm >= td.engine_max_rpm * self.MIN_PEAK_PCT
        if not high_rpm:
            self._drop_streak.pop(car, None)
            return

        self._observe_power_cliff(car, td)
        self._observe_limiter_bounce(car, td)

    def _observe_power_cliff(self, car: tuple, td: Telemetry):
        power_hp = self._power_hp(td)
        if power_hp <= 0:
            return

        current_peak = self._high_power_peak.get(car, 0.0)
        if power_hp > current_peak:
            self._high_power_peak[car] = power_hp
            self._drop_streak.pop(car, None)
            return

        # Once high-rpm power has been observed, a sharp full-throttle drop is
        # usually fuel cut. Normal post-peak roll-off is much gentler.
        if current_peak >= 50.0 and power_hp <= current_peak * self.POWER_DROP_RATIO:
            streak = self._drop_streak.get(car, 0) + 1
            self._drop_streak[car] = streak
            if streak >= self.POWER_DROP_FRAMES:
                self._learn(car, td.current_rpm)
        else:
            self._drop_streak.pop(car, None)

    def _observe_limiter_bounce(self, car: tuple, td: Telemetry):
        win = self._rpm_window.setdefault(car, deque(maxlen=self.WINDOW))
        win.append(td.current_rpm)
        if len(win) < self.WINDOW:
            return

        wmax, wmin = max(win), min(win)
        if wmax < td.engine_max_rpm * self.MIN_PEAK_PCT or (wmax - wmin) < self.MIN_OSCILLATION:
            self._peak_hold.pop(car, None)
            return

        held_peak, held_frames = self._peak_hold.get(car, (wmax, 0))
        if abs(wmax - held_peak) <= self.PEAK_EPS:
            held_peak = max(held_peak, wmax)
            held_frames += 1
        else:
            held_peak, held_frames = wmax, 0
        self._peak_hold[car] = (held_peak, held_frames)

        if held_frames >= self.STABLE_FRAMES:
            self._learn(car, held_peak)

    def _learn(self, car: tuple, redline: float):
        if redline <= 0:
            return
        current = self._redline.get(car)
        if current is None:
            self._redline[car] = float(redline)
            return
        # Ignore one-frame low estimates, but allow the ceiling to move in
        # both directions as stronger evidence arrives.
        if redline < current * 0.92:
            return
        if redline > current:
            self._redline[car] = current * 0.80 + float(redline) * 0.20
        else:
            self._redline[car] = current * 0.65 + float(redline) * 0.35

    def effective_redline(self, td: Telemetry) -> float | None:
        return self._redline.get(td.car_key)

    def dump(self, car: tuple) -> float | None:
        return self._redline.get(car)

    def load(self, car: tuple, redline: float):
        if isinstance(redline, (int, float)) and redline > 0:
            self._redline[car] = float(redline)
