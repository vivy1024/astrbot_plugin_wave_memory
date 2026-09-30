import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { DatabaseZapIcon, Gamepad2Icon, PlusIcon, RefreshCwIcon, SaveIcon } from 'lucide-react'
import { toast } from 'sonner'

import { isRequestCancelled } from '@/api/client'
import {
  createGame,
  getGame,
  getGameOverview,
  migrateLegacySource,
  scanLegacySource,
  updateGame,
  type GameItemKind,
  type GameLibrary,
  type GameOverview,
  type LegacySource,
} from '@/api/gameKnowledge'
import { QueryState } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'

import { CardsPanel, ReviewPanel, TermsPanel } from './game-knowledge/GameItems'
import { LessonsPanel } from './game-knowledge/GameLessons'
import { ImportPanel, TrialPanel } from './game-knowledge/GameTools'
import { errorText, number, splitList, when } from './game-knowledge/shared'

type Focus = { kind: GameItemKind; id: number; nonce: number }

function positiveInt(value: string | null): number | undefined {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined
}

export function GameKnowledgePage() {
  const [params, setParams] = useSearchParams()
  const [overview, setOverview] = useState<GameOverview | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [error, setError] = useState<unknown>()
  const [tab, setTab] = useState(() => (params.get('term') ? 'terms' : 'cards'))
  const [stages, setStages] = useState<Array<{ name: string; count: number }>>([])
  const [itemsVersion, setItemsVersion] = useState(0)
  const [focus, setFocus] = useState<Focus | null>(() => {
    const card = positiveInt(params.get('card'))
    const term = positiveInt(params.get('term'))
    if (card) return { kind: 'cards', id: card, nonce: 0 }
    if (term) return { kind: 'terms', id: term, nonce: 0 }
    return null
  })

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setOverview(await getGameOverview(signal))
      setStatus('success')
    } catch (reason) {
      if (isRequestCancelled(reason)) return
      setError(reason)
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const games = overview?.games ?? []
  const requested = params.get('game') ?? ''
  const selected = games.find((game) => game.id === requested) ?? games[0] ?? null
  const selectedId = selected?.id ?? ''

  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    getGame(selectedId, controller.signal)
      .then((detail) => setStages(detail.stages))
      .catch((reason) => { if (!isRequestCancelled(reason)) setStages([]) })
    return () => controller.abort()
  }, [selectedId, itemsVersion])

  const changed = useCallback(async () => {
    setItemsVersion((value) => value + 1)
    await load()
  }, [load])

  function select(gameId: string) {
    const next = new URLSearchParams(params)
    next.set('game', gameId)
    next.delete('card')
    next.delete('term')
    setParams(next, { replace: true })
    setFocus(null)
  }

  function edit(kind: GameItemKind, id: number) {
    setTab(kind)
    setFocus({ kind, id, nonce: Date.now() })
  }

  return (
    <div className="flex flex-col gap-4" data-page="game-knowledge">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <header className="max-w-3xl">
          <h1 className="text-xl font-bold tracking-tight">游戏知识库</h1>
          <p className="text-xs text-muted-foreground">
            按游戏分库的攻略常识，所有 Bot 共用。直播时 Cortico 在 /context/prepare 带上 game，通道 game_knowledge 按游戏标识选库，
            注入命中的攻略卡（最多 2 张）与相关词条；工具 wave_memory_game_knowledge 可以随时查。她自己玩过的事走记忆；
            从这些经历里总结出的做法写成她自己的经验卡（「经验」标签页，只属于她），对人和事的看法走 <Link className="underline" to="/beliefs">看法</Link>。
            新增一个游戏 = 建一个库、导入知识。
          </p>
        </header>
        <Button type="button" size="sm" variant="outline" onClick={() => void changed()}><RefreshCwIcon aria-hidden="true" />刷新</Button>
      </div>

      <QueryState status={status} error={error} title="游戏知识库不可用" onRetry={() => void load()}>
        {overview ? (
          <div className="grid gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
            <div className="flex flex-col gap-4">
              <GamesCard games={games} selectedId={selectedId} onSelect={select} onCreated={async (id) => { await load(); select(id) }} />
              <LegacyCard sources={overview.legacy_sources} onMigrated={async (gameId) => { await load(); if (gameId) select(gameId) }} />
            </div>
            {selected ? (
              <div className="flex min-w-0 flex-col gap-3">
                <GameHeader game={selected} />
                <Tabs value={tab} onValueChange={setTab} className="flex flex-col gap-3">
                  <TabsList>
                    <TabsTrigger value="cards">知识卡</TabsTrigger>
                    <TabsTrigger value="terms">词条</TabsTrigger>
                    <TabsTrigger value="review">草稿审核{selected.counts.cards.draft + selected.counts.terms.draft ? `（${selected.counts.cards.draft + selected.counts.terms.draft}）` : ''}</TabsTrigger>
                    <TabsTrigger value="lessons">经验{selected.counts.lessons?.active ? `（${selected.counts.lessons.active}）` : ''}</TabsTrigger>
                    <TabsTrigger value="import">导入导出</TabsTrigger>
                    <TabsTrigger value="trial">试一下</TabsTrigger>
                    <TabsTrigger value="settings">设置</TabsTrigger>
                  </TabsList>
                  <TabsContent value="cards">
                    <CardsPanel key={`cards-${selected.id}`} game={selected} stages={stages} triggerRule={overview.trigger_rule} version={itemsVersion} onChanged={changed} focusId={focus?.kind === 'cards' ? focus.id : undefined} focusNonce={focus?.nonce} />
                  </TabsContent>
                  <TabsContent value="terms">
                    <TermsPanel key={`terms-${selected.id}`} game={selected} version={itemsVersion} onChanged={changed} focusId={focus?.kind === 'terms' ? focus.id : undefined} focusNonce={focus?.nonce} />
                  </TabsContent>
                  <TabsContent value="review">
                    <ReviewPanel key={`review-${selected.id}`} game={selected} version={itemsVersion} onChanged={changed} onEdit={edit} />
                  </TabsContent>
                  <TabsContent value="import"><ImportPanel key={`import-${selected.id}`} game={selected} onChanged={changed} /></TabsContent>
                  <TabsContent value="lessons">
                    <LessonsPanel key={`lessons-${selected.id}`} game={selected} bots={overview.bots ?? []} lessonsEnabled={overview.lessons_enabled} version={itemsVersion} onChanged={changed} />
                  </TabsContent>
                  <TabsContent value="trial"><TrialPanel key={`trial-${selected.id}`} game={selected} bots={overview.bots ?? []} /></TabsContent>
                  <TabsContent value="settings"><GameSettings key={`settings-${selected.id}-${selected.version}`} game={selected} lessonsEnabled={overview.lessons_enabled} onSaved={changed} /></TabsContent>
                </Tabs>
              </div>
            ) : (
              <Card className="border-border/60">
                <CardContent className="p-6 text-sm text-muted-foreground">
                  还没有游戏库。先在左侧新建一个（如 minecraft、oni、slay_the_spire），再导入知识或手工录入。
                </CardContent>
              </Card>
            )}
          </div>
        ) : null}
      </QueryState>
    </div>
  )
}

function GameHeader({ game }: { game: GameLibrary }) {
  const { cards, terms } = game.counts
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Gamepad2Icon className="size-5 text-muted-foreground" aria-hidden="true" />
      <h2 className="text-lg font-semibold">{game.name}</h2>
      <span className="font-mono text-xs text-muted-foreground">{game.id}</span>
      {game.enabled ? <Badge>启用</Badge> : <Badge variant="outline">已停用（不注入、工具查不到）</Badge>}
      {game.aliases.length ? <span className="text-xs text-muted-foreground">别名：{game.aliases.join('、')}</span> : null}
      <span className="text-xs text-muted-foreground">
        知识卡 启用 {number.format(cards.active)} · 草稿 {number.format(cards.draft)} · 归档 {number.format(cards.archived)}；
        词条 启用 {number.format(terms.active)} · 草稿 {number.format(terms.draft)}
      </span>
    </div>
  )
}

function GamesCard({ games, selectedId, onSelect, onCreated }: {
  games: GameLibrary[]
  selectedId: string
  onSelect: (id: string) => void
  onCreated: (id: string) => Promise<void>
}) {
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ id: '', name: '', aliases: '', description: '', enabled: true })
  const [busy, setBusy] = useState(false)

  async function create() {
    setBusy(true)
    try {
      const result = await createGame({ id: form.id.trim(), name: form.name.trim() || form.id.trim(), aliases: splitList(form.aliases), description: form.description.trim(), enabled: form.enabled })
      toast.success(`已新建游戏库「${result.item.name}」`)
      setCreating(false)
      setForm({ id: '', name: '', aliases: '', description: '', enabled: true })
      await onCreated(result.item.id)
    } catch (reason) {
      toast.error(errorText(reason, '新建失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm">游戏</CardTitle>
          <Button type="button" size="sm" variant="outline" onClick={() => setCreating((value) => !value)}><PlusIcon aria-hidden="true" />新建游戏库</Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 p-2">
        {creating ? (
          <div className="flex flex-col gap-2 rounded-md border p-2" aria-label="新建游戏库">
            <Input aria-label="游戏标识" value={form.id} onChange={(event) => setForm({ ...form, id: event.target.value })} placeholder="游戏标识（小写英文，如 minecraft）" />
            <Input aria-label="显示名" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="显示名，如 我的世界" />
            <Input aria-label="游戏别名" value={form.aliases} onChange={(event) => setForm({ ...form, aliases: event.target.value })} placeholder="别名，用逗号分隔，如 MC、Minecraft" />
            <Textarea aria-label="游戏说明" rows={2} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="说明（版本、玩法…）" />
            <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />启用</label>
            <div className="flex gap-2">
              <Button type="button" size="sm" disabled={busy || !form.id.trim()} onClick={() => void create()}>创建</Button>
              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setCreating(false)}>取消</Button>
            </div>
          </div>
        ) : null}
        {games.length ? (
          <ul className="flex flex-col gap-1" aria-label="游戏列表">
            {games.map((game) => (
              <li key={game.id}>
                <button
                  type="button"
                  aria-current={game.id === selectedId ? 'true' : undefined}
                  className={`flex w-full flex-col gap-0.5 rounded-md p-2 text-left hover:bg-muted/50 ${game.id === selectedId ? 'bg-muted' : ''}`}
                  onClick={() => onSelect(game.id)}
                >
                  <span className="flex items-center gap-2 text-sm">
                    <span className="font-medium">{game.name}</span>
                    <span className="font-mono text-xs text-muted-foreground">{game.id}</span>
                    {game.enabled ? null : <Badge variant="outline">停用</Badge>}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    启用 {game.counts.cards.active} 张卡 · 草稿 {game.counts.cards.draft + game.counts.terms.draft} · 词条 {game.counts.terms.active}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : <p className="p-2 text-sm text-muted-foreground">还没有游戏库。</p>}
      </CardContent>
    </Card>
  )
}

function LegacyCard({ sources, onMigrated }: { sources: LegacySource[]; onMigrated: (gameId: string | null) => Promise<void> }) {
  const [confirming, setConfirming] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  if (!sources.length) return null

  async function scan(source: LegacySource) {
    setBusy(true)
    try {
      const result = await scanLegacySource(source.source)
      toast.success(`记忆表里有 ${result.rows} 条 ${source.source}${result.parsed !== null ? `，其中 ${result.parsed} 条能解析出集数` : ''}`)
      await onMigrated(null)
    } catch (reason) {
      toast.error(errorText(reason, '检查失败'))
    } finally {
      setBusy(false)
    }
  }

  async function migrate(source: LegacySource) {
    setBusy(true)
    try {
      const result = await migrateLegacySource(source.source)
      if (result.skipped) toast.success(`记忆表里没有 ${source.source} 的行，无需迁移`)
      else toast.success(`已迁移为草稿：新增 ${result.cards?.created ?? 0} 张，跳过 ${result.cards?.skipped ?? 0} 张已迁移的${result.parse_failed ? `；${result.parse_failed} 条前缀没解析出集数，也已导入并备注` : ''}`)
      setConfirming(null)
      await onMigrated(result.skipped ? null : source.game_id)
    } catch (reason) {
      toast.error(errorText(reason, '迁移失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">记忆表里的旧知识点</CardTitle>
        <CardDescription>以前直接导进记忆表、没有归属任何 Bot 的游戏知识，迁移成游戏库草稿（原记忆行不删不改，重复执行只补新行）。检查与迁移要扫描整个记忆表，库大时需要几秒到几十秒。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 p-2">
        {sources.map((source) => (
          <div key={source.source} className="flex flex-col gap-1 rounded-md border p-2 text-sm">
            <span className="font-medium">{source.game_name} · <span className="font-mono text-xs">source={source.source}</span></span>
            <span className="text-xs text-muted-foreground">{source.description}</span>
            <span className="text-xs">
              {source.scanned ? `记忆表里 ${source.rows ?? 0} 条（${when(source.scanned_at ?? 0)} 检查）` : '还没检查'}
              {source.migrated ? ` · 已迁移 ${source.migrated} 张` : ''}
            </span>
            {confirming === source.source ? (
              <div className="flex gap-2">
                <Button type="button" size="sm" disabled={busy} onClick={() => void migrate(source)}>确认迁移为草稿</Button>
                <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(null)}>取消</Button>
              </div>
            ) : (
              <div className="flex gap-2">
                <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void scan(source)}>检查</Button>
                <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setConfirming(source.source)}><DatabaseZapIcon aria-hidden="true" />迁移为草稿</Button>
              </div>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

function GameSettings({ game, lessonsEnabled, onSaved }: { game: GameLibrary; lessonsEnabled: boolean; onSaved: () => Promise<void> }) {
  const [form, setForm] = useState({
    name: game.name,
    aliases: game.aliases.join('、'),
    description: game.description,
    enabled: game.enabled,
    lessons_enabled: game.lessons_enabled !== false,
  })
  const [busy, setBusy] = useState(false)
  const stateKeys = Object.entries(game.state_keys ?? {})

  async function save() {
    setBusy(true)
    try {
      await updateGame(game.id, {
        name: form.name.trim(),
        aliases: splitList(form.aliases),
        description: form.description.trim(),
        enabled: form.enabled,
        lessons_enabled: form.lessons_enabled,
      }, game.version)
      toast.success('已保存')
      await onSaved()
    } catch (reason) {
      toast.error(errorText(reason, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">游戏库设置 · 版本 {game.version}</CardTitle>
        <CardDescription>游戏标识 {game.id} 建好后不能改（Cortico 按它选库）。停用后不注入、工具也查不到，内容保留。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 p-4">
        <Input aria-label="设置显示名" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="显示名" />
        <Input aria-label="设置别名" value={form.aliases} onChange={(event) => setForm({ ...form, aliases: event.target.value })} placeholder="别名，用逗号分隔（工具与请求里写别名也能找到这个库）" />
        <Textarea aria-label="设置说明" rows={3} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="说明" />
        <label className="flex items-center gap-1 text-xs"><input type="checkbox" aria-label="启用游戏库" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />启用（直播时注入、工具可查）</label>
        <label className="flex items-center gap-1 text-xs">
          <input type="checkbox" aria-label="允许写经验卡" checked={form.lessons_enabled} onChange={(event) => setForm({ ...form, lessons_enabled: event.target.checked })} />
          允许 Bot 在这个游戏里从自己的经历写经验卡
          {lessonsEnabled ? null : <span className="text-muted-foreground">（游戏经验总开关 Learning_Settings.game_lesson_enabled 没开，开了才生效）</span>}
        </label>
        <Button type="button" size="sm" className="self-start" disabled={busy || !form.name.trim()} onClick={() => void save()}><SaveIcon aria-hidden="true" />保存</Button>
        <div className="flex flex-col gap-1 border-t pt-2">
          <span className="text-xs font-medium">直播请求里见过的状态键（只读）</span>
          {stateKeys.length ? (
            <ul className="flex flex-wrap gap-1" aria-label="见过的状态键">
              {stateKeys.map(([key, info]) => (
                <li key={key}><Badge variant="outline" className="font-mono">{key}{info?.example !== undefined && info.example !== '' ? ` = ${info.example}` : ''}</Badge></li>
              ))}
            </ul>
          ) : <span className="text-xs text-muted-foreground">还没见过（Cortico 在 /context/prepare 的 game.state 里带上后会自动记下，写条件时可以参考）。</span>}
        </div>
      </CardContent>
    </Card>
  )
}
