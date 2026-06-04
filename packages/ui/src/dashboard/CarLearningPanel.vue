<script setup lang="ts">
import type { TelemetrySnapshot } from '@virtual-tcu/shared/types/telemetry'
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
const statusText = computed(() => {
  const status = props.clearStatus
  if (!status)
    return props.telemetry?.using_cached_car ? 'Using last detected car' : 'Current telemetry car'
  if (status.ok)
    return 'Learning cleared'
  return status.error === 'no_current_car' ? 'No car cached yet' : 'Clear failed'
})

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
