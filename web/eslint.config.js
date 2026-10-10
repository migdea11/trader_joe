// ESLint flat config (ESLint 10).
//
// ADR tj-x5yghe guard rail: Composition API with <script setup> only, enforced here via
// vue/component-api-style so an Options API component (or script-setup mixed with Options
// API) fails lint rather than relying on review to catch it.
//
// tj-grna9p.3 (component kit decision): PrimeVue is the one ruled kit. no-restricted-imports
// bans the other kits that were considered and rejected, plus ag-grid-enterprise (tj-grna9p.12
// rules ag-grid-community only -- the Enterprise package is a paid license and must never be
// added, even though neither grid package is installed yet).
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import globals from 'globals'

const BANNED_KIT_MESSAGE =
  'PrimeVue is the one ruled component kit (tj-grna9p.3); do not add a second one without a decision record.'

// tj-mujie8 ruling 5 / tj-grna9p.60: Lucide (@lucide/vue, named imports) is the one icon set.
// lucide-vue-next is deprecated; primeicons and the other sets compared on the canvas are out.
const BANNED_ICON_MESSAGE =
  'Icons are Lucide via @lucide/vue (tj-mujie8); no other icon set and no primeicons classes.'
const NO_POLLING_MESSAGE =
  'No polling: setInterval is banned in web/src (tj-grna9p.29). Refetch on user action or on an event.'
const BANNED_ICON_PACKAGES = [
  'primeicons',
  'lucide-vue-next',
  'lucide-static',
  '@tabler/icons-vue',
  '@phosphor-icons/vue',
  '@heroicons/vue',
  '@mdi/js',
  '@mdi/font',
  'material-symbols',
  '@fortawesome/fontawesome-free',
  '@fortawesome/vue-fontawesome',
  'feather-icons',
  'vue-feather',
]

export default defineConfigWithVueTs(
  {
    ignores: ['dist/**', 'coverage/**', 'node_modules/**'],
  },
  js.configs.recommended,
  pluginVue.configs['flat/recommended'],
  vueTsConfigs.recommended,
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    rules: {
      'vue/component-api-style': ['error', ['script-setup']],
      'no-restricted-imports': [
        'error',
        {
          paths: [
            { name: 'naive-ui', message: BANNED_KIT_MESSAGE },
            { name: 'vuetify', message: BANNED_KIT_MESSAGE },
            { name: 'element-plus', message: BANNED_KIT_MESSAGE },
            { name: 'reka-ui', message: BANNED_KIT_MESSAGE },
            { name: 'radix-vue', message: BANNED_KIT_MESSAGE },
            {
              name: 'ag-grid-enterprise',
              message:
                'Only ag-grid-community is ruled (tj-grna9p.12); ag-grid-enterprise is a paid license and is never added.',
            },
            ...BANNED_ICON_PACKAGES.map((name) => ({ name, message: BANNED_ICON_MESSAGE })),
          ],
          patterns: [
            ...BANNED_ICON_PACKAGES.map((name) => ({
              group: [`${name}/*`],
              message: BANNED_ICON_MESSAGE,
            })),
          ],
        },
      ],
      // No primeicons classes ("pi pi-check") in app code: icons are Lucide.
      'no-restricted-syntax': [
        'error',
        {
          selector: 'Literal[value=/(^|\\s)pi pi-/]',
          message: BANNED_ICON_MESSAGE,
        },
        // No polling anywhere in web/src (tj-grna9p.29): the UI refetches on user action, and
        // phase 2 refreshes on events (tj-grna9p.75), never on an interval.
        { selector: "CallExpression[callee.name='setInterval']", message: NO_POLLING_MESSAGE },
        { selector: "CallExpression[callee.property.name='setInterval']", message: NO_POLLING_MESSAGE },
      ],
      'vue/no-restricted-syntax': [
        'error',
        {
          selector: 'VAttribute[value.value=/(^|\\s)pi pi-/]',
          message: BANNED_ICON_MESSAGE,
        },
      ],
    },
  },
)
