// CSS custom properties generated from tokens.ts, so plain CSS (base.css, component styles) and
// the tokens cannot diverge. Names are --tj-<token>, with the camelCase key kebab-cased.
import { colors, fonts, radii, spacing } from './tokens'

const kebab = (key: string): string => key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)

const px = (value: number): string => `${value}px`

export function tokenCssVariables(): Record<string, string> {
  const vars: Record<string, string> = {}
  const set = (name: string, value: string): void => {
    vars[`--tj-${name}`] = value
  }

  for (const [key, value] of Object.entries(colors)) {
    if (typeof value === 'string') {
      set(kebab(key), value)
    } else if (!Array.isArray(value)) {
      for (const [inner, innerValue] of Object.entries(value)) {
        set(`${kebab(key)}-${kebab(inner)}`, innerValue as string)
      }
    }
  }
  colors.series.forEach((value, index) => set(`series-${index + 1}`, value))

  set('font-sans', fonts.sans)
  set('font-mono', fonts.mono)
  for (const [key, value] of Object.entries(radii)) set(`radius-${kebab(key)}`, px(value))
  for (const [key, value] of Object.entries(spacing)) {
    set(kebab(key), typeof value === 'number' ? px(value) : value)
  }
  return vars
}

export function applyTokenCssVariables(target: HTMLElement): void {
  for (const [name, value] of Object.entries(tokenCssVariables())) {
    target.style.setProperty(name, value)
  }
}
