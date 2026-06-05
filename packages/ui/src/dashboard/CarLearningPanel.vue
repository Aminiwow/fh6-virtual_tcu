<script setup lang="ts">
import type {
  ShiftOutcomeGearStatus,
  TelemetrySnapshot,
} from '@virtual-tcu/shared/types/telemetry'
import { computed, ref, watch } from 'vue'
import { actionBtnDanger } from '../styles/ui'

const props = withDefaults(defineProps<{
  telemetry?: TelemetrySnapshot | null
  clearStatus?: { ok: boolean; error?: string; at: number } | null
}>(), {
  telemetry: null,
  clearStatus: null,
})

const emit = defineEmits<{
  clearLearning: []
}>()

const confirming = ref(false)

const carLabel = computed(() => {
  const t = props.telemetry
  if (!t?.car_ordinal)
    return 'NO CAR'
  return `${t.car_ordinal} / ${t.car_class ?? '-'} / PI ${t.pi ?? '-'}`
})

const canClear = computed(() => !!props.telemetry?.car_ordinal)
const shiftOutcomeRows = computed(() => {
  const rows = props.telemetry?.shift_guide?.shift_outcome_gears ?? []
  const currentGear = Number(props.telemetry?.gear ?? 0)
  const visible = rows.filter(
    row => row.samples > 0 || row.ready || row.gear === currentGear || row.gear <= 3,
  )
  return visible.slice(0, 5)
})
const shiftOutcomeTotal = computed(
  () => props.telemetry?.shift_guide?.shift_outcome_total_samples ?? 0,
)
const shiftOutcomeReady = computed(
  () => props.telemetry?.shift_guide?.shift_outcome_ready_gears ?? 0,
)
const statusText = computed(() => {
  const status = props.clearStatus
  if (!status)
    return props.telemetry?.using_cached_car ? 'Using last detected car' : 'Current telemetry car'
  if (status.ok)
    return 'Learning cleared'
  return status.error === 'no_current_car' ? 'No car cached yet' : 'Clear failed'
})

function offsetText(row: ShiftOutcomeGearStatus) {
  const offset = Math.round(row.offset_rpm || 0)
  if (offset > 0)
    return `+${offset} rpm`
  return `${offset} rpm`
}

function rewardText(row: ShiftOutcomeGearStatus) {
  if (row.recent_reward_kmh_s == null)
    return '--'
  return `${row.recent_reward_kmh_s.toFixed(1)} km/h/s`
}

function onClearClick() {
  if (!canClear.value)
    return
  if (!confirming.value) {
    confirming.value = true
    window.setTimeout(() => {
      confirming.value = false
    }, 2500)
    return
  }
  confirming.value = false
  emit('clearLearning')
}

watch(() => props.clearStatus?.at, () => {
  confirming.value = false
})
</script>

<template>
  <section class="border-tcu-border bg-tcu-bg-1 flex shrink-0 flex-col gap-3 rounded-lg border p-4">
    <div class="flex items-start justify-between gap-3">
      <div>
        <div class="text-tcu-txt-dim text-[10px] tracking-widest uppercase">Car Learning</div>
        <div class="mt-1 font-mono text-sm font-semibold text-white">
          {{ carLabel }}
        </div>
      </div>
      <span
        class="rounded-full px-2 py-0.5 text-[10px] font-bold tracking-wide uppercase"
        :class="telemetry?.using_cached_car ? 'bg-warn/15 text-warn' : 'bg-accent/15 text-accent'"
      >
        {{ telemetry?.using_cached_car ? 'cached' : 'live' }}
      </span>
    </div>
    <div class="border-tcu-border/70 bg-tcu-bg-2/60 rounded-md border p-2.5">
      <div class="flex items-center justify-between gap-2">
        <span class="text-tcu-txt-dim text-[10px] tracking-widest uppercase">Race Loop</span>
        <span class="font-mono text-[10px] font-bold text-white">
          {{ shiftOutcomeTotal }} samples / {{ shiftOutcomeReady }} ready
        </span>
      </div>
      <div v-if="shiftOutcomeRows.length" class="mt-2 grid gap-1.5">
        <div
          v-for="row in shiftOutcomeRows"
          :key="`${row.gear}-${row.to_gear}`"
          class="grid grid-cols-[42px_1fr_auto] items-center gap-2 font-mono text-[10px]"
        >
          <span class="font-bold text-white">{{ row.gear }}-&gt;{{ row.to_gear }}</span>
          <span class="text-tcu-txt-muted truncate">
            {{ row.samples }} samples / {{ rewardText(row) }}
          </span>
          <span
            class="font-bold tabular-nums"
            :class="row.ready ? 'text-accent' : 'text-tcu-txt-dim'"
          >
            {{ offsetText(row) }}
          </span>
        </div>
      </div>
      <div v-else class="text-tcu-txt-dim mt-2 text-[10px] leading-snug">
        No Race loop samples yet
      </div>
    </div>
    <button
      type="button"
      :class="actionBtnDanger"
      :disabled="!canClear"
      @click="onClearClick"
    >
      {{ confirming ? 'Confirm clear' : 'Clear this car learning' }}
    </button>
    <div class="text-tcu-txt-dim text-[10px] leading-snug">
      {{ statusText }}
    </div>
  </section>
</template>
