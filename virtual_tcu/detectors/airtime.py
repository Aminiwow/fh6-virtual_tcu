from virtual_tcu.telemetry.model import Telemetry


class AirtimeDetector:
    """Detects all four wheels off the ground.

    FH6 reports normalized suspension travel directly: 0.0 is max stretch and
    1.0 is max compression. That is the primary signal. The older accel/slip
    heuristic remains as a fallback for bad frames or old replay data.
    """

    LOW_VERTICAL_G_THRESHOLD = 3.0
    SLIP_ALL_WHEELS_THRESHOLD = 1.2
    SUSPENSION_AIRBORNE_THRESHOLD = 0.06
    SUSPENSION_GROUNDED_THRESHOLD = 0.12
    MIN_SPEED_FOR_AIRBORNE = 5.0
    FRAMES_TO_ENGAGE = 3
    FRAMES_TO_DISENGAGE = 2

    def __init__(self):
        self._airborne_streak = 0
        self._grounded_streak = 0
        self._is_airborne = False

    def update(self, td: Telemetry) -> bool:
        suspension_airborne = (
            td.speed_kmh > self.MIN_SPEED_FOR_AIRBORNE
            and max(td.suspension_norm) <= self.SUSPENSION_AIRBORNE_THRESHOLD
        )
        suspension_grounded = (
            td.speed_kmh <= self.MIN_SPEED_FOR_AIRBORNE
            or td.min_suspension_norm >= self.SUSPENSION_GROUNDED_THRESHOLD
        )

        low_g = abs(td.accel_y) < self.LOW_VERTICAL_G_THRESHOLD
        thr = self.SLIP_ALL_WHEELS_THRESHOLD
        all_spin = (
            abs(td.slip_fl) > thr
            and abs(td.slip_fr) > thr
            and abs(td.slip_rl) > thr
            and abs(td.slip_rr) > thr
        )
        fallback_airborne = td.speed_kmh > 20.0 and low_g and all_spin
        airborne_now = suspension_airborne or fallback_airborne

        if airborne_now:
            self._airborne_streak += 1
            self._grounded_streak = 0
            if self._airborne_streak >= self.FRAMES_TO_ENGAGE:
                self._is_airborne = True
        elif suspension_grounded:
            self._grounded_streak += 1
            self._airborne_streak = 0
            if self._grounded_streak >= self.FRAMES_TO_DISENGAGE:
                self._is_airborne = False
        else:
            self._airborne_streak = 0
            self._grounded_streak = 0
        return self._is_airborne

    @property
    def is_airborne(self) -> bool:
        return self._is_airborne
