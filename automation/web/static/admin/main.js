// static/admin/main.js — admin shell boot: tab switching, clock, mounts every tab, then restores the saved tab.
// (Workstream F: split verbatim from app.js; E1 replaces localStorage tab state with URL state.)
import {$, $$} from './shared.js';
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

// ───────── tabs ─────────

const panels = {
  launcher: $('[data-pane="launcher"]'),
  drafts:   $('[data-pane="drafts"]'),
  auctions: $('[data-pane="auctions"]'),
  inventory: $('[data-pane="inventory"]'),
  inquiries: $('[data-pane="inquiries"]'),
  'listings-db': $('[data-pane="listings-db"]'),
  'test-scrape': $('[data-pane="test-scrape"]'),
  subscribers: $('[data-pane="subscribers"]'),
  deals: $('[data-pane="deals"]'),
  tracking: $('[data-pane="tracking"]'),
};

const TAB_STORAGE_KEY = 'admin.lastTab';

function activateTab(name, {persist = true} = {}) {
  const btn = $$('.tab').find(t => t.dataset.tab === name);
  if (!btn) return false;
  $$('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  Object.entries(panels).forEach(([k, el]) => {
    if (el) el.hidden = (k !== name);
  });
  TABS[name]?.load?.();
  if (persist) {
    try { localStorage.setItem(TAB_STORAGE_KEY, name); } catch (_) {}
  }
  return true;
}

$$('.tab').forEach(btn => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});
// Restore the last tab on load. Falls back to whichever tab the markup
// rendered as `.active` (typically 01 Launcher) if storage is empty or the
// saved tab no longer exists in the DOM.
function restoreLastTab() {
  let saved = null;
  try { saved = localStorage.getItem(TAB_STORAGE_KEY); } catch (_) {}
  if (saved && saved !== 'launcher') {
    activateTab(saved, {persist: false});
  }
}

// ───────── clock ─────────

setInterval(() => {
  const d = new Date();
  $('#clock').textContent = d.toTimeString().slice(0, 8);
}, 1000);

// Bind every tab's listeners first, then boot the launcher + scrape streams (app.js "boot"), then restore the saved tab.
Object.values(TABS).forEach(t => t.mount());

fetch('/api/runs/state').then(r => r.json()).then(applyState).catch(() => {});
connectStream();
fetch('/api/scrape/state').then(r => r.json()).then(setScrapeStrip).catch(() => {});
connectScrapeStream();

restoreLastTab();
