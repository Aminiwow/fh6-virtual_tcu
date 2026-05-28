from dataclasses import dataclass

FH6_PACKET_SIZE = 324


@dataclass
class Telemetry:
    is_race_on: int = 0
    engine_max_rpm: float = 8000.0
    current_rpm: float = 0.0
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 0.0
    vel_x: float = 0.0
    vel_y: float = 0.0
    vel_z: float = 0.0
    ang_vel_x: float = 0.0
    ang_vel_y: float = 0.0
    ang_vel_z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    suspension_norm_fl: float = 0.0
    suspension_norm_fr: float = 0.0
    suspension_norm_rl: float = 0.0
    suspension_norm_rr: float = 0.0
    slip_ratio_fl: float = 0.0
    slip_ratio_fr: float = 0.0
    slip_ratio_rl: float = 0.0
    slip_ratio_rr: float = 0.0
    wheel_speed_fl: float = 0.0
    wheel_speed_fr: float = 0.0
    wheel_speed_rl: float = 0.0
    wheel_speed_rr: float = 0.0
    rumble_strip_fl: int = 0
    rumble_strip_fr: int = 0
    rumble_strip_rl: int = 0
    rumble_strip_rr: int = 0
    puddle_fl: int = 0
    puddle_fr: int = 0
    puddle_rl: int = 0
    puddle_rr: int = 0
    surface_rumble_fl: float = 0.0
    surface_rumble_fr: float = 0.0
    surface_rumble_rl: float = 0.0
    surface_rumble_rr: float = 0.0
    slip_angle_fl: float = 0.0
    slip_angle_fr: float = 0.0
    slip_angle_rl: float = 0.0
    slip_angle_rr: float = 0.0
    speed_ms: float = 0.0
    power_w: float = 0.0
    torque_nm: float = 0.0
    boost_raw: float = 0.0
    accel_raw: int = 0
    brake_raw: int = 0
    clutch_raw: int = 0
    gear: int = 0
    car_ordinal: int = 0
    car_class: int = 0
    pi: int = 0
    session_timestamp: int = 0
    idle_rpm: float = 0.0
    drivetrain: int = 0
    num_cylinders: int = 0
    car_group: int = 0
    smashable_vel_diff: float = 0.0
    smashable_mass: float = 0.0
    position_x: float = 0.0
    position_y: float = 0.0
    position_z: float = 0.0
    slip_fl: float = 0.0
    slip_fr: float = 0.0
    slip_rl: float = 0.0
    slip_rr: float = 0.0
    suspension_m_fl: float = 0.0
    suspension_m_fr: float = 0.0
    suspension_m_rl: float = 0.0
    suspension_m_rr: float = 0.0
    handbrake_raw: int = 0
    steer_raw: int = 0
    normalized_driving_line: int = 0
    normalized_ai_brake_difference: int = 0
    is_shifting: bool = False

    @property
    def rear_slip(self) -> float:
        return max(abs(self.slip_rl), abs(self.slip_rr))

    @property
    def front_slip(self) -> float:
        return max(abs(self.slip_fl), abs(self.slip_fr))

    @property
    def max_combined_slip(self) -> float:
        return max(self.front_slip, self.rear_slip)

    @property
    def suspension_norm(self) -> tuple[float, float, float, float]:
        return (
            self.suspension_norm_fl,
            self.suspension_norm_fr,
            self.suspension_norm_rl,
            self.suspension_norm_rr,
        )

    @property
    def min_suspension_norm(self) -> float:
        return min(self.suspension_norm)

    @property
    def all_suspension_stretched(self) -> bool:
        return max(self.suspension_norm) <= 0.08

    @property
    def is_grounded(self) -> bool:
        return self.min_suspension_norm > 0.08

    @property
    def max_surface_rumble(self) -> float:
        return max(
            abs(self.surface_rumble_fl),
            abs(self.surface_rumble_fr),
            abs(self.surface_rumble_rl),
            abs(self.surface_rumble_rr),
        )

    @property
    def any_puddle(self) -> bool:
        return bool(self.puddle_fl or self.puddle_fr or self.puddle_rl or self.puddle_rr)

    @property
    def wheel_speeds(self) -> tuple[float, float, float, float]:
        return (
            self.wheel_speed_fl,
            self.wheel_speed_fr,
            self.wheel_speed_rl,
            self.wheel_speed_rr,
        )

    @property
    def driven_wheel_speed_rad_s(self) -> float:
        fl, fr, rl, rr = (abs(w) for w in self.wheel_speeds)
        if self.drivetrain == 0:
            return (fl + fr) / 2.0
        if self.drivetrain == 1:
            return (rl + rr) / 2.0
        return (fl + fr + rl + rr) / 4.0

    @property
    def speed_kmh(self) -> float:
        if 0.0 <= self.speed_ms < 200.0:
            return self.speed_ms * 3.6
        mag = (self.vel_x**2 + self.vel_y**2 + self.vel_z**2) ** 0.5
        return mag * 3.6

    @property
    def speed_effective_ms(self) -> float:
        if 0.0 <= self.speed_ms < 200.0:
            return self.speed_ms
        return (self.vel_x**2 + self.vel_y**2 + self.vel_z**2) ** 0.5

    @property
    def rpm_pct(self) -> float:
        return self.current_rpm / self.engine_max_rpm if self.engine_max_rpm > 0 else 0.0

    @property
    def throttle(self) -> float:
        return self.accel_raw / 255.0

    @property
    def brake(self) -> float:
        return self.brake_raw / 255.0

    @property
    def car_key(self) -> tuple[int, int, int]:
        """Composite vehicle identifier — distinct per car model *and* tune.

        ``car_ordinal`` alone is not enough: the same car with a different
        tune (gearing / engine swap / PI change) keeps the same ordinal,
        so the learning systems would silently reuse stale data.
        Including ``car_class`` and ``pi`` disambiguates tuned variants."""
        return (self.car_ordinal, self.car_class, self.pi)

    @property
    def drivetrain_name(self) -> str:
        return {0: "FWD", 1: "RWD", 2: "AWD"}.get(self.drivetrain, "—")
