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


def wheel_speed_for(speed_ms: float, radius: float = 0.34) -> float:
    return speed_ms / radius


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


def test_race_upshift_uses_power_cross_before_limiter(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)
    tcu._power_curve.has_mature_data = lambda _car_key: True
    tcu._power_curve.has_power_lookup = lambda _car_key: True
    tcu._power_curve.peak_power_abs_rpm = lambda _car_key: 5700.0
    tcu._power_curve.max_high_power_rpm = lambda _car_key, min_peak_ratio=0.80: 7600.0

    def power_at_rpm(_car_key, rpm):
        if rpm < 4800.0:
            return 340.0
        if rpm < 5700.0:
            return 420.0
        return max(300.0, 420.0 - (rpm - 5700.0) * 0.04)

    tcu._power_curve.power_at_rpm = power_at_rpm
    tcu._power_curve.power_slope_at_rpm = lambda _car_key, _rpm: -0.04

    tcu.process(telemetry(current_rpm=6500.0, gear=5, accel_raw=240))

    assert output.up == 1
    assert tcu._tcu_state == "UPSHIFT"
    assert "power" in tcu._tcu_state_sub


def test_race_upshift_leads_fast_rpm_rise(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.86, "power cross")
    tcu._power_curve.peak_power_abs_rpm = lambda _car_key: 6200.0
    tcu._rpm_rate_history.extend([7600.0, 8000.0, 8400.0])

    tcu.process(telemetry(current_rpm=6700.0, gear=5, accel_raw=245))

    assert output.up == 1
    assert tcu._tcu_state == "UPSHIFT"
    assert "lead" in tcu._tcu_state_sub


def test_optimal_shift_snapshot_is_gear_pair_specific(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)
    tcu._power_curve.has_mature_data = lambda _car_key: True
    tcu._power_curve.has_power_lookup = lambda _car_key: True
    tcu._power_curve.peak_power_abs_rpm = lambda _car_key: 6000.0
    tcu._power_curve.max_high_power_rpm = lambda _car_key, min_peak_ratio=0.80: 7800.0

    def power_at_rpm(_car_key, rpm):
        if rpm < 6000.0:
            return 250.0 + (rpm - 3000.0) * 0.05
        return max(250.0, 400.0 - (rpm - 6000.0) * 0.045)

    tcu._power_curve.power_at_rpm = power_at_rpm
    tcu._power_curve.power_slope_at_rpm = lambda _car_key, _rpm: -0.045

    second = tcu._optimal_shift_snapshot(telemetry(current_rpm=5000.0, gear=2))
    fifth = tcu._optimal_shift_snapshot(telemetry(current_rpm=5000.0, gear=5))

    assert second["optimal_shift_from_gear"] == 2
    assert second["optimal_shift_to_gear"] == 3
    assert fifth["optimal_shift_from_gear"] == 5
    assert fifth["optimal_shift_to_gear"] == 6
    assert second["optimal_shift_rpm"] != fifth["optimal_shift_rpm"]


def test_learn_mode_guides_without_auto_shifting(tmp_path):
    tcu, output = make_tcu(tmp_path, "LEARN")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)

    tcu.process(telemetry(current_rpm=7700.0, gear=5, accel_raw=255))

    assert output.up == 0
    assert output.down == 0
    assert tcu._tcu_state == "LEARNING"
    assert "Hold full throttle" in tcu._shift_hint


def test_learn_mode_rejects_spin_without_changing_tune(tmp_path):
    tcu, output = make_tcu(tmp_path, "LEARN")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)

    tcu.process(
        telemetry(
            current_rpm=5200.0,
            gear=3,
            accel_raw=255,
            slip_fl=2.2,
            slip_fr=2.2,
            slip_rl=2.2,
            slip_rr=2.2,
        )
    )

    assert output.up == 0
    assert output.down == 0
    assert tcu._current_car_key == car_key
    assert tcu._tcu_state == "LEARN PAUSED"
    assert "reduce wheelspin" in tcu._tcu_state_sub


def test_learn_mode_announces_done_when_curve_and_ratios_are_ready(tmp_path):
    tcu, output = make_tcu(tmp_path, "LEARN")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)
    tcu._power_curve.has_mature_data = lambda _car_key: True
    tcu._power_curve.has_power_lookup = lambda _car_key: True
    tcu._power_curve.learning_progress = lambda _car_key: {
        "samples": 128,
        "points": 32,
        "confidence": 0.72,
        "rpm_spread": 0.44,
        "min_rpm": 2600,
        "max_rpm": 7600,
    }

    tcu.process(telemetry(current_rpm=4200.0, gear=3, accel_raw=80))

    assert output.up == 0
    assert output.down == 0
    assert tcu._tcu_state == "LEARN DONE"
    assert "switch to Race" in tcu._tcu_state_sub


def test_race_track_brake_accepts_sustained_medium_brake(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    tcu._speed_history.extend([120, 119, 118, 117, 116, 115, 113, 111])

    tcu.process(telemetry(current_rpm=4200.0, brake_raw=88, accel_raw=0, gear=5))

    assert output.down == 1
    assert tcu._tcu_state == "BRAKE DOWN"


def test_race_brake_down_with_only_current_ratio_learned(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {5: 4.4}
    tcu._calibrator._counts[car_key] = {5: 8}
    tcu._calibrator._wheel_radius[car_key] = 0.34
    tcu._calibrator._wheel_radius_counts[car_key] = 8
    tcu._speed_history.extend([120, 119, 118, 117, 116, 115, 113, 111])
    tcu._brake_history.extend([90 / 255] * 6)
    tcu._brake_raw_history.extend([90 / 255] * 6)

    speed_ms = 32.0
    wheel_speed = wheel_speed_for(speed_ms)
    tcu.process(
        telemetry(
            current_rpm=4200.0,
            gear=5,
            speed_ms=speed_ms,
            accel_raw=0,
            brake_raw=90,
            wheel_speed_fl=wheel_speed,
            wheel_speed_fr=wheel_speed,
            wheel_speed_rl=wheel_speed,
            wheel_speed_rr=wheel_speed,
        )
    )

    assert output.down == 1
    assert tcu._tcu_state == "BRAKE DOWN"


def test_race_brake_engine_downshifts_when_target_would_hold_current(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)
    tcu._speed_history.extend([168, 166, 164, 162, 160, 158, 156, 154])
    tcu._brake_history.extend([82 / 255] * 6)
    tcu._brake_raw_history.extend([82 / 255] * 6)

    speed_ms = 160.0 / 3.6
    wheel_speed = wheel_speed_for(speed_ms)
    td = telemetry(
        current_rpm=5500.0,
        gear=5,
        speed_ms=speed_ms,
        accel_raw=0,
        brake_raw=82,
        wheel_speed_fl=wheel_speed,
        wheel_speed_fr=wheel_speed,
        wheel_speed_rl=wheel_speed,
        wheel_speed_rr=wheel_speed,
    )
    brake_margin = 0.20 * min(1.0, td.brake / 0.80)
    assert tcu._target_gear_for_braking(td, td.speed_kmh * (1.0 - brake_margin)) == td.gear

    tcu.process(td)

    assert output.down == 1
    assert tcu._tcu_state == "BRAKE DOWN"
    assert "engine brake" in tcu._tcu_state_sub


def test_invalid_zero_packet_does_not_reverse_lock_brake_down(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)

    tcu.process(
        telemetry(
            engine_max_rpm=0.0,
            current_rpm=0.0,
            gear=0,
            car_ordinal=0,
            car_class=0,
            pi=0,
        )
    )
    assert tcu._tcu_state == "NO DATA"
    assert tcu._reverse_lock_until == 0.0

    tcu._speed_history.extend([138, 136, 134, 132, 130, 128, 126, 124])
    tcu._brake_history.extend([255 / 255] * 6)
    tcu._brake_raw_history.extend([255 / 255] * 6)
    speed_ms = 130.0 / 3.6
    wheel_speed = wheel_speed_for(speed_ms)
    tcu.process(
        telemetry(
            current_rpm=5200.0,
            gear=5,
            speed_ms=speed_ms,
            accel_raw=0,
            brake_raw=255,
            wheel_speed_fl=wheel_speed,
            wheel_speed_fr=wheel_speed,
            wheel_speed_rl=wheel_speed,
            wheel_speed_rr=wheel_speed,
        )
    )

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


def test_brake_lockup_slip_does_not_count_as_airtime():
    detector = AirtimeDetector()
    locked_brake = telemetry(
        speed_ms=30.0,
        brake_raw=255,
        accel_y=0.0,
        slip_fl=3.0,
        slip_fr=3.0,
        slip_rl=3.0,
        slip_rr=3.0,
        suspension_norm_fl=0.25,
        suspension_norm_fr=0.25,
        suspension_norm_rl=0.25,
        suspension_norm_rr=0.25,
    )

    for i in range(5):
        detector.update(locked_brake, now=200.0 + i * 0.016)

    assert not detector.is_airborne
    assert not detector.just_landed


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
