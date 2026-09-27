import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { AlertCircleIcon, ChevronRightIcon, PencilIcon, RefreshCwIcon, ShieldAlertIcon, XIcon } from 'lucide-react'

import { getConfigSchema, getHotConfig, type ConfigGroup, type HotParam } from '@/api/config'
import { getConfigInventory, type ConfigInventoryPayload } from '@/api/configInventory'
import { InputWithIcon, QueryState } from '@/components/shared'
import { Alert, AlertAction, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { humanizeApiError } from '@/lib/reason-label'
import { cn } from '@/lib/utils'
import { ChannelConfigPage } from '@/pages/channels/ChannelConfigPage'
import {
  APPLY_LABELS,
  LAYER_LABELS,
  buildConfigEntries,
  displayValue,
  type ConfigEntry,
} from '@/pages/config/config-catalog'
import { legacyBotIdsFrom, locateConfig, locationHref, type ConfigLocation } from '@/pages/config/config-location'
import { groupByFeature, searchConfig, type FeatureBucket } from '@/pages/config/feature-groups'
import { RISK_KEYS, assessRisks, type RiskAssessment, type RiskStatus } from '@/pages/config/risk'
import { ConfigInventoryPage } from '@/pages/settings/ConfigInventoryPage'
import { SettingsPage } from '@/pages/settings/SettingsPage'

type View = 'feature' | 'risk' | 'settings' | 'channels' | 'inventory'
const VIEWS: View[] = ['feature', 'risk', 'settings', 'channels', 'inventory']

function parseView(raw: string | null): View {
  return VIEWS.includes(raw as View) ? (raw as View) : 'feature'
}

type LoadState<T> = { status: 'loading' } | { status: 'error'; error: unknown } | { status: 'success'; data: T }

interface CenterData {
  schema: LoadState<ConfigGroup[]>
  hot: LoadState<HotParam[]>
  inventory: LoadState<ConfigInventoryPayload>
}

const LOADING: CenterData = { schema: { status: 'loading' }, hot: { status: 'loading' }, inventory: { status: 'loading' } }

function settle<T>(result: PromiseSettledResult<T>): LoadState<T> {
  return result.status === 'fulfilled' ? { status: 'success', data: result.value } : { status: 'error', error: result.reason }
}

function dataOf<T>(state: LoadState<T>): T | null {
  return state.status === 'success' ? state.data : null
}

// ---------------------------------------------------------------- 条目行

type Locate = (location: ConfigLocation) => void

function applyBadge(entry: ConfigEntry) {
  if (entry.restartRequired || entry.applyMode === 'restart') return <Badge variant="secondary">需重启</Badge>
  return <Badge variant="outline">{APPLY_LABELS[entry.applyMode] ?? entry.applyMode}</Badge>
}

function EntryRow({ entry, location, onLocate, groupTitle, risky }: {
  entry: ConfigEntry
  location: ConfigLocation
  onLocate: Locate
  groupTitle?: string
  risky?: boolean
}) {
  return (
    <div className="grid gap-2 rounded-lg border p-3 md:grid-cols-[minmax(0,1fr)_minmax(10rem,16rem)_auto] md:items-center">
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium">{entry.title}</span>
          {groupTitle ? <Badge variant="outline">{groupTitle}</Badge> : null}
          {risky ? <Badge variant="destructive"><ShieldAlertIcon />高风险</Badge> : null}
        </div>
        <div className="truncate font-mono text-xs text-muted-foreground" title={entry.key}>{entry.key}</div>
        {entry.hint ? <p className="line-clamp-2 text-xs text-muted-foreground">{entry.hint}</p> : null}
        {entry.mirrorOf ? <p className="text-xs text-muted-foreground">与 <span className="font-mono">{entry.mirrorOf}</span> 共用存储，任一处修改都会改到同一个值。</p> : null}
        {entry.error ? <p className="text-xs text-destructive">{entry.error}</p> : null}
      </div>
      <div className="min-w-0 space-y-1">
        <div className="truncate font-mono text-xs" title={displayValue(entry.key, entry.effectiveValue)}>
          {displayValue(entry.key, entry.effectiveValue)}
        </div>
        <div className="flex flex-wrap gap-1">
          {entry.layer ? <Badge variant={entry.layer === 'builtin' ? 'outline' : 'secondary'}>{LAYER_LABELS[entry.layer]}</Badge> : null}
          {entry.changed ? <Badge variant="default">已改</Badge> : null}
          {applyBadge(entry)}
        </div>
      </div>
      <div className="flex flex-col items-start gap-1 md:items-end">
        <Button type="button" size="sm" variant="outline" aria-label={`修改 ${entry.key}`} onClick={() => onLocate(location)}>
          <PencilIcon />修改
        </Button>
        <span className="text-xs text-muted-foreground">{location.label}</span>
      </div>
      {location.note ? <p className="text-xs text-muted-foreground md:col-span-3">{location.note}</p> : null}
    </div>
  )
}

// ---------------------------------------------------------------- 按功能

function FeatureSection({ bucket, open, onOpenChange, locateOf, onLocate }: {
  bucket: FeatureBucket
  open: boolean
  onOpenChange: (open: boolean) => void
  locateOf: (entry: ConfigEntry) => ConfigLocation
  onLocate: Locate
}) {
  const { group, entries, changedCount } = bucket
  return (
    <Collapsible open={open} onOpenChange={onOpenChange} className="rounded-xl border bg-card" id={`feature-${group.id}`}>
      <CollapsibleTrigger
        className="flex w-full items-start gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/40 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
        aria-label={`${open ? '收起' : '展开'} ${group.title}`}
      >
        <ChevronRightIcon className={cn('mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')} aria-hidden="true" />
        <span className="min-w-0 flex-1 space-y-1">
          <span className="block text-sm font-semibold">{group.title}</span>
          <span className="block text-xs text-muted-foreground">{group.description}</span>
        </span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {entries.length} 项{changedCount ? ` · ${changedCount} 项已改` : ''}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="space-y-3 border-t px-4 py-3">
          {group.symptoms.length || group.links?.length ? (
            <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
              {group.symptoms.length ? <span>常见问题：</span> : null}
              {group.symptoms.map((symptom) => <Badge key={symptom} variant="outline">{symptom}</Badge>)}
              {group.links?.map((link) => (
                <Button key={link.to} asChild size="sm" variant="link" className="h-auto px-1 text-xs">
                  <Link to={link.to}>{link.label}</Link>
                </Button>
              ))}
            </div>
          ) : null}
          {entries.length ? entries.map((entry) => (
            <EntryRow key={entry.key} entry={entry} location={locateOf(entry)} onLocate={onLocate} risky={RISK_KEYS.has(entry.key)} />
          )) : <p className="py-2 text-xs text-muted-foreground">这一组没有与默认值不同的配置项。</p>}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

function FeatureView({ entries, locateOf, onLocate, onOpenChannels }: {
  entries: ConfigEntry[]
  locateOf: (entry: ConfigEntry) => ConfigLocation
  onLocate: Locate
  onOpenChannels: () => void
}) {
  const [changedOnly, setChangedOnly] = useState(false)
  const [openState, setOpenState] = useState<Record<string, boolean>>({})
  const listed = useMemo(() => entries.filter((entry) => entry.kind === 'static' || entry.kind === 'hot'), [entries])
  const buckets = useMemo(() => {
    const all = groupByFeature(listed)
    if (!changedOnly) return all
    return all.map((bucket) => ({ ...bucket, entries: bucket.entries.filter((entry) => entry.changed) }))
  }, [changedOnly, listed])
  const channelChanged = entries.filter((entry) => entry.kind === 'channel' && entry.changed).length
  const botOverrides = entries.filter((entry) => entry.kind === 'bot').length

  if (!listed.length) return <QueryState status="empty" title="没有可展示的配置项" description="后端 schema 没有返回任何配置项。" />

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">按你想解决的问题分组；每项显示当前生效值与来源，「修改」会跳到它真正的编辑位置。</p>
        <label className="flex items-center gap-2 text-sm">
          <Switch aria-label="只看与默认值不同的" checked={changedOnly} onCheckedChange={setChangedOnly} />只看与默认值不同的
        </label>
      </div>
      {channelChanged || botOverrides ? (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
          <span>
            注入通道有 {channelChanged} 项与默认值不同{botOverrides ? `，另有 ${botOverrides} 项 Bot 级通道覆盖` : ''}；通道逐项设置不在下面的分组里，见通道配置或配置来源。
          </span>
          <Button type="button" size="sm" variant="outline" onClick={onOpenChannels}>去通道配置</Button>
        </div>
      ) : null}
      {buckets.map((bucket) => (
        <FeatureSection
          key={bucket.group.id}
          bucket={bucket}
          open={openState[bucket.group.id] ?? (changedOnly || bucket.group.defaultOpen)}
          onOpenChange={(next) => setOpenState((current) => ({ ...current, [bucket.group.id]: next }))}
          locateOf={locateOf}
          onLocate={onLocate}
        />
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- 高风险

const RISK_STATUS_LABEL: Record<RiskStatus, string> = {
  danger: '危险',
  warn: '需确认',
  deviated: '偏离默认',
  normal: '正常',
  missing: '未找到',
}

function riskBadge(status: RiskStatus) {
  if (status === 'danger') return <Badge variant="destructive">{RISK_STATUS_LABEL[status]}</Badge>
  if (status === 'warn' || status === 'deviated') return <Badge variant="secondary">{RISK_STATUS_LABEL[status]}</Badge>
  return <Badge variant="outline">{RISK_STATUS_LABEL[status]}</Badge>
}

function RiskCard({ assessment, locateOf, onLocate }: {
  assessment: RiskAssessment
  locateOf: (entry: ConfigEntry) => ConfigLocation
  onLocate: Locate
}) {
  const { rule, entry, status, message } = assessment
  if (!entry) return null
  const location = locateOf(entry)
  return (
    <Card className={cn(status === 'danger' && 'border-destructive')}>
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0 space-y-1">
            <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
              {entry.title}
              {riskBadge(status)}
              {rule.restart ? <Badge variant="secondary">需重启</Badge> : null}
              {rule.affectsData ? <Badge variant="secondary">影响已有数据</Badge> : null}
            </CardTitle>
            <CardDescription className="font-mono text-xs">{rule.key}</CardDescription>
          </div>
          <div className="flex flex-col items-start gap-1 sm:items-end">
            <Button type="button" size="sm" variant="outline" aria-label={`去修改 ${rule.key}`} onClick={() => onLocate(location)}>
              <PencilIcon />去修改
            </Button>
            <span className="text-xs text-muted-foreground">{location.label}</span>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2 text-xs">
        <div className="grid gap-2 sm:grid-cols-2">
          <div><span className="text-muted-foreground">当前生效：</span><span className={cn('font-mono', status === 'danger' && 'font-semibold text-destructive')}>{displayValue(rule.key, entry.effectiveValue)}</span></div>
          <div><span className="text-muted-foreground">默认值：</span><span className="font-mono">{displayValue(rule.key, entry.defaultValue)}</span></div>
        </div>
        {message ? <p className={cn(status === 'danger' ? 'text-destructive' : 'text-foreground')}>{message}</p> : null}
        <p><span className="text-muted-foreground">为什么高风险：</span>{rule.reason}</p>
        <p><span className="text-muted-foreground">影响：</span>{rule.impact}</p>
      </CardContent>
    </Card>
  )
}

function RiskView({ assessments, locateOf, onLocate }: {
  assessments: RiskAssessment[]
  locateOf: (entry: ConfigEntry) => ConfigLocation
  onLocate: Locate
}) {
  const danger = assessments.filter((item) => item.status === 'danger')
  const attention = assessments.filter((item) => item.status !== 'missing' && item.status !== 'normal')
  const normal = assessments.filter((item) => item.status === 'normal')
  const [normalOpen, setNormalOpen] = useState(false)
  const missing = assessments.filter((item) => item.status === 'missing')
  return (
    <div className="space-y-3">
      {danger.length ? (
        <Alert variant="destructive">
          <ShieldAlertIcon />
          <AlertTitle>{danger.length} 项处于危险状态</AlertTitle>
          <AlertDescription>
            <ul className="list-disc space-y-1 pl-5">{danger.map((item) => <li key={item.rule.key}>{item.message}</li>)}</ul>
          </AlertDescription>
        </Alert>
      ) : (
        <Alert>
          <ShieldAlertIcon />
          <AlertTitle>没有处于危险状态的高风险项</AlertTitle>
          <AlertDescription>这里只读展示；改动请点「去修改」到真实入口，改之前先看清「影响」。</AlertDescription>
        </Alert>
      )}
      {attention.map((item) => <RiskCard key={item.rule.key} assessment={item} locateOf={locateOf} onLocate={onLocate} />)}
      {normal.length ? (
        <Collapsible open={normalOpen} onOpenChange={setNormalOpen} className="rounded-xl border">
          <CollapsibleTrigger className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm hover:bg-muted/40 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none">
            <ChevronRightIcon className={cn('size-4 shrink-0 text-muted-foreground transition-transform', normalOpen && 'rotate-90')} aria-hidden="true" />
            <span className="font-medium">其余 {normal.length} 项当前正常</span>
            <span className="ml-auto text-xs text-muted-foreground">{normal.map((item) => item.entry?.title ?? item.rule.key).slice(0, 4).join('、')}{normal.length > 4 ? ' 等' : ''}</span>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="space-y-3 border-t p-3">
              {normal.map((item) => <RiskCard key={item.rule.key} assessment={item} locateOf={locateOf} onLocate={onLocate} />)}
            </div>
          </CollapsibleContent>
        </Collapsible>
      ) : null}
      {missing.length ? (
        <p className="text-xs text-muted-foreground">
          当前配置里没有这些高风险键（可能是版本差异）：<span className="font-mono">{missing.map((item) => item.rule.key).join('、')}</span>
        </p>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------- 搜索结果

function SearchResults({ entries, term, locateOf, onLocate, onClear }: {
  entries: ConfigEntry[]
  term: string
  locateOf: (entry: ConfigEntry) => ConfigLocation
  onLocate: Locate
  onClear: () => void
}) {
  const hits = useMemo(() => searchConfig(entries, term), [entries, term])
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <div>
          <CardTitle className="text-sm">搜索「{term.trim()}」</CardTitle>
          <CardDescription>{hits.length ? `${hits.length} 项；可以搜键名、名称、说明，也可以直接搜症状，如「回复太长」「召回不准」` : '没有匹配的配置项'}</CardDescription>
        </div>
        <Button type="button" size="sm" variant="ghost" onClick={onClear}><XIcon />清除搜索</Button>
      </CardHeader>
      <CardContent className="space-y-2">
        {hits.length ? hits.map(({ entry, group, bySymptom, symptom }) => (
          <div key={entry.key} className="space-y-1">
            <EntryRow entry={entry} location={locateOf(entry)} onLocate={onLocate} groupTitle={group.title} risky={RISK_KEYS.has(entry.key)} />
            {bySymptom ? <p className="pl-3 text-xs text-muted-foreground">{symptom ? `常见问题「${symptom}」可能与这一项有关` : `属于「${group.title}」`}</p> : null}
          </div>
        )) : <QueryState status="empty" title="没有匹配的配置项" description="换个说法试试，例如键名的一部分、中文名称或症状描述。" />}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------- 页面

export function ConfigCenterPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const view = parseView(searchParams.get('view'))
  const [visited, setVisited] = useState<ReadonlySet<View>>(() => new Set([view]))
  // 嵌入的系统配置 / 通道配置首次打开后保持挂载，切换标签不丢未保存草稿。
  if (!visited.has(view)) setVisited(new Set([...visited, view]))

  const [search, setSearch] = useState('')
  const [data, setData] = useState<CenterData>(LOADING)
  const requestRef = useRef(0)

  const load = useCallback(async () => {
    const requestId = ++requestRef.current
    setData(LOADING)
    const [schema, hot, inventory] = await Promise.allSettled([
      getConfigSchema().then((payload) => payload.groups ?? []),
      getHotConfig().then((payload) => payload.params ?? []),
      getConfigInventory(),
    ])
    if (requestId !== requestRef.current) return
    setData({ schema: settle(schema), hot: settle(hot), inventory: settle(inventory) })
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const schemaGroups = dataOf(data.schema)
  const hotParams = dataOf(data.hot)
  const inventory = dataOf(data.inventory)
  const entries = useMemo(
    () => schemaGroups ? buildConfigEntries({ schemaGroups, hotParams, inventoryRows: inventory?.items }) : [],
    [hotParams, inventory, schemaGroups],
  )
  const legacyBotIds = useMemo(() => legacyBotIdsFrom(entries.map((entry) => ({ key: entry.key, effective: entry.effectiveValue }))), [entries])
  const locateOf = useCallback((entry: ConfigEntry) => locateConfig(entry.key, { applyMode: entry.applyMode, legacyBotIds }), [legacyBotIds])
  const assessments = useMemo(() => assessRisks(entries), [entries])
  const dangerCount = assessments.filter((item) => item.status === 'danger').length

  const setView = useCallback((next: string) => setSearchParams({ view: parseView(next) }), [setSearchParams])

  const onLocate = useCallback<Locate>((location) => {
    setSearch('')
    if (location.target === 'bots') {
      navigate(locationHref(location))
      return
    }
    // 系统配置 / 通道配置在本页内嵌：切到对应标签并带上定位参数（key/tab、channel/field）。
    setSearchParams({ view: location.target, ...location.params })
  }, [navigate, setSearchParams])

  const searching = search.trim().length > 0
  const schemaStatus = data.schema.status
  const partialErrors = [
    data.hot.status === 'error' ? { title: '热参数加载失败', detail: '按功能与搜索里暂不含实时热参数。', error: data.hot.error } : null,
    data.inventory.status === 'error' ? { title: '配置来源加载失败', detail: '来源徽标与注入通道 / Bot 覆盖条目暂不可用。', error: data.inventory.error } : null,
  ].filter((item): item is { title: string; detail: string; error: unknown } => item !== null)

  const overview = schemaStatus === 'loading' ? (
    <QueryState status="loading" title="正在加载配置" loadingRows={4} />
  ) : schemaStatus === 'error' ? (
    <QueryState status="error" title="配置 schema 加载失败" error={data.schema.status === 'error' ? data.schema.error : undefined} onRetry={() => void load()} />
  ) : null

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>配置中心</CardTitle>
            <CardDescription className="mt-2">
              按功能找配置、集中查看高风险项；系统配置、通道配置与配置来源也在这里。所有修改都在各自的真实入口完成。
            </CardDescription>
          </div>
          <Button type="button" variant="outline" onClick={() => void load()} disabled={schemaStatus === 'loading'}>
            <RefreshCwIcon className={cn(schemaStatus === 'loading' && 'animate-spin')} />刷新
          </Button>
        </CardHeader>
        <CardContent>
          <InputWithIcon
            containerClassName="max-w-xl"
            placeholder="搜索配置键、名称、说明或症状（如：回复太长、召回不准、标签太多）"
            value={search}
            onValueChange={setSearch}
          />
        </CardContent>
      </Card>

      {partialErrors.map((item) => (
        <Alert key={item.title}>
          <AlertCircleIcon />
          <AlertTitle>{item.title}</AlertTitle>
          <AlertDescription>{humanizeApiError(item.error, item.title)}；{item.detail}</AlertDescription>
          <AlertAction><Button type="button" size="sm" variant="outline" onClick={() => void load()}>重试</Button></AlertAction>
        </Alert>
      ))}

      {searching ? (
        overview ?? <SearchResults entries={entries} term={search} locateOf={locateOf} onLocate={onLocate} onClear={() => setSearch('')} />
      ) : null}

      <Tabs value={view} onValueChange={setView} className={cn(searching && 'hidden')}>
        <TabsList className="grid h-auto! w-full grid-cols-3 gap-1 sm:grid-cols-5">
          <TabsTrigger value="feature">按功能</TabsTrigger>
          <TabsTrigger value="risk">
            高风险{dangerCount ? <Badge variant="destructive" className="ml-1">{dangerCount}</Badge> : null}
          </TabsTrigger>
          <TabsTrigger value="settings">系统配置</TabsTrigger>
          <TabsTrigger value="channels">通道配置</TabsTrigger>
          <TabsTrigger value="inventory">配置来源</TabsTrigger>
        </TabsList>

        <TabsContent value="feature" className="mt-4">
          {overview ?? <FeatureView entries={entries} locateOf={locateOf} onLocate={onLocate} onOpenChannels={() => setView('channels')} />}
        </TabsContent>
        <TabsContent value="risk" className="mt-4">
          {overview ?? <RiskView assessments={assessments} locateOf={locateOf} onLocate={onLocate} />}
        </TabsContent>
        {/* 以下三个直接复用现有页面；首次打开后强制保持挂载，避免切标签丢草稿。 */}
        <TabsContent value="settings" forceMount className="mt-4 data-[state=inactive]:hidden">
          {visited.has('settings') ? <SettingsPage /> : null}
        </TabsContent>
        <TabsContent value="channels" forceMount className="mt-4 data-[state=inactive]:hidden">
          {visited.has('channels') ? <ChannelConfigPage /> : null}
        </TabsContent>
        <TabsContent value="inventory" forceMount className="mt-4 data-[state=inactive]:hidden">
          {visited.has('inventory') ? <ConfigInventoryPage onLocate={onLocate} /> : null}
        </TabsContent>
      </Tabs>
    </div>
  )
}
