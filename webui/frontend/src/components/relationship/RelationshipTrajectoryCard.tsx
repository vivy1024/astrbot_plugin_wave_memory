import { useEffect, useMemo, useState } from 'react'
import { GitBranchIcon, MessageSquareQuoteIcon } from 'lucide-react'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'

import { getSoulState, type RelationshipHistoryItem } from '@/api/soul'
import { Badge } from '@/components/ui/badge'
import { EvidenceList } from '@/components/shared'
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { formatDisplayNumber } from '@/lib/format-number'
import { humanizeApiError } from '@/lib/reason-label'

export const RELATIONSHIP_DIMENSIONS = [
  ['familiarity', '熟悉度'],
  ['trust', '信任'],
  ['fun', '趣味'],
  ['hostility', '敌意'],
  ['depth', '深度'],
] as const

const trajectoryChartConfig = {
  familiarity: { label: '熟悉度', color: 'var(--chart-1)' },
  trust: { label: '信任', color: 'var(--chart-2)' },
  fun: { label: '趣味', color: 'var(--chart-3)' },
  hostility: { label: '敌意', color: 'var(--chart-4)' },
  depth: { label: '深度', color: 'var(--chart-5)' },
} satisfies ChartConfig

const DIMENSION_COLORS: Record<string, string> = {
  familiarity: 'var(--chart-1)',
  trust: 'var(--chart-2)',
  fun: 'var(--chart-3)',
  hostility: 'var(--chart-4)',
  depth: 'var(--chart-5)',
}

function formatTime(seconds: unknown): string {
  const value = Number(seconds)
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString('zh-CN') : '未记录'
}

function displayValue(value: unknown): string {
  return formatDisplayNumber(value, '未知 / 未记录')
}

function dimensionLabel(dimension: string): string {
  return RELATIONSHIP_DIMENSIONS.find(([key]) => key === dimension)?.[1] ?? dimension
}

/** 五维关系轨迹同图：每个维度每条线，Badge 可开关叠加。 */
function TrajectoryChart({ history }: { history: RelationshipHistoryItem[] }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const data = useMemo(() => {
    const byTime = new Map<number, Record<string, number | null>>()
    for (const item of history) {
      if (!item.after) continue
      const timestamp = Number(item.timestamp)
      if (!Number.isFinite(timestamp)) continue
      const row = (byTime.get(timestamp) ?? {}) as Record<string, number | null>
      const dim = String(item.dimension)
      if (RELATIONSHIP_DIMENSIONS.some(([key]) => key === dim)) {
        row[dim] = item.after?.effective_value ?? null
        byTime.set(timestamp, row)
      }
    }
    return [...byTime.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([timestamp, values]) => ({ timestamp, ...values }))
  }, [history])

  const featuredDimensions = RELATIONSHIP_DIMENSIONS.filter(([key]) =>
    data.some((row) => typeof (row as Record<string, unknown>)[key] === 'number'),
  )
  if (!featuredDimensions.length) {
    return <div className="rounded-xl border border-dashed bg-muted/10 p-8 text-center text-sm text-muted-foreground">当前时间范围没有五维的正式轨迹记录。</div>
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="轨迹维度开关">
        {RELATIONSHIP_DIMENSIONS.map(([key, label]) => {
          const hasData = featuredDimensions.some(([dim]) => dim === key)
          const active = !hidden.has(key)
          return (
            <Badge
              key={key}
              variant={active ? 'secondary' : 'outline'}
              aria-pressed={active}
              className={`cursor-pointer select-none text-[11px] transition-opacity ${!hasData ? 'opacity-40 saturate-50' : ''}`}
              onClick={() => setHidden((current) => {
                const next = new Set(current)
                if (next.has(key)) next.delete(key)
                else next.add(key)
                return next
              })}
            >
              {label}
            </Badge>
          )
        })}
      </div>
      <ChartContainer config={trajectoryChartConfig} className="h-[240px] w-full min-w-0">
        <LineChart data={data} margin={{ left: 4, right: 12, top: 8 }}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" opacity={0.2} />
          <XAxis dataKey="timestamp" tickFormatter={(value) => formatTime(value).slice(5, 16)} tickLine={false} axisLine={false} minTickGap={28} className="text-[10px]" />
          <YAxis tickLine={false} axisLine={false} width={42} className="text-[10px]" />
          <ChartTooltip labelFormatter={(value) => formatTime(value)} content={<ChartTooltipContent />} />
          <ChartLegend content={<ChartLegendContent />} />
          {RELATIONSHIP_DIMENSIONS.map(([key]) => (
            <Line
              key={key}
              dataKey={key}
              type="monotone"
              stroke={DIMENSION_COLORS[key]}
              strokeWidth={2}
              dot={false}
              connectNulls
              hide={hidden.has(key)}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ChartContainer>
      <p className="text-[10px] text-muted-foreground">同一图上按时间合并各维度；只显示有实际记录的维度，badge 可点按开关叠加。</p>
    </div>
  )
}

function HistoryList({ history }: { history: RelationshipHistoryItem[] }) {
  if (!history.length) return <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">当前群没有可追踪的关系变化或人工校准记录。</p>
  return (
    <div className="flex flex-col gap-3">
      {history.map((item) => (
        <div key={item.id} className="rounded-lg border bg-muted/10 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={item.kind === 'manual' ? 'secondary' : 'outline'}>{item.kind === 'manual' ? 'manual calibration' : 'automatic RelationshipEvent'}</Badge>
            <Badge variant="outline">{dimensionLabel(item.dimension)}</Badge>
            {item.action ? <Badge variant="outline">{item.action}</Badge> : null}
            <span className="text-[10px] text-muted-foreground">{formatTime(item.timestamp)} · revision {item.revision ?? '未记录'}</span>
          </div>
          <p className="mt-2 text-sm font-medium">{item.reason || '服务端未提供原因'}</p>
          <div className="mt-2 grid gap-2 text-xs sm:grid-cols-2">
            <div className="rounded border bg-background/60 p-2">
              <span className="text-muted-foreground">变化</span>
              <span className="ml-2 font-mono">{item.delta === null ? '人工层变更' : displayValue(item.delta)}</span>
            </div>
            <div className="rounded border bg-background/60 p-2">
              <span className="text-muted-foreground">来源</span>
              <span className="ml-2 font-mono">{item.source_memory_id !== null ? `memory:${item.source_memory_id}` : item.source_episode_id !== null ? `episode:${item.source_episode_id}` : '未提供真实消息引用'}</span>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap gap-2 text-[10px] text-muted-foreground">
            {item.operation_id ? <span className="font-mono">operation:{item.operation_id}</span> : null}
            {item.actor ? <span>actor:{item.actor}</span> : null}
          </div>
          <div className="mt-3"><EvidenceList evidence={item.evidence} /></div>
        </div>
      ))}
    </div>
  )
}

export function RelationshipTrajectoryCard({
  botId,
  sessionId,
  subjectPrincipalId,
}: {
  botId: string
  sessionId: string
  subjectPrincipalId: string
}) {
  const [history, setHistory] = useState<RelationshipHistoryItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!botId || !sessionId || !subjectPrincipalId) {
      setHistory([])
      setError(null)
      setLoading(false)
      return
    }
    let active = true
    setLoading(true)
    setError(null)
    getSoulState(
      { bot_id: botId, session_id: sessionId, visibility: 'group', subject_principal_id: subjectPrincipalId },
      100,
      0,
    )
      .then((payload) => {
        if (!active) return
        setHistory(payload.relationship_history?.items ?? [])
      })
      .catch((reason: unknown) => {
        if (!active) return
        setHistory([])
        setError(humanizeApiError(reason, '关系历史读取失败'))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [botId, sessionId, subjectPrincipalId])

  const graphableHistory = useMemo(() => history.filter((item) => item.after), [history])

  return (
    <div className="rounded-xl border bg-card p-4 text-sm">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs font-semibold">
          <GitBranchIcon className="size-4 text-violet-500" />
          关系变化轨迹
        </div>
      </div>
      {loading ? (
        <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">正在读取关系变化…</p>
      ) : error ? (
        <p className="rounded-xl border border-destructive/20 p-6 text-center text-sm text-destructive">{error}</p>
      ) : !graphableHistory.length ? (
        <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">还没有五维轨迹数据。</p>
      ) : (
        <TrajectoryChart history={history} />
      )}
      <div className="mt-4">
        <div className="mb-2 flex items-center gap-2 text-xs font-semibold">
          <MessageSquareQuoteIcon className="size-4 text-muted-foreground" />
          导致变化的真实来源
        </div>
        <HistoryList history={history} />
      </div>
    </div>
  )
}

export default RelationshipTrajectoryCard
