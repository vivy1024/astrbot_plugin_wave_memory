import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { humanizeApiError } from '@/lib/reason-label'
import { BookOpenIcon, HeartIcon, MessageSquareIcon, SparklesIcon, UserIcon, UsersIcon } from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { listExperiences, type ExperienceEpisode } from '@/api/experiences'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import { HeroHeader, ScopeFilterBar } from '@/components/shared'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { scopedHref } from '@/lib/navigation-search'

const EPISODE_TYPE_LABELS: Record<string, string> = {
  user_message: '用户消息',
  bot_reply: 'Bot 回复',
  correction: '被纠正',
  proactive: '主动发起',
  shared_event: '共同经历',
  shared_problem_solving: '共同解决问题',
  group_turning_point: '群事件转折',
  daily_diary: '每日日记',
}

function sourceMemoryIds(value: ExperienceEpisode['source_memory_ids']): string[] {
  if (Array.isArray(value)) return value.map(String).filter(Boolean)
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean)
    } catch { /* 兼容旧数据 */ }
    return value.split(',').map((item) => item.trim()).filter(Boolean)
  }
  return []
}

export function ExperiencesPage() {
  const pagination = usePaginationSearchParams()
  const botId = pagination.searchParams.get('bot_id') ?? ''
  const sessionId = pagination.searchParams.get('session_id') ?? ''
  const searchParam = pagination.searchParams.get('search') ?? ''
  const episodeTypeParam = pagination.searchParams.get('episode_type') ?? ''
  const groupId = sessionId.startsWith('legacy:')
    ? sessionId.split(':').slice(2).join(':')
    : sessionId.split(':').slice(2).join(':')
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters, enabled: !sessionId.startsWith('legacy:') })

  const [items, setItems] = useState<ExperienceEpisode[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchDraft, setSearchDraft] = useState(searchParam)
  const [episodeTypeDraft, setEpisodeTypeDraft] = useState(episodeTypeParam)
  const [minWeight, setMinWeight] = useState('all')
  const [page, setPage] = useState(1)
  const limit = 18

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId), [botId])

  const requestRef = useRef<AbortController | null>(null)

  const loadData = useCallback(async (signal?: AbortSignal) => {
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
        search: searchParam.trim() || undefined,
        episode_type: episodeTypeParam || undefined,
        min_emotional_weight: weightVal,
        limit,
        offset: (page - 1) * limit,
        signal,
      })
      if (signal?.aborted) return
      setItems(res.items ?? [])
      setTotal(res.page?.total ?? res.items?.length ?? 0)
    } catch (err) {
      if (signal?.aborted || isRequestCancelled(err)) return
      setError(humanizeApiError(err, '加载历史经历失败'))
      setItems([])
      setTotal(0)
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [botId, episodeTypeParam, groupId, minWeight, page, searchParam])

  useEffect(() => {
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    void loadData(controller.signal)
    return () => controller.abort()
  }, [loadData])

  useEffect(() => {
    setSearchDraft(searchParam)
  }, [searchParam])

  const handleSearchSubmit = (e: FormEvent) => {
    e.preventDefault()
    pagination.setFilters({
      search: searchDraft.trim() || null,
      episode_type: episodeTypeDraft || null,
    })
    setPage(1)
  }

  const handleReset = () => {
    setSearchDraft('')
    setEpisodeTypeDraft('')
    setMinWeight('all')
    pagination.setFilters({ search: null, episode_type: null })
    setPage(1)
  }

  const totalPages = Math.max(1, Math.ceil(total / limit))
  const diaryCount = items.filter((i) => i.episode_type === 'daily_diary').length
  const weightedCount = items.filter((i) => (i.emotional_weight ?? 0) >= 0.4).length
  const anchoredCount = items.filter((i) => sourceMemoryIds(i.source_memory_ids).length > 0).length

  return (
    <div className="flex flex-col gap-6" data-page="experiences" data-slot="experiences-page">
      <HeroHeader
        icon={<BookOpenIcon className="size-6" />}
        title="经历片段与群聊日记"
        description={`结构化群经历与每日日记（共 ${total} 条）。日记会进入 Bot 经历时间线；群友印象请到印象时间线查看。`}
        badge={
          <div className="flex flex-wrap items-center gap-2">
            <Button asChild variant="outline" size="sm">
              <Link to={scopedHref('/soul', pagination.searchParams.toString())}>
                <SparklesIcon data-icon="inline-start" aria-hidden="true" />
                Bot 经历时间线
              </Link>
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to={scopedHref('/people', pagination.searchParams.toString())}>
                <UsersIcon data-icon="inline-start" aria-hidden="true" />
                群友印象时间线
              </Link>
            </Button>
          </div>
        }
        metrics={[
          { label: '匹配总数', value: loading ? '…' : total },
          { label: '本页日记', value: loading ? '…' : diaryCount, tone: 'text-amber-600' },
          { label: '重要经历', value: loading ? '…' : weightedCount, tone: 'text-primary' },
          { label: '已溯源记忆', value: loading ? '…' : anchoredCount, tone: 'text-emerald-600' },
        ]}
      />

      <ScopeFilterBar
        botId={botId}
        sessionId={sessionId}
        loadBots={loadBots}
        loadSessions={loadSessions}
        onBotChange={(val) => {
          pagination.setFilters({ bot_id: val, session_id: null })
          setPage(1)
        }}
        onSessionChange={(val) => {
          pagination.setFilters({ session_id: val })
          setPage(1)
        }}
        searchValue={searchDraft}
        onSearchChange={setSearchDraft}
        searchPlaceholder="搜索触发内容、回复或日记正文…"
        onSubmit={handleSearchSubmit}
        onReset={handleReset}
      >
        <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
          <FieldLabel htmlFor="experience-type-filter">经历类型</FieldLabel>
          <Select
            value={episodeTypeDraft || 'all'}
            onValueChange={(val) => {
              setEpisodeTypeDraft(val === 'all' ? '' : val)
            }}
          >
            <SelectTrigger id="experience-type-filter" className="w-full">
              <SelectValue placeholder="经历类型" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部类型</SelectItem>
              <SelectItem value="daily_diary">📖 每日日记</SelectItem>
              <SelectItem value="shared_event">🤝 共同经历</SelectItem>
              <SelectItem value="shared_problem_solving">🧩 解决问题</SelectItem>
              <SelectItem value="group_turning_point">🔄 关系转折</SelectItem>
              <SelectItem value="bot_reply">💬 Bot 回复</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
          <FieldLabel htmlFor="experience-weight-filter">情感权重</FieldLabel>
          <Select
            value={minWeight}
            onValueChange={(val) => {
              setMinWeight(val)
              setPage(1)
            }}
          >
            <SelectTrigger id="experience-weight-filter" className="w-full">
              <SelectValue placeholder="情感权重" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部情感</SelectItem>
              <SelectItem value="high">高分 ≥ 0.4</SelectItem>
              <SelectItem value="medium">中等 ≥ 0.3</SelectItem>
            </SelectContent>
          </Select>
        </Field>
      </ScopeFilterBar>

      {error ? (
        <Card className="border-destructive/60">
          <CardContent className="py-6 text-center text-sm text-destructive">{error}</CardContent>
        </Card>
      ) : null}

      {!botId || !groupId ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            请先选择 Bot 和群，不会加载全部 Bot 的经历。
          </CardContent>
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
            <p className="text-base font-medium">没有匹配的经历片段或日记</p>
            <p className="text-xs text-muted-foreground">可调整搜索词或重置类型/情感权重筛选。</p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((item) => {
            const isDiary = item.episode_type === 'daily_diary'
            return (
              <Card
                key={item.id}
                className={
                  isDiary
                    ? 'flex flex-col border-amber-500/30 bg-gradient-to-b from-amber-500/[0.04] to-card transition-all hover:border-amber-500/60 shadow-xs'
                    : 'flex flex-col transition-colors hover:border-primary/50'
                }
              >
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-1.5">
                      {isDiary ? (
                        <Badge variant="outline" className="border-amber-500/40 bg-amber-500/10 font-semibold text-amber-700 dark:text-amber-400">
                          📖 群聊日记
                        </Badge>
                      ) : (
                        <CardTitle className="text-sm font-semibold leading-snug">
                          {EPISODE_TYPE_LABELS[item.episode_type ?? ''] ?? item.episode_type ?? `片段 #${item.id}`}
                        </CardTitle>
                      )}
                    </div>
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
                    {item.outcome && !isDiary ? ` · ${item.outcome}` : ''}
                  </CardDescription>
                </CardHeader>
                <CardContent className="flex flex-1 flex-col gap-3 pt-0 text-xs">
                  {isDiary ? (
                    // 日记专属结构化排版
                    <>
                      {item.trigger_text ? (
                        <div>
                          <p className="font-semibold text-foreground text-sm leading-snug">{item.trigger_text}</p>
                        </div>
                      ) : null}
                      {item.bot_inner_thought ? (
                        <div className="rounded-md border border-amber-500/20 bg-amber-500/[0.06] p-2.5">
                          <p className="mb-1 font-medium text-amber-800 dark:text-amber-300">主线摘要</p>
                          <p className="line-clamp-3 leading-relaxed text-foreground/90">{item.bot_inner_thought}</p>
                        </div>
                      ) : null}
                      {item.bot_reply ? (
                        <div className="rounded-md border bg-muted/20 p-2.5">
                          <p className="mb-1 font-medium text-muted-foreground">亲笔随笔正文</p>
                          <p className="line-clamp-4 whitespace-pre-wrap leading-relaxed">{item.bot_reply}</p>
                        </div>
                      ) : null}
                    </>
                  ) : (
                    // 普通经历片段排版
                    <>
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
                    </>
                  )}

                  <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-border/40 pt-2 text-[11px] text-muted-foreground">
                    <span className="font-mono">episode:{item.id}</span>
                    {item.reflection_candidate ? <Badge variant="secondary">反思候选</Badge> : null}
                    {sourceMemoryIds(item.source_memory_ids).length ? (
                      <span className="font-mono text-primary">来源 memory: {sourceMemoryIds(item.source_memory_ids).join(', ')}</span>
                    ) : (
                      <span>暂无来源记忆</span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
                    {item.user_id ? (
                      <Link className="flex items-center gap-1 truncate text-primary hover:underline" to={scopedHref('/people', pagination.searchParams.toString(), { search: item.user_id })}>
                        <UserIcon className="size-3" aria-hidden="true" />{item.user_id}
                      </Link>
                    ) : <span>{item.group_id ?? '未记录会话'}</span>}
                    {item.created_at ? <span>{formatTime(item.created_at)}</span> : null}
                  </div>
                </CardContent>
              </Card>
            )
          })}
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
