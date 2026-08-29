import { ArrowDownToLineIcon, ArrowRightIcon, ArrowUpFromLineIcon, DatabaseIcon, RouteIcon } from 'lucide-react'

import type { TagGraphNode, TagGraphPathPayload } from '@/api/tagGraph'
import { ObjectDeepLink } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

function percent(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value || 0)) * 100)}%`
}

export interface TagGraphDetailProps {
  node: TagGraphNode | null
  sourceRef?: string | null
  targetRef?: string | null
  path?: TagGraphPathPayload | null
  pathLoading?: boolean
  onSetSource: (node: TagGraphNode) => void
  onSetTarget: (node: TagGraphNode) => void
  onRunPath: () => void
  onClearPath: () => void
}

export function TagGraphDetail({ node, sourceRef, targetRef, path, pathLoading, onSetSource, onSetTarget, onRunPath, onClearPath }: TagGraphDetailProps) {
  return <Card className="h-fit border-border/60" data-tag-graph-detail>
    <CardHeader className="border-b pb-3"><CardTitle className="text-sm">标签详情与路径</CardTitle><CardDescription>跳转只打开服务端给出的本群链接；本面板不能改或删标签。</CardDescription></CardHeader>
    <CardContent className="flex flex-col gap-4 p-4">
      {!node ? <p className="text-sm text-muted-foreground">选择一个标签节点，查看来源、关联记忆和入出度。</p> : <>
        <div><div className="flex flex-wrap items-center gap-2"><h2 className="text-lg font-semibold">{node.name}</h2><Badge variant="outline">{node.type}</Badge><Badge variant="secondary">只读</Badge></div>{node.description ? <p className="mt-2 text-sm text-muted-foreground">{node.description}</p> : null}</div>
        <dl className="grid grid-cols-2 gap-2 text-sm"><div className="rounded-md border p-2"><dt className="text-xs text-muted-foreground">关联记忆</dt><dd className="mt-1 font-semibold tabular-nums">{node.memory_count}</dd></div><div className="rounded-md border p-2"><dt className="text-xs text-muted-foreground">置信度</dt><dd className="mt-1 font-semibold tabular-nums">{percent(node.confidence)}</dd></div><div className="rounded-md border p-2"><dt className="flex items-center gap-1 text-xs text-muted-foreground"><ArrowDownToLineIcon className="size-3" />入度</dt><dd className="mt-1 font-semibold tabular-nums">{node.in_degree} · {node.in_weight.toFixed(2)}</dd></div><div className="rounded-md border p-2"><dt className="flex items-center gap-1 text-xs text-muted-foreground"><ArrowUpFromLineIcon className="size-3" />出度</dt><dd className="mt-1 font-semibold tabular-nums">{node.out_degree} · {node.out_weight.toFixed(2)}</dd></div></dl>
        <div><p className="text-xs font-medium text-muted-foreground">来源</p><div className="mt-2 flex flex-wrap gap-2">{Object.entries(node.source_counts).map(([source, count]) => <Badge key={source} variant="outline">{source} · {count}</Badge>)}</div></div>
        <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant={sourceRef === node.ref ? 'secondary' : 'outline'} onClick={() => onSetSource(node)}>设为起点</Button><Button type="button" size="sm" variant={targetRef === node.ref ? 'secondary' : 'outline'} onClick={() => onSetTarget(node)}>设为终点</Button></div>
        <div><p className="flex items-center gap-1 text-xs font-medium text-muted-foreground"><DatabaseIcon className="size-3.5" />最近关联记忆</p><div className="mt-2 grid gap-2">{node.associated_memories.length ? node.associated_memories.map((memory) => <article key={memory.ref ?? memory.id} className="rounded-md border p-2"><p className="line-clamp-3 text-sm">{memory.content}</p><div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{memory.sender || '未知来源'} · {memory.tag_source} · 相关度 {memory.relevance.toFixed(2)}</span><ObjectDeepLink to="/memories" objectRef={memory.object_ref}>查看记忆</ObjectDeepLink></div></article>) : <p className="text-xs text-muted-foreground">当前群没有健康、已解析的关联记忆。</p>}</div></div>
      </>}
      <div className="border-t pt-4"><div className="flex flex-wrap items-center gap-2"><RouteIcon className="size-4 text-muted-foreground" /><span className="text-sm font-medium">标签路径</span><Button type="button" size="sm" disabled={!sourceRef || !targetRef || pathLoading} onClick={onRunPath}>查询当前可见图层</Button><Button type="button" size="sm" variant="ghost" disabled={!sourceRef && !targetRef && !path} onClick={onClearPath}>清除</Button></div><p className="mt-2 text-xs text-muted-foreground">隐藏图层不会被用来找路；路径只走有向边。</p>{pathLoading ? <p role="status" className="mt-3 text-sm">正在计算路径…</p> : path ? path.found ? <ol className="mt-3 flex flex-wrap items-center gap-2 text-sm">{path.nodes.map((item, index) => <li key={item.id} className="flex items-center gap-2"><Badge variant="secondary">{item.name}</Badge>{index < path.nodes.length - 1 ? <ArrowRightIcon className="size-3.5 text-muted-foreground" /> : null}</li>)}</ol> : <p className="mt-3 text-sm text-muted-foreground">当前可见图层内没有有向路径。</p> : null}</div>
    </CardContent>
  </Card>
}
