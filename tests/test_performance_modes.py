import time
from pathlib import Path

from virtual_tcu.config.constants import Cfg
from virtual_tcu.config.store import ConfigStore
from virtual_tcu.core.mode import Mode
from virtual_tcu.detectors.airtime import AirtimeDetector
from virtual_tcu.input.interface import OutputInterface
from virtual_tcu.learning.rev_limiter import RevLimiterDetector
from virtual_tcu.learning.shift_lag import ShiftLagLearner
from virtual_tcu.learning.shift_outcome import ShiftOutcomeLearner
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


def test_profile_signature_tracks_new_learned_ratios(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {1: 13.0, 2: 10.0, 3: 7.2}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8, 3: 8}

    before = tcu._profile_signature(car_key)

    tcu._calibrator._ratios[car_key][4] = 5.6
    tcu._calibrator._counts[car_key][4] = 8

    assert tcu._profile_signature(car_key) != before


def test_snapshot_uses_last_valid_car_when_telemetry_is_missing(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._last_valid_telemetry = telemetry(
        car_ordinal=car_key[0],
        car_class=car_key[1],
        pi=car_key[2],
        gear=3,
        current_rpm=5200.0,
    )
    seed_ratios(tcu, car_key)

    snapshot = tcu.snapshot(None)

    assert snapshot["using_cached_car"] is True
    assert snapshot["car_ordinal"] == car_key[0]
    assert snapshot["gear"] == 3
    assert snapshot["shift_guide"]["available"] is True


def test_clear_current_car_learning_resets_profile_and_memory(tmp_path):
    profiles = ProfileStore(tmp_path / "profiles.json")
    tcu, _output = make_tcu(tmp_path, "RACE")
    tcu._profiles = profiles
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._last_valid_telemetry = telemetry(
        car_ordinal=car_key[0],
        car_class=car_key[1],
        pi=car_key[2],
    )
    seed_ratios(tcu, car_key)
    tcu._power_curve._fits[car_key] = object()
    tcu._rev_limiter.load(car_key, 7400.0)
    tcu._shift_lag.record_shift_command(car_key, "UP", 1, 1.0, command_rpm=7000.0)
    tcu._shift_lag.observe_command_frame(car_key, 1, 7120.0)
    tcu._shift_lag.observe_gear_change(car_key, 2, 1.04)
    profiles.set(car_key, {"telemetry_schema": tcu.PROFILE_SCHEMA, "rev_limiter": 7400.0})

    result = tcu.clear_current_car_learning()

    assert result["ok"] is True
    assert profiles.get(car_key) is None
    assert tcu._calibrator.dump(car_key) is None
    assert tcu._power_curve.dump(car_key) is None
    assert tcu._rev_limiter.dump(car_key) is None
    assert tcu._shift_lag.dump(car_key) is None
    assert tcu._shift_outcome.dump(car_key) is None


def test_upshift_decision_logs_target_context(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    events = []
    tcu._logger.record_decision = events.append

    shifted = tcu._track_upshift_in_band(
        telemetry(current_rpm=7900.0, gear=3, accel_raw=255, speed_ms=35.0),
        time.time(),
        offset=0.03,
    )

    assert shifted
    assert output.up == 1
    assert events[-1]["event"] == "shift_up"
    assert events[-1]["upshift_target_rpm"] > 0
    assert events[-1]["upshift_strategy_source"]
    assert events[-1]["ratio_current"] == 7.2
    assert events[-1]["ratio_next"] == 5.6


def test_confirmed_upshift_shortens_lock_when_next_target_is_close(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    tcu._current_car_key = (1, 5, 900)
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.715, "power cross")
    events = []
    tcu._logger.record_decision = events.append

    now = time.time()
    tcu._prev_gear = 4
    tcu._we_shifted = False
    tcu._pending_upshift_gear = 5
    tcu._pending_upshift_until = now + 0.65
    tcu._lock_until = now + 0.65
    tcu._no_upshift_until = now + 0.65

    tcu.process(telemetry(current_rpm=5600.0, gear=5, accel_raw=255, speed_ms=42.0))

    assert tcu._pending_upshift_gear is None
    assert tcu._lock_until < time.time() + 0.10
    assert tcu._no_upshift_until < time.time() + 0.10
    assert events[-1]["event"] == "upshift_confirm"
    assert events[-1]["post_upshift_hold_s"] == 0.03


def test_learned_power_upshift_overrides_turbo_lag_block(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.75, "power cross")
    tcu._turbo_lag_block_upshift = lambda _td: True

    shifted = tcu._track_upshift_in_band(
        telemetry(current_rpm=6100.0, gear=4, accel_raw=255, speed_ms=35.0),
        time.time(),
        offset=0.03,
    )

    assert shifted
    assert output.up == 1


def test_turbo_lag_still_blocks_fallback_upshift(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    tcu._performance_upshift_target_pct = lambda _td, _offset: None
    tcu._race_upshift_target_pct = lambda _td: (0.75, "race fallback")
    tcu._turbo_lag_block_upshift = lambda _td: True

    shifted = tcu._track_upshift_in_band(
        telemetry(current_rpm=6100.0, gear=4, accel_raw=255, speed_ms=35.0),
        time.time(),
        offset=0.03,
    )

    assert not shifted
    assert output.up == 0


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


def test_race_power_down_skips_when_landing_near_upshift_point(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (2739, 3, 700)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {3: 5.04345, 4: 3.99845}
    tcu._calibrator._counts[car_key] = {3: 8, 4: 8}
    tcu._power_curve.has_power_lookup = lambda _car_key: True
    tcu._power_curve.power_at_rpm = lambda _car_key, rpm: 900.0 if rpm > 6200 else 650.0
    tcu._command_upshift_rpm_for_gear = lambda _td, _gear, *, offset: 6515.0

    td = telemetry(
        car_ordinal=2739,
        car_class=3,
        pi=700,
        engine_max_rpm=8000.0,
        current_rpm=5100.0,
        gear=4,
        speed_ms=45.0,
        accel_raw=255,
    )

    target = tcu._target_gear_for_power(
        td,
        fallback_pct=0.72,
        target_bias=0.45,
        floor_pct=0.60,
        min_upshift_reserve_rpm=500.0,
    )

    assert target is None


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


def test_race_fuel_cut_escape_bypasses_upshift_locks(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (2739, 3, 700)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {1: 9.93, 2: 6.78}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8}
    tcu._rev_limiter.load(car_key, 7108.7)
    tcu._power_curve.peak_power_abs_rpm = lambda _car_key: 6200.0
    tcu._cornering_locked = True
    tcu._no_upshift_until = time.time() + 5.0

    tcu._mode_race(
        telemetry(
            car_ordinal=2739,
            car_class=3,
            pi=700,
            engine_max_rpm=8000.0,
            current_rpm=6950.0,
            gear=1,
            speed_ms=25.0,
            accel_raw=255,
            power_w=-400000.0,
        ),
        time.time(),
    )

    assert output.up == 1
    assert tcu._tcu_state == "FUEL CUT"


def test_race_low_gear_limiter_guard_bypasses_airtime_hold(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key
    tcu._rev_limiter.load(car_key, 7505.0)
    tcu._airtime._is_airborne = True

    tcu.process(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=7100.0,
            gear=1,
            speed_ms=65.0,
            accel_raw=255,
            power_w=1200000.0,
        )
    )

    assert output.up == 1
    assert tcu._tcu_state == "UPSHIFT"
    assert tcu._tcu_state_sub == "low gear limiter guard"


def test_race_low_gear_limiter_guard_bypasses_upshift_lock_before_fuel_cut(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key
    tcu._rev_limiter.load(car_key, 7505.0)
    tcu._no_upshift_until = time.time() + 0.8

    tcu._mode_race(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=7100.0,
            gear=1,
            speed_ms=65.0,
            accel_raw=255,
            power_w=1200000.0,
        ),
        time.time(),
    )

    assert output.up == 1
    assert tcu._tcu_state == "UPSHIFT"
    assert tcu._tcu_state_sub == "low gear limiter guard"


def test_race_first_gear_wheelspin_holds_low_speed_upshift(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {1: 3.50, 2: 2.68}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8}
    tcu._calibrator._wheel_radius[car_key] = 0.34
    tcu._calibrator._wheel_radius_counts[car_key] = 8
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.854, "power ceiling")
    events = []
    tcu._logger.record_decision = events.append

    shifted_or_held = tcu._track_upshift_in_band(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=6840.0,
            gear=1,
            speed_ms=42.0 / 3.6,
            accel_raw=255,
            power_w=1297000.0,
            slip_fl=41.0,
            slip_fr=40.0,
            slip_rl=37.0,
            slip_rr=36.0,
        ),
        time.time(),
        offset=0.03,
    )

    assert shifted_or_held
    assert output.up == 0
    assert tcu._tcu_state == "TRACTION HOLD"
    assert events[-1]["event"] == "traction_hold"


def test_race_first_gear_traction_hold_continues_when_speed_catches_up_but_slips(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {1: 3.50, 2: 2.68}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8}
    tcu._calibrator._wheel_radius[car_key] = 0.34
    tcu._calibrator._wheel_radius_counts[car_key] = 8
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.854, "power ceiling")
    events = []
    tcu._logger.record_decision = events.append

    shifted_or_held = tcu._track_upshift_in_band(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=6840.0,
            gear=1,
            speed_ms=75.0 / 3.6,
            accel_raw=255,
            power_w=1297000.0,
            slip_fl=8.0,
            slip_fr=7.5,
            slip_rl=7.0,
            slip_rr=6.5,
        ),
        time.time(),
        offset=0.03,
    )

    assert shifted_or_held
    assert output.up == 0
    assert tcu._tcu_state == "TRACTION HOLD"
    assert events[-1]["event"] == "race_slip_hold"
    assert events[-1]["hold_reason"] == "upshift"


def test_race_first_gear_traction_hold_releases_when_speed_catches_up_cleanly(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {1: 3.50, 2: 2.68}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8}
    tcu._calibrator._wheel_radius[car_key] = 0.34
    tcu._calibrator._wheel_radius_counts[car_key] = 8
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.854, "power ceiling")

    shifted = tcu._track_upshift_in_band(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=6840.0,
            gear=1,
            speed_ms=75.0 / 3.6,
            accel_raw=255,
            power_w=1297000.0,
            slip_fl=0.8,
            slip_fr=0.7,
            slip_rl=0.7,
            slip_rr=0.6,
        ),
        time.time(),
        offset=0.03,
    )

    assert shifted
    assert output.up == 1
    assert tcu._tcu_state == "UPSHIFT"


def test_race_fuel_cut_escape_holds_during_severe_traction_loss(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key
    tcu._calibrator._ratios[car_key] = {1: 3.50, 2: 2.68}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8}
    tcu._calibrator._wheel_radius[car_key] = 0.34
    tcu._calibrator._wheel_radius_counts[car_key] = 8
    events = []
    tcu._logger.record_decision = events.append

    tcu._mode_race(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=6900.0,
            gear=1,
            speed_ms=42.0 / 3.6,
            accel_raw=255,
            power_w=-120000.0,
            slip_fl=41.0,
            slip_fr=40.0,
            slip_rl=37.0,
            slip_rr=36.0,
        ),
        time.time(),
    )

    assert output.up == 0
    assert tcu._tcu_state == "TRACTION HOLD"
    assert events[-1]["event"] == "race_slip_hold"
    assert events[-1]["hold_reason"] == "fuel cut"


def test_race_second_gear_fuel_cut_escape_holds_when_wheelspin_continues(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key
    events = []
    tcu._logger.record_decision = events.append

    tcu._mode_race(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=7190.0,
            gear=2,
            speed_ms=40.6 / 3.6,
            accel_raw=255,
            power_w=-390000.0,
            slip_fl=26.0,
            slip_fr=25.0,
            slip_rl=24.0,
            slip_rr=23.0,
        ),
        time.time(),
    )

    assert output.up == 0
    assert tcu._tcu_state == "TRACTION HOLD"
    assert events[-1]["event"] == "race_slip_hold"
    assert events[-1]["hold_reason"] == "fuel cut"


def test_race_fuel_cut_escape_still_shifts_when_traction_is_clean(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (4197, 7, 999)
    tcu._current_car_key = car_key

    tcu._mode_race(
        telemetry(
            car_ordinal=4197,
            car_class=7,
            pi=999,
            engine_max_rpm=8000.0,
            current_rpm=6900.0,
            gear=1,
            speed_ms=42.0 / 3.6,
            accel_raw=255,
            power_w=-120000.0,
            slip_fl=0.5,
            slip_fr=0.4,
            slip_rl=0.4,
            slip_rr=0.3,
        ),
        time.time(),
    )

    assert output.up == 1
    assert tcu._tcu_state == "FUEL CUT"


def test_power_ceiling_cannot_raise_learned_limiter_ceiling(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._rev_limiter.load(car_key, 15510.2)
    tcu._power_curve.max_high_power_rpm = lambda _car_key, min_peak_ratio=0.80: 15500.0

    td = telemetry(
        engine_max_rpm=16000.0,
        current_rpm=14500.0,
        gear=2,
        accel_raw=255,
    )

    ceiling_rpm = tcu._upshift_ceiling_pct(td) * td.engine_max_rpm

    assert ceiling_rpm <= 15310.2
    assert ceiling_rpm < 15480.0


def test_power_ceiling_holds_gear_when_landing_power_is_poor(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._rev_limiter.load(car_key, 7499.0)
    tcu._calibrator._ratios[car_key] = {1: 5.70, 2: 3.78}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8}
    tcu._rpm_rate_history.extend([3200.0, 3400.0, 3600.0])
    tcu._power_curve.power_at_rpm = lambda _car_key, rpm: 680.0 if rpm < 5000.0 else 1740.0

    td = telemetry(
        car_ordinal=1,
        car_class=5,
        pi=900,
        engine_max_rpm=8000.0,
        current_rpm=6900.0,
        gear=1,
        accel_raw=255,
    )

    base_pct = tcu._upshift_base_target_pct(td, 0.884, "power ceiling", 0.900)
    command_pct, source = tcu._upshift_command_target_pct(td, base_pct, "power ceiling")

    assert base_pct * td.engine_max_rpm > 7300.0
    assert command_pct * td.engine_max_rpm > 7100.0
    assert "lead" in source


def test_power_ceiling_uses_learned_actual_rpm_gain_for_lead(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._rev_limiter.load(car_key, 7499.0)
    tcu._calibrator._ratios[car_key] = {1: 5.70, 2: 3.78}
    tcu._calibrator._counts[car_key] = {1: 8, 2: 8}
    tcu._rpm_rate_history.extend([3200.0, 3400.0, 3600.0])
    tcu._power_curve.power_at_rpm = lambda _car_key, rpm: 680.0 if rpm < 5000.0 else 1740.0

    for idx, gain in enumerate((115.0, 125.0, 132.0), start=1):
        now = float(idx)
        tcu._shift_lag.record_shift_command(
            car_key,
            "UP",
            1,
            now,
            command_rpm=7200.0,
        )
        tcu._shift_lag.observe_command_frame(car_key, 1, 7200.0 + gain)
        tcu._shift_lag.observe_gear_change(car_key, 2, now + 0.045)

    td = telemetry(
        car_ordinal=1,
        car_class=5,
        pi=900,
        engine_max_rpm=8000.0,
        current_rpm=6900.0,
        gear=1,
        accel_raw=255,
    )

    base_pct = tcu._upshift_base_target_pct(td, 0.884, "power ceiling", 0.900)
    command_pct, source = tcu._upshift_command_target_pct(td, base_pct, "power ceiling")

    assert base_pct * td.engine_max_rpm > 7300.0
    assert base_pct * td.engine_max_rpm - command_pct * td.engine_max_rpm <= 220.0
    assert command_pct * td.engine_max_rpm > 7240.0
    assert "lead" in source


def test_power_ceiling_stays_conservative_when_landing_power_is_close(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._rev_limiter.load(car_key, 7499.0)
    tcu._calibrator._ratios[car_key] = {3: 2.70, 4: 2.25}
    tcu._calibrator._counts[car_key] = {3: 8, 4: 8}
    tcu._power_curve.power_at_rpm = lambda _car_key, _rpm: 1500.0

    td = telemetry(
        car_ordinal=1,
        car_class=5,
        pi=900,
        engine_max_rpm=8000.0,
        current_rpm=6900.0,
        gear=3,
        accel_raw=255,
    )

    assert tcu._upshift_base_target_pct(td, 0.884, "power ceiling", 0.900) == 0.884


def test_shift_lag_accepts_forza_execution_delay_samples():
    learner = ShiftLagLearner()
    car_key = (1, 5, 900)

    for idx, lag in enumerate((0.200, 0.220, 0.250), start=1):
        now = float(idx)
        learner.record_shift_command(car_key, "UP", 1, now)
        learner.observe_gear_change(car_key, 2, now + lag)

    assert abs(learner.get_upshift_lag(car_key) - 0.220) < 0.001


def test_shift_lag_rejects_unresponsive_auto_control_sample():
    learner = ShiftLagLearner()
    car_key = (1, 5, 900)

    learner.record_shift_command(car_key, "UP", 1, 1.0, command_rpm=6650.0)
    learner.observe_command_frame(car_key, 1, 6900.0)
    learner.observe_command_frame(car_key, 1, 7240.0)
    learner.observe_gear_change(car_key, 2, 1.245)

    assert learner.dump(car_key) is None
    assert learner.get_upshift_lag(car_key) == ShiftLagLearner.DEFAULT_UPSHIFT_LAG


def test_shift_lag_learns_and_persists_actual_upshift_rpm_gain():
    learner = ShiftLagLearner()
    car_key = (1, 5, 900)

    for idx, gain in enumerate((110.0, 125.0, 145.0), start=1):
        now = float(idx)
        learner.record_shift_command(car_key, "UP", 1, now, command_rpm=7000.0)
        learner.observe_command_frame(car_key, 1, 7000.0 + gain)
        learner.observe_gear_change(car_key, 2, now + 0.040)

    assert learner.get_upshift_rpm_gain(car_key, 1) == 125.0

    dumped = learner.dump(car_key)
    restored = ShiftLagLearner()
    restored.load(car_key, dumped)

    assert restored.get_upshift_rpm_gain(car_key, 1) == 125.0


def test_race_upshift_lead_caps_learned_200ms_lag(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._rpm_rate_history.extend([3000.0, 3200.0, 3400.0])

    for idx, lag in enumerate((0.200, 0.220, 0.250), start=1):
        now = float(idx)
        tcu._shift_lag.record_shift_command(car_key, "UP", 1, now)
        tcu._shift_lag.observe_gear_change(car_key, 2, now + lag)

    lead_rpm = tcu._upshift_lead_rpm(
        telemetry(
            engine_max_rpm=16000.0,
            current_rpm=14500.0,
            gear=2,
            accel_raw=255,
        )
    )

    assert 350.0 <= lead_rpm <= 420.0


def test_race_upshift_uses_power_peak_when_cross_unavailable(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)
    tcu._rev_limiter.load(car_key, 6840.0)
    tcu._power_curve.has_mature_data = lambda _car_key: True
    tcu._power_curve.has_power_lookup = lambda _car_key: False
    tcu._power_curve.peak_power_abs_rpm = lambda _car_key: 5300.0
    tcu._power_curve.max_high_power_rpm = lambda _car_key, min_peak_ratio=0.80: 6850.0

    tcu.process(telemetry(current_rpm=5600.0, gear=5, accel_raw=255))

    assert output.up == 1
    assert tcu._tcu_state == "UPSHIFT"
    assert tcu._tcu_state_sub == "power peak"


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


def test_optimal_shift_snapshot_uses_power_peak_fallback(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)
    tcu._power_curve.has_mature_data = lambda _car_key: True
    tcu._power_curve.has_power_lookup = lambda _car_key: False
    tcu._power_curve.peak_power_abs_rpm = lambda _car_key: 5300.0
    tcu._power_curve.max_high_power_rpm = lambda _car_key, min_peak_ratio=0.80: 6850.0

    snapshot = tcu._optimal_shift_snapshot(telemetry(current_rpm=5000.0, gear=5))

    assert snapshot["optimal_shift_from_gear"] == 5
    assert snapshot["optimal_shift_to_gear"] == 6
    assert snapshot["optimal_shift_rpm"] == 5540
    assert snapshot["optimal_shift_source"] == "power peak"


def test_learn_mode_guides_without_auto_shifting(tmp_path):
    tcu, output = make_tcu(tmp_path, "LEARN")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    seed_ratios(tcu, car_key)

    tcu.process(telemetry(current_rpm=7700.0, gear=5, accel_raw=255))

    assert output.up == 0
    assert output.down == 0
    assert tcu._tcu_state == "LEARNING"
    assert "Race mode will learn final shift RPM" in tcu._shift_hint


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
    assert "base model ready" in tcu._tcu_state_sub
    assert "Race loop samples" in tcu._tcu_state_sub
    assert "Race mode WOT 1-2, 2-3, 3-4" in tcu._shift_hint


def test_rev_limiter_ignores_positive_power_drag_plateau():
    detector = RevLimiterDetector()
    car_key = telemetry().car_key
    detector.load(car_key, 8000.0)

    rpms = [6749, 6801, 6755, 6797, 6761, 6792, 6766, 6787, 6772, 6782] * 4
    for i, rpm in enumerate(rpms):
        detector.observe(
            telemetry(
                current_rpm=float(rpm),
                gear=5,
                accel_raw=255,
                speed_ms=42.0,
                power_w=78000.0,
                torque_nm=110.0,
            ),
            last_downshift_time=-999.0,
            now=float(i) * 0.05,
        )

    assert detector.effective_redline(telemetry()) == 8000.0


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


def test_airtime_detector_uses_accel_y_freefall_signal():
    detector = AirtimeDetector()
    airborne = telemetry(
        speed_ms=22.0,
        accel_y=-8.5,
        suspension_norm_fl=0.5,
        suspension_norm_fr=0.5,
        suspension_norm_rl=0.5,
        suspension_norm_rr=0.5,
    )
    grounded = telemetry(speed_ms=22.0, accel_y=0.0, vel_y=0.0)

    for i in range(3):
        detector.update(airborne, now=300.0 + i * 0.016)

    assert detector.is_airborne

    detector.update(grounded, now=300.10)
    detector.update(grounded, now=300.12)

    assert not detector.is_airborne
    assert detector.just_landed


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


def test_launch_wheelspin_slip_does_not_count_as_airtime():
    detector = AirtimeDetector()
    launch_spin = telemetry(
        speed_ms=22.3 / 3.6,
        gear=1,
        accel_raw=255,
        accel_y=-0.64,
        vel_y=-0.30,
        slip_fl=11.45,
        slip_fr=9.69,
        slip_rl=9.99,
        slip_rr=7.07,
        suspension_norm_fl=0.193,
        suspension_norm_fr=0.224,
        suspension_norm_rl=0.653,
        suspension_norm_rr=0.712,
    )

    for i in range(5):
        detector.update(launch_spin, now=400.0 + i * 0.016)

    assert not detector.is_airborne
    assert not detector.just_landed


def test_airtime_detector_grounded_suspension_releases_even_with_vertical_speed():
    detector = AirtimeDetector()
    airborne = telemetry(
        speed_ms=65.0,
        suspension_norm_fl=0.0,
        suspension_norm_fr=0.0,
        suspension_norm_rl=0.0,
        suspension_norm_rr=0.0,
    )
    grounded = telemetry(
        speed_ms=65.0,
        vel_y=-1.22,
        suspension_norm_fl=0.591,
        suspension_norm_fr=0.584,
        suspension_norm_rl=0.650,
        suspension_norm_rr=0.641,
    )

    for i in range(3):
        detector.update(airborne, now=500.0 + i * 0.016)
    assert detector.is_airborne

    detector.update(grounded, now=500.10)
    detector.update(grounded, now=500.12)

    assert not detector.is_airborne
    assert detector.just_landed


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


def test_impact_shortens_post_shift_lock(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    now = time.time()
    tcu._lock_until = now + 1.0
    tcu._speed_history.append(120.0)

    tcu.process(telemetry(current_rpm=4300.0, gear=4, speed_ms=24.0, accel_raw=60))

    assert tcu._lock_until <= time.time() + 0.30
    assert tcu._no_downshift_until == 0.0


def test_offroad_uses_torque_power_down(tmp_path):
    tcu, output = make_tcu(tmp_path, "OFFROAD")
    seed_ratios(tcu)

    tcu.process(telemetry(current_rpm=3400.0, gear=4, accel_raw=150, speed_ms=18.0))

    assert output.down + output.double_down >= 1
    assert tcu._tcu_state == "TORQUE DOWN"


def test_offroad_does_not_wheelspin_upshift(tmp_path):
    tcu, output = make_tcu(tmp_path, "OFFROAD")
    seed_ratios(tcu)

    tcu.process(
        telemetry(
            current_rpm=1331.7,
            gear=1,
            speed_ms=20.6 / 3.6,
            accel_raw=255,
            power_w=123300.0,
            slip_fl=2.80,
            slip_fr=2.75,
            slip_rl=1.90,
            slip_rr=1.85,
            suspension_norm_fl=0.20,
            suspension_norm_fr=0.22,
            suspension_norm_rl=0.64,
            suspension_norm_rr=0.61,
        )
    )

    assert output.up == 0
    assert tcu._tcu_state != "WHEELSPIN"


def test_drift_keeps_single_downshift(tmp_path):
    tcu, output = make_tcu(tmp_path, "DRIFT")
    seed_ratios(tcu)

    tcu.process(telemetry(current_rpm=4200.0, gear=4, accel_raw=150, speed_ms=28.0))

    assert output.down == 1
    assert output.double_down == 0
    assert tcu._tcu_state == "DRIFT HOLD"


def test_shift_outcome_learner_moves_toward_better_side():
    learner = ShiftOutcomeLearner()
    car_key = (1, 5, 900)

    for reward in (12.0, 12.2, 12.1):
        learner.record_sample(car_key, 1, applied_offset_rpm=-40.0, reward_kmh_s=reward)
    for reward in (13.1, 13.4, 13.3):
        update = learner.record_sample(
            car_key,
            1,
            applied_offset_rpm=40.0,
            reward_kmh_s=reward,
        )

    assert update.changed
    assert update.offset_rpm == 25.0
    assert learner.base_offset_rpm(car_key, 1) == 25.0


def test_shift_outcome_waits_through_dirty_settle_before_sampling():
    learner = ShiftOutcomeLearner()
    car_key = (1, 5, 900)
    now = 100.0

    learner.record_command(
        car_key,
        1,
        now,
        command_rpm=7200.0,
        command_speed_kmh=130.0,
        target_rpm=7200.0,
        nominal_target_rpm=7200.0,
        applied_offset_rpm=0.0,
        source="power ceiling",
    )
    assert learner.confirm_upshift(
        car_key,
        2,
        now + 0.05,
        landing_rpm=4300.0,
        landing_speed_kmh=131.0,
        landing_power_ratio=0.72,
    )

    dirty_settle = telemetry(
        car_ordinal=1,
        car_class=5,
        pi=900,
        gear=2,
        speed_ms=132.0 / 3.6,
        current_rpm=4400.0,
        accel_raw=255,
        power_w=-100000.0,
    )
    assert learner.observe(dirty_settle, now + 0.12, clean=False) is None
    assert learner.sample_count(car_key, 1) == 0

    clean_pull = telemetry(
        car_ordinal=1,
        car_class=5,
        pi=900,
        gear=2,
        speed_ms=146.0 / 3.6,
        current_rpm=5200.0,
        accel_raw=255,
        power_w=900000.0,
    )
    update = learner.observe(clean_pull, now + 0.40, clean=True)

    assert update is not None
    assert update.sample_count == 1
    assert learner.sample_count(car_key, 1) == 1


def test_shift_outcome_needs_reward_comparison_before_adjusting():
    learner = ShiftOutcomeLearner()
    car_key = (1, 5, 900)

    learner.record_sample(
        car_key,
        1,
        applied_offset_rpm=0.0,
        reward_kmh_s=18.0,
        landing_power_ratio=0.72,
    )
    update = learner.record_sample(
        car_key,
        1,
        applied_offset_rpm=0.0,
        reward_kmh_s=18.4,
        landing_power_ratio=0.74,
    )

    assert not update.changed
    assert learner.base_offset_rpm(car_key, 1) == 0.0


def test_tcu_records_shift_outcome_after_post_shift_settle(tmp_path):
    tcu, _output = make_tcu(tmp_path, "RACE")
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._prev_gear = 1
    tcu._pending_upshift_gear = 2
    tcu._pending_upshift_until = time.time() + 0.8
    tcu._shift_outcome.record_command(
        car_key,
        1,
        time.time(),
        command_rpm=7200.0,
        command_speed_kmh=130.0,
        target_rpm=7200.0,
        nominal_target_rpm=7200.0,
        applied_offset_rpm=0.0,
        source="power ceiling",
    )

    tcu.process(
        telemetry(
            car_ordinal=car_key[0],
            car_class=car_key[1],
            pi=car_key[2],
            gear=2,
            current_rpm=4300.0,
            speed_ms=131.0 / 3.6,
            accel_raw=255,
            power_w=-100000.0,
        )
    )
    assert tcu._shift_outcome.sample_count(car_key, 1) == 0

    tcu._shift_outcome._pending_observation.confirm_time = time.time() - 0.40
    tcu.process(
        telemetry(
            car_ordinal=car_key[0],
            car_class=car_key[1],
            pi=car_key[2],
            gear=2,
            current_rpm=5200.0,
            speed_ms=146.0 / 3.6,
            accel_raw=255,
            power_w=900000.0,
        )
    )

    assert tcu._shift_outcome.sample_count(car_key, 1) == 1


def test_race_shift_outcome_offset_affects_upshift_target(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._shift_outcome._offsets[(car_key, 3)] = 100.0
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.75, "power cross")
    events = []
    tcu._logger.record_decision = events.append

    shifted = tcu._track_upshift_in_band(
        telemetry(current_rpm=6100.0, gear=3, accel_raw=255, speed_ms=35.0),
        time.time(),
        offset=0.03,
    )

    assert shifted
    assert output.up == 1
    assert events[-1]["upshift_base_target_rpm"] == 6100.0
    assert events[-1]["shift_outcome_offset_rpm"] == 100.0


def test_race_shift_outcome_offset_requires_clean_sample(tmp_path):
    tcu, output = make_tcu(tmp_path, "RACE")
    seed_ratios(tcu)
    car_key = (1, 5, 900)
    tcu._current_car_key = car_key
    tcu._shift_outcome._offsets[(car_key, 3)] = 100.0
    tcu._performance_upshift_target_pct = lambda _td, _offset: (0.75, "power cross")

    shifted = tcu._track_upshift_in_band(
        telemetry(
            current_rpm=6020.0,
            gear=3,
            accel_raw=255,
            speed_ms=35.0,
            slip_fl=0.9,
        ),
        time.time(),
        offset=0.03,
    )

    assert shifted
    assert output.up == 1
    assert tcu._shift_outcome._pending_command is None


def test_shift_outcome_persists_and_restores(tmp_path):
    profiles = ProfileStore(tmp_path / "profiles.json")
    config = ConfigStore(tmp_path / "config.json")
    config.set("current_mode", "RACE")
    car_key = (1, 5, 900)

    tcu = TCULogic(CountingOutput(), profiles, config, TelemetryLogger())
    tcu._current_car_key = car_key
    tcu._shift_outcome._offsets[(car_key, 2)] = -50.0
    tcu._shift_outcome.record_sample(car_key, 2, applied_offset_rpm=-40.0, reward_kmh_s=12.0)
    tcu.save_profiles()

    restored = TCULogic(CountingOutput(), profiles, config, TelemetryLogger())
    restored._load_profiles(car_key)

    assert restored._shift_outcome.base_offset_rpm(car_key, 2) == -50.0
    assert restored._shift_outcome.sample_count(car_key, 2) == 1


def test_old_profile_keeps_base_learning_but_drops_polluted_dynamic_learning(tmp_path):
    profiles = ProfileStore(tmp_path / "profiles.json")
    config = ConfigStore(tmp_path / "config.json")
    config.set("current_mode", "RACE")
    car_key = (1, 5, 900)
    profiles.set(
        car_key,
        {
            "telemetry_schema": "fh6-dataout-2026-05-15-v8-smart-margin",
            "gear_ratios": {"1": 5.7, "2": 3.8},
            "gear_counts": {"1": 8, "2": 8},
            "gear_ratio_basis": "engine_rad_per_driven_wheel_rad",
            "wheel_radius": 0.34,
            "wheel_radius_count": 8,
            "power_curve": {
                "format": "power_bins_v2",
                "max_rpm": 8000.0,
                "best_power_hp": 1700.0,
                "bin_rpm": 50,
                "bins": {},
            },
            "rev_limiter": 7249.0,
            "shift_lag": {"upshift_lags": [0.09, 0.10, 0.11]},
            "shift_outcome": {
                "offsets_by_gear": {"3": -200.0},
                "samples_by_gear": {
                    "3": [
                        {
                            "applied_offset_rpm": -200.0,
                            "reward_kmh_s": 20.0,
                        }
                    ]
                },
            },
        },
    )

    restored = TCULogic(CountingOutput(), profiles, config, TelemetryLogger())
    restored._load_profiles(car_key)

    assert restored._calibrator.ratio_for_gear(car_key, 1) == 5.7
    assert restored._power_curve.dump(car_key) is not None
    assert restored._rev_limiter.dump(car_key) is None
    assert restored._shift_lag.dump(car_key) is None
    assert restored._shift_outcome.dump(car_key) is None


def test_shift_outcome_rejects_offroad_samples(tmp_path):
    tcu, _output = make_tcu(tmp_path, "OFFROAD")
    seed_ratios(tcu)

    assert not tcu._race_shift_outcome_sample_clean(
        telemetry(current_rpm=7200.0, gear=3, accel_raw=255, speed_ms=35.0)
    )
