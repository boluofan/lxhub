import { computed, ref } from 'vue'
import { fetchSourceDetail, fetchSources } from '@/api/sources'
import { PROBE_RANK, probeKey, statusKey } from '@/utils/source'
import { setupPrefixes } from '@/utils/prefix'
import type { Source } from '@/types'

export interface SortState {
  key: string
  dir: number
}

const SORT_KEY = 'lxh.sort'

// 沙箱实测为真相源：实测可用/降级时，后端 ping 判「挂」不降权；
// 仅当未实测或实测失败时，ping 挂才作为排序的反向权重。
function pingBlocked(s: Source): boolean {
  return (
    s.api_alive === 'down' &&
    s.probe_status !== 'usable' &&
    s.probe_status !== 'degraded'
  )
}

function loadSort(): SortState {
  try {
    const s = JSON.parse(localStorage.getItem(SORT_KEY) || 'null')
    if (s && typeof s.key === 'string') return s as SortState
  } catch {
    return { key: '', dir: 1 }
  }
  return { key: '', dir: 1 }
}

const SORT_GET: Record<string, (s: Source) => string | number> = {
  status: (s) => ['ok', 'down', 'unk', 'comp', 'dead'].indexOf(statusKey(s)),
  name: (s) => (s.name || '').toLowerCase(),
  plat: (s) => (s.platforms || []).join(','),
  ver: (s) => s.version || '',
  auth: (s) => (s.author || '').toLowerCase(),
  upd: (s) => s.repo_pushed_at || s.content_updated_at || s.first_seen || '',
  probe: (s) => PROBE_RANK[probeKey(s)],
  confidence: (s) => s.confidence || 0,
}

/** 音源列表：加载、筛选、排序，前台与后台「音源管理」共用 */
export function useSources() {
  const all = ref<Source[]>([])
  const loading = ref(false)
  const sort = ref<SortState>(loadSort())
  const q = ref('')
  const statusFilter = ref('all')
  const typeFilter = ref('src')

  async function load(): Promise<void> {
    loading.value = true
    try {
      const data = await fetchSources()
      all.value = data.sources || []
      setupPrefixes(data.mirror_prefixes)
    } finally {
      loading.value = false
    }
  }

  function sortList(list: Source[]): Source[] {
    const k = sort.value.key
    const dir = sort.value.dir
    const t = (s: Source) => s.repo_pushed_at || s.content_updated_at || s.first_seen || ''
    if (!k) {
      // 与后端 list_sources() 的 ORDER BY 对齐：存活/有效是硬门槛，之后
      // 实测置信度高的整体靠前，再比静态分级 / 实测状态 / 更新时间。
      return [...list].sort(
        (a, b) =>
          Number(pingBlocked(a)) - Number(pingBlocked(b)) ||
          Number(!!b.is_alive) - Number(!!a.is_alive) ||
          Number(!!b.looks_valid) - Number(!!a.looks_valid) ||
          Number(b.confidence || 0) - Number(a.confidence || 0) ||
          Number(b.valid_tier === 'confirmed') - Number(a.valid_tier === 'confirmed') ||
          PROBE_RANK[probeKey(a)] - PROBE_RANK[probeKey(b)] ||
          (t(b) < t(a) ? -1 : t(b) > t(a) ? 1 : 0),
      )
    }
    const get = SORT_GET[k] || (() => '')
    return [...list].sort((a, b) => {
      const x = get(a)
      const y = get(b)
      let c = 0
      if (k === 'ver') c = String(x).localeCompare(String(y), undefined, { numeric: true })
      else if (k !== 'no') c = x < y ? -1 : x > y ? 1 : 0
      if (c === 0) c = Number(pingBlocked(a)) - Number(pingBlocked(b))
      return dir * c
    })
  }

  const rows = computed<Source[]>(() => {
    const st = statusFilter.value
    const tp = typeFilter.value
    const list = all.value.filter((s) => {
      if (st === 'alive' && !s.is_alive) return false
      if (st === 'dead' && s.is_alive) return false
      if (st === 'down' && s.api_alive !== 'down') return false
      if (st === 'probegood' && probeKey(s) !== 'usable') return false
      if (st === 'probebad' && !['degraded', 'not-loadable'].includes(probeKey(s)))
        return false
      if (tp === 'all') return true
      if (!s.looks_valid) return false
      if (tp === 'source') return s.script_type === 'source'
      if (tp === 'unk') return s.script_type === 'unknown'
      if (tp === 'src') return s.script_type !== 'companion'
      return true
    })
    const sorted = sortList(list)
    const query = q.value.trim()
    if (/^\d+$/.test(query)) {
      const n = parseInt(query, 10)
      return n >= 1 && n <= sorted.length ? [{ ...sorted[n - 1], _no: n }] : []
    }
    let result = sorted
    if (query) {
      const ql = query.toLowerCase()
      result = sorted.filter((s) => {
        const hay =
          (s.name || '') + ' ' + (s.author || '') + ' ' + (s.description || '') + ' ' + (s.url || '')
        return hay.toLowerCase().includes(ql)
      })
    }
    return result.map((s, i) => ({ ...s, _no: i + 1 }))
  })

  function onSortChange(prop: string, order: 'ascending' | 'descending' | null): void {
    if (!order) {
      sort.value = { key: '', dir: 1 }
    } else if (prop === 'no') {
      sort.value =
        sort.value.key === 'no' && sort.value.dir === 1
          ? { key: 'no', dir: -1 }
          : { key: 'no', dir: order === 'descending' ? -1 : 1 }
    } else {
      sort.value = { key: prop, dir: order === 'descending' ? -1 : 1 }
    }
    try {
      localStorage.setItem(SORT_KEY, JSON.stringify(sort.value))
    } catch {
      return
    }
  }

  async function openDetail(id: number): Promise<Source | null> {
    const s = all.value.find((x) => x.id === id)
    if (!s) return null
    try {
      const d = await fetchSourceDetail(id)
      d.mirrors = s.mirrors || []
      return d
    } catch {
      return null
    }
  }

  return {
    all,
    loading,
    sort,
    q,
    statusFilter,
    typeFilter,
    rows,
    load,
    onSortChange,
    openDetail,
  }
}
