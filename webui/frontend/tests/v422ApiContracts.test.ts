import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const apiDir = resolve(import.meta.dirname, '../src/api')
const beliefsSource = readFileSync(resolve(apiDir, 'beliefs.ts'), 'utf8')
const jargonSource = readFileSync(resolve(apiDir, 'jargon.ts'), 'utf8')

test('Beliefs v2 API is bot-level with status groups, diary evidence and history', () => {
  assert.match(beliefsSource, /export type BeliefStatus = 'sprout' \| 'formed' \| 'stable' \| 'shaken' \| 'faded' \| 'vetoed' \| 'legacy_archived'/)
  assert.match(beliefsSource, /groups: BeliefGroup\[\]/)
  assert.match(beliefsSource, /evidence: BeliefDiaryRef\[\]/)
  assert.match(beliefsSource, /history: BeliefHistoryItem\[\]/)
  assert.match(beliefsSource, /new URLSearchParams\(\{ bot_id: botId \}\)/)
  // 看法属于 Bot 本人：不再带群 Scope，也没有旧的逐条审核接口
  assert.doesNotMatch(beliefsSource, /scopeEnvelope/)
  assert.doesNotMatch(beliefsSource, /batch-lifecycle/)
  assert.doesNotMatch(beliefsSource, /approve/)
})

test('Beliefs mutations use the shared operation/revision envelope', () => {
  assert.match(beliefsSource, /operation: \{ kind: string; status: string; id\?: string \}/)
  assert.match(beliefsSource, /revision: number \| string \| null/)
  assert.match(beliefsSource, /body: JSON\.stringify\(\{ bot_id: botId, revision: item\.revision, reason \}\)/)
})

test('Scoped Jargon API uses explicit scope and canonical review/catalog routes', () => {
  assert.match(jargonSource, /limit: 25 \| 50 \| 100/)
  assert.match(jargonSource, /offset: number/)
  assert.match(jargonSource, /PageResponse<JargonItem>/)
  assert.match(jargonSource, /\/api\/jargon\/\$\{id\}\/review\//)
  assert.match(jargonSource, /\/api\/jargon\/holyman/)
  assert.doesNotMatch(jargonSource, /\/api\/jargon\/batch-delete/)
  assert.doesNotMatch(jargonSource, /\/api\/jargon\/toggle_global/)
})

test('Jargon mutations expose evidence and operation state instead of historical count fields', () => {
  assert.match(jargonSource, /anchors: EvidenceRef\[\]/)
  assert.match(jargonSource, /object_ref: ObjectRefDescriptor \| null/)
  assert.match(jargonSource, /operation: \{ status: string \}/)
})
