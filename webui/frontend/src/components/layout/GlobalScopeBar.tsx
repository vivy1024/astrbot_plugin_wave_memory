import { useMemo } from 'react'
import { AlertTriangleIcon, BotIcon, Loader2Icon, MessagesSquareIcon, RefreshCwIcon } from 'lucide-react'

import { scopeOptionsFor } from '@/api/options'
import { useGlobalScope } from '@/app/global-scope'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { isLegacySessionId } from '@/lib/global-scope'
import { cn } from '@/lib/utils'

interface SessionChoice {
  value: string
  label: string
  group: 'group' | 'private' | 'legacy'
}

const GROUP_LABELS: Record<SessionChoice['group'], string> = {
  group: '群',
  private: '私聊',
  legacy: '未绑定的旧群',
}

/**
 * 顶栏里的全局 Bot / 群选择器：所有需要作用域的页面共用这一处，读写 URL 的
 * bot_id / session_id / visibility。不需要作用域的页面（level=none）不渲染。
 */
export function GlobalScopeBar({ className }: { className?: string }) {
  const scope = useGlobalScope()
  const { payload, level, allow } = scope

  const bots = useMemo(() => (payload ? scopeOptionsFor(payload, ['bot']) : []), [payload])
  const sessions = useMemo<SessionChoice[]>(() => {
    if (!payload || !scope.botId) return []
    const labels = new Map(scopeOptionsFor(payload, ['session']).map((option) => [option.value, option.label]))
    const choices: SessionChoice[] = []
    const groups = payload.sessions
      .filter((item) => item.bot_id === scope.botId && item.kind === 'group' && item.is_primary_alias !== false)
      .sort((left, right) => (right.count ?? 0) - (left.count ?? 0))
    for (const item of groups) choices.push({ value: item.id, label: labels.get(item.id) ?? item.id, group: 'group' })
    if (allow.private) {
      for (const item of payload.sessions.filter((entry) => entry.bot_id === scope.botId && entry.kind === 'private')) {
        choices.push({ value: item.id, label: labels.get(item.id) ?? item.id, group: 'private' })
      }
    }
    if (allow.legacy) {
      for (const group of (payload.legacy_groups ?? []).filter((entry) => entry.bot_id === scope.botId)) {
        const value = `legacy:${group.bot_id}:${group.group_id}`
        choices.push({ value, label: labels.get(value) ?? `${group.label || group.group_id}（未绑定）`, group: 'legacy' })
      }
    }
    // URL 里的会话不在列表时（例如旧链接）仍然显示，避免下拉显示空白。
    if (scope.sessionId && !choices.some((choice) => choice.value === scope.sessionId)) {
      choices.unshift({
        value: scope.sessionId,
        label: labels.get(scope.sessionId) ?? scope.sessionId,
        group: isLegacySessionId(scope.sessionId) ? 'legacy' : 'group',
      })
    }
    return choices
  }, [allow.legacy, allow.private, payload, scope.botId, scope.sessionId])

  if (level === 'none') return null

  if (scope.optionsStatus === 'error' && !payload) {
    return (
      <div className={cn('flex items-center gap-2 text-sm text-destructive', className)} role="alert">
        <AlertTriangleIcon className="size-4" aria-hidden="true" />
        <span className="hidden md:inline">{scope.optionsError || '作用域选项加载失败'}</span>
        <Button type="button" size="sm" variant="outline" onClick={scope.reloadOptions}>
          <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
          重试
        </Button>
      </div>
    )
  }

  if (!payload) {
    return (
      <div className={cn('flex items-center gap-2 text-sm text-muted-foreground', className)} role="status">
        <Loader2Icon className="size-4 animate-spin" aria-hidden="true" />
        <span className="hidden sm:inline">加载 Bot 与群…</span>
      </div>
    )
  }

  const grouped = (['group', 'private', 'legacy'] as const)
    .map((group) => ({ group, items: sessions.filter((choice) => choice.group === group) }))
    .filter((entry) => entry.items.length)

  return (
    <div className={cn('flex min-w-0 items-center gap-2', className)} data-slot="global-scope-bar">
      <Tooltip>
        <TooltipTrigger asChild>
          <div className="min-w-0">
            <Select value={scope.botId || undefined} onValueChange={(value) => scope.setScope({ botId: value })}>
              <SelectTrigger size="sm" className="w-24 sm:w-40" aria-label="当前 Bot">
                <BotIcon className="size-4 text-muted-foreground" aria-hidden="true" />
                <SelectValue placeholder="选择 Bot" />
              </SelectTrigger>
              <SelectContent>
                {bots.map((bot) => (
                  <SelectItem key={bot.value} value={bot.value} disabled={bot.disabled}>{bot.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </TooltipTrigger>
        <TooltipContent>当前 Bot（所有页面共用）</TooltipContent>
      </Tooltip>
      {level === 'session' || scope.sessionId ? (
        <Select
          value={scope.sessionId || undefined}
          onValueChange={(value) => scope.setScope({ sessionId: value })}
          disabled={!scope.botId || !sessions.length}
        >
          <SelectTrigger size="sm" className="w-36 min-w-0 sm:w-60" aria-label="当前群 / 会话">
            <MessagesSquareIcon className="size-4 text-muted-foreground" aria-hidden="true" />
            <SelectValue placeholder={sessions.length ? '选择群' : '该 Bot 暂无群'} />
          </SelectTrigger>
          <SelectContent>
            {grouped.map(({ group, items }) => (
              <SelectGroup key={group}>
                {grouped.length > 1 ? <SelectLabel>{GROUP_LABELS[group]}</SelectLabel> : null}
                {items.map((choice) => (
                  <SelectItem key={choice.value} value={choice.value}>{choice.label}</SelectItem>
                ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
      ) : null}
      {scope.status === 'resolving' ? <Loader2Icon className="size-4 animate-spin text-muted-foreground" aria-label="正在应用作用域" /> : null}
    </div>
  )
}
