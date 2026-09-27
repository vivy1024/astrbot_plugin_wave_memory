import { AlertCircleIcon, CheckCircle2Icon } from 'lucide-react'

import type { ServiceHealth, SystemHealthSummary } from '@/api/system'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

function serviceNameLabel(name: unknown): string {
  const value = String(name ?? '-')
  const labels: Record<string, string> = {
    Embedding: '向量嵌入',
    MetaThinking: '元思考',
    'Tag 索引': '标签索引',
    'Tag 提取': '标签提取',
    'EPA 基底': 'EPA 基底',
  }
  return labels[value] ?? value
}

function serviceStatusLabel(status: unknown): string {
  const value = String(status ?? 'unknown')
  if (value === 'ok') return '正常'
  if (value === 'error') return '错误'
  if (value === 'timeout') return '超时'
  if (value === 'degraded') return '降级'
  if (value === 'off') return '未启用'
  return '未知'
}

function fallbackOverall(services: ServiceHealth[]): string {
  if (services.some((service) => service.severity === 'critical')) return 'critical'
  if (services.some((service) => service.severity === 'degraded' || service.severity === 'disabled')) return 'degraded'
  return services.length > 0 ? 'healthy' : 'unknown'
}

export function SystemHealthCard({ services = [], summary }: { services?: ServiceHealth[]; summary?: SystemHealthSummary }) {
  const overall = summary?.overall ?? fallbackOverall(services)
  const problems = services.filter((service) => service.severity !== 'ok')
  const isHealthy = overall === 'healthy' && problems.length === 0

  return (
    <Card className="border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="text-base">系统健康</CardTitle>
            <CardDescription className="text-xs">仅突出需要处理的服务；正常项汇总显示</CardDescription>
          </div>
          <Badge variant={overall === 'critical' ? 'destructive' : overall === 'degraded' ? 'outline' : 'secondary'}>
            {summary?.label ?? (isHealthy ? '健康' : '需检查')}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            ['服务总数', summary?.total ?? services.length],
            ['正常', summary?.ok_count ?? services.filter((item) => item.severity === 'ok').length],
            ['降级', summary?.degraded_count ?? 0],
            ['核心异常', summary?.critical_count ?? 0],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-lg border bg-muted/5 px-3 py-2">
              <p className="text-xs text-muted-foreground">{label}</p>
              <p className="mt-1 font-mono text-sm font-semibold">{String(value)}</p>
            </div>
          ))}
        </div>

        {isHealthy ? (
          <div className="flex items-center gap-2 rounded-lg border border-emerald-500/15 bg-emerald-500/[0.03] px-3 py-2.5 text-xs text-muted-foreground">
            <CheckCircle2Icon className="size-4 shrink-0 text-emerald-500" />
            <span>{services.length} 个已注册服务全部正常；不在总览重复展开正常项。</span>
          </div>
        ) : problems.length > 0 ? (
          <div className="grid gap-2 md:grid-cols-2">
            {problems.map((service, index) => (
              <div key={`${service.name}-${index}`} className="flex items-start justify-between gap-3 rounded-lg border border-destructive/15 px-3 py-2.5">
                <div className="flex min-w-0 items-start gap-2">
                  <AlertCircleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
                  <div className="min-w-0">
                    <p className="text-xs font-medium">{serviceNameLabel(service.name)}</p>
                    {service.reason ? <p className="text-xs text-muted-foreground">{service.reason}</p> : null}
                  </div>
                </div>
                <Badge variant="outline" className="shrink-0 text-xs">{serviceStatusLabel(service.status)}</Badge>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">暂无服务健康数据。</p>
        )}
      </CardContent>
    </Card>
  )
}
