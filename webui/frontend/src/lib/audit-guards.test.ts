import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// src/lib/ -> webui/frontend（前端根）；插件根还要再上两级
const ROOT = resolve(__dirname, '../..')
const PLUGIN_ROOT = resolve(ROOT, '../..')
const read = (relative: string) => readFileSync(resolve(ROOT, relative), 'utf-8')
const readPlugin = (relative: string) => readFileSync(resolve(PLUGIN_ROOT, relative), 'utf-8')

describe('全站审计回归锁：后端词语不直接暴露', () => {
  it('Beliefs 页的门禁与拒绝原因走 humanizeReason，不裸渲染 code', () => {
    const page = read('src/pages/beliefs/BeliefsPage.tsx')
    // 关系门禁区块：英文标签已改中文
    expect(page).not.toContain('>trust {')
    expect(page).not.toContain('>hostility {')
    expect(page).toContain('信任度')
    expect(page).toContain('敌意')
    // 内部术语 evidence-v1 不再出现在可见文案里
    expect(page).not.toContain('evidence-v1')
    // 原因经映射后再渲染
    expect(page).toContain("humanizeReason(item.gating.reason_code")
    expect(page).toContain("humanizeReason(item.actions.approve.reason_code")
  })

  it('维护页错误状态不打印 error_code 标签', () => {
    const page = read('src/pages/maintenance/MaintenancePage.tsx')
    expect(page).not.toContain("{detail.error_code ?? '无错误'}")
    expect(page).toContain('humanizeReason(detail.error_code')
    expect(page).toContain("from '@/lib/reason-label'")
  })

  it('People 页事件类型走中文映射，不留英文 code', () => {
    const page = read('src/pages/people/PeoplePage.tsx')
    expect(page).toContain('TIMELINE_EVENT_LABELS')
    expect(page).toContain('function timelineEventLabel')
    expect(page).not.toContain('${item.event_type}×${item.count}')
    expect(page).not.toContain('className="font-mono">{entry.event_type}<')
    // 未知取值回退到中性文案，而不是原样显示
    expect(page).toContain("?? '其他事件'")
  })
})

describe('全站审计回归锁：版本号来自后端', () => {
  it('侧栏不再硬编码版本徽章', () => {
    const sidebar = read('src/components/layout/WaveSidebar.tsx')
    expect(sidebar).not.toContain('<Badge variant="secondary">v1</Badge>')
    expect(sidebar).toContain('plugin_version')
    // 取不到时不显示，而不是编造
    expect(sidebar).toContain('setVersion(value || null)')
  })

  it('后端从 metadata.yaml 读版本并在 status 里返回', () => {
    const blueprint = readPlugin('webui/blueprints/system.py')
    expect(blueprint).toContain('def _plugin_version')
    expect(blueprint).toContain('metadata.yaml')
    expect(blueprint).toContain('"plugin_version": _plugin_version()')
  })

  it('前端类型声明了 plugin_version', () => {
    expect(read('src/api/system.ts')).toContain('plugin_version?: string')
  })
})

describe('全站审计回归锁：请求竞态守卫', () => {
  it('经历片段页丢弃过期响应', () => {
    const page = read('src/pages/knowledge/ExperiencesPage.tsx')
    expect(page).toContain('requestRef')
    expect(page).toContain('controller.abort()')
    expect(page).toContain('if (signal?.aborted) return')
    expect(page).toContain('isRequestCancelled')
  })

  it('系统配置页三条加载路径都有序号守卫', () => {
    const page = read('src/pages/settings/SettingsPage.tsx')
    for (const ref of ['schemaRequestRef', 'hotRequestRef', 'providersRequestRef']) {
      expect(page).toContain(ref)
    }
    // 每条路径都要比对序号后再写 state
    expect(page.match(/!== \w+RequestRef\.current/g)?.length).toBeGreaterThanOrEqual(6)
  })

  it('信念详情用序号守卫，且 ref 声明在正确组件内', () => {
    const page = read('src/pages/beliefs/BeliefsPage.tsx')
    const dialogIndex = page.indexOf('function BeliefEvidenceDialog')
    const refIndex = page.indexOf('const detailRequest = useRef(0)')
    const pageIndex = page.indexOf('export function BeliefsPage')
    // ref 必须在 BeliefEvidenceDialog 内、BeliefsPage 之前
    expect(refIndex).toBeGreaterThan(dialogIndex)
    expect(refIndex).toBeLessThan(pageIndex)
    expect(page).toContain('if (request !== detailRequest.current) return')
  })
})

describe('全站审计回归锁：错误信息不被固定说明顶掉', () => {
  it('QueryState 在 error 与 description 同时存在时两者都渲染', () => {
    const src = read('src/components/shared/QueryState.tsx')
    // 旧实现是 description ?? errorMessage(error)，有 description 就丢掉了真实错误
    expect(src).not.toContain('{description ?? errorMessage(error)}')
    expect(src).toContain('const failure = hasError ? errorMessage(error) :')
    expect(src).toContain('const note = description && description !== failure')
  })

  it('Jargon 页两处失败都保留并传出错误对象', () => {
    const page = read('src/pages/jargon/JargonPage.tsx')
    // catch 不能再吞掉错误
    expect(page).not.toContain('} catch {\n      if (request !== blocklistRequest.current) return')
    expect(page).toContain('setBlocklistError(reason)')
    expect(page).toContain('error={blocklistError}')
    // 目录已迁入广域黑话面板，仍必须保留真实错误并提供重试。
    expect(page).toContain('<GlobalJargonPanel botId={botId} />')
    const catalog = read('src/pages/jargon/GlobalJargonPanel.tsx')
    expect(catalog).toContain('setCatalogError(reason)')
    expect(catalog).toContain("humanizeApiError(catalogError, '内置资产读取失败')")
    expect(catalog).toContain('onClick={() => void loadCatalog()}>重试</Button>')
  })
})
