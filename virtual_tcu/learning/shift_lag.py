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

    def __init__(self):
        self._upshift_lags: dict[tuple, deque[float]] = {}
        self._downshift_lags: dict[tuple, deque[float]] = {}
        self._last_shift_command_time: float | None = None
        self._last_shift_command_gear: int | None = None
        self._last_shift_direction: str | None = None
        self._last_shift_command_car_key: tuple | None = None

    def record_shift_command(self, car_key: tuple, direction: str, gear: int, now: float):
        """Record when a shift command was sent."""
        self._last_shift_command_time = now
        self._last_shift_command_gear = gear
        self._last_shift_direction = direction
        self._last_shift_command_car_key = car_key

    def _clear_last_shift_command(self):
        self._last_shift_command_time = None
        self._last_shift_command_gear = None
        self._last_shift_direction = None
        self._last_shift_command_car_key = None

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
        self._clear_last_shift_command()

        if not (self.VALID_LAG_RANGE[0] <= lag <= self.VALID_LAG_RANGE[1]):
            return

        if direction == "UP":
            samples = self._upshift_lags.setdefault(car_key, deque(maxlen=self.MAX_SAMPLES))
        else:
            samples = self._downshift_lags.setdefault(car_key, deque(maxlen=self.MAX_SAMPLES))

        samples.append(lag)

    def get_upshift_lag(self, car_key: tuple) -> float:
        """Return learned upshift latency in seconds."""
        samples = self._upshift_lags.get(car_key, deque())
        if len(samples) < 3:
            return self.DEFAULT_UPSHIFT_LAG

        sorted_samples = sorted(samples)
        idx = min(len(sorted_samples) - 1, round((len(sorted_samples) - 1) * 0.70))
        return sorted_samples[idx]

    def get_downshift_lag(self, car_key: tuple) -> float:
        """Return learned downshift latency in seconds."""
        samples = self._downshift_lags.get(car_key, deque())
        if len(samples) < 3:
            return self.DEFAULT_DOWNSHIFT_LAG

        sorted_samples = sorted(samples)
        mid = len(sorted_samples) // 2
        return sorted_samples[mid]

    def dump(self, car_key: tuple) -> dict | None:
        """Export persisted learning data."""
        up_samples = self._upshift_lags.get(car_key)
        down_samples = self._downshift_lags.get(car_key)

        if not up_samples and not down_samples:
            return None

        return {
            "upshift_lags": list(up_samples) if up_samples else [],
            "downshift_lags": list(down_samples) if down_samples else [],
        }

    def load(self, car_key: tuple, data: dict):
        """Restore persisted learning data."""
        if not isinstance(data, dict):
            return

        up_lags = data.get("upshift_lags", [])
        down_lags = data.get("downshift_lags", [])

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
