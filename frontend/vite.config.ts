import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'

export default defineConfig(({ mode }) => ({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  build: {
    outDir: fileURLToPath(new URL('../static/app', import.meta.url)),
    emptyOutDir: true,
    manifest: 'manifest.json',
    sourcemap: mode === 'development',
    rollupOptions: {
      input: fileURLToPath(new URL('src/main.ts', import.meta.url)),
    },
  },
}))
