<script setup lang="ts">
  import type { LogStatus, TelemetrySnapshot } from '@virtual-tcu/shared/types/telemetry'
  import { DRIVE_MODES } from '@virtual-tcu/shared/config/modes'
  import { modeBtnClass } from '@virtual-tcu/shared/utils/mode-colors'
  import { toRefs } from 'vue'
  import {
    actionBtn,
    actionBtnDanger,
    actionBtnPrimary,
    cardSm,
    col,
    sectionTitle,
  } from '../styles/ui'
  import { useModeSidebar } from './mode-sidebar'

  const props = withDefaults(
    defineProps<{
      mode: string
      shiftCount: number
      packetsTotal: number
      telemetry?: TelemetrySnapshot | null
      logStatus?: LogStatus | null
      interactive?: boolean
    }>(),
    {
      telemetry: null,
      logStatus: null,
      interactive: true,
    },
  )
  const emit = defineEmits<{
    setMode: [mode: string]
    logStart: [mode: string]
    logStop: []
  }>()

  const { telemetry, logStatus, interactive } = toRefs(props)
  const {
    hasTorque,
    hasPower,
    peakTorqueText,
    peakPowerText,
    peakRpm,
    peakG,
    logMode,
    logSize,
  } = useModeSidebar(telemetry, logStatus)

  const isRecording = () => !!logStatus.value?.recording
</script>

<template>
  <div :class="col">
    <h3 :class="sectionTitle">
      {{ $t('modes.title') }}
    </h3>
    <template v-if="interactive">
      <button
        v-for="m in DRIVE_MODES"
        :key="m.id"
        type="button"
        :class="modeBtnClass(m.id, mode === m.id)"
        @click="emit('setMode', m.id)"
      >
        <span class="font-medium">{{ $t(`modes.${m.i18nKey}.name`) }}</span>
        <span
          class="bg-tcu-bg-3 text-tcu-txt-dim rounded px-1.5 py-0.5 text-[10px] tracking-wide uppercase"
          :class="mode === m.id && 'bg-white/5 text-current'"
        >
          {{ $t(`modes.${m.i18nKey}.tag`) }}
        </span>
      </button>
    </template>
    <template v-else>
      <div
        v-for="m in DRIVE_MODES"
        :key="m.id"
        class="flex items-center justify-between rounded-md border px-3 py-2.5 text-sm"
        :class="
          mode === m.id
            ? modeBtnClass(m.id, true)
            : 'border-tcu-border bg-tcu-bg-1 text-tcu-txt-dim opacity-60'
        "
      >
        <span class="font-medium">{{ $t(`modes.${m.i18nKey}.name`) }}</span>
        <span class="text-[10px] tracking-wide uppercase">{{ $t(`modes.${m.i18nKey}.tag`) }}</span>
      </div>
    </template>
    <div class="bg-tcu-bg-1 text-tcu-txt-dim mt-3 rounded-md p-2.5 text-center text-xs">
      {{ $t('modes.hotkeyHintBefore') }}<kbd>F9</kbd><br />{{ $t('modes.hotkeyHintAfter') }}
    </div>

    <h3 class="mt-6" :class="[sectionTitle]">
      {{ $t('powerBand.title') }}
    </h3>
    <div class="text-tcu-txt-muted mt-2.5 text-[11px]" :class="[cardSm]">
      <div class="flex justify-between py-0.5">
        <span class="text-tcu-txt-dim">{{ $t('powerBand.peakTorque') }}</span>
        <span
          class="text-tcu-txt font-mono font-semibold"
          :class="!hasTorque && 'text-warn font-normal'"
        >
          {{ hasTorque ? peakTorqueText : $t('powerBand.learning') }}
        </span>
      </div>
      <div class="flex justify-between py-0.5">
        <span class="text-tcu-txt-dim">{{ $t('powerBand.peakPower') }}</span>
        <span
          class="text-tcu-txt font-mono font-semibold"
          :class="!hasPower && 'text-warn font-normal'"
        >
          {{ hasPower ? peakPowerText : $t('powerBand.learning') }}
        </span>
      </div>
      <div class="text-tcu-txt-dim mt-1.5 text-[10px] leading-snug">
        {{ $t('powerBand.hint') }}
      </div>
    </div>

    <h3 class="mt-6" :class="[sectionTitle]">
      {{ $t('logger.title') }}
    </h3>
    <div class="bg-tcu-bg-1 text-tcu-txt-muted mb-2 rounded-md p-2.5 text-xs">
      <div class="mb-1 flex justify-between">
        <span>{{ $t('logger.status') }}:</span>
        <span :class="isRecording() && 'text-danger font-semibold before:content-[\'●_\']'">
          {{ isRecording() ? $t('logger.recording') : $t('logger.stopped') }}
        </span>
      </div>
      <div class="mb-1 flex justify-between">
        <span>{{ $t('logger.mode') }}:</span><span>{{ logMode }}</span>
      </div>
      <div class="mb-1 flex justify-between">
        <span>{{ $t('logger.packets') }}:</span><span>{{ logStatus?.packets ?? 0 }}</span>
      </div>
      <div class="flex justify-between">
        <span>{{ $t('logger.size') }}:</span><span>{{ logSize }}</span>
      </div>
    </div>
    <button
      v-if="interactive"
      type="button"
      :class="actionBtnPrimary"
      :disabled="logStatus?.recording"
      @click="emit('logStart', 'events')"
    >
      {{ $t('logger.startEvents') }}
    </button>
    <button
      v-if="interactive"
      type="button"
      :class="actionBtn"
      :disabled="logStatus?.recording"
      @click="emit('logStart', 'all')"
    >
      {{ $t('logger.startAll') }}
    </button>
    <button
      v-if="interactive"
      type="button"
      :class="actionBtnDanger"
      :disabled="!logStatus?.recording"
      @click="emit('logStop')"
    >
      {{ $t('logger.stop') }}
    </button>
    <div v-if="!interactive" class="text-tcu-txt-dim text-[10px] leading-snug">
      {{ $t('logger.viewOnlyHint') }}
    </div>
    <div v-if="interactive" class="text-tcu-txt-dim mt-1 text-[10px] leading-snug">
      {{ $t('logger.hint') }}
    </div>

    <h3 class="mt-6" :class="[sectionTitle]">
      {{ $t('session.title') }}
    </h3>
    <div class="grid grid-cols-2 gap-2">
      <div class="bg-tcu-bg-1 rounded-md p-2.5">
        <div class="text-tcu-txt-dim text-[10px] uppercase">{{ $t('session.shifts') }}</div>
        <div class="mt-0.5 font-mono text-base font-semibold">{{ shiftCount }}</div>
      </div>
      <div class="bg-tcu-bg-1 rounded-md p-2.5">
        <div class="text-tcu-txt-dim text-[10px] uppercase">{{ $t('session.packets') }}</div>
        <div class="mt-0.5 font-mono text-base font-semibold">{{ packetsTotal }}</div>
      </div>
      <div class="bg-tcu-bg-1 rounded-md p-2.5">
        <div class="text-tcu-txt-dim text-[10px] uppercase">{{ $t('session.peakRpm') }}</div>
        <div class="mt-0.5 font-mono text-base font-semibold">{{ peakRpm }}</div>
      </div>
      <div class="bg-tcu-bg-1 rounded-md p-2.5">
        <div class="text-tcu-txt-dim text-[10px] uppercase">{{ $t('session.peakG') }}</div>
        <div class="mt-0.5 font-mono text-base font-semibold">{{ peakG }}</div>
      </div>
    </div>
  </div>
</template>
