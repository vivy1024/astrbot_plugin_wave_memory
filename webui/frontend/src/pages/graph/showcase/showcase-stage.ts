/**
 * 舞台底色用纯黑：3d-force-graph 的背景在经过 EffectComposer（辉光）后会被当成线性色再转一次 sRGB，
 * #04060b 这类深色会被抬成 #222a3b 的灰雾；只有 0 在转换前后不变。
 */
export const STAGE_BACKGROUND = '#000000'

export function supportsWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'))
  } catch {
    return false
  }
}
