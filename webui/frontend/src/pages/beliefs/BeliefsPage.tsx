import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BanIcon, BrainCircuitIcon, HistoryIcon, Loader2Icon, NotebookTextIcon, RotateCcwIcon, SproutIcon } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import {
  listBeliefs,
  restoreBelief,
  vetoBelief,
  type BeliefGroup,
  type BeliefHistoryItem,
  type BeliefItem,
  type BeliefStatus,
  type BeliefsResponse,
} from '@/api/beliefs'
import { isRequestCancelled } from '@/api/client'
import { useGlobalScope } from '@/app/global-scope'
import { HeroHeader, QueryState } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { humanizeApiError, humanizeReason } from '@/lib/reason-label'
import { cn } from '@/lib/utils'

const GROUP_ORDER: BeliefStatus[] = ['sprout', 'formed', 'stable', 'shaken', 'faded', 'vetoed', 'legacy_archived']
// 打开页面时优先落在「已形成」：那是正在影响她说话的看法
const DEFAULT_TAB_PRIORITY: BeliefStatus[] = ['formed', 'stable', 'sprout', 'shaken', 'faded', 'vetoed', 'legacy_archived']

const LEGACY_STATUS_LABELS: Record<string, string> = {
  active: '已生效',
  approved: '已生效',
  pending: '待审核',
  quarantined: '已隔离',
  archived: '已归档',
  rejected: '已驳回',
}

function statusBadgeClass(status: BeliefStatus): string {
  if (status === 'formed' || status === 'stable') return 'border-primary/20 bg-primary/10 text-primary'
  if (status === 'sprout') return 'border-border bg-secondary text-secondary-foreground'
  if (status === 'shaken') return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
  if (status === 'vetoed') return 'border-destructive/20 bg-destructive/10 text-destructive'
  return 'border-border bg-muted text-muted-foreground'
}

function formatTime(seconds?: number | null): string {
  return seconds && Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false }) : '未记录'
}

function percent(value?: number): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` : '—'
}

function reflectionLabel(data: BeliefsResponse | null): string {
  if (!data) return '反思状态读取中'
  if (!data.reflection.enabled) return '反思未开启'
  const days = data.reflection.every_n_days ?? 1
  return days <= 1 ? '每天日记后反思' : `每 ${days} 天反思一次`
}

function lastRunText(data: BeliefsResponse | null): string {
  const run = data?.reflection.last_run
  if (!run) return '还没有'
  if (run.status === 'succeeded') return run.run_date
  if (run.status === 'failed') return `${run.run_date}（失败）`
  return `${run.run_date}（进行中）`
}

function HistoryList({ history }: { history: BeliefHistoryItem[] }) {
  if (!history.length) return <p className="text-sm text-muted-foreground">还没有变化记录。</p>
  return (
    <ol className="flex flex-col gap-2" data-slot="belief-history">
      {[...history].reverse().map((event) => (
        <li key={event.id} className="rounded-md border bg-background p-3 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium">{event.label}</span>
            <span className="text-xs text-muted-foreground">{formatTime(event.occurred_at)}</span>
          </div>
          {event.from_label !== event.to_label && event.to_label ? (
            <p className="mt-1 text-xs text-muted-foreground">{event.from_label ? `${event.from_label} → ${event.to_label}` : `成为${event.to_label}`}</p>
          ) : null}
          {event.reason ? <p className="mt-1 text-sm text-foreground/90">{event.reason}</p> : null}
          {event.evidence.length ? (
            <p className="mt-1 text-xs text-muted-foreground">依据：{event.evidence.map((ref) => `${ref.date}「${ref.group_name}」${ref.title}`).join('；')}</p>
          ) : null}
        </li>
      ))}
    </ol>
  )
}

function EvidenceList({ item }: { item: BeliefItem }) {
  if (!item.evidence.length) return <p className="text-sm text-muted-foreground">没有记录依据日记。</p>
  return (
    <ul className="flex flex-col gap-1.5" data-slot="belief-evidence">
      {item.evidence.map((ref) => (
        <li key={`${ref.diary_id}:${ref.polarity ?? 'support'}`} className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant="outline" className={ref.polarity === 'challenge' ? 'border-amber-500/30 text-amber-700 dark:text-amber-300' : undefined}>
            {ref.polarity === 'challenge' ? '反例' : '印证'}
          </Badge>
          <span className="tabular-nums text-muted-foreground">{ref.date}</span>
          <span className="text-muted-foreground">「{ref.group_name}」</span>
          <span className="text-foreground/90">{ref.title}</span>
          <span className="text-xs text-muted-foreground">日记 #{ref.diary_id}</span>
        </li>
      ))}
    </ul>
  )
}

function BeliefCard({
  item,
  mutating,
  onVeto,
  onRestore,
}: {
  item: BeliefItem
  mutating: boolean
  onVeto: (item: BeliefItem) => void
  onRestore: (item: BeliefItem) => void
}) {
  if (item.legacy) {
    return (
      <article data-slot="belief-card" data-legacy="true" className="flex flex-col gap-2 rounded-lg border bg-muted/30 p-4">
        <p className="whitespace-pre-wrap break-words leading-6 text-foreground/90">{item.content}</p>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="outline">旧版人物判断</Badge>
          {item.legacy_type_label ? <Badge variant="outline">{item.legacy_type_label}</Badge> : null}
          {item.legacy_status ? <span>原状态：{LEGACY_STATUS_LABELS[item.legacy_status] ?? humanizeReason(item.legacy_status, '未记录')}</span> : null}
          {item.session_id ? <span className="font-mono">{item.session_id}</span> : null}
          <span>只读保留，不会注入</span>
        </div>
      </article>
    )
  }
  return (
    <article data-slot="belief-card" className="flex flex-col gap-3 rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="min-w-0 flex-1 whitespace-pre-wrap break-words font-medium leading-6">{item.content}</p>
        <div className="flex shrink-0 items-center gap-1.5">
          {item.actions.veto.available ? (
            <Button type="button" size="sm" variant="outline" disabled={mutating} aria-label={`否决看法 ${item.id}`}
              title={humanizeReason(item.actions.veto.reason_code, '否决后不再浮现，也不会再生成相似的看法')}
              onClick={() => onVeto(item)}>
              {mutating ? <Loader2Icon className="animate-spin" data-icon="inline-start" /> : <BanIcon data-icon="inline-start" />}
              否决
            </Button>
          ) : null}
          {item.actions.restore.available ? (
            <Button type="button" size="sm" variant="outline" disabled={mutating} aria-label={`恢复看法 ${item.id}`}
              title={humanizeReason(item.actions.restore.reason_code, '撤销否决，回到否决前的状态')}
              onClick={() => onRestore(item)}>
              {mutating ? <Loader2Icon className="animate-spin" data-icon="inline-start" /> : <RotateCcwIcon data-icon="inline-start" />}
              恢复
            </Button>
          ) : null}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge className={statusBadgeClass(item.status)}>{item.status_label}</Badge>
        {item.category_label ? <Badge variant="outline">{item.category_label}</Badge> : null}
        <span className="text-muted-foreground">支持强度 {percent(item.strength)}</span>
        <span className="text-muted-foreground">{item.support_days ?? 0} 个日子的经历</span>
        {item.challenge_count ? <span className="text-muted-foreground">反例 {item.challenge_count} 次</span> : null}
        {item.experience_phrase ? <span className="text-muted-foreground">{item.experience_phrase}</span> : null}
      </div>
      {item.triggers?.length ? (
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          <span>聊到这些时会想起：</span>
          {item.triggers.map((trigger) => <Badge key={trigger} variant="secondary" className="font-normal">{trigger}</Badge>)}
        </div>
      ) : null}
      <details className="rounded-lg border bg-muted/20 p-3">
        <summary className="cursor-pointer text-sm font-medium">依据日记与变化历史</summary>
        <div className="mt-3 grid gap-4 lg:grid-cols-2">
          <section className="flex flex-col gap-2">
            <h4 className="flex items-center gap-1.5 text-sm font-medium"><NotebookTextIcon className="size-4 text-primary" aria-hidden="true" />依据日记</h4>
            <EvidenceList item={item} />
          </section>
          <section className="flex flex-col gap-2">
            <h4 className="flex items-center gap-1.5 text-sm font-medium"><HistoryIcon className="size-4 text-primary" aria-hidden="true" />变化历史</h4>
            <HistoryList history={item.history} />
          </section>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">第一次出现 {formatTime(item.first_seen_at)} · 最近一次被印证 {formatTime(item.last_confirmed_at)}</p>
      </details>
    </article>
  )
}

function GroupPanel({ group, mutating, onVeto, onRestore }: {
  group: BeliefGroup
  mutating: number | null
  onVeto: (item: BeliefItem) => void
  onRestore: (item: BeliefItem) => void
}) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">{group.description}</p>
      {group.items.length ? group.items.map((item) => (
        <BeliefCard key={item.key} item={item} mutating={mutating === item.id} onVeto={onVeto} onRestore={onRestore} />
      )) : (
        <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">这一组还没有看法。</div>
      )}
    </div>
  )
}

export function BeliefsPage() {
  const { botId, status: scopeStatus, optionsError } = useGlobalScope()
  const [searchParams, setSearchParams] = useSearchParams()
  const [data, setData] = useState<BeliefsResponse | null>(null)
  const [queryStatus, setQueryStatus] = useState<'loading' | 'success' | 'empty' | 'error'>('loading')
  const [error, setError] = useState<unknown>()
  const [mutating, setMutating] = useState<number | null>(null)
  // 序号守卫：切换 Bot 时旧请求可能后到，丢弃过期响应，避免把别的 Bot 的看法显示出来
  const listRequest = useRef(0)
  const requestedTab = searchParams.get('status') as BeliefStatus | null

  const load = useCallback(async () => {
    const request = ++listRequest.current
    if (!botId) {
      setData(null)
      setQueryStatus('empty')
      return
    }
    setQueryStatus('loading')
    setError(undefined)
    try {
      const next = await listBeliefs(botId)
      if (request !== listRequest.current) return
      setData(next)
      setQueryStatus('success')
    } catch (reason) {
      if (request !== listRequest.current || isRequestCancelled(reason)) return
      setData(null)
      setError(reason)
      setQueryStatus('error')
    }
  }, [botId])

  useEffect(() => { void load() }, [load])

  const groups = useMemo(() => {
    const byStatus = new Map((data?.groups ?? []).map((group) => [group.status, group]))
    return GROUP_ORDER.map((status) => byStatus.get(status)).filter((group): group is BeliefGroup => Boolean(group))
  }, [data])

  const activeTab = useMemo<BeliefStatus>(() => {
    if (requestedTab && GROUP_ORDER.includes(requestedTab)) return requestedTab
    return DEFAULT_TAB_PRIORITY.find((status) => groups.find((group) => group.status === status)?.count) ?? 'formed'
  }, [groups, requestedTab])

  function changeTab(value: string) {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      next.set('status', value)
      return next
    }, { replace: true })
  }

  async function mutate(item: BeliefItem, action: 'veto' | 'restore') {
    if (!botId) return
    setMutating(item.id)
    try {
      const result = action === 'veto' ? await vetoBelief(item, botId) : await restoreBelief(item, botId)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认操作成功')
      toast.success(action === 'veto' ? '已否决：不再浮现，以后也不会再生成相似的看法' : '已恢复到否决前的状态')
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, action === 'veto' ? '否决失败' : '恢复失败'))
      await load()
    } finally {
      setMutating(null)
    }
  }

  const count = (status: BeliefStatus) => data?.counts?.[status] ?? 0
  const header = (
    <HeroHeader
      icon={<BrainCircuitIcon className="size-6" />}
      title="她的看法"
      description="从她自己的经历里慢慢形成的看法：每天写完日记后回想，在不同日子里被印证才算形成；只在聊到相关话题时浮现，可以一键否决或恢复。"
      badge={<Badge variant="outline" className="w-fit"><SproutIcon className="size-3.5" />{reflectionLabel(data)}</Badge>}
      metrics={[
        { label: '已形成 / 稳固', value: data ? count('formed') + count('stable') : '—', tone: 'text-primary', description: data ? `上限 ${data.policy.max_active} 条` : undefined },
        { label: '萌芽', value: data ? count('sprout') : '—' },
        { label: '动摇中', value: data ? count('shaken') : '—' },
        { label: '最近一次反思', value: <span className="text-base">{lastRunText(data)}</span> },
      ]}
    />
  )

  if (!botId) {
    return (
      <div data-slot="beliefs-page" className="flex flex-col gap-6">
        {header}
        {scopeStatus === 'resolving' ? <QueryState status="loading" title="正在确定 Bot" /> : scopeStatus === 'error'
          ? <QueryState status="error" title="Bot 列表加载失败" error={optionsError || undefined} />
          : <QueryState status="empty" emptyTitle="先选一个 Bot" emptyDescription="看法属于 Bot 本人：在顶栏选择 Bot 后查看她形成的看法。" />}
      </div>
    )
  }

  return (
    <div data-slot="beliefs-page" className="flex flex-col gap-6">
      {header}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">按状态分组</CardTitle>
          <CardDescription>
            {data
              ? `萌芽要在至少 ${data.policy.form_min_days} 个不同日子的经历里得到支持并被再次印证才形成；${data.policy.decay_days} 天不再被印证会慢慢变弱。`
              : '正在读取她的看法。'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <QueryState status={queryStatus} error={error} onRetry={() => void load()}>
            <Tabs value={activeTab} onValueChange={changeTab} className="flex flex-col gap-4">
              <TabsList className="h-auto w-full flex-wrap justify-start">
                {groups.map((group) => (
                  <TabsTrigger key={group.status} value={group.status} className={cn('gap-1.5', !group.count && 'opacity-70')}>
                    {group.label}
                    <span className="tabular-nums text-xs text-muted-foreground">{group.count}</span>
                  </TabsTrigger>
                ))}
              </TabsList>
              {groups.map((group) => (
                <TabsContent key={group.status} value={group.status}>
                  <GroupPanel group={group} mutating={mutating} onVeto={(item) => void mutate(item, 'veto')} onRestore={(item) => void mutate(item, 'restore')} />
                </TabsContent>
              ))}
            </Tabs>
          </QueryState>
        </CardContent>
      </Card>
    </div>
  )
}
