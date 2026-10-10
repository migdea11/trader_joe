<script setup lang="ts">
// The About and Credits modal host: the frame and open state, mounted once by AppShell so it is
// reachable from every screen. The body is AboutContent.vue (a stub until tj-grna9p.54).
import Dialog from 'primevue/dialog'

import AboutContent from './AboutContent.vue'
import { useShellModals } from './useShellModals'

const { aboutOpen } = useShellModals()
</script>

<template>
  <Dialog
    v-model:visible="aboutOpen"
    modal
    header="About and Credits"
    :style="{ width: '32rem' }"
    :draggable="false"
  >
    <AboutContent />
  </Dialog>
</template>
