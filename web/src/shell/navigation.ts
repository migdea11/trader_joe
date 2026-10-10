// The shell's navigation, as data. Sections are the top bar (tj-mujie8, canvas v18 Data boards);
// sub-tabs belong to the Data section. A destination with no `to` is DISABLED IN PLACE: it keeps its
// position (so the bar does not shift when it is built), shows "Not built yet" and never navigates.
import type { Component } from 'vue'
import {
  Activity,
  ArrowLeftRight,
  ChartCandlestick,
  ChartPie,
  Database,
  LayoutDashboard,
} from '@lucide/vue'

import { DATA_TABS, type DataTab } from '@/theme/copy'

export const NOT_BUILT_TOOLTIP = 'Not built yet'

export interface SectionItem {
  label: string
  icon: Component
  /** Absent: disabled in place, no route. */
  to?: string
}

export const DATA_HOME = '/data/datasets'

export const SECTIONS: readonly SectionItem[] = [
  { label: 'Overview', icon: LayoutDashboard },
  { label: 'Data', icon: Database, to: DATA_HOME },
  { label: 'Strategies', icon: ChartCandlestick },
  { label: 'Portfolio', icon: ChartPie },
  { label: 'Orders', icon: ArrowLeftRight },
  { label: 'Ops', icon: Activity },
]

export interface SubTabItem {
  label: DataTab
  path: string
  /** False: rendered disabled in place until its phase. The route still exists (placeholder). */
  built: boolean
}

const SUB_TAB_PATHS: Record<DataTab, { path: string; built: boolean }> = {
  Datasets: { path: '/data/datasets', built: true },
  Requests: { path: '/data/requests', built: false },
  Health: { path: '/data/health', built: false },
  Usage: { path: '/data/usage', built: false },
}

export const DATA_SUB_TABS: readonly SubTabItem[] = DATA_TABS.map((label) => ({
  label,
  ...SUB_TAB_PATHS[label],
}))
