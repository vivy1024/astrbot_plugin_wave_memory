import { lazy, type ComponentType } from 'react'
import {
  ActivityIcon,
  BookHeartIcon,
  BookOpenIcon, NotebookPenIcon,
  BotIcon,
  ServerCogIcon,
  BrainCircuitIcon,
  CompassIcon,
  DatabaseIcon,
  FlaskConicalIcon,
  DownloadIcon,
  GaugeIcon,
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
const ExplorePage = lazy(() => import('@/pages/PlaceholderPage').then((m) => ({ default: m.ExplorePage })))
const MemoriesPage = lazy(() => import('@/pages/memories/MemoriesPage').then((m) => ({ default: m.MemoriesPage })))
const TagsPage = lazy(() => import('@/pages/tags/TagsPage').then((m) => ({ default: m.TagsPage })))
const TagGraphPage = lazy(() => import('@/pages/tags/TagGraphPage').then((m) => ({ default: m.TagGraphPage })))
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
const QueryLabPage = lazy(() => import('@/pages/lab/QueryLabPage').then((m) => ({ default: m.QueryLabPage })))

export type RouteGroup = 'overview' | 'data' | 'runtime' | 'cognition' | 'knowledge' | 'system'
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
  { path: '/dashboard', title: '总览', description: '真实健康、待办与近期异常', group: 'overview', icon: GaugeIcon, element: DashboardPage },
  { path: '/explore', title: '神经云图', description: '3D 交互式高维记忆与关系星图', group: 'overview', icon: CompassIcon, element: ExplorePage },
  { path: '/memories', scope: 'session', title: '记忆', description: '按 Bot 和群查看、编辑记忆', group: 'data', icon: DatabaseIcon, element: MemoriesPage },
  { path: '/tags', title: '标签', description: '覆盖率、频率、类型与置信度总览', group: 'data', icon: TagsIcon, element: TagsPage },
  { path: '/tags/graph', title: '标签神经星云', description: '标签共现网络、突触脉冲与高维拓扑星云', group: 'data', icon: BrainCircuitIcon, element: TagGraphPage },
  { path: '/lab', scope: 'session', title: '算法实验室', description: '同一句查询对比标签神经星云的高级拓扑检索算法', group: 'data', icon: FlaskConicalIcon, element: QueryLabPage, hidden: true },
  { path: '/import', title: '导入', description: '从来源预检并导入记忆', group: 'data', icon: DownloadIcon, element: ImportPage },
  { path: '/maintenance', title: '维护任务', description: '可恢复任务、进度、日志与取消', group: 'runtime', icon: SlidersIcon, element: MaintenancePage },
  { path: '/observatory', title: '注入观测台', description: '查看一次回复用了哪些记忆通道', group: 'runtime', icon: ActivityIcon, element: InjectionPage },
  { path: '/channels', title: '通道配置', description: '各注入通道的保存值与当前生效值', group: 'runtime', icon: Settings2Icon, element: ChannelConfigPage },
  { path: '/beliefs', scope: 'session', title: '信念', description: '证据健康与生命周期审核', group: 'cognition', icon: BookHeartIcon, element: BeliefsPage },
  { path: '/jargon', scope: 'session', title: '黑话与口癖', description: '群聊习得黑话、证据审核与内置口癖资产', group: 'cognition', icon: SmileIcon, element: JargonPage },
  { path: '/facts', scope: 'session', scopeAllows: { private: true }, title: '事实', description: '人物/事物关系、证据与人工审核', group: 'cognition', icon: GitBranchIcon, element: FactsPage },
  { path: '/soul', scope: 'session', title: '心智状态', description: 'Bot 自己的心情、关切、作息与时间线', group: 'cognition', icon: HeartIcon, element: SoulPage },
  { path: '/knowledge/book-lore', title: '书设定', description: '独立只读语料、解析与本地化', group: 'knowledge', icon: BookOpenIcon, element: BookLorePage },
  { path: '/knowledge/book-lore/workbench', title: '书设工作台', description: '新章节导入、笔记编辑、检索索引补齐，保存即生效', group: 'knowledge', icon: NotebookPenIcon, element: BookLoreWorkbenchPage },
  { path: '/knowledge/experiences', scope: 'session', scopeAllows: { legacy: true }, title: '经历片段', description: '对话沉淀的个人经历与重要事件', group: 'knowledge', icon: SparklesIcon, element: ExperiencesPage },
  { path: '/knowledge/style-examples', scope: 'session', title: '风格样例', description: '已通过审核的正式回复范例', group: 'knowledge', icon: BrainCircuitIcon, element: FewShotPage },
  { path: '/people', scope: 'session', scopeAllows: { legacy: true }, title: '人物与关系', description: '按 Bot、群和用户查看画像与好感', group: 'knowledge', icon: UsersIcon, element: PeoplePage },
  { path: '/bots', title: 'Bot 管理', description: '新增、编辑、停用 Bot，保存即热生效', group: 'system', icon: BotIcon, element: BotsPage },
  { path: '/services', title: '服务与扩展', description: '后台服务启停、工具开关与扩展加载状态', group: 'runtime', icon: ServerCogIcon, element: ServicesPage },
  { path: '/diagnostics/indexes', title: '索引诊断', description: '检索索引来源、数量与健康状态', group: 'system', icon: SearchCheckIcon, element: IndexesPage },
  { path: '/compatibility', title: '生态兼容', description: '探测状态、来源、错误与证据', group: 'system', icon: GitCompareArrowsIcon, element: CompatibilityPage },
  { path: '/settings/inventory', title: '配置来源', description: '四层配置摊平：每项的当前值、来源与生效方式', group: 'system', icon: SlidersIcon, element: ConfigInventoryPage },
  { path: '/settings', title: '系统配置', description: '默认值、已保存值和当前生效方式', group: 'system', icon: SlidersIcon, element: SettingsPage },
]

export const defaultRoute = '/dashboard'
export function getRouteByPath(pathname: string): AppRoute {
  return appRoutes.find((route) => route.path === pathname) ?? appRoutes[0]
}
