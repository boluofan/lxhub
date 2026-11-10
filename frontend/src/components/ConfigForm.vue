<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { QuestionFilled } from '@element-plus/icons-vue'
import { getAdminConfig, setAdminConfig } from '@/api/auth'
import { message } from '@/utils/message'
import type { AdminConfigResp, AdminField } from '@/types'

interface ConfigGroup {
  title: string
  fields: AdminField[]
}

/**
 * 后台配置分组。字段与后端 `EDITABLE` 一一对应：
 * `github_token / keywords / seed_repos / seed_urls / mirror_prefixes / fetch_mirrors /
 * max_repos_per_keyword / max_repos_total / max_files_per_repo / max_file_bytes /
 * request_timeout / concurrency / check_interval_hours / crawl_interval_hours`
 */
const GROUPS: Record<string, ConfigGroup> = {
  source: {
    title: '收录来源',
    fields: [
      {
        key: 'github_token',
        label: 'GitHub Token（可选；留空则不修改，沿用启动环境变量）',
        type: 'password',
      },
      { key: 'keywords', label: '关键词（每行一个）', type: 'lines' },
      { key: 'seed_repos', label: '种子仓库（每行一个：owner/repo）', type: 'lines' },
      { key: 'seed_urls', label: '种子直链（每行一个 URL）', type: 'lines' },
      {
        key: 'mirror_prefixes',
        label:
          '加速前缀（每行一个 URL，例：https://gh1.lhl.one/）。复制 / 导出链接时会自动拼在前面；留空则不启用',
        type: 'prefixes',
      },
    ],
  },
  crawl: {
    title: '爬取设置',
    fields: [
      { key: 'max_repos_per_keyword', label: '每关键词最多仓库数', type: 'number' },
      { key: 'max_repos_total', label: '仓库总数上限', type: 'number' },
      { key: 'max_files_per_repo', label: '每仓库最多文件数', type: 'number' },
      { key: 'max_file_bytes', label: '单文件体积上限（字节）', type: 'number' },
      { key: 'concurrency', label: '并发数', type: 'number' },
      { key: 'request_timeout', label: '请求超时（秒）', type: 'float' },
      {
        key: 'fetch_mirrors',
        label: 'GitHub 加速前缀（每行一个）',
        type: 'lines',
        tip: '留空直连。填 gh-proxy.com 这类前缀镜像（如 https://gh-proxy.com/）。镜像请求失败会自动回退直连，保存后即时生效（进行中的一轮沿用旧配置）',
      },
    ],
  },
  schedule: {
    title: '定时任务',
    fields: [
      { key: 'crawl_interval_hours', label: '全量爬取间隔（小时）', type: 'float' },
      { key: 'check_interval_hours', label: '可用性复检间隔（小时）', type: 'float' },
    ],
  },
  probe: {
    title: '沙箱实测（probe）',
    fields: [
      {
        key: 'probe.enabled',
        label: '启用沙箱实测',
        type: 'bool',
        tip: '控制「定时实测」与「复检后自动补测」；手动点「沙箱实测」按钮不受此开关限制',
      },
      {
        key: 'probe.interval_hours',
        label: '定时间隔（小时，0 = 不定时）',
        type: 'float',
        tip: '开启后每 N 小时自动实测一轮未测/内容有变化/缓存过期的音源',
      },
      {
        key: 'probe.deep',
        label: '附带 L2 音质核验（flac / flac24bit）',
        type: 'bool',
        tip: '对实测通过的平台按声明音质重新取链核对容器参数，耗时与流量倍增',
      },
      {
        key: 'probe.qualities',
        label: '核验音质清单（每行一个：128k / flac / flac24bit）',
        type: 'lines',
        tip: '仅 deep 模式用到',
      },
      { key: 'probe.max_per_run', label: '每轮最多实测条数', type: 'number' },
      {
        key: 'probe.refresh_hours',
        label: '结果缓存有效期（小时）',
        type: 'float',
        tip: '超过后视为待重测；脚本内容变化时始终立即重测',
      },
      { key: 'probe.concurrency', label: '并行 worker 数', type: 'number' },
      { key: 'probe.timeout', label: '单条脚本总预算（秒）', type: 'number' },
      { key: 'probe.node', label: 'Node 可执行文件（默认 node；需 ≥20）', type: 'text' },
      {
        key: 'probe.allow_private',
        label: '允许探测私网/本机后端域名',
        type: 'bool',
        tip: '默认禁止（防 SSRF）；仅当音源自建后端在本网段时才需要开启',
      },
    ],
  },
}

const props = defineProps<{ groupKey: string }>()
const emit = defineEmits<{ saved: [] }>()

const group = GROUPS[props.groupKey] || { title: '配置', fields: [] }
const loading = ref(false)
const saving = ref(false)
const loaded = ref(false)
const tokenPlaceholder = ref('未设置（可选，用于提高 GitHub API 配额）')
const textModel = reactive<Record<string, string>>({})
const numModel = reactive<Record<string, number | undefined>>({})
const boolModel = reactive<Record<string, boolean>>({})

function fmtPrefix(p: unknown): string {
  return typeof p === 'string' ? p : ((p as { prefix?: string })?.prefix || '')
}

function resetModels(): void {
  for (const f of group.fields) {
    if (f.type === 'bool') boolModel[f.key] = false
    else if (f.type === 'number' || f.type === 'float') numModel[f.key] = undefined
    else textModel[f.key] = ''
  }
}

async function load(): Promise<void> {
  loading.value = true
  loaded.value = false
  resetModels()
  try {
    const resp: AdminConfigResp = await getAdminConfig()
    const c = resp.config || {}
    for (const f of group.fields) {
      const val = c[f.key]
      if (f.type === 'bool') {
        boolModel[f.key] = !!val
        continue
      }
      if (f.key === 'github_token') {
        tokenPlaceholder.value = resp.github_token_set
          ? '已设置（留空则不修改）'
          : '未设置（可选，用于提高 GitHub API 配额）'
        continue
      }
      if (f.type === 'lines' || f.type === 'prefixes') {
        textModel[f.key] = (Array.isArray(val) ? val : []).map(fmtPrefix).filter(Boolean).join('\n')
        continue
      }
      if (f.type === 'number' || f.type === 'float') {
        numModel[f.key] = typeof val === 'number' ? val : undefined
        continue
      }
      textModel[f.key] = val == null ? '' : String(val)
    }
    loaded.value = true
  } finally {
    loading.value = false
  }
}

async function save(): Promise<boolean> {
  if (!loaded.value) {
    message('配置尚未载入，保存已取消（避免清空配置）', { type: 'warning' })
    return false
  }
  saving.value = true
  const body: Record<string, unknown> = {}
  for (const f of group.fields) {
    if (f.type === 'bool') {
      body[f.key] = !!boolModel[f.key]
      continue
    }
    if (f.type === 'number' || f.type === 'float') {
      const n = numModel[f.key]
      if (n != null) body[f.key] = n
      continue
    }
    const raw = textModel[f.key] ?? ''
    const s = raw.trim()
    if (f.type === 'lines' || f.type === 'prefixes') {
      body[f.key] = s
        ? s
            .split(/\n+/)
            .map((x) => x.trim())
            .filter(Boolean)
        : []
      continue
    }
    if (f.type === 'password') {
      if (s) body[f.key] = s
      continue
    }
    body[f.key] = s
  }
  try {
    await setAdminConfig(body)
    message('配置已保存并即时生效（无需重启）', { type: 'success' })
    emit('saved')
    return true
  } finally {
    saving.value = false
  }
}

defineExpose({ load, save })

onMounted(() => void load())
</script>

<template>
  <el-card v-loading="loading" shadow="never">
    <template #header>{{ group.title }}</template>
    <el-form label-position="top">
      <el-form-item v-for="f in group.fields" :key="f.key">
        <template #label>
          <span>
            {{ f.label }}
            <el-tooltip v-if="f.tip" :content="f.tip" placement="top">
              <el-icon class="field-tip"><QuestionFilled /></el-icon>
            </el-tooltip>
          </span>
        </template>
        <el-switch v-if="f.type === 'bool'" v-model="boolModel[f.key]" />
        <el-input-number
          v-else-if="f.type === 'number'"
          v-model="numModel[f.key]"
          :min="0"
          controls-position="right"
          style="width: 200px"
        />
        <el-input-number
          v-else-if="f.type === 'float'"
          v-model="numModel[f.key]"
          :min="0"
          :step="0.5"
          :precision="2"
          controls-position="right"
          style="width: 200px"
        />
        <el-input
          v-else-if="f.type === 'password'"
          v-model="textModel[f.key]"
          type="password"
          show-password
          autocomplete="new-password"
          :placeholder="tokenPlaceholder"
        />
        <el-input
          v-else-if="f.type === 'lines' || f.type === 'prefixes'"
          v-model="textModel[f.key]"
          type="textarea"
          :rows="4"
        />
        <el-input v-else v-model="textModel[f.key]" />
      </el-form-item>
    </el-form>
    <div class="admin-actions">
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      <el-button @click="load">重新载入</el-button>
    </div>
  </el-card>
</template>

<style scoped>
.field-tip {
  margin-left: 4px;
  vertical-align: -2px;
  color: var(--el-text-color-placeholder);
  cursor: help;
}
</style>
