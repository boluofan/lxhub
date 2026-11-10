<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ConfigForm from '@/components/ConfigForm.vue'
import {
  fetchLocalExportStatus,
  localFileUrl,
  runLocalExport,
} from '@/api/localExport'
import { message } from '@/utils/message'
import type { LocalExportStatus } from '@/types'

defineOptions({ name: 'AdminConfigExport' })

const status = ref<LocalExportStatus | null>(null)
const exporting = ref(false)

const exampleUrl = computed(() => {
  const tpl = status.value?.filename || 'lx-source-{rank}.js'
  const name = tpl.replace(/\{rank:?[^}]*\}/, '1')
  return `${window.location.origin}${localFileUrl(name)}`
})

const lastTime = computed(() => {
  const r = status.value?.last_run
  const s = r?.finished_at || r?.started_at || ''
  return s ? s.replace('T', ' ').replace('+00:00', ' UTC') : '尚未导出'
})

async function load(): Promise<void> {
  try {
    status.value = await fetchLocalExportStatus()
  } catch {
    return
  }
}

async function exportNow(): Promise<void> {
  exporting.value = true
  try {
    const r = await runLocalExport()
    message(`已导出 ${r.written} 个文件${r.shortfall ? `（缺口 ${r.shortfall}）` : ''}`, {
      type: 'success',
    })
    await load()
  } finally {
    exporting.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div>
    <el-alert type="info" show-icon :closable="false" style="margin-bottom: 14px">
      <template #title>固定文件名 · 自动覆盖 · 对外地址永不变</template>
      容器内导出目录固定为 <code>/app/outjs</code>，在 docker-compose.yml 里把宿主机或 NAS
      目录挂载到该路径即可自定义落盘位置。Top 榜更换时覆盖同名文件（原子替换，
      不会读到半截脚本），对外接口的文件名始终不变。
    </el-alert>

    <el-card shadow="never" style="margin-bottom: 14px">
      <template #header>
        <div class="card-head">
          <span>导出状态</span>
          <div>
            <el-button size="small" @click="load">刷新</el-button>
            <el-button size="small" type="primary" :loading="exporting" @click="exportNow">
              立即导出
            </el-button>
          </div>
        </div>
      </template>

      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="导出目录">{{ status?.dir || '—' }}</el-descriptions-item>
        <el-descriptions-item label="最近导出">{{ lastTime }}</el-descriptions-item>
        <el-descriptions-item label="固定 URL 示例" :span="2">
          <a :href="exampleUrl" target="_blank" rel="noopener">{{ exampleUrl }}</a>
        </el-descriptions-item>
      </el-descriptions>

      <el-table v-if="status?.files?.length" :data="status.files" size="small" style="margin-top: 14px">
        <el-table-column prop="rank" label="名次" width="70" />
        <el-table-column label="文件名" min-width="180">
          <template #default="{ row }">
            <a :href="localFileUrl(row.filename)" target="_blank" rel="noopener">
              {{ row.filename }}
            </a>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="音源" min-width="160" show-overflow-tooltip />
        <el-table-column prop="confidence" label="置信度" width="90" />
        <el-table-column label="标记" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.filled" type="warning" size="small">补齐</el-tag>
            <el-tag v-else type="success" size="small">实测</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="尚无导出文件，点「立即导出」生成" :image-size="70" />
    </el-card>

    <ConfigForm group-key="export" />
  </div>
</template>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

code {
  padding: 1px 5px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

a {
  color: var(--el-color-primary);
  text-decoration: none;
}

a:hover {
  text-decoration: underline;
}
</style>
