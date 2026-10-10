// Vitest setup (vitest.config.ts setupFiles). jsdom has no window.matchMedia, which the PrimeVue
// Select (the top bar's time zone control) calls when it mounts. A stub that matches nothing is
// enough: no spec depends on a media query.
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList
}
