import { useEffect, useState } from 'react'
import { CheckCircle2Icon, HardDriveIcon } from 'lucide-react'

import { getOutboxStatus, type OutboxStatus } from '@/api/maintenance'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

export function OutboxMonitorCard() {
  const [data, setData] = useState<OutboxStatus | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getOutboxStatus()
      .then((res) => setData(res))
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <Card className="mb-6">
        <CardHeader className="py-4">
          <Skeleton className="h-5 w-48" />
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-4">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </CardContent>
      </Card>
    )
  }

  if (!data) return null

  return (
    <Card className="mb-6 overflow-hidden border-primary/20 bg-gradient-to-br from-card to-primary/[0.02]">
      <CardHeader className="py-4 border-b bg-muted/10">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <HardDriveIcon className="size-4 text-primary" />
            <CardTitle className="text-sm font-semibold">WriteGateway & Outbox 管道健康</CardTitle>
          </div>
          <Badge variant={data.write_gateway_wired ? 'default' : 'secondary'} className="text-[11px]">
            <CheckCircle2Icon className="mr-1 size-3" />
            {data.write_gateway_wired ? '写协调器就绪' : '挂载中'}
          </Badge>
        </div>
        <CardDescription className="text-xs">
          后端 SQLite 事务、Domain Outbox 异步投递与恢复队列实时指标
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 py-4 sm:grid-cols-4 text-center">
        <div className="rounded-lg border bg-card/60 p-3">
          <p className="text-xs text-muted-foreground">Domain Outbox 项</p>
          <p className="mt-1 text-xl font-bold font-mono text-primary">{(data.outbox_items ?? 0).toLocaleString('zh-CN')}</p>
        </div>
        <div className="rounded-lg border bg-card/60 p-3">
          <p className="text-xs text-muted-foreground">Outbox 累计投递</p>
          <p className="mt-1 text-xl font-bold font-mono">{(data.outbox_deliveries ?? 0).toLocaleString('zh-CN')}</p>
        </div>
        <div className="rounded-lg border bg-card/60 p-3">
          <p className="text-xs text-muted-foreground">当前群恢复项队列</p>
          <p className="mt-1 text-xl font-bold font-mono">{(data.scope_recovery_items ?? 0).toLocaleString('zh-CN')}</p>
        </div>
        <div className="rounded-lg border bg-card/60 p-3">
          <p className="text-xs text-muted-foreground">Job 请求并发计数</p>
          <p className="mt-1 text-xl font-bold font-mono">{(data.job_requests ?? 0).toLocaleString('zh-CN')}</p>
        </div>
      </CardContent>
    </Card>
  )
}
