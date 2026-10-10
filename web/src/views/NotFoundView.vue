<script setup lang="ts">
// Any path no route matches, inside the shell so the way back (the Data tab) stays in reach.
</script>

<template>
  <section>
    <h1 class="page-title">
      Page Not Found
    </h1>
    <p class="page-note">
      Nothing lives at this address. Use the Data tab to go back.
    </p>
  </section>
</template>

<style scoped>
.page-title {
  margin: 0 0 8px;
  font-size: 20px;
  font-weight: 600;
}

.page-note {
  margin: 0;
  color: var(--tj-text2);
}
</style>
