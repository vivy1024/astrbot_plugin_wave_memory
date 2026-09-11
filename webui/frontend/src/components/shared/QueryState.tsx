import type { ReactNode } from 'react'
import { AlertCircleIcon, CircleHelpIcon, InboxIcon, RefreshCwIcon } from 'lucide-react'

import { Alert, AlertAction, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { humanizeApiError } from '@/lib/reason-label'

export type QueryStatus = 'loading' | 'error' | 'empty' | 'unknown' | 'success'

export interface QueryStateProps {
  status: QueryStatus
  children?: ReactNode
  title?: string
  description?: string
  emptyTitle?: string
  emptyDescription?: string
  error?: unknown
  onRetry?: () => void
  loadingRows?: number
  className?: string
}

function errorMessage(error: unknown): string {
  return humanizeApiError(error, '请求失败，服务端没有返回可读错误信息。')
}

export function QueryState({
  status,
  children,
  title,
  description,
  emptyTitle,
  emptyDescription,
  error,
  onRetry,
  loadingRows = 3,
  className,
}: QueryStateProps) {
  if (status === 'success') return <>{children}</>

  if (status === 'loading') {
    return (
      <div data-slot="query-state" role="status" aria-live="polite" aria-label={title ?? '正在加载'} className={cn('flex flex-col gap-3 rounded-lg border p-4', className)}>
        <span className="sr-only">{title ?? '正在加载真实数据'}</span>
        {Array.from({ length: loadingRows }, (_, index) => <Skeleton key={index} className="h-10 w-full" />)}
      </div>
    )
  }

  if (status === 'error') {
    // error 与 description 同时存在时两者都要显示：description 是固定说明，
    // 真实失败原因不能被它顶掉，否则用户永远看不到服务端返回了什么。
    const hasError = error !== undefined && error !== null
    const failure = hasError ? errorMessage(error) : ''
    const note = description && description !== failure ? description : ''
    const detail = failure || note ? [failure, note].filter(Boolean) : [errorMessage(error)]
    return (
      <Alert data-slot="query-state" variant="destructive" className={className}>
        <AlertCircleIcon />
        <AlertTitle>{title ?? '加载失败'}</AlertTitle>
        <AlertDescription>
          {detail.map((line, index) => (
            <span key={index} className={index > 0 ? 'text-muted-foreground' : undefined}>
              {index > 0 ? ' ' : null}{line}
            </span>
          ))}
        </AlertDescription>
        {onRetry ? (
          <AlertAction>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>
              <RefreshCwIcon data-icon="inline-start" />
              重试
            </Button>
          </AlertAction>
        ) : null}
      </Alert>
    )
  }

  if (status === 'unknown') {
    return (
      <Alert data-slot="query-state" className={className}>
        <CircleHelpIcon />
        <AlertTitle>{title ?? '状态未知'}</AlertTitle>
        <AlertDescription>{description ?? '服务端无法确认当前状态；这不等同于空数据或成功。'}</AlertDescription>
        {onRetry ? (
          <AlertAction><Button type="button" variant="outline" size="sm" onClick={onRetry}>重新检查</Button></AlertAction>
        ) : null}
      </Alert>
    )
  }

  return (
    <Alert data-slot="query-state" className={className}>
      <InboxIcon />
      <AlertTitle>{emptyTitle ?? '暂无记录'}</AlertTitle>
      <AlertDescription>{emptyDescription ?? description ?? '当前筛选条件下没有匹配的数据。'}</AlertDescription>
    </Alert>
  )
}
