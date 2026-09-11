import { MoonStarIcon, WavesIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'

import { getSystemStatus } from '@/api/system'
import { appRoutes, type RouteGroup } from '@/app/routes'
import { Badge } from '@/components/ui/badge'
import { Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader, SidebarMenu, SidebarMenuButton, SidebarMenuItem } from '@/components/ui/sidebar'
import { sharedScopeSearch } from '@/lib/navigation-search'

const groups: Array<{ id: RouteGroup; label: string }> = [
  { id: 'overview', label: '总览' },
  { id: 'data', label: '数据' },
  { id: 'runtime', label: '运行' },
  { id: 'cognition', label: '认知与学习' },
  { id: 'knowledge', label: '知识与关系' },
  { id: 'system', label: '系统' },
]

/**
 * 版本号来自后端读取的 metadata.yaml。这里刻意不写死常量：
 * 写死会在发版后立刻过期（此前就出现过显示 v1、实际 v5.0.0 的情况）。
 * 取不到时不显示版本徽章，而不是编造一个。
 */
function usePluginVersion(): string | null {
  const [version, setVersion] = useState<string | null>(null)
  useEffect(() => {
    let active = true
    getSystemStatus()
      .then((payload) => {
        if (!active) return
        const value = String(payload?.plugin_version ?? '').trim()
        setVersion(value || null)
      })
      .catch(() => {
        // 版本号只是装饰性信息，读取失败不应干扰侧栏导航。
      })
    return () => { active = false }
  }, [])
  return version
}

export function WaveSidebar() {
  const location = useLocation()
  const navigationSearch = sharedScopeSearch(location.search)
  const version = usePluginVersion()
  return <Sidebar collapsible="icon" variant="inset"><SidebarHeader><SidebarMenu><SidebarMenuItem><SidebarMenuButton asChild size="lg" tooltip="Wave Memory"><NavLink to={{ pathname: '/dashboard', search: navigationSearch }}><WavesIcon aria-hidden="true" /><span className="flex flex-col gap-0.5"><span className="font-semibold">Wave Memory</span><span className="text-sm text-muted-foreground">WebUI 控制台</span></span></NavLink></SidebarMenuButton></SidebarMenuItem></SidebarMenu></SidebarHeader><SidebarContent>
    {groups.map((group) => <SidebarGroup key={group.id}><SidebarGroupLabel>{group.label}</SidebarGroupLabel><SidebarGroupContent><SidebarMenu>{appRoutes.filter((route) => route.group === group.id).map((route) => { const Icon = route.icon; return <SidebarMenuItem key={route.path}><SidebarMenuButton asChild isActive={location.pathname === route.path || location.pathname.startsWith(`${route.path}/`)} tooltip={route.title}><NavLink to={{ pathname: route.path, search: navigationSearch }}><Icon aria-hidden="true" /><span>{route.title}</span></NavLink></SidebarMenuButton></SidebarMenuItem> })}</SidebarMenu></SidebarGroupContent></SidebarGroup>)}
  </SidebarContent><SidebarFooter><div className="flex items-center gap-2 px-2 py-1 text-sm text-muted-foreground"><MoonStarIcon className="size-4" aria-hidden="true" /><span>shadcn · Nova</span>{version ? <Badge variant="secondary">{version}</Badge> : null}</div></SidebarFooter></Sidebar>
}
