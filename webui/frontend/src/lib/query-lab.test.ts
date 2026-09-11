import { describe, expect, it } from 'vitest'

import type { QueryDebugResponse } from '@/api/memories'
import {
  QUERY_PARAM_SPECS,
  QUERY_STAGE_NAMES,
  compareQueryRuns,
  diffStages,
  readStageState,
  stageOutcome,
} from '@/lib/query-lab'

function run(
  results: Array<{ id: number; content?: string; similarity?: number }>,
  debug: QueryDebugResponse['debug'] = {},
  totalMs = 10,
): QueryDebugResponse {
  return {
    results: results.map((item) => ({ content: `内容 ${item.id}`, ...item })),
    timing: { total_ms: totalMs, embedding_ms: 1 },
    debug,
    readonly: true,
    touch: false,
  }
}

const stage = (enabled: boolean, available: boolean, reason_code?: string) => ({ enabled, available, reason_code })

describe('算法实验室对比逻辑', () => {
  it('按 id 归类共同命中与各自独有，并给出重合度', () => {
    const a = run([{ id: 1 }, { id: 2 }, { id: 3 }])
    const b = run([{ id: 2 }, { id: 3 }, { id: 9 }])
    const { rows, summary } = compareQueryRuns(a, b)
    expect(summary.common).toBe(2)
    expect(summary.aOnly).toBe(1)
    expect(summary.bOnly).toBe(1)
    // 并集 4 个 id，共同 2 个 => 50%
    expect(summary.overlapRatio).toBe(50)
    expect(summary.sameSet).toBe(false)
    // 共同命中排在独有之前
    expect(rows.slice(0, 2).every((row) => row.type === 'common')).toBe(true)
  })

  it('命中集合与顺序都一致时标出 sameSet 与 sameOrder', () => {
    const a = run([{ id: 1 }, { id: 2 }])
    const b = run([{ id: 1 }, { id: 2 }])
    const { summary } = compareQueryRuns(a, b)
    expect(summary.sameOrder).toBe(true)
    expect(summary.sameSet).toBe(true)
    expect(summary.overlapRatio).toBe(100)
  })

  it('命中集合相同但排序不同时 sameOrder 为 false', () => {
    const a = run([{ id: 1 }, { id: 2 }])
    const b = run([{ id: 2 }, { id: 1 }])
    const { summary } = compareQueryRuns(a, b)
    expect(summary.sameOrder).toBe(false)
    expect(summary.sameSet).toBe(true)
    expect(summary.common).toBe(2)
  })

  it('两组都为空时不报错，重合度按 100 处理', () => {
    const { rows, summary } = compareQueryRuns(run([]), run([]))
    expect(rows).toEqual([])
    expect(summary.overlapRatio).toBe(100)
    expect(summary.sameSet).toBe(true)
  })

  it('排名与相似度分别来自各自那一组', () => {
    const a = run([{ id: 7, similarity: 0.91 }])
    const b = run([{ id: 7, similarity: 0.42 }])
    const [row] = compareQueryRuns(a, b).rows
    expect(row.rankA).toBe(1)
    expect(row.rankB).toBe(1)
    expect(row.similarityA).toBe(0.91)
    expect(row.similarityB).toBe(0.42)
  })

  it('缺 id 的结果用下标兜底，不会把两条并成一条', () => {
    const a = run([{ id: undefined as unknown as number }, { id: undefined as unknown as number }])
    expect(compareQueryRuns(a, a).rows).toHaveLength(2)
  })
})

describe('算法实验室阶段判定', () => {
  it('启用且可用 = 生效；启用但不可用 = 降级并保留 reason_code', () => {
    expect(stageOutcome(readStageState({ geo: stage(true, true) } as never, 'geodesic'))).toBe('not_reported')
    expect(readStageState({ geodesic: stage(true, false, 'geodesic_rerank_failed') }, 'geodesic'))
      .toEqual({ enabled: true, available: false, reasonCode: 'geodesic_rerank_failed' })
    expect(stageOutcome({ enabled: true, available: false, reasonCode: 'x' })).toBe('degraded')
    expect(stageOutcome({ enabled: false, available: false, reasonCode: 'disabled_by_request' })).toBe('disabled')
  })

  it('只报告了部分阶段时，缺失阶段标记为无数据而不是伪造降级', () => {
    const state = readStageState({ epa: stage(true, true) }, 'spike')
    expect(state).toEqual({ enabled: false, available: false, reasonCode: 'not_reported' })
    expect(stageOutcome(state)).toBe('not_reported')
  })

  it('阶段差异只标出结论变化的行', () => {
    const a = run([], { epa: stage(true, true), spike: stage(true, false, 'x') })
    const b = run([], { epa: stage(true, true), spike: stage(false, false, 'disabled_by_request') })
    const diffs = diffStages(a, b)
    expect(diffs.find((row) => row.stage === 'epa')?.changed).toBe(false)
    expect(diffs.find((row) => row.stage === 'spike')?.changed).toBe(true)
    expect(diffs).toHaveLength(QUERY_STAGE_NAMES.length)
  })

  it('参数白名单覆盖服务端接受的 5 个键且范围与后端一致', () => {
    const expected = {
      pyramid_max_levels: [1, 10],
      pyramid_top_k: [1, 50],
      spike_max_hops: [0, 16],
      spike_firing_threshold: [0, 1],
      geodesic_alpha: [0, 1],
    }
    expect(QUERY_PARAM_SPECS.map((spec) => spec.name).sort()).toEqual(Object.keys(expected).sort())
    for (const spec of QUERY_PARAM_SPECS) {
      expect([spec.min, spec.max]).toEqual(expected[spec.name])
    }
  })
})
