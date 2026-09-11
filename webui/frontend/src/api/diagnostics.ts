import { fetchJson } from './client'

export type DiagnosticHealth = 'healthy' | 'empty' | 'not_configured' | 'probe_error' | 'drift' | 'repairing'

export interface DiagnosticCheck {
  name: string
  health: DiagnosticHealth
  source: string
  checked_at: string
  evidence: Record<string, unknown>
}

export interface IndexDiagnostics {
  health: DiagnosticHealth
  source: 'wave_memory_readonly_diagnostics'
  checked_at: string
  evidence: {
    read_only: true
    probe_count: number
    health_counts: Record<string, number>
  }
  checks: DiagnosticCheck[]
}

export function getIndexDiagnostics(signal?: AbortSignal): Promise<IndexDiagnostics> {
  return fetchJson<IndexDiagnostics>('/api/diagnostics/indexes', { signal, timeoutMs: 30000 })
}
