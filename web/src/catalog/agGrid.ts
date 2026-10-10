// AG Grid Community setup (ADR tj-grna9p.2): the modules this app uses, registered once, and the
// theme built from the design tokens. Community only: never import ag-grid-enterprise (a lint rule
// bans it); the server-side row model is Enterprise, and the INFINITE row model used here is not.
import {
  InfiniteRowModelModule,
  ModuleRegistry,
  ValidationModule,
  colorSchemeDark,
  themeQuartz,
} from 'ag-grid-community'

import { colors, fontSizes, fonts } from '@/theme/tokens'

let registered = false

/** Register the grid modules (idempotent). Only what the screens use is listed. */
export function registerGridModules(): void {
  if (registered) return
  registered = true
  ModuleRegistry.registerModules([
    InfiniteRowModelModule,
        // Warnings for a misused grid option; development builds only.
    ...(import.meta.env.DEV ? [ValidationModule] : []),
  ])
}

// The theme maps AG Grid's parameters onto the tokens, so the grid matches the rest of the screen.
// Table density is the canvas's: 13px cells, 12px 500-weight headers, 9px 12px cell padding.
export const gridTheme = themeQuartz.withPart(colorSchemeDark).withParams({
  backgroundColor: colors.surface,
  foregroundColor: colors.text,
  headerBackgroundColor: colors.surface,
  headerTextColor: colors.text2,
  borderColor: colors.line,
  rowHoverColor: colors.raised,
  accentColor: colors.accentTint,
  fontFamily: fonts.sans,
  fontSize: fontSizes.body,
  headerFontSize: fontSizes.small,
  headerFontWeight: 500,
  cellHorizontalPadding: 12,
  rowHeight: 38,
  headerHeight: 36,
  wrapperBorder: false,
  wrapperBorderRadius: 0,
})
