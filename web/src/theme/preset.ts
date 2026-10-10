// The PrimeVue 5 preset: Aura restyled to the canvas tokens (tj-grna9p.3, tj-grna9p.60). Dark
// colour scheme only. Every value comes from tokens.ts; the semantic tokens below are set to
// plain dark values (no light-dark()), so the result does not depend on the page's colour scheme.
//
// Accent rule (tokens.ts): the primary token (focus ring, links, checkbox and radio checked,
// tab bar, progress, slider, toggle switch: all shape-only or text uses) is the accent TINT. Only
// where a text label carries the meaning does the accent FILL appear: the primary button and the
// selected toggle segment, both overridden explicitly under `components`.
//
// Not specified by the canvas, and therefore derived, not invented: hover and active shades are
// the fill or tint mixed toward white (see HOVER_MIX, ACTIVE_MIX). No accent ramp exists in the
// spec, so every primary.50..950 step is the tint rather than an invented scale.
import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'

import { colors, fonts } from './tokens'

const HOVER_MIX = 15
const ACTIVE_MIX = 25

function towardWhite(color: string, percent: number): string {
  return `color-mix(in srgb, ${color}, ${colors.onAccent} ${percent}%)`
}

function tintAlpha(percent: number): string {
  return `color-mix(in srgb, ${colors.accentTint}, transparent ${100 - percent}%)`
}

const PRIMARY_STEPS = ['50', '100', '200', '300', '400', '500', '600', '700', '800', '900', '950']

const primaryRamp = Object.fromEntries(PRIMARY_STEPS.map((step) => [step, colors.accentTint]))

// Residual references to the surface ramp (components that name surface.N directly) resolve to
// the canvas neutrals: 0 and 50-100 text, 200-300 text2, 400-500 text3, 600-700 line, 800 raised,
// 900 surface, 950 ground.
const surfaceRamp = {
  0: colors.text,
  50: colors.text,
  100: colors.text,
  200: colors.text2,
  300: colors.text2,
  400: colors.text3,
  500: colors.text3,
  600: colors.line,
  700: colors.line,
  800: colors.raised,
  900: colors.surface,
  950: colors.ground,
}

const overlay = {
  background: colors.surface,
  borderColor: colors.line,
  color: colors.text,
}

export const traderJoePreset = definePreset(Aura, {
  semantic: {
    typography: {
      fontFamily: fonts.sans,
      fontSize: '0.8125rem',
    },
    primary: {
      ...primaryRamp,
      color: colors.accentTint,
      contrastColor: colors.ground,
      hoverColor: towardWhite(colors.accentTint, HOVER_MIX),
      activeColor: towardWhite(colors.accentTint, ACTIVE_MIX),
    },
    surface: surfaceRamp,
    formField: {
      background: 'transparent',
      disabledBackground: colors.raised,
      filledBackground: colors.raised,
      filledHoverBackground: colors.raised,
      filledFocusBackground: colors.raised,
      borderColor: colors.line,
      hoverBorderColor: colors.text3,
      focusBorderColor: colors.accentTint,
      invalidBorderColor: colors.down,
      color: colors.text,
      disabledColor: colors.text3,
      placeholderColor: colors.text3,
      invalidPlaceholderColor: colors.down,
      floatLabelColor: colors.text3,
      floatLabelFocusColor: colors.accentTint,
      floatLabelActiveColor: colors.text3,
      iconColor: colors.text2,
    },
    list: {
      option: {
        focusBackground: colors.raised,
        icon: { color: colors.text3, focusColor: colors.text2 },
      },
    },
    content: {
      background: colors.surface,
      hoverBackground: colors.raised,
      borderColor: colors.line,
      color: colors.text,
      hoverColor: colors.text,
    },
    mask: {
      background: 'rgba(0, 0, 0, 0.6)',
      color: colors.text2,
    },
    navigation: {
      item: {
        focusBackground: colors.raised,
        activeBackground: colors.raised,
        icon: { color: colors.text3, focusColor: colors.text2, activeColor: colors.text2 },
      },
      submenuIcon: { color: colors.text3, focusColor: colors.text2, activeColor: colors.text2 },
    },
    overlay: {
      select: overlay,
      popover: overlay,
      modal: overlay,
    },
    highlight: {
      background: tintAlpha(16),
      focusBackground: tintAlpha(24),
      color: colors.text,
      focusColor: colors.text,
    },
    text: {
      color: colors.text,
      hoverColor: colors.text,
      mutedColor: colors.text3,
      hoverMutedColor: colors.text2,
    },
  },
  components: {
    // A text label carries the meaning: the accent FILL with its white text.
    button: {
      root: {
        primary: {
          background: colors.accentFill,
          hoverBackground: towardWhite(colors.accentFill, HOVER_MIX),
          activeBackground: towardWhite(colors.accentFill, ACTIVE_MIX),
          borderColor: colors.accentFill,
          hoverBorderColor: towardWhite(colors.accentFill, HOVER_MIX),
          activeBorderColor: towardWhite(colors.accentFill, ACTIVE_MIX),
          color: colors.onAccent,
          hoverColor: colors.onAccent,
          activeColor: colors.onAccent,
        },
      },
    },
    // The selected toggle segment: the accent fill with its text. (The Live segment is orange
    // with DARK text; that is the shell's trading-group toggle, see semantics.ts GROUP_SPECS.)
    togglebutton: {
      root: {
        checkedColor: colors.onAccent,
      },
      content: {
        checkedBackground: colors.accentFill,
      },
    },
    // Density: the canvas table is 9px 12px cells with 12px/500 text2 headers.
    datatable: {
      headerCell: {
        background: 'transparent',
        color: colors.text2,
        padding: '9px 12px',
      },
      columnTitle: { fontWeight: '500', fontSize: '0.75rem' },
      bodyCell: { padding: '9px 12px' },
      row: { background: 'transparent' },
    },
  },
})
