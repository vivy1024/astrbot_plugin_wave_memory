import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCwIcon } from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { getConfigInventory, type ConfigInventoryPayload, type ConfigLayer } from '@/api/configInventory'
import { QueryState } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

const LAYER_LABELS: Record<ConfigLayer, string> = {
  builtin: '内置默认',
  static: 'AstrBot 静态配置',
  bot: 'Bot Profile',
  override: '9876 覆盖',
}
const APPLY_LABELS: Record<string, string> = { hot: '立即生效', next_run: '下次运行', restart: '需重启', unknown: '未知' }

function show(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value === '' ? '（空）' : value
  return JSON.stringify(value)
}

export function ConfigInventoryPage() {
  const [payload, setPayload] = useState<ConfigInventoryPayload | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [layer, setLayer] = useState<ConfigLayer | 'all'>('all')
  const [changedOnly, setChangedOnly] = useState(true)

  const load = useCallback(async (signal?: AbortSignal) => {
    setStatus('loading')
    try {
      setPayload(await getConfigInventory(signal))
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

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return (payload?.items ?? []).filter((row) =>
      (layer === 'all' || row.layer === layer)
      && (!changedOnly || row.changed || Boolean(row.warning))
      && (!needle || row.key.toLowerCase().includes(needle) || row.title.toLowerCase().includes(needle)))
  }, [payload, query, layer, changedOnly])

  const precedence = payload?.precedence ?? (['builtin', 'static', 'bot', 'override'] as ConfigLayer[])
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2">
          <div>
            <CardTitle>配置来源</CardTitle>
            <CardDescription>
              优先级从低到高：{precedence.map((item) => LAYER_LABELS[item]).join(' < ')}。每一项都能看到当前生效值从哪一层来、改了以后怎么生效。
            </CardDescription>
          </div>
          <Button type="button" variant="outline" onClick={() => void load()}><RefreshCwIcon className="size-4" />刷新</Button>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <Input aria-label="搜索配置" className="w-64" placeholder="搜索键名或说明" value={query} onChange={(event) => setQuery(event.target.value)} />
          <div className="flex flex-wrap gap-1">
            {(['all', 'builtin', 'static', 'bot', 'override'] as const).map((item) => (
              <Button key={item} type="button" size="sm" variant={layer === item ? 'default' : 'outline'} onClick={() => setLayer(item)}>
                {item === 'all' ? '全部' : LAYER_LABELS[item]}{item !== 'all' && payload?.counts[item] ? ` ${payload.counts[item]}` : ''}
              </Button>
            ))}
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Switch aria-label="只看改过的" checked={changedOnly} onCheckedChange={setChangedOnly} />只看和默认值不同的
          </label>
          {payload ? <span className="font-mono text-xs text-muted-foreground">{payload.revision}</span> : null}
        </CardContent>
      </Card>
      {payload?.suspects.length ? (
        <Alert variant="destructive">
          <AlertTitle>{payload.suspects.length} 个默认开启的开关被保存成了关闭</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-5">{payload.suspects.map((item) => <li key={item.key}>{item.message}</li>)}</ul>
          </AlertDescription>
        </Alert>
      ) : null}
      <QueryState status={status} error={error} onRetry={() => void load()}>
        <Card>
          <CardContent className="pt-6">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>配置项</TableHead>
                  <TableHead>来源</TableHead>
                  <TableHead>默认值</TableHead>
                  <TableHead>当前生效</TableHead>
                  <TableHead>生效方式</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.key}>
                    <TableCell>
                      <div className="font-mono text-xs">{row.key}</div>
                      <div className="text-xs text-muted-foreground">{row.title}{row.scope !== 'global' ? ` · ${row.scope}` : ''}</div>
                      {row.warning ? <div className="text-xs text-destructive">{row.warning}</div> : null}
                    </TableCell>
                    <TableCell><Badge variant={row.layer === 'builtin' ? 'outline' : 'default'}>{LAYER_LABELS[row.layer]}</Badge></TableCell>
                    <TableCell className="max-w-48 truncate font-mono text-xs">{show(row.default)}</TableCell>
                    <TableCell className="max-w-48 truncate font-mono text-xs">{show(row.effective)}</TableCell>
                    <TableCell className="text-xs">{APPLY_LABELS[row.apply_mode] ?? row.apply_mode}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {!rows.length ? <p className="py-6 text-center text-sm text-muted-foreground">没有符合筛选条件的配置项。</p> : null}
          </CardContent>
        </Card>
      </QueryState>
    </div>
  )
}
