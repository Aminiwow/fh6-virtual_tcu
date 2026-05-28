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
    STABLE_FRAMES = 6
    MIN_PEAK_PCT = 0.84
    MIN_POWER_CLIFF_PCT = 0.84
    MAX_SURFACE_RUMBLE = 0.20
    PEAK_EPS = 55.0
    MIN_OSCILLATION = 120.0
    POWER_DROP_RATIO = 0.78
    POWER_DROP_FRAMES = 3
    MAX_RPM_GROWTH = 90.0

    def __init__(self):
        self._redline: dict[tuple, float] = {}
        self._rpm_window: dict[tuple, deque[float]] = {}
        self._peak_hold: dict[tuple, tuple] = {}
        self._high_power_peak: dict[tuple, float] = {}
        self._high_rpm_seen: dict[tuple, float] = {}
        self._drop_streak: dict[tuple, int] = {}
        self._lower_candidate: dict[tuple, tuple[float, int]] = {}

    def _reset_transient(self, car: tuple):
        self._rpm_window.pop(car, None)
        self._peak_hold.pop(car, None)
        self._drop_streak.pop(car, None)
        self._lower_candidate.pop(car, None)

    def reset_car(self, car: tuple):
        self._redline.pop(car, None)
        self._rpm_window.pop(car, None)
        self._peak_hold.pop(car, None)
        self._high_power_peak.pop(car, None)
        self._high_rpm_seen.pop(car, None)
        self._drop_streak.pop(car, None)
        self._lower_candidate.pop(car, None)

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
            or td.clutch_raw > 5
            or td.max_combined_slip > 1.2
            or td.max_surface_rumble > self.MAX_SURFACE_RUMBLE
            or not td.is_grounded
            or td.any_puddle
        ):
            self._reset_transient(car)
            return

        rpm = td.current_rpm
        high_rpm = rpm >= td.engine_max_rpm * self.MIN_PEAK_PCT
        if not high_rpm:
            self._drop_streak.pop(car, None)
            self._high_rpm_seen.pop(car, None)
            self._lower_candidate.pop(car, None)
            return

        self._high_rpm_seen[car] = max(self._high_rpm_seen.get(car, 0.0), rpm)
        if rpm >= td.engine_max_rpm * self.MIN_POWER_CLIFF_PCT:
            self._observe_power_cliff(car, td)
        self._observe_limiter_bounce(car, td)

    def _observe_power_cliff(self, car: tuple, td: Telemetry):
        has_raw_power = abs(td.power_w) > 1.0
        raw_power_hp = td.power_w / W_PER_HP if has_raw_power else None
        power_hp = self._power_hp(td)
        current_peak = self._high_power_peak.get(car, 0.0)
        if power_hp <= 0 and current_peak <= 0:
            return

        if power_hp > current_peak:
            self._high_power_peak[car] = power_hp
            self._drop_streak.pop(car, None)
            return

        # Once high-rpm power has been observed, a sharp full-throttle drop is
        # usually fuel cut. FH6 often reports negative torque while the engine
        # bounces below nominal max RPM, so learn from the first clean drop.
        hard_cut = has_raw_power and raw_power_hp is not None and raw_power_hp <= 5.0
        soft_cliff = current_peak >= 50.0 and power_hp <= current_peak * self.POWER_DROP_RATIO
        if current_peak >= 50.0 and (hard_cut or soft_cliff):
            streak = self._drop_streak.get(car, 0) + 1
            self._drop_streak[car] = streak
            needed = 1 if hard_cut else self.POWER_DROP_FRAMES
            if streak >= needed:
                self._learn(car, max(td.current_rpm, self._high_rpm_seen.get(car, 0.0)))
        else:
            self._drop_streak.pop(car, None)

    def _observe_limiter_bounce(self, car: tuple, td: Telemetry):
        win = self._rpm_window.setdefault(car, deque(maxlen=self.WINDOW))
        win.append(td.current_rpm)
        if len(win) < self.WINDOW:
            return

        wmax, wmin = max(win), min(win)
        rpm_growth = win[-1] - win[0]
        if (
            wmax < td.engine_max_rpm * self.MIN_PEAK_PCT
            or (wmax - wmin) < self.MIN_OSCILLATION
            or abs(rpm_growth) > self.MAX_RPM_GROWTH
        ):
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
        # Keep the learned value at the high edge of limiter contact. Lower
        # RPM negative-power frames are usually the bounce after fuel cut, not
        # the real shift target.
        lower_tolerance = 220.0
        if redline >= current - lower_tolerance:
            self._redline[car] = max(current, float(redline))
            self._lower_candidate.pop(car, None)
            return

        candidate, count = self._lower_candidate.get(car, (redline, 0))
        if abs(candidate - redline) <= lower_tolerance:
            count += 1
            candidate = max(candidate, redline)
        else:
            candidate, count = redline, 1
        self._lower_candidate[car] = (candidate, count)
        if count >= 6:
            self._redline[car] = float(candidate)
            self._lower_candidate.pop(car, None)

    def effective_redline(self, td: Telemetry) -> float | None:
        return self._redline.get(td.car_key)

    def dump(self, car: tuple) -> float | None:
        return self._redline.get(car)

    def load(self, car: tuple, redline: float):
        if isinstance(redline, (int, float)) and redline > 0:
            self._redline[car] = float(redline)
