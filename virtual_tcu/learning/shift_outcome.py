"""Race upshift outcome learner.

This layer keeps a small per-car/per-gear RPM correction for Race mode.  It
does not replace the power-curve strategy; it only nudges that strategy after
clean WOT shifts show that a slightly earlier or later shift accelerates better.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import mean
from typing import Any

from virtual_tcu.telemetry.model import Telemetry


@dataclass(frozen=True)
class ShiftOutcomeUpdate:
    changed: bool
    offset_rpm: float
    reason: str
    sample_count: int
    reward_delta: float | None = None


@dataclass
class _PendingCommand:
    car_key: tuple
    from_gear: int
    to_gear: int
    command_time: float
    command_rpm: float
    command_speed_kmh: float
    target_rpm: float
    nominal_target_rpm: float
    applied_offset_rpm: float
    source: str


@dataclass
class _PendingObservation:
    command: _PendingCommand
    confirm_time: float
    confirm_speed_kmh: float
    landing_rpm: float
    landing_power_ratio: float | None


class ShiftOutcomeLearner:
    """Learns tiny Race-mode upshift RPM offsets from clean acceleration samples."""

    MAX_SAMPLES = 24
    MAX_OFFSET_RPM = 250.0
    STEP_RPM = 25.0
    LOW_POWER_STEP_RPM = 50.0
    PROBE_RPM = 40.0
    MIN_COMPARE_SAMPLES = 6
    MIN_POWER_BAND_SAMPLES = 2
    MIN_SIDE_SAMPLES = 2
    MIN_OFFSET_SPAN_RPM = 60.0
    MIN_NEW_SAMPLES_AFTER_ADJUST = 2
    REWARD_MARGIN_KMH_S = 0.35
    LOW_GEAR_OBSERVE_AFTER_S = 0.32
    HIGH_GEAR_OBSERVE_AFTER_S = 0.42
    MAX_OBSERVE_AFTER_S = 0.95

    def __init__(self):
        self._samples: dict[tuple[tuple, int], deque[dict[str, float]]] = {}
        self._offsets: dict[tuple[tuple, int], float] = {}
        self._active_offsets: dict[tuple[tuple, int], float] = {}
        self._probe_cursor: dict[tuple[tuple, int], int] = {}
        self._last_adjust_count: dict[tuple[tuple, int], int] = {}
        self._pending_command: _PendingCommand | None = None
        self._pending_observation: _PendingObservation | None = None

    def base_offset_rpm(self, car_key: tuple, gear: int) -> float:
        return self._offsets.get((car_key, gear), 0.0)

    def sample_count(self, car_key: tuple, gear: int) -> int:
        return len(self._samples.get((car_key, gear), ()))

    def status(self, car_key: tuple, gear: int) -> dict:
        """Return a compact UI-facing status for one upshift pair."""
        samples = list(self._samples.get((car_key, gear), ()))
        offset = self.base_offset_rpm(car_key, gear)
        recent_reward = None
        if samples:
            recent = samples[-min(3, len(samples)) :]
            recent_reward = sum(sample["reward_kmh_s"] for sample in recent) / len(recent)
        return {
            "samples": len(samples),
            "offset_rpm": round(offset, 1),
            "active_offset_rpm": round(
                self._active_offsets.get((car_key, gear), offset),
                1,
            ),
            "ready": len(samples) >= self.MIN_COMPARE_SAMPLES,
            "recent_reward_kmh_s": round(recent_reward, 2)
            if recent_reward is not None
            else None,
        }

    def active_offset_rpm(self, car_key: tuple, gear: int, *, allow_probe: bool) -> float:
        if gear < 1 or gear >= 10:
            return 0.0
        key = (car_key, gear)
        active = self._active_offsets.get(key)
        if active is not None:
            return active

        base = self.base_offset_rpm(car_key, gear)
        probe = self._next_probe_rpm(key) if allow_probe else 0.0
        offset = self._clamp_offset(base + probe)
        self._active_offsets[key] = offset
        return offset

    def record_command(
        self,
        car_key: tuple,
        from_gear: int,
        now: float,
        *,
        command_rpm: float,
        command_speed_kmh: float,
        target_rpm: float,
        nominal_target_rpm: float,
        applied_offset_rpm: float,
        source: str,
    ):
        if from_gear < 1 or from_gear >= 10:
            self.cancel_pending()
            return

        self._pending_command = _PendingCommand(
            car_key=car_key,
            from_gear=from_gear,
            to_gear=from_gear + 1,
            command_time=now,
            command_rpm=float(command_rpm),
            command_speed_kmh=float(command_speed_kmh),
            target_rpm=float(target_rpm),
            nominal_target_rpm=float(nominal_target_rpm),
            applied_offset_rpm=float(applied_offset_rpm),
            source=source,
        )
        self._active_offsets.pop((car_key, from_gear), None)

    def confirm_upshift(
        self,
        car_key: tuple,
        to_gear: int,
        now: float,
        *,
        landing_rpm: float,
        landing_speed_kmh: float,
        landing_power_ratio: float | None,
    ) -> bool:
        command = self._pending_command
        if command is None:
            return False
        if command.car_key != car_key or command.to_gear != to_gear:
            self.cancel_pending()
            return False

        self._pending_observation = _PendingObservation(
            command=command,
            confirm_time=now,
            confirm_speed_kmh=float(landing_speed_kmh),
            landing_rpm=float(landing_rpm),
            landing_power_ratio=landing_power_ratio,
        )
        self._pending_command = None
        return True

    def observe(self, td: Telemetry, now: float, *, clean: bool) -> ShiftOutcomeUpdate | None:
        pending = self._pending_observation
        if pending is None:
            return None

        command = pending.command
        elapsed = now - pending.confirm_time
        if td.car_key != command.car_key:
            self.cancel_pending()
            return None
        if elapsed > self.MAX_OBSERVE_AFTER_S or td.gear != command.to_gear:
            self.cancel_pending()
            return None

        observe_after = (
            self.LOW_GEAR_OBSERVE_AFTER_S
            if command.from_gear <= 2
            else self.HIGH_GEAR_OBSERVE_AFTER_S
        )
        if elapsed < observe_after:
            return None
        if not clean:
            self.cancel_pending()
            return None

        speed_gain = td.speed_kmh - pending.confirm_speed_kmh
        reward = speed_gain / max(0.001, elapsed)
        if not (-5.0 <= reward <= 140.0):
            self.cancel_pending()
            return None

        update = self.record_sample(
            command.car_key,
            command.from_gear,
            applied_offset_rpm=command.applied_offset_rpm,
            reward_kmh_s=reward,
            command_rpm=command.command_rpm,
            target_rpm=command.target_rpm,
            nominal_target_rpm=command.nominal_target_rpm,
            landing_rpm=pending.landing_rpm,
            landing_power_ratio=pending.landing_power_ratio,
        )
        self._pending_observation = None
        return update

    def record_sample(
        self,
        car_key: tuple,
        gear: int,
        *,
        applied_offset_rpm: float,
        reward_kmh_s: float,
        command_rpm: float | None = None,
        target_rpm: float | None = None,
        nominal_target_rpm: float | None = None,
        landing_rpm: float | None = None,
        landing_power_ratio: float | None = None,
    ) -> ShiftOutcomeUpdate:
        key = (car_key, gear)
        samples = self._samples.setdefault(key, deque(maxlen=self.MAX_SAMPLES))
        sample: dict[str, float] = {
            "applied_offset_rpm": round(float(applied_offset_rpm), 3),
            "reward_kmh_s": round(float(reward_kmh_s), 3),
        }
        for name, value in (
            ("command_rpm", command_rpm),
            ("target_rpm", target_rpm),
            ("nominal_target_rpm", nominal_target_rpm),
            ("landing_rpm", landing_rpm),
            ("landing_power_ratio", landing_power_ratio),
        ):
            if isinstance(value, (int, float)):
                sample[name] = round(float(value), 3)
        samples.append(sample)
        return self._maybe_adjust_offset(key)

    def penalize_late_shift(
        self,
        car_key: tuple,
        gear: int,
        *,
        reason: str = "late shift",
    ) -> ShiftOutcomeUpdate:
        key = (car_key, gear)
        return self._move_offset(key, -self.STEP_RPM, reason, reward_delta=None)

    def cancel_pending(self):
        self._pending_command = None
        self._pending_observation = None

    def reset_car(self, car_key: tuple):
        for key in [key for key in self._samples if key[0] == car_key]:
            self._samples.pop(key, None)
        for key in [key for key in self._offsets if key[0] == car_key]:
            self._offsets.pop(key, None)
        for key in [key for key in self._active_offsets if key[0] == car_key]:
            self._active_offsets.pop(key, None)
        for key in [key for key in self._probe_cursor if key[0] == car_key]:
            self._probe_cursor.pop(key, None)
        for key in [key for key in self._last_adjust_count if key[0] == car_key]:
            self._last_adjust_count.pop(key, None)
        if self._pending_command and self._pending_command.car_key == car_key:
            self._pending_command = None
        if (
            self._pending_observation
            and self._pending_observation.command.car_key == car_key
        ):
            self._pending_observation = None

    def dump(self, car_key: tuple) -> dict | None:
        offsets = {
            str(gear): round(offset, 3)
            for (sample_car_key, gear), offset in sorted(
                self._offsets.items(),
                key=lambda item: item[0][1],
            )
            if sample_car_key == car_key and abs(offset) > 0.01
        }
        samples = {
            str(gear): list(values)
            for (sample_car_key, gear), values in sorted(
                self._samples.items(),
                key=lambda item: item[0][1],
            )
            if sample_car_key == car_key and values
        }
        if not offsets and not samples:
            return None
        data: dict[str, Any] = {}
        if offsets:
            data["offsets_by_gear"] = offsets
        if samples:
            data["samples_by_gear"] = samples
        return data

    def load(self, car_key: tuple, data: dict):
        if not isinstance(data, dict):
            return

        offsets = data.get("offsets_by_gear", {})
        if isinstance(offsets, dict):
            for gear_key, offset in offsets.items():
                try:
                    gear = int(gear_key)
                    offset_rpm = float(offset)
                except (TypeError, ValueError):
                    continue
                if gear < 1 or gear >= 10:
                    continue
                clamped = self._clamp_offset(offset_rpm)
                if abs(clamped) > 0.01:
                    self._offsets[(car_key, gear)] = clamped

        samples = data.get("samples_by_gear", {})
        if isinstance(samples, dict):
            for gear_key, raw_samples in samples.items():
                try:
                    gear = int(gear_key)
                except (TypeError, ValueError):
                    continue
                if gear < 1 or gear >= 10 or not isinstance(raw_samples, list):
                    continue
                valid: list[dict[str, float]] = []
                for raw in raw_samples:
                    if not isinstance(raw, dict):
                        continue
                    sample = self._coerce_sample(raw)
                    if sample is not None:
                        valid.append(sample)
                if valid:
                    self._samples[(car_key, gear)] = deque(valid, maxlen=self.MAX_SAMPLES)

    def _next_probe_rpm(self, key: tuple[tuple, int]) -> float:
        samples = self._samples.get(key)
        if samples is None or len(samples) < self.MIN_COMPARE_SAMPLES:
            return 0.0
        cursor = self._probe_cursor.get(key, 0)
        sequence = (0.0, self.PROBE_RPM, 0.0, -self.PROBE_RPM)
        self._probe_cursor[key] = cursor + 1
        return sequence[cursor % len(sequence)]

    def _maybe_adjust_offset(self, key: tuple[tuple, int]) -> ShiftOutcomeUpdate:
        samples = list(self._samples.get(key, ()))
        sample_count = len(samples)
        current = self._offsets.get(key, 0.0)
        new_samples = sample_count - self._last_adjust_count.get(key, 0)
        if new_samples >= self.MIN_NEW_SAMPLES_AFTER_ADJUST:
            recent = samples[-min(4, len(samples)) :]
            landing_ratios = [
                sample["landing_power_ratio"]
                for sample in recent
                if "landing_power_ratio" in sample
            ]
            if (
                len(landing_ratios) >= self.MIN_POWER_BAND_SAMPLES
                and mean(landing_ratios) < 0.80
            ):
                return self._move_offset(
                    key,
                    self.LOW_POWER_STEP_RPM,
                    "next gear lands below power band",
                    reward_delta=None,
                )

        if sample_count < self.MIN_COMPARE_SAMPLES:
            return ShiftOutcomeUpdate(False, current, "", sample_count)
        if new_samples < self.MIN_NEW_SAMPLES_AFTER_ADJUST:
            return ShiftOutcomeUpdate(False, current, "", sample_count)

        offsets = [sample["applied_offset_rpm"] for sample in samples]
        if max(offsets) - min(offsets) >= self.MIN_OFFSET_SPAN_RPM:
            split_offset = current
            low = [sample for sample in samples if sample["applied_offset_rpm"] < split_offset]
            high = [sample for sample in samples if sample["applied_offset_rpm"] > split_offset]
            if len(low) < self.MIN_SIDE_SAMPLES or len(high) < self.MIN_SIDE_SAMPLES:
                sorted_offsets = sorted(offsets)
                mid_hi = len(sorted_offsets) // 2
                mid_lo = max(0, mid_hi - 1)
                split_offset = (sorted_offsets[mid_lo] + sorted_offsets[mid_hi]) / 2.0
                low = [
                    sample
                    for sample in samples
                    if sample["applied_offset_rpm"] <= split_offset
                ]
                high = [
                    sample
                    for sample in samples
                    if sample["applied_offset_rpm"] > split_offset
                ]
            if len(low) >= self.MIN_SIDE_SAMPLES and len(high) >= self.MIN_SIDE_SAMPLES:
                low_reward = mean(sample["reward_kmh_s"] for sample in low)
                high_reward = mean(sample["reward_kmh_s"] for sample in high)
                reward_delta = high_reward - low_reward
                if reward_delta > self.REWARD_MARGIN_KMH_S:
                    return self._move_offset(
                        key,
                        self.STEP_RPM,
                        "later samples accelerate better",
                        reward_delta=reward_delta,
                    )
                if reward_delta < -self.REWARD_MARGIN_KMH_S:
                    return self._move_offset(
                        key,
                        -self.STEP_RPM,
                        "earlier samples accelerate better",
                        reward_delta=reward_delta,
                    )

        return ShiftOutcomeUpdate(False, current, "", sample_count)

    def _move_offset(
        self,
        key: tuple[tuple, int],
        delta_rpm: float,
        reason: str,
        *,
        reward_delta: float | None,
    ) -> ShiftOutcomeUpdate:
        current = self._offsets.get(key, 0.0)
        new_offset = self._clamp_offset(current + delta_rpm)
        sample_count = len(self._samples.get(key, ()))
        if abs(new_offset - current) < 0.01:
            return ShiftOutcomeUpdate(False, current, "", sample_count, reward_delta)
        self._offsets[key] = new_offset
        self._active_offsets.pop(key, None)
        self._last_adjust_count[key] = sample_count
        return ShiftOutcomeUpdate(True, new_offset, reason, sample_count, reward_delta)

    def _clamp_offset(self, offset_rpm: float) -> float:
        return max(-self.MAX_OFFSET_RPM, min(self.MAX_OFFSET_RPM, float(offset_rpm)))

    def _coerce_sample(self, raw: dict) -> dict[str, float] | None:
        try:
            applied_offset = float(raw["applied_offset_rpm"])
            reward = float(raw["reward_kmh_s"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (-self.MAX_OFFSET_RPM <= applied_offset <= self.MAX_OFFSET_RPM):
            return None
        if not (-5.0 <= reward <= 140.0):
            return None
        sample = {
            "applied_offset_rpm": round(applied_offset, 3),
            "reward_kmh_s": round(reward, 3),
        }
        for name in (
            "command_rpm",
            "target_rpm",
            "nominal_target_rpm",
            "landing_rpm",
            "landing_power_ratio",
        ):
            value = raw.get(name)
            if isinstance(value, (int, float)):
                sample[name] = round(float(value), 3)
        return sample
