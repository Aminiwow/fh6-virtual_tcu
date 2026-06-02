<script setup lang="ts">
  import { NButton, NCard, NFlex, NGrid, NGridItem, NInput, NSlider, NSwitch, NText } from 'naive-ui'
  import { inject } from 'vue'
  import { settingsContextKey } from './context'

  const ctx = inject(settingsContextKey)!
  const {
    t,
    store,
    hotkeyFields,
    shiftKeyFields,
    clutchFields,
    clutchTimingSliders,
    advancedSliders,
    networkDraftHost,
    networkDraftWebPort,
    networkDraftUdpPort,
    networkDirty,
    networkApplyError,
    networkApplyOk,
    networkApplying,
    applyNetworkSettings,
    onExportProfile,
    onOpenImport,
    restartBackend,
    configNumber,
    configBool,
    configText,
    sliderUnit,
  } = ctx

  function applyAndRestart() {
    const host = networkDraftHost.value
    const webPort = networkDraftWebPort.value
    const udpPort = networkDraftUdpPort.value
    if (host) store.setConfig('web_host', host)
    if (webPort) store.setConfig('web_port', Number(webPort))
    if (udpPort) store.setConfig('udp_port', Number(udpPort))
    setTimeout(() => restartBackend(), 600)
  }
</script>

<template>
  <NFlex vertical :size="16">
    <NCard :title="t('extras.networkTitle')" size="small" :bordered="false">
      <NText depth="3" style="font-size: 12px; display: block; margin-bottom: 8px">
        {{ t('extras.networkHint') }}
      </NText>
      <NGrid :cols="2" :x-gap="16" :y-gap="10">
        <NGridItem>
          <NFlex justify="space-between" align="center" :size="8">
            <NText>{{ t('extras.webHost') }}</NText>
            <NInput
              v-model:value="networkDraftHost"
              placeholder="0.0.0.0"
              maxlength="15"
              size="small"
              style="width: 120px; font-family: ui-monospace, monospace"
            />
          </NFlex>
        </NGridItem>
        <NGridItem>
          <NFlex justify="space-between" align="center" :size="8">
            <NText>{{ t('extras.webPort') }}</NText>
            <NInput
              v-model:value="networkDraftWebPort"
              placeholder="8765"
              maxlength="5"
              size="small"
              style="width: 120px; font-family: ui-monospace, monospace"
            />
          </NFlex>
        </NGridItem>
        <NGridItem>
          <NFlex justify="space-between" align="center" :size="8">
            <NText>{{ t('extras.udpPort') }}</NText>
            <NInput
              v-model:value="networkDraftUdpPort"
              placeholder="5555"
              maxlength="5"
              size="small"
              style="width: 120px; font-family: ui-monospace, monospace"
            />
          </NFlex>
        </NGridItem>
      </NGrid>
      <NText depth="3" style="font-size: 11px; display: block; margin-top: 8px">
        {{ t('extras.udpPortHint') }}
      </NText>
      <NFlex :size="8" align="center" style="margin-top: 12px">
        <NButton
          type="primary"
          size="small"
          :disabled="!networkDirty || networkApplying"
          :loading="networkApplying"
          @click="applyNetworkSettings"
        >
          {{ t('extras.networkApply') }}
        </NButton>
        <NText v-if="networkApplyOk" depth="3" style="font-size: 12px; color: #16a34a">
          {{ t('extras.networkApplyOk') }}
        </NText>
        <NText v-else-if="networkApplyError" depth="3" style="font-size: 12px; color: #dc2626">
          {{ t(`extras.networkErrors.${networkApplyError}`) }}
        </NText>
        <NButton
          v-if="networkDirty"
          type="warning"
          size="small"
          style="margin-left: 8px"
          @click="applyAndRestart()"
        >
          {{ t('extras.saveAndRestart') }}
        </NButton>
      </NFlex>
    </NCard>

    <NCard :title="t('extras.profileTitle')" size="small" :bordered="false">
      <NText depth="3" style="font-size: 12px; display: block; margin-bottom: 12px">
        {{ t('extras.profileHint') }}
      </NText>
      <NFlex :size="8">
        <NButton @click="onExportProfile">
          {{ t('extras.export') }}
        </NButton>
        <NButton @click="onOpenImport">
          {{ t('extras.import') }}
        </NButton>
      </NFlex>
    </NCard>

    <NCard :title="t('extras.outputMode')" size="small" :bordered="false">
      <NText depth="3" style="font-size: 12px; display: block; margin-bottom: 8px">
        {{ t('extras.outputModeHint') }}
      </NText>
      <NText code style="font-family: ui-monospace, monospace">
        {{ t('extras.outputModeKeyboard') }}
      </NText>
      <NFlex :size="8" align="center" style="margin-top: 10px">
        <NButton type="warning" size="small" @click="restartBackend()">
          {{ t('extras.saveAndRestart') }}
        </NButton>
        <NText depth="3" style="font-size: 11px; color: #d97706">
          {{ t('extras.outputModeRestart') }}
        </NText>
      </NFlex>
    </NCard>

    <NCard :title="t('extras.shiftKeys')" size="small" :bordered="false">
      <NText depth="3" style="font-size: 12px; display: block; margin-bottom: 8px">
        {{ t('extras.shiftKeyHint') }}
      </NText>
      <NGrid :cols="2" :x-gap="16" :y-gap="10">
        <NGridItem v-for="h1 in shiftKeyFields" :key="h1.key">
          <NFlex justify="space-between" align="center" :size="8">
            <NText>{{ t(`extras.${h1.i18nKey}`) }}</NText>
            <NInput
              :value="configText(h1.key)"
              :placeholder="h1.placeholder"
              size="small"
              style="width: 100px; font-family: ui-monospace, monospace"
              @update:value="(v) => store.setConfig(h1.key, v.trim().toLowerCase())"
            />
          </NFlex>
        </NGridItem>
      </NGrid>
    </NCard>

    <NCard :title="t('extras.clutchAssist')" size="small" :bordered="false">
      <NFlex justify="space-between" align="center" style="margin-bottom: 10px">
        <NText depth="3" style="font-size: 12px">
          {{ t('extras.clutchAssistHint') }}
        </NText>
        <NSwitch
          :value="configBool('feat_clutch_assist')"
          @update:value="(v) => store.setConfig('feat_clutch_assist', v)"
        />
      </NFlex>
      <NGrid :cols="2" :x-gap="16" :y-gap="10">
        <NGridItem v-for="field in clutchFields" :key="field.key">
          <NFlex justify="space-between" align="center" :size="8">
            <NText>{{ t(`extras.${field.i18nKey}`) }}</NText>
            <NInput
              :value="configText(field.key)"
              :placeholder="field.placeholder"
              size="small"
              style="width: 100px; font-family: ui-monospace, monospace"
              @update:value="(v) => store.setConfig(field.key, v.trim().toLowerCase())"
            />
          </NFlex>
        </NGridItem>
      </NGrid>
      <NFlex vertical :size="14" style="margin-top: 14px">
        <div v-for="s in clutchTimingSliders" :key="s.key">
          <NFlex justify="space-between" align="center" style="margin-bottom: 4px">
            <NText>{{ t(`extras.${s.i18nKey}`) }}</NText>
            <NText code style="font-family: ui-monospace, monospace">
              {{ configNumber(s.key) }}{{ sliderUnit(s) }}
            </NText>
          </NFlex>
          <NSlider
            :value="configNumber(s.key)"
            :min="s.min"
            :max="s.max"
            :step="s.step ?? 1"
            @update:value="(v) => store.setConfig(s.key, v)"
          />
        </div>
      </NFlex>
    </NCard>

    <NCard :title="t('extras.hotkeys')" size="small" :bordered="false">
      <NText depth="3" style="font-size: 12px; display: block; margin-bottom: 8px">
        {{ t('extras.hotkeyHint') }}
      </NText>
      <NGrid :cols="2" :x-gap="16" :y-gap="10">
        <NGridItem v-for="h1 in hotkeyFields" :key="h1.key">
          <NFlex justify="space-between" align="center" :size="8">
            <NText>{{ t(`extras.${h1.i18nKey}`) }}</NText>
            <NInput
              :value="configText(h1.key)"
              :placeholder="h1.placeholder"
              size="small"
              style="width: 100px; font-family: ui-monospace, monospace"
              @update:value="(v) => store.setConfig(h1.key, v.trim().toLowerCase())"
            />
          </NFlex>
        </NGridItem>
      </NGrid>
    </NCard>

    <NCard :title="t('extras.fullTuning')" size="small" :bordered="false">
      <NFlex vertical :size="14">
        <div v-for="s in advancedSliders" :key="s.key">
          <NFlex justify="space-between" align="center" style="margin-bottom: 4px">
            <NText>{{ t(`settings.${s.i18nKey}`) }}</NText>
            <NText code style="font-family: ui-monospace, monospace">
              {{ configNumber(s.key) }}{{ sliderUnit(s) }}
            </NText>
          </NFlex>
          <NSlider
            :value="configNumber(s.key)"
            :min="s.min"
            :max="s.max"
            :step="s.step ?? 1"
            @update:value="(v) => store.setConfig(s.key, v)"
          />
        </div>
      </NFlex>
    </NCard>
  </NFlex>
</template>
