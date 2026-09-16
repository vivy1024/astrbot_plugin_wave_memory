import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const ROOT = resolve(__dirname, '../../..')

function read(relative: string): string {
  return readFileSync(resolve(ROOT, relative), 'utf-8')
}

const CANVAS = 'src/components/tag-graph/TagGraphCanvas.tsx'
const PAGE = 'src/pages/tags/TagGraphPage.tsx'

// 标签图谱是刻意的深空观测台例外：按节点类型区分色相，不走 radix-nova 中性 token。
// 这里锁住的是「配色集中、不散落」——色板必须集中声明，且绘制逻辑不得内联裸 hex。
describe('标签图谱观测台配色', () => {
  it('色板集中声明，且覆盖全部节点类型', () => {
    const canvas = read(CANVAS)
    expect(canvas).toContain('TYPE_PALETTES')
    for (const type of ['keyword', 'entity', 'topic', 'emotion', 'fact', 'jargon', 'default']) {
      expect(canvas).toContain(`${type}:`)
    }
    // 每种类型都要有核心色、光晕、文字色三档
    expect(canvas.match(/core:/g)?.length ?? 0).toBeGreaterThanOrEqual(7)
    expect(canvas.match(/glow:/g)?.length ?? 0).toBeGreaterThanOrEqual(7)
    expect(canvas.match(/text:/g)?.length ?? 0).toBeGreaterThanOrEqual(7)
  })

  it('节点与连线取色走调色函数与 palette，不在绘制循环里内联 hex', () => {
    const canvas = read(CANVAS)
    expect(canvas).toContain('paletteFor(node.raw.type)')
    // 悬停已合并进统一节点绘制循环，不再单独绘制 hoveredNode。
    expect(canvas).toContain('node.raw.ref === hoveredNode?.ref')
    expect(canvas).toContain('isHovered ? 2.0 : 1.5')
    expect(canvas).toContain('radGrad.addColorStop(0, isLabHit ? palette.hitGlow : p.glow)')
    expect(canvas).toContain('ctx.fillStyle = isLabHit ? palette.ring : p.core')
    expect(canvas).toContain('ctx.fillStyle = isSelected || isLabHit ? palette.selectedText : p.text')
    const drawing = canvas.slice(canvas.indexOf('const render = () =>'), canvas.indexOf('// 鼠标交互'))
    expect(drawing).not.toMatch(/#[0-9a-f]{3,8}\b/i)
    expect(canvas).toContain('palette.edgeRelations')
    expect(canvas).toContain('palette.edgeCooccurrence')
    // 深空渐变背景三档色阶
    expect(canvas).toContain('palette.backgroundMid')
    expect(canvas).toContain('palette.backgroundOuter')
  })

  it('页面外壳正常嵌入 AppShell 布局，画布卡片保持深空观测台背景', () => {
    const page = read(PAGE)
    expect(page).toContain('data-page="tag-graph"')
    expect(page).not.toContain('fixed inset-0')
    const canvas = read(CANVAS)
    expect(canvas).toContain('bg-[#07101b]')
  })

  it('工具栏每个操作只有一份，不重复渲染', () => {
    const canvas = read(CANVAS)
    const toolbar = canvas.slice(canvas.indexOf('悬浮控制工具栏'), canvas.indexOf('状态徽章与图例'))
    const titles = toolbar.match(/title="[^"]+"/g) ?? []
    expect(new Set(titles).size).toBe(titles.length)
    for (const label of ['放大', '缩小', '复位视角']) {
      expect(titles.filter((item) => item === `title="${label}"`)).toHaveLength(1)
    }
  })

  it('全屏与常态定位互斥，relative 不会覆盖 fixed', () => {
    const canvas = read(CANVAS)
    // 桌面分支：从容器 ref 到 canvas 元素之间就是 className 的 cn(...) 块
    const block = canvas.slice(canvas.indexOf('ref={containerRef}'), canvas.indexOf('<canvas'))
    // 全屏分支必须带 fixed 定位并填满视口
    expect(block).toContain('fixed inset-0 z-50')
    // 常态分支才带 relative
    expect(block).toContain("'relative h-[38rem] w-full")
    // 基础样式行不得内联 relative，否则 Tailwind 里 .relative 会覆盖 .fixed
    const baseLine = block.split('\n').find((line) => line.includes('border-sky-950/80'))
    expect(baseLine).toBeDefined()
    expect(baseLine).not.toContain('relative')
    // relative 只能出现在常态分支那一行
    const linesWithRelative = block.split('\n').filter((line) => /'relative/.test(line))
    expect(linesWithRelative).toHaveLength(1)
    expect(linesWithRelative[0]).toContain('h-[38rem]')
  })
})
