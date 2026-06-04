import type { LogStatus, TelemetrySnapshot } from '@virtual-tcu/shared/types/telemetry'
import type { Ref } from 'vue'
import { computed } from 'vue'

export function useModeSidebar(
  telemetry: Ref<TelemetrySnapshot | null>,
  logStatus: Ref<LogStatus | null>,
) {
  const hasTorque = computed(() => telemetry.value?.peak_torque_rpm_pct != null)
  const hasPower = computed(() => telemetry.value?.peak_power_rpm_pct != null)

  const peakTorqueText = computed(() => {
    const p = telemetry.value?.peak_torque_rpm_pct
    return p == null ? '' : `${Math.round(p * 100)}% RPM`
  })

  const peakPowerText = computed(() => {
    const p = telemetry.value?.peak_power_rpm_pct
    return p == null ? '' : `${Math.round(p * 100)}% RPM`
  })

  const peakRpm = computed(() => Math.round(telemetry.value?.peak_rpm ?? 0))
  const peakG = computed(() => (telemetry.value?.peak_g ?? 0).toFixed(2))

  const logMode = computed(() =>
    logStatus.value?.mode === 'off' ? '—' : (logStatus.value?.mode ?? '—'),
  )
  const logSize = computed(() => `${logStatus.value?.size_kb ?? 0} KB`)

  return {
    hasTorque,
    hasPower,
    peakTorqueText,
    peakPowerText,
    peakRpm,
    peakG,
    logMode,
    logSize,
  }
}
