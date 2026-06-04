import math

from virtual_tcu.telemetry.model import Telemetry


class GearRatioCalibrator:
    """Learns engine/wheel gear ratio per car/gear from driven wheel speed.

    FH6 exposes wheel rotation speed, so use the physical ratio
    engine_rad_s / driven_wheel_rad_s instead of rpm/kmh. A rolling wheel
    radius is also learned so speed-based guards can still project RPM.
    """

    MIN_SPEED_KMH = 25.0
    OUTLIER_TOLERANCE = 0.18
    LEARN_RATE = 0.08
    OUTLIER_GRACE = 5
    MIN_DRIVEN_WHEEL_RAD_S = 5.0
    MAX_CLEAN_SLIP = 0.8
    MIN_SUSPENSION_NORM = 0.08
    MAX_SURFACE_RUMBLE = 0.20
    RATIO_MONOTONIC_TOLERANCE = 0.02
    PROFILE_BASIS = "engine_rad_per_driven_wheel_rad"

    def __init__(self):
        self._ratios: dict[tuple, dict[int, float]] = {}
        self._counts: dict[tuple, dict[int, int]] = {}
        self._wheel_radius: dict[tuple, float] = {}
        self._wheel_radius_counts: dict[tuple, int] = {}

    def observe(self, td: Telemetry):
        ck = td.car_key
        gear = td.gear
        if ck[0] <= 0 or gear < 1 or gear > 10:
            return
        if td.is_shifting:
            return
        if td.speed_kmh < self.MIN_SPEED_KMH or td.current_rpm <= 0:
            return
        if not self._is_clean_contact(td):
            return

        driven_rad_s = td.driven_wheel_speed_rad_s
        if driven_rad_s < self.MIN_DRIVEN_WHEEL_RAD_S:
            return

        engine_rad_s = td.current_rpm * (2.0 * math.pi / 60.0)
        ratio = engine_rad_s / driven_rad_s
        if ratio < 0.5 or ratio > 50:
            return
        if td.clutch_raw > 5:
            return
        if not self._ratio_fits_sequence(ck, gear, ratio):
            return

        car_ratios = self._ratios.setdefault(ck, {})
        car_counts = self._counts.setdefault(ck, {})

        if gear not in car_ratios:
            car_ratios[gear] = ratio
            car_counts[gear] = 1
            self._observe_wheel_radius(td, ck, driven_rad_s)
            return

        current = car_ratios[gear]
        n = car_counts[gear]
        if n >= self.OUTLIER_GRACE and abs(ratio - current) / current > self.OUTLIER_TOLERANCE:
            return

        rate = max(self.LEARN_RATE, 1.0 / (n + 1))
        car_ratios[gear] = current + rate * (ratio - current)
        car_counts[gear] = n + 1
        self._observe_wheel_radius(td, ck, driven_rad_s)

    def _is_clean_contact(self, td: Telemetry) -> bool:
        if td.max_combined_slip > self.MAX_CLEAN_SLIP:
            return False
        if td.min_suspension_norm <= self.MIN_SUSPENSION_NORM:
            return False
        if td.max_surface_rumble > self.MAX_SURFACE_RUMBLE:
            return False
        if td.any_puddle:
            return False
        return True

    def _observe_wheel_radius(self, td: Telemetry, car_key: tuple, driven_rad_s: float):
        speed_ms = td.speed_effective_ms
        if speed_ms <= 3.0 or driven_rad_s <= self.MIN_DRIVEN_WHEEL_RAD_S:
            return
        radius = speed_ms / driven_rad_s
        if radius < 0.15 or radius > 0.60:
            return

        current = self._wheel_radius.get(car_key)
        n = self._wheel_radius_counts.get(car_key, 0)
        if current is None:
            self._wheel_radius[car_key] = radius
            self._wheel_radius_counts[car_key] = 1
            return

        rate = max(self.LEARN_RATE, 1.0 / (n + 1))
        self._wheel_radius[car_key] = current + rate * (radius - current)
        self._wheel_radius_counts[car_key] = n + 1

    def _ratio_fits_sequence(self, car_key: tuple, gear: int, ratio: float) -> bool:
        tol = self.RATIO_MONOTONIC_TOLERANCE
        lower = self._nearest_mature_ratio(car_key, gear, -1)
        if lower is not None and ratio >= lower * (1.0 - tol):
            return False
        higher = self._nearest_mature_ratio(car_key, gear, 1)
        if higher is not None and ratio <= higher * (1.0 + tol):
            return False
        return True

    def _nearest_mature_ratio(self, car_key: tuple, gear: int, direction: int) -> float | None:
        ratios = self._ratios.get(car_key, {})
        counts = self._counts.get(car_key, {})
        for g in range(gear + direction, 11 if direction > 0 else 0, direction):
            ratio = ratios.get(g)
            if ratio is not None and counts.get(g, 0) >= self.OUTLIER_GRACE:
                return ratio
        return None

    def _valid_ratios(self, car_key: tuple) -> dict[int, float]:
        raw = self._ratios.get(car_key, {})
        counts = self._counts.get(car_key, {})
        valid: dict[int, float] = {}
        previous: float | None = None
        tol = self.RATIO_MONOTONIC_TOLERANCE
        for gear in sorted(raw):
            ratio = raw[gear]
            if counts.get(gear, 0) < self.OUTLIER_GRACE:
                continue
            if previous is not None and ratio >= previous * (1.0 - tol):
                break
            valid[gear] = ratio
            previous = ratio
        return valid

    def project_rpm_after_shift(self, td: Telemetry, target_gear: int) -> float | None:
        car_ratios = self.get_ratios(td.car_key)
        if not car_ratios:
            return None
        target_ratio = car_ratios.get(target_gear)
        if not target_ratio:
            return None

        driven_rad_s = td.driven_wheel_speed_rad_s
        if driven_rad_s < self.MIN_DRIVEN_WHEEL_RAD_S:
            radius = self._wheel_radius.get(td.car_key)
            if not radius:
                return None
            driven_rad_s = td.speed_effective_ms / radius
        return self._wheel_speed_to_rpm(driven_rad_s, target_ratio)

    def project_rpm_at_speed(
        self, car_key: tuple, target_gear: int, speed_kmh: float
    ) -> float | None:
        car_ratios = self.get_ratios(car_key)
        radius = self._wheel_radius.get(car_key)
        if not car_ratios or not radius:
            return None
        target_ratio = car_ratios.get(target_gear)
        if not target_ratio:
            return None
        driven_rad_s = (speed_kmh / 3.6) / radius
        return self._wheel_speed_to_rpm(driven_rad_s, target_ratio)

    def speed_for_rpm(self, car_key: tuple, gear: int, rpm: float) -> float | None:
        car_ratios = self.get_ratios(car_key)
        radius = self._wheel_radius.get(car_key)
        if not car_ratios or not radius:
            return None
        ratio = car_ratios.get(gear)
        if not ratio:
            return None
        engine_rad_s = rpm * (2.0 * math.pi / 60.0)
        driven_rad_s = engine_rad_s / ratio
        return driven_rad_s * radius * 3.6

    @staticmethod
    def _wheel_speed_to_rpm(driven_rad_s: float, ratio: float) -> float:
        return driven_rad_s * ratio * (60.0 / (2.0 * math.pi))

    def get_ratios(self, car_key: tuple) -> dict[int, float]:
        return self._valid_ratios(car_key)

    def ratio_for_gear(self, car_key: tuple, gear: int) -> float | None:
        return self.get_ratios(car_key).get(gear)

    def has_data(self, car_key: tuple) -> bool:
        return len(self.get_ratios(car_key)) >= 2

    def reset_car(self, car_key: tuple):
        self._ratios.pop(car_key, None)
        self._counts.pop(car_key, None)
        self._wheel_radius.pop(car_key, None)
        self._wheel_radius_counts.pop(car_key, None)

    def dump(self, car_key: tuple) -> dict | None:
        if not self.has_data(car_key):
            return None
        return {
            "ratios": dict(self._ratios.get(car_key, {})),
            "counts": dict(self._counts.get(car_key, {})),
            "wheel_radius": self._wheel_radius.get(car_key),
            "wheel_radius_count": self._wheel_radius_counts.get(car_key, 0),
            "basis": self.PROFILE_BASIS,
        }

    def load(self, car_key: tuple, data: dict):
        if not isinstance(data, dict):
            return
        if data.get("basis") != self.PROFILE_BASIS:
            return
        ratios = data.get("ratios")
        counts = data.get("counts")
        if isinstance(ratios, dict):
            self._ratios[car_key] = {int(k): float(v) for k, v in ratios.items()}
        if isinstance(counts, dict):
            self._counts[car_key] = {int(k): int(v) for k, v in counts.items()}

        radius = data.get("wheel_radius")
        radius_count = data.get("wheel_radius_count", 0)
        if isinstance(radius, (int, float)) and radius > 0:
            self._wheel_radius[car_key] = float(radius)
            self._wheel_radius_counts[car_key] = int(radius_count)
