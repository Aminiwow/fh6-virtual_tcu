"""自适应换挡延迟学习器

测量从发送换挡指令到实际档位变化的延迟，用于优化预判补偿。
"""

from collections import deque


class ShiftLagLearner:
    """学习每辆车的换挡延迟时间"""

    MAX_SAMPLES = 15
    VALID_LAG_RANGE = (0.020, 0.150)  # 20-150ms合理范围

    def __init__(self):
        self._upshift_lags: dict[tuple, deque[float]] = {}
        self._downshift_lags: dict[tuple, deque[float]] = {}
        self._last_shift_command_time: float | None = None
        self._last_shift_command_gear: int | None = None
        self._last_shift_direction: str | None = None

    def record_shift_command(self, car_key: tuple, direction: str, gear: int, now: float):
        """记录换挡指令发出时刻"""
        self._last_shift_command_time = now
        self._last_shift_command_gear = gear
        self._last_shift_direction = direction

    def observe_gear_change(self, car_key: tuple, new_gear: int, now: float):
        """检测到档位变化，计算延迟"""
        if self._last_shift_command_time is None:
            return

        # 验证是否是我们发出的换挡
        expected_gear = None
        if self._last_shift_direction == "UP":
            expected_gear = self._last_shift_command_gear + 1
        elif self._last_shift_direction == "DOWN":
            expected_gear = self._last_shift_command_gear - 1

        if expected_gear != new_gear:
            return  # 玩家手动换挡或其他原因

        lag = now - self._last_shift_command_time

        # 验证延迟合理性
        if not (self.VALID_LAG_RANGE[0] <= lag <= self.VALID_LAG_RANGE[1]):
            return

        # 存储样本
        if self._last_shift_direction == "UP":
            samples = self._upshift_lags.setdefault(car_key, deque(maxlen=self.MAX_SAMPLES))
        else:
            samples = self._downshift_lags.setdefault(car_key, deque(maxlen=self.MAX_SAMPLES))

        samples.append(lag)

        # 清除状态
        self._last_shift_command_time = None
        self._last_shift_command_gear = None
        self._last_shift_direction = None

    def get_upshift_lag(self, car_key: tuple) -> float:
        """获取升档延迟（秒）"""
        samples = self._upshift_lags.get(car_key, deque())
        if len(samples) < 3:
            return 0.032  # 默认值

        # 使用中位数（抗离群值）
        sorted_samples = sorted(samples)
        mid = len(sorted_samples) // 2
        return sorted_samples[mid]

    def get_downshift_lag(self, car_key: tuple) -> float:
        """获取降档延迟（秒）"""
        samples = self._downshift_lags.get(car_key, deque())
        if len(samples) < 3:
            return 0.028  # 降档通常更快

        sorted_samples = sorted(samples)
        mid = len(sorted_samples) // 2
        return sorted_samples[mid]

    def dump(self, car_key: tuple) -> dict | None:
        """导出数据用于持久化"""
        up_samples = self._upshift_lags.get(car_key)
        down_samples = self._downshift_lags.get(car_key)

        if not up_samples and not down_samples:
            return None

        return {
            "upshift_lags": list(up_samples) if up_samples else [],
            "downshift_lags": list(down_samples) if down_samples else [],
        }

    def load(self, car_key: tuple, data: dict):
        """从持久化数据恢复"""
        if not isinstance(data, dict):
            return

        up_lags = data.get("upshift_lags", [])
        down_lags = data.get("downshift_lags", [])

        if up_lags:
            self._upshift_lags[car_key] = deque(up_lags, maxlen=self.MAX_SAMPLES)
        if down_lags:
            self._downshift_lags[car_key] = deque(down_lags, maxlen=self.MAX_SAMPLES)
