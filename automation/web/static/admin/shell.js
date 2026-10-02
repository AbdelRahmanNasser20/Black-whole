// static/admin/shell.js — admin shell boot (Workstream E1, replaces main.js).
// Owns: the rail tab nav, URL-param tab state (`?tab=`), the Sales group + its view switch, the keyboard shortcuts,
// the clock, mounting every tab, the launcher/scrape boot.
// Per-tab modules own their own URL keys through shared.js getParams()/setParams(); the shell owns only `tab`.
import {$, $$, apiFetch, getParams, setParams} from './shared.js';
import * as launcher from './launcher.js';
import * as drafts from './drafts.js';
import * as auctions from './auctions.js';
import * as inventory from './inventory.js';
import * as quotes from './quotes.js';
import * as inquiries from './inquiries.js';
import * as subscribers from './subscribers.js';
import * as listingsDb from './listings_db.js';
import * as testScrape from './test_scrape.js';
import * as deals from './deals.js';
import * as tracking from './tracking.js';
import * as deposits from './deposits.js';
import * as channels from './channels.js';
import * as archive from './archive.js';
import {applyState, connectStream} from './launcher.js';
import {setScrapeStrip, connectScrapeStream} from './auctions.js';

// Every `?tab=` value and its module. There are more TABS than rail entries: the four Sales views are separate
// panes/modules (each keeps its own URL keys and its old `?tab=` link) that share ONE rail tab.
const TABS = {launcher, drafts, auctions, inventory, quotes, inquiries, subscribers, 'listings-db': listingsDb, 'test-scrape': testScrape, deals, tracking, deposits, channels, archive};
const DEFAULT_TAB = 'launcher';
const LEGACY_TAB_KEY = 'admin.lastTab';   // pre-E1 localStorage key — read once, moved into the URL, deleted

// ───────── the Sales group ─────────
// Order = the order of the view switch in index.html (#sales-nav) and of the q/i/d/s keys (SALES_CODES below).
const SALES_VIEWS = ['quotes', 'inquiries', 'deposits', 'subscribers'];
let lastSalesView = SALES_VIEWS[0];      // where the rail's Sales tab (and `5`) lands — the view last looked at
const railNameFor = (name) => SALES_VIEWS.includes(name) ? 'sales' : name;
const tabForRail = (railName) => railName === 'sales' ? lastSalesView : railName;

// URL params live in shared.js (getParams/setParams); re-exported here so shell stays the E1 entry point.
export {getParams, setParams} from './shared.js';

// ───────── tabs ─────────

const panels = Object.fromEntries(Object.keys(TABS).map(k => [k, $(`[data-pane="${k}"]`)]));

export function activateTab(name) {
  if (!TABS[name]) return false;
  const railName = railNameFor(name);
  const link = $$('.rail-tab').find(t => t.dataset.tab === railName);
  if (!link) return false;
  $$('.rail-tab').forEach(t => { t.classList.remove('is-active'); t.removeAttribute('aria-current'); });
  link.classList.add('is-active');
  link.setAttribute('aria-current', 'page');
  Object.entries(panels).forEach(([k, el]) => {
    if (el) el.hidden = (k !== name);
  });
  const inSales = railName === 'sales';
  const nav = $('#sales-nav');
  if (nav) {
    nav.hidden = !inSales;
    $$('[data-sales-view]', nav).forEach(b => {
      const on = b.dataset.salesView === name;
      b.classList.toggle('is-active', on);
      if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
  }
  if (inSales) { lastSalesView = name; refreshSalesCounts(); }
  TABS[name]?.load?.();
  return true;
}

function tabFromUrl() {
  const t = getParams().tab;
  return t && TABS[t] ? t : null;
}

/** Navigate to a `?tab=` value: push history, then activate. No-op when already there. */
function goTo(name) {
  if (!TABS[name] || name === tabFromUrl()) return;
  setParams({tab: name}, {replace: false});
  activateTab(name);
}

const plainClick = (e) => !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0);   // let "open in new tab" through

$$('.rail-tab').forEach(link => {
  link.addEventListener('click', (e) => {
    if (!plainClick(e)) return;
    e.preventDefault();
    goTo(tabForRail(link.dataset.tab));
  });
});

$$('#sales-nav [data-sales-view]').forEach(link => {
  link.addEventListener('click', (e) => {
    if (!plainClick(e)) return;
    e.preventDefault();
    goTo(link.dataset.salesView);
  });
});

window.addEventListener('popstate', () => activateTab(tabFromUrl() || DEFAULT_TAB));

// ───────── keyboard ─────────
// [ ]      previous / next rail tab (wraps)
// 1–9, 0   jump to rail tab 1–10
// q i d s  inside Sales: Quotes / Inquiries / Deposits / Subscribers
// Matched on the PHYSICAL key (`e.code`), not the character: with an Arabic layout active the same keys type
// ج د ض ه ي س and Arabic-Indic digits, and a character match would simply never fire.
// Never while typing (input, textarea, select, contenteditable), never with Cmd/Ctrl/Alt held (browser and OS
// shortcuts stay theirs), and never while the deal drawer or a dialog is open (digits belong to what is on top).
const DIGIT_CODE = /^(?:Digit|Numpad)([0-9])$/;
const SALES_CODES = {KeyQ: 'quotes', KeyI: 'inquiries', KeyD: 'deposits', KeyS: 'subscribers'};
const overlayOpen = () => !!document.querySelector('#deal-drawer.is-open, dialog[open]');

function isTyping(el) {
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName || ''));
}

document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;
  if (isTyping(e.target) || overlayOpen()) return;
  const rail = $$('.rail-tab').map(t => t.dataset.tab);
  const current = railNameFor(tabFromUrl() || DEFAULT_TAB);
  const digit = DIGIT_CODE.exec(e.code || '');
  let target = null;
  if (e.code === 'BracketLeft' || e.code === 'BracketRight') {
    if (e.shiftKey) return;                       // { } are someone else's
    const at = Math.max(0, rail.indexOf(current));
    target = tabForRail(rail[(at + (e.code === 'BracketRight' ? 1 : rail.length - 1)) % rail.length]);
  } else if (digit && !e.shiftKey) {
    const railName = rail[(digit[1] === '0' ? 10 : Number(digit[1])) - 1];
    if (railName) target = tabForRail(railName);
  } else if (current === 'sales' && !e.shiftKey && SALES_CODES[e.code]) {
    target = SALES_CODES[e.code];
  }
  if (!target) return;
  e.preventDefault();
  goTo(target);
});

// ───────── Sales badge ─────────
// How much is waiting on the operator: requests nobody has touched + new inquiries. Read-only and memoised server-side
// (15 s), so asking on boot and on every Sales activation is cheap. A failure just leaves the badge off.

function setCount(el, n) {
  if (!el) return;
  el.hidden = !(n > 0);
  el.textContent = n > 0 ? String(n) : '';
}

function refreshSalesCounts() {
  return apiFetch('/api/sales/counts').then((c) => {
    const quotesNew = Number(c?.quotes_new) || 0;
    const inquiriesNew = Number(c?.inquiries_new) || 0;
    setCount($('#sales-badge'), quotesNew + inquiriesNew);
    setCount($('#sales-count-quotes'), quotesNew);
    setCount($('#sales-count-inquiries'), inquiriesNew);
  }).catch(() => {});
}

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
refreshSalesCounts();

boot();
