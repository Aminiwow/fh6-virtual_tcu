<script setup lang="ts">
  import TcuConfigProvider from '@virtual-tcu/ui/components/TcuConfigProvider.vue'
  import AppFooter from './components/AppFooter.vue'
  import AppHeader from './components/AppHeader.vue'
  import DashboardPanel from './components/DashboardPanel.vue'
  import ModeSidebar from './components/ModeSidebar.vue'
  import StatsHistoryPanel from './components/StatsHistoryPanel.vue'
  import { useTcuViewStore } from './composables/useTcuViewStore'

  const {
    mode,
    connected,
    live,
    shiftCount,
    packetsTotal,
    telemetry,
    logStatus,
    shiftHistory,
    sessionStats,
    learningClearStatus,
    clearCurrentCarLearning,
  } = useTcuViewStore()
</script>

<template>
  <TcuConfigProvider dark>
    <AppHeader :mode="mode" :connected="connected" :live="live" />
    <main
      class="bg-tcu-border grid min-h-0 grid-cols-[220px_minmax(0,1fr)_300px] gap-px max-[1100px]:grid-cols-1"
    >
      <ModeSidebar
        :mode="mode"
        :shift-count="shiftCount"
        :packets-total="packetsTotal"
        :telemetry="telemetry"
        :log-status="logStatus"
        :interactive="false"
      />
      <DashboardPanel
        :live="live"
        :telemetry="telemetry"
        :clear-status="learningClearStatus"
        @clear-learning="clearCurrentCarLearning"
      />
      <StatsHistoryPanel
        :telemetry="telemetry"
        :session-stats="sessionStats"
        :shift-history="shiftHistory"
      />
    </main>
    <AppFooter />
  </TcuConfigProvider>
</template>
