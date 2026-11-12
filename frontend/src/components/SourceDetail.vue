<script setup lang="ts">
import { computed } from 'vue'
import {
  baseName,
  confidenceTag,
  confidenceText,
  confidenceTip,
  fmtSpec,
  parseHosts,
  PLAT,
  PROBE_TAG,
  PROBE_TXT,
  probeKey,
  STATUS_TXT,
  TAG_TYPE,
  statusKey,
  TYPE_TXT,
} from '@/utils/source'
import { fmtFull, fmtSize } from '@/utils/format'
import { withPrefix } from '@/utils/prefix'
import { copyText } from '@/utils/clipboard'
import type { ProbeDetail, ProbeRes, Source } from '@/types'

const props = defineProps<{ source: Source | null; no?: number | null }>()
const visible = defineModel<boolean>({ default: false })

const shownUrl = computed(() => withPrefix(props.source?.url || ''))

const preText = computed(() => {
  const full = props.source?.content_text || ''
  if (!full) return '（无内容）'
  return full.slice(0, 6000) + (full.length > 6000 ? '\n\n…(已截断，点「下载」查看完整脚本)' : '')
})

const subText = computed(() => {
  const s = props.source
  if (!s) return ''
  const bits: string[] = []
  if (s.version) bits.push('v' + s.version)
  if (s.author) bits.push(s.author)
  const plats = (s.platforms || []).map((p) => PLAT[p] || p)
  if (plats.length) bits.push(plats.join(' / '))
  return bits.join(' · ') || '无元数据'
})

const apiText = computed(() => {
  const s = props.source
  if (!s) return '—'
  const hosts = parseHosts(s)
  // 沙箱实测为真相源：实测可用/降级时，即使后端 ping 判「挂」也以实测为准
  if (s.probe_status === 'usable' || s.probe_status === 'degraded') {
    return s.api_alive === 'down'
      ? '疑似挂 · 但沙箱实测可用（以实测为准）'
      : `活 · ${hosts.length} 个域名可访问`
  }
  if (s.api_alive === 'up') return `活 · ${hosts.length} 个域名可访问`
  if (s.api_alive === 'down') return '挂 · 声明的后端域名均不可达'
  return hosts.length ? '未探测' : '脚本未声明后端域名'
})

const typeText = computed(() => {
  const s = props.source
  if (!s) return '—'
  const t = TYPE_TXT[s.script_type || ''] || '存疑'
  const tier =
    s.valid_tier === 'confirmed' ? ' · 高置信' : s.valid_tier === 'probable' ? ' · 待验证' : ''
  return t + tier
})

const mirrors = computed(() => props.source?.mirrors || [])
const variants = computed(() => props.source?.variants || [])

const probeDetail = computed(() => {
  const d = props.source?.probe_detail
  return d && typeof d === 'object' ? (d as ProbeDetail) : null
})
const probedAt = computed(() => {
  const v = props.source?.probe_checked
  return typeof v === 'string' ? fmtFull(v) : ''
})

/** 置信度评分依据（tooltip 与弹层共用一份文案） */
const confBits = computed<string[]>(() =>
  props.source ? confidenceTip(props.source).split('\n') : [],
)

interface ProbeLine {
  key: string
  name: string
  res: ProbeRes | null
  tag: 'success' | 'warning' | 'danger' | 'info'
  text: string
}

function lineText(r: ProbeRes): string {
  if (r.status === 'usable') {
    const bits = [r.message || '返回真实音频']
    if (r.spec) bits.push(fmtSpec(r.spec))
    if (r.quality && r.quality !== 'unknown') {
      bits.push(
        '音质' +
          ({ matched: '达标', degraded: '降级', mismatch: '异常', refused: '被拒绝核验' }[r.quality] ||
            r.quality),
      )
    }
    if (r.ms) bits.push(`耗时 ${r.ms}ms`)
    return bits.join(' · ')
  }
  return r.reason || r.message || '失败'
}

const probeLines = computed<ProbeLine[]>(() => {
  const d = probeDetail.value
  if (!d) return []
  const keys = d.declared?.length ? d.declared : Object.keys(d.platforms || {})
  return keys.map((k) => {
    const r = d.platforms?.[k] || null
    let tag: ProbeLine['tag'] = 'info'
    let text = '未测'
    if (r && r.status !== 'skipped') {
      if (r.status === 'usable') {
        tag = 'success'
        text = lineText(r)
      } else {
        tag = 'danger'
        text = lineText(r)
      }
    }
    return { key: k, name: PLAT[k] || k, res: r, tag, text }
  })
})
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="source ? source.name || baseName(source.url) : '—'"
    width="680px"
  >
    <div class="dlg-sub muted">{{ subText }}</div>

    <el-descriptions :column="2" border size="small" style="margin-top: 12px">
      <el-descriptions-item label="可用状态">
        <el-tag v-if="source" :type="TAG_TYPE[statusKey(source)]" size="small">
          {{ STATUS_TXT[statusKey(source)] }}
        </el-tag>
        <span class="muted">（HTTP {{ source?.http_status ?? '—' }}）</span>
      </el-descriptions-item>
      <el-descriptions-item label="后端">{{ apiText }}</el-descriptions-item>
      <el-descriptions-item label="类型">{{ typeText }}</el-descriptions-item>
      <el-descriptions-item label="编号">第 {{ no ?? source?.id }} 条</el-descriptions-item>
    </el-descriptions>

    <el-descriptions :column="1" border size="small" style="margin-top: 12px">
      <el-descriptions-item label="沙箱实测">
        <template v-if="probeDetail">
          <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
            <el-tag :type="PROBE_TAG[probeKey(source)]" size="small">
              {{ PROBE_TXT[probeKey(source)] }}
            </el-tag>
            <el-tooltip
              v-if="source?.confidence"
              placement="top"
              :content="confidenceTip(source)"
              popper-class="probe-tip-popper"
            >
              <el-tag :type="confidenceTag(source)" size="small" effect="plain">
                置信度 {{ confidenceText(source) }}
              </el-tag>
            </el-tooltip>
            <span class="muted">经 Node 沙箱真实取歌核验</span>
          </div>
          <div class="probe-res">
            <div v-for="l in probeLines" :key="l.key" class="probe-row">
              <span class="probe-plat">{{ l.name }}</span>
              <el-tag :type="l.tag" size="small" effect="plain">
                {{ l.res && l.res.status === 'usable' ? '通过' : l.res ? '失败' : '未测' }}
              </el-tag>
              <span class="muted probe-txt">{{ l.text }}</span>
            </div>
            <div v-if="probeDetail.updateAlert?.log" class="probe-row">
              <span class="probe-plat">提醒</span>
              <el-tag type="warning" size="small" effect="plain">更新</el-tag>
              <span class="muted probe-txt">{{ probeDetail.updateAlert.log }}</span>
            </div>
            <div v-if="probeDetail.loadError" class="probe-row">
              <span class="probe-plat">脚本</span>
              <span class="muted probe-txt">{{ probeDetail.loadError }}</span>
            </div>
          </div>
          <div v-if="probedAt" class="muted probe-at">实测时间 {{ probedAt }}</div>
          <div v-if="source?.confidence" class="conf-box">
            <div v-for="(l, i) in confBits" :key="i" class="muted">{{ l }}</div>
          </div>
        </template>
        <span v-else class="muted">未实测（可到后台「配置管理 → 沙箱实测」开启）</span>
      </el-descriptions-item>
    </el-descriptions>

    <el-descriptions :column="1" border size="small" style="margin-top: 12px">
      <el-descriptions-item label="链接">
        <div class="url-box">
          <el-input :model-value="shownUrl" readonly size="small" />
          <el-button size="small" @click="copyText(shownUrl)">复制</el-button>
        </div>
      </el-descriptions-item>
      <el-descriptions-item label="镜像">
        <template v-if="mirrors.length">
          <div v-for="u in mirrors" :key="u" class="break muted">{{ u }}</div>
        </template>
        <span v-else>—</span>
      </el-descriptions-item>
      <el-descriptions-item label="同名变体">
        <template v-if="variants.length">
          <div v-for="(v, i) in variants" :key="i" class="break muted">
            {{ v.origin_repo || v.url }}{{ v.version ? ' · v' + v.version : '' }}{{ v.is_alive ? '' : ' · 失效' }}
          </div>
        </template>
        <span v-else>—</span>
      </el-descriptions-item>
      <el-descriptions-item label="发现自">
        <a
          v-if="source?.origin_url"
          :href="source.origin_url"
          target="_blank"
          rel="noopener"
        >{{ source.origin_repo || '来源' }}</a>
        <span v-else class="muted">外部直链</span>
      </el-descriptions-item>
      <el-descriptions-item label="大小">{{ fmtSize(source?.file_size) }}</el-descriptions-item>
      <el-descriptions-item label="发现时间">{{ fmtFull(source?.first_seen || '') }}</el-descriptions-item>
      <el-descriptions-item label="最近检测">{{ fmtFull(source?.last_checked || '') }}</el-descriptions-item>
      <el-descriptions-item label="SHA256">
        <span class="mono muted">{{ source?.sha256 || '—' }}</span>
      </el-descriptions-item>
    </el-descriptions>

    <div class="pre-hd">脚本头部内容（截取）</div>
    <pre class="script-pre">{{ preText }}</pre>

    <template #footer>
      <el-button type="primary" @click="source && copyText(shownUrl)">复制链接</el-button>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.probe-res {
  margin-top: 6px;
  display: grid;
  gap: 4px;
}
.probe-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.probe-plat {
  min-width: 52px;
}
.probe-txt {
  flex: 1;
  word-break: break-all;
}
.probe-at {
  margin-top: 6px;
  font-size: 12px;
}
.conf-box {
  margin-top: 8px;
  padding: 8px 10px;
  border-left: 2px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-lighter);
  border-radius: 2px;
  display: grid;
  gap: 2px;
  font-size: 12px;
  line-height: 1.5;
}
</style>
