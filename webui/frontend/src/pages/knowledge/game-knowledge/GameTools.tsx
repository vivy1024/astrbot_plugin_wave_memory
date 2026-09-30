import { useState } from 'react'
import { DownloadIcon, FileUpIcon, FlaskConicalIcon } from 'lucide-react'
import { toast } from 'sonner'

import {
  exportGame,
  previewGameImport,
  runGameImport,
  trialGameMatch,
  type GameBot,
  type GameLibrary,
  type ImportOnExisting,
  type ImportPreview,
  type ImportResult,
  type ImportStatusMode,
  type TrialResult,
} from '@/api/gameKnowledge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

import { STATUS_LABELS, errorText, number, parseStateLines, selectClass, splitList } from './shared'

const STATUS_MODE_LABELS: Record<ImportStatusMode, string> = {
  auto: '按文件里的状态（没写的为草稿）',
  draft: '全部导入为草稿（推荐，审核后再启用）',
  active: '全部直接启用',
}
const ON_EXISTING_LABELS: Record<ImportOnExisting, string> = {
  skip: '跳过（不动已有条目）',
  merge: '合并补全（本次内容在前，旧条目保留）',
  update: '覆盖正文',
}

function readFileText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('文件读取失败'))
    reader.readAsText(file, 'utf-8')
  })
}

function summary(result: ImportResult): string {
  const cards = `知识卡 新增 ${result.cards.created}、更新 ${result.cards.updated}、未变 ${result.cards.unchanged}、跳过 ${result.cards.skipped}`
  const terms = `词条 新增 ${result.terms.created}、更新 ${result.terms.updated}、跳过 ${result.terms.skipped}`
  const invalid = result.invalid.cards + result.invalid.terms
  return `${cards}；${terms}${invalid ? `；${invalid} 条不合法未导入` : ''}`
}

function download(name: string, document: unknown) {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' })
  const create = typeof URL.createObjectURL === 'function' ? URL.createObjectURL : null
  if (!create) return false
  const url = create(blob)
  const anchor = window.document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  URL.revokeObjectURL(url)
  return true
}

export function ImportPanel({ game, onChanged }: { game: GameLibrary; onChanged: () => Promise<void> }) {
  const [fileName, setFileName] = useState('')
  const [data, setData] = useState<unknown>(null)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [statusMode, setStatusMode] = useState<ImportStatusMode>('draft')
  const [onExisting, setOnExisting] = useState<ImportOnExisting>('skip')
  const [result, setResult] = useState<ImportResult | null>(null)
  const [busy, setBusy] = useState(false)

  async function choose(file: File | undefined) {
    setPreview(null)
    setResult(null)
    if (!file) return
    setFileName(file.name)
    let parsed: unknown
    try {
      parsed = JSON.parse(await readFileText(file))
    } catch {
      toast.error('不是合法的 JSON 文件')
      return
    }
    setBusy(true)
    try {
      const next = await previewGameImport(game.id, parsed)
      setData(parsed)
      setPreview(next)
      setStatusMode(next.force_status === 'draft' ? 'draft' : next.default_status_mode)
      setOnExisting(next.cards.existing + next.terms.existing > 0 && next.format !== 'generic' ? 'merge' : 'skip')
    } catch (reason) {
      toast.error(errorText(reason, '解析失败'))
    } finally {
      setBusy(false)
    }
  }

  async function run() {
    if (!preview) return
    setBusy(true)
    try {
      const done = await runGameImport(game.id, data, { status: statusMode, on_existing: onExisting })
      setResult(done)
      toast.success(`已导入：${summary(done)}`)
      setPreview(null)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason, '导入失败'))
    } finally {
      setBusy(false)
    }
  }

  async function exportAll(status?: string) {
    try {
      const document = await exportGame(game.id, status)
      const saved = download(`${game.id}-game-knowledge${status ? `-${status}` : ''}.json`, document)
      toast.success(saved ? '已导出' : '已生成导出内容（当前环境不支持下载）')
    } catch (reason) {
      toast.error(errorText(reason, '导出失败'))
    }
  }

  const importable = preview ? preview.cards.new + preview.cards.existing + preview.terms.new + preview.terms.existing : 0
  return (
    <div className="flex flex-col gap-4">
      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">导入</CardTitle>
          <CardDescription>
            支持 WaveMemory 通用格式（format=wavememory.game_knowledge，见 docs/game-knowledge.md），
            以及缺氧教程的 oni_tutorial_cards.json 与 oni_timeline.json（自动识别；两份按集数对齐为同一张卡，
            先导入策略卡，再以「合并补全」导入时间线）。先预览，确认后才写入。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 p-4">
          <label className="flex flex-wrap items-center gap-2 text-sm">
            <FileUpIcon className="size-4" aria-hidden="true" />
            <input type="file" accept=".json,application/json" aria-label="选择导入文件" disabled={busy} onChange={(event) => void choose(event.target.files?.[0])} />
            {fileName ? <span className="text-xs text-muted-foreground">{fileName}</span> : null}
          </label>
          {preview ? (
            <div className="flex flex-col gap-3" aria-label="导入预览">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge>{preview.format_label}</Badge>
                <span>知识卡 {preview.cards.total}：新增 {preview.cards.new}、已有 {preview.cards.existing}{preview.cards.invalid ? `、不合法 ${preview.cards.invalid}` : ''}</span>
                <span>词条 {preview.terms.total}：新增 {preview.terms.new}、已有 {preview.terms.existing}{preview.terms.invalid ? `、不合法 ${preview.terms.invalid}` : ''}</span>
              </div>
              {preview.warnings.length ? (
                <Alert>
                  <AlertTitle>注意</AlertTitle>
                  <AlertDescription><ul className="list-disc pl-4">{preview.warnings.map((w) => <li key={w}>{w}</li>)}</ul></AlertDescription>
                </Alert>
              ) : null}
              {preview.errors.length ? (
                <Alert variant="destructive">
                  <AlertTitle>{preview.errors.length} 条不合法，不会导入</AlertTitle>
                  <AlertDescription>
                    <ul className="list-disc pl-4 text-xs">{preview.errors.slice(0, 10).map((e) => <li key={`${e.kind}-${e.index}`}>{e.kind === 'card' ? '知识卡' : '词条'} #{e.index + 1}{e.label ? `「${e.label}」` : ''}：{e.message}</li>)}</ul>
                  </AlertDescription>
                </Alert>
              ) : null}
              {preview.samples.cards.length ? (
                <table className="w-full text-xs">
                  <thead className="text-muted-foreground"><tr><th className="text-left font-normal">知识卡</th><th className="text-left font-normal">阶段</th><th className="text-left font-normal">触发</th><th className="text-right font-normal">状态</th></tr></thead>
                  <tbody>
                    {preview.samples.cards.map((card) => (
                      <tr key={card.key} className="border-t align-top">
                        <td className="py-1 pr-2">{card.title}{card.exists ? <Badge variant="outline" className="ml-1">已有</Badge> : null}</td>
                        <td className="py-1 pr-2">{card.stage}</td>
                        <td className="py-1 pr-2 text-muted-foreground">{[...card.keywords.slice(0, 4), ...card.conditions].join('、') || '—'}</td>
                        <td className="py-1 text-right">{STATUS_LABELS[card.status as keyof typeof STATUS_LABELS] ?? card.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
              <div className="flex flex-wrap items-center gap-2">
                <select aria-label="导入状态" className={selectClass} value={statusMode} disabled={Boolean(preview.force_status)} onChange={(event) => setStatusMode(event.target.value as ImportStatusMode)}>
                  {(Object.keys(STATUS_MODE_LABELS) as ImportStatusMode[]).map((mode) => <option key={mode} value={mode}>{STATUS_MODE_LABELS[mode]}</option>)}
                </select>
                <select aria-label="已有条目" className={selectClass} value={onExisting} onChange={(event) => setOnExisting(event.target.value as ImportOnExisting)}>
                  {(Object.keys(ON_EXISTING_LABELS) as ImportOnExisting[]).map((mode) => <option key={mode} value={mode}>已有同键条目：{ON_EXISTING_LABELS[mode]}</option>)}
                </select>
                <Button type="button" size="sm" disabled={busy || !importable} onClick={() => void run()}>确认导入</Button>
                <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setPreview(null)}>取消</Button>
              </div>
              <p className="text-xs text-muted-foreground">已有条目的状态不会被导入改动（归档的仍是归档）。</p>
            </div>
          ) : null}
          {result ? <p className="text-sm" role="status">{summary(result)}</p> : null}
        </CardContent>
      </Card>

      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">导出</CardTitle>
          <CardDescription>导出为通用格式，可以备份，也可以导入到另一个游戏库或另一套 WaveMemory。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2 p-4">
          <Button type="button" size="sm" variant="outline" onClick={() => void exportAll('active')}><DownloadIcon aria-hidden="true" />导出启用的</Button>
          <Button type="button" size="sm" variant="outline" onClick={() => void exportAll()}><DownloadIcon aria-hidden="true" />导出全部（含草稿、归档）</Button>
        </CardContent>
      </Card>
    </div>
  )
}

function botName(bots: GameBot[], id: string | undefined): string {
  return bots.find((bot) => bot.id === id)?.name ?? id ?? ''
}

export function TrialPanel({ game, bots = [] }: { game: GameLibrary; bots?: GameBot[] }) {
  const [message, setMessage] = useState('')
  const [keywords, setKeywords] = useState('')
  const [stateText, setStateText] = useState('')
  const [includeDrafts, setIncludeDrafts] = useState(false)
  const [botId, setBotId] = useState('')
  const [result, setResult] = useState<TrialResult | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true)
    try {
      const body = { message, keywords: splitList(keywords), state: parseStateLines(stateText), include_drafts: includeDrafts }
      // 选了 Bot 就连它自己的经验卡一起算（和直播时她的请求一样）
      setResult(await trialGameMatch(game.id, botId ? { ...body, bot_id: botId } : body))
    } catch (reason) {
      toast.error(errorText(reason, '试匹配失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">试一下</CardTitle>
        <CardDescription>模拟一次直播请求（/context/prepare 的 game 字段），看会注入哪些卡、为什么；不写任何数据。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 p-4">
        <Textarea aria-label="试匹配消息" rows={2} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="当前消息，如「门口有苦力怕」" />
        <Input aria-label="试匹配关键词" value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder="keywords，如 夜晚、河边" />
        <Textarea aria-label="试匹配状态" rows={3} value={stateText} onChange={(event) => setStateText(event.target.value)} placeholder={'state，每行 键=值，如\ntime_of_day=night\nhealth=6'} />
        <div className="flex flex-wrap items-center gap-2">
          {bots.length ? (
            <select aria-label="试匹配 Bot" className={selectClass} value={botId} onChange={(event) => setBotId(event.target.value)}>
              <option value="">只算共用的攻略</option>
              {bots.map((bot) => <option key={bot.id} value={bot.id}>连 {bot.name} 自己的经验一起算</option>)}
            </select>
          ) : null}
          <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={includeDrafts} onChange={(event) => setIncludeDrafts(event.target.checked)} />把草稿也算上（审核前看看启用后会不会触发）</label>
          <Button type="button" size="sm" disabled={busy} onClick={() => void run()}><FlaskConicalIcon aria-hidden="true" />试匹配</Button>
        </div>
        {result ? (
          <div className="flex flex-col gap-2 text-sm" aria-label="试匹配结果">
            {result.warnings.length ? <p className="text-xs text-amber-700 dark:text-amber-400">{result.warnings.join('；')}</p> : null}
            {result.cards.length || result.terms.length ? null : <p className="text-muted-foreground">没有会注入的内容（{result.reason === 'game_library_disabled' ? '游戏库已停用' : result.reason === 'nothing_to_match' ? '消息、关键词、状态都是空的' : '没有卡或词条被触发'}）。</p>}
            {result.cards.map((card) => (
              <div key={card.id} className="rounded-md border p-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{card.title}</span>
                  {card.experience ? <Badge variant="secondary" title={botName(bots, card.bot_id)}>她自己的经验</Badge> : null}
                  <Badge variant="outline">得分 {number.format(card.score)}</Badge>
                  {card.status !== 'active' ? <Badge variant="secondary">{STATUS_LABELS[card.status]}</Badge> : null}
                </div>
                <p className="text-xs text-muted-foreground">{card.reason}</p>
              </div>
            ))}
            {result.overflow.length ? <p className="text-xs text-muted-foreground">也被触发但超出 2 张上限：{result.overflow.map((card) => `${card.title}${card.experience ? '〔她自己的经验〕' : ''}（${number.format(card.score)}）`).join('、')}</p> : null}
            {result.terms.length ? <p className="text-xs">词条：{result.terms.map((term) => term.term).join('、')}</p> : null}
            {result.blocked.length ? <p className="text-xs text-muted-foreground">关键词命中但被条件挡住：{result.blocked.map((item) => `${item.title}（${item.reason}）`).join('；')}</p> : null}
            {result.text ? <pre className="whitespace-pre-wrap rounded-md bg-muted/50 p-2 text-xs">{result.text}</pre> : null}
            <p className="text-xs text-muted-foreground">{result.rule}</p>
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}
