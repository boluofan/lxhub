import type { App } from 'vue'

/** 兜底配置：`public/platform-config.json` 拉取失败时仍然可以启动 */
const defaultConfig = {
  Version: '1.5.0',
  Title: 'LX 音源',
  FixedHeader: true,
  HiddenSideBar: false,
  MultiTagsCache: true,
  KeepAlive: true,
  Layout: 'vertical',
  Theme: 'light',
  DarkMode: false,
  OverallStyle: 'light',
  Grey: false,
  Weak: false,
  HideTabs: false,
  HideFooter: true,
  Stretch: false,
  SidebarStatus: true,
  EpThemeColor: '#409EFF',
  ShowLogo: true,
  ShowModel: 'smart',
  TooltipEffect: 'light',
  MaxTagsLevel: 10,
  ResponsiveStorageNameSpace: 'lxhub-',
} as PlatformConfigs

let config: PlatformConfigs = { ...defaultConfig }

const setConfig = (cfg?: Partial<PlatformConfigs>): void => {
  config = Object.assign(config, cfg)
}

/** 支持 `a.b.c` 形式的点路径取值，不传 key 时返回整份配置 */
const getConfig = (key?: string): any => {
  if (typeof key === 'string' && key) {
    let data: any = config
    for (const k of key.split('.')) {
      if (data && typeof data[k] !== 'undefined') data = data[k]
      else return null
    }
    return data
  }
  return config
}

/**
 * 获取项目动态全局配置。
 *
 * 配置放在 `public/platform-config.json`，打包后仍可修改，无需重新构建。
 * 拉取失败时回退到内置默认值而不是阻断启动。
 */
export const getPlatformConfig = async (app: App): Promise<PlatformConfigs> => {
  app.config.globalProperties.$config = getConfig()
  try {
    const resp = await fetch(`${import.meta.env.BASE_URL}platform-config.json`, {
      cache: 'no-cache',
    })
    const remote = (await resp.json()) as Partial<PlatformConfigs>
    if (remote && typeof remote === 'object') {
      setConfig(remote)
      const $config = Object.assign(app.config.globalProperties.$config, remote)
      app.config.globalProperties.$config = $config
    }
  } catch {
    // 静态部署时可能取不到该文件，忽略并使用默认值
  }
  return getConfig()
}

/** 本地响应式存储的命名空间 */
const responsiveStorageNameSpace = (): string => getConfig().ResponsiveStorageNameSpace ?? 'lxhub-'

export { getConfig, setConfig, responsiveStorageNameSpace }
