// static/admin/shell.js — admin shell boot (Workstream E1, replaces main.js).
// Owns: the rail tab nav, URL-param tab state (`?tab=`), the clock, mounting every tab, the launcher/scrape boot.
// Per-tab modules own their own URL keys through shared.js getParams()/setParams(); the shell owns only `tab`.
import {$, $$, apiFetch, getParams, setParams} from './shared.js';
import * as launcher from './launcher.js';
import * as drafts from './drafts.js';
import * as auctions from './auctions.js';
import * as inventory from './inventory.js';
import * as inquiries from './inquiries.js';
import * as subscribers from './subscribers.js';
import * as listingsDb from './listings_db.js';
import * as testScrape from './test_scrape.js';
import * as deals from './deals.js';
import * as tracking from './tracking.js';
import {applyState, connectStream} from './launcher.js';
import {setScrapeStrip, connectScrapeStream} from './auctions.js';

const TABS = {launcher, drafts, auctions, inventory, inquiries, subscribers, 'listings-db': listingsDb, 'test-scrape': testScrape, deals, tracking};
const DEFAULT_TAB = 'launcher';
const LEGACY_TAB_KEY = 'admin.lastTab';   // pre-E1 localStorage key — read once, moved into the URL, deleted

// URL params live in shared.js (getParams/setParams); re-exported here so shell stays the E1 entry point.
export {getParams, setParams} from './shared.js';

// ───────── tabs ─────────

const panels = Object.fromEntries(Object.keys(TABS).map(k => [k, $(`[data-pane="${k}"]`)]));

export function activateTab(name) {
  const link = $$('.rail-tab').find(t => t.dataset.tab === name);
  if (!link) return false;
  $$('.rail-tab').forEach(t => { t.classList.remove('is-active'); t.removeAttribute('aria-current'); });
  link.classList.add('is-active');
  link.setAttribute('aria-current', 'page');
  Object.entries(panels).forEach(([k, el]) => {
    if (el) el.hidden = (k !== name);
  });
  TABS[name]?.load?.();
  return true;
}

function tabFromUrl() {
  const t = getParams().tab;
  return t && TABS[t] ? t : null;
}

$$('.rail-tab').forEach(link => {
  link.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;   // let "open in new tab" through
    e.preventDefault();
    const name = link.dataset.tab;
    if (name === tabFromUrl()) return;
    setParams({tab: name}, {replace: false});
    activateTab(name);
  });
});

window.addEventListener('popstate', () => activateTab(tabFromUrl() || DEFAULT_TAB));

// Resolve the tab to show: `?tab=` wins; otherwise migrate the legacy localStorage key into the URL once.
function boot() {
  let name = tabFromUrl();
  if (!name) {
    let saved = null;
    try { saved = localStorage.getItem(LEGACY_TAB_KEY); localStorage.removeItem(LEGACY_TAB_KEY); } catch (_) {}
    name = saved && TABS[saved] ? saved : DEFAULT_TAB;
    setParams({tab: name}, {replace: true});
  }
  activateTab(name);
}

// ───────── clock ─────────

setInterval(() => {
  const d = new Date();
  const clock = $('#clock');
  if (clock) clock.textContent = d.toTimeString().slice(0, 8);
}, 1000);

// Bind every tab's listeners first, then boot the launcher + scrape streams, then activate the URL tab.
Object.values(TABS).forEach(t => t.mount());

apiFetch('/api/runs/state').then(applyState).catch(() => {});
connectStream();
apiFetch('/api/scrape/state').then(setScrapeStrip).catch(() => {});
connectScrapeStream();

boot();
