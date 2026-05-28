from __future__ import annotations

from bisect import bisect_left

from virtual_tcu.telemetry.model import Telemetry


W_PER_HP = 745.699872
NM_RPM_PER_HP = 7127.0


class _PowerBin:
    MAX_SAMPLES = 14

    def __init__(self):
        self.count = 0
        self.power_samples: list[float] = []
        self.torque_samples: list[float] = []

    def add(self, power_hp: float, torque_nm: float):
        self.count += 1
        self._add_top_sample(self.power_samples, power_hp)
        self._add_top_sample(self.torque_samples, torque_nm)

    @classmethod
    def _add_top_sample(cls, values: list[float], value: float):
        if not value or value <= 0:
            return
        values.append(float(value))
        values.sort(reverse=True)
        del values[cls.MAX_SAMPLES :]

    @staticmethod
    def _robust_top(values: list[float]) -> float | None:
        if not values:
            return None
        n = max(1, min(5, (len(values) + 2) // 3))
        return sum(values[:n]) / n

    @property
    def power_hp(self) -> float | None:
        return self._robust_top(self.power_samples)

    @property
    def torque_nm(self) -> float | None:
        return self._robust_top(self.torque_samples)

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "power_samples": self.power_samples,
            "torque_samples": self.torque_samples,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_PowerBin":
        bin_ = cls()
        bin_.count = int(data.get("count", 0))
        power = data.get("power_samples", [])
        torque = data.get("torque_samples", [])
        if isinstance(power, list):
            bin_.power_samples = sorted(
                [float(v) for v in power if isinstance(v, (int, float)) and v > 0],
                reverse=True,
            )[: cls.MAX_SAMPLES]
        if isinstance(torque, list):
            bin_.torque_samples = sorted(
                [float(v) for v in torque if isinstance(v, (int, float)) and v > 0],
                reverse=True,
            )[: cls.MAX_SAMPLES]
        return bin_


class _PowerCurveFit:
    BIN_RPM = 50

    def __init__(self):
        self.max_rpm = 0.0
        self.bins: dict[int, _PowerBin] = {}

    @staticmethod
    def _bin_rpm(rpm: float) -> int:
        return int(round(rpm / _PowerCurveFit.BIN_RPM) * _PowerCurveFit.BIN_RPM)

    def add(self, rpm: float, max_rpm: float, power_hp: float, torque_nm: float):
        if rpm <= 0 or power_hp <= 0 or torque_nm <= 0:
            return
        self.max_rpm = max(self.max_rpm, max_rpm)
        key = self._bin_rpm(rpm)
        self.bins.setdefault(key, _PowerBin()).add(power_hp, torque_nm)

    def points(self) -> list[tuple[int, float, float]]:
        pts: list[tuple[int, float, float]] = []
        for rpm, bin_ in self.bins.items():
            power = bin_.power_hp
            torque = bin_.torque_nm
            if power is None or torque is None:
                continue
            pts.append((rpm, power, torque))
        pts.sort(key=lambda p: p[0])
        return pts

    @property
    def total_samples(self) -> int:
        return sum(bin_.count for bin_ in self.bins.values())

    @property
    def rpm_spread(self) -> float:
        pts = self.points()
        if len(pts) < 2 or self.max_rpm <= 0:
            return 0.0
        return (pts[-1][0] - pts[0][0]) / self.max_rpm

    def to_dict(self) -> dict:
        return {
            "format": "power_bins_v2",
            "max_rpm": self.max_rpm,
            "bin_rpm": self.BIN_RPM,
            "bins": {str(rpm): bin_.to_dict() for rpm, bin_ in self.bins.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_PowerCurveFit":
        fit = cls()
        fit.max_rpm = float(data.get("max_rpm", 0.0))
        bins = data.get("bins", {})
        if isinstance(bins, dict):
            for rpm, bin_data in bins.items():
                if not isinstance(bin_data, dict):
                    continue
                try:
                    fit.bins[int(rpm)] = _PowerBin.from_dict(bin_data)
                except (TypeError, ValueError):
                    continue
        return fit


class PowerCurveDetector:
    """Per-car full-load engine model.

    The shift logic needs power at arbitrary RPM, not only the peak. This
    detector stores robust top samples per absolute-RPM bin, then interpolates
    engine power so the TCU can compare the current gear against the next gear.
    """

    MIN_SAMPLES = 12
    FULL_CONF_SAMPLES = 110
    MIN_SPREAD = 0.10
    GOOD_SPREAD = 0.35
    TRUST_MODEL_CONFIDENCE = 0.35
    MIN_THROTTLE = 0.70
    MIN_CLEAN_SUSPENSION = 0.08
    MAX_LEARN_SLIP = 1.5
    MAX_SURFACE_RUMBLE = 0.20

    def __init__(self):
        self._fits: dict[tuple, _PowerCurveFit] = {}

    @staticmethod
    def _power_hp(td: Telemetry) -> float:
        if abs(td.power_w) > 1.0:
            return max(0.0, td.power_w / W_PER_HP)
        if td.torque_nm > 0 and td.current_rpm > 0:
            return td.torque_nm * td.current_rpm / NM_RPM_PER_HP
        return 0.0

    def observe(self, td: Telemetry):
        ck = td.car_key
        if ck[0] <= 0 or td.gear < 1:
            return
        if td.throttle < self.MIN_THROTTLE or td.torque_nm <= 0 or td.is_shifting:
            return
        if td.current_rpm < 500 or td.current_rpm > td.engine_max_rpm * 1.02:
            return
        if (
            td.min_suspension_norm <= self.MIN_CLEAN_SUSPENSION
            or td.any_puddle
            or td.max_surface_rumble > self.MAX_SURFACE_RUMBLE
            or td.max_combined_slip > self.MAX_LEARN_SLIP
        ):
            return

        power_hp = self._power_hp(td)
        if power_hp <= 1.0:
            return
        self._fits.setdefault(ck, _PowerCurveFit()).add(
            td.current_rpm,
            td.engine_max_rpm,
            power_hp,
            td.torque_nm,
        )

    def _peaks(self, car_key: tuple):
        """Return (peak_torque_pct, peak_power_pct, confidence)."""
        fit = self._fits.get(car_key)
        if fit is None or fit.total_samples < self.MIN_SAMPLES:
            return None, None, 0.0
        pts = fit.points()
        if len(pts) < 3 or fit.max_rpm <= 0:
            return None, None, 0.0

        peak_power = max(pts, key=lambda p: p[1])
        peak_torque = max(pts, key=lambda p: p[2])

        n_conf = max(
            0.0,
            min(1.0, (fit.total_samples - self.MIN_SAMPLES) / (self.FULL_CONF_SAMPLES - self.MIN_SAMPLES)),
        )
        s_conf = max(
            0.0,
            min(1.0, (fit.rpm_spread - self.MIN_SPREAD) / (self.GOOD_SPREAD - self.MIN_SPREAD)),
        )
        confidence = n_conf * s_conf
        return peak_torque[0] / fit.max_rpm, peak_power[0] / fit.max_rpm, confidence

    def _point_arrays(self, car_key: tuple) -> tuple[list[int], list[float]] | None:
        fit = self._fits.get(car_key)
        if fit is None:
            return None
        pts = fit.points()
        if len(pts) < 3:
            return None
        return [p[0] for p in pts], [p[1] for p in pts]

    def power_at_rpm(self, car_key: tuple, rpm: float) -> float | None:
        arrays = self._point_arrays(car_key)
        if arrays is None:
            return None
        rpms, powers = arrays
        if rpm < rpms[0] - _PowerCurveFit.BIN_RPM or rpm > rpms[-1] + _PowerCurveFit.BIN_RPM:
            return None
        idx = bisect_left(rpms, rpm)
        if idx <= 0:
            return powers[0]
        if idx >= len(rpms):
            return powers[-1]
        left_rpm, right_rpm = rpms[idx - 1], rpms[idx]
        left_power, right_power = powers[idx - 1], powers[idx]
        if right_rpm == left_rpm:
            return left_power
        t = (rpm - left_rpm) / (right_rpm - left_rpm)
        return left_power + (right_power - left_power) * t

    def power_slope_at_rpm(self, car_key: tuple, rpm: float, step_rpm: float = 200.0) -> float | None:
        low = self.power_at_rpm(car_key, rpm - step_rpm)
        high = self.power_at_rpm(car_key, rpm + step_rpm)
        if low is None or high is None:
            return None
        return (high - low) / (2.0 * step_rpm)

    def peak_torque_rpm(self, car_key: tuple) -> float | None:
        return self._peaks(car_key)[0]

    def peak_power_rpm(self, car_key: tuple) -> float | None:
        return self._peaks(car_key)[1]

    def peak_power_abs_rpm(self, car_key: tuple) -> float | None:
        pct = self.peak_power_rpm(car_key)
        fit = self._fits.get(car_key)
        if pct is None or fit is None or fit.max_rpm <= 0:
            return None
        return pct * fit.max_rpm

    def confidence(self, car_key: tuple) -> float:
        return self._peaks(car_key)[2]

    def optimal_upshift_rpm(
        self,
        td: Telemetry,
        fallback: float = 0.85,
        offset: float = 0.03,
        blend_fallback: bool = True,
    ) -> float:
        _, pp, conf = self._peaks(td.car_key)
        if pp is None:
            return fallback
        model = max(0.65, min(0.985, pp + offset))
        if not blend_fallback or conf >= self.TRUST_MODEL_CONFIDENCE:
            return model
        return conf * model + (1.0 - conf) * fallback

    def has_data(self, car_key: tuple) -> bool:
        return self._peaks(car_key)[1] is not None

    def has_mature_data(self, car_key: tuple) -> bool:
        return self.confidence(car_key) >= self.TRUST_MODEL_CONFIDENCE

    def has_power_lookup(self, car_key: tuple) -> bool:
        fit = self._fits.get(car_key)
        return bool(fit and len(fit.points()) >= 8 and self.confidence(car_key) >= 0.20)

    def dump(self, car_key: tuple) -> dict | None:
        fit = self._fits.get(car_key)
        return fit.to_dict() if fit is not None else None

    def load(self, car_key: tuple, data: dict):
        if not isinstance(data, dict):
            return
        if data.get("format") != "power_bins_v2":
            return
        self._fits[car_key] = _PowerCurveFit.from_dict(data)
