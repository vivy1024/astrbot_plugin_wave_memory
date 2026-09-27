import { useState, type FormEvent } from 'react'
import { ArrowDownToLineIcon, ArrowUpFromLineIcon, DatabaseIcon, Edit3Icon } from 'lucide-react'
import { toast } from 'sonner'

import { updateTagCommand, type TagGraphNode, type TagGraphScope } from '@/api/tagGraph'
import { ObjectDeepLink } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import type { GraphTheme } from '@/lib/graph-palette'
import type { RelationGraph } from '@/lib/graph/types'
import { humanizeApiError } from '@/lib/reason-label'
import { PathEndpointButtons, Stat, StrongestNeighbors, TypeBadge } from './detail-shared'
import { formatTime } from './graph-format'

function percent(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value || 0)) * 100)}%`
}

function fixed2(value: unknown): string {
  const num = Number(value)
  return Number.isFinite(num) ? num.toFixed(2) : '—'
}

export interface TagNodeDetailProps {
  scope: TagGraphScope
  node: TagGraphNode
  graph: RelationGraph
  theme: GraphTheme
  recentWindowHours?: number
  pathFrom: string | null
  pathTo: string | null
  onSetSource: (id: string) => void
  onSetTarget: (id: string) => void
  onSelect: (id: string) => void
  onMutated: () => void
}

/** 标签节点详情：来源、关联记忆、入 / 出度；编辑沿用原「标签神经星云」的权威命令 + CAS 修订号流程。 */
export function TagNodeDetail({ scope, node, graph, theme, recentWindowHours, pathFrom, pathTo, onSetSource, onSetTarget, onSelect, onMutated }: TagNodeDetailProps) {
  const [editorOpen, setEditorOpen] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [typeDraft, setTypeDraft] = useState('')
  const [descDraft, setDescDraft] = useState('')
  const [aliasesDraft, setAliasesDraft] = useState('')
  const [saving, setSaving] = useState(false)

  const openEditor = () => {
    setNameDraft(node.name)
    setTypeDraft(node.type)
    setDescDraft(node.description || '')
    const aliases = (node.metadata?.aliases as string[] | undefined) ?? []
    setAliasesDraft(aliases.join(', '))
    setEditorOpen(true)
  }

  const saveTag = async (event: FormEvent) => {
    event.preventDefault()
    if (!node.object_ref) return
    if (!nameDraft.trim()) { toast.warning('Tag 名称不能为空'); return }
    setSaving(true)
    try {
      await updateTagCommand(scope, {
        object_ref: node.object_ref,
        revision: node.revision || 1,
        patch: {
          name: nameDraft.trim(),
          tag_type: typeDraft.trim() || undefined,
          description: descDraft.trim(),
          aliases: aliasesDraft.split(',').map((item) => item.trim()).filter(Boolean),
        },
      })
      toast.success('Tag 信息已更新')
      setEditorOpen(false)
      onMutated()
    } catch (failure) {
      toast.error(humanizeApiError(failure, '更新 Tag 失败'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-4" data-graph-detail="tags">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold break-all">{node.name}</h2>
          <TypeBadge type={node.type} theme={theme} />
          <Button
            type="button"
            size="xs"
            variant="outline"
            className="ml-auto"
            disabled={!node.object_ref}
            title={node.object_ref ? '通过权威命令安全编辑当前 Tag 资料' : '缺少服务端签发的对象引用，不能编辑'}
            onClick={openEditor}
          >
            <Edit3Icon data-icon="inline-start" aria-hidden="true" />编辑
          </Button>
        </div>
        {node.description ? <p className="mt-2 text-sm text-muted-foreground">{node.description}</p> : null}
      </div>

      <dl className="grid grid-cols-2 gap-2">
        <Stat label="关联记忆" value={node.memory_count} />
        <Stat label="置信度" value={percent(node.confidence)} />
        <Stat label="入度 · 权重" icon={<ArrowDownToLineIcon className="size-3" aria-hidden="true" />} value={`${node.in_degree} · ${fixed2(node.in_weight)}`} />
        <Stat label="出度 · 权重" icon={<ArrowUpFromLineIcon className="size-3" aria-hidden="true" />} value={`${node.out_degree} · ${fixed2(node.out_weight)}`} />
        <Stat label="创建于" value={formatTime(node.created_at)} />
        <Stat label="最近出现" value={formatTime(node.last_seen_ts ?? node.associated_memories?.[0]?.timestamp)} />
        {typeof node.recent_memory_count === 'number' ? (
          <Stat label={`最近 ${Math.round((recentWindowHours ?? 168) / 24)} 天的记忆`} value={node.recent_memory_count} />
        ) : null}
      </dl>

      <div>
        <p className="text-xs font-medium text-muted-foreground">来源</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {Object.entries(node.source_counts ?? {}).map(([source, count]) => <Badge key={source} variant="outline">{source} · {count}</Badge>)}
        </div>
      </div>

      <PathEndpointButtons nodeId={node.id} pathFrom={pathFrom} pathTo={pathTo} onSetSource={onSetSource} onSetTarget={onSetTarget} />

      <div>
        <p className="text-xs font-medium text-muted-foreground">最强关联</p>
        <div className="mt-1"><StrongestNeighbors graph={graph} nodeId={node.id} theme={theme} onSelect={onSelect} /></div>
      </div>

      <div>
        <p className="flex items-center gap-1 text-xs font-medium text-muted-foreground"><DatabaseIcon className="size-3.5" aria-hidden="true" />最近关联记忆</p>
        <div className="mt-2 grid gap-2">
          {node.associated_memories?.length ? node.associated_memories.map((memory) => (
            <article key={memory.ref ?? memory.id} className="rounded-md border p-2">
              <p className="line-clamp-3 text-sm">{memory.content}</p>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                <span>{memory.sender || '未知来源'} · {formatTime(memory.timestamp)} · 相关度 {fixed2(memory.relevance)}</span>
                <ObjectDeepLink to="/memories" objectRef={memory.object_ref}>查看记忆</ObjectDeepLink>
              </div>
            </article>
          )) : <p className="text-xs text-muted-foreground">当前群没有健康、已解析的关联记忆。</p>}
        </div>
      </div>

      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={saveTag} className="flex flex-col gap-3">
            <DialogHeader>
              <DialogTitle>编辑标签资料</DialogTitle>
              <DialogDescription>通过服务端权威命令更新当前群 Tag 属性，并使用 CAS 并发校验。</DialogDescription>
            </DialogHeader>
            <label className="flex flex-col gap-2 text-xs text-muted-foreground">标签名称
              <Input required value={nameDraft} onChange={(event) => setNameDraft(event.target.value)} placeholder="名称…" />
            </label>
            <label className="flex flex-col gap-2 text-xs text-muted-foreground">标签类型
              <Input required value={typeDraft} onChange={(event) => setTypeDraft(event.target.value)} placeholder="topic / entity / person…" />
            </label>
            <label className="flex flex-col gap-2 text-xs text-muted-foreground">别名（逗号分隔）
              <Input value={aliasesDraft} onChange={(event) => setAliasesDraft(event.target.value)} placeholder="别名1, 别名2…" />
            </label>
            <label className="flex flex-col gap-2 text-xs text-muted-foreground">描述
              <Textarea rows={3} value={descDraft} onChange={(event) => setDescDraft(event.target.value)} placeholder="描述信息…" />
            </label>
            <DialogFooter className="mt-2">
              <Button type="button" variant="outline" onClick={() => setEditorOpen(false)} disabled={saving}>取消</Button>
              <Button type="submit" disabled={saving}>{saving ? '保存中…' : '保存更改'}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
