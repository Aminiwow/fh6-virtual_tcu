<script setup lang="ts">
  import { computed } from 'vue'
  import { useI18n } from 'vue-i18n'
  import { hudModeI18nKey } from './hud-mode-label'

  const props = withDefaults(
    defineProps<{
      mode: string
      modeColor: string
      tcuState: string
      clickThrough: boolean
      compact?: boolean
      footer?: boolean
    }>(),
    { compact: false, footer: false },
  )

  const emit = defineEmits<{
    toggleClickThrough: [e: MouseEvent]
    close: []
  }>()

  const { t } = useI18n()
  const modeLabel = computed(() => t(hudModeI18nKey(props.mode)))
</script>

<template>
  <div class="hud-chrome" :class="{ compact, footer, interactive: clickThrough }">
    <div class="meta">
      <span
        class="mode-dot"
        :style="{ background: modeColor, boxShadow: `0 0 10px ${modeColor}88` }"
      />
      <span class="mode-name" :style="{ color: modeColor }">{{ modeLabel }}</span>
      <template v-if="!compact">
        <span class="sep" aria-hidden="true">·</span>
        <span class="state-name" :class="{ shifting: tcuState === 'SHIFTING' }">{{
          tcuState
        }}</span>
      </template>
    </div>

    <div class="actions interactive">
      <button
        type="button"
        class="btn"
        :class="{ active: clickThrough }"
        :title="clickThrough ? t('electronApp.hudUnlock') : t('electronApp.hudLock')"
        @click="emit('toggleClickThrough', $event)"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" stroke-linecap="round" />
        </svg>
      </button>
      <button type="button" class="btn" :title="t('electronApp.hudClose')" @click="emit('close')">
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <path d="M18 6L6 18M6 6l12 12" stroke-linecap="round" />
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
  .hud-chrome {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 22px;
    box-sizing: border-box;
    font-size: 9px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--hud-chrome-fg, #94a3b8);
  }

  .hud-chrome:not(.footer) {
    width: 100%;
  }

  .hud-chrome.compact:not(.footer) {
    min-height: 20px;
    margin-bottom: 4px;
  }

  .hud-chrome.footer {
    width: 100%;
    min-height: 28px;
    padding: 0;
  }

  .meta {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .mode-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .mode-name {
    font-weight: 800;
    font-size: 10px;
    color: var(--hud-chrome-mode-fg, #e2e8f0);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 140px;
  }

  .sep {
    opacity: 0.45;
  }

  .state-name {
    color: var(--hud-chrome-state-fg, #64748b);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .state-name.shifting {
    color: #c4b5fd;
  }

  .actions {
    display: flex;
    gap: 3px;
    flex-shrink: 0;
  }

  .btn {
    -webkit-app-region: no-drag;
    background: var(--hud-chrome-btn-bg, rgba(255, 255, 255, 0.06));
    border: 1px solid var(--hud-chrome-btn-border, rgba(255, 255, 255, 0.1));
    color: var(--hud-chrome-btn-fg, #94a3b8);
    border-radius: 6px;
    width: 22px;
    height: 22px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    opacity: 1;
    transition:
      background 150ms,
      border-color 150ms,
      color 150ms;
  }

  .btn:hover {
    background: var(--hud-chrome-btn-hover-bg, rgba(255, 255, 255, 0.12));
    color: var(--hud-chrome-btn-hover-fg, #e2e8f0);
  }

  .btn.active {
    background: var(--hud-chrome-btn-active-bg, rgba(255, 255, 255, 0.18));
    border-color: var(--hud-chrome-btn-active-border, rgba(255, 255, 255, 0.28));
    color: var(--hud-chrome-btn-active-fg, #ffffff);
  }
</style>
