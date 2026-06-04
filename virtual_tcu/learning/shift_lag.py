"""Adaptive shift-lag learner.

Measures the delay between a TCU command and the telemetry gear change so
predictive upshift compensation can leave enough room before the limiter.
"""

from collections import deque


class ShiftLagLearner:
    """Learns per-car shift execution latency."""

    MAX_SAMPLES = 15
    VALID_LAG_RANGE = (0.020, 0.350)
    DEFAULT_UPSHIFT_LAG = 0.090
    DEFAULT_DOWNSHIFT_LAG = 0.035
    MIN_UNRESPONSIVE_UPSHIFT_LAG = 0.080
    MAX_POST_COMMAND_RPM_GAIN = 220.0
    MIN_MEANINGFUL_RPM_GAIN = 60.0
    MAX_MEANINGFUL_RPM_GAIN = 500.0

    def __init__(self):
        self._upshift_lags: dict[tuple, deque[float]] = {}
        self._downshift_lags: dict[tuple, deque[float]] = {}
        self._upshift_rpm_gains: dict[tuple[tuple, int], deque[float]] = {}
        self._last_shift_command_time: float | None = None
        self._last_shift_command_gear: int | None = None
        self._last_shift_direction: str | None = None
        self._last_shift_command_car_key: tuple | None = None
        self._last_shift_command_rpm: float | None = None
        self._last_shift_command_peak_rpm: float | None = None

    def record_shift_command(
        self,
        car_key: tuple,
        direction: str,
        gear: int,
        now: float,
        *,
        command_rpm: float | None = None,
    ):
        """Record when a shift command was sent."""
        self._last_shift_command_time = now
        self._last_shift_command_gear = gear
        self._last_shift_direction = direction
        self._last_shift_command_car_key = car_key
        self._last_shift_command_rpm = command_rpm if command_rpm and command_rpm > 0 else None
        self._last_shift_command_peak_rpm = self._last_shift_command_rpm

    def _clear_last_shift_command(self):
        self._last_shift_command_time = None
        self._last_shift_command_gear = None
        self._last_shift_direction = None
        self._last_shift_command_car_key = None
        self._last_shift_command_rpm = None
        self._last_shift_command_peak_rpm = None

    def observe_command_frame(self, car_key: tuple, gear: int, rpm: float):
        """Track same-gear RPM after a command to reject auto-mode drag samples."""
        if (
            self._last_shift_command_time is None
            or self._last_shift_direction != "UP"
            or self._last_shift_command_car_key != car_key
            or self._last_shift_command_gear != gear
            or rpm <= 0
        ):
            return
        peak = self._last_shift_command_peak_rpm or rpm
        self._last_shift_command_peak_rpm = max(peak, rpm)

    def observe_gear_change(self, car_key: tuple, new_gear: int, now: float):
        """Observe a telemetry gear change and store a valid latency sample."""
        if self._last_shift_command_time is None:
            return
        if (
            self._last_shift_command_car_key is not None
            and self._last_shift_command_car_key != car_key
        ):
            self._clear_last_shift_command()
            return

        expected_gear = None
        if self._last_shift_direction == "UP":
            expected_gear = self._last_shift_command_gear + 1
        elif self._last_shift_direction == "DOWN":
            expected_gear = self._last_shift_command_gear - 1

        if expected_gear != new_gear:
            return

        lag = now - self._last_shift_command_time
        direction = self._last_shift_direction
        command_gear = self._last_shift_command_gear
        command_rpm = self._last_shift_command_rpm
        peak_rpm = self._last_shift_command_peak_rpm
        self._clear_last_shift_command()

        if not (self.VALID_LAG_RANGE[0] <= lag <= self.VALID_LAG_RANGE[1]):
            return
        if (
            direction == "UP"
            and lag >= self.MIN_UNRESPONSIVE_UPSHIFT_LAG
            and command_rpm is not None
            and peak_rpm is not None
            and peak_rpm - command_rpm > self.MAX_POST_COMMAND_RPM_GAIN
        ):
            return

        if direction == "UP":
            samples = self._upshift_lags.setdefault(car_key, deque(maxlen=self.MAX_SAMPLES))
        else:
            samples = self._downshift_lags.setdefault(car_key, deque(maxlen=self.MAX_SAMPLES))

        samples.append(lag)
        if (
            direction == "UP"
            and command_gear is not None
            and command_rpm is not None
            and peak_rpm is not None
        ):
            rpm_gain = max(0.0, peak_rpm - command_rpm)
            if self.MIN_MEANINGFUL_RPM_GAIN <= rpm_gain <= self.MAX_MEANINGFUL_RPM_GAIN:
                gain_samples = self._upshift_rpm_gains.setdefault(
                    (car_key, command_gear),
                    deque(maxlen=self.MAX_SAMPLES),
                )
                gain_samples.append(rpm_gain)

    def get_upshift_lag(self, car_key: tuple) -> float:
        """Return learned upshift latency in seconds."""
        samples = self._upshift_lags.get(car_key, deque())
        if len(samples) < 3:
            return self.DEFAULT_UPSHIFT_LAG

        sorted_samples = sorted(samples)
        idx = min(len(sorted_samples) - 1, round((len(sorted_samples) - 1) * 0.30))
        return sorted_samples[idx]

    def get_downshift_lag(self, car_key: tuple) -> float:
        """Return learned downshift latency in seconds."""
        samples = self._downshift_lags.get(car_key, deque())
        if len(samples) < 3:
            return self.DEFAULT_DOWNSHIFT_LAG

        sorted_samples = sorted(samples)
        mid = len(sorted_samples) // 2
        return sorted_samples[mid]

    def get_upshift_rpm_gain(self, car_key: tuple, gear: int) -> float | None:
        """Return learned same-gear RPM gain after an upshift command."""
        samples = self._upshift_rpm_gains.get((car_key, gear), deque())
        if not samples:
            return None

        sorted_samples = sorted(samples)
        idx = min(len(sorted_samples) - 1, round((len(sorted_samples) - 1) * 0.70))
        return sorted_samples[idx]

    def dump(self, car_key: tuple) -> dict | None:
        """Export persisted learning data."""
        up_samples = self._upshift_lags.get(car_key)
        down_samples = self._downshift_lags.get(car_key)
        up_gain_samples = {
            gear: list(samples)
            for (gain_car_key, gear), samples in sorted(
                self._upshift_rpm_gains.items(),
                key=lambda item: item[0][1],
            )
            if gain_car_key == car_key and samples
        }

        if not up_samples and not down_samples and not up_gain_samples:
            return None

        data = {
            "upshift_lags": list(up_samples) if up_samples else [],
            "downshift_lags": list(down_samples) if down_samples else [],
        }
        if up_gain_samples:
            data["upshift_rpm_gains_by_gear"] = {
                str(gear): samples for gear, samples in up_gain_samples.items()
            }
        return data

    def load(self, car_key: tuple, data: dict):
        """Restore persisted learning data."""
        if not isinstance(data, dict):
            return

        up_lags = data.get("upshift_lags", [])
        down_lags = data.get("downshift_lags", [])
        up_gain_samples = data.get("upshift_rpm_gains_by_gear", {})

        if up_lags:
            valid_up_lags = [
                lag
                for lag in up_lags
                if isinstance(lag, (int, float))
                and self.VALID_LAG_RANGE[0] <= lag <= self.VALID_LAG_RANGE[1]
            ]
            if valid_up_lags:
                self._upshift_lags[car_key] = deque(valid_up_lags, maxlen=self.MAX_SAMPLES)
        if down_lags:
            valid_down_lags = [
                lag
                for lag in down_lags
                if isinstance(lag, (int, float))
                and self.VALID_LAG_RANGE[0] <= lag <= self.VALID_LAG_RANGE[1]
            ]
            if valid_down_lags:
                self._downshift_lags[car_key] = deque(valid_down_lags, maxlen=self.MAX_SAMPLES)
        if isinstance(up_gain_samples, dict):
            for gear_key, gains in up_gain_samples.items():
                try:
                    gear = int(gear_key)
                except (TypeError, ValueError):
                    continue
                if not isinstance(gains, list):
                    continue
                valid_gains = [
                    gain
                    for gain in gains
                    if isinstance(gain, (int, float))
                    and self.MIN_MEANINGFUL_RPM_GAIN
                    <= gain
                    <= self.MAX_MEANINGFUL_RPM_GAIN
                ]
                if valid_gains:
                    self._upshift_rpm_gains[(car_key, gear)] = deque(
                        valid_gains,
                        maxlen=self.MAX_SAMPLES,
                    )
