/**
 * 配置中心「外部接入」：Runtime API（/api/runtime/v1）的访问令牌。
 * 外部程序（如 Cortico 直播）调用记忆注入、写回、工具与存查删记忆时都要带它。
 * 可以看当前令牌与来源、复制、生成新的或自定义；改动立即生效。
 */
import { useCallback, useEffect, useState } from 'react'
import { AlertTriangleIcon, CheckIcon, CopyIcon, EyeIcon, EyeOffIcon, KeyRoundIcon, RefreshCwIcon } from 'lucide-react'
import { toast } from 'sonner'

import {
  changeRuntimeToken,
  getRuntimeToken,
  type RuntimeTokenChange,
  type RuntimeTokenPayload,
} from '@/api/runtimeToken'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { humanizeApiError } from '@/lib/reason-label'

type LoadState = { status: 'loading' } | { status: 'error'; error: unknown } | { status: 'success'; data: RuntimeTokenPayload }
type Pending = { kind: 'generate' } | { kind: 'custom'; token: string } | { kind: 'reset' } | null

const SOURCE_LABEL: Record<RuntimeTokenPayload['source'], string> = {
  default: '公开默认值',
  database: '管理台设置',
  env: '环境变量',
}

function formatTime(ts: number | null | undefined): string {
  if (!ts) return ''
  try {
    return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return ''
  }
}

function UsageLine({ label, count, at, from, path, tone }: {
  label: string
  count: number
  at: number | null
  from: string
  path: string
  tone?: 'warn'
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs">
      <span className="w-14 shrink-0 text-muted-foreground">{label}</span>
      <span className={tone === 'warn' && count ? 'font-semibold text-destructive' : 'font-semibold'}>{count} 次</span>
      {at ? (
        <span className="text-muted-foreground">
          最近 {formatTime(at)}{from ? ` · 来自 ${from}` : ''}{path ? <span className="font-mono"> · {path}</span> : null}
        </span>
      ) : null}
    </div>
  )
}

export function RuntimeTokenPanel() {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [revealed, setRevealed] = useState(false)
  const [copied, setCopied] = useState(false)
  const [custom, setCustom] = useState('')
  const [editing, setEditing] = useState(false)
  const [pending, setPending] = useState<Pending>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await getRuntimeToken(signal)
      setState({ status: 'success', data })
    } catch (error) {
      if (!signal?.aborted) setState({ status: 'error', error })
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('复制失败，请手动选中复制')
    }
  }

  const apply = async () => {
    if (!pending) return
    const change: RuntimeTokenChange = pending.kind === 'custom'
      ? { token: pending.token }
      : { action: pending.kind }
    setSaving(true)
    try {
      const data = await changeRuntimeToken(change)
      setState({ status: 'success', data })
      setPending(null)
      setEditing(false)
      setCustom('')
      setRevealed(pending.kind !== 'reset')
      toast.success(pending.kind === 'reset' ? '已恢复为公开默认令牌' : '新令牌已生效，记得同步到调用方')
    } catch (error) {
      toast.error(humanizeApiError(error, '保存令牌'))
    } finally {
      setSaving(false)
    }
  }

  if (state.status === 'loading') return <Skeleton className="h-56 w-full" />
  if (state.status === 'error') {
    return (
      <Alert variant="destructive">
        <AlertTriangleIcon />
        <AlertTitle>读取 Runtime 令牌失败</AlertTitle>
        <AlertDescription>
          {humanizeApiError(state.error, 'Runtime 令牌')}
          <Button type="button" size="sm" variant="outline" className="mt-2 w-fit" onClick={() => void load()}>重试</Button>
        </AlertDescription>
      </Alert>
    )
  }

  const data = state.data
  const { usage } = data
  const customTooShort = custom.trim().length > 0 && custom.trim().length < data.min_length

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="gap-2">
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            <KeyRoundIcon className="size-4" aria-hidden="true" />
            Runtime API 令牌
            <Badge variant={data.is_default ? 'destructive' : 'secondary'}>{SOURCE_LABEL[data.source]}</Badge>
          </CardTitle>
          <CardDescription>
            外部程序（如 Cortico 直播）调用 <span className="font-mono">/api/runtime/v1</span> 时要带上这个令牌：
            直播记忆注入、弹幕与游戏事件写回、记忆工具、存 / 查 / 删记忆都经过它。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {data.is_default ? (
            <Alert variant="destructive">
              <AlertTriangleIcon />
              <AlertTitle>正在使用公开的默认令牌</AlertTitle>
              <AlertDescription>
                这个值写在开源代码里，任何能访问 9876 端口的人都能用它读写、删除记忆。
                9876 只在本机或隧道里可达时风险不大；对外开放前务必换一个。
              </AlertDescription>
            </Alert>
          ) : null}
          {data.env_set ? (
            <Alert>
              <AlertTitle>由环境变量 <span className="font-mono">{data.env_var}</span> 设置</AlertTitle>
              <AlertDescription>环境变量优先，管理台只能查看。要改请修改容器环境变量后重启；删掉环境变量后才能在这里管理。</AlertDescription>
            </Alert>
          ) : null}

          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <Input
                readOnly
                aria-label="当前 Runtime 令牌"
                type={revealed ? 'text' : 'password'}
                value={data.token}
                className="font-mono text-xs"
                onFocus={(event) => event.currentTarget.select()}
              />
              <Button type="button" size="icon" variant="outline" aria-label={revealed ? '隐藏令牌' : '显示令牌'}
                onClick={() => setRevealed((value) => !value)}>
                {revealed ? <EyeOffIcon /> : <EyeIcon />}
              </Button>
              <Button type="button" size="icon" variant="outline" aria-label="复制令牌" onClick={() => void copy(data.token)}>
                {copied ? <CheckIcon /> : <CopyIcon />}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              请求头写 <span className="font-mono">Authorization: Bearer &lt;令牌&gt;</span>
              {data.updated_at ? ` · 更新于 ${formatTime(data.updated_at)}` : ''}
            </p>
          </div>

          {data.can_edit ? (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" onClick={() => setPending({ kind: 'generate' })}>
                  <RefreshCwIcon />生成新令牌
                </Button>
                <Button type="button" size="sm" variant="outline" onClick={() => setEditing((value) => !value)}>
                  {editing ? '取消自定义' : '自定义…'}
                </Button>
                {data.source === 'database' ? (
                  <Button type="button" size="sm" variant="ghost" onClick={() => setPending({ kind: 'reset' })}>
                    恢复默认
                  </Button>
                ) : null}
              </div>
              {editing ? (
                <form
                  className="flex flex-col gap-2 sm:flex-row sm:items-start"
                  onSubmit={(event) => {
                    event.preventDefault()
                    const token = custom.trim()
                    if (token.length >= data.min_length) setPending({ kind: 'custom', token })
                  }}
                >
                  <div className="flex-1 space-y-1">
                    <Input
                      aria-label="自定义令牌"
                      value={custom}
                      onChange={(event) => setCustom(event.target.value)}
                      placeholder={`至少 ${data.min_length} 个字符，不含空白`}
                      className="font-mono text-xs"
                      autoComplete="off"
                    />
                    {customTooShort ? <p className="text-xs text-destructive">还差 {data.min_length - custom.trim().length} 个字符</p> : null}
                  </div>
                  <Button type="submit" size="sm" disabled={custom.trim().length < data.min_length || /\s/.test(custom.trim())}>
                    保存
                  </Button>
                </form>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2">
          <div>
            <CardTitle className="text-sm">调用情况</CardTitle>
            <CardDescription>本次启动以来；用来确认调用方有没有换上新令牌。</CardDescription>
          </div>
          <Button type="button" size="sm" variant="ghost" onClick={() => void load()}>
            <RefreshCwIcon />刷新
          </Button>
        </CardHeader>
        <CardContent className="space-y-1.5">
          <UsageLine label="通过" count={usage.accepted} at={usage.last_accepted_at}
            from={usage.last_accepted_from} path={usage.last_accepted_path} />
          <UsageLine label="被拒" count={usage.rejected} at={usage.last_rejected_at}
            from={usage.last_rejected_from} path={usage.last_rejected_path} tone="warn" />
          {usage.rejected && usage.last_rejected_at && (!usage.last_accepted_at || usage.last_rejected_at > usage.last_accepted_at) ? (
            <p className="pt-1 text-xs text-destructive">最近一次请求被拒：有调用方还在用旧令牌，或者没带令牌。</p>
          ) : null}
        </CardContent>
      </Card>

      <Dialog open={pending !== null} onOpenChange={(open) => { if (!open && !saving) setPending(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{pending?.kind === 'reset' ? '恢复为公开默认令牌？' : '更换 Runtime 令牌？'}</DialogTitle>
            <DialogDescription>
              {pending?.kind === 'reset'
                ? '会改回写在开源代码里的默认值，任何能访问 9876 的人都能用它。'
                : '保存后立即生效，旧令牌马上失效。'}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 text-sm">
            <p>所有调用方都要同步改成新值，否则会立刻被拒（例如直播时 Bot 会拿不到记忆）。</p>
            <p className="text-muted-foreground">
              用 Cortico 接入的：<span className="font-mono">deployments/&lt;部署名&gt;/config.json</span> 里的
              <span className="font-mono"> wavememory.authToken</span>，改完重启 Cortico。
            </p>
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={saving}>取消</Button>
            </DialogClose>
            <Button type="button" variant={pending?.kind === 'reset' ? 'destructive' : 'default'} disabled={saving} onClick={() => void apply()}>
              {saving ? '保存中…' : pending?.kind === 'reset' ? '恢复默认' : '确认更换'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
