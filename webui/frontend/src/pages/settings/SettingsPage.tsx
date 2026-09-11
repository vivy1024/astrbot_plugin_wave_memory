import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { humanizeApiError } from '@/lib/reason-label'
import { AlertCircleIcon, Loader2Icon, RefreshCwIcon, SaveIcon, Undo2Icon } from 'lucide-react'
import { toast } from 'sonner'

import {
  getConfigSchema,
  getHotConfig,
  listProviders,
  saveFullConfig,
  saveHotConfig,
  type ConfigApplyMode,
  type ConfigGroup,
  type ConfigItem,
  type HotParam,
  type ProviderPayload,
} from '@/api/config'
import { useUnsavedChangesGuard } from '@/app/unsaved-changes'
import { FieldValueState, QueryState, type ApplyMode } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { SettingsSection } from '@/components/ui/collapsible'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { validateNumericDraft } from '@/lib/numeric-draft'
import { changedPayload } from '@/pages/settings/settings-state'
import { groupMeta, isHiddenItem, isHiddenSection, matchesSearch } from '@/pages/settings/settings-groups'

function cloneGroups(groups: ConfigGroup[]): ConfigGroup[] {
  return typeof structuredClone === 'function'
    ? structuredClone(groups)
    : groups.map((group) => ({ ...group, items: group.items?.map((item) => ({ ...item })) }))
}

function cloneHotParams(params: HotParam[]): HotParam[] {
  return params.map((param) => ({ ...param }))
}

function applyMode(mode?: ConfigApplyMode): ApplyMode {
  return mode === 'next_run' ? 'next-run' : (mode ?? 'unknown')
}

function pathState(item: ConfigItem | ConfigGroup) {
  return {
    defaultValue: item.default,
    savedValue: item.saved,
    effectiveValue: item.effective,
    applyMode: applyMode(item.apply_mode),
    effectiveSince: item.effective_since,
  }
}

function numericBound(meta: ConfigItem | ConfigGroup, side: 'min' | 'max'): number | undefined {
  const value = side === 'min' ? (meta.min ?? meta.minimum) : (meta.max ?? meta.maximum)
  return Number.isFinite(value) ? value : undefined
}

function schemaNumericDrafts(groups: ConfigGroup[]): Record<string, string> {
  const drafts: Record<string, string> = {}
  groups.forEach((group) => {
    if (group.kind === 'object') {
      group.items?.forEach((item) => {
        if (item.type === 'int' || item.type === 'float') drafts[`${group.key}.${item.key}`] = item.value == null ? '' : String(item.value)
      })
    } else if (group.type === 'int' || group.type === 'float') {
      drafts[group.key] = group.value == null ? '' : String(group.value)
    }
  })
  return drafts
}

function hotNumericDrafts(params: HotParam[]): Record<string, string> {
  return Object.fromEntries(params.map((param) => [param.key, param.current == null ? '' : String(param.current)]))
}

function errorMessage(error: unknown, fallback: string): string {
  return humanizeApiError(error, fallback)
}

export function SettingsPage() {
  const [activeTab, setActiveTab] = useState('static')
  const [schemaGroups, setSchemaGroups] = useState<ConfigGroup[]>([])
  const [originalGroups, setOriginalGroups] = useState<ConfigGroup[]>([])
  const [schemaDrafts, setSchemaDrafts] = useState<Record<string, string>>({})
  const [schemaErrors, setSchemaErrors] = useState<Record<string, string>>({})
  const [hotParams, setHotParams] = useState<HotParam[]>([])
  const [originalHotParams, setOriginalHotParams] = useState<HotParam[]>([])
  const [hotDrafts, setHotDrafts] = useState<Record<string, string>>({})
  const [hotErrors, setHotErrors] = useState<Record<string, string>>({})
  const [providers, setProviders] = useState<ProviderPayload[]>([])
  const [warnings, setWarnings] = useState<Array<{ key: string; message: string }>>([])
  const [search, setSearch] = useState('')
  const [schemaLoading, setSchemaLoading] = useState(true)
  const [hotLoading, setHotLoading] = useState(true)
  const [providersLoading, setProvidersLoading] = useState(true)
  const [schemaError, setSchemaError] = useState<unknown>(null)
  const [hotError, setHotError] = useState<unknown>(null)
  const [providersError, setProvidersError] = useState<unknown>(null)
  const [saving, setSaving] = useState(false)
  // 请求序号：重试加载与保存后回读可能交错，只接受最后一次发出的响应。
  const schemaRequestRef = useRef(0)
  const hotRequestRef = useRef(0)
  const providersRequestRef = useRef(0)

  const loadSchema = useCallback(async () => {
    const requestId = ++schemaRequestRef.current
    setSchemaLoading(true)
    setSchemaError(null)
    try {
      const schema = await getConfigSchema()
      if (requestId !== schemaRequestRef.current) return
      const groups = cloneGroups(schema.groups ?? [])
      setSchemaGroups(groups)
      setOriginalGroups(cloneGroups(groups))
      setSchemaDrafts(schemaNumericDrafts(groups))
      setSchemaErrors({})
      setWarnings(schema.warnings ?? [])
    } catch (error) {
      if (requestId !== schemaRequestRef.current) return
      setSchemaError(error)
    } finally {
      if (requestId === schemaRequestRef.current) setSchemaLoading(false)
    }
  }, [])

  const loadHot = useCallback(async () => {
    const requestId = ++hotRequestRef.current
    setHotLoading(true)
    setHotError(null)
    try {
      const hot = await getHotConfig()
      if (requestId !== hotRequestRef.current) return
      const params = cloneHotParams(hot.params ?? [])
      setHotParams(params)
      setOriginalHotParams(cloneHotParams(params))
      setHotDrafts(hotNumericDrafts(params))
      setHotErrors({})
    } catch (error) {
      if (requestId !== hotRequestRef.current) return
      setHotError(error)
    } finally {
      if (requestId === hotRequestRef.current) setHotLoading(false)
    }
  }, [])

  const loadProviders = useCallback(async () => {
    const requestId = ++providersRequestRef.current
    setProvidersLoading(true)
    setProvidersError(null)
    try {
      const providerData = await listProviders()
      if (requestId !== providersRequestRef.current) return
      setProviders(providerData.providers ?? [])
    } catch (error) {
      if (requestId !== providersRequestRef.current) return
      setProviders([])
      setProvidersError(error)
    } finally {
      if (requestId === providersRequestRef.current) setProvidersLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSchema()
    void loadHot()
    void loadProviders()
  }, [loadHot, loadProviders, loadSchema])

  const fullPayload = useMemo(() => changedPayload(schemaGroups, originalGroups), [schemaGroups, originalGroups])
  const dirtyHot = useMemo(() => hotParams.filter((item) => {
    const old = originalHotParams.find((candidate) => candidate.key === item.key)
    return !old || !Object.is(old.current, item.current)
  }), [hotParams, originalHotParams])
  const hasSchemaErrors = Object.keys(schemaErrors).length > 0
  const hasHotErrors = Object.keys(hotErrors).length > 0
  const isDirty = Object.keys(fullPayload).length > 0 || dirtyHot.length > 0 || hasSchemaErrors || hasHotErrors
  useUnsavedChangesGuard(isDirty, '您当前有未保存的系统配置修改，离开后这些草稿将丢失。')

  function updateScalar(groupKey: string, value: unknown) {
    setSchemaGroups((groups) => groups.map((group) => group.key === groupKey ? { ...group, value } : group))
  }

  function updateItem(groupKey: string, itemKey: string, value: unknown) {
    setSchemaGroups((groups) => groups.map((group) => group.key === groupKey
      ? { ...group, items: group.items?.map((item) => item.key === itemKey ? { ...item, value } : item) }
      : group))
  }

  function setValidationError(setter: typeof setSchemaErrors, key: string, message: string | null) {
    setter((current) => {
      const next = { ...current }
      if (message) next[key] = message
      else delete next[key]
      return next
    })
  }

  function updateSchemaNumeric(group: ConfigGroup, item: ConfigItem | undefined, raw: string) {
    const meta = item ?? group
    const key = item ? `${group.key}.${item.key}` : group.key
    const type = item?.type ?? group.type
    const label = item?.description ?? group.description
    const result = validateNumericDraft(raw, {
      label,
      integer: type === 'int',
      min: numericBound(meta, 'min'),
      max: numericBound(meta, 'max'),
    })
    setSchemaDrafts((current) => ({ ...current, [key]: raw }))
    setValidationError(setSchemaErrors, key, result.error)
    if (result.value !== null) {
      if (item) updateItem(group.key, item.key, result.value)
      else updateScalar(group.key, result.value)
    }
  }

  function updateHotNumeric(param: HotParam, raw: string) {
    const result = validateNumericDraft(raw, {
      label: param.description,
      integer: param.type === 'int',
      min: param.min,
      max: param.max,
    })
    setHotDrafts((current) => ({ ...current, [param.key]: raw }))
    setValidationError(setHotErrors, param.key, result.error)
    if (result.value !== null) setHotParams((items) => items.map((item) => item.key === param.key ? { ...item, current: result.value as number } : item))
  }

  function discardSchema() {
    const groups = cloneGroups(originalGroups)
    setSchemaGroups(groups)
    setSchemaDrafts(schemaNumericDrafts(groups))
    setSchemaErrors({})
  }

  function discardHot() {
    const params = cloneHotParams(originalHotParams)
    setHotParams(params)
    setHotDrafts(hotNumericDrafts(params))
    setHotErrors({})
  }

  async function saveSchema() {
    if (hasSchemaErrors) {
      toast.error('请先修正配置中的数值错误')
      return
    }
    setSaving(true)
    try {
      const response = await saveFullConfig(fullPayload)
      if (!response.ok) throw new Error(response.errors?.join('；') || response.error || '配置保存失败')
      toast.success(response.message || '配置已保存；运行时是否生效以回读值为准。')
      const schema = response.schema ?? await getConfigSchema()
      const groups = cloneGroups(schema.groups ?? [])
      setSchemaGroups(groups)
      setOriginalGroups(cloneGroups(groups))
      setSchemaDrafts(schemaNumericDrafts(groups))
      setSchemaErrors({})
      setWarnings(schema.warnings ?? [])
    } catch (error) {
      toast.error(errorMessage(error, '配置保存失败'))
    } finally {
      setSaving(false)
    }
  }

  async function saveHot() {
    if (hasHotErrors) {
      toast.error('请先修正热参数中的数值错误')
      return
    }
    setSaving(true)
    try {
      const payload = Object.fromEntries(dirtyHot.map((item) => [item.key, item.current]))
      const response = await saveHotConfig(payload)
      if (!response.ok) throw new Error(response.errors.join('；') || '热参数应用失败')
      toast.success(response.message || '热参数已应用；持久化状态以字段回读为准。')
      if (response.warnings?.length) toast.warning(response.warnings.join('；'))
      const nextParams = response.params ?? (await getHotConfig()).params ?? []
      const params = cloneHotParams(nextParams)
      setHotParams(params)
      setOriginalHotParams(cloneHotParams(params))
      setHotDrafts(hotNumericDrafts(params))
      setHotErrors({})
    } catch (error) {
      toast.error(errorMessage(error, '热参数应用失败'))
    } finally {
      setSaving(false)
    }
  }

  function providerEditor(value: unknown, setValue: (next: unknown) => void) {
    if (providersLoading) return <Input aria-label="Provider 选项状态" disabled value="正在加载 Provider 选项…" />
    if (providersError) {
      return (
        <div className="flex flex-col gap-2" role="alert">
          <Input aria-label="Provider 选项状态" disabled value="Provider 选项当前不可用" />
          <div className="flex items-center justify-between gap-3 text-xs text-destructive">
            <span>{errorMessage(providersError, 'Provider 列表加载失败')}；主配置仍可编辑和保存。</span>
            <Button type="button" size="sm" variant="outline" onClick={() => void loadProviders()}><RefreshCwIcon />重试 Provider</Button>
          </div>
        </div>
      )
    }
    if (!providers.length) return <Input aria-label="Provider 选项状态" disabled value="当前没有可用 Provider" />
    const currentValue = String(value ?? '')
    const options = providers.some((provider) => provider.id === currentValue) || !currentValue
      ? providers
      : [{ id: currentValue, name: currentValue, model: '当前已配置，未出现在选项列表' }, ...providers]
    return (
      <Select value={currentValue} onValueChange={setValue}>
        <SelectTrigger><SelectValue placeholder="请选择 Provider" /></SelectTrigger>
        <SelectContent>{options.map((provider) => <SelectItem key={provider.id} value={provider.id}>{provider.id} ({provider.model})</SelectItem>)}</SelectContent>
      </Select>
    )
  }

  function editor(group: ConfigGroup, item?: ConfigItem) {
    const value = item ? item.value : group.value
    const type = item?.type ?? group.type
    const special = item?.special ?? group.special
    const key = item ? `${group.key}.${item.key}` : group.key
    const setValue = (next: unknown) => item ? updateItem(group.key, item.key, next) : updateScalar(group.key, next)
    if (type === 'bool') return <Switch checked={value === true} onCheckedChange={setValue} aria-label={`修改 ${item?.description ?? group.description}`} />
    if (special === 'select_provider') return providerEditor(value, setValue)
    if (type === 'int' || type === 'float') {
      const meta = item ?? group
      const error = schemaErrors[key]
      return (
        <div className="space-y-1.5">
          <Input
            aria-label={`修改 ${item?.description ?? group.description}`}
            aria-invalid={Boolean(error)}
            type="number"
            min={numericBound(meta, 'min')}
            max={numericBound(meta, 'max')}
            step={type === 'float' ? 'any' : 1}
            value={schemaDrafts[key] ?? ''}
            onChange={(event) => updateSchemaNumeric(group, item, event.target.value)}
          />
          {error ? <p className="text-xs text-destructive" role="alert">{error}</p> : null}
        </div>
      )
    }
    return <Input type="text" value={value == null ? '' : String(value)} onChange={(event) => setValue(event.target.value)} />
  }

  // 分组展开状态：默认按「常用组」展开，用户的手动开合覆盖默认值。
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({})
  const toggleSection = useCallback((key: string, next: boolean) => {
    setOpenSections((current) => ({ ...current, [key]: next }))
  }, [])

  const visibleGroups = useMemo(() => {
    const term = search.trim().toLowerCase()
    // 搜索时忽略 tab 与折叠过滤：用户已经明确表达了要找什么。
    const searching = term.length > 0
    return schemaGroups.filter((group) => !isHiddenSection(group.key)).map((group) => {
      const meta = groupMeta(group.key)
      const allItems = group.kind === 'object'
        ? (group.items ?? []).filter((item) => !isHiddenItem(group.key, item.key))
        : []
      if (!matchesSearch(meta, group.key, group, allItems, term)) return null
      if (searching) return group
      const mode = activeTab === 'restart' ? 'restart' : activeTab === 'static' ? 'next_run' : null
      if (group.kind === 'object') {
        const items = allItems.filter((item) => !mode || item.apply_mode === mode)
        return items.length ? { ...group, items } : null
      }
      return !mode || group.apply_mode === mode ? group : null
    }).filter((group): group is ConfigGroup => group !== null).map((group) => {
      const meta = groupMeta(group.key)
      const itemCount = group.kind === 'object' ? (group.items?.length ?? 0) : 1
      return { group, meta, itemCount }
    })
  }, [activeTab, schemaGroups, search])

  const schemaRegion = schemaLoading ? <Skeleton className="h-72 w-full" /> : schemaError ? (
    <Alert variant="destructive"><AlertCircleIcon /><AlertTitle>配置 schema 加载失败</AlertTitle><AlertDescription className="flex items-center justify-between gap-3"><span>{errorMessage(schemaError, '配置 schema 加载失败')}</span><Button size="sm" variant="outline" onClick={() => void loadSchema()}>重试</Button></AlertDescription></Alert>
  ) : (
    <>
      <Card>
        <CardContent className="flex flex-wrap items-center justify-between gap-3 pt-6">
          <Input className="max-w-md" placeholder="搜索配置键、名称或说明" value={search} onChange={(event) => setSearch(event.target.value)} />
          <div className="flex gap-2"><Button variant="outline" onClick={discardSchema} disabled={saving}><Undo2Icon />放弃修改</Button><Button onClick={() => void saveSchema()} disabled={saving || !Object.keys(fullPayload).length || hasSchemaErrors}>{saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存配置</Button></div>
        </CardContent>
      </Card>
      {!visibleGroups.length ? <QueryState status="empty" title="没有匹配的配置项" description="请调整搜索词或切换配置分类。" /> : visibleGroups.map(({ group, meta, itemCount }) => {
        // 搜索时强制展开，否则用户会以为没有命中；其余情况用用户的开合状态覆盖默认值。
        const searching = search.trim().length > 0
        const open = searching || (openSections[group.key] ?? meta.defaultOpen)
        return (
          <SettingsSection
            key={group.key}
            title={meta.title}
            count={itemCount}
            open={open}
            onOpenChange={(next) => toggleSection(group.key, next)}
          >
            <p className="mb-3 text-xs text-muted-foreground">{meta.description}</p>
            <div className="space-y-3">
              {group.kind === 'object' ? (group.items ?? []).map((item) => (
                <div key={item.key} className="grid gap-3 rounded-lg border p-3 xl:grid-cols-[minmax(16rem,0.8fr)_minmax(0,1.2fr)]">
                  <Field>
                    <div className="flex items-center justify-between gap-3">
                      <FieldLabel>{item.description}</FieldLabel>
                      <div className="flex gap-1.5">
                        {item.apply_mode === 'hot' ? null : item.restart_required
                          ? <Badge variant="secondary">需重启</Badge>
                          : <Badge variant="outline">保存即生效</Badge>}
                      </div>
                    </div>
                    {editor(group, item)}
                    <FieldDescription>
                      {item.hint}
                      {item.error ? <span className="text-destructive"> {item.error}</span> : null}
                    </FieldDescription>
                  </Field>
                  <FieldValueState label={item.description} {...pathState(item)} />
                </div>
              )) : (
                <div className="grid gap-3 xl:grid-cols-[minmax(16rem,0.8fr)_minmax(0,1.2fr)]">
                  <Field>
                    <div className="flex items-center justify-between gap-3">
                      <FieldLabel>{group.description}</FieldLabel>
                      <div className="flex gap-1.5">
                        {group.apply_mode === 'hot' ? null : group.restart_required
                          ? <Badge variant="secondary">需重启</Badge>
                          : <Badge variant="outline">保存即生效</Badge>}
                      </div>
                    </div>
                    {editor(group)}
                    <FieldDescription>
                      {group.hint}
                      {group.error ? <span className="text-destructive"> {group.error}</span> : null}
                    </FieldDescription>
                  </Field>
                  <FieldValueState label={group.description} {...pathState(group)} />
                </div>
              )}
            </div>
          </SettingsSection>
        )
      })}
    </>
  )

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle>系统配置</CardTitle><CardDescription className="mt-2">分别展示默认值、已保存值和当前生效值；保存成功不代表运行时已经生效。</CardDescription></div>{isDirty ? <Badge variant="secondary">有未保存修改</Badge> : null}</div></CardHeader>
      </Card>

      {warnings.length ? <Alert><AlertCircleIcon /><AlertTitle>旧配置兼容诊断</AlertTitle><AlertDescription><ul className="list-disc space-y-1 pl-5">{warnings.map((item) => <li key={item.key}><span className="font-mono">{item.key}</span>：{item.message}</li>)}</ul></AlertDescription></Alert> : null}

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="grid h-auto w-full grid-cols-2 gap-1 sm:grid-cols-4">
          <TabsTrigger value="static">保存即生效</TabsTrigger><TabsTrigger value="hot">实时热参数</TabsTrigger><TabsTrigger value="restart">需重启参数</TabsTrigger><TabsTrigger value="advanced">全部高级设置</TabsTrigger>
        </TabsList>

        <TabsContent value="hot" className="mt-4">
          {hotLoading ? <Skeleton className="h-72 w-full" /> : hotError ? (
            <Alert variant="destructive"><AlertCircleIcon /><AlertTitle>热参数加载失败</AlertTitle><AlertDescription className="flex items-center justify-between gap-3"><span>{errorMessage(hotError, '热参数加载失败')}</span><Button size="sm" variant="outline" onClick={() => void loadHot()}>重试</Button></AlertDescription></Alert>
          ) : (
            <Card>
              <CardHeader><CardTitle>实时热参数</CardTitle><CardDescription>只有服务端回读的 effective 才表示当前进程实际使用；无持久化映射时会明确提示。</CardDescription></CardHeader>
              <CardContent className="space-y-4">
                <div className="flex justify-end gap-2"><Button variant="outline" onClick={discardHot} disabled={saving}><Undo2Icon />放弃修改</Button><Button onClick={() => void saveHot()} disabled={saving || !dirtyHot.length || hasHotErrors}>{saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}应用热参数</Button></div>
                {hotParams.map((param) => {
                  const error = hotErrors[param.key]
                  return (
                    <div key={param.key} className="grid gap-4 rounded-lg border p-4 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,1fr)]">
                      <Field><FieldLabel>{param.description}</FieldLabel><Input aria-label={`修改 ${param.description}`} aria-invalid={Boolean(error)} type="number" min={param.min} max={param.max} step={param.type === 'int' ? 1 : 'any'} value={hotDrafts[param.key] ?? ''} onChange={(event) => updateHotNumeric(param, event.target.value)} />{error ? <p className="text-xs text-destructive" role="alert">{error}</p> : null}<FieldDescription>{param.key}；范围 {param.min}–{param.max}。{param.error ?? ''}</FieldDescription></Field>
                      <FieldValueState label={param.key} defaultValue={param.default} savedValue={param.saved} effectiveValue={param.effective} applyMode="hot" effectiveSince={param.effective_since} />
                    </div>
                  )
                })}
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {(['static', 'restart', 'advanced'] as const).map((tab) => <TabsContent key={tab} value={tab} className="mt-4 space-y-4">{schemaRegion}</TabsContent>)}
      </Tabs>
    </div>
  )
}
