import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLinkIcon, Link2OffIcon } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { buildObjectDeepLink } from '@/lib/object-deep-link'
import { cn } from '@/lib/utils'
import type { ObjectRefDescriptor } from './types'

export type ObjectRefState = 'ready' | 'not-found' | 'scope-mismatch' | 'version-stale'

export interface ObjectDeepLinkProps {
  to: string
  objectRef?: ObjectRefDescriptor | null
  state?: ObjectRefState
  tab?: string
  traceId?: string
  children?: ReactNode
  className?: string
  ariaLabel?: string
  replace?: boolean
}

const FAILURE_LABELS: Record<Exclude<ObjectRefState, 'ready'>, string> = {
  'not-found': '对象不存在或引用已失效',
  'scope-mismatch': '对象不属于当前授权作用域',
  'version-stale': '对象版本已更新，请刷新列表获取新引用',
}

export function ObjectDeepLink({
  to,
  objectRef,
  state = 'ready',
  tab,
  traceId,
  children = '打开对象',
  className,
  ariaLabel,
  replace = false,
}: ObjectDeepLinkProps) {
  if (state !== 'ready' || !objectRef?.ref.trim()) {
    const label = state === 'ready' ? '缺少服务端签发的对象引用' : FAILURE_LABELS[state]
    return (
      <span data-slot="object-deep-link" role="status" className={cn('inline-flex items-center gap-2 text-sm text-muted-foreground', className)}>
        <Link2OffIcon className="size-4" aria-hidden="true" />
        {label}；不会用裸编号或默认 Bot 猜测。
      </span>
    )
  }

  return (
    <Button asChild variant="outline" size="sm" className={className}>
      <Link data-slot="object-deep-link" to={buildObjectDeepLink(to, objectRef, tab, traceId)} replace={replace} aria-label={ariaLabel}>
        {children}
        <ExternalLinkIcon data-icon="inline-end" aria-hidden="true" />
      </Link>
    </Button>
  )
}
