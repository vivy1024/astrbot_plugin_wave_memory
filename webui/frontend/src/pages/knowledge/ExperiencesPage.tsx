import { useCallback, useEffect, useState } from 'react'
import { BookOpenIcon, HeartIcon, MessageSquareIcon, RefreshCwIcon, SearchIcon, UserIcon } from 'lucide-react'

import { listExperiences, type ExperienceEpisode } from '@/api/experiences'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import { ScopeSelect } from '@/components/shared'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'

const EPISODE_TYPE_LABELS: Record<string, string> = {
  user_message: '用户消息',
  bot_reply: 'Bot 回复',
  correction: '被纠正',
  proactive: '主动发起',
}

export function ExperiencesPage() {
  const pagination = usePaginationSearchParams()
  const botId = pagination.searchParams.get('bot_id') ?? ''
  const sessionId = pagination.searchParams.get('session_id') ?? ''
  const groupId = sessionId.startsWith('legacy:')
    ? sessionId.split(':').slice(2).join(':')
    : sessionId.split(':').slice(2).join(':')
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters, enabled: !sessionId.startsWith('legacy:') })
  const [items, setItems] = useState<ExperienceEpisode[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [minWeight, setMinWeight] = useState('all')
  const [page, setPage] = useState(1)
  const limit = 18
  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId), [botId])

  const loadData = useCallback(async () => {
    if (!botId || !groupId) {
      setItems([])
      setTotal(0)
      setLoading(false)
      setError(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const weightVal = minWeight === 'high' ? 0.4 : minWeight === 'medium' ? 0.3 : undefined
      const res = await listExperiences({
        bot_id: botId,
        group_id: groupId,
        search: search.trim() || undefined,
        min_emotional_weight: weightVal,
        limit,
        offset: (page - 1) * limit,
      })
      setItems(res.items ?? [])
      setTotal(res.page?.total ?? res.items?.length ?? 0)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载历史经历失败')
      setItems([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [botId, groupId, minWeight, page, search])

  useEffect(() => {
    void loadData()
  }, [loadData])

  const totalPages = Math.max(1, Math.ceil(total / limit))

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">经历片段</h1>
          <p className="text-sm text-muted-foreground">
            按当前 Bot 与群查看经历片段：触发内容、内心活动、实际回复与用户反馈（共 {total} 条）
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadData()} disabled={loading}>
          <RefreshCwIcon className={loading ? 'animate-spin' : undefined} data-icon="inline-start" aria-hidden="true" />
          刷新
        </Button>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <ScopeSelect className="min-w-48" value={botId || undefined} loadOptions={loadBots} label="Bot" placeholder="选择 Bot" onValueChange={(value) => { pagination.setFilters({ bot_id: value, session_id: null }); setPage(1) }} />
        <ScopeSelect className="min-w-56" value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" placeholder="选择群会话" disabled={!botId} onValueChange={(value) => { pagination.setFilters({ session_id: value }); setPage(1) }} />
        <div className="relative min-w-[220px] flex-1 sm:max-w-md">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" aria-hidden="true" />
          <Input
            placeholder="搜索触发内容、回复或反馈…"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value)
              setPage(1)
            }}
            className="pl-8"
          />
        </div>
        <Select
          value={minWeight}
          onValueChange={(value) => {
            setMinWeight(value)
            setPage(1)
          }}
        >
          <SelectTrigger className="w-[180px]">
            <SelectValue placeholder="情感权重" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部情感权重</SelectItem>
            <SelectItem value="high">较高 ≥ 0.4</SelectItem>
            <SelectItem value="medium">中等 ≥ 0.3</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {error ? (
        <Card className="border-destructive/60">
          <CardContent className="py-6 text-center text-sm text-destructive">{error}</CardContent>
        </Card>
      ) : null}

      {!botId || !groupId ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">请先选择 Bot 和群，不会加载全部 Bot 的经历。</CardContent>
        </Card>
      ) : loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Card key={index}>
              <CardHeader className="pb-2"><Skeleton className="h-5 w-3/4" /></CardHeader>
              <CardContent className="flex flex-col gap-2">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
                <Skeleton className="h-4 w-2/3" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : items.length === 0 && !error ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
            <BookOpenIcon className="size-10 text-muted-foreground/60" aria-hidden="true" />
            <p className="text-base font-medium">没有匹配的经历片段</p>
            <p className="text-xs text-muted-foreground">可调整搜索词或重置情感权重筛选。</p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((item) => (
            <Card key={item.id} className="flex flex-col transition-colors hover:border-primary/50">
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <CardTitle className="text-sm font-semibold leading-snug">
                    {EPISODE_TYPE_LABELS[item.episode_type ?? ''] ?? item.episode_type ?? `片段 #${item.id}`}
                  </CardTitle>
                  {typeof item.emotional_weight === 'number' ? (
                    <Badge variant={item.emotional_weight >= 0.4 ? 'default' : 'secondary'} className="shrink-0">
                      <HeartIcon data-icon="inline-start" aria-hidden="true" />
                      {item.emotional_weight.toFixed(2)}
                    </Badge>
                  ) : null}
                </div>
                <CardDescription className="text-xs">
                  {item.bot_id ? `bot ${item.bot_id}` : '未记录 bot'}
                  {item.group_id ? ` · 群 ${item.group_id}` : ''}
                  {item.outcome ? ` · ${item.outcome}` : ''}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-1 flex-col gap-3 pt-0 text-xs">
                {item.trigger_text ? (
                  <div className="rounded-md border bg-muted/20 p-2">
                    <p className="mb-1 font-medium text-muted-foreground">触发</p>
                    <p className="line-clamp-2 leading-relaxed">{item.trigger_text}</p>
                  </div>
                ) : null}
                {item.bot_inner_thought ? (
                  <div className="rounded-md border bg-muted/20 p-2">
                    <p className="mb-1 font-medium text-muted-foreground">内心活动</p>
                    <p className="line-clamp-2 leading-relaxed">{item.bot_inner_thought}</p>
                  </div>
                ) : null}
                {item.bot_reply ? (
                  <div className="rounded-md border border-primary/20 bg-primary/[0.04] p-2">
                    <p className="mb-1 flex items-center gap-1 font-medium text-muted-foreground">
                      <MessageSquareIcon className="size-3" aria-hidden="true" />回复
                    </p>
                    <p className="line-clamp-3 leading-relaxed">{item.bot_reply}</p>
                  </div>
                ) : null}
                {item.user_reaction ? (
                  <div className="rounded-md border bg-muted/20 p-2">
                    <p className="mb-1 font-medium text-muted-foreground">用户反馈</p>
                    <p className="line-clamp-2 leading-relaxed">{item.user_reaction}</p>
                  </div>
                ) : null}
                <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-border/40 pt-2 text-[11px] text-muted-foreground">
                  {item.user_id ? (
                    <span className="flex items-center gap-1 truncate">
                      <UserIcon className="size-3" aria-hidden="true" />{item.user_id}
                    </span>
                  ) : <span>{item.group_id ?? '未记录会话'}</span>}
                  {item.created_at ? <span>{formatTime(item.created_at)}</span> : null}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {totalPages > 1 ? (
        <div className="flex items-center justify-between border-t pt-4">
          <p className="text-xs text-muted-foreground">
            第 {(page - 1) * limit + 1} – {Math.min(page * limit, total)} 条，共 {total} 条
          </p>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={page <= 1 || loading} onClick={() => setPage((value) => Math.max(1, value - 1))}>
              上一页
            </Button>
            <span className="px-2 text-xs font-medium">{page} / {totalPages}</span>
            <Button variant="outline" size="sm" disabled={page >= totalPages || loading} onClick={() => setPage((value) => Math.min(totalPages, value + 1))}>
              下一页
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function formatTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}
