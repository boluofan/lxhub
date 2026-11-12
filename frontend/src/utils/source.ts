import type { ProbeDetail, ProbeRes, Source } from '@/types'

export const PLAT: Record<string, string> = {
  kw: '酷我',
  wy: '网易云',
  kg: '酷狗',
  mg: '咪咕',
  tx: 'QQ',
}

export const TYPE_TXT: Record<string, string> = {
  source: '音源脚本',
  companion: '配套脚本（不用于导入）',
  unknown: '存疑',
}

export const STATUS_TXT: Record<string, string> = {
  ok: '可用',
  dead: '失效',
  down: '后端挂',
  comp: '配套',
  unk: '存疑',
}

export type StatusKey = 'ok' | 'dead' | 'down' | 'comp' | 'unk'

export const TAG_TYPE: Record<StatusKey, 'success' | 'danger' | 'warning' | 'info' | 'primary'> = {
  ok: 'success',
  dead: 'danger',
  down: 'danger',
  comp: 'info',
  unk: 'warning',
}

export function statusKey(s: {
  is_alive?: boolean
  api_alive?: string
  script_type?: string
  probe_status?: string
}): StatusKey {
  if (!s.is_alive) return 'dead'
  // 沙箱实测为真相源：实测可用 / 降级说明脚本确实能取回真实音频，
  // 此时即使后端 ping（api_alive）判为「挂」也只是扒错的域名/出口差异，
  // 以实测结果为准，避免误标「后端挂」。
  if (s.probe_status === 'usable' || s.probe_status === 'degraded') return 'ok'
  if (s.api_alive === 'down') return 'down'
  if (s.script_type === 'companion') return 'comp'
  if (s.script_type === 'unknown') return 'unk'
  return 'ok'
}

/** 实测结果标签：usable / degraded / not-loadable / 未测（none） */
export type ProbeKey = 'usable' | 'degraded' | 'not-loadable' | 'none'

export const PROBE_TXT: Record<string, string> = {
  usable: '实测可用',
  degraded: '实测降级',
  'not-loadable': '实测失败',
  none: '未实测',
}

/** 沙箱实测的排序/展示优先级：可用 > 降级 > 未测 > 失败 */
export function probeKey(
  s: Pick<Source, 'probe_status' | 'is_alive'> | null,
): ProbeKey {
  const v = s?.probe_status
  if (v === 'usable') return 'usable'
  if (v === 'degraded') return 'degraded'
  if (v === 'not-loadable') return 'not-loadable'
  return 'none'
}

export const PROBE_TAG: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  usable: 'success',
  degraded: 'warning',
  'not-loadable': 'danger',
  none: 'info',
}

export const PROBE_RANK: Record<ProbeKey, number> = {
  usable: 0,
  degraded: 1,
  none: 2,
  'not-loadable': 3,
}

/** 音频规格摘要：worker 产出的 spec 对象 → 人类可读（容器/码率/采样率/位深） */
export function fmtSpec(spec: NonNullable<ProbeRes['spec']>): string {
  const bits: string[] = []
  if (spec.container) bits.push(String(spec.container).toUpperCase())
  if (spec.bitrate) bits.push(`${spec.bitrate}kbps`)
  if (spec.sampleRate) bits.push(`${Math.round(spec.sampleRate / 1000)}kHz`)
  if (spec.bitDepth) bits.push(`${spec.bitDepth}bit`)
  if (spec.channels) bits.push(`${spec.channels}ch`)
  if (spec.avgBitrateKbps) bits.push(`均值 ${spec.avgBitrateKbps}kbps`)
  if (spec.specUnverified) bits.push('规格存疑')
  return bits.join(' / ')
}

/** 实测 tooltip 明细：逐平台一行；失败展示原因；含 L2 音质标注 */
export function probeTip(s: Source): string {
  const d = s.probe_detail as ProbeDetail | undefined
  if (probeKey(s) === 'none' || !d) return '尚未进行沙箱实测'
  const bits: string[] = []
  if (s.probe_checked && typeof s.probe_checked === 'string') {
    bits.push(`实测时间 ${s.probe_checked.replace('T', ' ')}`)
  }
  if (d.updateAlert?.log) bits.push(`⚠ ${d.updateAlert.log}`)
  const plats = d.declared || Object.keys(d.platforms || {})
  for (const k of plats) {
    const r: ProbeRes | undefined = d.platforms?.[k]
    const name = PLAT[k] || k
    if (!r || r.status === 'skipped') {
      bits.push(`${name}：未测`)
    } else if (r.status === 'usable') {
      const q = r.quality && r.quality !== 'unknown' ? ` · 音质${qLabel(r.quality)}` : ''
      const spec = r.spec ? ` · ${fmtSpec(r.spec)}` : ''
      bits.push(`${name}：✓ ${r.message || '真实音频'}${spec}${q}`)
    } else {
      bits.push(`${name}：✗ ${r.reason || r.message || '失败'}`)
    }
  }
  if (d.loadError) bits.push(`脚本: ${d.loadError}`)
  return bits.join('\n')
}

function qLabel(q: string): string {
  return { matched: '达标', degraded: '降级', mismatch: '异常', refused: '拒绝核验' }[q] || q
}

/** 置信度分项标签（与后端 confidence.py 的 parts 键一一对应） */
const CONF_LABELS: Record<string, string> = {
  base: '实测可用基础分',
  platforms: '成功平台数',
  coverage: '平台覆盖率',
  quality: '音质核验',
  spec: '真实音频规格',
  freshness: '实测新鲜度',
  penalty: '更新提醒扣减',
}

const QUALITY_TIP: Record<string, string> = {
  matched: '达标', degraded: '降级', mismatch: '异常', refused: '拒绝核验',
  unknown: '未判定', unverified: '未核验（未开 L2 音质核验）',
}

const SPEC_TIP: Record<string, string> = {
  high: '检出无损 / 高码率', verified: '仅核验到普通规格', none: '无规格数据',
}

/** 列表「置信度」列文本：只有实测可用且有分数时才显示数字 */
export function confidenceText(s: Source): string {
  return s.probe_status === 'usable' && s.confidence ? String(s.confidence) : '—'
}

export function confidenceTag(s: Source): 'success' | 'primary' | 'info' {
  if (s.probe_status !== 'usable' || !s.confidence) return 'info'
  if (s.confidence >= 80) return 'success'
  if (s.confidence >= 55) return 'primary'
  return 'info'
}

/** 置信度 tooltip：分数 + 评分依据，便于理解为什么排在前面 */
export function confidenceTip(s: Source): string {
  const d = s.confidence_detail
  if (s.probe_status !== 'usable' || !s.confidence || !d?.parts) {
    return '仅对「实测可用」的音源评分（0–100）：按成功平台数、覆盖率、音质核验结论、真实音频规格、实测新鲜度等加权；\n未实测 / 实测降级 / 实测失败均为 0 分。'
  }
  const bits = [`置信度 ${s.confidence} / 100 · 分数越高排序越靠前`]
  if (typeof d.success === 'number' && typeof d.tested === 'number') {
    bits.push(`实测成功平台 ${d.success} / ${d.tested}`)
  }
  if (d.quality_verdict) {
    bits.push(`最佳平台音质：${QUALITY_TIP[d.quality_verdict] || d.quality_verdict}`)
  }
  if (d.spec_level) bits.push(`音频规格：${SPEC_TIP[d.spec_level] || d.spec_level}`)
  bits.push('—— 评分构成 ——')
  for (const [k, v] of Object.entries(d.parts as Record<string, number>)) {
    if (typeof v !== 'number') continue
    bits.push(`${CONF_LABELS[k] || k} ${v >= 0 ? '+' : ''}${v}`)
  }
  if (d.reason === 'detail-missing') bits.push('（缺少逐平台明细，仅计基础分）')
  return bits.join('\n')
}

export function parseHosts(s: { api_hosts?: string | string[] }): string[] {
  const h = s?.api_hosts
  if (Array.isArray(h)) return h
  if (typeof h === 'string' && h) {
    try {
      const v = JSON.parse(h)
      return Array.isArray(v) ? v : []
    } catch {
      return []
    }
  }
  return []
}

export function baseName(u: string): string {
  try {
    return decodeURIComponent(u.split('?')[0].split('/').pop() || '')
  } catch {
    return u
  }
}