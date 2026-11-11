import { http } from '@/utils/http'
import type { LocalExportStatus } from '@/types'

export interface LocalExportResult {
  ok: boolean
  written: number
  dir: string
  filenames: string[]
  pruned: string[]
  shortfall: number
  filled: number
  generated_at: string
  trigger: string
}

/** 状态 + 已导出的固定文件清单（只读，无需登录） */
export function fetchLocalExportStatus(): Promise<LocalExportStatus> {
  return http.get<LocalExportStatus>('/api/export/local', { silent: true })
}

/** 立即导出一轮；后端不要求 enabled，方便先验证再开自动 */
export function runLocalExport(): Promise<LocalExportResult> {
  return http.post<LocalExportResult>('/api/export/local', { data: {} })
}

/** 固定文件名的对外访问地址 */
export function localFileUrl(filename: string): string {
  return `/local/${encodeURIComponent(filename)}`
}
