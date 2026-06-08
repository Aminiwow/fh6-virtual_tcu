import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC

import keyboard

from virtual_tcu.config.constants import Cfg
from virtual_tcu.config.store import ConfigStore
from virtual_tcu.core.mode import MODE_ORDER, Mode
from virtual_tcu.deps import WINSOUND_OK, winsound
from virtual_tcu.detectors.airtime import AirtimeDetector
from virtual_tcu.detectors.reverse_hold import ReverseHoldDetector
from virtual_tcu.detectors.yaw_transient import YawTransientDetector
from virtual_tcu.input.interface import OutputInterface
from virtual_tcu.integrations.discord import DiscordRPC
from virtual_tcu.learning.drive_style import DriveStyleTracker
from virtual_tcu.learning.gear_ratio import GearRatioCalibrator
from virtual_tcu.learning.power_curve import PowerCurveDetector
from virtual_tcu.learning.rev_limiter import RevLimiterDetector
from virtual_tcu.learning.shift_lag import ShiftLagLearner
from virtual_tcu.learning.shift_outcome import ShiftOutcomeLearner, ShiftOutcomeUpdate
from virtual_tcu.state.graph_buffer import GraphBuffer
from virtual_tcu.state.session_stats import SessionStats
from virtual_tcu.state.shift_history import ShiftHistory
from virtual_tcu.state.watchdog import Watchdog
from virtual_tcu.storage.profiles import ProfileStore
from virtual_tcu.telemetry.logger import TelemetryLogger
from virtual_tcu.telemetry.model import Telemetry


class TCULogic:
    PROFILE_SCHEMA = "fh6-dataout-2026-05-15-v9-clean-limiter-outcome"
    COMPAT_PROFILE_SCHEMAS = {
        PROFILE_SCHEMA,
        "fh6-dataout-2026-05-15-v8-smart-margin",
        "fh6-dataout-2026-05-15-v7-smart-limiter",
    }

    def __init__(
        self,
        kb: OutputInterface,
        profiles: ProfileStore,
        config: ConfigStore,
        logger: TelemetryLogger,
    ):
        self._kb = kb
        self._profiles = profiles
        self._config = config
        self._logger = logger
        self._mode_lock = threading.Lock()
        self._data_lock = threading.RLock()

        # Dedicated executors keep slow integrations off the telemetry loop.
        self._audio_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TCU_Audio")
        self._discord_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TCU_Discord")

        try:
            self._mode = Mode(str(config.get("current_mode", "RACE")).upper())
        except (ValueError, KeyError):
            self._mode = Mode.RACE

        self._last_drive_mode = (
            self._mode if self._mode not in (Mode.LEARN, Mode.MANUAL) else Mode.RACE
        )
        self._last_processed_mode = self._mode

        self._lock_until = 0.0
        self._no_upshift_until = 0.0
        self._shift_count = 0
        self._peak_rpm = 0.0
        self._peak_g = 0.0
        self._turbo_bar = 0.0

        self._brake_history = deque(maxlen=10)
        self._throttle_history = deque(maxlen=6)
        self._speed_history = deque(maxlen=20)
        self._rpm_rate_history = deque(maxlen=8)
        self._brake_raw_history = deque(maxlen=10)
        self._throttle_raw_history = deque(maxlen=10)
        self._no_downshift_until = 0.0
        self._no_predictive_until = 0.0
        self._last_brake_time = 0.0
        self._last_hard_brake_time = 0.0
        self._last_downshift_time = 0.0
        self._last_packet_time = 0.0
        self._prev_gear = -1
        self._we_shifted = False
        self._last_rpm_sample: tuple[float, float] | None = None
        self._pending_upshift_gear: int | None = None
        self._pending_upshift_from_gear: int | None = None
        self._pending_upshift_car_key: tuple | None = None
        self._pending_upshift_until = 0.0
        self._failed_upshift_attempts: dict[tuple, dict[int, int]] = {}
        self._top_gear_by_car: dict[tuple, int] = {}
        self._last_saved_profile_at = 0.0
        self._last_profile_signature: tuple | None = None
        self._last_valid_telemetry: Telemetry | None = None

        self._reverse_lock_until = 0.0
        self._current_car_key: tuple | None = None

        self._reverse_hold = ReverseHoldDetector(kb)
        self._calibrator = GearRatioCalibrator()
        self._power_curve = PowerCurveDetector()
        self._airtime = AirtimeDetector()
        self._yaw_transient = YawTransientDetector()
        self._drive_style = DriveStyleTracker()
        self._rev_limiter = RevLimiterDetector()
        self._shift_lag = ShiftLagLearner()
        self._shift_outcome = ShiftOutcomeLearner()
        self._shift_history = ShiftHistory()
        self._session_stats = SessionStats()
        self._graph_buffer = GraphBuffer()
        self._watchdog = Watchdog()
        self._discord_rpc = DiscordRPC() if config.get("feat_discord_rpc") else None
        self._last_decision = {"rule": "", "reason": "", "blocked_by": None}
        self._last_traction_hold_log_at = 0.0
        self._race_slip_hold_until = 0.0
        self._last_race_slip_hold_log_at = 0.0

        self._tcu_state = "STANDBY"
        self._tcu_state_sub = ""
        self._attitude = "NEUTRAL"
        self._attitude_sub = ""
        self._shift_hint = ""
        self._shift_advice = ""
        self._grip_usage = 0.0
        self._g_lat = 0.0
        self._g_lon = 0.0

        self._launch_armed = False
        self._cornering_locked = False
        self._down_held = False
        self._up_held = False
        self._paddle_keys: tuple[str, str] = ("", "")

        if Cfg.REVERSE_HOLD_MS > 0:
            self._setup_paddle_listeners()

    def save_profiles(self):
        """Persist all learning data to ProfileStore for the current car."""
        ck = self._current_car_key
        if ck is None or ck[0] <= 0:
            return
        profile: dict = {}
        gr = self._calibrator.dump(ck)
        if gr is not None:
            profile["gear_ratios"] = gr["ratios"]
            profile["gear_counts"] = gr["counts"]
            profile["gear_ratio_basis"] = gr["basis"]
            profile["wheel_radius"] = gr["wheel_radius"]
            profile["wheel_radius_count"] = gr["wheel_radius_count"]
        pc = self._power_curve.dump(ck)
        if pc is not None:
            profile["power_curve"] = pc
        rl = self._rev_limiter.dump(ck)
        if rl is not None:
            profile["rev_limiter"] = rl
        sl = self._shift_lag.dump(ck)
        if sl is not None:
            profile["shift_lag"] = sl
        so = self._shift_outcome.dump(ck)
        if so is not None:
            profile["shift_outcome"] = so
        if profile:
            from datetime import datetime

            profile["telemetry_schema"] = self.PROFILE_SCHEMA
            profile["updated_at"] = datetime.now(UTC).isoformat()
            self._profiles.set(ck, profile)
            self._last_saved_profile_at = time.time()
            self._last_profile_signature = self._profile_signature(ck)

    def _profile_signature(self, ck: tuple) -> tuple:
        ratios = self._calibrator.get_ratios(ck)
        counts = self._calibrator._counts.get(ck, {})
        ratio_signature = tuple(
            (gear, round(ratio, 4), counts.get(gear, 0))
            for gear, ratio in sorted(ratios.items())
        )
        progress = self._power_curve.learning_progress(ck)
        return (
            self._rev_limiter.dump(ck),
            ratio_signature,
            self._shift_lag.dump(ck),
            self._shift_outcome.dump(ck),
            progress.get("samples", 0),
            progress.get("points", 0),
            progress.get("min_rpm"),
            progress.get("max_rpm"),
            self._power_curve.has_mature_data(ck),
            round(self._power_curve.confidence(ck), 2),
        )

    def _save_profiles_if_changed(self, now: float, *, force: bool = False):
        ck = self._current_car_key
        if ck is None or ck[0] <= 0:
            return
        sig = self._profile_signature(ck)
        if sig == self._last_profile_signature and not force:
            return
        min_interval = 2.0 if force else 15.0
        if now - self._last_saved_profile_at < min_interval:
            return
        self.save_profiles()

    def _load_profiles(self, ck: tuple):
        """Restore learning data from ProfileStore for *ck*."""
        data = self._profiles.get(ck)
        if data is None:
            return
        schema = data.get("telemetry_schema")
        if schema not in self.COMPAT_PROFILE_SCHEMAS:
            return
        trusted_dynamic_learning = schema == self.PROFILE_SCHEMA
        if "gear_ratios" in data:
            self._calibrator.load(
                ck,
                {
                    "ratios": data["gear_ratios"],
                    "counts": data.get("gear_counts", {}),
                    "basis": data.get("gear_ratio_basis"),
                    "wheel_radius": data.get("wheel_radius"),
                    "wheel_radius_count": data.get("wheel_radius_count", 0),
                },
            )
        if "power_curve" in data:
            self._power_curve.load(ck, data["power_curve"])
        if trusted_dynamic_learning and "rev_limiter" in data:
            self._rev_limiter.load(ck, data["rev_limiter"])
        if trusted_dynamic_learning and "shift_lag" in data:
            self._shift_lag.load(ck, data["shift_lag"])
        if trusted_dynamic_learning and "shift_outcome" in data:
            self._shift_outcome.load(ck, data["shift_outcome"])

    def shutdown(self):
        self.save_profiles()
        self._audio_executor.shutdown(wait=False)
        self._discord_executor.shutdown(wait=False)
        if self._discord_rpc:
            self._discord_rpc.close()
        self._teardown_paddle_listeners()

    def _setup_paddle_listeners(self):
        kb = self._kb
        down_key = kb.key_down
        up_key = kb.key_up

        if not down_key or not up_key:
            return

        if (down_key, up_key) == self._paddle_keys:
            return

        self._teardown_paddle_listeners()
        self._down_held = False
        self._up_held = False

        def on_down_press(_e):
            if hasattr(kb, "is_self_press") and not kb.is_self_press(down_key):
                self._down_held = True

        def on_down_release(_e):
            if hasattr(kb, "is_self_press") and not kb.is_self_press(down_key):
                self._down_held = False

        def on_up_press(_e):
            if hasattr(kb, "is_self_press") and not kb.is_self_press(up_key):
                self._up_held = True

        def on_up_release(_e):
            if hasattr(kb, "is_self_press") and not kb.is_self_press(up_key):
                self._up_held = False

        try:
            keyboard.on_press_key(down_key, on_down_press)
            keyboard.on_release_key(down_key, on_down_release)
            keyboard.on_press_key(up_key, on_up_press)
            keyboard.on_release_key(up_key, on_up_release)
            self._paddle_keys = (down_key, up_key)
        except Exception as e:
            print(f"[Paddle hooks] failed: {e}")

    def _teardown_paddle_listeners(self):
        down_key, up_key = self._paddle_keys
        for key in (down_key, up_key):
            if not key:
                continue
            try:
                keyboard.unhook_key(key)
            except Exception:
                pass
        self._paddle_keys = ("", "")

    def refresh_shift_keys(self):
        if Cfg.REVERSE_HOLD_MS > 0:
            self._setup_paddle_listeners()

    @property
    def mode(self) -> Mode:
        with self._mode_lock:
            return self._mode

    def set_mode(self, mode_name: str):
        try:
            new_mode = Mode(str(mode_name).upper())
            with self._mode_lock:
                if new_mode in (Mode.LEARN, Mode.MANUAL) and self._mode not in (
                    Mode.LEARN,
                    Mode.MANUAL,
                ):
                    self._last_drive_mode = self._mode
                self._mode = new_mode
            self._config.set("current_mode", new_mode.value)
        except ValueError:
            pass

    def cycle_mode(self):
        with self._mode_lock:
            idx = MODE_ORDER.index(self._mode)
            new_mode = MODE_ORDER[(idx + 1) % len(MODE_ORDER)]
            if new_mode in (Mode.LEARN, Mode.MANUAL) and self._mode not in (
                Mode.LEARN,
                Mode.MANUAL,
            ):
                self._last_drive_mode = self._mode
            self._mode = new_mode
            new_value = self._mode.value
        self._config.set("current_mode", new_value)

    @property
    def shift_count(self) -> int:
        with self._data_lock:
            return self._shift_count

    def snapshot(self, td: Telemetry | None) -> dict:
        with self._data_lock:
            snapshot_td = td if td is not None and not self._invalid_drive_packet(td) else None
            using_cached_car = False
            if snapshot_td is None and self._last_valid_telemetry is not None:
                snapshot_td = self._last_valid_telemetry
                using_cached_car = True

            if snapshot_td is None:
                return {
                    "gear": -1,
                    "speed_kmh": 0,
                    "rpm": 0,
                    "rpm_max": 0,
                    "rpm_pct": 0,
                    "throttle": 0,
                    "brake": 0,
                    "is_race_on": False,
                    "tcu_state": "OFFLINE",
                    "tcu_state_sub": "no telemetry",
                    "power_kw": 0,
                    "torque_nm": 0,
                    "turbo_bar": 0,
                    "drivetrain": "-",
                    "attitude": "NEUTRAL",
                    "attitude_sub": "",
                    "g_lat": 0,
                    "g_lon": 0,
                    "grip_usage": 0,
                    "shift_hint": "",
                    "shift_advice": "",
                    "peak_rpm": self._peak_rpm,
                    "peak_g": self._peak_g,
                    "calibrated": False,
                    "log_status": self._logger.status,
                    "power_curve_learned": False,
                    "shift_history": [],
                    "session_stats": self._session_stats.snapshot(),
                    "watchdog_stuck": self._watchdog.check(),
                    "drive_style_index": 0.0,
                    "drive_style_regime": "CRUISE",
                    "airborne": False,
                    "landing_recovery": False,
                    "yaw_transient": False,
                    "peak_power_rpm_pct": None,
                    "peak_torque_rpm_pct": None,
                    "power_curve_available": False,
                    "power_curve_confidence": 0.0,
                    "optimal_shift_rpm": None,
                    "optimal_shift_rpm_pct": None,
                    "optimal_shift_from_gear": None,
                    "optimal_shift_to_gear": None,
                    "optimal_shift_source": "",
                    "shift_guide": self._empty_shift_guide(),
                    "car_ordinal": self._current_car_key[0] if self._current_car_key else 0,
                    "car_class": self._current_car_key[1] if self._current_car_key else 0,
                    "pi": self._current_car_key[2] if self._current_car_key else 0,
                    "using_cached_car": False,
                }
            td = snapshot_td
            optimal_shift = self._optimal_shift_snapshot(td)
            shift_guide = self._shift_guide_snapshot(td)
            state = "STANDBY" if using_cached_car else self._tcu_state
            sub_state = "last car cached" if using_cached_car else self._tcu_state_sub
            return {
                "gear": td.gear,
                "speed_kmh": td.speed_kmh,
                "rpm": td.current_rpm,
                "rpm_max": td.engine_max_rpm,
                "rpm_pct": td.rpm_pct,
                "throttle": td.throttle,
                "brake": td.brake,
                "is_race_on": bool(td.is_race_on),
                "tcu_state": state,
                "tcu_state_sub": sub_state,
                "power_kw": td.power_w / 1000.0,
                "torque_nm": td.torque_nm,
                "turbo_bar": self._turbo_bar,
                "drivetrain": td.drivetrain_name,
                "attitude": self._attitude,
                "attitude_sub": self._attitude_sub,
                "g_lat": self._g_lat,
                "g_lon": self._g_lon,
                "grip_usage": self._grip_usage,
                "shift_hint": self._shift_hint,
                "shift_advice": self._shift_advice,
                "peak_rpm": self._peak_rpm,
                "peak_g": self._peak_g,
                "calibrated": self._calibrator.has_data(td.car_key),
                "power_curve_available": self._power_curve.has_data(td.car_key),
                "power_curve_learned": self._power_curve.has_mature_data(td.car_key),
                "power_curve_confidence": self._power_curve.confidence(td.car_key),
                "log_status": self._logger.status,
                "shift_history": self._shift_history.snapshot(),
                "session_stats": self._session_stats.snapshot(),
                "watchdog_stuck": self._watchdog.check(),
                "car_ordinal": td.car_ordinal,
                "car_class": td.car_class,
                "pi": td.pi,
                "drive_style_index": round(self._drive_style.index, 2),
                "drive_style_regime": self._drive_style.regime,
                "airborne": self._airtime.is_airborne,
                "landing_recovery": self._airtime.landing_recovery(),
                "yaw_transient": self._yaw_transient.is_blocking,
                "peak_power_rpm_pct": self._power_curve.peak_power_rpm(td.car_key),
                "peak_torque_rpm_pct": self._power_curve.peak_torque_rpm(td.car_key),
                **optimal_shift,
                "shift_guide": shift_guide,
                "using_cached_car": using_cached_car,
            }

    @staticmethod
    def _empty_shift_guide() -> dict:
        return {
            "available": False,
            "learned": False,
            "confidence": 0.0,
            "sample_count": 0,
            "bin_count": 0,
            "rpm_min": None,
            "rpm_max_seen": None,
            "engine_max_rpm": None,
            "peak_hp": None,
            "peak_hp_rpm": None,
            "peak_torque_nm": None,
            "peak_torque_rpm": None,
            "shift_outcome_total_samples": 0,
            "shift_outcome_ready_gears": 0,
            "shift_outcome_gears": [],
            "curve": [],
            "gears": [],
        }

    def _shift_guide_td(self, td: Telemetry, gear: int) -> Telemetry:
        peak_rpm = self._power_curve.peak_power_abs_rpm(td.car_key)
        current_rpm = peak_rpm if peak_rpm is not None else td.engine_max_rpm * 0.75
        return Telemetry(
            is_race_on=td.is_race_on,
            engine_max_rpm=td.engine_max_rpm,
            current_rpm=current_rpm,
            gear=gear,
            car_ordinal=td.car_ordinal,
            car_class=td.car_class,
            pi=td.pi,
            idle_rpm=td.idle_rpm,
            drivetrain=td.drivetrain,
            accel_raw=255,
            brake_raw=0,
        )

    def _guide_upshift_target(self, td: Telemetry, gear: int) -> dict | None:
        ratios = self._calibrator.get_ratios(td.car_key)
        if gear < 1 or gear >= 10 or gear not in ratios or gear + 1 not in ratios:
            return None
        if td.engine_max_rpm <= 0:
            return None

        guide_td = self._shift_guide_td(td, gear)
        target = self._learned_power_upshift_target_pct(guide_td, offset=0.03)
        if target is None:
            return None

        target_pct, source = target
        target_pct = self._upshift_base_target_pct(
            guide_td,
            target_pct,
            source,
            self._upshift_ceiling_pct(guide_td),
        )
        upshift_rpm = target_pct * td.engine_max_rpm
        upshift_speed = self._calibrator.speed_for_rpm(td.car_key, gear, upshift_rpm)
        landing_rpm = upshift_rpm * ratios[gear + 1] / ratios[gear]
        return {
            "rpm": round(upshift_rpm),
            "rpm_pct": round(target_pct, 4),
            "speed_kmh": round(upshift_speed, 1) if upshift_speed is not None else None,
            "source": source,
            "landing_rpm": round(landing_rpm),
            "power_hp": self._round_optional(
                self._power_curve.power_at_rpm(td.car_key, upshift_rpm),
                1,
            ),
            "landing_power_hp": self._round_optional(
                self._power_curve.power_at_rpm(td.car_key, landing_rpm),
                1,
            ),
        }

    @staticmethod
    def _round_optional(value: float | None, digits: int = 0) -> float | int | None:
        if value is None:
            return None
        rounded = round(value, digits)
        return int(rounded) if digits == 0 else rounded

    def _shift_guide_snapshot(self, td: Telemetry) -> dict:
        guide = self._empty_shift_guide()
        if td.car_key[0] <= 0:
            return guide

        progress = self._power_curve.learning_progress(td.car_key)
        curve = self._power_curve.curve_points(td.car_key)
        ratios = self._calibrator.get_ratios(td.car_key)
        counts = self._calibrator._counts.get(td.car_key, {})
        gear_targets = {
            gear: target
            for gear in sorted(ratios)
            if (target := self._guide_upshift_target(td, gear)) is not None
        }

        peak_hp_point = max(curve, key=lambda point: point["hp"], default=None)
        peak_torque_point = max(curve, key=lambda point: point["torque_nm"], default=None)
        max_rpm = td.engine_max_rpm if td.engine_max_rpm > 0 else progress.get("max_rpm")

        gears = []
        outcome_gears = []
        for gear in sorted(ratios):
            if gear < 1 or gear > 10:
                continue
            target = gear_targets.get(gear)
            next_gear = gear + 1 if gear + 1 in ratios else None
            outcome = (
                self._shift_outcome.status(td.car_key, gear)
                if next_gear is not None
                else {
                    "samples": 0,
                    "offset_rpm": 0.0,
                    "active_offset_rpm": 0.0,
                    "ready": False,
                    "recent_reward_kmh_s": None,
                }
            )
            if next_gear is not None:
                outcome_gears.append(
                    {
                        "gear": gear,
                        "to_gear": next_gear,
                        **outcome,
                    }
                )
            min_speed = gear_targets.get(gear - 1, {}).get("speed_kmh") if gear > 1 else 0.0
            max_speed = target.get("speed_kmh") if target else None
            if max_speed is None and max_rpm:
                ceiling_td = self._shift_guide_td(td, gear)
                max_speed = self._calibrator.speed_for_rpm(
                    td.car_key,
                    gear,
                    self._upshift_ceiling_pct(ceiling_td) * max_rpm,
                )
            entry_rpm = (
                self._calibrator.project_rpm_at_speed(td.car_key, gear, min_speed)
                if min_speed is not None
                else None
            )

            gears.append(
                {
                    "gear": gear,
                    "ratio": round(ratios[gear], 4),
                    "samples": counts.get(gear, 0),
                    "speed_min_kmh": round(min_speed, 1) if min_speed is not None else None,
                    "speed_max_kmh": round(max_speed, 1) if max_speed is not None else None,
                    "entry_rpm": round(entry_rpm) if entry_rpm is not None else None,
                    "upshift_rpm": target.get("rpm") if target else None,
                    "upshift_rpm_pct": target.get("rpm_pct") if target else None,
                    "upshift_speed_kmh": target.get("speed_kmh") if target else None,
                    "next_gear": next_gear,
                    "landing_rpm": target.get("landing_rpm") if target else None,
                    "power_hp": target.get("power_hp") if target else None,
                    "landing_power_hp": target.get("landing_power_hp") if target else None,
                    "source": target.get("source") if target else "top or learning",
                    "shift_outcome_samples": outcome["samples"],
                    "shift_outcome_offset_rpm": outcome["offset_rpm"],
                    "shift_outcome_active_offset_rpm": outcome["active_offset_rpm"],
                    "shift_outcome_ready": outcome["ready"],
                    "shift_outcome_recent_reward_kmh_s": outcome["recent_reward_kmh_s"],
                }
            )

        outcome_total_samples = sum(int(row["samples"]) for row in outcome_gears)
        outcome_ready_gears = sum(1 for row in outcome_gears if row["ready"])
        guide.update(
            {
                "available": bool(curve or ratios),
                "learned": self._power_curve.has_mature_data(td.car_key)
                and self._power_curve.has_power_lookup(td.car_key)
                and len(ratios) >= 2,
                "confidence": round(self._power_curve.confidence(td.car_key), 3),
                "sample_count": int(progress.get("samples", 0)),
                "bin_count": int(progress.get("points", 0)),
                "rpm_min": progress.get("min_rpm"),
                "rpm_max_seen": progress.get("max_rpm"),
                "engine_max_rpm": round(max_rpm) if max_rpm else None,
                "peak_hp": peak_hp_point["hp"] if peak_hp_point else None,
                "peak_hp_rpm": peak_hp_point["rpm"] if peak_hp_point else None,
                "peak_torque_nm": peak_torque_point["torque_nm"] if peak_torque_point else None,
                "peak_torque_rpm": peak_torque_point["rpm"] if peak_torque_point else None,
                "shift_outcome_total_samples": outcome_total_samples,
                "shift_outcome_ready_gears": outcome_ready_gears,
                "shift_outcome_gears": outcome_gears,
                "curve": curve,
                "gears": gears,
            }
        )
        return guide

    def snapshot_graph(self) -> list:
        with self._data_lock:
            return self._graph_buffer.snapshot()

    def clear_current_car_learning(self) -> dict:
        with self._data_lock:
            car_key = self._current_car_key
            if car_key is None and self._last_valid_telemetry is not None:
                car_key = self._last_valid_telemetry.car_key
            if car_key is None or car_key[0] <= 0:
                return {"ok": False, "error": "no_current_car"}

            self._calibrator.reset_car(car_key)
            self._power_curve.reset_car(car_key)
            self._rev_limiter.reset_car(car_key)
            self._shift_lag.reset_car(car_key)
            self._shift_outcome.reset_car(car_key)
            profile_deleted = self._profiles.delete(car_key)

            if self._current_car_key == car_key:
                self._last_profile_signature = self._profile_signature(car_key)
                self._last_saved_profile_at = time.time()

            td = self._last_valid_telemetry or Telemetry(
                car_ordinal=car_key[0],
                car_class=car_key[1],
                pi=car_key[2],
            )
            self._record_decision("clear_learning", td, profile_deleted=profile_deleted)
            return {
                "ok": True,
                "car_key": list(car_key),
                "profile_deleted": profile_deleted,
            }

    def process(self, td: Telemetry, raw_packet: bytes | None = None):
        with self._data_lock:
            self._process_internal(td, raw_packet)

    def _process_internal(self, td: Telemetry, raw_packet: bytes | None):
        now = time.time()

        dt = now - self._last_packet_time if self._last_packet_time > 0.0 else 0.016
        dt = max(0.001, min(dt, 0.100))

        if self._last_packet_time > 0.0 and (now - self._last_packet_time) > 0.8:
            self._prev_gear = td.gear
            self._no_downshift_until = 0.0
            self._no_upshift_until = 0.0
            self._lock_until = 0.0
            self._no_predictive_until = 0.0
            self._reverse_lock_until = 0.0
            self._launch_armed = False
            self._last_hard_brake_time = 0.0
            self._brake_history.clear()
            self._throttle_history.clear()
            self._speed_history.clear()
            self._rpm_rate_history.clear()
            self._brake_raw_history.clear()
            self._throttle_raw_history.clear()
            self._last_rpm_sample = None
            self._pending_upshift_gear = None
            self._pending_upshift_from_gear = None
            self._pending_upshift_car_key = None
            self._pending_upshift_until = 0.0
            self._shift_outcome.cancel_pending()
            self._tcu_state = "RESUMING"
            self._tcu_state_sub = "from menu/pause"

        self._last_packet_time = now

        if self._invalid_drive_packet(td):
            self._reset_for_invalid_drive_packet()
            self._tcu_state = "NO DATA"
            self._tcu_state_sub = "waiting telemetry"
            return

        self._last_valid_telemetry = td

        if td.is_shifting:
            self._tcu_state = "SHIFTING"
            self._tcu_state_sub = "Forza mid-shift"
            return

        self._shift_lag.observe_command_frame(td.car_key, td.gear, td.current_rpm)

        if td.gear != self._prev_gear and td.gear > 0 and self._prev_gear > 0:
            # 记录档位变化用于延迟学习
            self._shift_lag.observe_gear_change(td.car_key, td.gear, now)
            self._observe_higher_gear(td)

            if self._pending_upshift_gear is not None and td.gear >= self._pending_upshift_gear:
                pending_gear = self._pending_upshift_gear
                from_gear = max(1, pending_gear - 1)
                self._shift_outcome.confirm_upshift(
                    td.car_key,
                    td.gear,
                    now,
                    landing_rpm=td.current_rpm,
                    landing_speed_kmh=td.speed_kmh,
                    landing_power_ratio=self._shift_outcome_landing_power_ratio(td, from_gear),
                )
                self._pending_upshift_gear = None
                self._pending_upshift_from_gear = None
                self._pending_upshift_car_key = None
                self._pending_upshift_until = 0.0
                hold_s = self._post_upshift_confirm_hold_s(td)
                self._lock_until = min(self._lock_until, now + hold_s)
                self._no_upshift_until = min(self._no_upshift_until, now + hold_s)
                self._record_decision(
                    "upshift_confirm",
                    td,
                    pending_gear=pending_gear,
                    post_upshift_hold_s=round(hold_s, 3),
                )
            elif not self._we_shifted:
                airborne = self._config.get("feat_airtime_lock") and self._airtime.is_airborne
                external_downshift = td.gear < self._prev_gear
                if td.brake < 0.30 and not airborne:
                    self._no_downshift_until = max(self._no_downshift_until, now + 0.8)
                if not airborne:
                    hold_s = (
                        2.5
                        if external_downshift and self.mode == Mode.RACE
                        else 1.5
                        if external_downshift
                        else 0.5
                    )
                    self._no_upshift_until = max(self._no_upshift_until, now + hold_s)
                self._shift_outcome.cancel_pending()
        if self._pending_upshift_gear is not None and now > self._pending_upshift_until:
            self._record_unresponsive_upshift(td)
            self._pending_upshift_gear = None
            self._pending_upshift_from_gear = None
            self._pending_upshift_car_key = None
            self._pending_upshift_until = 0.0
            self._shift_outcome.cancel_pending()
        self._prev_gear = td.gear
        self._we_shifted = False

        self._brake_history.append(td.brake)
        self._throttle_history.append(td.throttle)
        self._speed_history.append(td.speed_kmh)
        self._brake_raw_history.append(td.brake)
        self._throttle_raw_history.append(td.throttle)

        td.accel_raw = int(
            (sum(self._throttle_history) / max(1, len(self._throttle_history))) * 255
        )
        td.brake_raw = int((sum(self._brake_history) / max(1, len(self._brake_history))) * 255)

        if td.brake > 0.15:
            self._last_brake_time = now
        if td.brake > 0.50:
            self._last_hard_brake_time = now

        if td.current_rpm > self._peak_rpm:
            self._peak_rpm = td.current_rpm

        self._g_lat = td.accel_x / 9.81
        self._g_lon = td.accel_z / 9.81
        g_total = (self._g_lat**2 + self._g_lon**2) ** 0.5
        if g_total > self._peak_g:
            self._peak_g = g_total

        ck = td.car_key
        if ck[0] > 0 and ck != self._current_car_key:
            # Save previous car's learned state before switching.
            if self._current_car_key is not None:
                self.save_profiles()
            self._current_car_key = ck
            self._pending_upshift_gear = None
            self._pending_upshift_from_gear = None
            self._pending_upshift_car_key = None
            self._pending_upshift_until = 0.0
            self._peak_rpm = 0.0
            self._peak_g = 0.0
            self._last_profile_signature = None
            # Reset per-tune learning data so stale ratios / curves from a
            # different build don't poison shift decisions.
            self._calibrator.reset_car(ck)
            self._power_curve.reset_car(ck)
            self._rev_limiter.reset_car(ck)
            self._shift_lag.reset_car(ck)
            self._shift_outcome.reset_car(ck)
            # Restore previously-saved learning data for this car+tune.
            self._load_profiles(ck)
            self._last_profile_signature = self._profile_signature(ck)

        self._update_turbo(td, dt)
        self._update_rpm_rate(td, now)
        self._update_attitude(td)
        self._calibrator.observe(td)

        in_shift_settle = now < self._lock_until

        if not in_shift_settle:
            before_limiter = self._rev_limiter.effective_redline(td)
            self._rev_limiter.observe(td, self._last_downshift_time, now)
            after_limiter = self._rev_limiter.effective_redline(td)
            if after_limiter != before_limiter:
                self._record_decision(
                    "learn_limiter",
                    td,
                    learned_limiter=after_limiter,
                    previous_limiter=before_limiter,
                )
                self._save_profiles_if_changed(now, force=True)

        if self._config.get("feat_power_curve") and not in_shift_settle:
            self._power_curve.observe(td)
        self._save_profiles_if_changed(now)
        if self._config.get("feat_airtime_lock"):
            air_state = self._airtime.update(td, now)
            if air_state.just_landed and self._config.get("feat_landing_recovery", True):
                self._no_downshift_until = 0.0
                self._no_predictive_until = 0.0
                self._lock_until = min(self._lock_until, now + 0.10)
                if not self._low_gear_limiter_guard_ready(td, now)[0]:
                    self._no_upshift_until = max(self._no_upshift_until, now + 0.80)
        outcome_update = self._shift_outcome.observe(
            td,
            now,
            clean=self._race_shift_outcome_sample_clean(td),
        )
        if outcome_update is not None:
            if outcome_update.changed:
                self._record_shift_outcome_update("shift_outcome_adjust", td, outcome_update)
            self._save_profiles_if_changed(now, force=outcome_update.changed)
        if self._config.get("feat_transient_lock"):
            self._yaw_transient.update(td, now)
        if self._config.get("feat_drive_style"):
            self._drive_style.update(td, self._g_lat, now)

        self._session_stats.update_peaks(td, self._g_lat, self._g_lon, td.power_w / 1000.0)
        self._graph_buffer.push(td)
        self._watchdog.heartbeat()

        # O(1) Thread Offload - Prevents telemetry processing stall if Discord RPC is slow
        if self._discord_rpc is not None and self._config.get("feat_discord_rpc"):
            self._discord_executor.submit(
                self._discord_rpc.update, self.mode.value, self._shift_count, td.speed_kmh
            )

        if self._config.get("feat_reverse_hold"):
            result = self._reverse_hold.update(td, self._down_held, self._up_held, now)
            if result == "ENGAGED_REVERSE":
                self._tcu_state = "REVERSE (held)"
                self._tcu_state_sub = "user engaged R"

        is_reverse_now = (td.gear == 0) or (td.vel_z < -1.5 and td.gear <= 1)
        if is_reverse_now:
            self._tcu_state = "REVERSE"
            self._tcu_state_sub = "TCU passive"
            self._reverse_lock_until = now + 2.0
            return

        if now < self._reverse_lock_until:
            self._tcu_state = "REVERSE"
            self._tcu_state_sub = "exiting R..."
            return

        current_mode = self.mode
        if current_mode != self._last_processed_mode:
            self._last_processed_mode = current_mode
            self._launch_armed = False
            self._no_upshift_until = 0.0
            self._shift_outcome.cancel_pending()

        if self.mode == Mode.MANUAL:
            self._tcu_state = "MANUAL"
            self._tcu_state_sub = "TCU off"
            self._shift_hint = ""
            self._shift_advice = ""
            if self._config.get("feat_shift_advisor"):
                self._compute_shift_advisor(td)
            return

        if self.mode == Mode.LEARN:
            self._mode_learn(td, now)
            return

        self._shift_hint = ""
        self._shift_advice = ""

        if self._config.get("feat_launch_control") and self._launch_control(td, now):
            return

        if now < self._lock_until:
            if self._just_impacted():
                self._lock_until = now + 0.20
                self._no_downshift_until = 0.0
                self._no_predictive_until = 0.0
            elif td.brake > 0.45 and (self._lock_until - now) > 0.20:
                self._lock_until = now + 0.20
            else:
                self._tcu_state = "POST-SHIFT"
                self._tcu_state_sub = "stabilizing"
                return

        if self._config.get("feat_airtime_lock") and self._airtime.is_airborne:
            if self.mode == Mode.RACE:
                if self._fuel_cut_escape_upshift(td, now):
                    return
                if self._low_gear_limiter_guard_upshift(td, now):
                    return
            self._tcu_state = "AIRBORNE"
            self._tcu_state_sub = "holding decisions"
            return

        if self.mode in (Mode.RACE, Mode.OFFROAD) and self._fuel_cut_escape_upshift(td, now):
            return

        min_sensible_speed = self._min_sensible_speed_for_gear(td)
        if td.gear >= 2 and td.speed_kmh < min_sensible_speed and td.rpm_pct < 0.40:
            if self._race_wheel_speed_untrusted(td, now):
                self._race_wheel_speed_hold(td, now, "mismatch")
                return
            self._tcu_state = "GEAR MISMATCH"
            self._tcu_state_sub = f"too high for {td.speed_kmh:.0f} km/h"
            self._no_downshift_until = 0.0
            self._shift_down(td, 350, "MISMATCH", f"{td.gear}->{td.gear - 1}")
            return

        if td.speed_kmh < Cfg.MIN_SPEED_KMH:
            self._tcu_state = "STANDSTILL"
            self._tcu_state_sub = ""
            if td.gear >= 2 and td.speed_kmh < 10.0:
                self._shift_down(td, 600, "STANDSTILL", f"{td.gear}->{td.gear - 1}")
            return

        self._cornering_locked = False
        cornering_thr = self._config.get("cornering_yaw", 22) / 100.0
        if self._config.get("feat_cornering_lock") and abs(td.ang_vel_z) > cornering_thr:
            self._cornering_locked = True
            self._tcu_state = "CORNERING"
            self._tcu_state_sub = "upshift locked"

        m = self.mode
        if m == Mode.RACE:
            self._mode_race(td, now)
        elif m == Mode.DRIFT:
            self._mode_drift(td, now)
        elif m == Mode.OFFROAD:
            self._mode_offroad(td, now)

    def _invalid_drive_packet(self, td: Telemetry) -> bool:
        return td.car_ordinal <= 0 or td.engine_max_rpm <= 0.0

    def _reset_for_invalid_drive_packet(self):
        self._prev_gear = -1
        self._we_shifted = False
        self._lock_until = 0.0
        self._no_downshift_until = 0.0
        self._no_upshift_until = 0.0
        self._no_predictive_until = 0.0
        self._reverse_lock_until = 0.0
        self._launch_armed = False
        self._last_hard_brake_time = 0.0
        self._brake_history.clear()
        self._throttle_history.clear()
        self._speed_history.clear()
        self._rpm_rate_history.clear()
        self._brake_raw_history.clear()
        self._throttle_raw_history.clear()
        self._last_rpm_sample = None
        self._pending_upshift_gear = None
        self._pending_upshift_from_gear = None
        self._pending_upshift_car_key = None
        self._pending_upshift_until = 0.0
        self._shift_hint = ""
        self._shift_advice = ""

    def _shift_up(
        self,
        td: Telemetry,
        lock_ms: int,
        state: str,
        sub: str = "",
        *,
        downshift_lock_s: float = 1.0,
        decision_extra: dict | None = None,
        allow_cornering_locked: bool = False,
        allow_airborne_locked: bool = False,
    ) -> bool:
        if td.gear >= 10:
            return False
        if self._upshift_blocked_by_top_gear(td):
            self._tcu_state = "TOP GEAR"
            self._tcu_state_sub = "confirmed max gear"
            return True
        if (
            self._config.get("feat_airtime_lock")
            and self._airtime.is_airborne
            and not allow_airborne_locked
        ):
            self._tcu_state = "AIRBORNE"
            self._tcu_state_sub = "upshift locked"
            return False
        if self._cornering_locked and not allow_cornering_locked:
            return False
        if td.gear <= 2:
            lock_ms = max(lock_ms, Cfg.LOW_GEAR_LOCK_MS)

        self._tcu_state = state
        self._tcu_state_sub = sub
        now = time.time()
        self._lock_until = now + (lock_ms / 1000.0)
        self._no_upshift_until = max(self._no_upshift_until, self._lock_until)
        pending_until = now + max(lock_ms / 1000.0, 0.65)
        self._pending_upshift_gear = td.gear + 1
        self._pending_upshift_from_gear = td.gear
        self._pending_upshift_car_key = td.car_key
        self._pending_upshift_until = pending_until
        self._no_upshift_until = max(self._no_upshift_until, pending_until)
        self._no_downshift_until = max(self._no_downshift_until, now + downshift_lock_s)
        self._we_shifted = True
        self._shift_count += 1

        # 记录换挡指令时刻（用于延迟学习）
        self._shift_lag.record_shift_command(
            td.car_key,
            "UP",
            td.gear,
            now,
            command_rpm=td.current_rpm,
        )
        self._record_shift_outcome_command(td, now, decision_extra)

        self._kb.shift_up()
        self._logger.mark_event()
        decision = {"state": state, "reason": sub}
        if decision_extra:
            decision.update(decision_extra)
        self._record_decision("shift_up", td, **decision)
        self._shift_history.record("UP", td, reason=state, rule=self.mode.value)
        self._session_stats.record_shift("UP", state)
        if WINSOUND_OK and self._config.get("feat_sound_beep"):
            self._audio_executor.submit(winsound.Beep, 3000, 40)
        return True

    def _low_gear_limiter_guard_ready(self, td: Telemetry, now: float) -> tuple[bool, float]:
        if self.mode != Mode.RACE:
            return False, 0.0
        if td.gear != 1 or td.engine_max_rpm <= 0:
            return False, 0.0
        if td.speed_kmh <= Cfg.MIN_SPEED_KMH or td.throttle < 0.80 or td.brake > 0.10:
            return False, 0.0
        if self._pending_upshift_gear is not None:
            return False, 0.0

        learned = self._rev_limiter.effective_redline(td)
        if learned is not None:
            guard_rpm = max(td.engine_max_rpm * 0.82, learned - 500.0)
        else:
            guard_rpm = td.engine_max_rpm * 0.88
        return td.current_rpm >= guard_rpm, guard_rpm

    def _low_gear_limiter_guard_upshift(self, td: Telemetry, now: float) -> bool:
        ready, guard_rpm = self._low_gear_limiter_guard_ready(td, now)
        if not ready:
            return False
        if self._race_wheel_speed_untrusted(td, now):
            return self._race_wheel_speed_hold(td, now, "limiter guard")

        blocked_by_airtime = self._config.get("feat_airtime_lock") and self._airtime.is_airborne
        blocked_by_upshift_lock = now < self._no_upshift_until
        blocked_by_cornering = self._cornering_locked
        if not (blocked_by_airtime or blocked_by_upshift_lock or blocked_by_cornering):
            return False

        old_no_upshift_until = self._no_upshift_until
        old_lock_until = self._lock_until
        self._no_upshift_until = min(self._no_upshift_until, now)
        self._lock_until = min(self._lock_until, now)
        shifted = self._shift_up(
            td,
            260,
            "UPSHIFT",
            "low gear limiter guard",
            downshift_lock_s=0.65,
            allow_cornering_locked=True,
            allow_airborne_locked=True,
            decision_extra={
                "low_gear_limiter_guard": True,
                "guard_rpm": round(guard_rpm, 1),
                "airborne_guard": bool(blocked_by_airtime),
                "previous_no_upshift_until_s": round(
                    max(0.0, old_no_upshift_until - now),
                    3,
                ),
            },
        )
        if not shifted:
            self._no_upshift_until = old_no_upshift_until
            self._lock_until = old_lock_until
        return shifted

    def _fuel_cut_escape_upshift(self, td: Telemetry, now: float) -> bool:
        if td.gear < 1 or td.gear >= 10 or td.engine_max_rpm <= 0:
            return False
        if td.speed_kmh <= Cfg.MIN_SPEED_KMH or td.throttle < 0.80 or td.brake > 0.10:
            return False
        if td.power_w > -10000.0:
            return False
        if self._pending_upshift_gear is not None:
            return False

        learned = self._rev_limiter.effective_redline(td)
        if learned is not None:
            escape_rpm = max(td.engine_max_rpm * 0.72, learned - 650.0)
        else:
            escape_rpm = td.engine_max_rpm * 0.84
        if td.current_rpm < escape_rpm:
            return False

        if self.mode == Mode.RACE and self._race_wheel_speed_untrusted(td, now):
            return self._race_wheel_speed_hold(td, now, "fuel cut")

        old_no_upshift_until = self._no_upshift_until
        self._no_upshift_until = min(self._no_upshift_until, now)
        hold_after_escape = (
            self.mode == Mode.RACE and td.max_combined_slip >= 4.0 and td.throttle >= 0.55
        )
        shifted = self._shift_up(
            td,
            260,
            "FUEL CUT",
            "escape",
            downshift_lock_s=0.65,
            allow_cornering_locked=True,
            allow_airborne_locked=True,
            decision_extra={
                "fuel_cut_escape": True,
                "escape_rpm": round(escape_rpm, 1),
                "previous_no_upshift_until_s": round(max(0.0, old_no_upshift_until - now), 3),
                "race_slip_hold_after_escape": hold_after_escape,
            },
        )
        if not shifted:
            self._no_upshift_until = old_no_upshift_until
        elif hold_after_escape:
            self._race_slip_hold_until = max(self._race_slip_hold_until, now + 0.70)
        return shifted

    def _upshift_blocked_by_top_gear(self, td: Telemetry) -> bool:
        top_gear = self._top_gear_by_car.get(td.car_key)
        return top_gear is not None and td.gear >= top_gear

    def _observe_higher_gear(self, td: Telemetry):
        if td.gear > 10:
            return
        top_gear = self._top_gear_by_car.get(td.car_key)
        if top_gear is not None and td.gear > top_gear:
            self._top_gear_by_car.pop(td.car_key, None)

        attempts = self._failed_upshift_attempts.get(td.car_key)
        if attempts is None:
            return
        for gear in [gear for gear in attempts if gear < td.gear]:
            attempts.pop(gear, None)
        if not attempts:
            self._failed_upshift_attempts.pop(td.car_key, None)

    def _record_unresponsive_upshift(self, td: Telemetry):
        target_gear = self._pending_upshift_gear
        from_gear = self._pending_upshift_from_gear
        car_key = self._pending_upshift_car_key
        if target_gear is None or from_gear is None or car_key is None:
            return
        if td.car_key != car_key or td.gear != from_gear or td.gear >= target_gear:
            return
        if from_gear < 5 or td.speed_kmh <= Cfg.MIN_SPEED_KMH:
            return

        attempts = self._failed_upshift_attempts.setdefault(car_key, {})
        attempts[from_gear] = attempts.get(from_gear, 0) + 1
        if attempts[from_gear] < 2:
            return

        self._top_gear_by_car[car_key] = from_gear
        self._record_decision(
            "upshift_block",
            td,
            state="TOP GEAR",
            reason=f"{from_gear}->{target_gear} unresponsive",
            top_gear=from_gear,
            failed_upshift_attempts=attempts[from_gear],
        )

    def _shift_down(
        self,
        td: Telemetry,
        lock_ms: int,
        state: str,
        sub: str = "",
        *,
        cascade_lock_s: float | None = None,
    ) -> bool:
        if td.gear <= 1:
            return False
        now = time.time()
        if now < self._no_downshift_until:
            return False

        projected = self._calibrator.project_rpm_after_shift(td, td.gear - 1)
        if projected is None:
            projected = td.current_rpm * (td.gear / max(td.gear - 1, 1))

        if projected > td.engine_max_rpm * Cfg.OVER_REV_LIMIT:
            self._tcu_state = "OVER-REV BLOCKED"
            return False

        self._tcu_state = state
        self._tcu_state_sub = sub
        self._lock_until = now + (lock_ms / 1000.0)

        if cascade_lock_s is not None:
            cascade_lock = cascade_lock_s
        elif state in ("BRAKE DOWN", "MISMATCH", "ENGINE BRAKE") or td.brake > 0.45:
            cascade_lock = 0.30
        elif state in ("KICKDOWN", "PREDICTIVE", "TORQUE DOWN", "BAND DOWN"):
            cascade_lock = 0.70
        elif state in ("ANTI-STALL", "STANDSTILL", "COAST DOWN", "DRIFT HOLD"):
            cascade_lock = 0.60
        else:
            cascade_lock = 0.90

        self._no_downshift_until = now + cascade_lock
        self._we_shifted = True
        self._shift_count += 1
        self._last_downshift_time = now

        # 记录换挡指令时刻（用于延迟学习）
        self._shift_lag.record_shift_command(td.car_key, "DOWN", td.gear, now)

        self._kb.shift_down()
        self._logger.mark_event()
        self._record_decision("shift_down", td, state=state, reason=sub)
        self._shift_history.record("DOWN", td, reason=state, rule=self.mode.value)
        self._session_stats.record_shift("DOWN", state)
        if WINSOUND_OK and self._config.get("feat_sound_beep"):
            self._audio_executor.submit(winsound.Beep, 1500, 50)
        return True

    def _shift_down_double(
        self,
        td: Telemetry,
        lock_ms: int,
        target: int,
        *,
        state: str = "BRAKE DOWN",
        cascade_lock_s: float = 0.30,
    ) -> bool:
        if td.gear <= 2:
            return False
        now = time.time()
        if now < self._no_downshift_until:
            return False

        projected = self._calibrator.project_rpm_after_shift(td, td.gear - 2)
        if projected is None:
            projected = td.current_rpm * (td.gear / max(td.gear - 2, 1))

        if projected > td.engine_max_rpm * Cfg.OVER_REV_LIMIT:
            return False

        self._tcu_state = state
        self._tcu_state_sub = f"skip ->{target}"
        self._lock_until = now + (lock_ms / 1000.0)
        self._no_downshift_until = now + cascade_lock_s
        self._we_shifted = True
        self._shift_count += 2
        self._last_downshift_time = now
        self._kb.shift_down_double()
        self._logger.mark_event()
        self._record_decision("shift_down_double", td, state=state, reason=f"skip to {target}")
        self._shift_history.record("DOWN", td, reason="SKIP DOWN", rule=self.mode.value)
        self._session_stats.record_shift("DOWN", state)
        self._session_stats.record_shift("DOWN", state)
        if WINSOUND_OK and self._config.get("feat_sound_beep"):
            self._audio_executor.submit(winsound.Beep, 1500, 50)
        return True

    @staticmethod
    def _curve(throttle: float, low: float, mid: float, high: float) -> float:
        throttle = max(0.0, min(1.0, throttle))
        if throttle <= 0.50:
            t = throttle / 0.50
            return low + (mid - low) * t
        t = (throttle - 0.50) / 0.50
        return mid + (high - mid) * t

    def _speed_stable(self, delta_kmh: float = 3.0) -> bool:
        if len(self._speed_history) < 15:
            return False
        return (max(self._speed_history) - min(self._speed_history)) < delta_kmh

    def _speed_decreasing(self, delta_kmh: float = 0.8) -> bool:
        if len(self._speed_history) < 8:
            return False
        recent = list(self._speed_history)
        old_speed = sum(recent[:4]) / 4
        new_speed = sum(recent[-4:]) / 4
        return (old_speed - new_speed) > delta_kmh

    def _just_impacted(self) -> bool:
        if len(self._speed_history) < 2:
            return False
        prev_speed, current_speed = self._speed_history[-2], self._speed_history[-1]
        if current_speed < Cfg.MIN_SPEED_KMH:
            return False
        return (prev_speed - current_speed) >= Cfg.IMPACT_DECEL_KMH

    def _kickdown_pedal_threshold(self, td: Telemetry, base: float) -> float:
        if not self._config.get("feat_drivetrain_aware"):
            return base
        if td.drivetrain == 0:
            return min(0.95, base + 0.08)
        elif td.drivetrain == 2:
            return max(0.40, base - 0.05)
        return base

    def _brake_is_spike(self) -> bool:
        if len(self._brake_raw_history) < 8:
            return False
        recent = list(self._brake_raw_history)
        old = sum(recent[:4]) / 4
        new = sum(recent[-4:]) / 4
        return (new - old) > Cfg.BRAKE_SPIKE_DELTA

    def _throttle_ramp_up(self) -> float:
        if len(self._throttle_raw_history) < 9:
            return 0.0
        recent = list(self._throttle_raw_history)
        old = sum(recent[:3]) / 3
        new = sum(recent[-3:]) / 3
        return max(0.0, new - old)

    def _update_rpm_rate(self, td: Telemetry, now: float):
        prev = self._last_rpm_sample
        self._last_rpm_sample = (now, td.current_rpm)
        if prev is None or td.engine_max_rpm <= 0 or td.current_rpm <= 0:
            return
        prev_t, prev_rpm = prev
        dt = now - prev_t
        if dt <= 0.001 or dt > 0.25:
            return
        rate = (td.current_rpm - prev_rpm) / dt
        if -5000.0 <= rate <= 16000.0:
            self._rpm_rate_history.append(rate)

    def _rpm_rise_rate(self) -> float:
        rates = [r for r in self._rpm_rate_history if r > 0.0]
        if not rates:
            return 0.0
        return sum(rates) / len(rates)

    def _race_limiter_margin_rpm(self, td: Telemetry) -> float:
        # Estimate how much RPM the engine can gain during command latency and
        # the first part of the shift, then clamp it so bad telemetry cannot
        # create an unreachable or overly early target.
        rise_rate = self._rpm_rise_rate()
        learned_lag = self._shift_lag.get_upshift_lag(td.car_key)
        trusted_lag = min(learned_lag, 0.140)
        base = 145.0
        margin = rise_rate * max(0.030, trusted_lag * 0.30)
        if td.gear <= 2:
            margin += 30.0
        elif td.gear == 3:
            margin += 20.0
        if td.throttle > 0.90:
            margin += 15.0
        return max(200.0, min(360.0, base + margin))

    def _upshift_lead_rpm(self, td: Telemetry) -> float:
        if td.engine_max_rpm <= 0 or td.throttle < 0.55 or td.brake > 0.05:
            return 0.0
        rise_rate = self._rpm_rise_rate()
        if rise_rate < 800.0:
            return 0.0

        # 使用学习到的换挡延迟（如果可用）
        learned_lag = self._shift_lag.get_upshift_lag(td.car_key)

        if self.mode == Mode.RACE:
            latency_s = max(0.045, min(learned_lag, 0.140))
            cap_rpm = 420.0
            base_rpm = 35.0
        elif self.mode == Mode.OFFROAD:
            latency_s = max(0.050, learned_lag * 0.8)  # 越野略快
            cap_rpm = 260.0
            base_rpm = 25.0
        else:
            latency_s = max(0.060, learned_lag * 0.9)
            cap_rpm = 320.0
            base_rpm = 30.0

        lead = base_rpm + rise_rate * latency_s
        if td.gear <= 2:
            lead += 45.0 if self.mode == Mode.RACE else 80.0
        elif td.gear == 3 and self.mode == Mode.RACE:
            lead += 25.0
        if td.throttle > 0.90:
            lead += 20.0 if self.mode == Mode.RACE else 15.0

        learned_rpm_gain = self._shift_lag.get_upshift_rpm_gain(td.car_key, td.gear)
        if learned_rpm_gain is not None and self.mode == Mode.RACE:
            learned_cap = max(85.0, learned_rpm_gain + 35.0)
            if td.gear <= 2:
                learned_cap += 15.0
            lead = min(lead, learned_cap)
        return max(0.0, min(cap_rpm, lead))

    def _upshift_command_target_pct(
        self,
        td: Telemetry,
        target_pct: float,
        source: str,
    ) -> tuple[float, str]:
        if source not in {"power cross", "falling power", "power ceiling", "power peak"}:
            return target_pct, source

        lead_rpm = self._upshift_lead_rpm(td)
        if lead_rpm <= 0.0:
            return target_pct, source

        target_rpm = target_pct * td.engine_max_rpm
        command_rpm = target_rpm - lead_rpm
        peak_power = self._power_curve.peak_power_abs_rpm(td.car_key)
        if peak_power is not None and target_rpm > peak_power:
            if self.mode == Mode.RACE:
                post_peak_room = target_rpm - peak_power
                min_post_peak = max(120.0, min(260.0, post_peak_room * 0.55))
                after_peak_floor = peak_power + min(min_post_peak, max(0.0, post_peak_room - 80.0))
            else:
                after_peak_floor = peak_power + 25.0
            command_rpm = max(command_rpm, after_peak_floor)
        if self.mode == Mode.RACE and source == "power ceiling":
            learned_rpm_gain = self._shift_lag.get_upshift_rpm_gain(td.car_key, td.gear)
            max_power_ceiling_lead = 260.0
            if learned_rpm_gain is not None:
                max_power_ceiling_lead = max(110.0, min(260.0, learned_rpm_gain + 40.0))
                if td.gear <= 2:
                    max_power_ceiling_lead = min(260.0, max_power_ceiling_lead + 15.0)
            command_rpm = max(command_rpm, target_rpm - max_power_ceiling_lead)

        command_pct = max(0.45, min(target_pct, command_rpm / td.engine_max_rpm))
        if target_pct - command_pct < 0.002:
            return target_pct, source
        applied_lead_rpm = max(0.0, target_rpm - command_pct * td.engine_max_rpm)
        return command_pct, f"{source} lead -{applied_lead_rpm:.0f}"

    def _upshift_base_target_pct(
        self,
        td: Telemetry,
        target_pct: float,
        source: str,
        ceiling_pct: float,
    ) -> float:
        conservative = min(target_pct, ceiling_pct)
        if source != "power ceiling":
            return conservative

        dynamic = self._dynamic_power_ceiling_target_pct(td, conservative)
        if dynamic is None:
            return conservative
        return max(conservative, min(dynamic, 0.992))

    def _dynamic_power_ceiling_target_pct(
        self,
        td: Telemetry,
        conservative_pct: float,
    ) -> float | None:
        if self.mode != Mode.RACE or td.engine_max_rpm <= 0 or td.throttle < 0.80:
            return None
        if td.brake > 0.05 or td.gear < 1 or td.gear >= 10:
            return None

        learned_redline = self._rev_limiter.effective_redline(td)
        if learned_redline is None:
            return None

        current_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear)
        next_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear + 1)
        if not current_ratio or not next_ratio:
            return None

        target_rpm = conservative_pct * td.engine_max_rpm
        next_rpm = target_rpm * next_ratio / current_ratio
        current_power = self._power_curve.power_at_rpm(td.car_key, target_rpm)
        next_power = self._power_curve.power_at_rpm(td.car_key, next_rpm)
        if current_power is None or next_power is None or current_power <= 1.0:
            return None

        landing_power_ratio = max(0.0, min(1.25, next_power / current_power))
        if landing_power_ratio >= 0.86:
            return None

        # If the next gear lands far below current-gear power, the fastest
        # move is usually to hold until close to the learned safe redline.
        pull_bias = (0.86 - landing_power_ratio) / (0.86 - 0.55)
        pull_bias = max(0.0, min(1.0, pull_bias))

        redline_margin = max(55.0, min(140.0, self._race_limiter_margin_rpm(td) * 0.25))
        safe_redline_pct = max(
            conservative_pct,
            min(0.992, (learned_redline - redline_margin) / td.engine_max_rpm),
        )
        if safe_redline_pct <= conservative_pct:
            return None

        return conservative_pct + (safe_redline_pct - conservative_pct) * pull_bias

    def _record_decision(self, event: str, td: Telemetry, **extra):
        data = {
            "event": event,
            "mode": self.mode.value,
            "gear": td.gear,
            "rpm": round(td.current_rpm, 1),
            "rpm_max": round(td.engine_max_rpm, 1),
            "rpm_pct": round(td.rpm_pct, 4),
            "speed_kmh": round(td.speed_kmh, 1),
            "throttle": round(td.throttle, 3),
            "brake": round(td.brake, 3),
            "power_kw": round(td.power_w / 1000.0, 1),
            "slip": round(td.max_combined_slip, 3),
            "car_key": list(td.car_key),
        }
        data.update(extra)
        self._logger.record_decision(data)

    def _post_upshift_confirm_hold_s(self, td: Telemetry) -> float:
        if td.engine_max_rpm <= 0 or td.gear < 1 or td.throttle < 0.55 or td.brake > 0.05:
            return 0.12

        offset_by_mode = {
            Mode.RACE: 0.03,
            Mode.OFFROAD: 0.07,
        }
        offset = offset_by_mode.get(self.mode)
        target_pct: float | None = None
        if offset is not None:
            performance_target = self._performance_upshift_target_pct(td, offset)
            if performance_target is not None:
                target_pct = self._upshift_base_target_pct(
                    td,
                    performance_target[0],
                    performance_target[1],
                    self._upshift_ceiling_pct(td),
                )
            elif self.mode == Mode.RACE:
                target_pct, _source = self._race_upshift_target_pct(td)

        if target_pct is None:
            return 0.12

        gap_rpm = target_pct * td.engine_max_rpm - td.current_rpm
        if gap_rpm <= 250.0:
            return 0.03
        if gap_rpm <= 500.0:
            return 0.05
        if gap_rpm <= 900.0:
            return 0.08
        return 0.12

    def _upshift_decision_fields(
        self,
        td: Telemetry,
        *,
        command_pct: float,
        command_source: str,
        strategy_pct: float,
        strategy_source: str,
        base_pct: float,
        ceiling_pct: float,
        offset: float,
    ) -> dict:
        max_rpm = max(td.engine_max_rpm, 1.0)
        current_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear)
        next_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear + 1)
        projected_next_rpm = None
        if current_ratio and next_ratio:
            projected_next_rpm = command_pct * max_rpm * next_ratio / current_ratio

        progress = self._power_curve.learning_progress(td.car_key)
        peak_rpm = self._power_curve.peak_power_abs_rpm(td.car_key)
        current_power = self._power_curve.power_at_rpm(td.car_key, td.current_rpm)
        projected_power = (
            self._power_curve.power_at_rpm(td.car_key, projected_next_rpm)
            if projected_next_rpm is not None
            else None
        )
        command_rpm = command_pct * max_rpm
        base_rpm = base_pct * max_rpm
        if self.mode == Mode.RACE and self._race_shift_outcome_sample_clean(td):
            outcome_offset = self._shift_outcome.base_offset_rpm(td.car_key, td.gear)
            outcome_active = self._shift_outcome.active_offset_rpm(
                td.car_key,
                td.gear,
                allow_probe=False,
            )
            outcome_samples = self._shift_outcome.sample_count(td.car_key, td.gear)
        else:
            outcome_offset = 0.0
            outcome_active = 0.0
            outcome_samples = 0
        return {
            "upshift_target_rpm": round(command_rpm, 1),
            "upshift_target_pct": round(command_pct, 4),
            "upshift_target_source": command_source,
            "upshift_strategy_rpm": round(strategy_pct * max_rpm, 1),
            "upshift_strategy_pct": round(strategy_pct, 4),
            "upshift_strategy_source": strategy_source,
            "upshift_base_target_rpm": round(base_rpm, 1),
            "upshift_ceiling_rpm": round(ceiling_pct * max_rpm, 1),
            "upshift_lead_rpm": round(max(0.0, base_rpm - command_rpm), 1),
            "upshift_offset": round(offset, 4),
            "projected_next_rpm": round(projected_next_rpm, 1)
            if projected_next_rpm is not None
            else None,
            "ratio_current": round(current_ratio, 5) if current_ratio else None,
            "ratio_next": round(next_ratio, 5) if next_ratio else None,
            "power_peak_rpm": round(peak_rpm, 1) if peak_rpm is not None else None,
            "power_current_hp": round(current_power, 1) if current_power is not None else None,
            "power_projected_next_hp": round(projected_power, 1)
            if projected_power is not None
            else None,
            "turbo_lag_block": self._turbo_lag_block_upshift(td),
            "boost_raw": round(td.boost_raw, 3),
            "turbo_bar": round(self._turbo_bar, 3),
            "power_samples": progress.get("samples", 0),
            "power_points": progress.get("points", 0),
            "power_confidence": round(self._power_curve.confidence(td.car_key), 3),
            "shift_outcome_offset_rpm": round(outcome_offset, 1),
            "shift_outcome_active_offset_rpm": round(outcome_active, 1),
            "shift_outcome_samples": outcome_samples,
        }

    def _race_shift_outcome_sample_clean(self, td: Telemetry) -> bool:
        if self.mode != Mode.RACE:
            return False
        if td.car_key[0] <= 0 or td.gear < 1 or td.engine_max_rpm <= 0:
            return False
        if td.throttle < 0.90 or td.brake > 0.03:
            return False
        if self._config.get("feat_airtime_lock") and self._airtime.is_airborne:
            return False
        if td.max_combined_slip > 0.75:
            return False
        if abs(td.ang_vel_z) > 0.12 or abs(self._g_lat) > 0.35:
            return False
        if td.power_w < -5000.0:
            return False
        return td.speed_kmh > Cfg.MIN_SPEED_KMH

    def _race_first_gear_traction_hold(self, td: Telemetry, now: float) -> bool:
        if self.mode != Mode.RACE or td.gear != 1 or td.engine_max_rpm <= 0:
            return False
        if td.throttle < 0.80 or td.brake > 0.05 or td.speed_kmh <= Cfg.MIN_SPEED_KMH:
            return False
        if self._pending_upshift_gear is not None:
            return False
        if td.power_w < -5000.0:
            return False
        if td.max_combined_slip < 4.0:
            return False

        projected_speed = self._calibrator.speed_for_rpm(
            td.car_key,
            1,
            min(td.current_rpm, td.engine_max_rpm * 0.95),
        )
        if projected_speed is not None:
            hold_until_speed = max(38.0, min(72.0, projected_speed * 0.35))
        else:
            hold_until_speed = 55.0
        if td.speed_kmh >= hold_until_speed:
            return False

        self._tcu_state = "TRACTION HOLD"
        self._tcu_state_sub = f"1st wheelspin {td.speed_kmh:.0f}<{hold_until_speed:.0f} km/h"
        if now - self._last_traction_hold_log_at >= 0.35:
            self._last_traction_hold_log_at = now
            self._record_decision(
                "traction_hold",
                td,
                hold_until_speed_kmh=round(hold_until_speed, 1),
                projected_gear_speed_kmh=round(projected_speed, 1)
                if projected_speed is not None
                else None,
            )
        return True

    def _race_wheel_speed_untrusted(self, td: Telemetry, now: float) -> bool:
        if self.mode != Mode.RACE:
            return False
        if td.gear < 1 or td.engine_max_rpm <= 0 or td.speed_kmh <= Cfg.MIN_SPEED_KMH:
            return False
        if td.brake > 0.12:
            return False
        if self._config.get("feat_airtime_lock") and self._airtime.is_airborne:
            return False
        severe_slip = td.max_combined_slip >= 4.0 and td.throttle >= 0.55
        lingering_slip = now < self._race_slip_hold_until and td.throttle >= 0.35
        if severe_slip:
            self._race_slip_hold_until = max(self._race_slip_hold_until, now + 0.70)
            return True
        return lingering_slip

    def _race_wheel_speed_hold(self, td: Telemetry, now: float, reason: str) -> bool:
        self._tcu_state = "TRACTION HOLD"
        self._tcu_state_sub = f"wheel speed untrusted ({reason})"
        self._no_upshift_until = max(self._no_upshift_until, now + 0.20)
        self._shift_outcome.cancel_pending()
        if now - self._last_race_slip_hold_log_at >= 0.35:
            self._last_race_slip_hold_log_at = now
            self._record_decision(
                "race_slip_hold",
                td,
                hold_reason=reason,
                hold_until_s=round(max(0.0, self._race_slip_hold_until - now), 3),
            )
        return True

    def _race_shift_outcome_offset_rpm(self, td: Telemetry) -> float:
        if not self._race_shift_outcome_sample_clean(td):
            return 0.0
        if td.gear < 1 or td.gear >= 10:
            return 0.0
        return self._shift_outcome.active_offset_rpm(
            td.car_key,
            td.gear,
            allow_probe=td.throttle >= 0.95,
        )

    def _apply_shift_outcome_offset_pct(self, td: Telemetry, target_pct: float) -> float:
        if (
            self.mode != Mode.RACE
            or td.engine_max_rpm <= 0
            or not self._race_shift_outcome_sample_clean(td)
        ):
            return target_pct
        offset_rpm = self._shift_outcome.active_offset_rpm(
            td.car_key,
            td.gear,
            allow_probe=False,
        )
        if abs(offset_rpm) < 0.01:
            return target_pct
        target = target_pct + offset_rpm / td.engine_max_rpm
        return max(0.50, min(target, self._upshift_ceiling_pct(td)))

    def _record_shift_outcome_command(
        self,
        td: Telemetry,
        now: float,
        decision_extra: dict | None,
    ):
        if not self._race_shift_outcome_sample_clean(td) or not decision_extra:
            self._shift_outcome.cancel_pending()
            return

        source = str(decision_extra.get("upshift_target_source", ""))
        if not source:
            self._shift_outcome.cancel_pending()
            return
        target_rpm = decision_extra.get("upshift_target_rpm")
        nominal_rpm = decision_extra.get("upshift_base_target_rpm", target_rpm)
        active_offset = decision_extra.get("shift_outcome_active_offset_rpm")
        if not isinstance(target_rpm, (int, float)):
            self._shift_outcome.cancel_pending()
            return
        if not isinstance(nominal_rpm, (int, float)):
            nominal_rpm = target_rpm
        if not isinstance(active_offset, (int, float)):
            active_offset = 0.0

        self._shift_outcome.record_command(
            td.car_key,
            td.gear,
            now,
            command_rpm=td.current_rpm,
            command_speed_kmh=td.speed_kmh,
            target_rpm=float(target_rpm),
            nominal_target_rpm=float(nominal_rpm) - float(active_offset),
            applied_offset_rpm=float(active_offset),
            source=source,
        )

    def _shift_outcome_landing_power_ratio(
        self,
        td: Telemetry,
        from_gear: int,
    ) -> float | None:
        from_rpm = self._calibrator.project_rpm_at_speed(td.car_key, from_gear, td.speed_kmh)
        if from_rpm is None:
            current_ratio = self._calibrator.ratio_for_gear(td.car_key, from_gear)
            next_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear)
            if current_ratio and next_ratio:
                from_rpm = td.current_rpm * current_ratio / next_ratio
        if from_rpm is None:
            return None
        current_power = self._power_curve.power_at_rpm(td.car_key, from_rpm)
        landing_power = self._power_curve.power_at_rpm(td.car_key, td.current_rpm)
        if current_power is None or landing_power is None or current_power <= 1.0:
            return None
        return max(0.0, min(1.5, landing_power / current_power))

    def _record_shift_outcome_update(
        self,
        event: str,
        td: Telemetry,
        update: ShiftOutcomeUpdate,
    ):
        extra = {
            "from_gear": max(1, td.gear - 1),
            "shift_outcome_offset_rpm": round(update.offset_rpm, 1),
            "shift_outcome_reason": update.reason,
            "shift_outcome_samples": update.sample_count,
        }
        if update.reward_delta is not None:
            extra["shift_outcome_reward_delta"] = round(update.reward_delta, 3)
        self._record_decision(event, td, **extra)

    def _should_brake_downshift(self, td: Telemetry, base_thr: float) -> bool:
        if td.brake < base_thr:
            return False
        if not self._config.get("feat_brake_curve"):
            return True
        if self._brake_is_spike():
            return True
        if len(self._brake_raw_history) >= 6 and td.brake > 0.55:
            recent = list(self._brake_raw_history)[-6:]
            if min(recent) > 0.45:
                return True
        return False

    def _should_track_brake_downshift(self, td: Telemetry, base_thr: float) -> bool:
        if td.brake < base_thr:
            return False
        if not self._config.get("feat_brake_curve"):
            return True
        if self._brake_is_spike():
            return True
        if td.brake > 0.45:
            return True
        if td.brake > 0.30 and self._speed_decreasing(delta_kmh=0.6):
            return True
        if len(self._brake_raw_history) >= 6 and td.brake > base_thr + 0.10:
            recent = list(self._brake_raw_history)[-6:]
            if min(recent) > base_thr:
                return True
        return False

    def _track_brake_down(
        self,
        td: Telemetry,
        now: float,
        brake_thr: float,
        lock_ms: int = 250,
        *,
        track_brake: bool = False,
        cascade_lock_s: float | None = None,
        engine_brake: bool = False,
        engine_brake_max_current_pct: float = 0.70,
        engine_brake_max_projected_pct: float = 0.90,
    ) -> bool:
        should_downshift = (
            self._should_track_brake_downshift(td, brake_thr)
            if track_brake
            else self._should_brake_downshift(td, brake_thr)
        )
        if not should_downshift:
            return False
        if td.gear <= 1 or td.speed_kmh <= 25.0:
            return False
        if self._brake_slip_downshift_block(td):
            self._tcu_state = "BRAKE HOLD"
            self._tcu_state_sub = "wheel slip"
            return False

        brake_margin = 0.20 * min(1.0, td.brake / 0.80)
        projected_speed = td.speed_kmh * (1.0 - brake_margin)
        target = self._target_gear_for_braking(td, speed_override=projected_speed)

        engine_brake_sub = ""
        if target is not None and target >= td.gear:
            if engine_brake:
                engine_brake_sub = self._brake_engine_downshift_reason(
                    td,
                    brake_thr,
                    max_current_pct=engine_brake_max_current_pct,
                    max_projected_pct=engine_brake_max_projected_pct,
                )
            if not engine_brake_sub and not (td.rpm_pct < 0.50 and td.brake > 0.70):
                return False

        if target is not None and target <= td.gear - 3 and td.brake > 0.80 and td.gear >= 4:
            if self._shift_down_double(
                td,
                lock_ms,
                target,
                cascade_lock_s=cascade_lock_s or 0.30,
            ):
                self._no_upshift_until = now + 0.5
                return True

        if target is not None and target < td.gear - 1:
            sub = f"->{target}"
        elif engine_brake_sub:
            sub = engine_brake_sub
        elif target is None:
            sub = "no ratio data"
        else:
            sub = "panic brake"

        if not self._shift_down(td, lock_ms, "BRAKE DOWN", sub, cascade_lock_s=cascade_lock_s):
            return False

        self._no_upshift_until = now + 0.5
        return True

    def _brake_engine_downshift_reason(
        self,
        td: Telemetry,
        brake_thr: float,
        *,
        max_current_pct: float,
        max_projected_pct: float,
    ) -> str:
        if not self._config.get("feat_engine_brake"):
            return ""
        if td.throttle > 0.08 or td.gear <= 1 or td.engine_max_rpm <= 0:
            return ""
        if td.brake < brake_thr:
            return ""

        committed_brake = td.brake >= max(brake_thr + 0.10, 0.32) or self._speed_decreasing(
            delta_kmh=0.35
        )
        if not committed_brake:
            return ""
        if td.rpm_pct >= max_current_pct and td.brake < 0.70:
            return ""

        projected = self._calibrator.project_rpm_after_shift(td, td.gear - 1)
        if projected is None:
            projected = td.current_rpm * (td.gear / max(td.gear - 1, 1))
        projected_pct = projected / td.engine_max_rpm

        ceiling = max_projected_pct if td.brake < 0.70 else min(0.95, max_projected_pct + 0.04)
        if projected_pct > ceiling:
            return ""
        if projected_pct <= td.rpm_pct + 0.06:
            return ""
        if projected_pct < 0.38 and td.brake < 0.45:
            return ""
        return f"engine brake {projected:.0f} rpm"

    def _brake_slip_downshift_block(self, td: Telemetry) -> bool:
        if td.brake < 0.55 or td.max_combined_slip < 2.6:
            return False
        if td.rpm_pct < 0.38:
            return False
        if td.gear <= 2:
            return False
        projected = self._calibrator.project_rpm_after_shift(td, td.gear - 1)
        if projected is not None and projected < td.engine_max_rpm * 0.58:
            return False
        return True

    def _track_out_of_band_kickdown(
        self,
        td: Telemetry,
        now: float,
        climb_only: bool = False,
        *,
        lock_ms: int = 400,
        cascade_lock_s: float | None = None,
        upshift_lock_s: float = 0.8,
    ) -> bool:
        climbing = self._on_climb(td)
        if climb_only and not climbing:
            return False
        had_hard_brake = (now - self._last_hard_brake_time) < 2.0
        throttle_threshold = 0.50 if had_hard_brake else 0.60
        if td.throttle < throttle_threshold:
            return False
        if td.gear <= 2:
            return False

        peak_torque_abs = self._power_curve.peak_torque_abs_rpm(td.car_key)
        threshold = (
            peak_torque_abs / td.engine_max_rpm - 0.10 if peak_torque_abs is not None else 0.55
        )
        if climbing:
            threshold += 0.08

        if td.rpm_pct >= threshold:
            return False
        if not self._shift_down(
            td,
            lock_ms,
            "BAND DOWN",
            "climb" if climbing else "out of band",
            cascade_lock_s=cascade_lock_s,
        ):
            return False
        self._no_upshift_until = now + upshift_lock_s
        return True

    def _safe_downshift_redline(self, td: Telemetry, margin: float = 0.985) -> float:
        learned = self._rev_limiter.effective_redline(td)
        redline = learned if learned is not None else td.engine_max_rpm
        return redline * margin

    def _power_floor_pct(
        self,
        td: Telemetry,
        *,
        fallback: float,
        torque_offset: float,
        min_floor: float,
        max_floor: float,
    ) -> float:
        peak_torque = self._power_curve.peak_torque_rpm(td.car_key)
        if peak_torque is None or self._power_curve.confidence(td.car_key) < 0.20:
            return fallback
        return max(min_floor, min(max_floor, peak_torque + torque_offset))

    def _power_target_rpm(
        self,
        td: Telemetry,
        *,
        fallback_pct: float,
        bias: float,
    ) -> float:
        peak_torque = self._power_curve.peak_torque_abs_rpm(td.car_key)
        peak_power = self._power_curve.peak_power_abs_rpm(td.car_key)
        if peak_torque is None or peak_power is None:
            target = td.engine_max_rpm * fallback_pct
        else:
            peak_power = max(peak_power, peak_torque)
            target = peak_torque + (peak_power - peak_torque) * bias

        if self._turbo_lag_block_upshift(td):
            target = max(target, td.engine_max_rpm * 0.68)
        return max(td.engine_max_rpm * 0.45, min(target, self._safe_downshift_redline(td)))

    def _projected_power_is_better(self, td: Telemetry, projected_rpm: float) -> bool:
        if not self._power_curve.has_power_lookup(td.car_key):
            return True
        current_power = self._power_curve.power_at_rpm(td.car_key, td.current_rpm)
        projected_power = self._power_curve.power_at_rpm(td.car_key, projected_rpm)
        if current_power is None or projected_power is None:
            return True
        margin = max(4.0, current_power * 0.015)
        return projected_power >= current_power + margin

    def _target_gear_for_power(
        self,
        td: Telemetry,
        *,
        fallback_pct: float,
        target_bias: float,
        floor_pct: float,
        min_upshift_reserve_rpm: float = 0.0,
    ) -> tuple[int, float] | None:
        if td.engine_max_rpm <= 0 or td.gear <= 1:
            return None

        target_rpm = self._power_target_rpm(td, fallback_pct=fallback_pct, bias=target_bias)
        floor_rpm = td.engine_max_rpm * floor_pct
        safe_redline = self._safe_downshift_redline(td)
        ratios = self._calibrator.get_ratios(td.car_key)
        best: tuple[int, float] | None = None
        best_score = float("inf")

        for gear in range(td.gear - 1, 0, -1):
            projected = self._calibrator.project_rpm_after_shift(td, gear)
            if projected is None and not ratios and gear == td.gear - 1:
                projected = td.current_rpm * (td.gear / max(gear, 1))
            if projected is None:
                continue
            if projected > safe_redline or projected <= td.current_rpm * 1.08:
                continue
            if not self._projected_power_is_better(td, projected):
                continue
            if min_upshift_reserve_rpm > 0.0:
                upshift_rpm = self._command_upshift_rpm_for_gear(td, gear, offset=0.03)
                if (
                    upshift_rpm is not None
                    and projected >= upshift_rpm - min_upshift_reserve_rpm
                ):
                    continue

            below_floor_penalty = max(0.0, floor_rpm - projected) * 1.5
            score = abs(projected - target_rpm) + below_floor_penalty
            if score < best_score:
                best = (gear, projected)
                best_score = score

        return best

    def _command_upshift_rpm_for_gear(
        self,
        td: Telemetry,
        gear: int,
        *,
        offset: float,
    ) -> float | None:
        if td.engine_max_rpm <= 0 or gear < 1 or gear >= 10:
            return None
        guide_td = self._shift_guide_td(td, gear)
        target = self._learned_power_upshift_target_pct(guide_td, offset)
        if target is None:
            return None
        target_pct, source = target
        base_pct = self._upshift_base_target_pct(
            guide_td,
            target_pct,
            source,
            self._upshift_ceiling_pct(guide_td),
        )
        command_pct, _source = self._upshift_command_target_pct(guide_td, base_pct, source)
        return command_pct * td.engine_max_rpm

    def _track_power_demand_downshift(
        self,
        td: Telemetry,
        now: float,
        *,
        min_throttle: float,
        fallback_pct: float,
        target_bias: float,
        floor_pct: float,
        lock_ms: int,
        state: str,
        sub: str,
        min_gear: int = 3,
        min_speed_kmh: float = 30.0,
        allow_skip: bool = True,
        cascade_lock_s: float = 0.35,
        upshift_lock_s: float = 0.70,
        block_spin: bool = True,
        min_upshift_reserve_rpm: float = 0.0,
    ) -> bool:
        if td.throttle < min_throttle or td.brake > 0.08:
            return False
        if td.gear < min_gear or td.speed_kmh < min_speed_kmh:
            return False
        if td.rpm_pct >= floor_pct:
            return False
        if block_spin and self._race_wheel_speed_untrusted(td, now):
            return self._race_wheel_speed_hold(td, now, "power down")
        if block_spin and td.max_combined_slip > 2.4 and td.rpm_pct > 0.45:
            return False

        target = self._target_gear_for_power(
            td,
            fallback_pct=fallback_pct,
            target_bias=target_bias,
            floor_pct=floor_pct,
            min_upshift_reserve_rpm=min_upshift_reserve_rpm,
        )
        if target is None:
            return False
        target_gear, projected_rpm = target
        if target_gear >= td.gear:
            return False

        if allow_skip and target_gear <= td.gear - 2 and td.gear >= 4:
            if self._shift_down_double(
                td,
                lock_ms,
                target_gear,
                state=state,
                cascade_lock_s=cascade_lock_s,
            ):
                self._no_upshift_until = now + upshift_lock_s
                return True
            return False

        reason = f"{sub} ->{target_gear} ({projected_rpm:.0f} rpm)"
        if not self._shift_down(
            td,
            lock_ms,
            state,
            reason,
            cascade_lock_s=cascade_lock_s,
        ):
            return False
        self._no_upshift_until = now + upshift_lock_s
        return True

    def _landing_recovery_downshift(self, td: Telemetry, now: float, mode: Mode) -> bool:
        if not self._config.get("feat_landing_recovery", True):
            return False
        if not self._airtime.landing_recovery(now):
            return False
        if td.gear <= 1 or td.speed_kmh < Cfg.MIN_SPEED_KMH:
            return False

        self._no_downshift_until = 0.0
        if td.brake > 0.25:
            return self._track_brake_down(
                td,
                now,
                self._config.get("brake_thr", 35) / 100 * 0.7,
                lock_ms=240,
                track_brake=True,
                cascade_lock_s=0.25,
            )

        if mode == Mode.RACE:
            floor = self._power_floor_pct(
                td,
                fallback=0.58,
                torque_offset=-0.02,
                min_floor=0.52,
                max_floor=0.72,
            )
            return self._track_power_demand_downshift(
                td,
                now,
                min_throttle=0.34,
                fallback_pct=0.70,
                target_bias=0.45,
                floor_pct=floor,
                lock_ms=260,
                state="LANDING DOWN",
                sub="recovery",
                min_gear=3,
                min_speed_kmh=25.0,
                allow_skip=True,
                cascade_lock_s=0.28,
                upshift_lock_s=0.65,
                min_upshift_reserve_rpm=500.0,
            )

        if mode == Mode.OFFROAD:
            floor = self._power_floor_pct(
                td,
                fallback=self._config.get("offroad_down_rpm", 55) / 100,
                torque_offset=-0.08,
                min_floor=0.46,
                max_floor=0.64,
            )
            return self._track_power_demand_downshift(
                td,
                now,
                min_throttle=0.28,
                fallback_pct=0.62,
                target_bias=0.20,
                floor_pct=floor,
                lock_ms=300,
                state="LANDING TORQUE",
                sub="recovery",
                min_gear=2,
                min_speed_kmh=12.0,
                allow_skip=True,
                cascade_lock_s=0.35,
                upshift_lock_s=0.85,
                block_spin=False,
            )

        if mode == Mode.DRIFT:
            floor = self._config.get("drift_down", 65) / 100
            return self._track_power_demand_downshift(
                td,
                now,
                min_throttle=0.20,
                fallback_pct=max(0.62, floor),
                target_bias=0.25,
                floor_pct=floor,
                lock_ms=280,
                state="DRIFT HOLD",
                sub="landing",
                min_gear=2,
                min_speed_kmh=25.0,
                allow_skip=False,
                cascade_lock_s=0.42,
                upshift_lock_s=0.75,
                block_spin=False,
            )

        return False

    def _track_coast_recovery_downshift(
        self,
        td: Telemetry,
        now: float,
        floor_pct: float,
        state: str,
        sub: str,
        *,
        min_gear: int = 3,
        min_speed_kmh: float = 35.0,
        lock_ms: int = 300,
        upshift_lock_s: float = 0.80,
    ) -> bool:
        if td.throttle > 0.08 or td.brake > 0.12:
            return False
        if td.gear < min_gear or td.speed_kmh < min_speed_kmh:
            return False
        if td.rpm_pct >= floor_pct:
            return False
        if not (self._speed_decreasing(0.6) or self._airtime.landing_recovery(now)):
            return False
        self._no_downshift_until = 0.0
        if not self._shift_down(td, lock_ms, state, sub, cascade_lock_s=0.30):
            return False
        self._no_upshift_until = now + upshift_lock_s
        return True

    def _track_upshift_in_band(
        self,
        td: Telemetry,
        now: float,
        offset: float,
        min_throttle: float = 0.05,
        *,
        downshift_lock_s: float = 1.0,
    ) -> bool:
        if td.throttle < min_throttle:
            return False
        if td.brake > 0.05:
            return False
        if now < self._no_upshift_until:
            return False
        if td.speed_kmh <= Cfg.MIN_SPEED_KMH:
            return False

        if self._race_first_gear_traction_hold(td, now):
            return True

        if self._race_wheel_speed_untrusted(td, now):
            return self._race_wheel_speed_hold(td, now, "upshift")

        if self.mode == Mode.RACE:
            self._race_shift_outcome_offset_rpm(td)

        performance_target = self._performance_upshift_target_pct(td, offset)
        if performance_target is not None:
            strategy_pct, strategy_source = performance_target
            ceiling_pct = self._upshift_ceiling_pct(td)
            base_pct = self._upshift_base_target_pct(
                td,
                strategy_pct,
                strategy_source,
                ceiling_pct,
            )
            if self.mode == Mode.RACE:
                base_pct = self._apply_shift_outcome_offset_pct(td, base_pct)
            target_pct, source = self._upshift_command_target_pct(td, base_pct, strategy_source)
            if td.rpm_pct < target_pct:
                return False
            return self._shift_up(
                td,
                300,
                "UPSHIFT",
                source,
                downshift_lock_s=downshift_lock_s,
                decision_extra=self._upshift_decision_fields(
                    td,
                    command_pct=target_pct,
                    command_source=source,
                    strategy_pct=strategy_pct,
                    strategy_source=strategy_source,
                    base_pct=base_pct,
                    ceiling_pct=ceiling_pct,
                    offset=offset,
                ),
            )

        if self._turbo_lag_block_upshift(td):
            return False

        if self.mode == Mode.RACE:
            target_pct, source = self._race_upshift_target_pct(td)
            target_pct = self._apply_shift_outcome_offset_pct(td, target_pct)
            if td.rpm_pct < target_pct:
                return False
            return self._shift_up(
                td,
                300,
                "UPSHIFT",
                source,
                downshift_lock_s=downshift_lock_s,
                decision_extra=self._upshift_decision_fields(
                    td,
                    command_pct=target_pct,
                    command_source=source,
                    strategy_pct=target_pct,
                    strategy_source=source,
                    base_pct=target_pct,
                    ceiling_pct=self._upshift_ceiling_pct(td),
                    offset=offset,
                ),
            )

        fallback = self._mode_upshift_fallback_pct()
        mature_curve = self._power_curve.has_mature_data(td.car_key)
        strategy_pct = self._power_curve.optimal_upshift_rpm(
            td,
            fallback=fallback,
            offset=offset,
            blend_fallback=not mature_curve,
        )
        ceiling_pct = self._upshift_ceiling_pct(td)
        base_pct = self._upshift_base_target_pct(td, strategy_pct, "in band", ceiling_pct)
        target_pct, source = self._upshift_command_target_pct(td, base_pct, "in band")
        if td.rpm_pct < target_pct:
            return False
        return self._shift_up(
            td,
            300,
            "UPSHIFT",
            source,
            downshift_lock_s=downshift_lock_s,
            decision_extra=self._upshift_decision_fields(
                td,
                command_pct=target_pct,
                command_source=source,
                strategy_pct=strategy_pct,
                strategy_source="in band",
                base_pct=base_pct,
                ceiling_pct=ceiling_pct,
                offset=offset,
            ),
        )

    def _race_upshift_target_pct(self, td: Telemetry) -> tuple[float, str]:
        if td.engine_max_rpm <= 0:
            return 0.98, "race fallback"

        learned = self._rev_limiter.effective_redline(td)
        if learned is not None:
            margin = self._race_limiter_margin_rpm(td)
            target = max(0.60, (learned - margin) / td.engine_max_rpm)
            return min(target, 0.985), f"learned limiter -{margin:.0f}"

        configured = self._config.get("race_up_wot", 98) / 100
        return max(0.88, min(0.99, configured)), "race fallback"

    def _mode_upshift_fallback_pct(self) -> float:
        if self.mode == Mode.OFFROAD:
            return self._config.get("offroad_up_wot", 90) / 100
        return self._config.get("race_up_wot", 98) / 100

    def _performance_upshift_target_pct(
        self,
        td: Telemetry,
        offset: float,
    ) -> tuple[float, str] | None:
        if not self._config.get("feat_power_curve"):
            return None
        if td.gear < 1 or td.gear >= 10 or td.engine_max_rpm <= 0:
            return None
        if td.throttle < 0.55:
            return None
        return self._learned_power_upshift_target_pct(td, offset)

    def _learned_power_upshift_target_pct(
        self,
        td: Telemetry,
        offset: float,
    ) -> tuple[float, str] | None:
        target = self._power_cross_upshift_target_pct(td, offset)
        if target is not None:
            return target
        return self._power_peak_upshift_target_pct(td, offset)

    def _power_peak_upshift_target_pct(
        self,
        td: Telemetry,
        offset: float,
    ) -> tuple[float, str] | None:
        if td.gear < 1 or td.gear >= 10 or td.engine_max_rpm <= 0:
            return None
        current_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear)
        next_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear + 1)
        if not current_ratio or not next_ratio:
            return None
        if not self._power_curve.has_mature_data(td.car_key):
            return None
        peak_abs_rpm = self._power_curve.peak_power_abs_rpm(td.car_key)
        if peak_abs_rpm is None:
            return None

        peak_pct = peak_abs_rpm / td.engine_max_rpm
        target_pct = max(0.50, min(0.985, peak_pct + offset))
        return min(target_pct, self._upshift_ceiling_pct(td)), "power peak"

    def _power_cross_upshift_target_pct(
        self,
        td: Telemetry,
        offset: float,
    ) -> tuple[float, str] | None:
        if td.gear < 1 or td.gear >= 10 or td.engine_max_rpm <= 0:
            return None
        current_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear)
        next_ratio = self._calibrator.ratio_for_gear(td.car_key, td.gear + 1)
        if not current_ratio or not next_ratio:
            return None
        if not self._power_curve.has_power_lookup(td.car_key):
            return None

        ceiling_pct = self._upshift_ceiling_pct(td)
        peak_abs_rpm = self._power_curve.peak_power_abs_rpm(td.car_key)
        peak_pct = peak_abs_rpm / td.engine_max_rpm if peak_abs_rpm is not None else None
        search_start = max(0.45, (peak_pct or 0.72) - 0.06)
        search_end = max(search_start, min(ceiling_pct, 0.992))
        step_pct = max(0.003, 50.0 / td.engine_max_rpm)
        first_cross: float | None = None
        best_pct = search_end
        best_delta = -1e9
        valid_points = 0

        pct = search_start
        while pct <= search_end + 1e-9:
            rpm = td.engine_max_rpm * pct
            next_rpm = rpm * next_ratio / current_ratio
            if next_rpm < max(td.idle_rpm * 1.15, 900.0):
                pct += step_pct
                continue
            cur_power = self._power_curve.power_at_rpm(td.car_key, rpm)
            next_power = self._power_curve.power_at_rpm(td.car_key, next_rpm)
            if cur_power is None or next_power is None:
                pct += step_pct
                continue
            valid_points += 1
            delta = next_power - cur_power
            if delta > best_delta:
                best_delta = delta
                best_pct = pct
            noise_margin_hp = max(1.0, min(6.0, cur_power * 0.01))
            if first_cross is None and delta >= -noise_margin_hp:
                first_cross = pct
                break
            pct += step_pct

        if valid_points < 3:
            return None

        if first_cross is not None:
            return min(max(first_cross, search_start), search_end), "power cross"

        current_power = self._power_curve.power_at_rpm(td.car_key, td.current_rpm)
        slope = self._power_curve.power_slope_at_rpm(td.car_key, td.current_rpm)
        if (
            current_power is not None
            and slope is not None
            and slope < -0.002
            and best_delta >= -max(2.0, min(12.0, current_power * 0.02))
            and td.rpm_pct >= max(search_start, (peak_pct or 0.72) + offset * 0.5)
        ):
            return min(max(best_pct, search_start), search_end), "falling power"

        # No power crossing before the usable ceiling: hold the gear for max
        # acceleration, but do not sit exactly on the fuel-cut guard.
        ceiling_guard_rpm = 120.0 if self.mode == Mode.RACE else 80.0
        ceiling_guard_pct = ceiling_guard_rpm / td.engine_max_rpm
        return max(search_start, search_end - ceiling_guard_pct), "power ceiling"

    def _optimal_shift_snapshot(self, td: Telemetry) -> dict:
        empty = {
            "optimal_shift_rpm": None,
            "optimal_shift_rpm_pct": None,
            "optimal_shift_from_gear": None,
            "optimal_shift_to_gear": None,
            "optimal_shift_source": "",
        }
        if not self._config.get("feat_power_curve"):
            return empty
        if not self._power_curve.has_mature_data(td.car_key):
            return empty
        target = self._learned_power_upshift_target_pct(td, offset=0.03)
        if target is None:
            return empty

        target_pct, source = target
        target_pct = self._upshift_base_target_pct(
            td,
            target_pct,
            source,
            self._upshift_ceiling_pct(td),
        )
        target_rpm = target_pct * td.engine_max_rpm
        return {
            "optimal_shift_rpm": round(target_rpm),
            "optimal_shift_rpm_pct": target_pct,
            "optimal_shift_from_gear": td.gear,
            "optimal_shift_to_gear": td.gear + 1,
            "optimal_shift_source": source,
        }

    def _upshift_ceiling_pct(self, td: Telemetry) -> float:
        learned = self._rev_limiter.effective_redline(td)
        if learned is not None and td.engine_max_rpm > 0:
            margin = 200.0
            if self.mode == Mode.RACE and td.throttle > 0.80:
                margin = self._race_limiter_margin_rpm(td)
            return max(0.50, min(0.992, (learned - margin) / td.engine_max_rpm))
        high_power_rpm = self._power_curve.max_high_power_rpm(td.car_key, min_peak_ratio=0.80)
        if high_power_rpm is not None and td.engine_max_rpm > 0:
            return max(0.50, min(0.975, (high_power_rpm - 200.0) / td.engine_max_rpm))
        # Keep a small safety margin for unknown cars, but no longer cap at 92%;
        # otherwise the limiter learner never gets high-RPM evidence.
        return 0.975

    def _should_engine_brake(self, td: Telemetry) -> bool:
        if not self._config.get("feat_engine_brake"):
            return False
        if td.throttle > 0.05 or td.brake > 0.05:
            return False
        if td.gear <= 2 or td.speed_kmh < 40.0:
            return False
        if len(self._speed_history) < 15:
            return False

        old_speed = sum(list(self._speed_history)[:5]) / 5
        new_speed = sum(list(self._speed_history)[-5:]) / 5
        return (new_speed - old_speed) > 2.0

    def _on_climb(self, td: Telemetry) -> bool:
        if td.throttle < 0.30 or td.brake > 0.05 or td.gear <= 1:
            return False
        if len(self._speed_history) < 15:
            return False
        recent = list(self._speed_history)
        old_speed = sum(recent[:5]) / 5
        new_speed = sum(recent[-5:]) / 5
        return (new_speed - old_speed) < -0.5

    def _min_sensible_speed_for_gear(self, td: Telemetry) -> float:
        target_rpm = td.engine_max_rpm * 0.25
        learned_speed = self._calibrator.speed_for_rpm(td.car_key, td.gear, target_rpm)
        if learned_speed is not None:
            return learned_speed
        if td.gear <= 1:
            return 0.0
        return max(0.0, (td.gear - 2) * 20 + 15)

    def _turbo_lag_block_upshift(self, td: Telemetry) -> bool:
        if not self._config.get("feat_turbo_compensate"):
            return False
        if td.boost_raw < 0.3 or td.throttle < 0.50:
            return False
        if td.rpm_pct > 0.85:
            return False

        # Use original fixed threshold (0.7) - learning disabled for stability
        if self._turbo_bar < td.boost_raw * 0.7:
            return True
        return False

    def _update_turbo(self, td: Telemetry, dt: float):
        if 0.01 < td.boost_raw < 5.0:
            target = min(td.boost_raw, 1.8)
        else:
            target = td.throttle * td.rpm_pct * 1.8

        if target > self._turbo_bar:
            self._turbo_bar += 3.5 * dt * (target - self._turbo_bar)
        else:
            self._turbo_bar -= 4.2 * dt * (self._turbo_bar - target)
        self._turbo_bar = max(0.0, min(self._turbo_bar, 1.8))

    def _update_attitude(self, td: Telemetry):
        speed = td.speed_effective_ms
        if speed < 5.0:
            self._attitude = "NEUTRAL"
            self._attitude_sub = "low speed"
            self._grip_usage = 0.0
            return

        lat_g = abs(self._g_lat)
        self._grip_usage = min(1.0, lat_g / 1.2)
        yaw_abs = abs(td.ang_vel_z)

        if lat_g < 0.3 and yaw_abs < 0.1:
            self._attitude = "NEUTRAL"
            self._attitude_sub = "straight or gentle"
        elif lat_g > 1.0 and yaw_abs > 0.5:
            self._attitude = "OVER"
            self._attitude_sub = "oversteering"
        elif lat_g > 0.6 and yaw_abs < 0.2:
            self._attitude = "UNDER"
            self._attitude_sub = "understeering"
        else:
            self._attitude = "NEUTRAL"
            self._attitude_sub = "grip ok"

    def _compute_shift_advisor(self, td: Telemetry):
        thr = td.throttle
        base_mode = self._last_drive_mode
        self._shift_advice = ""

        if base_mode == Mode.RACE:
            performance_target = self._performance_upshift_target_pct(td, offset=0.03)
            if performance_target is not None:
                up_pct = min(performance_target[0], self._upshift_ceiling_pct(td))
            else:
                up_pct, _source = self._race_upshift_target_pct(td)
        elif base_mode == Mode.OFFROAD:
            performance_target = self._performance_upshift_target_pct(td, offset=0.07)
            if performance_target is not None:
                up_pct = min(performance_target[0], self._upshift_ceiling_pct(td))
            else:
                up_pct = self._power_curve.optimal_upshift_rpm(
                    td, fallback=self._config.get("offroad_up_wot", 90) / 100, offset=0.07
                )
        elif base_mode == Mode.DRIFT:
            up_pct = self._config.get("drift_up", 92) / 100
        else:
            up_pct = self._curve(
                thr,
                self._config.get("comfort_up_idle", 40) / 100,
                self._config.get("comfort_up_mid", 58) / 100,
                self._config.get("comfort_up_wot", 82) / 100,
            )

        if td.rpm_pct >= up_pct and td.speed_kmh > Cfg.MIN_SPEED_KMH:
            self._shift_hint = f"UP to {td.gear + 1}"
            self._shift_advice = "up"
        elif td.rpm_pct < 0.30 and td.gear > 2 and thr > 0.30:
            self._shift_hint = f"DOWN to {td.gear - 1}"
            self._shift_advice = "down"
        elif td.brake > 0.50 and td.rpm_pct < 0.40 and td.gear > 1:
            self._shift_hint = f"DOWN to {td.gear - 1} (brake)"
            self._shift_advice = "down"
        else:
            self._shift_hint = ""
            self._shift_advice = ""

    def _learn_progress_label(self, progress: dict) -> str:
        confidence = int(round(float(progress.get("confidence", 0.0)) * 100))
        samples = int(progress.get("samples", 0))
        points = int(progress.get("points", 0))
        return f"{confidence}% / {samples} samples / {points} bins"

    def _race_loop_progress_label(self, car_key: tuple) -> str:
        ratios = self._calibrator.get_ratios(car_key)
        total_samples = 0
        ready_gears = 0
        for gear in sorted(ratios):
            if gear < 1 or gear + 1 not in ratios:
                continue
            status = self._shift_outcome.status(car_key, gear)
            total_samples += int(status["samples"])
            if status["ready"]:
                ready_gears += 1
        return f"{total_samples} Race loop samples / {ready_gears} ready gears"

    def _gear_learning_hint(self, td: Telemetry) -> str:
        if td.gear < 1:
            return "select 2nd or 3rd gear"
        if td.speed_kmh < GearRatioCalibrator.MIN_SPEED_KMH:
            return "drive above 25 km/h in 2nd-4th"
        if td.is_shifting:
            return "wait for shift to settle"
        if td.clutch_raw > 5:
            return "release clutch"
        if td.max_combined_slip > GearRatioCalibrator.MAX_CLEAN_SLIP:
            return "reduce wheelspin for ratio learning"
        if td.min_suspension_norm <= GearRatioCalibrator.MIN_SUSPENSION_NORM:
            return "stay grounded for ratio learning"
        if td.any_puddle or td.max_surface_rumble > GearRatioCalibrator.MAX_SURFACE_RUMBLE:
            return "use smoother dry pavement"
        return "hold steady throttle in each gear"

    def _mode_learn(self, td: Telemetry, now: float):
        del now
        self._shift_hint = ""
        self._shift_advice = ""
        progress = self._power_curve.learning_progress(td.car_key)
        progress_label = self._learn_progress_label(progress)
        ratios_ready = self._calibrator.has_data(td.car_key)
        curve_ready = self._power_curve.has_mature_data(td.car_key)
        lookup_ready = self._power_curve.has_power_lookup(td.car_key)

        if ratios_ready and curve_ready and lookup_ready:
            loop_label = self._race_loop_progress_label(td.car_key)
            self._tcu_state = "LEARN DONE"
            self._tcu_state_sub = (
                f"base model ready ({progress_label}); {loop_label}; switch to Race"
            )
            self._shift_hint = (
                "Base learning complete - use Race mode WOT 1-2, 2-3, 3-4 pulls "
                "to tune shift RPM"
            )
            return

        if not ratios_ready:
            self._tcu_state = "LEARN GEARS"
            self._tcu_state_sub = f"base step 1/2: {self._gear_learning_hint(td)}"
            self._shift_hint = "Learn base model first: steady 2nd-4th gear pulls on dry pavement"
            return

        clean, reason = self._power_curve.sample_status(td)
        if not clean:
            ready = reason.startswith("hold throttle") or reason in {
                "select 2nd or 3rd gear",
                "raise RPM above idle",
            }
            self._tcu_state = "LEARN READY" if ready else "LEARN PAUSED"
            self._tcu_state_sub = f"base step 2/2: {reason}; {progress_label}"
            self._shift_hint = (
                "Build power curve: straight dry road, 2nd/3rd gear, WOT to near redline"
            )
            return

        max_seen = progress.get("max_rpm")
        if isinstance(max_seen, (int, float)) and td.engine_max_rpm > 0:
            top_seen = max_seen / td.engine_max_rpm
        else:
            top_seen = 0.0

        self._tcu_state = "LEARNING"
        if top_seen < 0.82:
            self._tcu_state_sub = f"base step 2/2 clean; keep WOT higher ({progress_label})"
        else:
            self._tcu_state_sub = f"base step 2/2 clean pull; repeat if needed ({progress_label})"
        self._shift_hint = (
            "Hold WOT and shift before limiter; Race mode will learn final shift RPM"
        )

    def _launch_control(self, td: Telemetry, now: float) -> bool:
        is_stationary = td.speed_effective_ms < 3.0
        if is_stationary and td.gear == 1 and td.brake > 0.30 and td.throttle > 0.70:
            if not self._launch_armed:
                self._launch_armed = True
                self._no_upshift_until = now + 999
            self._tcu_state = "LAUNCH ARMED"
            self._tcu_state_sub = "release brake - hold throttle"
            return True

        if self._launch_armed and is_stationary and td.brake < 0.10 and td.throttle > 0.70:
            self._launch_armed = False
            self._no_upshift_until = 0.0
            self._tcu_state = "LAUNCHING !"
            self._tcu_state_sub = "full send"
            self._lock_until = now + 0.3
            return True

        if self._launch_armed and (td.throttle < 0.40 or td.speed_kmh > 5.0):
            self._launch_armed = False
            self._no_upshift_until = 0.0
        return False

    def _blocked_by_transient(self) -> str | None:
        if self._config.get("feat_airtime_lock") and self._airtime.is_airborne:
            return "AIRBORNE"
        if self._config.get("feat_transient_lock") and self._yaw_transient.is_blocking:
            return "CORRECTING"
        return None

    def _target_gear_for_braking(
        self, td: Telemetry, speed_override: float | None = None
    ) -> int | None:
        car_ratios = self._calibrator.get_ratios(td.car_key)
        if not car_ratios:
            return None
        speed = speed_override if speed_override is not None else td.speed_kmh
        if speed < 10.0:
            return 1

        peak_torque = self._power_curve.peak_torque_abs_rpm(td.car_key)
        peak_power = self._power_curve.peak_power_abs_rpm(td.car_key)
        if peak_torque is None or peak_power is None:
            target_rpm = td.engine_max_rpm * 0.70
        else:
            peak_power = max(peak_power, peak_torque)
            target_rpm = peak_torque + (peak_power - peak_torque) * 0.6

        best_gear: int | None = None
        best_diff = float("inf")
        lower_ratio_seen = any(1 <= gear < td.gear for gear in car_ratios)
        for gear in car_ratios:
            if gear < 1 or gear > 10:
                continue
            rpm_at_gear = self._calibrator.project_rpm_at_speed(td.car_key, gear, speed)
            if rpm_at_gear is None:
                continue
            if rpm_at_gear > td.engine_max_rpm * 0.95:
                continue
            diff = abs(rpm_at_gear - target_rpm)
            if diff < best_diff:
                best_diff = diff
                best_gear = gear
        if best_gear is None:
            return None
        if best_gear >= td.gear and not lower_ratio_seen:
            return None
        return min(best_gear, td.gear)

    def _is_spinning_not_traction(self, td: Telemetry) -> bool:
        if td.rear_slip < 1.2:
            return False
        if td.rpm_pct < 0.65:
            return False
        if len(self._speed_history) < 10:
            return False
        recent = list(self._speed_history)[-10:]
        old = sum(recent[:3]) / 3
        new = sum(recent[-3:]) / 3
        return (new - old) < 0.5

    def _mode_race(self, td: Telemetry, now: float):
        thr = td.throttle
        brake_thr = self._config.get("brake_thr", 35) / 100 * 0.6

        if (
            td.current_rpm < Cfg.ANTI_STALL_RPM
            and td.gear > 1
            and thr < 0.10
            and td.speed_kmh < 20.0
        ):
            self._shift_down(td, 350, "ANTI-STALL", "engine save")
            return

        if self._fuel_cut_escape_upshift(td, now):
            return

        if self._low_gear_limiter_guard_upshift(td, now):
            return

        blocker = self._blocked_by_transient()
        if blocker is not None:
            self._tcu_state = blocker
            self._tcu_state_sub = "holding decisions"
            return

        if self._landing_recovery_downshift(td, now, Mode.RACE):
            return

        if self._track_brake_down(
            td,
            now,
            brake_thr,
            lock_ms=250,
            track_brake=True,
            cascade_lock_s=0.25,
            engine_brake=True,
            engine_brake_max_current_pct=0.72,
            engine_brake_max_projected_pct=0.90,
        ):
            return

        race_floor = self._power_floor_pct(
            td,
            fallback=0.60,
            torque_offset=-0.02,
            min_floor=0.52,
            max_floor=0.72,
        )
        min_throttle = 0.50 if (now - self._last_hard_brake_time) < 2.0 else 0.66
        if self._track_power_demand_downshift(
            td,
            now,
            min_throttle=min_throttle,
            fallback_pct=0.72,
            target_bias=0.45,
            floor_pct=race_floor,
            lock_ms=300,
            state="RACE POWER DOWN",
            sub="power",
            min_gear=3,
            min_speed_kmh=35.0,
            allow_skip=True,
            cascade_lock_s=0.35,
            upshift_lock_s=0.70,
            min_upshift_reserve_rpm=500.0,
        ):
            return

        if self._track_out_of_band_kickdown(
            td,
            now,
            climb_only=True,
            lock_ms=360,
            cascade_lock_s=0.40,
            upshift_lock_s=0.75,
        ):
            return

        if self._track_coast_recovery_downshift(
            td,
            now,
            floor_pct=self._config.get("race_coast_rpm", 30) / 100,
            state="RACE RECOVER",
            sub="coast",
            min_gear=3,
            min_speed_kmh=35.0,
            lock_ms=280,
            upshift_lock_s=0.70,
        ):
            return

        # Use standard offset for RACE mode
        if self._track_upshift_in_band(td, now, offset=0.03, downshift_lock_s=0.55):
            return

        self._tcu_state = "RACE"
        self._tcu_state_sub = "in band"

    def _mode_drift(self, td: Telemetry, now: float):
        if self._config.get("feat_airtime_lock") and self._airtime.is_airborne:
            self._tcu_state = "AIRBORNE"
            self._tcu_state_sub = "drift - hold"
            return
        if self._landing_recovery_downshift(td, now, Mode.DRIFT):
            return
        if td.speed_kmh < 30.0:
            self._tcu_state = "DRIFT"
            self._tcu_state_sub = "low speed"
            return
        if td.rpm_pct < 0.20 and td.gear > 1:
            self._shift_down(td, 350, "DRIFT HOLD", "save engine", cascade_lock_s=0.42)
            return
        if td.rpm_pct < self._config.get("drift_down", 65) / 100 and td.gear > 1:
            floor = self._config.get("drift_down", 65) / 100
            if td.throttle > 0.20 and self._track_power_demand_downshift(
                td,
                now,
                min_throttle=0.20,
                fallback_pct=max(0.62, floor),
                target_bias=0.25,
                floor_pct=floor,
                lock_ms=300,
                state="DRIFT HOLD",
                sub="rpm low",
                min_gear=2,
                min_speed_kmh=30.0,
                allow_skip=False,
                cascade_lock_s=0.42,
                upshift_lock_s=0.75,
                block_spin=False,
            ):
                return
            self._shift_down(td, 300, "DRIFT HOLD", "rpm low", cascade_lock_s=0.42)
            return
        if td.rpm_pct >= self._config.get("drift_up", 92) / 100:
            self._shift_up(td, 300, "DRIFT HOLD", "limiter", downshift_lock_s=0.75)
            return
        self._tcu_state = "DRIFT HOLD"
        self._tcu_state_sub = "in power band"

    def _mode_offroad(self, td: Telemetry, now: float):
        thr = td.throttle
        brake_thr = self._config.get("brake_thr", 35) / 100

        if (
            td.current_rpm < Cfg.ANTI_STALL_RPM * 1.2
            and td.gear > 1
            and thr < 0.10
            and td.speed_kmh < 25.0
        ):
            self._shift_down(td, 400, "ANTI-STALL", "save engine")
            return

        blocker = self._blocked_by_transient()
        if blocker is not None:
            self._tcu_state = blocker
            self._tcu_state_sub = "offroad - hold"
            return

        if self._landing_recovery_downshift(td, now, Mode.OFFROAD):
            return

        if self._track_brake_down(
            td,
            now,
            brake_thr,
            lock_ms=300,
            track_brake=True,
            cascade_lock_s=0.30,
            engine_brake=True,
            engine_brake_max_current_pct=0.62,
            engine_brake_max_projected_pct=0.84,
        ):
            return

        down_rpm = self._config.get("offroad_down_rpm", 55) / 100
        offroad_floor = self._power_floor_pct(
            td,
            fallback=down_rpm,
            torque_offset=-0.08,
            min_floor=0.46,
            max_floor=0.64,
        )
        if self._track_power_demand_downshift(
            td,
            now,
            min_throttle=0.38,
            fallback_pct=0.62,
            target_bias=0.20,
            floor_pct=offroad_floor,
            lock_ms=380,
            state="TORQUE DOWN",
            sub="torque",
            min_gear=2,
            min_speed_kmh=10.0,
            allow_skip=True,
            cascade_lock_s=0.42,
            upshift_lock_s=0.95,
            block_spin=False,
        ):
            return

        if thr >= 0.40 and td.rpm_pct < down_rpm and td.gear > 1 and td.speed_kmh > 8.0:
            self._shift_down(td, 450, "TORQUE DOWN", "climbing", cascade_lock_s=0.45)
            self._no_upshift_until = now + 1.5
            return

        if self._track_out_of_band_kickdown(
            td,
            now,
            lock_ms=420,
            cascade_lock_s=0.45,
            upshift_lock_s=0.9,
        ):
            return

        if self._track_coast_recovery_downshift(
            td,
            now,
            floor_pct=self._config.get("offroad_coast_rpm", 32) / 100,
            state="OFFROAD RECOVER",
            sub="coast",
            min_gear=2,
            min_speed_kmh=15.0,
            lock_ms=340,
            upshift_lock_s=0.90,
        ):
            return

        if self._track_upshift_in_band(
            td,
            now,
            offset=0.07,
            min_throttle=0.20,
            downshift_lock_s=0.80,
        ):
            return

        self._tcu_state = "OFFROAD"
        self._tcu_state_sub = "torque ready"
