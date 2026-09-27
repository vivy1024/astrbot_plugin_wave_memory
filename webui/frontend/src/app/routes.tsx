import { lazy, type ComponentType } from 'react'
import {
  ActivityIcon,
  BookHeartIcon,
  BookOpenIcon, NotebookPenIcon,
  BotIcon,
  ServerCogIcon,
  DatabaseIcon,
  FlaskConicalIcon,
  DownloadIcon,
  GaugeIcon,
  FeatherIcon,
  MessageSquareTextIcon,
  NetworkIcon,
  GitBranchIcon,
  GitCompareArrowsIcon,
  HeartIcon,
  SearchCheckIcon,
  Settings2Icon,
  SlidersIcon,
  SmileIcon,
  SparklesIcon,
  TagsIcon,
  UsersIcon,
} from 'lucide-react'

import type { RouteScopeAllowances, RouteScopeLevel } from '@/lib/global-scope'

const DashboardPage = lazy(() => import('@/pages/dashboard/DashboardPage').then((m) => ({ default: m.DashboardPage })))
const MemoriesPage = lazy(() => import('@/pages/memories/MemoriesPage').then((m) => ({ default: m.MemoriesPage })))
const TagsPage = lazy(() => import('@/pages/tags/TagsPage').then((m) => ({ default: m.TagsPage })))
const GraphPage = lazy(() => import('@/pages/graph/GraphPage').then((m) => ({ default: m.GraphPage })))
const ImportPage = lazy(() => import('@/pages/import/ImportPage').then((m) => ({ default: m.ImportPage })))
const MaintenancePage = lazy(() => import('@/pages/maintenance/MaintenancePage').then((m) => ({ default: m.MaintenancePage })))
const InjectionPage = lazy(() => import('@/pages/injection/InjectionPage').then((m) => ({ default: m.InjectionPage })))
const ChannelConfigPage = lazy(() => import('@/pages/channels/ChannelConfigPage').then((m) => ({ default: m.ChannelConfigPage })))
const BeliefsPage = lazy(() => import('@/pages/beliefs/BeliefsPage').then((m) => ({ default: m.BeliefsPage })))
const JargonPage = lazy(() => import('@/pages/jargon/JargonPage').then((m) => ({ default: m.JargonPage })))
const SoulPage = lazy(() => import('@/pages/soul/SoulPage').then((m) => ({ default: m.SoulPage })))
const BookLorePage = lazy(() => import('@/pages/knowledge/BookLorePage').then((m) => ({ default: m.BookLorePage })))
const BookLoreWorkbenchPage = lazy(() => import('@/pages/knowledge/BookLoreWorkbenchPage').then((m) => ({ default: m.BookLoreWorkbenchPage })))
const ExperiencesPage = lazy(() => import('@/pages/knowledge/ExperiencesPage').then((m) => ({ default: m.ExperiencesPage })))
const FewShotPage = lazy(() => import('@/pages/knowledge/FewShotPage').then((m) => ({ default: m.FewShotPage })))
const FactsPage = lazy(() => import('@/pages/knowledge/FactsPage').then((m) => ({ default: m.FactsPage })))
const PeoplePage = lazy(() => import('@/pages/people/PeoplePage').then((m) => ({ default: m.PeoplePage })))
const IndexesPage = lazy(() => import('@/pages/diagnostics/IndexesPage').then((m) => ({ default: m.IndexesPage })))
const CompatibilityPage = lazy(() => import('@/pages/review/CompatibilityPage').then((m) => ({ default: m.CompatibilityPage })))
const SettingsPage = lazy(() => import('@/pages/settings/SettingsPage').then((m) => ({ default: m.SettingsPage })))
const ConfigInventoryPage = lazy(() => import('@/pages/settings/ConfigInventoryPage').then((m) => ({ default: m.ConfigInventoryPage })))
const ServicesPage = lazy(() => import('@/pages/services/ServicesPage').then((m) => ({ default: m.ServicesPage })))
const BotsPage = lazy(() => import('@/pages/bots/BotsPage').then((m) => ({ default: m.BotsPage })))
const BotHomePage = lazy(() => import('@/pages/bot-home/BotHomePage').then((m) => ({ default: m.BotHomePage })))
const ConfigCenterPage = lazy(() => import('@/pages/config/ConfigCenterPage').then((m) => ({ default: m.ConfigCenterPage })))
const QueryLabPage = lazy(() => import('@/pages/lab/QueryLabPage').then((m) => ({ default: m.QueryLabPage })))

export type RouteGroup = 'home' | 'trace' | 'memory' | 'cognition' | 'system'
export interface AppRoute {
  /** 顶栏全局作用域：none 不显示也不自动填充；bot 只需要 Bot；session 需要 Bot + 群。默认 none。 */
  scope?: RouteScopeLevel
  /** session 级页面额外允许的会话类型。 */
  scopeAllows?: RouteScopeAllowances
  path: string
  title: string
  description: string
  group: RouteGroup
  icon: ComponentType<{ className?: string }>
  element: ComponentType
  hidden?: boolean
}

export const appRoutes: AppRoute[] = [
  // 羽书：以 Bot 为中心的视角
  { path: '/home', scope: 'bot', title: 'Bot 主页', description: '她今天记住了什么、心情、关系变化与新学到的', group: 'home', icon: FeatherIcon, element: BotHomePage },
  { path: '/dashboard', title: '系统总览', description: '真实健康、待办与近期异常', group: 'home', icon: GaugeIcon, element: DashboardPage },
  // 回复溯源：一次回复用了什么、为什么
  { path: '/observatory', title: '注入观测台', description: '查看一次回复用了哪些记忆通道', group: 'trace', icon: ActivityIcon, element: InjectionPage },
  { path: '/lab', scope: 'session', title: '检索实验室', description: '同一句查询对比不同检索算法的召回差异', group: 'trace', icon: FlaskConicalIcon, element: QueryLabPage },
  // 记忆库
  { path: '/memories', scope: 'session', title: '记忆', description: '按 Bot 和群查看、编辑记忆', group: 'memory', icon: DatabaseIcon, element: MemoriesPage },
  { path: '/tags', title: '标签', description: '覆盖率、频率、类型与置信度总览', group: 'memory', icon: TagsIcon, element: TagsPage },
  { path: '/graph', scope: 'session', title: '关系图谱', description: '知识图谱与标签共现：最强关系、新出现、当前话题与路径', group: 'memory', icon: NetworkIcon, element: GraphPage },
  { path: '/knowledge/book-lore', title: '书设定', description: '独立只读语料、解析与本地化', group: 'memory', icon: BookOpenIcon, element: BookLorePage },
  { path: '/knowledge/book-lore/workbench', title: '书设工作台', description: '新章节导入、笔记编辑、检索索引补齐，保存即生效', group: 'memory', icon: NotebookPenIcon, element: BookLoreWorkbenchPage },
  { path: '/import', title: '导入', description: '从来源预检并导入记忆', group: 'memory', icon: DownloadIcon, element: ImportPage },
  // 认知：她对人和世界的理解
  { path: '/people', scope: 'session', scopeAllows: { legacy: true }, title: '人物与关系', description: '按 Bot、群和用户查看画像与好感', group: 'cognition', icon: UsersIcon, element: PeoplePage },
  { path: '/facts', scope: 'session', scopeAllows: { private: true }, title: '事实', description: '人物/事物关系、证据与人工审核', group: 'cognition', icon: GitBranchIcon, element: FactsPage },
  { path: '/beliefs', scope: 'session', title: '信念', description: '证据健康与生命周期审核', group: 'cognition', icon: BookHeartIcon, element: BeliefsPage },
  { path: '/jargon', scope: 'session', title: '黑话与口癖', description: '群聊习得黑话、证据审核与内置口癖资产', group: 'cognition', icon: SmileIcon, element: JargonPage },
  { path: '/knowledge/experiences', scope: 'session', scopeAllows: { legacy: true }, title: '经历片段', description: '对话沉淀的个人经历与重要事件', group: 'cognition', icon: SparklesIcon, element: ExperiencesPage },
  { path: '/knowledge/style-examples', scope: 'session', title: '风格样例', description: '已通过审核的正式回复范例', group: 'cognition', icon: MessageSquareTextIcon, element: FewShotPage },
  { path: '/soul', scope: 'session', title: '心智状态', description: 'Bot 自己的心情、关切、作息与时间线', group: 'cognition', icon: HeartIcon, element: SoulPage },
  // 系统
  { path: '/config', title: '配置中心', description: '按功能查找配置、高风险项与各层来源，一处进入', group: 'system', icon: Settings2Icon, element: ConfigCenterPage },
  { path: '/bots', title: 'Bot 管理', description: '新增、编辑、停用 Bot，保存即热生效', group: 'system', icon: BotIcon, element: BotsPage },
  { path: '/services', title: '服务与扩展', description: '后台服务启停、工具开关与扩展加载状态', group: 'system', icon: ServerCogIcon, element: ServicesPage },
  { path: '/maintenance', title: '维护任务', description: '可恢复任务、进度、日志与取消', group: 'system', icon: SlidersIcon, element: MaintenancePage },
  { path: '/diagnostics/indexes', title: '索引诊断', description: '检索索引来源、数量与健康状态', group: 'system', icon: SearchCheckIcon, element: IndexesPage },
  { path: '/compatibility', title: '生态兼容', description: '探测状态、来源、错误与证据', group: 'system', icon: GitCompareArrowsIcon, element: CompatibilityPage },
  // 已收进配置中心的旧入口：保留路由供深链接（?key= / ?channel= 定位），不在侧栏单独出现
  { path: '/channels', title: '通道配置', description: '各注入通道的保存值与当前生效值', group: 'system', icon: Settings2Icon, element: ChannelConfigPage, hidden: true },
  { path: '/settings/inventory', title: '配置来源', description: '四层配置摊平：每项的当前值、来源与生效方式', group: 'system', icon: SlidersIcon, element: ConfigInventoryPage, hidden: true },
  { path: '/settings', title: '系统配置', description: '默认值、已保存值和当前生效方式', group: 'system', icon: SlidersIcon, element: SettingsPage, hidden: true },
]

export const defaultRoute = '/home'
export function getRouteByPath(pathname: string): AppRoute {
  return appRoutes.find((route) => route.path === pathname) ?? appRoutes[0]
}
