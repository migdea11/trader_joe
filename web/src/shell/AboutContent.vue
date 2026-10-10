<script setup lang="ts">
// The body of the About and Credits modal (frame and open state: AboutModalHost.vue, useShellModals).
// The TradingView notice and link here are the Lightweight Charts attribution obligation, because
// the on-chart logo is off (tj-grna9p.1): keep them, and keep the modal reachable from every screen.
import { CHART_CREDITS, TRADINGVIEW_URL } from '@/components/charts/credits'
import { useUiConfigStore } from '@/stores/uiConfig'

import { THIRD_PARTY_CREDITS } from './credits'

const config = useUiConfigStore()
</script>

<template>
  <div class="about">
    <section aria-labelledby="about-versions">
      <h3
        id="about-versions"
        class="about__heading"
      >
        Versions
      </h3>
      <dl class="about__versions">
        <dt>Server</dt>
        <dd data-testid="about-server-version">
          {{ config.serverVersionText }}
        </dd>
        <dt>UI</dt>
        <dd data-testid="about-ui-version">
          {{ config.uiVersionText }}
        </dd>
      </dl>
    </section>

    <section aria-labelledby="about-charts">
      <h3
        id="about-charts"
        class="about__heading"
      >
        Charts
      </h3>
      <p class="about__lead">
        Charts are drawn with
        <a
          :href="TRADINGVIEW_URL"
          target="_blank"
          rel="noopener noreferrer"
          data-testid="tradingview-link"
        >TradingView Lightweight Charts (tradingview.com)</a>.
      </p>
      <ul class="about__list">
        <li
          v-for="credit in CHART_CREDITS"
          :key="credit.name"
          :data-testid="`credit-${credit.name}`"
        >
          <h4 class="about__name">
            {{ credit.name }} <span class="about__licence">{{ credit.licence }}</span>
          </h4>
          <pre class="about__notice">{{ credit.notice }}</pre>
          <a
            :href="credit.url"
            target="_blank"
            rel="noopener noreferrer"
          >{{ credit.name }} website</a>
        </li>
      </ul>
    </section>

    <section aria-labelledby="about-third-party">
      <h3
        id="about-third-party"
        class="about__heading"
      >
        Other Third-Party Software
      </h3>
      <ul class="about__list">
        <li
          v-for="credit in THIRD_PARTY_CREDITS"
          :key="credit.name"
          :data-testid="`credit-${credit.name}`"
        >
          <h4 class="about__name">
            {{ credit.name }} <span class="about__licence">{{ credit.licence }}</span>
          </h4>
          <p
            v-if="credit.copyright !== ''"
            class="about__line"
          >
            {{ credit.copyright }}
          </p>
          <p
            v-if="credit.note"
            class="about__line"
          >
            {{ credit.note }}
          </p>
          <a
            :href="credit.url"
            target="_blank"
            rel="noopener noreferrer"
          >{{ credit.name }} licence and website</a>
          <details
            v-if="credit.licenceText"
            class="about__details"
          >
            <summary>Full licence text for {{ credit.name }}</summary>
            <pre class="about__notice">{{ credit.licenceText }}</pre>
          </details>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.about {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-height: 60vh;
  overflow-y: auto;
  font-size: 13px;
}

.about__heading {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--tj-text);
}

.about__versions {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 16px;
  margin: 0;
}

.about__versions dt {
  color: var(--tj-text2);
}

.about__versions dd {
  margin: 0;
  font-family: var(--tj-font-mono);
}

.about__lead {
  margin: 0 0 12px;
  color: var(--tj-text2);
}

.about__list {
  display: flex;
  flex-direction: column;
  gap: 16px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.about__name {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 500;
}

.about__licence {
  margin-left: 6px;
  font-weight: 400;
  color: var(--tj-text3);
}

.about__line {
  margin: 0 0 4px;
  color: var(--tj-text2);
}

.about__notice {
  margin: 4px 0 8px;
  padding: 8px 10px;
  background: var(--tj-raised);
  border: 1px solid var(--tj-line);
  border-radius: 6px;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.about__details {
  margin-top: 6px;
  color: var(--tj-text2);
}

a {
  text-decoration: underline;
}
</style>
