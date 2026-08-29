import type { FormEvent, ReactNode } from 'react'
import { SearchIcon } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ScopeSelect } from './ScopeSelect'
import type { ScopeOption } from './types'

export interface ScopeFilterBarProps {
  botId?: string | null
  sessionId?: string | null
  loadBots: (signal: AbortSignal) => Promise<ScopeOption[]>
  loadSessions: (signal: AbortSignal) => Promise<ScopeOption[]>
  onBotChange: (botId: string) => void
  onSessionChange: (sessionId: string) => void
  searchValue?: string
  onSearchChange?: (value: string) => void
  searchPlaceholder?: string
  onSubmit?: (e: FormEvent) => void
  onReset?: () => void
  children?: ReactNode
  actions?: ReactNode
}

/**
 * 统一的 Scope + 搜索筛选栏组件
 * 封装 Bot / Session 级联选择、搜索输入框及各类辅助筛选下拉
 */
export function ScopeFilterBar({
  botId,
  sessionId,
  loadBots,
  loadSessions,
  onBotChange,
  onSessionChange,
  searchValue,
  onSearchChange,
  searchPlaceholder = '输入关键词搜索…',
  onSubmit,
  onReset,
  children,
  actions,
}: ScopeFilterBarProps) {
  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    onSubmit?.(e)
  }

  return (
    <Card className="border-border/60">
      <CardContent className="p-4">
        <form className="flex flex-wrap items-end gap-2" onSubmit={handleSubmit}>
          <ScopeSelect
            className="w-48 shrink-0 [&_[data-slot=field-label]]:sr-only"
            value={botId || undefined}
            loadOptions={loadBots}
            label="Bot"
            placeholder="选择 Bot"
            onValueChange={(val) => onBotChange(val)}
          />
          <ScopeSelect
            className="w-56 shrink-0 [&_[data-slot=field-label]]:sr-only"
            value={sessionId || undefined}
            loadOptions={loadSessions}
            label="群 / 会话"
            placeholder="选择真实群会话"
            disabled={!botId}
            onValueChange={(val) => onSessionChange(val)}
          />
          {children}
          {onSearchChange !== undefined ? (
            <Field className="min-w-52 flex-1 gap-0 [&_[data-slot=field-label]]:sr-only">
              <FieldLabel htmlFor="filter-search">搜索</FieldLabel>
              <Input
                id="filter-search"
                value={searchValue ?? ''}
                placeholder={searchPlaceholder}
                onChange={(e) => onSearchChange(e.target.value)}
              />
            </Field>
          ) : null}
          {onSubmit ? (
            <Button type="submit" size="sm">
              <SearchIcon data-icon="inline-start" />
              搜索
            </Button>
          ) : null}
          {onReset ? (
            <Button type="button" variant="outline" size="sm" onClick={onReset}>
              重置
            </Button>
          ) : null}
          {actions}
        </form>
      </CardContent>
    </Card>
  )
}
