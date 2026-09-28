import { lazy, Suspense } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'

import { GlobalScopeProvider } from '@/app/global-scope'
import { appRoutes, defaultRoute } from '@/app/routes'
import { PageHeader } from '@/components/layout/PageHeader'
import { WaveSidebar } from '@/components/layout/WaveSidebar'
import { ScrollArea } from '@/components/ui/scroll-area'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { Skeleton } from '@/components/ui/skeleton'
import { Toaster } from '@/components/ui/sonner'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

// 直播舞台页：不带侧边栏与顶栏，直接给 OBS 浏览器源用
const StagePage = lazy(() => import('@/pages/stage/StagePage').then((m) => ({ default: m.StagePage })))

function PageFallback() {
  return (
    <div className="flex flex-col gap-4 p-4" data-slot="page-fallback">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-32 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  )
}

function RenamedPath({ to, set, drop = [] }: { to: string; set?: Record<string, string>; drop?: string[] }) {
  const location = useLocation()
  if (!set && !drop.length) return <Navigate replace to={{ pathname: to, search: location.search }} />
  const params = new URLSearchParams(location.search)
  for (const key of drop) params.delete(key)
  for (const [key, value] of Object.entries(set ?? {})) params.set(key, value)
  return <Navigate replace to={{ pathname: to, search: `?${params.toString()}` }} />
}

function NotFoundPage() {
  return <main className="mx-auto w-full max-w-2xl p-6"><Card><CardHeader><CardTitle>页面不存在</CardTitle><CardDescription>该地址已废弃或从未存在，不会猜测群或把旧编号转换成新链接。</CardDescription></CardHeader><CardContent>请从当前规范导航重新选择真实 Bot、会话和对象。</CardContent></Card></main>
}

export function AppShell() {
  return (
    <GlobalScopeProvider>
    <SidebarProvider>
      <WaveSidebar />
      <SidebarInset>
        <PageHeader />
        <ScrollArea className="h-[calc(100svh-3.5rem)]">
          <div className="mx-auto flex w-full max-w-7xl flex-col gap-6 p-4 md:p-6">
            <Suspense fallback={<PageFallback />}>
              <Outlet />
            </Suspense>
          </div>
        </ScrollArea>
      </SidebarInset>
      <Toaster richColors />
    </SidebarProvider>
    </GlobalScopeProvider>
  )
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate replace to={defaultRoute} />} />
        {appRoutes.map((route) => {
          const Element = route.element
          return <Route key={route.path} path={route.path} element={<Element />} />
        })}
        <Route path="/injection" element={<RenamedPath to="/observatory" />} />
        <Route path="/maintain" element={<RenamedPath to="/maintenance" />} />
        <Route path="/knowledge/fewshot" element={<RenamedPath to="/knowledge/style-examples" />} />
        <Route path="/knowledge/facts" element={<RenamedPath to="/facts" />} />
        {/* 两个旧「神经云图」合并为关系图谱：保留原查询参数，只补上数据层 */}
        <Route path="/tags/graph" element={<RenamedPath to="/graph" set={{ layer: 'tags' }} />} />
        <Route path="/explore" element={<RenamedPath to="/graph" set={{ layer: 'kg' }} drop={['embed']} />} />
        <Route path="/login" element={<RenamedPath to={defaultRoute} />} />
      </Route>
      <Route path="/stage" element={<Suspense fallback={<div className="h-svh bg-black" />}><StagePage /></Suspense>} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
