import { useState } from 'react'
import { ArrowDownToLineIcon, ArrowRightIcon, ArrowUpFromLineIcon, DatabaseIcon, Edit3Icon, RouteIcon } from 'lucide-react'
import { toast } from 'sonner'

import type { TagGraphNode, TagGraphPathPayload, TagGraphScope } from '@/api/tagGraph'
import { updateTagCommand } from '@/api/tagGraph'
import { ObjectDeepLink } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { humanizeApiError } from '@/lib/reason-label'

function percent(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value || 0)) * 100)}%`
}

function fixed2(value: unknown): string {
  const num = Number(value)
  return Number.isFinite(num) ? num.toFixed(2) : '—'
}

export interface TagGraphDetailProps {
  scope?: TagGraphScope | null
  node: TagGraphNode | null
  sourceRef?: string | null
  targetRef?: string | null
  path?: TagGraphPathPayload | null
  pathLoading?: boolean
  onSetSource: (node: TagGraphNode) => void
  onSetTarget: (node: TagGraphNode) => void
  onRunPath: () => void
  onClearPath: () => void
  onMutated?: () => void
}

export function TagGraphDetail({ scope, node, sourceRef, targetRef, path, pathLoading, onSetSource, onSetTarget, onRunPath, onClearPath, onMutated }: TagGraphDetailProps) {
  const [editorOpen, setEditorOpen] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [typeDraft, setTypeDraft] = useState('')
  const [descDraft, setDescDraft] = useState('')
  const [aliasesDraft, setAliasesDraft] = useState('')
  const [saving, setSaving] = useState(false)

  const openEditor = () => {
    if (!node) return
    setNameDraft(node.name)
    setTypeDraft(node.type)
    setDescDraft(node.description || '')
    const aliases = (node.metadata?.aliases as string[] | undefined) ?? []
    setAliasesDraft(aliases.join(', '))
    setEditorOpen(true)
  }

  const saveTag = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!node || !scope || !node.object_ref) return
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
      onMutated?.()
    } catch (failure) {
      toast.error(humanizeApiError(failure, '更新 Tag 失败'))
    } finally {
      setSaving(false)
    }
  }

  return <Card className="h-fit border-sky-950/70 bg-sky-950/[.06] shadow-sm" data-tag-graph-detail>
    <CardHeader className="border-b pb-3"><CardTitle className="text-sm">标签详情与路径</CardTitle><CardDescription>查看本群节点与关联记忆；支持通过权威命令安全编辑当前 Tag 资料。</CardDescription></CardHeader>
    <CardContent className="flex flex-col gap-4 p-4">
      {!node ? <p className="text-sm text-muted-foreground">选择一个标签节点，查看来源、关联记忆和入出度。</p> : <>
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold">{node.name}</h2>
            <Badge variant="outline">{node.type}</Badge>
            <Button type="button" size="xs" variant="outline" className="ml-auto flex items-center gap-1" disabled={!node.object_ref} title={node.object_ref ? '通过权威命令安全编辑当前 Tag 资料' : '缺少服务端签发的对象引用，不能编辑'} onClick={openEditor}><Edit3Icon className="size-3" />编辑</Button>
          </div>
          {node.description ? <p className="mt-2 text-sm text-muted-foreground">{node.description}</p> : null}
        </div>
        <dl className="grid grid-cols-2 gap-2 text-sm"><div className="rounded-md border p-2"><dt className="text-xs text-muted-foreground">关联记忆</dt><dd className="mt-1 font-semibold tabular-nums">{node.memory_count}</dd></div><div className="rounded-md border p-2"><dt className="text-xs text-muted-foreground">置信度</dt><dd className="mt-1 font-semibold tabular-nums">{percent(node.confidence)}</dd></div><div className="rounded-md border p-2"><dt className="flex items-center gap-1 text-xs text-muted-foreground"><ArrowDownToLineIcon className="size-3" />入度</dt><dd className="mt-1 font-semibold tabular-nums">{node.in_degree} · {fixed2(node.in_weight)}</dd></div><div className="rounded-md border p-2"><dt className="flex items-center gap-1 text-xs text-muted-foreground"><ArrowUpFromLineIcon className="size-3" />出度</dt><dd className="mt-1 font-semibold tabular-nums">{node.out_degree} · {fixed2(node.out_weight)}</dd></div></dl>
        <div><p className="text-xs font-medium text-muted-foreground">来源</p><div className="mt-2 flex flex-wrap gap-2">{Object.entries(node.source_counts).map(([source, count]) => <Badge key={source} variant="outline">{source} · {count}</Badge>)}</div></div>
        <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant={sourceRef === node.ref ? 'secondary' : 'outline'} onClick={() => onSetSource(node)}>设为起点</Button><Button type="button" size="sm" variant={targetRef === node.ref ? 'secondary' : 'outline'} onClick={() => onSetTarget(node)}>设为终点</Button></div>
        <div><p className="flex items-center gap-1 text-xs font-medium text-muted-foreground"><DatabaseIcon className="size-3.5" />最近关联记忆</p><div className="mt-2 grid gap-2">{node.associated_memories.length ? node.associated_memories.map((memory) => <article key={memory.ref ?? memory.id} className="rounded-md border p-2"><p className="line-clamp-3 text-sm">{memory.content}</p><div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{memory.sender || '未知来源'} · {memory.tag_source} · 相关度 {fixed2(memory.relevance)}</span><ObjectDeepLink to="/memories" objectRef={memory.object_ref}>查看记忆</ObjectDeepLink></div></article>) : <p className="text-xs text-muted-foreground">当前群没有健康、已解析的关联记忆。</p>}</div></div>
      </>}
      <div className="border-t pt-4"><div className="flex flex-wrap items-center gap-2"><RouteIcon className="size-4 text-muted-foreground" /><span className="text-sm font-medium">标签路径</span><Button type="button" size="sm" disabled={!sourceRef || !targetRef || pathLoading} onClick={onRunPath}>查询当前可见图层</Button><Button type="button" size="sm" variant="ghost" disabled={!sourceRef && !targetRef && !path} onClick={onClearPath}>清除</Button></div><p className="mt-2 text-xs text-muted-foreground">隐藏图层不会被用来找路；路径只走有向边。</p>{pathLoading ? <p role="status" className="mt-3 text-sm">正在计算路径…</p> : path ? path.found ? <ol className="mt-3 flex flex-wrap items-center gap-2 text-sm">{path.nodes.map((item, index) => <li key={item.id} className="flex items-center gap-2"><Badge variant="secondary">{item.name}</Badge>{index < path.nodes.length - 1 ? <ArrowRightIcon className="size-3.5 text-muted-foreground" /> : null}</li>)}</ol> : <p className="mt-3 text-sm text-muted-foreground">当前可见图层内没有有向路径。</p> : null}</div>
    </CardContent>

    <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={saveTag} className="flex flex-col gap-3">
          <DialogHeader>
            <DialogTitle>编辑标签资料</DialogTitle>
            <DialogDescription>通过服务端权威命令更新当前群 Tag 属性，并使用 CAS 并发校验。</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <label className="text-xs text-muted-foreground">标签名称</label>
            <Input required value={nameDraft} onChange={(e) => setNameDraft(e.target.value)} placeholder="名称…" />
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-xs text-muted-foreground">标签类型</label>
            <Input required value={typeDraft} onChange={(e) => setTypeDraft(e.target.value)} placeholder="topic / entity / person…" />
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-xs text-muted-foreground">别名（逗号分隔）</label>
            <Input value={aliasesDraft} onChange={(e) => setAliasesDraft(e.target.value)} placeholder="别名1, 别名2…" />
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-xs text-muted-foreground">描述</label>
            <Textarea rows={3} value={descDraft} onChange={(e) => setDescDraft(e.target.value)} placeholder="描述信息…" />
          </div>
          <DialogFooter className="mt-2">
            <Button type="button" variant="outline" onClick={() => setEditorOpen(false)} disabled={saving}>取消</Button>
            <Button type="submit" disabled={saving}>{saving ? '保存中…' : '保存更改'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  </Card>
}

