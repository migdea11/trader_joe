import { createApp } from 'vue'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'

import App from './App.vue'
import router from './router'
import './theme/fonts/fonts.css'
import './theme/base.css'
import { traderJoePreset } from './theme/preset'
import { applyTokenCssVariables } from './theme/cssVars'
import { primeUiLicenceKey } from './config/licence'

// PrimeVue 5, styled mode, Aura restyled to the canvas tokens (tj-grna9p.3, tj-grna9p.60).
// Dark colour scheme only: no dark-mode selector is configured and base.css pins color-scheme.
applyTokenCssVariables(document.documentElement)

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(PrimeVue, {
  // Empty when no key is built in: PrimeVue then shows its licence notice (src/config/licence.ts).
  license: primeUiLicenceKey(),
  theme: {
    preset: traderJoePreset,
    options: {
      darkModeSelector: false,
    },
  },
})

app.mount('#app')
