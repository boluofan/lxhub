<script setup lang="ts">
import { downloadUrl } from '@/api/sources'
import {
  baseName,
  confidenceTag,
  confidenceText,
  confidenceTip,
  PLAT,
  PROBE_TAG,
  PROBE_TXT,
  probeKey,
  probeTip,
  STATUS_TXT,
  TAG_TYPE,
  statusKey,
} from '@/utils/source'
import { fmtFull, fmtRel } from '@/utils/format'
import { withPrefix } from '@/utils/prefix'
import { copyText } from '@/utils/clipboard'
import type { Source } from '@/types'

defineProps<{ rows: Source[] }>()
const emit = defineEmits<{
  sort: [prop: string, order: 'ascending' | 'descending' | null]
  open: [id: number]
}>()

function onSortChange(e: { prop: string; order: 'ascending' | 'descending' | null }): void {
  emit('sort', e.prop, e.order)
}

function download(id: number): void {
  const a = document.createElement('a')
  a.href = downloadUrl(id)
  document.body.appendChild(a)
  a.click()
  a.remove()
}

function rel(s: Source): { t: string; full: string } {
  return fmtRel(s.repo_pushed_at || s.content_updated_at || s.last_alive || s.last_checked || '')
}

function tipBits(s: Source): string {
  const bits: string[] = []
  if (s.repo_pushed_at) bits.push(`GitHub 仓库更新 ${fmtFull(s.repo_pushed_at)}`)
  if (s.content_updated_at) bits.push(`脚本内容更新 ${fmtFull(s.content_updated_at)}`)
  if (s.last_checked) bits.push(`最近检测 ${fmtFull(s.last_checked)}`)
  return bits.join('\n')
}
</script>

<template>
  <el-card shadow="never" class="table-card">
    <el-table
      :data="rows"
      style="width: 100%"
      @sort-change="onSortChange"
      @row-click="(row: Source) => emit('open', row.id)"
    >
      <el-table-column prop="_no" label="#" width="64" sortable="custom" align="center" />
      <el-table-column prop="status" label="状态" width="100" sortable="custom">
        <template #default="{ row }">
          <el-tag :type="TAG_TYPE[statusKey(row)]" size="small">
            {{ STATUS_TXT[statusKey(row)] }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="probe" label="实测" width="92" sortable="custom" align="center">
        <template #default="{ row }">
          <!-- 不用 raw-content：实测明细文本来自外网音源脚本（作者可控），按纯文本渲染防 XSS -->
          <el-tooltip placement="top" :content="probeTip(row)" popper-class="probe-tip-popper">
            <el-tag :type="PROBE_TAG[probeKey(row)]" size="small" effect="plain">
              {{ PROBE_TXT[probeKey(row)] }}
            </el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="confidence" label="置信度" width="96" sortable="custom" align="center">
        <template #default="{ row }">
          <!-- 评分明细来自后端计算结果，纯文本渲染 -->
          <el-tooltip placement="top" :content="confidenceTip(row)" popper-class="probe-tip-popper">
            <el-tag :type="confidenceTag(row)" size="small" effect="plain">
              {{ confidenceText(row) }}
            </el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="name" label="名称" sortable="custom" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          <div class="name-cell">
            <span class="name-txt">{{ row.name || baseName(row.url) }}</span>
            <el-tag v-if="!row.looks_valid" type="warning" size="small">非音源?</el-tag>
            <el-tag
              v-else-if="row.valid_tier !== 'confirmed'"
              type="warning"
              size="small"
              effect="plain"
            >
              待验证
            </el-tag>
            <el-tooltip v-if="(row.mirrors || []).length" placement="top">
              <template #content>
                <div v-for="(u, i) in row.mirrors" :key="i">{{ u }}</div>
              </template>
              <el-tag size="small" type="info" effect="plain">+{{ row.mirrors.length }} 镜像</el-tag>
            </el-tooltip>
            <el-tooltip v-if="(row.variants || []).length" placement="top">
              <template #content>
                <div v-for="(v, i) in row.variants" :key="i">
                  {{ v.origin_repo || v.url }}{{ v.version ? ' · v' + v.version : '' }}{{ v.is_alive ? '' : ' · 失效' }}
                </div>
              </template>
              <el-tag size="small" type="info" effect="plain">
                +{{ row.variants.length }} 变体
              </el-tag>
            </el-tooltip>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="plat" label="疑似平台" sortable="custom" width="160">
        <template #default="{ row }">
          <template v-if="(row.platforms || []).length">
            <el-tag
              v-for="p in row.platforms"
              :key="p"
              size="small"
              effect="plain"
              class="plat-tag"
            >
              {{ PLAT[p] || p }}
            </el-tag>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="ver" label="版本" sortable="custom" width="90">
        <template #default="{ row }">
          <span class="muted">{{ row.version || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="auth" label="作者" sortable="custom" width="130" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="muted">{{ row.author || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="upd" label="更新" sortable="custom" width="110">
        <template #default="{ row }">
          <span class="muted" :title="tipBits(row)">{{ rel(row).t }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="156" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click.stop="copyText(withPrefix(row.url))">复制</el-button>
          <el-button size="small" @click.stop="download(row.id)">下载</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>
