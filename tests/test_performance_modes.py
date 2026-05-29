import time
from pathlib import Path

from virtual_tcu.config.constants import Cfg
from virtual_tcu.config.store import ConfigStore
from virtual_tcu.core.mode import Mode
from virtual_tcu.detectors.airtime import AirtimeDetector
from virtual_tcu.input.interface import OutputInterface
from virtual_tcu.logic.tcu import TCULogic
from virtual_tcu.storage.profiles import ProfileStore
from virtual_tcu.telemetry.logger import TelemetryLogger
from virtual_tcu.telemetry.model import Telemetry


class CountingOutput(OutputInterface):
    def __init__(self):
        self.up = 0
        self.down = 0
        self.double_down = 0

    @property
    def key_up(self) -> str:
        return "e"

    @property
    def key_down(self) -> str:
        return "q"

    def is_self_press(self, key: str) -> bool:
        return False

    def shift_up(self):
        self.up += 1

    def shift_down(self):
        self.down += 1

    def shift_down_double(self):
        self.double_down += 1

    def shutdown(self):
        pass


def make_tcu(tmp_path: Path, mode: str) -> tuple[TCULogic, CountingOutput]:
    Cfg.REVERSE_HOLD_MS = 0
    config = ConfigStore(tmp_path / "config.json")
    config.set("current_mode", mode)
    output = CountingOutput()
    tcu = TCULogic(output, ProfileStore(tmp_path / "profiles.json"), config, TelemetryLogger())
    return tcu, output


def telemetry(**overrides) -> Telemetry:
    values = {
        "is_race_on": 1,
        "engine_max_rpm": 8000.0,
        "idle_rpm": 900.0,
        "current_rpm": 3300.0,
        "gear": 5,
        "speed_ms": 32.0,
        "accel_raw": 190,
        "brake_raw": 0,
        "car_ordinal": 1,
        "car_class": 5,
        "pi": 900,
        "drivetrain": 2,
        "suspension_norm_fl": 0.5,
        "suspension_norm_fr": 0.5,
        "suspension_norm_rl": 0.5,
        "suspension_norm_rr": 0.5,
        "wheel_speed_fl": 80.0,
        "wheel_speed_fr": 80.0,
        "wheel_speed_rl": 80.0,
        "wheel_speed_rr": 80.0,
        "power_w": 220000.0,
        "torque_nm": 420.0,
    }
    values.update(overrides)
    return Telemetry(**values)


def seed_ratios(tcu: TCULogic, car_key=(1, 5, 900)):
    tcu._calibrator._ratios[car_key] = {
        1: 13.0,
        2: 10.0,
        3: 7.2,
        4: 5.6,
        5: 4.4,
        6: 3.5,
    }
    tcu._calibrator._counts[car_key] = {gear: 8 for gear in range(1, 7)}
    tcu._calibrator._wheel_radius[car_key] = 0.34
    tcu._calibrator._wheel_radius_counts[car_key] = 8


def test_race_power_down_uses_target_gear(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)

    td = telemetry(current_rpm=3300.0, gear=5, accel_raw=205)
    target = tcu._target_gear_for_power(td, fallback_pct=0.72, target_bias=0.45, floor_pct=0.60)
    assert target is not None
    assert target[0] < td.gear

    tcu.process(td)

    assert output.down + output.double_down >= 1
    assert tcu._tcu_state == "RACE POWER DOWN"


def test_race_power_down_skips_when_projected_power_is_worse(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    tcu._power_curve.has_power_lookup = lambda _car_key: True
    tcu._power_curve.power_at_rpm = lambda _car_key, rpm: 320.0 if rpm < 3900 else 260.0

    tcu.process(telemetry(current_rpm=3300.0, gear=5, accel_raw=205))

    assert output.down == 0
    assert output.double_down == 0
    assert tcu._tcu_state == "RACE"


def test_race_track_brake_accepts_sustained_medium_brake(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    tcu._speed_history.extend([120, 119, 118, 117, 116, 115, 113, 111])

    tcu.process(telemetry(current_rpm=4200.0, brake_raw=88, accel_raw=0, gear=5))

    assert output.down == 1
    assert tcu._tcu_state == "BRAKE DOWN"


def test_airtime_detector_reports_landing_window():
    detector = AirtimeDetector()
    airborne = telemetry(
        speed_ms=25.0,
        suspension_norm_fl=0.0,
        suspension_norm_fr=0.0,
        suspension_norm_rl=0.0,
        suspension_norm_rr=0.0,
    )
    grounded = telemetry(speed_ms=25.0, vel_y=0.0)

    for i in range(3):
        detector.update(airborne, now=100.0 + i * 0.016)
    assert detector.is_airborne

    detector.update(grounded, now=100.10)
    assert not detector.just_landed
    detector.update(grounded, now=100.12)

    assert not detector.is_airborne
    assert detector.just_landed
    assert detector.landing_recovery(now=100.20)


def test_landing_recovery_clears_downshift_lock(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    tcu._airtime._landing_until = time.time() + 1.0
    tcu._no_downshift_until = time.time() + 10.0

    shifted = tcu._landing_recovery_downshift(
        telemetry(current_rpm=3200.0, gear=5, accel_raw=150),
        time.time(),
        Mode.RACE,
    )

    assert shifted
    assert output.double_down == 1
    assert tcu._no_downshift_until < time.time() + 1.0


def test_offroad_uses_torque_power_down(tmp_path):
    tcu, output = make_tcu(tmp_path, "OFFROAD")
    seed_ratios(tcu)

    tcu.process(telemetry(current_rpm=3400.0, gear=4, accel_raw=150, speed_ms=18.0))

    assert output.down + output.double_down >= 1
    assert tcu._tcu_state == "TORQUE DOWN"


def test_drift_keeps_single_downshift(tmp_path):
    tcu, output = make_tcu(tmp_path, "DRIFT")
    seed_ratios(tcu)

    tcu.process(telemetry(current_rpm=4200.0, gear=4, accel_raw=150, speed_ms=28.0))

    assert output.down == 1
    assert output.double_down == 0
    assert tcu._tcu_state == "DRIFT HOLD"
