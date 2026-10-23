import { mergeConfig, defineConfig, configDefaults } from 'vitest/config'
import viteConfig from './vite.config.ts'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test-setup.ts'],
      exclude: [...configDefaults.exclude],
      root: import.meta.dirname,
      // Whole-app specs are CPU-bound and exceed the 5s default under load.
      testTimeout: 20_000,
    },
  }),
)
