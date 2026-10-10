// Third-party chart attribution, rendered by the About/Credits modal (tj-grna9p.54).
//
// Lightweight Charts is Apache-2.0 and its README requires "the attribution notice from the NOTICE
// file and a link to https://www.tradingview.com/" on a page the users can reach. PriceChart turns
// the on-chart logo off (attributionLogo: false, tj-grna9p.1 ruling), so this text and link are the
// whole of the obligation: the About/Credits modal must render CHART_CREDITS and stay reachable
// from every screen. The NOTICE text below is the upstream NOTICE file at v5.2.1, verbatim.

export const TRADINGVIEW_URL = 'https://www.tradingview.com/'

export interface ThirdPartyNotice {
  name: string
  licence: string
  /** The attribution text the licence asks to be shown. */
  notice: string
  /** Where the link in the notice points. */
  url: string
}

export const LIGHTWEIGHT_CHARTS_NOTICE =
  'TradingView Lightweight Charts™\nCopyright (с) 2025 TradingView, Inc. https://www.tradingview.com/'

// Apache ECharts ships a NOTICE file as well; it is carried here with the same obligation.
export const ECHARTS_NOTICE =
  'Apache ECharts\nCopyright 2017-2026 The Apache Software Foundation\n\nThis product includes software developed at\nThe Apache Software Foundation (https://www.apache.org/).'

export const CHART_CREDITS: readonly ThirdPartyNotice[] = [
  {
    name: 'TradingView Lightweight Charts',
    licence: 'Apache-2.0',
    notice: LIGHTWEIGHT_CHARTS_NOTICE,
    url: TRADINGVIEW_URL,
  },
  {
    name: 'Apache ECharts',
    licence: 'Apache-2.0',
    notice: ECHARTS_NOTICE,
    url: 'https://echarts.apache.org/',
  },
]
