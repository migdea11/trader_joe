// Third-party credits shown by the About and Credits modal (tj-grna9p.54), as data: the next
// runtime dependency that needs attribution is one more entry here. Every field below was read
// from the installed package's LICENSE or the vendored fonts' licence files, not recalled.
//
// The chart libraries' notices live next to the charts (components/charts/credits.ts, which carries
// the TradingView obligation) and are imported by the modal, not copied here.
import mono from '@/theme/fonts/LICENSE-ibm-plex-mono.txt?raw'
import sans from '@/theme/fonts/LICENSE-ibm-plex-sans.txt?raw'

export interface RuntimeCredit {
  name: string
  /** SPDX-style licence name, or the licence's own title where it has no SPDX id. */
  licence: string
  /** The copyright line as the licence file states it; empty where the package ships none. */
  copyright: string
  url: string
  /** A caveat worth showing next to the entry. */
  note?: string
  /** The full licence text, where it ships in this repository. */
  licenceText?: string
}

export const FONT_LICENCE_URL = 'http://scripts.sil.org/OFL'

export const THIRD_PARTY_CREDITS: readonly RuntimeCredit[] = [
  {
    name: 'vue-echarts',
    licence: 'MIT',
    copyright: 'Copyright (c) 2016-present GU Yiling & ECOMFE',
    url: 'https://github.com/ecomfe/vue-echarts',
  },
  {
    name: 'PrimeVue and @primeuix/themes',
    licence: 'PrimeUI Community License or Commercial License (not MIT)',
    copyright: 'Copyright (c) 2026 PrimeTek Informatics. All rights reserved.',
    url: 'https://primeui.dev/licenses/community',
    note: 'Distributed under the PrimeUI licence terms, which depend on the user of this deployment. A valid PrimeUI licence key (Community licence: annual renewal by confirming eligibility) is supplied at build time; without one PrimeVue displays a licence notice.',
  },
  {
    name: 'AG Grid Community (ag-grid-community and ag-grid-vue3)',
    licence: 'MIT',
    copyright: 'Copyright (c) 2015-2026 AG GRID LTD',
    url: 'https://www.ag-grid.com/',
    note: 'The Community edition only; no AG Grid Enterprise module is used.',
  },
  {
    name: 'Lucide',
    licence: 'ISC',
    copyright: 'Copyright (c) 2026 Lucide Icons and Contributors',
    url: 'https://lucide.dev/',
  },
  {
    name: 'Feather icons (as derived in Lucide)',
    licence: 'MIT',
    copyright: 'Copyright (c) 2013-present Cole Bemis',
    url: 'https://feathericons.com/',
  },
  {
    name: 'IBM Plex Sans',
    licence: 'SIL Open Font License 1.1',
    copyright: 'Copyright 2019 IBM Corp. All rights reserved.',
    url: 'https://github.com/IBM/plex',
    note: 'Packaged by Fontsource (@fontsource/ibm-plex-sans).',
    licenceText: sans,
  },
  {
    name: 'IBM Plex Mono',
    licence: 'SIL Open Font License 1.1',
    copyright: 'Copyright 2017 IBM Corp. All rights reserved.',
    url: 'https://github.com/IBM/plex',
    note: 'Packaged by Fontsource (@fontsource/ibm-plex-mono).',
    licenceText: mono,
  },
  {
    name: 'Vue',
    licence: 'MIT',
    copyright: 'Copyright (c) 2018-present, Yuxi (Evan) You',
    url: 'https://vuejs.org/',
  },
  {
    name: 'Vue Router',
    licence: 'MIT',
    copyright: 'Copyright (c) 2019-present Eduardo San Martin Morote',
    url: 'https://router.vuejs.org/',
  },
  {
    name: 'Pinia',
    licence: 'MIT',
    copyright: 'Copyright (c) 2019-present Eduardo San Martin Morote',
    url: 'https://pinia.vuejs.org/',
  },
  {
    name: '@bufbuild/protobuf',
    licence: 'Apache-2.0 AND BSD-3-Clause',
    copyright: '',
    url: 'https://github.com/bufbuild/protobuf-es',
    note: 'The installed package ships no licence file; the licence is the package.json licence field.',
  },
]
