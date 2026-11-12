export type FieldType =
  | 'text'
  | 'password'
  | 'lines'
  | 'prefixes'
  | 'number'
  | 'float'
  | 'bool'
  /** 展示容器：把若干附属字段收进一个视觉子块，自身不参与配置读写 */
  | 'subgroup'

export interface AdminField {
  key: string
  label: string
  type: FieldType
  /** 补充说明（tooltip） */
  tip?: string
  /** 输入框下方的一行浅色短提示 */
  hint?: string
  /** 数值字段的上下界 */
  min?: number
  max?: number
  /** subgroup 的附属字段；其 key 才参与配置读写 */
  children?: AdminField[]
}

export interface AdminConfigResp {
  config: Record<string, unknown>
  github_token_set: boolean
  admin_enabled: boolean
}

export interface LoginResult {
  token: string
  expires_in: number
}

export interface RunInfo {
  started_at?: string
  finished_at?: string
}

/** worker 解析出的音频真实规格（容器/码率/采样率/位深等） */
export interface ProbeSpec {
  container?: string
  bitrate?: number
  sampleRate?: number
  bitDepth?: number
  channels?: number
  totalSamples?: number
  avgBitrateKbps?: number
  seconds?: number
  bytes?: number
  mpeg?: number
  specUnverified?: string
}

/** 单个平台实测结果（Node 沙箱 worker 产出） */
export interface ProbeRes {
  status: 'usable' | 'failed' | 'skipped'
  ms?: number
  bitrate?: number
  duration?: number
  format?: string
  spec?: ProbeSpec | null
  quality?: 'matched' | 'degraded' | 'mismatch' | 'refused' | 'unknown'
  reason?: string
  message?: string
  url?: string
}

/** 一条音源的完整实测结果 JSON（存 probe_detail） */
export interface ProbeDetail {
  status: 'usable' | 'degraded' | 'not-loadable'
  declared?: string[]
  platforms?: Record<string, ProbeRes>
  loadError?: string
  updateAlert?: { version?: string; log?: string } | null
}

/** 实测置信度评分分项（后端 confidence.py 产出，存 confidence_detail） */
export interface ConfidenceParts {
  base?: number
  platforms?: number
  coverage?: number
  quality?: number
  spec?: number
  freshness?: number
  penalty?: number
}

export interface ConfidenceDetail {
  score?: number
  parts?: ConfidenceParts
  success?: number
  tested?: number
  quality_verdict?: string
  spec_level?: string
  reason?: string
}

/** 沙箱实测可用性（/api/status.probe_capability）：缺 Node 时前端禁用入口 */
export interface ProbeCapability {
  available: boolean
  node?: string
  reason?: string | null
}

/** 本地化输出：一个固定文件名对应的音源（来自导出目录 manifest.json） */
export interface LocalExportFile {
  filename: string
  rank: number
  source_id: number
  name: string
  url: string
  confidence: number
  sha256: string
  bytes: number
  /** true = 高置信记录不足时由次优记录补齐 */
  filled: boolean
}

export interface LocalExportStatus {
  enabled: boolean
  top_n: number
  filename: string
  interval_hours: number
  after_probe: boolean
  min_confidence: number
  fill_shortfall: boolean
  prune_stale: boolean
  dir: string
  dir_from_env: boolean
  serve_prefix: string
  files: LocalExportFile[]
  generated_at: string
  last_run: RunInfo | null
}

export interface StatusResp {
  busy: 'crawl' | 'check' | 'probe' | null
  stats: {
    total: number
    alive: number
    valid: number
    probe_usable?: number
    probe_degraded?: number
    probe_not_loadable?: number
  }
  last_crawl: RunInfo | null
  last_check: RunInfo | null
  last_probe: RunInfo | null
  last_export?: RunInfo | null
  probe_capability?: ProbeCapability
}

export interface ProgressStats {
  urls_total?: number | null
  urls_done?: number | null
  repos_total?: number | null
  repos_done?: number | null
  checked?: number | null
  valid?: number | null
  new?: number | null
  alive?: number | null
  dead?: number | null
  usable?: number | null
  degraded?: number | null
  not_loadable?: number | null
}

export interface ProgressSnapshot {
  running: boolean
  kind: 'crawl' | 'check' | 'probe' | null
  cancelled?: boolean
  message?: string
  skipped_reason?: string | null
  elapsed?: number
  stats?: ProgressStats
  logs?: string[]
}

export interface Source {
  id: number
  url: string
  name?: string
  version?: string
  author?: string
  description?: string
  platforms?: string[]
  script_type?: 'source' | 'companion' | 'unknown'
  looks_valid?: boolean
  valid_tier?: 'confirmed' | 'probable' | null
  is_alive?: boolean
  api_alive?: 'up' | 'down' | 'unk'
  http_status?: number | null
  api_hosts?: string | string[]
  mirrors?: string[]
  variants?: {
    origin_repo?: string
    url?: string
    version?: string
    is_alive?: boolean
    confidence?: number
  }[]
  origin_url?: string
  origin_repo?: string
  file_size?: number | null
  sha256?: string | null
  first_seen?: string
  content_updated_at?: string
  repo_pushed_at?: string
  last_checked?: string
  last_alive?: string
  probe_status?: 'usable' | 'degraded' | 'not-loadable' | ''
  probe_detail?: ProbeDetail | { [k: string]: unknown }
  probe_checked?: string
  probe_fail_streak?: number
  /** 实测置信度 0–100，仅 probe_status='usable' 有非零分 */
  confidence?: number
  confidence_detail?: ConfidenceDetail
  content_text?: string
  _no?: number
}

export interface SourcesResp {
  sources: Source[]
  mirror_prefixes: (string | { name: string; prefix: string; hosts?: string[] })[]
}
