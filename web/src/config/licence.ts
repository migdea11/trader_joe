// The PrimeUI licence key (risk tj-refgtb, bead tj-cwerrn). PrimeVue 5 takes it as the `license` option
// of app.use(PrimeVue, ...) (@primevue/core config: registerLicense({ primeui: options.license })) and
// verifies the Ed25519 signature offline in the browser, so a static SPA behind Caddy needs no network.
//
// The key is supplied at BUILD time through the Vite variable below and is never committed: this public
// repo only documents the name with an empty value. PrimeUI's Community licence says the key "may appear
// in your application bundle and contains no sensitive data", so bundling it is permitted, but it may not
// be published for others to use. Without a key the build and the tests still work; PrimeVue then
// logs a console warning and shows a small fixed "Invalid PrimeUI License" notice (pointer-events: none).
export const PRIMEUI_LICENCE_ENV = 'VITE_PRIMEUI_LICENSE_KEY'

/** The configured licence key, or '' when none is set (dev, test and CI builds). */
export function primeUiLicenceKey(env: { readonly VITE_PRIMEUI_LICENSE_KEY?: unknown } = import.meta.env): string {
  const value = env.VITE_PRIMEUI_LICENSE_KEY
  return typeof value === 'string' ? value.trim() : ''
}
