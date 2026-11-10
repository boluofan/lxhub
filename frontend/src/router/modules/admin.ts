import Layout from '@/layout/index.vue'
import {
  Cpu,
  Download,
  Files,
  Fold,
  HomeFilled,
  Odometer,
  Operation,
  Setting,
  Timer,
} from '@element-plus/icons-vue'
import type { RouteConfigs } from '@/layout/types'

/**
 * 后台路由（即菜单来源）。
 *
 * 菜单完全由路由驱动：新增页面只需在此追加一条记录，
 * 侧边栏、标签页、面包屑、权限过滤都会自动生效。
 */
export default {
  path: '/admin',
  name: 'Admin',
  component: Layout,
  redirect: '/admin/overview',
  meta: {
    title: '后台管理',
    icon: HomeFilled,
    rank: 1,
  },
  children: [
    {
      path: '/admin/overview',
      name: 'AdminOverview',
      component: () => import('@/views/admin/overview/index.vue'),
      meta: {
        title: '概览',
        icon: Odometer,
        rank: 1,
        keepAlive: true,
        auths: ['task:crawl', 'task:check', 'task:cancel'],
      },
    },
    {
      path: '/admin/sources',
      name: 'AdminSources',
      component: () => import('@/views/admin/sources/index.vue'),
      meta: {
        title: '音源管理',
        icon: Files,
        rank: 2,
        keepAlive: true,
        auths: ['source:export'],
      },
    },
    {
      path: '/admin/config',
      name: 'AdminConfig',
      redirect: '/admin/config/source',
      meta: {
        title: '配置管理',
        icon: Setting,
        rank: 3,
      },
      children: [
        {
          path: '/admin/config/source',
          name: 'AdminConfigSource',
          component: () => import('@/views/admin/config/source.vue'),
          meta: {
            title: '收录来源',
            icon: Fold,
            keepAlive: true,
            auths: ['config:save'],
          },
        },
        {
          path: '/admin/config/crawl',
          name: 'AdminConfigCrawl',
          component: () => import('@/views/admin/config/crawl.vue'),
          meta: {
            title: '爬取设置',
            icon: Operation,
            keepAlive: true,
            auths: ['config:save'],
          },
        },
        {
          path: '/admin/config/schedule',
          name: 'AdminConfigSchedule',
          component: () => import('@/views/admin/config/schedule.vue'),
          meta: {
            title: '定时任务',
            icon: Timer,
            keepAlive: true,
            auths: ['config:save'],
          },
        },
        {
          path: '/admin/config/probe',
          name: 'AdminConfigProbe',
          component: () => import('@/views/admin/config/probe.vue'),
          meta: {
            title: '沙箱实测',
            icon: Cpu,
            keepAlive: true,
            auths: ['config:save'],
          },
        },
        {
          path: '/admin/config/export',
          name: 'AdminConfigExport',
          component: () => import('@/views/admin/config/export.vue'),
          meta: {
            title: '本地化输出',
            icon: Download,
            keepAlive: true,
            auths: ['config:save'],
          },
        },
      ],
    },
  ],
} satisfies RouteConfigs
