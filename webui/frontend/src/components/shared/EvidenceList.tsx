import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useIsMobile } from '@/hooks/use-mobile'
import { ObjectDeepLink } from './ObjectDeepLink'
import { ResponsiveDetail } from './ResponsiveDetail'
import type { EvidenceRef } from './types'

export interface EvidenceListProps {
  evidence?: EvidenceRef[] | null
  objectPath?: string | ((evidence: EvidenceRef) => string)
  emptyDescription?: string
}

const AVAILABILITY_LABELS: Record<NonNullable<EvidenceRef['availability']>, string> = {
  available: '可用',
  unavailable: '不可用',
  quarantined: '已隔离',
  unknown: '未知',
}

function Availability({ value }: { value?: EvidenceRef['availability'] }) {
  const status = value ?? 'unknown'
  return <Badge variant={status === 'available' ? 'default' : status === 'quarantined' ? 'secondary' : 'outline'}>{AVAILABILITY_LABELS[status]}</Badge>
}

function formatCapturedAt(value: EvidenceRef['captured_at']): string {
  if (value === undefined || value === null || value === '') return '未知'
  if (typeof value === 'string' && Number.isNaN(Number(value))) return value
  const seconds = Number(value)
  if (!Number.isFinite(seconds) || seconds <= 0) return '未知'
  const millis = seconds < 10_000_000_000 ? seconds * 1000 : seconds
  const date = new Date(millis)
  return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN')
}

function formatSourceScope(value: EvidenceRef['source_scope']): string {
  if (value === undefined || value === null || value === '') return '未知'
  if (typeof value === 'string') {
    const parts = value.split('|').map((part) => part.trim()).filter(Boolean)
    return parts.length ? parts.join(' · ') : value
  }
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>
    const session = record.session
    const sessionId = session && typeof session === 'object' ? String((session as { id?: unknown }).id ?? '') : ''
    const parts = [record.bot_id, sessionId || record.session_id, record.visibility].map((part) => String(part ?? '').trim()).filter(Boolean)
    return parts.length ? parts.join(' · ') : '未知'
  }
  return String(value)
}

function EvidenceDetails({ item, objectPath }: { item: EvidenceRef; objectPath?: EvidenceListProps['objectPath'] }) {
  const path = typeof objectPath === 'function' ? objectPath(item) : objectPath
  return (
    <div data-slot="evidence-details" className="flex flex-col gap-4">
      {item.summary ? <p className="text-sm leading-6">{item.summary}</p> : null}
      <dl className="grid gap-3 sm:grid-cols-2">
        <div><dt className="font-medium text-muted-foreground">类型</dt><dd>{item.type}</dd></div>
        <div><dt className="font-medium text-muted-foreground">稳定编号</dt><dd className="break-all font-mono">{item.id}</dd></div>
        <div><dt className="font-medium text-muted-foreground">内容哈希</dt><dd className="break-all font-mono">{item.content_hash ?? '未知'}</dd></div>
        <div><dt className="font-medium text-muted-foreground">采集时间</dt><dd>{formatCapturedAt(item.captured_at)}</dd></div>
        <div><dt className="font-medium text-muted-foreground">来源群</dt><dd className="break-all">{formatSourceScope(item.source_scope)}</dd></div>
        <div><dt className="font-medium text-muted-foreground">可用状态</dt><dd><Availability value={item.availability} /></dd></div>
      </dl>
      {path && item.object_ref ? <ObjectDeepLink to={path} objectRef={item.object_ref}>打开这条证据</ObjectDeepLink> : null}
    </div>
  )
}

export function EvidenceList({ evidence, objectPath, emptyDescription }: EvidenceListProps) {
  const isMobile = useIsMobile()
  const items = Array.isArray(evidence) ? evidence : []
  if (items.length === 0) {
    return (
      <p data-slot="evidence-list" data-evidence-layout="empty" className="rounded-md border border-dashed bg-muted/10 p-3 text-sm text-muted-foreground">
        {emptyDescription ?? '当前没有可展示的证据。'}
      </p>
    )
  }

  if (isMobile) {
    return (
      <ul data-slot="evidence-list" className="flex flex-col gap-3" data-evidence-layout="mobile">
        {items.map((item) => (
          <li key={`${item.type}:${item.id}`} className="flex flex-col gap-3 rounded-lg border bg-card p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="font-medium">{item.summary || item.type}</p>
                <p className="break-all font-mono text-sm text-muted-foreground">{item.id}</p>
              </div>
              <Availability value={item.availability} />
            </div>
            <ResponsiveDetail title={`证据 ${item.id}`} description="完整证据引用与本群跳转入口">
              <EvidenceDetails item={item} objectPath={objectPath} />
            </ResponsiveDetail>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <Table data-evidence-layout="table">
      <TableHeader>
        <TableRow>
          <TableHead>类型</TableHead>
          <TableHead>摘要 / 编号</TableHead>
          <TableHead>来源群</TableHead>
          <TableHead>状态</TableHead>
          <TableHead><span className="sr-only">操作</span></TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <TableRow key={`${item.type}:${item.id}`}>
            <TableCell>{item.type}</TableCell>
            <TableCell className="max-w-72">
              <div className="flex min-w-0 flex-col">
                {item.summary ? <span className="truncate">{item.summary}</span> : null}
                <span className="truncate font-mono text-xs text-muted-foreground">{item.id}</span>
              </div>
            </TableCell>
            <TableCell>{formatSourceScope(item.source_scope)}</TableCell>
            <TableCell><Availability value={item.availability} /></TableCell>
            <TableCell className="text-right">
              <ResponsiveDetail title={`证据 ${item.id}`} description="完整证据引用与本群跳转入口">
                <EvidenceDetails item={item} objectPath={objectPath} />
              </ResponsiveDetail>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
