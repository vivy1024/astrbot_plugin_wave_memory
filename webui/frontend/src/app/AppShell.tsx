import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'

import { appRoutes, defaultRoute } from '@/app/routes'
import { PageHeader } from '@/components/layout/PageHeader'
import { WaveSidebar } from '@/components/layout/WaveSidebar'
import { ScrollArea } from '@/components/ui/scroll-area'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { Toaster } from '@/components/ui/sonner'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

function RenamedPath({ to }: { to: string }) {
  const location = useLocation()
  return <Navigate replace to={{ pathname: to, search: location.search }} />
}

function NotFoundPage() {
  return <main className="mx-auto w-full max-w-2xl p-6"><Card><CardHeader><CardTitle>页面不存在</CardTitle><CardDescription>该地址已废弃或从未存在，不会猜测群或把旧编号转换成新链接。</CardDescription></CardHeader><CardContent>请从当前规范导航重新选择真实 Bot、会话和对象。</CardContent></Card></main>
}

export function AppShell() {
  return (
    <SidebarProvider>
      <WaveSidebar />
      <SidebarInset>
        <PageHeader />
        <ScrollArea className="h-[calc(100svh-3.5rem)]">
          <div className="mx-auto flex w-full max-w-7xl flex-col gap-6 p-4 md:p-6">
            <Outlet />
          </div>
        </ScrollArea>
      </SidebarInset>
      <Toaster richColors />
    </SidebarProvider>
  )
}

export function AppRoutes() {
  return (
    <Routes>
      {appRoutes.filter((route) => route.path === '/explore').map((route) => {
        const Element = route.element
        return <Route key={route.path} path={route.path} element={<Element />} />
      })}
      <Route element={<AppShell />}>
        <Route index element={<Navigate replace to={defaultRoute} />} />
        {appRoutes.filter((route) => route.path !== '/explore').map((route) => {
          const Element = route.element
          return <Route key={route.path} path={route.path} element={<Element />} />
        })}
        <Route path="/injection" element={<RenamedPath to="/observatory" />} />
        <Route path="/maintain" element={<RenamedPath to="/maintenance" />} />
        <Route path="/knowledge/fewshot" element={<RenamedPath to="/knowledge/style-examples" />} />
        <Route path="/login" element={<RenamedPath to={defaultRoute} />} />
      </Route>
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
