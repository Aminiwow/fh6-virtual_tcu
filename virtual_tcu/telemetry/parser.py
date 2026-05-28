import struct

from virtual_tcu.telemetry.model import FH6_PACKET_SIZE, Telemetry


def parse_fh6_packet(data: bytes) -> Telemetry | None:
    # Production fail-safe length verification
    if len(data) < 324 or len(data) < FH6_PACKET_SIZE:
        return None

    try:
        is_race, session_ts, max_rpm, idle_rpm, cur_rpm = struct.unpack_from("<iIfff", data, 0)
        ax, ay, az = struct.unpack_from("<fff", data, 20)
        vx, vy, vz = struct.unpack_from("<fff", data, 32)
        avx, avy, avz = struct.unpack_from("<fff", data, 44)
        yaw, pitch, roll = struct.unpack_from("<fff", data, 56)
        susp_fl, susp_fr, susp_rl, susp_rr = struct.unpack_from("<ffff", data, 68)
        slip_ratio_fl, slip_ratio_fr, slip_ratio_rl, slip_ratio_rr = struct.unpack_from(
            "<ffff", data, 84
        )
        wheel_speed_fl, wheel_speed_fr, wheel_speed_rl, wheel_speed_rr = struct.unpack_from(
            "<ffff", data, 100
        )
        rumble_fl, rumble_fr, rumble_rl, rumble_rr = struct.unpack_from("<iiii", data, 116)
        puddle_fl, puddle_fr, puddle_rl, puddle_rr = struct.unpack_from("<iiii", data, 132)
        surface_rumble_fl, surface_rumble_fr, surface_rumble_rl, surface_rumble_rr = (
            struct.unpack_from("<ffff", data, 148)
        )
        slip_angle_fl, slip_angle_fr, slip_angle_rl, slip_angle_rr = struct.unpack_from(
            "<ffff", data, 164
        )
        slip_fl, slip_fr, slip_rl, slip_rr = struct.unpack_from("<ffff", data, 180)
        susp_m_fl, susp_m_fr, susp_m_rl, susp_m_rr = struct.unpack_from("<ffff", data, 196)
        car_ord, car_cls, pi, drivetrain, ncyl = struct.unpack_from("<iiiii", data, 212)
        car_group = struct.unpack_from("<I", data, 232)[0]
        smashable_vel_diff, smashable_mass = struct.unpack_from("<ff", data, 236)
        pos_x, pos_y, pos_z = struct.unpack_from("<fff", data, 244)
        speed, power, torque = struct.unpack_from("<fff", data, 256)
        boost = struct.unpack_from("<f", data, 284)[0]

        accel = data[315]
        brake = data[316]
        clutch = data[317]
        handbrake = data[318]
        gear = data[319]
        steer = struct.unpack_from("<b", data, 320)[0]
        driving_line = struct.unpack_from("<b", data, 321)[0]
        ai_brake_diff = struct.unpack_from("<b", data, 322)[0]
    except (struct.error, IndexError):
        return None

    is_shifting = gear > 10

    return Telemetry(
        is_race_on=is_race,
        engine_max_rpm=max_rpm,
        current_rpm=cur_rpm,
        accel_x=ax,
        accel_y=ay,
        accel_z=az,
        vel_x=vx,
        vel_y=vy,
        vel_z=vz,
        ang_vel_x=avx,
        ang_vel_y=avy,
        ang_vel_z=avz,
        yaw=yaw,
        pitch=pitch,
        roll=roll,
        suspension_norm_fl=susp_fl,
        suspension_norm_fr=susp_fr,
        suspension_norm_rl=susp_rl,
        suspension_norm_rr=susp_rr,
        slip_ratio_fl=slip_ratio_fl,
        slip_ratio_fr=slip_ratio_fr,
        slip_ratio_rl=slip_ratio_rl,
        slip_ratio_rr=slip_ratio_rr,
        wheel_speed_fl=wheel_speed_fl,
        wheel_speed_fr=wheel_speed_fr,
        wheel_speed_rl=wheel_speed_rl,
        wheel_speed_rr=wheel_speed_rr,
        rumble_strip_fl=rumble_fl,
        rumble_strip_fr=rumble_fr,
        rumble_strip_rl=rumble_rl,
        rumble_strip_rr=rumble_rr,
        puddle_fl=puddle_fl,
        puddle_fr=puddle_fr,
        puddle_rl=puddle_rl,
        puddle_rr=puddle_rr,
        surface_rumble_fl=surface_rumble_fl,
        surface_rumble_fr=surface_rumble_fr,
        surface_rumble_rl=surface_rumble_rl,
        surface_rumble_rr=surface_rumble_rr,
        slip_angle_fl=slip_angle_fl,
        slip_angle_fr=slip_angle_fr,
        slip_angle_rl=slip_angle_rl,
        slip_angle_rr=slip_angle_rr,
        speed_ms=speed,
        power_w=power,
        torque_nm=torque,
        boost_raw=boost,
        accel_raw=accel,
        brake_raw=brake,
        clutch_raw=clutch,
        gear=gear,
        car_ordinal=car_ord,
        car_class=car_cls,
        pi=pi,
        session_timestamp=session_ts,
        idle_rpm=idle_rpm,
        drivetrain=drivetrain,
        num_cylinders=ncyl,
        car_group=car_group,
        smashable_vel_diff=smashable_vel_diff,
        smashable_mass=smashable_mass,
        position_x=pos_x,
        position_y=pos_y,
        position_z=pos_z,
        slip_fl=slip_fl,
        slip_fr=slip_fr,
        slip_rl=slip_rl,
        slip_rr=slip_rr,
        suspension_m_fl=susp_m_fl,
        suspension_m_fr=susp_m_fr,
        suspension_m_rl=susp_m_rl,
        suspension_m_rr=susp_m_rr,
        handbrake_raw=handbrake,
        steer_raw=steer,
        normalized_driving_line=driving_line,
        normalized_ai_brake_difference=ai_brake_diff,
        is_shifting=is_shifting,
    )
