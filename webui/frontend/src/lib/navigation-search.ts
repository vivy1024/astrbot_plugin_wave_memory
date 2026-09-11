const SHARED_SCOPE_QUERY_KEYS = ['bot_id', 'session_id', 'visibility'] as const

export function sharedScopeSearch(search: string): string {
  const source = new URLSearchParams(search)
  const scoped = new URLSearchParams()

  for (const key of SHARED_SCOPE_QUERY_KEYS) {
    const value = source.get(key)
    if (value !== null && value !== '') {
      scoped.set(key, value)
    }
  }

  const query = scoped.toString()
  return query ? `?${query}` : ''
}

export function scopedHref(
  pathname: string,
  search = '',
  extra: Record<string, string | number | null | undefined> = {},
): string {
  const source = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
  const params = new URLSearchParams()
  for (const key of SHARED_SCOPE_QUERY_KEYS) {
    const value = source.get(key)
    if (value) params.set(key, value)
  }
  for (const [key, value] of Object.entries(extra)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const query = params.toString()
  return query ? `${pathname}?${query}` : pathname
}
