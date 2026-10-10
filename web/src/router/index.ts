import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import AppShell from '@/shell/AppShell.vue'
import { DATA_HOME } from '@/shell/navigation'
import { GROUP_QUERY_PARAM } from '@/stores/tradingGroup'
import DatasetsSidebar from '@/views/data/DatasetsSidebar.vue'
import DatasetsView from '@/views/data/DatasetsView.vue'
import DatasetViewerSidebar from '@/views/data/DatasetViewerSidebar.vue'
import DatasetViewerView from '@/views/data/DatasetViewerView.vue'
import NotBuiltView from '@/views/data/NotBuiltView.vue'
import NotFoundView from '@/views/NotFoundView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** The page title where the route has no screen of its own (the placeholder). */
    title?: string
  }
}

// Vue Router (ADR tj-x5yghe). Every product route is a child of the shell (shell/AppShell.vue), which
// renders two router views of the matched child: `default` (main area) and `sidebar` (the 248px
// column). A screen fills the sidebar by adding `sidebar: <Component>` to its route's `components`.
const shellChildren: RouteRecordRaw[] = [
  { path: '', redirect: DATA_HOME },
  { path: 'data', redirect: DATA_HOME },
  {
    path: 'data/datasets',
    name: 'data-datasets',
    components: { default: DatasetsView, sidebar: DatasetsSidebar },
  },
  {
    path: 'data/datasets/:id/viewer',
    name: 'data-dataset-viewer',
    components: { default: DatasetViewerView, sidebar: DatasetViewerSidebar },
  },
  // Later phases: the sub-tab is disabled in place and the route shows the placeholder.
  { path: 'data/requests', name: 'data-requests', component: NotBuiltView, meta: { title: 'Requests' } },
  { path: 'data/health', name: 'data-health', component: NotBuiltView, meta: { title: 'Health' } },
  { path: 'data/usage', name: 'data-usage', component: NotBuiltView, meta: { title: 'Usage' } },
  { path: ':pathMatch(.*)*', name: 'not-found', component: NotFoundView },
]

const routes: RouteRecordRaw[] = [{ path: '/', component: AppShell, children: shellChildren }]

// The dev showcases are for the owner's visual check only (tj-grna9p.60, tj-grna9p.53). Behind the
// compile-time DEV flag the dynamic import is dropped from the production build. They sit outside
// the shell.
if (import.meta.env.DEV) {
  routes.push({
    path: '/dev/tokens',
    name: 'dev-tokens',
    component: () => import('@/views/dev/TokenShowcase.vue'),
  })
  routes.push({
    path: '/dev/charts',
    name: 'dev-charts',
    component: () => import('@/views/dev/ChartShowcase.vue'),
  })
}

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

// The trading group travels in the URL (?group=paper): carry it across navigations that do not name
// one, so a tab or link click does not drop it. Other query parameters belong to their screen and do
// not travel.
router.beforeEach((to, from) => {
  const group = from.query[GROUP_QUERY_PARAM]
  if (group !== undefined && to.query[GROUP_QUERY_PARAM] === undefined) {
    return { ...to, query: { ...to.query, [GROUP_QUERY_PARAM]: group } }
  }
  return true
})

export default router
