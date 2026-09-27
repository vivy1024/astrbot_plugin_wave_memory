export interface GraphScope {
  bot_id: string
  session_id: string
  visibility: 'group'
}

export function formatTime(seconds: number | undefined | null): string {
  if (!seconds || !Number.isFinite(seconds)) return '—'
  const date = new Date(seconds * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function memoryHref(scope: GraphScope, memoryId: number | string): string {
  const params = new URLSearchParams({ bot_id: scope.bot_id, session_id: scope.session_id, visibility: scope.visibility, memory_id: String(memoryId) })
  return `/memories?${params.toString()}`
}

