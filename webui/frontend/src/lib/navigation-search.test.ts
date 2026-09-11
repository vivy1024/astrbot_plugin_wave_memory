import { describe, expect, it } from 'vitest'

import { scopedHref, sharedScopeSearch } from '@/lib/navigation-search'

describe('sharedScopeSearch', () => {
  it('只保留跨正式路由共享的 scope 参数', () => {
    const search = sharedScopeSearch('?offset=40&bot_id=bot%3Ayushu&search=hello&session_id=qq%3Agroup%3A42&visibility=group&status=error&tab=trace&ref=signed&object_id=7&job_id=job-1&trace_id=trace-1&limit=20')

    expect(search).toBe('?bot_id=bot%3Ayushu&session_id=qq%3Agroup%3A42&visibility=group')
  })

  it('当前页面没有 scope 时生成无 search 的导航链接', () => {
    expect(sharedScopeSearch('?offset=20&limit=20&search=memory&status=active')).toBe('')
    expect(sharedScopeSearch('')).toBe('')
  })

  it('scopedHref 只携带共享 scope，并可附加人物或对象定位', () => {
    expect(scopedHref(
      '/people',
      '?offset=40&bot_id=bot%3Ayushu&search=hello&session_id=qq%3Agroup%3A42&visibility=group',
      { user_id: 'u1' },
    )).toBe('/people?bot_id=bot%3Ayushu&session_id=qq%3Agroup%3A42&visibility=group&user_id=u1')
    expect(scopedHref('/soul', '')).toBe('/soul')
  })
})
