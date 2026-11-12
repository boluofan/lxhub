import { onBeforeUnmount, reactive } from 'vue'
import { fetchProgress } from '@/api/status'
import { message } from '@/utils/message'
import { fmtElapsed } from '@/utils/format'
import type { ProgressSnapshot } from '@/types'

export interface ProgressView {
  running: boolean
  title: string
  msg: string
  elapsed: string
  pct: number
  indet: boolean
  statsText: string
  logs: string
}

/** 轮询任务进度，任务结束后回调 `onDone` 并弹出汇总提示 */
export function useProgress(onDone: () => void) {
  const view = reactive<ProgressView>({
    running: false,
    title: '',
    msg: '',
    elapsed: '',
    pct: 0,
    indet: false,
    statsText: '',
    logs: '',
  })
  let timer: number | null = null

  function render(p: ProgressSnapshot, done: boolean): void {
    const s = p.stats || {}
    view.running = !done
    view.title = done
      ? p.cancelled
        ? '已取消'
        : '已完成'
      : p.kind === 'crawl'
      ? '正在爬取'
      : p.kind === 'probe'
        ? '正在沙箱实测'
        : '正在检测'
    view.msg = (p.skipped_reason && p.kind === 'probe' ? p.skipped_reason : p.message) || ''
    view.elapsed = `已用时 ${fmtElapsed(p.elapsed || 0)}`
    const total = s.urls_total || 0
    const doneCount = s.urls_done || 0
    view.indet = !(done || total > 0)
    view.pct = done ? 100 : total > 0 ? Math.round((doneCount / total) * 1000) / 10 : 100
    const bits: string[] = []
    if (s.repos_total) bits.push(`仓库 ${s.repos_done || 0}/${s.repos_total}`)
    if (total) bits.push(`链接 ${doneCount}/${total}`)
    if (p.kind === 'crawl') {
      if (s.valid != null) bits.push(`疑似音源 ${s.valid}`)
      if (s.new != null) bits.push(`新增 ${s.new}`)
    } else if (p.kind === 'probe') {
      if (s.usable != null) bits.push(`实测可用 ${s.usable}`)
      if (s.degraded) bits.push(`降级 ${s.degraded}`)
      if (s.not_loadable) bits.push(`失败 ${s.not_loadable}`)
    } else if (s.alive != null) {
      bits.push(`存活 ${s.alive}`)
    }
    if (s.dead) bits.push(`失效 ${s.dead}`)
    view.statsText = bits.join(' · ') || '正在收集候选链接…'
    view.logs = (p.logs || []).join('\n')
  }

  function stop(): void {
    if (timer) {
      window.clearInterval(timer)
      timer = null
    }
  }

  async function tick(): Promise<void> {
    let p: ProgressSnapshot
    try {
      p = await fetchProgress()
    } catch {
      return
    }
    if (!p.running) {
      stop()
      render(p, true)
      onDone()
      const s = p.stats || {}
      if (p.skipped_reason && p.kind === 'probe') {
        message(`未触发实测：${p.skipped_reason}`, { type: 'warning' })
        return
      }
      message(
        p.cancelled
          ? '任务已取消'
          : p.kind === 'crawl'
            ? `爬取完成：检测 ${s.checked ?? '—'} 条，疑似音源 ${s.valid ?? '—'} 条`
            : p.kind === 'probe'
              ? `实测完成：可用 ${s.usable ?? '—'} / 降级 ${s.degraded ?? '—'} / 失败 ${s.not_loadable ?? '—'}`
              : `检测完成：存活 ${s.alive ?? '—'} / 失效 ${s.dead ?? '—'}`,
        { type: 'success' },
      )
      window.setTimeout(() => {
        view.running = false
      }, 6000)
      return
    }
    render(p, false)
  }

  async function start(): Promise<void> {
    if (timer) return
    await tick()
    if (timer) return
    timer = window.setInterval(() => void tick(), 1000)
  }

  onBeforeUnmount(stop)

  return { view, start, stop }
}
