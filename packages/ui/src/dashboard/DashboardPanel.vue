<script setup lang="ts">
  import type { TelemetrySnapshot } from '@virtual-tcu/shared/types/telemetry'
  import { toRefs } from 'vue'
  import CarLearningPanel from './CarLearningPanel.vue'
  import DashboardChart from './DashboardChart.vue'
  import { useDashboardPanel } from './dashboard-panel'

  const props = withDefaults(
    defineProps<{
      live: boolean
      telemetry?: TelemetrySnapshot | null
      clearStatus?: { ok: boolean; error?: string; at: number } | null
    }>(),
    {
      telemetry: null,
      clearStatus: null,
    },
  )
  const emit = defineEmits<{
    clearLearning: []
  }>()
  const { live, telemetry } = toRefs(props)

  const {
    t,
    powerKw,
    torqueNm,
    turboBar,
    gLat,
    gLon,
    gripUsage,
    state,
    subState,
    attitude,
    optimalShiftText,
    isAirborne,
    isYawLocked,
    gDotStyle,
  } = useDashboardPanel(telemetry)
</script>

<template>
  <div
    class="bg-tcu-bg-0 border-tcu-border text-tcu-txt flex h-full min-h-0 flex-col justify-start gap-3 overflow-hidden border-l p-4 font-mono select-none"
  >
    <div v-if="telemetry" class="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden">
      <div class="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_260px] gap-3 overflow-hidden max-[900px]:grid-cols-1">
        <div class="flex min-h-0 flex-col gap-3 overflow-hidden">
          <div class="min-h-[340px] flex-1">
            <DashboardChart :telemetry="telemetry" />
          </div>

          <div class="grid h-[156px] shrink-0 grid-cols-[minmax(0,1fr)_200px_200px] gap-3 overflow-hidden max-[900px]:h-auto max-[900px]:grid-cols-1">
          <div
            class="bg-tcu-bg-1 relative flex flex-col justify-center overflow-hidden rounded-lg border p-5"
            :class="
              state === 'SHIFTING'
                ? 'border-accent-2/50 bg-accent-2/5'
                : state.includes('LOCK') || state.includes('BLOCKED')
                  ? 'border-danger/50 bg-danger/5'
                  : 'border-tcu-border'
            "
          >
            <div
              class="text-tcu-txt-dim absolute top-3 left-4 text-[10px] tracking-widest uppercase"
            >
              TCU Controller Status
            </div>
            <div
              class="mt-2 text-3xl font-bold tracking-wide"
              :class="
                state.includes('DOWN')
                  ? 'text-warn'
                  : state.includes('UP')
                    ? 'text-accent'
                    : 'text-white'
              "
            >
              {{ state }}
            </div>
            <div class="text-tcu-txt-muted mt-1 text-sm font-semibold uppercase">
              {{ subState }}
            </div>

            <div class="absolute top-3 right-4 flex gap-2">
              <span v-if="!live" class="ui-badge-warn">CACHED</span>
              <span v-if="isAirborne" class="ui-badge-danger animate-pulse">AIRBORNE</span>
              <span v-if="isYawLocked" class="ui-badge-warn animate-pulse">YAW LOCK</span>
            </div>
          </div>

          <div
            class="border-tcu-border bg-tcu-bg-1 flex flex-col overflow-hidden rounded-lg border p-4"
          >
            <div class="text-tcu-txt-dim mb-3 text-[10px] tracking-widest uppercase">Dynamics</div>

            <div class="mb-1 flex items-center justify-between">
              <span class="text-tcu-txt-muted text-xs">Attitude</span>
              <span
                class="text-xs font-bold"
                :class="
                  attitude === 'OVER'
                    ? 'text-danger'
                    : attitude === 'UNDER'
                      ? 'text-warn'
                      : 'text-accent'
                "
              >
                {{ attitude }}
              </span>
            </div>

            <div class="mt-auto">
              <div class="mb-1 flex items-end justify-between">
                <span class="text-tcu-txt-dim text-[10px] uppercase">Grip Usage</span>
                <span class="text-xs font-bold text-white tabular-nums"
                  >{{ Math.round(gripUsage) }}%</span
                >
              </div>
              <div class="bg-tcu-bg-3 h-1.5 w-full overflow-hidden rounded-full">
                <div
                  class="h-full transition-all duration-75"
                  :class="gripUsage > 90 ? 'bg-danger' : gripUsage > 75 ? 'bg-warn' : 'bg-accent'"
                  :style="{ width: `${Math.min(100, gripUsage)}%` }"
                ></div>
              </div>
            </div>
          </div>

          <div
            class="border-tcu-border bg-tcu-bg-1 relative flex flex-col items-center justify-center overflow-hidden rounded-lg border p-3"
          >
            <div
              class="text-tcu-txt-dim absolute top-2 left-3 text-[10px] tracking-widest uppercase"
            >
              G-Force
            </div>

            <div
              class="border-tcu-border bg-tcu-bg-2 relative mt-3 h-20 w-20 shrink-0 rounded-full border-2"
            >
              <div class="bg-tcu-border absolute top-1/2 left-0 h-px w-full"></div>
              <div class="bg-tcu-border absolute top-0 left-1/2 h-full w-px"></div>
              <div
                class="border-tcu-border/50 absolute top-[25%] left-[25%] h-1/2 w-1/2 rounded-full border"
              ></div>

              <div
                class="absolute h-3 w-3 -translate-x-1/2 -translate-y-1/2 transform rounded-full bg-yellow-400 shadow-[0_0_8px_rgba(250,204,21,0.8)] transition-all duration-75"
                :style="gDotStyle"
              ></div>
            </div>

            <div class="mt-2 flex w-full shrink-0 justify-between px-2">
              <div class="flex flex-col items-center">
                <span class="text-tcu-txt-dim text-[9px]">LAT</span>
                <span class="text-xs font-bold text-white tabular-nums">{{ gLat.toFixed(2) }}</span>
              </div>
              <div class="flex flex-col items-center">
                <span class="text-tcu-txt-dim text-[9px]">LON</span>
                <span class="text-xs font-bold text-white tabular-nums">{{ gLon.toFixed(2) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

        <aside class="flex min-h-0 flex-col gap-3 overflow-y-auto">
          <CarLearningPanel
            :telemetry="telemetry"
            :clear-status="clearStatus"
            @clear-learning="emit('clearLearning')"
          />

          <div class="grid shrink-0 grid-cols-1 gap-3">
            <div
              class="border-tcu-border bg-tcu-bg-1 flex flex-col justify-between rounded-lg border p-3"
            >
              <span class="text-tcu-txt-dim text-[10px] tracking-widest uppercase">Power</span>
              <div class="text-2xl font-bold text-white tabular-nums">
                {{ powerKw }} <span class="text-tcu-txt-dim text-xs">kW</span>
              </div>
            </div>
            <div
              class="border-tcu-border bg-tcu-bg-1 flex flex-col justify-between rounded-lg border p-3"
            >
              <span class="text-tcu-txt-dim text-[10px] tracking-widest uppercase">Torque</span>
              <div class="text-2xl font-bold text-white tabular-nums">
                {{ torqueNm }} <span class="text-tcu-txt-dim text-xs">Nm</span>
              </div>
            </div>
            <div
              class="border-tcu-border bg-tcu-bg-1 relative flex flex-col justify-between overflow-hidden rounded-lg border p-3"
            >
              <span class="text-tcu-txt-dim z-10 text-[10px] tracking-widest uppercase">Turbo</span>
              <div class="text-accent-2 z-10 text-2xl font-bold tabular-nums">
                {{ turboBar }} <span class="text-tcu-txt-dim text-xs">Bar</span>
              </div>
              <div
                class="bg-accent-2/20 absolute bottom-0 left-0 w-full transition-all duration-75"
                :style="{ height: `${((t.turbo_bar ?? 0) / 2.0) * 100}%` }"
              ></div>
            </div>
          </div>

          <div class="border-tcu-border bg-tcu-bg-1 rounded-lg border p-4">
            <div class="text-tcu-txt-dim text-[10px] tracking-widest uppercase">Shift Target</div>
            <div class="text-accent-2 mt-2 text-sm font-bold">
              {{ optimalShiftText || 'No target yet' }}
            </div>
          </div>
        </aside>
      </div>
    </div>

    <div
      v-else
      class="border-tcu-border bg-tcu-bg-1 text-tcu-txt-muted flex h-full flex-col items-center justify-center rounded-lg border border-dashed"
    >
      <div class="text-tcu-border mb-3 text-4xl font-black tracking-widest">OFFLINE</div>
      <div class="text-tcu-txt-dim flex items-center gap-2 text-sm tracking-widest uppercase">
        <div class="bg-danger h-2 w-2 animate-pulse rounded-full"></div>
        Awaiting UDP Telemetry...
      </div>
    </div>
  </div>
</template>

<style scoped>
  .tabular-nums {
    font-variant-numeric: tabular-nums;
  }
</style>
