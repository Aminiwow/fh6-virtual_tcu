"""Turbo characteristics learning - learns each car's turbo spool characteristics."""

from collections import deque
from statistics import median


class TurboCharacteristics:
    """Learn each car's turbo spool time and boost response characteristics.

    Uses learned spool times to adapt the boost threshold for upshift protection.
    Faster-spooling turbos need lower thresholds (0.65), slower ones need higher (0.85).
    """

    MIN_SAMPLES = 5
    MAX_SAMPLES = 30
    SPOOL_WINDOW = 20

    def __init__(self):
        # dict[car_key] -> deque of spool times (seconds)
        self._spool_times: dict[tuple, deque] = {}
        # dict[car_key] -> (threshold, confidence)
        self._cached_thresholds: dict[tuple, tuple[float, float]] = {}
        # Track last boost value to detect spool events
        self._last_boost: dict[tuple, float] = {}
        self._last_time: dict[tuple, float] = {}

    def observe_turbo_spool(
        self, car_key: tuple, boost_current: float, boost_target: float, time_now: float
    ):
        """Record a turbo spool event.

        Detects when boost is ramping up (turbo spooling) and measures the response time.

        Args:
            car_key: (ordinal, class, pi_rating)
            boost_current: Current turbo bar value (0.0-1.8)
            boost_target: Target boost (usually td.boost_raw, 0.0-1.8)
            time_now: Current timestamp (seconds)
        """
        # Initialize tracking for new cars
        if car_key not in self._last_boost:
            self._last_boost[car_key] = boost_current
            self._last_time[car_key] = time_now
            return

        last_boost = self._last_boost[car_key]
        last_time = self._last_time[car_key]

        # Detect spool event: significant boost increase + target still above current
        boost_delta = boost_current - last_boost
        time_delta = time_now - last_time

        # Only record if:
        # 1. Boost is increasing significantly (> 0.3 in this frame)
        # 2. We're still below target (turbo still spooling)
        # 3. Time delta is reasonable (avoid huge gaps)
        if (boost_delta > 0.3 and
            boost_current < boost_target * 0.95 and
            0.05 < time_delta < 1.0):

            # Calculate spool rate (boost per second)
            spool_rate = boost_delta / time_delta

            # Convert to approximate spool time (time to reach 1.0 boost from 0.0)
            # Higher rate = faster spool = lower time
            if spool_rate > 0.1:
                estimated_spool_time = 1.0 / spool_rate

                if car_key not in self._spool_times:
                    self._spool_times[car_key] = deque(maxlen=self.MAX_SAMPLES)

                self._spool_times[car_key].append(estimated_spool_time)
                # Invalidate cache when new sample added
                self._cached_thresholds.pop(car_key, None)

        # Update tracking
        self._last_boost[car_key] = boost_current
        self._last_time[car_key] = time_now

    def get_hold_threshold(self, car_key: tuple) -> float:
        """Get the adaptive boost threshold for this car.

        Returns:
            0.60-0.90: lower = faster response, higher = slower response
        """
        # Check cache first
        if car_key in self._cached_thresholds:
            threshold, _ = self._cached_thresholds[car_key]
            return threshold

        samples = self._spool_times.get(car_key)
        if not samples or len(samples) < self.MIN_SAMPLES:
            return 0.70  # Default for unknown cars

        # Use median to be robust to outliers
        avg_spool_time = median(samples)
        confidence = min(1.0, len(samples) / self.MAX_SAMPLES)

        # Map spool time to threshold
        # Fast response (< 0.3s) -> 0.65 (less hold time)
        # Medium (0.3-0.6s) -> 0.70-0.75
        # Slow (> 0.8s) -> 0.85 (more hold time)
        if avg_spool_time < 0.25:
            threshold = 0.62
        elif avg_spool_time < 0.35:
            threshold = 0.65
        elif avg_spool_time < 0.50:
            threshold = 0.70
        elif avg_spool_time < 0.70:
            threshold = 0.75
        elif avg_spool_time < 0.90:
            threshold = 0.82
        else:
            threshold = 0.88

        # Cache with confidence
        self._cached_thresholds[car_key] = (threshold, confidence)
        return threshold

    def confidence(self, car_key: tuple) -> float:
        """Get confidence level (0.0-1.0) for this car's threshold."""
        if car_key in self._cached_thresholds:
            _, conf = self._cached_thresholds[car_key]
            return conf

        samples = self._spool_times.get(car_key)
        if not samples:
            return 0.0

        return min(1.0, len(samples) / self.MAX_SAMPLES)

    def dump(self, car_key: tuple) -> dict | None:
        """Serialize turbo characteristics for storage."""
        samples = self._spool_times.get(car_key)
        if not samples:
            return None

        return {
            "format": "turbo_characteristics_v1",
            "spool_times": list(samples),
            "threshold": self.get_hold_threshold(car_key),
            "confidence": self.confidence(car_key),
        }

    def load(self, car_key: tuple, data: dict):
        """Deserialize turbo characteristics from storage."""
        if not isinstance(data, dict):
            return

        if data.get("format") != "turbo_characteristics_v1":
            return

        spool_times = data.get("spool_times")
        if isinstance(spool_times, list) and len(spool_times) > 0:
            self._spool_times[car_key] = deque(spool_times, maxlen=self.MAX_SAMPLES)
            # Clear cache to force recalculation
            self._cached_thresholds.pop(car_key, None)
