import { describe, expect, it } from 'vitest'

import { tagFixture } from './fixtures'
import { findLocalPath } from './path'
import { tagPayloadToGraph } from './tag-adapter'

describe('findLocalPath', () => {
  const { graph } = tagPayloadToGraph(tagFixture())

  it('有向：只沿边的方向走，优先高权重边', () => {
    // 1→4→2（relation:7，0.9）比 1→4→3→2 短
    expect(findLocalPath(graph, 'tag:1', 'tag:2', { directed: true, maxDepth: 8 })).toEqual({ nodes: ['tag:1', 'tag:4', 'tag:2'], edges: ['cooccurrence:1:4', 'relation:7'] })
    // 没有指向 tag:1 的边
    expect(findLocalPath(graph, 'tag:2', 'tag:1', { directed: true, maxDepth: 8 })).toBeNull()
  })

  it('无向与深度上限', () => {
    expect(findLocalPath(graph, 'tag:2', 'tag:1', { directed: false, maxDepth: 8 })?.nodes).toEqual(['tag:2', 'tag:4', 'tag:1'])
    expect(findLocalPath(graph, 'tag:1', 'tag:2', { directed: true, maxDepth: 1 })).toBeNull()
    expect(findLocalPath(graph, 'tag:1', 'tag:404', { directed: true, maxDepth: 8 })).toBeNull()
  })
})
