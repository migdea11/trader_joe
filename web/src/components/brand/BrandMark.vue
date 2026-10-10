<script setup lang="ts">
// The owner's logo mark (same geometry as web/public/favicon.svg, the favicon). Drawn inline so the
// strokes follow the theme tokens: --tj-accent-tint is the brackets' #a58aec and --tj-text the T J
// glyph's #e6edf3. Decorative by default, because the wordmark beside it already names the app;
// pass `label` where the mark stands alone and it becomes an image with that accessible name.
withDefaults(defineProps<{ size?: number; label?: string }>(), { size: 24, label: undefined })
</script>

<template>
  <svg
    class="brand-mark"
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 64 64"
    :width="size"
    :height="size"
    fill="none"
    stroke-linecap="round"
    stroke-linejoin="round"
    :role="label ? 'img' : undefined"
    :aria-label="label"
    :aria-hidden="label ? undefined : 'true'"
  >
    <path
      class="brand-mark__brackets"
      d="M13 14 H8 V50 H13 M51 14 H56 V50 H51"
      stroke-width="4"
    />
    <path
      class="brand-mark__glyph"
      d="M17 22 H31 M24 22 V42 M45 22 V36 A6 6 0 0 1 33 36"
      stroke-width="5"
    />
  </svg>
</template>

<style scoped>
.brand-mark {
  flex-shrink: 0;
}

.brand-mark__brackets {
  stroke: var(--tj-accent-tint);
}

.brand-mark__glyph {
  stroke: var(--tj-text);
}
</style>
