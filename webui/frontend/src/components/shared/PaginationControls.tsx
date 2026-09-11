import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ChevronsLeftIcon, ChevronsRightIcon, ChevronLeftIcon, ChevronRightIcon } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { humanizeReason } from '@/lib/reason-label'
import { PAGE_SIZE_OPTIONS, type PageMetadata, type PageSize } from './types'

type FocusTarget = 'first' | 'previous' | 'next' | 'last' | 'jump'

export interface PaginationControlsProps {
  page: PageMetadata
  onOffsetChange: (offset: number) => void
  onLimitChange: (limit: PageSize) => void
  disabled?: boolean
  className?: string
  label?: string
}

export function PaginationControls({
  page,
  onOffsetChange,
  onLimitChange,
  disabled = false,
  className,
  label = '分页导航',
}: PaginationControlsProps) {
  const id = useId()
  const [jumpPage, setJumpPage] = useState(String(page.page))
  const focusTarget = useRef<FocusTarget | null>(null)
  const controls = useRef<Partial<Record<FocusTarget, HTMLElement>>>({})
  const previousOffset = useRef(page.offset)

  useEffect(() => {
    setJumpPage(String(page.page))
    if (previousOffset.current !== page.offset && focusTarget.current) {
      controls.current[focusTarget.current]?.focus()
      focusTarget.current = null
    }
    previousOffset.current = page.offset
  }, [page.offset, page.page])

  const canGoBack = !disabled && page.offset > 0
  const canGoForward = !disabled && page.has_more
  const canGoLast = canGoForward && page.page_count !== null

  const requestOffset = (target: FocusTarget, offset: number) => {
    focusTarget.current = target
    onOffsetChange(Math.max(0, offset))
  }

  const submitJump = (event: FormEvent) => {
    event.preventDefault()
    const parsed = Number(jumpPage)
    if (!Number.isSafeInteger(parsed) || parsed < 1) {
      setJumpPage(String(page.page))
      return
    }
    const bounded = page.page_count === null ? parsed : Math.min(parsed, page.page_count)
    requestOffset('jump', (bounded - 1) * page.limit)
  }

  const unavailableReason = page.reason_code ? humanizeReason(page.reason_code) : ''
  const statusText =
    page.total_status === 'exact' && page.total !== null
      ? `第 ${page.page} 页${page.page_count === null ? '' : `，共 ${page.page_count} 页`}，共 ${page.total} 条`
      : `第 ${page.page} 页，总数暂不可用${unavailableReason ? `：${unavailableReason}` : ''}`

  return (
    <nav data-slot="pagination-controls" aria-label={label} className={cn(        'flex flex-col gap-3 border-t border-border/70 pt-3 text-sm sm:flex-row sm:items-center sm:justify-between', className)}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">每页</span>
        <Select value={String(page.limit)} onValueChange={(value) => onLimitChange(Number(value) as PageSize)} disabled={disabled}>
          <SelectTrigger aria-label="每页条数" className="w-20">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {PAGE_SIZE_OPTIONS.map((size) => (
                <SelectItem key={size} value={String(size)}>{size}</SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        <span aria-live="polite" aria-atomic="true" className="text-muted-foreground">{statusText}</span>
      </div>

      <div className="flex flex-wrap items-center gap-1">
        <Button
          ref={(node) => { controls.current.first = node ?? undefined }}
          type="button"
          variant="outline"
          size="icon-sm"
          aria-label="第一页"
          disabled={!canGoBack}
          onClick={() => requestOffset('first', 0)}
        >
          <ChevronsLeftIcon data-icon="inline-start" aria-hidden="true" />
        </Button>
        <Button
          ref={(node) => { controls.current.previous = node ?? undefined }}
          type="button"
          variant="outline"
          size="icon-sm"
          aria-label="上一页"
          disabled={!canGoBack}
          onClick={() => requestOffset('previous', page.offset - page.limit)}
        >
          <ChevronLeftIcon data-icon="inline-start" aria-hidden="true" />
        </Button>
        <form className="flex items-center gap-1" onSubmit={submitJump}>
          <Field orientation="horizontal" className="w-auto items-center gap-1">
            <FieldLabel htmlFor={`${id}-jump`} className="sr-only">跳转页码</FieldLabel>
            <Input
              ref={(node) => { controls.current.jump = node ?? undefined }}
              id={`${id}-jump`}
              type="number"
              inputMode="numeric"
              min={1}
              max={page.page_count ?? undefined}
              value={jumpPage}
              onChange={(event) => setJumpPage(event.target.value)}
              className="h-8 w-16"
              disabled={disabled}
            />
          </Field>
          <Button type="submit" variant="outline" size="sm" disabled={disabled}>跳转</Button>
        </form>
        <Button
          ref={(node) => { controls.current.next = node ?? undefined }}
          type="button"
          variant="outline"
          size="icon-sm"
          aria-label="下一页"
          disabled={!canGoForward}
          onClick={() => requestOffset('next', page.offset + page.limit)}
        >
          <ChevronRightIcon data-icon="inline-start" aria-hidden="true" />
        </Button>
        <Button
          ref={(node) => { controls.current.last = node ?? undefined }}
          type="button"
          variant="outline"
          size="icon-sm"
          aria-label="最后一页"
          disabled={!canGoLast}
          onClick={() => requestOffset('last', ((page.page_count ?? 1) - 1) * page.limit)}
        >
          <ChevronsRightIcon data-icon="inline-start" aria-hidden="true" />
        </Button>
      </div>
    </nav>
  )
}
