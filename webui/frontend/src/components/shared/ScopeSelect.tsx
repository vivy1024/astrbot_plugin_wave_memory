import { useCallback, useEffect, useId, useState } from 'react'
import { RefreshCwIcon } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import type { ScopeOption, ScopeOptionKind } from './types'
import { humanizeApiError } from '@/lib/reason-label'

const KIND_LABELS: Record<ScopeOptionKind, string> = {
  bot: 'Bot',
  session: '会话',
  channel: '通道',
}

export interface ScopeSelectProps {
  value?: string
  onValueChange: (value: string, option: ScopeOption) => void
  loadOptions: (signal: AbortSignal) => Promise<ScopeOption[]>
  label?: string
  description?: string
  placeholder?: string
  disabled?: boolean
  required?: boolean
  className?: string
}

export function ScopeSelect({
  value,
  onValueChange,
  loadOptions,
  label = '作用域',
  description = '',
  placeholder = '选择 Bot、会话或通道',
  disabled = false,
  required = false,
  className,
}: ScopeSelectProps) {
  const id = useId()
  const [options, setOptions] = useState<ScopeOption[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'empty' | 'error'>('loading')
  const [error, setError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setStatus('loading')
    setError('')
    loadOptions(controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted) return
        setOptions(loaded)
        setStatus(loaded.length > 0 ? 'ready' : 'empty')
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setOptions([])
        setError(humanizeApiError(reason, '作用域选项加载失败'))
        setStatus('error')
      })
    return () => controller.abort()
  }, [loadOptions, reloadKey])

  const selectOption = useCallback((nextValue: string) => {
    const option = options.find((item) => item.value === nextValue) ?? {
      value: nextValue,
      label: nextValue,
      kind: 'session' as const,
      description: '选项已失效或尚未加载完成',
    }
    onValueChange(nextValue, option)
  }, [onValueChange, options])

  const grouped = (Object.keys(KIND_LABELS) as ScopeOptionKind[])
    .map((kind) => ({ kind, items: options.filter((option) => option.kind === kind) }))
    .filter((group) => group.items.length > 0)
  const selectedOption = options.find((option) => option.value === value)

  return (
    <Field data-slot="scope-select" data-invalid={status === 'error'} className={cn('min-w-0', className)}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {status === 'loading' ? (
        <Skeleton className="h-8 w-full" aria-label="正在加载作用域选项" />
      ) : (
        <Select value={value ?? ''} onValueChange={selectOption} disabled={disabled || status !== 'ready'} required={required}>
          <SelectTrigger id={id} className="w-full min-w-0 overflow-hidden" aria-describedby={description ? `${id}-description ${id}-status` : `${id}-status`} aria-invalid={status === 'error'}>
            <SelectValue placeholder={status === 'empty' ? '当前授权范围内没有可用作用域' : placeholder}>
              {selectedOption?.label}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {grouped.map((group) => (
              <SelectGroup key={group.kind}>
                <SelectLabel>{KIND_LABELS[group.kind]}</SelectLabel>
                {group.items.map((option) => (
                  <SelectItem key={`${option.kind}:${option.value}`} value={option.value} disabled={option.disabled}>
                    <span className="flex min-w-0 flex-col">
                      <span>{option.label}</span>
                      {option.description ? <span className="text-sm text-muted-foreground">{option.description}</span> : null}
                    </span>
                  </SelectItem>
                ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
      )}
      {description ? <FieldDescription id={`${id}-description`}>{description}</FieldDescription> : null}
      <span id={`${id}-status`} className="sr-only" aria-live="polite">
        {status === 'loading' ? '正在加载作用域选项' : status === 'ready' ? `已加载 ${options.length} 个真实作用域选项` : status === 'empty' ? '当前授权范围内没有可用作用域' : '作用域选项加载失败'}
      </span>
      {status === 'empty' ? <p className="text-sm text-muted-foreground">暂无可选项，请先检查 Bot 与会话配置。</p> : null}
      {status === 'error' ? (
        <div className="flex flex-wrap items-center gap-2">
          <FieldError>{error}</FieldError>
          <Button type="button" variant="outline" size="sm" onClick={() => setReloadKey((key) => key + 1)}>
            <RefreshCwIcon data-icon="inline-start" />
            重试
          </Button>
        </div>
      ) : null}
    </Field>
  )
}
