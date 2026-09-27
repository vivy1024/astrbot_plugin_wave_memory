import { LogOutIcon } from 'lucide-react'
import { useLocation } from 'react-router-dom'

import { useAuth } from '@/app/auth-context'
import { getRouteByPath } from '@/app/routes'
import { useUnsavedChangesConfirm } from '@/app/unsaved-changes'
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { GlobalScopeBar } from './GlobalScopeBar'

export function PageHeader() {
  const location = useLocation()
  const { state, logout } = useAuth()
  const confirmAction = useUnsavedChangesConfirm()
  const route = getRouteByPath(location.pathname)
  const showLogout = state.status === 'ready' && state.requiresAuth

  return (
    <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <SidebarTrigger />
      <Separator orientation="vertical" className="mr-2 hidden h-4 sm:block" />
      <Breadcrumb className="hidden min-w-0 sm:block">
        <BreadcrumbList>
          <BreadcrumbItem>
            <span>Wave Memory</span>
          </BreadcrumbItem>
          <BreadcrumbSeparator />
          <BreadcrumbItem>
            <BreadcrumbPage>{route.title}</BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>
      <div className="ml-auto flex min-w-0 flex-1 items-center justify-end gap-2">
      <GlobalScopeBar />
      {showLogout ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label="退出登录"
          onClick={() => confirmAction(logout, {
            message: '当前页面有未保存修改，退出登录后这些草稿将丢失。',
            confirmLabel: '放弃修改并退出登录',
          })}
        >
          <LogOutIcon aria-hidden="true" />
          <span className="hidden sm:inline">退出登录</span>
          <span className="sr-only sm:hidden">退出登录</span>
        </Button>
      ) : null}
      </div>
    </header>
  )
}
