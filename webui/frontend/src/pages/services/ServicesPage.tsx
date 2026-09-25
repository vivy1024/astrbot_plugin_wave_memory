import { useCallback, useEffect, useState } from 'react'
import { PackageIcon, PlayIcon, RefreshCwIcon, RotateCwIcon, SquareIcon } from 'lucide-react'
import { toast } from 'sonner'

import { isRequestCancelled } from '@/api/client'
import { controlService, getServices, reloadExtensions, setToolEnabled, type ServiceDto, type ServicesPayload } from '@/api/services'
import { QueryState } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'

const GROUP_LABELS: Record<string, string> = { memory: '记忆', social: '社交与经历', agent_feedback: '自诊断', book_lore: '书设' }

function serviceState(service: ServiceDto): { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline' } {
  if (!service.created) return { label: '未创建', variant: 'outline' }
  if (service.tasks.some((task) => task.state === 'failed')) return { label: '出错', variant: 'destructive' }
  if (service.running) return { label: '运行中', variant: 'default' }
  return { label: '已停止', variant: 'secondary' }
}

export function ServicesPage() {
  const [payload, setPayload] = useState<ServicesPayload | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setPayload(await getServices(signal))
      setStatus('success')
    } catch (err) {
      if (isRequestCancelled(err)) return
      setError(err instanceof Error ? err.message : String(err))
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function act(service: ServiceDto, action: 'start' | 'stop' | 'restart') {
    setBusy(`${service.name}:${action}`)
    try {
      await controlService(service.name, action)
      toast.success(`${service.title}：${action === 'start' ? '已启动' : action === 'stop' ? '已停止' : '已重启'}`)
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  async function toggleTool(name: string, enabled: boolean) {
    try {
      await setToolEnabled(name, enabled)
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  async function reload() {
    setBusy('extensions:reload')
    try {
      const result = await reloadExtensions()
      const failed = Object.keys(result.errors)
      const summary = `扩展 ${result.loaded.length} 个 · 工具 ${result.tools.added.length} · 通道 ${result.channels.added.length}`
      if (failed.length) toast.error(`${summary}；加载失败：${failed.join('、')}`)
      else toast.success(`已重新加载：${summary}`)
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const extensions = Object.entries(payload?.tool_registry?.extensions ?? {})
  const buildErrors = payload?.tool_registry ? { ...payload.tool_registry.build_errors, ...payload.tool_registry.extension_errors } : {}
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={() => void load()}><RefreshCwIcon className="size-4" />刷新</Button>
        <Button type="button" variant="outline" disabled={busy !== null} onClick={() => void reload()}><PackageIcon className="size-4" />重新加载扩展</Button>
      </div>
      {Object.keys(buildErrors).length ? (
        <Alert variant="destructive">
          <AlertTitle>有工具或扩展没有加载成功</AlertTitle>
          <AlertDescription><ul className="list-disc pl-5">{Object.entries(buildErrors).map(([key, message]) => <li key={key}><span className="font-mono">{key}</span>：{message}</li>)}</ul></AlertDescription>
        </Alert>
      ) : null}
      <QueryState status={status} error={error} onRetry={() => void load()}>
        <Card>
          <CardHeader>
            <CardTitle>后台服务</CardTitle>
            <CardDescription>单独停止、启动、重启，不中断 QQ 回复，也不用重启 AstrBot。「未创建」的服务是静态配置里关着的，需要在 AstrBot 配置里打开后重启一次。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {(payload?.services ?? []).map((service) => {
              const state = serviceState(service)
              const failed = service.tasks.find((task) => task.last_error)
              return (
                <div key={service.name} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
                  <div className="flex flex-col">
                    <span className="font-medium">{service.title} <span className="font-mono text-xs text-muted-foreground">{service.name}</span></span>
                    <span className="text-xs text-muted-foreground">{service.description}{service.generation ? ` · 本进程内重启 ${service.generation} 次` : ''}</span>
                    {failed?.last_error ? <span className="text-xs text-destructive">最近错误：{failed.last_error}</span> : null}
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={state.variant}>{state.label}</Badge>
                    {service.created && service.stoppable ? (
                      <>
                        <Button type="button" size="sm" variant="outline" disabled={busy !== null || service.running === true} onClick={() => void act(service, 'start')}><PlayIcon className="size-4" />启动</Button>
                        <Button type="button" size="sm" variant="outline" disabled={busy !== null || service.running === false} onClick={() => void act(service, 'stop')}><SquareIcon className="size-4" />停止</Button>
                        <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => void act(service, 'restart')}><RotateCwIcon className="size-4" />重启</Button>
                      </>
                    ) : null}
                  </div>
                </div>
              )
            })}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>工具</CardTitle>
            <CardDescription>停用后 AstrBot 与 Cortico 都不再提供该工具，立即生效。每个 Bot 还可以在「Bot 管理」里单独限制。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {(payload?.tools ?? []).map((tool) => (
              <div key={tool.name} className="flex items-center justify-between gap-2 rounded-md border p-2">
                <div className="flex flex-col">
                  <span className="font-mono text-sm">{tool.name}</span>
                  <span className="line-clamp-1 text-xs text-muted-foreground">{tool.description}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant="outline">{GROUP_LABELS[tool.group] ?? tool.group}</Badge>
                  {tool.writes ? <Badge variant="secondary">会写入</Badge> : null}
                  <span className="text-xs text-muted-foreground">调用 {tool.stats.calls} · 失败 {tool.stats.errors}</span>
                  <Switch aria-label={`启用 ${tool.name}`} checked={tool.enabled} onCheckedChange={(value) => void toggleTool(tool.name, value)} />
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>扩展</CardTitle>
            <CardDescription>放在插件数据目录 <span className="font-mono">extensions/</span> 下的 .py 文件。改完点「重新加载扩展」，扩展的工具与通道立即替换；内置工具、工具开关与通道停用状态不受影响。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-1 text-sm">
            {extensions.length ? extensions.map(([file, tools]) => (
              <div key={file} className="flex flex-wrap items-center gap-2"><span className="font-mono">{file}</span>{tools.map((tool) => <Badge key={tool} variant="outline" className="font-mono">{tool}</Badge>)}</div>
            )) : <span className="text-muted-foreground">没有加载扩展</span>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>注入通道</CardTitle><CardDescription>当前加载的通道；开关和预算在「通道配置」页调整。</CardDescription></CardHeader>
          <CardContent className="flex flex-wrap gap-1">{(payload?.channels ?? []).map((name) => <Badge key={name} variant="outline" className="font-mono">{name}</Badge>)}</CardContent>
        </Card>
      </QueryState>
    </div>
  )
}
