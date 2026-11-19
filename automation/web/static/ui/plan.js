// static/ui/plan.js — how many lots a visitor may star. Free = 3, Standard ($5/month) = MAX.
// The browser remembers the plan under PLAN_KEY once a signed-in page reads it from /platform/api/saved.
// This is a convenience only: the server caps saved lots per plan (platform_routes.save_lot), so editing
// localStorage gains nothing beyond this one browser.
export const FREE_STARS = 3;
export const MAX_STARS = 300;
export const PLAN_KEY = 'pf.plan.v1';
export const UPGRADE_URL = '/platform#pricing';
export const UPGRADE_TEXT = `The free plan keeps ${FREE_STARS} favorites. Standard ($5 a month) keeps all of them.`;

export function plan() {
  try { return window.localStorage.getItem(PLAN_KEY) === 'pro' ? 'pro' : 'free'; } catch { return 'free'; }
}
export function setPlan(p) {
  try { window.localStorage.setItem(PLAN_KEY, p === 'pro' ? 'pro' : 'free'); } catch { /* blocked storage */ }
}
export const starLimit = () => (plan() === 'pro' ? MAX_STARS : FREE_STARS);
