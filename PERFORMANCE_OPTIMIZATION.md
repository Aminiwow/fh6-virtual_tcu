# 🏎️ 游戏内性能优化指南

## 已实现的优化

### 1. 自适应换挡延迟学习 ✅
**文件**: `virtual_tcu/learning/shift_lag.py`

**效果**:
- 每辆车学习实际换挡延迟（20-150ms范围）
- 自动调整预判补偿，减少过早/过晚换挡
- 预期提升：圈速 **0.2-0.5秒**（快车更明显）

**如何启用**:
已集成到 TCULogic，自动学习，无需手动配置。

---

## 待实现的优化

### 2. 功率曲线平滑化 🎯 **推荐优先实现**

**当前问题**:
- 50 RPM分箱 + 线性插值 → 功率曲线有阶跃
- 在功率交叉点附近可能换挡不精确

**解决方案**: 三次埃尔米特插值

**预期效果**:
- 换挡点精度：±30 RPM → ±10 RPM
- 消除"犹豫换挡"现象
- 圈速提升：**0.1-0.3秒**

**实现位置**: `virtual_tcu/learning/power_curve.py` 的 `power_at_rpm()` 方法

---

### 3. 齿轮比学习速度优化 ⚡

**当前状态**:
- 需要多次clean样本才能学习（`OUTLIER_GRACE = 5`）
- 在打滑/跳跃多的情况下学习缓慢

**优化方案**:
```python
# gear_ratio.py
class GearRatioCalibrator:
    # 根据样本质量动态调整学习率
    def _adaptive_learn_rate(self, td: Telemetry, current_count: int) -> float:
        base_rate = max(0.08, 1.0 / (current_count + 1))
        
        # 高质量样本加速学习
        quality_score = 1.0
        if td.max_combined_slip < 0.3:  # 几乎无打滑
            quality_score *= 1.5
        if td.min_suspension_norm > 0.3:  # 完全着地
            quality_score *= 1.2
        if td.throttle > 0.5 and td.brake < 0.1:  # 稳定加速
            quality_score *= 1.1
        
        return min(0.25, base_rate * quality_score)
```

**预期效果**:
- 学习时间：30秒 → **10秒**
- 新车更快达到最佳性能

---

### 4. 转速限制器学习精度提升 🎯

**当前问题**:
- `rev_limiter.py` 在某些车上学到错误的限制器（如6800而非8000）
- 原因：高档位正功率平台被误判为燃油切断

**优化方案**:
```python
# rev_limiter.py:66
def _cut_like_power(self, car: tuple, td: Telemetry) -> bool:
    # 新增：检查功率导数
    if len(self._power_history.get(car, [])) >= 3:
        recent_powers = self._power_history[car][-3:]
        power_trend = (recent_powers[-1] - recent_powers[0]) / 2
        
        # 如果功率还在上升，不是燃油切断
        if power_trend > 5.0:  # 5 HP/帧增长
            return False
    
    # 原有逻辑...
    current_peak = self._high_power_peak.get(car, 0.0)
    return current_peak >= 50.0 and power_hp <= current_peak * 0.78
```

**预期效果**:
- 避免错误的低限制器学习
- Race模式可以正确用到红线区

---

### 5. 功率需求降档预判 🚀

**当前状态**:
- 油门全开但转速低时才降档
- 在弯道出弯时可能损失0.1-0.2秒

**优化方案**: 提前预判出弯需求
```python
# logic/tcu.py 新增方法
def _predict_corner_exit(self, td: Telemetry) -> bool:
    """预测即将出弯"""
    # 条件：刹车释放 + 转向角减小 + 低转速
    if len(self._brake_history) < 10:
        return False
    
    brake_trend = self._brake_history[-1] - self._brake_history[-5]
    yaw_trend = abs(td.ang_vel_z) - abs(self._yaw_history[-5] if len(self._yaw_history) >= 5 else td.ang_vel_z)
    
    return (
        brake_trend < -0.15 and  # 刹车正在释放
        yaw_trend < -0.05 and    # 转向角正在减小
        td.rpm_pct < 0.60 and    # 转速偏低
        td.throttle > 0.30        # 开始给油
    )

# 在 _mode_race 中使用
if self._predict_corner_exit(td):
    # 提前0.2秒降档
    if self._track_power_demand_downshift(...):
        return
```

**预期效果**:
- 出弯加速响应更快
- 每个弯节省 **0.05-0.15秒**

---

### 6. 涡轮迟滞补偿优化 🌪️

**当前实现**:
```python
# logic/tcu.py:2024
def _turbo_lag_block_upshift(self, td: Telemetry) -> bool:
    if td.boost_raw < 0.3:
        return False
    if self._turbo_bar < td.boost_raw * 0.7:
        return True  # 阻止换挡
```

**问题**: 阈值固定，不适应所有涡轮车

**优化方案**: 学习每辆车的涡轮特性
```python
class TurboCharacteristics:
    def __init__(self):
        self._spool_times: dict[tuple, list[float]] = {}
        self._boost_curves: dict[tuple, dict[int, float]] = {}
    
    def observe_spool(self, car_key: tuple, rpm_pct: float, spool_time: float):
        """记录涡轮启动时间"""
        # 分档位记录
        ...
    
    def should_hold_gear_for_boost(self, car_key: tuple, td: Telemetry) -> bool:
        """基于学习的涡轮特性决定是否保持档位"""
        learned_spool = self._spool_times.get(car_key, {}).get(td.gear)
        if learned_spool and learned_spool > 0.8:  # 涡轮迟滞严重
            # 更激进地保持档位
            return self._turbo_bar < td.boost_raw * 0.85
        else:
            # 正常阈值
            return self._turbo_bar < td.boost_raw * 0.70
```

**预期效果**:
- 涡轮车加速更线性
- 减少"换挡后失去动力"的情况

---

### 7. 抓地力自适应换挡 🏁

**场景**: 湿地、泥地、雪地等低抓地力情况

**优化方案**: 根据打滑程度调整换挡点
```python
def _grip_adaptive_upshift_offset(self, td: Telemetry) -> float:
    """根据抓地力动态调整换挡点"""
    base_offset = 0.03
    
    # 检测持续打滑
    slip_score = td.max_combined_slip
    
    if slip_score > 1.5:  # 明显打滑
        # 提前换挡，用更高档位减少扭矩
        return base_offset + 0.05
    elif slip_score > 0.8:
        return base_offset + 0.02
    
    # 检测路面类型（通过表面震动）
    if td.max_surface_rumble > 0.15:  # 粗糙路面
        return base_offset + 0.03
    
    return base_offset

# 在换挡逻辑中使用
offset = self._grip_adaptive_upshift_offset(td)
if self._track_upshift_in_band(td, now, offset):
    return
```

**预期效果**:
- 越野/雨天更稳定
- 减少失控次数

---

## 性能测量工具

### 添加实时性能监控
```python
# state/performance_monitor.py
class PerformanceMonitor:
    """监控换挡性能指标"""
    
    def __init__(self):
        self._shift_accuracy: deque[float] = deque(maxlen=100)
        self._optimal_shift_hits = 0
        self._total_shifts = 0
    
    def record_shift(self, td: Telemetry, decision: str, optimal_rpm: float):
        """记录换挡并计算精度"""
        if optimal_rpm > 0:
            accuracy = 1.0 - abs(td.current_rpm - optimal_rpm) / td.engine_max_rpm
            self._shift_accuracy.append(accuracy)
            
            if abs(td.current_rpm - optimal_rpm) < 100:
                self._optimal_shift_hits += 1
        
        self._total_shifts += 1
    
    def get_stats(self) -> dict:
        """获取性能统计"""
        if not self._shift_accuracy:
            return {"accuracy": 0, "optimal_rate": 0}
        
        return {
            "avg_accuracy": sum(self._shift_accuracy) / len(self._shift_accuracy),
            "optimal_rate": self._optimal_shift_hits / max(1, self._total_shifts),
            "recent_accuracy": list(self._shift_accuracy)[-10:],
        }
```

在Dashboard显示：
- 平均换挡精度
- 最优换挡命中率
- 近10次换挡趋势图

---

## 优化优先级排序

### 🔴 立即实现（预期最大收益）
1. **功率曲线平滑化** - 0.1-0.3秒/圈
2. **自适应换挡延迟** - 已实现 ✅
3. **转速限制器学习修复** - 避免错误红线

### 🟡 短期实现（1-2周）
4. **功率需求降档预判** - 出弯提速
5. **齿轮比学习加速** - 新车快速达到最佳

### 🟢 长期优化（可选）
6. **涡轮迟滞补偿** - 特定车型受益
7. **抓地力自适应** - 特殊路况受益

---

## 预期总体提升

在赛道测试中（以Goliath赛道为例）：

| 优化项 | 预期提升 |
|--------|---------|
| 功率曲线平滑化 | 0.2秒 |
| 自适应延迟 | 0.3秒 |
| 转速限制器修复 | 0.5秒（如果之前学错了）|
| 出弯预判降档 | 0.4秒（10个弯×0.04秒）|
| **总计** | **1.0-1.4秒** |

对于5分钟的赛道，这相当于 **0.3-0.5% 的圈速提升**。

---

## 实现顺序建议

1. **今天**: 实现功率曲线平滑化（最简单，收益明显）
2. **明天**: 修复转速限制器学习bug
3. **本周**: 添加出弯预判降档
4. **下周**: 优化齿轮比学习速度
5. **有空时**: 涡轮和抓地力自适应

每个优化都可以独立实现和测试，不会互相干扰。
