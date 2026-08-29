export const PAGE_SIZE_OPTIONS = [25, 50, 100] as const
export const DEFAULT_PAGE_SIZE = PAGE_SIZE_OPTIONS[0]

export type PageSize = (typeof PAGE_SIZE_OPTIONS)[number]
export type TotalStatus = 'exact' | 'unavailable' | 'error'

export interface PageMetadata {
  total: number | null
  total_status: TotalStatus
  reason_code: string | null
  limit: PageSize
  offset: number
  page: number
  page_count: number | null
  has_more: boolean
}

export interface PageResponse<T> {
  items: T[]
  page: PageMetadata
}

export type ScopeOptionKind = 'bot' | 'session' | 'channel'

export interface ScopeOption {
  value: string
  label: string
  kind: ScopeOptionKind
  description?: string
  disabled?: boolean
}

export interface ObjectRefScopeQuery {
  bot_id?: string
  session_id?: string
  visibility?: string
  subject_principal_id?: string
}

export interface ObjectRefDescriptor {
  /** Opaque token signed or otherwise validated by the server. */
  ref: string
  kind?: string
  /** Server-bound locator; never sufficient without ref + Scope validation. */
  locator?: number | string
  scope_key?: string
  scope_query?: ObjectRefScopeQuery
  version?: number
}

export interface EvidenceRef {
  type: string
  id: string
  content_hash?: string | null
  captured_at?: string | number | null
  source_scope?: string | Record<string, unknown> | null
  availability?: 'available' | 'unavailable' | 'quarantined' | 'unknown'
  object_ref?: ObjectRefDescriptor | null
  summary?: string | null
}

export type QualityDecision = 'allow' | 'quarantine' | 'reject' | 'defer' | 'unknown'
export type OperationState =
  | 'queued'
  | 'running'
  | 'committed'
  | 'succeeded'
  | 'rolled_back'
  | 'failed'
  | 'cancelled'
  | 'unknown'
