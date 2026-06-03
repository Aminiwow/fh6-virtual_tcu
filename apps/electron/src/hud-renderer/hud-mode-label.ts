import type { DriveMode } from '@virtual-tcu/shared/types/ws'

const MODE_I18N: Record<DriveMode, string> = {
  RACE: 'modes.race.name',
  DRIFT: 'modes.drift.name',
  OFFROAD: 'modes.offroad.name',
  LEARN: 'modes.learn.name',
  MANUAL: 'modes.manual.name',
}

export function hudModeI18nKey(mode: string): string {
  if (mode in MODE_I18N) return MODE_I18N[mode as DriveMode]
  return 'modes.race.name'
}
