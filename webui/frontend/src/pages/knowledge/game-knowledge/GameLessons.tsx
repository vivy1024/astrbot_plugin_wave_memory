import { useCallback, useEffect, useState } from 'react'
import { BrainIcon, ChevronDownIcon, ChevronRightIcon, RefreshCwIcon, Trash2Icon, Undo2Icon } from 'lucide-react'
import { toast } from 'sonner'

import { isRequestCancelled, type ApiError } from '@/api/client'
import {
  listGameLessons,
  reflectGameLessons,
  restoreGameLesson,
  vetoGameLesson,
  type GameBot,
  type GameCard,
  type GameLibrary,
  type LessonMeta,
  type LessonPage,
  type LessonRun,
} from '@/api/gameKnowledge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'

import { day, errorText, number, selectClass, triggerSummary, when } from './shared'

const PAGE_SIZE = 50

const TRIGGER_LABELS: Record<string, string> = { event: '死亡 / 危机后', daily: '每日补想', manual: '手动' }
const RUN_STATUS_LABELS: Record<string, string> = { succeeded: '完成', skipped: '没有值得想的', failed: '失败', running: '进行中' }
const EVIDENCE_TYPE_LABELS: Record<string, string> = { death: '死亡', crisis: '危机', goal: '目标', milestone: '里程碑', choice: '抉择', discovery: '发现' }

type LessonState = 'active' | 'archived' | 'vetoed' | 'draft'

function lessonState(card: GameCard): LessonState {
  if ((card.meta as LessonMeta).vetoed) return 'vetoed'
  return card.status
}

const LESSON_STATE: Record<LessonState, { label: string; variant: 'default' | 'secondary' | 'outline' | 'destructive' }> = {
  active: { label: '有效', variant: 'default' },
  archived: { label: '已归档', variant: 'outline' },
  vetoed: { label: '已删除', variant: 'destructive' },
  draft: { label: '草稿', variant: 'secondary' },
}

function isConflict(reason: unknown): boolean {
  return (reason as ApiError | undefined)?.status === 409
}

function runCounts(run: LessonRun): string {
  const counts = run.counts ?? {}
  const parts = [`新写 ${counts.new ?? 0}`, `应验 ${counts.again ?? 0}`, `改写 ${counts.revised ?? 0}`]
  if (counts.revived) parts.push(`想回来 ${counts.revived}`)
  if (counts.blocked_vetoed) parts.push(`与删掉的相近未写 ${counts.blocked_vetoed}`)
  if (counts.skipped_game_disabled) parts.push(`游戏不允许写经验 ${counts.skipped_game_disabled} 条`)
  return parts.join(' · ')
}

export function LessonsPanel({ game, bots, lessonsEnabled, version, onChanged }: {
  game: GameLibrary
  bots: GameBot[]
  /** 游戏经验总开关（Learning_Settings.game_lesson_enabled）。 */
  lessonsEnabled: boolean
  version: number
  onChanged: () => Promise<void>
}) {
  const [botId, setBotId] = useState(() => bots[0]?.id ?? '')
  const [filters, setFilters] = useState({ status: '', q: '' })
  const [draftQuery, setDraftQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<LessonPage | null>(null)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [confirming, setConfirming] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (!botId && bots[0]) setBotId(bots[0].id)
  }, [botId, bots])

  useEffect(() => {
    if (!botId) return
    const controller = new AbortController()
    listGameLessons(game.id, { bot_id: botId, ...filters, limit: PAGE_SIZE, offset }, controller.signal)
      .then(setPage)
      .catch((reason) => { if (!isRequestCancelled(reason)) toast.error(errorText(reason, '经验加载失败')) })
    return () => controller.abort()
  }, [game.id, botId, filters, offset, version, reload])

  const refresh = useCallback(() => setReload((value) => value + 1), [])

  async function change(card: GameCard, action: 'veto' | 'restore') {
    setBusy(true)
    try {
      await (action === 'veto' ? vetoGameLesson : restoreGameLesson)(game.id, card.id, botId, card.version)
      toast.success(action === 'veto' ? `已删掉「${card.title}」，她不会再写意思相近的经验` : `已恢复「${card.title}」`)
      setConfirming(null)
      refresh()
      await onChanged()
    } catch (reason) {
      if (isConflict(reason)) {
        toast.error('这条经验刚被改过（可能她刚又想了一次），已重新加载，请再操作一次')
        setConfirming(null)
        refresh()
      } else {
        toast.error(errorText(reason, action === 'veto' ? '删除失败' : '恢复失败'))
      }
    } finally {
      setBusy(false)
    }
  }

  async function reflect() {
    setBusy(true)
    try {
      const result = await reflectGameLessons(game.id, botId)
      if (result.status === 'started') toast.success('她开始回想还没想过的游戏经历了（所有游戏一起想），过一会儿点「刷新」看结果')
      else if (result.status === 'running') toast.success('她正在想，稍后点「刷新」看结果')
      else toast.error('游戏经验没有开启，想不了')
      refresh()
    } catch (reason) {
      toast.error(errorText(reason, '没能开始回想'))
    } finally {
      setBusy(false)
    }
  }

  function toggle(id: number) {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const enabled = lessonsEnabled && page?.enabled !== false
  const botLabel = bots.find((bot) => bot.id === botId)?.name ?? botId
  const rows = page?.items ?? []
  const total = page?.total ?? 0
  const runs = page?.runs ?? []
  const queued = Boolean(page?.pending.includes(botId))

  return (
    <div className="flex flex-col gap-4">
      {!enabled ? (
        <Alert>
          <AlertTitle>游戏经验没有开启</AlertTitle>
          <AlertDescription>
            在配置中心打开 Learning_Settings.game_lesson_enabled（会新增模型调用）后，Bot 才会从自己的游戏经历里写经验卡、才能「现在想一次」。
            已经写下的经验照样可以在这里查看、删除和恢复。
          </AlertDescription>
        </Alert>
      ) : null}
      {game.lessons_enabled === false ? (
        <Alert>
          <AlertTitle>这个游戏不允许写经验</AlertTitle>
          <AlertDescription>「设置」里关掉了「允许 Bot 在这个游戏里写经验卡」：回想时会跳过这个游戏的经历，已有的经验卡不受影响。</AlertDescription>
        </Alert>
      ) : null}

      <Card className="border-border/60">
        <CardHeader className="gap-2 border-b pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="text-sm">{botLabel ? `${botLabel} 的经验（${number.format(total)}）` : '经验'}</CardTitle>
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" variant="outline" disabled={!botId} onClick={refresh}><RefreshCwIcon aria-hidden="true" />刷新</Button>
              <Button
                type="button"
                size="sm"
                disabled={busy || !botId || !enabled}
                title={enabled ? undefined : '先在配置中心打开 Learning_Settings.game_lesson_enabled'}
                onClick={() => void reflect()}
              >
                <BrainIcon aria-hidden="true" />现在想一次
              </Button>
            </div>
          </div>
          <CardDescription>
            Bot 从自己在这个游戏里的经历（死亡、危机等）里写下的经验卡，只属于她自己：「做法」是她下次会怎么做，「经过」是当时发生了什么。
            直播时和共用攻略一起按触发词、条件注入。删掉的经验记为否决，她以后不会再写意思相近的。
            {enabled ? '' : '（现在想一次：先在配置中心打开 Learning_Settings.game_lesson_enabled）'}
          </CardDescription>
          {bots.length ? (
            <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setOffset(0); setFilters((f) => ({ ...f, q: draftQuery.trim() })) }}>
              <select aria-label="选择 Bot" className={selectClass} value={botId} onChange={(event) => { setOffset(0); setExpanded(new Set()); setConfirming(null); setBotId(event.target.value) }}>
                {bots.map((bot) => <option key={bot.id} value={bot.id}>{bot.name}</option>)}
              </select>
              <select aria-label="经验状态" className={selectClass} value={filters.status} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, status: event.target.value })) }}>
                <option value="">全部</option>
                <option value="active">有效</option>
                <option value="archived">已归档 / 已删除</option>
                <option value="draft">草稿</option>
              </select>
              <Input aria-label="搜索经验" className="h-8 w-40 text-xs" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="标题 / 做法 / 触发词" />
              <Button type="submit" size="sm">查询</Button>
            </form>
          ) : null}
          {queued ? <p className="text-xs text-muted-foreground">刚有新的游戏经历，已排队，过几分钟会自己想一次。</p> : null}
        </CardHeader>
        <CardContent className="p-2">
          {!bots.length ? (
            <p className="p-2 text-sm text-muted-foreground">还没有可选的 Bot。</p>
          ) : rows.length ? (
            <ul className="flex flex-col divide-y" aria-label="经验列表">
              {rows.map((card) => {
                const meta = card.meta as LessonMeta
                const state = lessonState(card)
                const evidence = meta.evidence ?? []
                const open = expanded.has(card.id)
                return (
                  <li key={card.id} className="flex flex-col gap-1 p-2" aria-label={`经验 ${card.title}`}>
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="font-medium">{card.title}</span>
                      <Badge variant={LESSON_STATE[state].variant}>{LESSON_STATE[state].label}</Badge>
                      <span className="text-xs text-muted-foreground">
                        经历过 {number.format(meta.times ?? 1)} 次 · 最近一次 {day(meta.last_at ?? card.updated_at)}
                        {meta.revisions ? ` · 改写过 ${meta.revisions} 次` : ''}
                      </span>
                    </div>
                    {card.points.length ? (
                      <div className="text-xs"><span className="text-muted-foreground">做法：</span>{card.points.join('；')}</div>
                    ) : null}
                    {card.pitfalls.length ? (
                      <div className="text-xs"><span className="text-muted-foreground">经过：</span>{card.pitfalls.join('；')}</div>
                    ) : null}
                    <span className="text-xs text-muted-foreground">{triggerSummary(card)}</span>
                    {meta.revised_why ? <span className="text-xs text-muted-foreground">上次改写的原因：{meta.revised_why}</span> : null}
                    <div className="flex flex-wrap items-center gap-2">
                      {evidence.length ? (
                        <Button type="button" size="sm" variant="ghost" aria-expanded={open} onClick={() => toggle(card.id)}>
                          {open ? <ChevronDownIcon aria-hidden="true" /> : <ChevronRightIcon aria-hidden="true" />}依据的经历（{evidence.length}）
                        </Button>
                      ) : null}
                      {confirming === card.id ? (
                        <span className="flex flex-wrap items-center gap-2 rounded-md border border-destructive/40 p-1 text-xs" role="group" aria-label="确认删除经验">
                          <span>删掉后她不会再写意思相近的经验。确定删除？</span>
                          <Button type="button" size="sm" variant="destructive" disabled={busy} onClick={() => void change(card, 'veto')}>确认删除</Button>
                          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(null)}>取消</Button>
                        </span>
                      ) : (
                        <>
                          {/* 已归档（超上限自动归档）的还能恢复，也能直接否决；已删除的只能恢复 */}
                          {state === 'archived' || state === 'vetoed' ? (
                            <Button type="button" size="sm" variant="outline" disabled={busy} aria-label={`恢复 ${card.title}`} onClick={() => void change(card, 'restore')}><Undo2Icon aria-hidden="true" />恢复</Button>
                          ) : null}
                          {state !== 'vetoed' ? (
                            <Button type="button" size="sm" variant="outline" disabled={busy} aria-label={`删除 ${card.title}`} onClick={() => setConfirming(card.id)}><Trash2Icon aria-hidden="true" />删除</Button>
                          ) : null}
                        </>
                      )}
                    </div>
                    {open ? (
                      <ol className="ml-4 flex list-decimal flex-col gap-1 text-xs" aria-label={`${card.title} 的依据经历`}>
                        {evidence.map((item) => (
                          <li key={`${item.memory_id}-${item.at}`}>
                            <span className="text-muted-foreground">{when(item.at)} · {EVIDENCE_TYPE_LABELS[item.type] ?? item.type} · #{item.memory_id}：</span>{item.text}
                          </li>
                        ))}
                      </ol>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          ) : (
            <p className="p-2 text-sm text-muted-foreground">{page ? '她在这个游戏里还没有写下经验。' : '加载中…'}</p>
          )}
          {total > PAGE_SIZE ? (
            <div className="flex items-center justify-between p-2 text-xs text-muted-foreground">
              <span>{`${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} / ${number.format(total)}`}</span>
              <span className="flex gap-2">
                <Button type="button" size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
                <Button type="button" size="sm" variant="ghost" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
              </span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {bots.length ? (
        <Card className="border-border/60">
          <CardHeader className="border-b pb-3">
            <CardTitle className="text-sm">最近的回想</CardTitle>
            <CardDescription>死亡 / 危机后等几分钟会自己想一次，每日日记写完后把没想过的补一遍，也可以手动「现在想一次」。只列读到了经历的回想。</CardDescription>
          </CardHeader>
          <CardContent className="p-2">
            {runs.length ? (
              <table className="w-full text-xs" aria-label="回想记录">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="text-left font-normal">时间</th>
                    <th className="text-left font-normal">触发</th>
                    <th className="text-left font-normal">状态</th>
                    <th className="text-right font-normal">读了经历</th>
                    <th className="text-right font-normal">调用模型</th>
                    <th className="pl-2 text-left font-normal">结果</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <tr key={run.id} className="border-t align-top">
                      <td className="py-1 pr-2">{when(run.started_at)}</td>
                      <td className="py-1 pr-2">{TRIGGER_LABELS[run.trigger] ?? run.trigger}</td>
                      <td className="py-1 pr-2">
                        <Badge variant={run.status === 'failed' ? 'destructive' : run.status === 'succeeded' ? 'default' : 'outline'}>{RUN_STATUS_LABELS[run.status] ?? run.status}</Badge>
                      </td>
                      <td className="py-1 text-right">{number.format(run.events)} 条</td>
                      <td className="py-1 text-right">{number.format(run.model_calls)} 次</td>
                      <td className="py-1 pl-2">
                        {runCounts(run)}
                        {run.error ? <div className="text-destructive">错误：{run.error}</div> : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="p-2 text-sm text-muted-foreground">还没有回想记录。</p>}
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
