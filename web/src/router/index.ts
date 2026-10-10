import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import HomeView from '@/views/HomeView.vue'

// Vue Router (ADR tj-x5yghe). One route for now, proving the scaffold chain; the Data section's
// real routes (Datasets, Requests, Health, Usage) are planned in epic tj-grna9p's later tasks.
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home',
    component: HomeView,
  },
]

// The token showcase (tj-grna9p.60) is for the owner's visual check in dev only. Behind the
// compile-time DEV flag the dynamic import is dropped from the production build.
if (import.meta.env.DEV) {
  routes.push({
    path: '/dev/tokens',
    name: 'dev-tokens',
    component: () => import('@/views/dev/TokenShowcase.vue'),
  })
  // The chart showcase (tj-grna9p.53), same rule: dev only, synthetic data.
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

export default router
