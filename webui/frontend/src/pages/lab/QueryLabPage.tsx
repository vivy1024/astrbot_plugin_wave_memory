import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { FlaskConicalIcon, Loader2Icon, PlayIcon, RotateCcwIcon, ShieldCheckIcon } from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { runQueryDebug, type QueryDebugResponse, type QueryStageName } from '@/api/memories'
import { getScopeOptions, scopeOptionsFor } from '@/api/options'
import { ScopeSelect } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { humanizeApiError } from '@/lib/reason-label'
import {
  QUERY_PARAM_SPECS,
  QUERY_STAGE_HINTS,
  QUERY_STAGE_LABELS,
  QUERY_STAGE_NAMES,
  STAGE_OUTCOME_LABELS,
  compareQueryRuns,
  diffStages,
  type QueryDebugParamsMap,
  type StageOutcome,
} from '@/lib/query-lab'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'

interface ArmConfig {
  stages: Record<QueryStageName, boolean>
  params: Partial<QueryDebugParamsMap>
}

function defaultArm(): ArmConfig {
  return {
    stages: { epa: true, pyramid: true, spike: true, geodesic: true },
    params: {},
  }
}

const OUTCOME_CLASS: Record<StageOutcome, string> = {
  ok: 'text-emerald-600',
  disabled: 'text-muted-foreground',
  degraded: 'text-amber-600',
  not_reported: 'text-muted-foreground',
}

export function QueryLabPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  const pagination = usePaginationSearchParams()
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })

  const [text, setText] = useState('')
  const [topK, setTopK] = useState(5)
  const [armA, setArmA] = useState<ArmConfig>(defaultArm)
  const [armB, setArmB] = useState<ArmConfig>(() => ({
    stages: { epa: false, pyramid: true, spike: false, geodesic: true },
    params: {},
  }))
  const [resultA, setResultA] = useState<QueryDebugResponse | null>(null)
  const [resultB, setResultB] = useState<QueryDebugResponse | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<unknown>()

  const scope = useMemo(
    () => (botId && sessionId ? { bot_id: botId, session_id: sessionId, visibility: 'group' as const } : null),
    [botId, sessionId],
  )

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    const options = scopeOptionsFor(await getScopeOptions(), ['session'])
    return botId ? options.filter((option) => option.description?.startsWith(`${botId} ·`)) : options
  }, [botId])

  const setScope = (changes: Record<string, string | null>) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      Object.entries(changes).forEach(([key, value]) => { if (value) next.set(key, value); else next.delete(key) })
      return next
    })
  }

  const updateArm = (arm: 'a' | 'b') => (patch: (current: ArmConfig) => ArmConfig) => {
    const setter = arm === 'a' ? setArmA : setArmB
    setter((current) => patch(current))
  }

  const run = async () => {
    if (!text.trim() || !scope || running) return
    setRunning(true)
    setError(undefined)
    try {
      // 两组串行执行：同一 scope 下并发触发两次全管线检索会互相争抢热索引，
      // 也会让耗时对比失去意义。
      const a = await runQueryDebug({ text: text.trim(), topK, scope, stages: armA.stages, params: armA.params })
      const b = await runQueryDebug({ text: text.trim(), topK, scope, stages: armB.stages, params: armB.params })
      setResultA(a)
      setResultB(b)
    } catch (failure) {
      if (isRequestCancelled(failure)) return
      setResultA(null)
      setResultB(null)
      setError(failure)
    } finally {
      setRunning(false)
    }
  }

  const reset = () => {
    setResultA(null)
    setResultB(null)
    setError(undefined)
  }

  const comparison = useMemo(
    () => (resultA && resultB ? compareQueryRuns(resultA, resultB) : null),
    [resultA, resultB],
  )
  const stageDiffs = useMemo(
    () => (resultA && resultB ? diffStages(resultA, resultB) : null),
    [resultA, resultB],
  )

  return (
    <div className="flex flex-col gap-3" data-page="query-lab">
      <Alert>
        <ShieldCheckIcon aria-hidden="true" />
        <AlertTitle>只读检索实验台</AlertTitle>
        <AlertDescription>用正式 QueryEngine 跑真实检索，不写入、不累加访问计数、不改任何记忆。同一句查询跑两组算法配置，并排看差异。</AlertDescription>
      </Alert>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm"><FlaskConicalIcon className="size-4" />查询与作用域</CardTitle>
          <CardDescription>先选 Bot 与群，再输入一句真实的查询。结果会让你看到算法开关带来的差异。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="w-52 shrink-0">
            <ScopeSelect
              value={botId || undefined}
              loadOptions={loadBots}
              label="Bot"
              onValueChange={(value) => setScope({ bot_id: value, session_id: null })}
            />
          </div>
          <div className="w-56 shrink-0">
            <ScopeSelect
              value={sessionId || undefined}
              loadOptions={loadSessions}
              label="群 / 会话"
              disabled={!botId}
              onValueChange={(value) => setScope({ session_id: value })}
            />
          </div>
          <div className="min-w-[240px] flex-1">
            <Field>
              <FieldLabel htmlFor="query-lab-text">查询内容</FieldLabel>
              <Input
                id="query-lab-text"
                value={text}
                placeholder="例如：上次说的那件事后来怎样了"
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') void run() }}
              />
            </Field>
          </div>
          <div className="w-24 shrink-0">
            <Field>
              <FieldLabel htmlFor="query-lab-topk">返回条数</FieldLabel>
              <Input
                id="query-lab-topk"
                type="number"
                min={1}
                max={50}
                value={topK}
                onChange={(event) => setTopK(Math.max(1, Math.min(50, Number(event.target.value) || 1)))}
              />
            </Field>
          </div>
          <div className="flex gap-2">
            <Button type="button" size="sm" disabled={!text.trim() || !scope || running} onClick={() => void run()}>
              {running ? <Loader2Icon className="size-3.5 animate-spin" /> : <PlayIcon className="size-3.5" />}
              {running ? '运行中' : '对比运行'}
            </Button>
            <Button type="button" size="sm" variant="outline" disabled={running || (!resultA && !resultB)} onClick={reset}>
              <RotateCcwIcon className="size-3.5" />清空结果
            </Button>
          </div>
          {!scope ? <p className="w-full text-xs text-muted-foreground">需要先选好 Bot 与群，检索只在该作用域内进行。</p> : null}
        </CardContent>
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        {(['a', 'b'] as const).map((arm) => {
          const config = arm === 'a' ? armA : armB
          const update = updateArm(arm)
          const result = arm === 'a' ? resultA : resultB
          return (
            <Card key={arm}>
              <CardHeader>
                <CardTitle className="text-sm">{arm.toUpperCase()} 组算法配置</CardTitle>
                <CardDescription>{result ? `耗时 ${Math.round(Number(result.timing?.total_ms ?? 0))}ms · 命中 ${result.results.length} 条` : '尚未运行'}</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <div className="grid gap-2">
                  {QUERY_STAGE_NAMES.map((stage) => (
                    <div key={stage} className="flex items-center justify-between gap-3 rounded-md border p-2">
                      <div className="min-w-0">
                        <p className="text-xs font-medium">{QUERY_STAGE_LABELS[stage]}</p>
                        <p className="text-[11px] text-muted-foreground">{QUERY_STAGE_HINTS[stage]}</p>
                      </div>
                      <Switch
                        checked={config.stages[stage]}
                        aria-label={`${arm.toUpperCase()} 组 ${QUERY_STAGE_LABELS[stage]}`}
                        onCheckedChange={(checked) => update((current) => ({
                          ...current,
                          stages: { ...current.stages, [stage]: checked },
                        }))}
                      />
                    </div>
                  ))}
                </div>
                <div className="grid gap-2">
                  {QUERY_PARAM_SPECS.map((spec) => {
                    const value = config.params[spec.name]
                    return (
                      <Field key={spec.name}>
                        <FieldLabel htmlFor={`${arm}-${spec.name}`} className="text-xs">
                          {spec.label}
                          <span className="ml-2 font-mono text-[11px] text-muted-foreground">
                            {value === undefined ? '默认' : value}
                          </span>
                        </FieldLabel>
                        <Input
                          id={`${arm}-${spec.name}`}
                          type="number"
                          min={spec.min}
                          max={spec.max}
                          step={spec.step}
                          placeholder="留空用默认"
                          value={value === undefined ? '' : String(value)}
                          onChange={(event) => {
                            const raw = event.target.value
                            update((current) => {
                              const params = { ...current.params }
                              if (raw === '') delete params[spec.name]
                              else {
                                const parsed = Number(raw)
                                if (!Number.isFinite(parsed)) delete params[spec.name]
                                else {
                                  const clamped = Math.max(spec.min, Math.min(spec.max, parsed))
                                  params[spec.name] = spec.integer ? Math.round(clamped) : clamped
                                }
                              }
                              return { ...current, params }
                            })
                          }}
                        />
                        <FieldDescription className="text-[11px]">范围 {spec.min}–{spec.max} · {spec.hint}</FieldDescription>
                      </Field>
                    )
                  })}
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>

      {error ? (
        <Alert variant="destructive">
          <AlertTitle>检索失败</AlertTitle>
          <AlertDescription>{humanizeApiError(error, '请检查作用域与查询内容后重试。')}</AlertDescription>
        </Alert>
      ) : null}

      {stageDiffs ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">阶段差异</CardTitle>
            <CardDescription>两组算法各阶段的启用与降级情况。降级通常意味着该阶段缺少所需索引。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {stageDiffs.map((row) => (
              <div key={row.stage} className="rounded-md border p-2">
                <p className="text-xs font-medium">{row.label}</p>
                <div className="mt-1 flex items-center gap-2 text-[11px]">
                  <span className={OUTCOME_CLASS[row.outcomeA]}>{STAGE_OUTCOME_LABELS[row.outcomeA]}</span>
                  <span className="text-muted-foreground">→</span>
                  <span className={OUTCOME_CLASS[row.outcomeB]}>{STAGE_OUTCOME_LABELS[row.outcomeB]}</span>
                </div>
                {row.changed ? <Badge variant="outline" className="mt-1 text-[10px]">有变化</Badge> : null}
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      {comparison ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">结果对比</CardTitle>
            <CardDescription>
              共同命中 {comparison.summary.common} · A 独有 {comparison.summary.aOnly} · B 独有 {comparison.summary.bOnly} · 重合度 {comparison.summary.overlapRatio}%
              {comparison.summary.sameOrder ? ' · 排序完全一致' : comparison.summary.sameSet ? ' · 命中集合相同但排序不同' : ''}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {comparison.rows.length ? comparison.rows.map((row) => (
              <div key={row.id} className="rounded-md border p-2">
                <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono text-muted-foreground">
                  <span>#{row.id}</span>
                  <Badge variant="outline" className="text-[10px]">
                    {row.type === 'common' ? '共同' : row.type === 'a_only' ? '仅 A' : '仅 B'}
                  </Badge>
                  <span>A {row.rankA ? `#${row.rankA}` : '未命中'}{row.similarityA !== null ? ` · ${row.similarityA}` : ''}</span>
                  <span>B {row.rankB ? `#${row.rankB}` : '未命中'}{row.similarityB !== null ? ` · ${row.similarityB}` : ''}</span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed">{row.content}</p>
              </div>
            )) : <p className="py-3 text-center text-xs text-muted-foreground">两组都没有命中任何记忆。</p>}
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
