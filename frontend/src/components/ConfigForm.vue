<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
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
 * mirror_max_attempts / mirror_fail_threshold / mirror_quarantine_seconds /
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
      {
        key: '_mirror_policy',
        label: '多前缀容错策略',
        type: 'subgroup',
        tip: '这三个参数只影响上方 GitHub 加速前缀的抓取行为；未配置前缀时不起作用（GitHub API 搜索、jsdelivr、后端域名探测一律直连）。修改任一参数会清空已有的失败计数与隔离状态。',
        children: [
          {
            key: 'mirror_max_attempts',
            label: '单请求最多尝试前缀数',
            type: 'number',
            min: 1,
            max: 10,
            hint: '默认 2 · 1 = 关闭补试',
            tip: '某条前缀失败时自动补试下一条，最多试几条（含起点那条）。默认 2，范围 1~10；1 = 关闭补试，调大更抗故障但会放大请求量并拉长最坏耗时',
          },
          {
            key: 'mirror_fail_threshold',
            label: '连续失败几次后摘除前缀',
            type: 'number',
            min: 1,
            max: 100,
            hint: '默认 3 · 越小越灵敏',
            tip: '某条前缀连续失败达到此值就临时摘除，期间不再被选中；期间成功一次即清零。默认 3，范围 1~100；调小更灵敏，但偶发 429 / 网络抖动容易误伤好镜像',
          },
          {
            key: 'mirror_quarantine_seconds',
            label: '前缀摘除时长（秒）',
            type: 'float',
            min: 0,
            max: 86400,
            hint: '默认 300 · 约 5 分钟',
            tip: '摘除后隔多久放一条半开探测，成功即恢复、失败重新摘除。默认 300（5 分钟），范围 0~86400。修改以上任一策略参数会清空已有的失败计数与隔离状态',
          },
        ],
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
  export: {
    title: '本地化输出',
    fields: [
      {
        key: 'export.enabled',
        label: '启用自动导出',
        type: 'bool',
        tip: '控制「定时导出」与「每轮实测后自动导出」。下方「立即导出」按钮不受此开关限制，方便先验证再开启',
      },
      {
        key: 'export.top_n',
        label: '导出前 N 名',
        type: 'number',
        min: 1,
        max: 50,
        hint: '默认 3 · 范围 1~50',
        tip: '按实测置信度降序取前 N 名；同名变体与同内容镜像已合并，不会重复占位',
      },
      {
        key: 'export.filename',
        label: '固定文件名模板',
        type: 'text',
        hint: '默认 lx-source-{rank}.js · 可写 {rank:02d} 补零',
        tip: '只允许 {rank} 占位符：文件名只由名次决定，Top 榜更换时覆盖同名文件，对外 URL 永远不变。含 {name}/{date} 等会漂移的模板会被拒绝',
      },
      {
        key: 'export.min_confidence',
        label: '最低置信度门槛',
        type: 'number',
        min: 0,
        max: 100,
        hint: '默认 1 · 只有实测 usable 才有非零分',
        tip: '只有置信度不低于此值的实测可用音源才算「高置信」；设为 0 表示不设门槛',
      },
      {
        key: 'export.interval_hours',
        label: '定时导出间隔（小时，0 = 不定时）',
        type: 'float',
        min: 0,
        hint: '默认 24 · 调度器每 15 分钟检查一次',
      },
      {
        key: 'export.after_probe',
        label: '每轮实测后自动导出',
        type: 'bool',
        tip: '置信度一更新就立即刷新本地文件，保证对外接口总能拿到最新最优的音源；推荐与「定时任务」里的复检周期配合',
      },
      {
        key: 'export.fill_shortfall',
        label: '不足 N 条时用次优音源补齐',
        type: 'bool',
        tip: '保证固定数量的文件始终存在，不会因为某轮合格音源变少而留下过期文件；补齐项会在 manifest.json 里标记 filled',
      },
      {
        key: 'export.prune_stale',
        label: '缩小 N 后清理多余旧文件',
        type: 'bool',
        tip: '只删除上一版 manifest.json 记录过、本次不再生成的 .js 文件，不会触碰目录里其他任何内容。默认关闭',
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

/** subgroup 只是展示容器，配置读写只认它 children 里的真实字段 */
const flatFields: AdminField[] = group.fields.flatMap((f) => f.children ?? [f])
/** 未配置加速前缀时，附属策略参数置灰并提示不生效 */
const hasMirrors = computed(() =>
  (textModel.fetch_mirrors || '').split('\n').some((x) => x.trim().length > 0),
)

function fmtPrefix(p: unknown): string {
  return typeof p === 'string' ? p : ((p as { prefix?: string })?.prefix || '')
}

function resetModels(): void {
  for (const f of flatFields) {
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
    for (const f of flatFields) {
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
  for (const f of flatFields) {
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
      <template v-for="f in group.fields" :key="f.key">
        <!-- 普通字段 -->
        <el-form-item v-if="!f.children">
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
            :min="f.min ?? 0"
            :max="f.max"
            controls-position="right"
            style="width: 200px"
          />
          <el-input-number
            v-else-if="f.type === 'float'"
            v-model="numModel[f.key]"
            :min="f.min ?? 0"
            :max="f.max"
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

        <!-- 附属参数组：缩进 + 竖线 + 浅底，视觉上从属于上方字段 -->
        <div v-else class="subgroup" :class="{ 'subgroup--idle': !hasMirrors }">
          <div class="subgroup-head">
            <span class="subgroup-title">{{ f.label }}</span>
            <el-tooltip v-if="f.tip" :content="f.tip" placement="top">
              <el-icon class="field-tip"><QuestionFilled /></el-icon>
            </el-tooltip>
            <span v-if="!hasMirrors" class="subgroup-warn">未配置GitHub 加速前缀，以下参数不会生效</span>
          </div>
          <div class="subgroup-grid">
            <div v-for="c in f.children" :key="c.key" class="subgroup-item">
              <div class="subgroup-label">
                {{ c.label }}
                <el-tooltip v-if="c.tip" :content="c.tip" placement="top">
                  <el-icon class="field-tip"><QuestionFilled /></el-icon>
                </el-tooltip>
              </div>
              <el-input-number
                v-model="numModel[c.key]"
                :min="c.min ?? 0"
                :max="c.max"
                :step="1"
                :precision="0"
                controls-position="right"
                style="width: 100%"
              />
              <div v-if="c.hint" class="subgroup-hint">{{ c.hint }}</div>
            </div>
          </div>
        </div>
      </template>
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

/* 附属参数子块：缩进 + 左侧竖线 + 浅底，明确「从属于上方那个字段」 */
.subgroup {
  margin: 2px 0 20px 24px;
  padding: 14px 16px 16px;
  border-left: 3px solid var(--el-color-primary-light-5);
  border-radius: 0 6px 6px 0;
  background: var(--el-fill-color-lighter);
  transition: opacity 0.2s ease;
}

.subgroup--idle {
  opacity: 0.62;
}

.subgroup-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 14px;
  font-size: 13px;
}

.subgroup-anchor {
  color: var(--el-color-primary);
  font-weight: 700;
}

.subgroup-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.subgroup-note {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.subgroup-warn {
  margin-left: auto;
  color: var(--el-color-warning);
  font-size: 12px;
}

.subgroup-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.subgroup-label {
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.subgroup-hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

@media (max-width: 900px) {
  .subgroup {
    margin-left: 8px;
  }

  .subgroup-grid {
    grid-template-columns: 1fr;
  }
}
</style>
