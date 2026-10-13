<script setup lang="ts">
// The page's breadcrumb trail (tj-grna9p.113): the way back up the hierarchy Datasets > dataset >
// Details without the browser's Back button. An item with a `to` is a link; the last item is the
// current page and is plain text with aria-current="page". The labels arrive ready to show: a screen
// runs its fixed words through titleCase() and passes a dataset's title as it already reads.
import { ChevronRight } from '@lucide/vue'
import type { RouteLocationRaw } from 'vue-router'

export interface BreadcrumbItem {
  label: string
  /** Where the crumb goes. Leave it out for the current page. */
  to?: RouteLocationRaw
  /** A hover title with more than the label says. A convenience only: the visible label is the name. */
  hint?: string
}

defineProps<{ items: readonly BreadcrumbItem[] }>()
</script>

<template>
  <nav
    class="crumbs"
    aria-label="Breadcrumb"
    data-testid="breadcrumbs"
  >
    <ol class="crumbs__list">
      <li
        v-for="(item, index) in items"
        :key="index"
        class="crumbs__item"
      >
        <RouterLink
          v-if="item.to !== undefined && index < items.length - 1"
          class="crumbs__link"
          :to="item.to"
          :title="item.hint"
          data-testid="breadcrumb-link"
        >
          {{ item.label }}
        </RouterLink>
        <span
          v-else
          class="crumbs__current"
          aria-current="page"
          :title="item.hint"
          data-testid="breadcrumb-current"
        >{{ item.label }}</span>
        <ChevronRight
          v-if="index < items.length - 1"
          class="crumbs__sep"
          :size="14"
          aria-hidden="true"
        />
      </li>
    </ol>
  </nav>
</template>

<style scoped>
.crumbs__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 4px;
  font-size: 12px;
}

.crumbs__item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.crumbs__link {
  color: var(--tj-accent-tint);
  text-decoration: none;
}

.crumbs__link:hover {
  text-decoration: underline;
}

.crumbs__current {
  color: var(--tj-text2);
}

.crumbs__sep {
  flex: 0 0 auto;
  color: var(--tj-text3);
}
</style>
