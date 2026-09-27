import { RefreshCwIcon } from 'lucide-react'
import { HashRouter } from 'react-router-dom'

import { AuthProvider } from '@/app/auth-provider'
import { useAuth } from '@/app/auth-context'
import { AppRoutes } from '@/app/AppShell'
import { UnsavedChangesProvider } from '@/app/unsaved-changes'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { ThemeProvider } from '@/components/theme/theme-provider'
import { Skeleton } from '@/components/ui/skeleton'
import { TooltipProvider } from '@/components/ui/tooltip'
import { LoginPage } from '@/pages/LoginPage'

export function AuthGate() {
  const { state, refresh } = useAuth()

  if (state.status === 'checking') {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background p-6" role="status" aria-label="正在检查认证状态">
        <div className="flex w-full max-w-md flex-col gap-3">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      </main>
    )
  }

  if (state.status === 'error') {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background p-6">
        <div className="flex w-full max-w-md flex-col gap-4">
          <Alert variant="destructive">
            <AlertTitle>无法确认认证状态</AlertTitle>
            <AlertDescription>{state.message}</AlertDescription>
          </Alert>
          <Button type="button" onClick={() => void refresh()}>
            <RefreshCwIcon aria-hidden="true" />
            重试认证检查
          </Button>
        </div>
      </main>
    )
  }

  if (state.status === 'anonymous') {
    return <LoginPage />
  }

  return <AppRoutes />
}

export default function App() {
  return (
    <ThemeProvider>
      <TooltipProvider>
        <HashRouter>
          <UnsavedChangesProvider>
            <AuthProvider>
              <AuthGate />
            </AuthProvider>
          </UnsavedChangesProvider>
        </HashRouter>
      </TooltipProvider>
    </ThemeProvider>
  )
}
