from __future__ import annotations

import time
from dataclasses import dataclass

from virtual_tcu.telemetry.model import Telemetry


@dataclass(frozen=True)
class AirState:
    airborne: bool
    airborne_started: bool
    just_landed: bool


class AirtimeDetector:
    """Detect jumps using suspension first, then accel/velocity fallbacks."""

    FREEFALL_ACCEL_Y = -6.0
    GROUNDED_ACCEL_Y = -4.0
    LOW_VERTICAL_G_THRESHOLD = 3.0
    SLIP_ALL_WHEELS_THRESHOLD = 1.2
    SLIP_VOTE_THRESHOLD = 1.2
    SUSPENSION_AIRBORNE_THRESHOLD = 0.06
    SUSPENSION_GROUNDED_THRESHOLD = 0.12
    MIN_SPEED_FOR_AIRBORNE = 5.0
    ACCEL_MIN_SPEED_KMH = 12.0
    VERTICAL_SPEED_AIRBORNE_MS = 2.2
    VERTICAL_SPEED_GROUNDED_MS = 1.3
    FRAMES_TO_ENGAGE = 3
    FRAMES_TO_DISENGAGE = 2
    LANDING_RECOVERY_S = 0.85

    def __init__(self):
        self._airborne_streak = 0
        self._grounded_streak = 0
        self._is_airborne = False
        self._just_landed = False
        self._landing_until = 0.0

    def update(self, td: Telemetry, now: float | None = None) -> AirState:
        now = time.time() if now is None else now
        self._just_landed = False
        was_airborne = self._is_airborne

        suspension_airborne = (
            td.speed_kmh > self.MIN_SPEED_FOR_AIRBORNE
            and max(td.suspension_norm) <= self.SUSPENSION_AIRBORNE_THRESHOLD
        )
        accel_airborne = (
            td.speed_kmh > self.ACCEL_MIN_SPEED_KMH
            and td.accel_y <= self.FREEFALL_ACCEL_Y
            and td.brake < 0.35
        )

        low_g = abs(td.accel_y) < self.LOW_VERTICAL_G_THRESHOLD
        thr = self.SLIP_ALL_WHEELS_THRESHOLD
        all_spin = (
            abs(td.slip_fl) > thr
            and abs(td.slip_fr) > thr
            and abs(td.slip_rl) > thr
            and abs(td.slip_rr) > thr
        )
        slip_votes = sum(
            1
            for slip in (td.slip_fl, td.slip_fr, td.slip_rl, td.slip_rr)
            if abs(slip) > self.SLIP_VOTE_THRESHOLD
        )
        vertical_airborne = (
            td.speed_kmh > 20.0
            and low_g
            and td.brake < 0.35
            and abs(td.vel_y) >= self.VERTICAL_SPEED_AIRBORNE_MS
            and slip_votes >= 2
        )
        fallback_airborne = (
            td.speed_kmh > 20.0
            and low_g
            and td.brake < 0.25
            and (all_spin or slip_votes >= 3)
        )
        airborne_now = (
            suspension_airborne or accel_airborne or vertical_airborne or fallback_airborne
        )

        suspension_grounded = td.speed_kmh <= self.MIN_SPEED_FOR_AIRBORNE or (
            td.min_suspension_norm >= self.SUSPENSION_GROUNDED_THRESHOLD
            and abs(td.vel_y) <= self.VERTICAL_SPEED_GROUNDED_MS
        )
        accel_grounded = (
            was_airborne
            and td.accel_y >= self.GROUNDED_ACCEL_Y
            and not suspension_airborne
            and abs(td.vel_y) <= self.VERTICAL_SPEED_GROUNDED_MS
        )
        grounded_now = suspension_grounded or accel_grounded

        if airborne_now:
            self._airborne_streak += 1
            self._grounded_streak = 0
            if self._airborne_streak >= self.FRAMES_TO_ENGAGE:
                self._is_airborne = True
        elif grounded_now:
            self._grounded_streak += 1
            self._airborne_streak = 0
            if self._grounded_streak >= self.FRAMES_TO_DISENGAGE:
                self._is_airborne = False
                if was_airborne:
                    self._just_landed = True
                    self._landing_until = now + self.LANDING_RECOVERY_S
        else:
            self._airborne_streak = 0
            self._grounded_streak = 0

        return AirState(
            airborne=self._is_airborne,
            airborne_started=self._is_airborne and not was_airborne,
            just_landed=self._just_landed,
        )

    @property
    def is_airborne(self) -> bool:
        return self._is_airborne

    @property
    def just_landed(self) -> bool:
        return self._just_landed

    @property
    def landing_until(self) -> float:
        return self._landing_until

    def landing_recovery(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now < self._landing_until
