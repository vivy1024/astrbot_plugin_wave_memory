import { CartesianGrid, Area, AreaChart, XAxis, YAxis } from 'recharts'
import { Loader2Icon, WavesIcon } from 'lucide-react'

import type { InjectionMetricsPayload } from '@/api/system'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { Button } from '@/components/ui/button'

const chartConfig = {
  total_tokens: { label: '总 token', color: 'var(--chart-1)' },
  memories_tokens: { label: '记忆', color: 'var(--chart-2)' },
  soul_tokens: { label: '灵魂', color: 'var(--chart-3)' },
} satisfies ChartConfig

function formatNumber(value: unknown): string {
  const number = Number(value)
  if (!Number.isFinite(number)) {
    return '-'
  }
  return new Intl.NumberFormat('zh-CN').format(number)
}

interface InjectionTrendCardProps {
  metrics?: InjectionMetricsPayload
  range: string
  onRangeChange: (range: string) => void
  loading?: boolean
}

export function InjectionTrendCard({ metrics, range, onRangeChange, loading }: InjectionTrendCardProps) {
  const series = metrics?.series ?? []

  // 针对不同时间跨度，智能展现最符合人类直觉的时间刻度
  const formatBucket = (value: unknown): string => {
    const seconds = Number(value)
    if (!Number.isFinite(seconds) || seconds <= 0) {
      return '-'
    }
    const date = new Date(seconds * 1000)
    if (range === '1d') {
      return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    }
    return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', hour12: false })
  }

  // 提取聚合属性
  const totalTokensSum = Number(metrics?.window?.total_tokens_sum ?? 0)
  const sampleCount = Number(metrics?.window?.sample_count ?? metrics?.count ?? 0)
  const avgTokensPerSample = Number(metrics?.window?.avg_tokens_per_sample ?? 0)
  const avgTokensPerDay = Number(metrics?.window?.avg_tokens_per_day ?? 0)
  const p95TokensPerSample = Number(metrics?.window?.p95_tokens_per_sample ?? 0)
  const maxTokensPerSample = Number(metrics?.window?.max_tokens_per_sample ?? 0)

  const rangeLabels: Record<string, string> = {
    '1d': '最近 24 小时',
    '3d': '最近 3 天',
    '7d': '最近 7 天',
    '30d': '最近 30 天',
  }

  return (
    <Card className="lg:col-span-2 flex flex-col border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm hover:border-primary/20 hover:shadow-[0_0_24px_rgba(124,58,237,0.02)] transition-all duration-300">
      <CardHeader className="flex flex-row items-center justify-between gap-4 pb-4">
        <div>
          <CardTitle className="tracking-tight flex items-center gap-2">
            <span>注入消耗趋势</span>
            {loading && <Loader2Icon className="size-4 animate-spin text-primary" />}
          </CardTitle>
          <CardDescription className="text-xs">
            按持久化注入指标聚合；切换时间范围会重新查询
          </CardDescription>
        </div>

        {/* 极富科技质感的时间段配置小药丸 */}
        <div className="flex items-center gap-1 bg-muted/60 p-1 rounded-lg border border-border/40 shrink-0">
          {(['1d', '3d', '7d', '30d'] as const).map((r) => {
            const active = range === r
            return (
              <Button
                key={r}
                size="sm"
                variant={active ? 'default' : 'ghost'}
                onClick={() => onRangeChange(r)}
                className={`h-7 px-2.5 text-xs font-medium transition-all duration-200 ${
                  active
                    ? 'bg-background shadow-sm hover:bg-background text-foreground border border-border/20'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {r === '1d' ? '24h' : r === '3d' ? '3天' : r === '7d' ? '7天' : '30天'}
              </Button>
            )
          })}
        </div>
      </CardHeader>

      {/* 全新的“副大盘仪表盘”：拒绝截断、直观、精美、满载科技霓虹感 */}
      <div className="px-6 pb-2 grid grid-cols-2 md:grid-cols-4 gap-4 border-b border-border/30">
        <div className="flex flex-col gap-1 py-2">
          <span className="text-xs text-muted-foreground/80 font-medium">窗口累计 Token ({rangeLabels[range] || range})</span>
          <span className="text-xl md:text-2xl font-bold font-mono tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-primary via-violet-400 to-indigo-400">
            {formatNumber(totalTokensSum)}
          </span>
          <span className="text-xs text-muted-foreground">共 {formatNumber(sampleCount)} 次注入样本</span>
        </div>

        <div className="flex flex-col gap-1 py-2 pl-2 border-l border-border/20">
          <span className="text-xs text-muted-foreground/80 font-medium">单次均值</span>
          <span className="text-lg md:text-xl font-bold font-mono tracking-tight text-foreground/90">
            {formatNumber(avgTokensPerSample)}
          </span>
          <span className="text-xs text-muted-foreground">P95 峰值 {formatNumber(p95TokensPerSample)}</span>
        </div>

        <div className="flex flex-col gap-1 py-2 pl-2 border-l border-border/20">
          <span className="text-xs text-muted-foreground/80 font-medium">日均 token</span>
          <span className="text-lg md:text-xl font-bold font-mono tracking-tight text-foreground/90">
            {formatNumber(avgTokensPerDay)}
          </span>
          <span className="text-xs text-muted-foreground">基于当前时间跨度算</span>
        </div>

        <div className="flex flex-col gap-1 py-2 pl-2 border-l border-border/20">
          <span className="text-xs text-muted-foreground/80 font-medium">单次最高值</span>
          <span className="text-lg md:text-xl font-bold font-mono tracking-tight text-foreground/90">
            {formatNumber(maxTokensPerSample)}
          </span>
          <span className="text-xs text-muted-foreground">所选窗口内实测最大值</span>
        </div>
      </div>

      <CardContent className="flex-1 min-h-[260px] pt-6">
        {series.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-[240px] text-center">
            <WavesIcon className="size-8 text-muted-foreground/30 animate-pulse mb-2" />
            <p className="text-xs text-muted-foreground">该时间段内暂无注入指标样本</p>
          </div>
        ) : (
          <ChartContainer config={chartConfig} className="h-[240px] w-full">
            <AreaChart data={series} margin={{ left: 12, right: 12, top: 4 }}>
              <defs>
                <linearGradient id="colorTotal" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="var(--chart-1)" stopOpacity={0.16} />
                  <stop offset="95%" stopColor="var(--chart-1)" stopOpacity={0.01} />
                </linearGradient>
                <linearGradient id="colorMemories" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="var(--chart-2)" stopOpacity={0.12} />
                  <stop offset="95%" stopColor="var(--chart-2)" stopOpacity={0.0} />
                </linearGradient>
                <linearGradient id="colorSoul" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="var(--chart-3)" stopOpacity={0.08} />
                  <stop offset="95%" stopColor="var(--chart-3)" stopOpacity={0.0} />
                </linearGradient>
              </defs>
              <CartesianGrid vertical={false} strokeDasharray="3 3" opacity={0.15} stroke="var(--border)" />
              <XAxis dataKey="bucket_ts" tickFormatter={formatBucket} tickLine={false} axisLine={false} minTickGap={32} className="text-xs font-mono" />
              <YAxis tickLine={false} axisLine={false} width={48} allowDataOverflow={false} domain={[0, 'auto']} className="text-xs font-mono" />
              <ChartTooltip content={<ChartTooltipContent className="backdrop-blur-md bg-background/90 border-border/80" />} />
              <Area dataKey="total_tokens" type="monotone" stroke="var(--chart-1)" strokeWidth={2.5} fillOpacity={1} fill="url(#colorTotal)" />
              <Area dataKey="memories_tokens" type="monotone" stroke="var(--chart-2)" strokeWidth={2} fillOpacity={1} fill="url(#colorMemories)" />
              <Area dataKey="soul_tokens" type="monotone" stroke="var(--chart-3)" strokeWidth={1.5} fillOpacity={1} fill="url(#colorSoul)" />
            </AreaChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  )
}
