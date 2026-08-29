import type { ComponentType } from 'react'
import { ActivityIcon, BookHeartIcon, BookOpenIcon, BrainCircuitIcon, DatabaseIcon, DownloadIcon, GaugeIcon, GitBranchIcon, GitCompareArrowsIcon, HeartIcon, SearchCheckIcon, Settings2Icon, SlidersIcon, SmileIcon, SparklesIcon, TagsIcon, UsersIcon, CompassIcon } from 'lucide-react'

import { BeliefsPage } from '@/pages/beliefs/BeliefsPage'
import { ChannelConfigPage } from '@/pages/channels/ChannelConfigPage'
import { DashboardPage } from '@/pages/dashboard/DashboardPage'
import { IndexesPage } from '@/pages/diagnostics/IndexesPage'
import { ImportPage } from '@/pages/import/ImportPage'
import { InjectionPage } from '@/pages/injection/InjectionPage'
import { JargonPage } from '@/pages/jargon/JargonPage'
import { BookLorePage } from '@/pages/knowledge/BookLorePage'
import { ExperiencesPage } from '@/pages/knowledge/ExperiencesPage'
import { FactsPage } from '@/pages/knowledge/FactsPage'
import { FewShotPage } from '@/pages/knowledge/FewShotPage'
import { MaintenancePage } from '@/pages/maintenance/MaintenancePage'
import { MemoriesPage } from '@/pages/memories/MemoriesPage'
import { PeoplePage } from '@/pages/people/PeoplePage'
import { CompatibilityPage } from '@/pages/review/CompatibilityPage'
import { SettingsPage } from '@/pages/settings/SettingsPage'
import { SoulPage } from '@/pages/soul/SoulPage'
import { TagGraphPage } from '@/pages/tags/TagGraphPage'
import { TagsPage } from '@/pages/tags/TagsPage'
import { ExplorePage } from '@/pages/PlaceholderPage'

export type RouteGroup = 'overview' | 'data' | 'runtime' | 'cognition' | 'knowledge' | 'system'
export interface AppRoute { path: string; title: string; description: string; group: RouteGroup; icon: ComponentType<{ className?: string }>; element: ComponentType }

export const appRoutes: AppRoute[] = [
  { path: '/dashboard', title: '总览', description: '真实健康、待办与近期异常', group: 'overview', icon: GaugeIcon, element: DashboardPage },
  { path: '/explore', title: '神经云图', description: '3D 交互式高维记忆与关系星图', group: 'overview', icon: CompassIcon, element: ExplorePage },
  { path: '/memories', title: '记忆', description: '按 Bot 和群查看、编辑记忆', group: 'data', icon: DatabaseIcon, element: MemoriesPage },
  { path: '/tags', title: '标签', description: '覆盖率、频率、类型与置信度总览', group: 'data', icon: TagsIcon, element: TagsPage },
  { path: '/tags/graph', title: '标签关系图', description: '标签共现、来源与关联路径', group: 'data', icon: BrainCircuitIcon, element: TagGraphPage },
  { path: '/import', title: '导入', description: '从来源预检并导入记忆', group: 'data', icon: DownloadIcon, element: ImportPage },
  { path: '/maintenance', title: '维护任务', description: '可恢复任务、进度、日志与取消', group: 'runtime', icon: SlidersIcon, element: MaintenancePage },
  { path: '/observatory', title: '注入观测台', description: '查看一次回复用了哪些记忆通道', group: 'runtime', icon: ActivityIcon, element: InjectionPage },
  { path: '/channels', title: '通道配置', description: '各注入通道的保存值与当前生效值', group: 'runtime', icon: Settings2Icon, element: ChannelConfigPage },
  { path: '/beliefs', title: '信念', description: '证据健康与生命周期审核', group: 'cognition', icon: BookHeartIcon, element: BeliefsPage },
  { path: '/jargon', title: '黑话与口癖', description: '群聊习得黑话、证据审核与内置口癖资产', group: 'cognition', icon: SmileIcon, element: JargonPage },
  { path: '/soul', title: '心智状态', description: '当前群的心情、关切、时间线与关系', group: 'cognition', icon: HeartIcon, element: SoulPage },
  { path: '/knowledge/book-lore', title: '书设定', description: '独立只读语料、解析与本地化', group: 'knowledge', icon: BookOpenIcon, element: BookLorePage },
  { path: '/knowledge/experiences', title: '经历片段', description: '对话沉淀的个人经历与重要事件', group: 'knowledge', icon: SparklesIcon, element: ExperiencesPage },
  { path: '/knowledge/style-examples', title: '风格样例', description: '已通过审核的正式回复范例', group: 'knowledge', icon: BrainCircuitIcon, element: FewShotPage },
  { path: '/knowledge/facts', title: '事实', description: '人物/事物关系与证据', group: 'knowledge', icon: GitBranchIcon, element: FactsPage },
  { path: '/people', title: '人物与关系', description: '按 Bot、群和用户查看画像与好感', group: 'knowledge', icon: UsersIcon, element: PeoplePage },
  { path: '/diagnostics/indexes', title: '索引诊断', description: '检索索引来源、数量与健康状态', group: 'system', icon: SearchCheckIcon, element: IndexesPage },
  { path: '/compatibility', title: '生态兼容', description: '探测状态、来源、错误与证据', group: 'system', icon: GitCompareArrowsIcon, element: CompatibilityPage },
  { path: '/settings', title: '系统配置', description: '默认值、已保存值和当前生效方式', group: 'system', icon: SlidersIcon, element: SettingsPage },
]

export const defaultRoute = '/dashboard'
export function getRouteByPath(pathname: string): AppRoute { return appRoutes.find((route) => route.path === pathname) ?? appRoutes[0] }
