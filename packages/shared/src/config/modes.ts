import type { DriveMode } from '../types/ws'

export interface ModeDef {
  id: DriveMode
  i18nKey: string
}

export const DRIVE_MODES: ModeDef[] = [
  { id: 'AUTO', i18nKey: 'auto' },
  { id: 'RACE', i18nKey: 'race' },
  { id: 'DRIFT', i18nKey: 'drift' },
  { id: 'OFFROAD', i18nKey: 'offroad' },
  { id: 'LEARN', i18nKey: 'learn' },
  { id: 'MANUAL', i18nKey: 'manual' },
]
