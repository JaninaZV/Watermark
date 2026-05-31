#!/usr/bin/env node
/**
 * KPI toggle test for generated dashboard.html (no npm deps).
 * Usage: node scripts/test_kpi_toggle.mjs path/to/dashboard.html
 */
import fs from 'node:fs';

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error('usage: node scripts/test_kpi_toggle.mjs path/to/dashboard.html');
  process.exit(2);
}

const html = fs.readFileSync(htmlPath, 'utf8');
const marker = 'window.WATERMARK_DATA = ';
const start = html.indexOf(marker);
const end = html.indexOf('};</script>', start);
if (start < 0 || end < 0) {
  console.error('WATERMARK_DATA not found');
  process.exit(1);
}
const payload = JSON.parse(html.slice(start + marker.length, end + 1));
const data = payload.primary || payload;

const store = {};
const localStorage = {
  getItem: (key) => (key in store ? store[key] : null),
  setItem: (key, value) => { store[key] = String(value); },
};

const kpiElements = {};
const toggleButtons = [];

function countRowsFromHtml(innerHTML) {
  const stacked = (innerHTML.match(/<div class="kpi-row(?: |")/g) || []).length;
  if (stacked) return stacked;
  return innerHTML.includes('value-wrap') && !innerHTML.includes('kpi-rows') ? 1 : 0;
}

function makeToggleButton(view) {
  const btn = {
    dataset: { view },
    disabled: false,
    title: '',
    classList: {
      toggle(_cls, on) { if (_cls === 'active') this._active = on; },
    },
    onclick: null,
    click() { if (this.onclick) this.onclick(); },
  };
  toggleButtons.push(btn);
  return btn;
}

const document = {
  getElementById(id) {
    if (id === 'kpiViewToggle') {
      return {
        querySelectorAll(sel) {
          if (sel === 'button[data-view]') return toggleButtons;
          return [];
        },
      };
    }
    return kpiElements[id] || null;
  },
  querySelector(sel) {
    if (sel === '[data-kpi="carbon"]') return kpiElements.carbon;
    if (sel === '[data-kpi="water"]') return kpiElements.water;
    if (sel === '[data-view="lifecycle"]') return toggleButtons.find((b) => b.dataset.view === 'lifecycle');
    if (sel === '[data-view="operational"]') return toggleButtons.find((b) => b.dataset.view === 'operational');
    return null;
  },
  querySelectorAll(sel) {
    if (sel === '[data-kpi]') return Object.values(kpiElements);
    if (sel === '.num[data-target]') return [];
    if (sel === '.kpi-info-btn') return [];
    return [];
  },
};

['facility', 'carbon', 'water', 'cost', 'gpu'].forEach((key) => {
  kpiElements[key] = { dataset: { kpi: key }, innerHTML: '' };
});

function tagHtml() { return ''; }
function sparklineSvg() { return ''; }
function runCounters() {}

function extractKpiScript(source) {
  const start = source.indexOf('function kpiDerivationHtml(');
  const endMarker = 'function setupKpiToggle(data) {';
  const endStart = source.indexOf(endMarker, start);
  if (start < 0 || endStart < 0) return null;
  let depth = 0;
  let end = endStart;
  for (let i = endStart; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) {
        end = i + 1;
        break;
      }
    }
  }
  return source.slice(start, end);
}

const scriptBlock = extractKpiScript(html);
if (!scriptBlock) {
  console.error('KPI toggle script block not found');
  process.exit(1);
}

const loadKpi = new Function(
  'document',
  'localStorage',
  'tagHtml',
  'sparklineSvg',
  'runCounters',
  `${scriptBlock}
  return {
    buildImpactKpiCards,
    countImpactKpiRows,
    renderKpis,
    setupKpiToggle,
    KPI_VIEW_STORAGE_KEY,
    getKpiView: () => kpiView,
  };`,
);

const hooks = loadKpi(document, localStorage, tagHtml, sparklineSvg, runCounters);
if (!hooks) {
  console.error('__watermarkTestHooks missing');
  process.exit(1);
}

const hasEmbodied = data.kpi.has_embodied === true;
if (hooks.countImpactKpiRows(hooks.buildImpactKpiCards(data.kpi, data, 'operational').carbon) !== 1) {
  console.error('operational carbon should be 1 row');
  process.exit(1);
}
if (hooks.countImpactKpiRows(hooks.buildImpactKpiCards(data.kpi, data, 'operational').water) !== 1) {
  console.error('operational water should be 1 row');
  process.exit(1);
}

makeToggleButton('operational');
makeToggleButton('lifecycle');
hooks.setupKpiToggle(data);
hooks.renderKpis(data);

if (countRowsFromHtml(kpiElements.carbon.innerHTML) !== 1 || countRowsFromHtml(kpiElements.water.innerHTML) !== 1) {
  console.error('initial render should show 1 operational row per impact card');
  process.exit(1);
}

if (hasEmbodied) {
  if (hooks.countImpactKpiRows(hooks.buildImpactKpiCards(data.kpi, data, 'lifecycle').carbon) !== 3) {
    console.error('lifecycle carbon should be 3 rows');
    process.exit(1);
  }
  const lifecycleBtn = document.querySelector('[data-view="lifecycle"]');
  if (lifecycleBtn.disabled) {
    console.error('lifecycle toggle should be enabled when has_embodied=true');
    process.exit(1);
  }
  lifecycleBtn.click();
  if (countRowsFromHtml(kpiElements.carbon.innerHTML) !== 3 || countRowsFromHtml(kpiElements.water.innerHTML) !== 3) {
    console.error('clicking lifecycle toggle should render 3 stacked rows');
    process.exit(1);
  }
  if (localStorage.getItem(hooks.KPI_VIEW_STORAGE_KEY) !== 'lifecycle') {
    console.error('lifecycle choice should persist to localStorage');
    process.exit(1);
  }
  document.querySelector('[data-view="operational"]').click();
  if (countRowsFromHtml(kpiElements.carbon.innerHTML) !== 1 || countRowsFromHtml(kpiElements.water.innerHTML) !== 1) {
    console.error('clicking operational toggle should return to single-row view');
    process.exit(1);
  }
} else {
  const lifecycleBtn = document.querySelector('[data-view="lifecycle"]');
  if (!lifecycleBtn?.disabled) {
    console.error('lifecycle toggle should be disabled without embodied data');
    process.exit(1);
  }
  if (lifecycleBtn.title !== 'Pass --hardware-sku to enable lifecycle view.') {
    console.error('disabled lifecycle toggle missing tooltip');
    process.exit(1);
  }
}

console.log('kpi toggle ok');
