import { promises as fs } from 'node:fs'
import path from 'node:path'
import { brotliCompress, constants as zlibConstants, gzip } from 'node:zlib'
import { promisify } from 'node:util'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:9876'
const proxiedBackendRoute = { target: apiProxyTarget, changeOrigin: true }

const gzipAsync = promisify(gzip)
const brotliAsync = promisify(brotliCompress)

/** 预压缩的文件类型；图片/字体等已压缩格式不再重复压缩。 */
const PRECOMPRESS_EXTENSIONS = new Set([
  '.js',
  '.mjs',
  '.css',
  '.html',
  '.svg',
  '.json',
  '.txt',
  '.map',
  '.wasm',
])

/** 小于 1KB 的文件压缩收益为负（还可能因块头变大），直接跳过。 */
const MIN_PRECOMPRESS_BYTES = 1024

async function collectFiles(dir: string): Promise<string[]> {
  const entries = await fs.readdir(dir, { withFileTypes: true })
  const files = await Promise.all(
    entries.map(async (entry) => {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) return collectFiles(full)
      if (entry.name.endsWith('.gz') || entry.name.endsWith('.br')) return []
      return entry.isFile() ? [full] : []
    }),
  )
  return files.flat()
}

/**
 * 构建后为产物生成 .gz / .br 兄弟文件（不引入任何新依赖）。
 * 服务端 `webui/static_assets.py` 按 Accept-Encoding 直接返回这些文件。
 */
function precompressAssets() {
  let buildOutDir = path.resolve(__dirname, '../static/app')
  return {
    name: 'wavememory-precompress-assets',
    apply: 'build' as const,
    configResolved(config: { root: string; build: { outDir: string } }) {
      // 跟随 --outDir：临时构建不能去压缩仓库里已提交的 static/app
      buildOutDir = path.resolve(config.root, config.build.outDir)
    },
    async closeBundle() {
      // 只有 /static/app/assets/ 由服务端做编码协商，其余文件（index.html 等）不需要预压缩。
      const outDir = path.join(buildOutDir, 'assets')
      const files = await collectFiles(outDir)
      const results: Array<{ file: string; raw: number; gzip: number; br: number }> = []
      for (const file of files) {
        const ext = path.extname(file).toLowerCase()
        if (!PRECOMPRESS_EXTENSIONS.has(ext)) continue
        const raw = await fs.readFile(file)
        if (raw.byteLength < MIN_PRECOMPRESS_BYTES) continue
        const [gz, br] = await Promise.all([
          gzipAsync(raw, { level: 9 }),
          brotliAsync(raw, {
            params: {
              [zlibConstants.BROTLI_PARAM_QUALITY]: 11,
              [zlibConstants.BROTLI_PARAM_SIZE_HINT]: raw.byteLength,
            },
          }),
        ])
        await Promise.all([fs.writeFile(`${file}.gz`, gz), fs.writeFile(`${file}.br`, br)])
        results.push({
          file: path.relative(outDir, file).split(path.sep).join('/'),
          raw: raw.byteLength,
          gzip: gz.byteLength,
          br: br.byteLength,
        })
      }
      results.sort((a, b) => b.raw - a.raw)
      const kb = (n: number) => `${(n / 1024).toFixed(1)}KB`
      for (const r of results.slice(0, 15)) {
        console.log(
          `precompress ${r.file}  raw ${kb(r.raw)} -> gzip ${kb(r.gzip)} / br ${kb(r.br)}`,
        )
      }
      const totalRaw = results.reduce((sum, r) => sum + r.raw, 0)
      const totalGz = results.reduce((sum, r) => sum + r.gzip, 0)
      const totalBr = results.reduce((sum, r) => sum + r.br, 0)
      console.log(
        `precompress ${results.length} files  total ${kb(totalRaw)} -> gzip ${kb(totalGz)} / br ${kb(totalBr)}`,
      )
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), precompressAssets()],
  base: '/static/app/',
  server: {
    proxy: {
      '/api': proxiedBackendRoute,
    },
  },
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
