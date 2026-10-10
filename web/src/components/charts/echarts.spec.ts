import { describe, expect, it, vi } from 'vitest'

import { ECHARTS_THEME_NAME, echartsTheme } from '@/theme/charts'

// tj-grna9p.53: ECharts registration. echarts/core is spied (the real functions still run) so the
// spec can see what was registered and how often. Not covered here: an ECharts chart actually
// rendering through vue-echarts, which needs a canvas jsdom does not have.
const core = vi.hoisted(() => ({ use: vi.fn(), registerTheme: vi.fn() }))
vi.mock('echarts/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('echarts/core')>()
  core.use.mockImplementation(actual.use)
  core.registerTheme.mockImplementation(actual.registerTheme)
  return { ...actual, use: core.use, registerTheme: core.registerTheme }
})

describe('ECharts registration', () => {
  it('registers exactly the parts the Data screens draw, and the app theme once', async () => {
    const first = await import('./echarts')
    const again = await import('./echarts')
    expect(again).toBe(first)

    expect(Object.keys(first.REGISTERED_ECHARTS_PARTS).sort()).toEqual([
      'BarChart',
      'CanvasRenderer',
      'DataZoomComponent',
      'GridComponent',
      'LegendComponent',
      'LineChart',
      'MarkLineComponent',
      'TooltipComponent',
    ])
    expect(core.use).toHaveBeenCalledTimes(1)
    expect(core.use).toHaveBeenCalledWith(Object.values(first.REGISTERED_ECHARTS_PARTS))

    expect(core.registerTheme).toHaveBeenCalledTimes(1)
    expect(core.registerTheme).toHaveBeenCalledWith(ECHARTS_THEME_NAME, echartsTheme)
    expect(first.ECHARTS_THEME_NAME).toBe('trader_joe')
    expect(first.VChart).toBeDefined()
  })
})
