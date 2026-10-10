// static/admin/fmt_time.js — readable auction close times, shared by the Auctions cards and the favorites strip.
// Pure (no DOM, no fetch): fmtClose(iso, nowMs) → {label, cls, title}. Times render in the VIEWER's local zone.
//   ended          → "Ended Oct 9"                      cls ends-over   (", 2025" when not this year)
//   < 1 h          → "30m left · today 3:00 PM"         cls ends-red
//   < 24 h         → "5h 0m left · tomorrow 5:00 PM"    cls ends-red under 2 h, else ends-yellow
//   otherwise      → "3d 4h left · Tue Oct 13"          cls ends-ok     (", 2027" when not this year)
// An empty / unparseable iso returns empty strings — the caller falls back to the scraper's own time_left text.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

const EMPTY = Object.freeze({label: '', cls: '', title: ''});

function clock(d) {
  const h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, '0');
  return `${h % 12 || 12}:${m} ${h < 12 ? 'AM' : 'PM'}`;
}

function monthDay(d, now) {
  const md = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return d.getFullYear() === now.getFullYear() ? md : `${md}, ${d.getFullYear()}`;
}

function dayWord(d, now) {
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(d) - start(now)) / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  return `${DAYS[d.getDay()]} ${monthDay(d, now)}`;
}

export function fmtClose(iso, nowMs = Date.now()) {
  if (!iso || typeof iso !== 'string') return EMPTY;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return EMPTY;
  const d = new Date(t);
  const now = new Date(nowMs);
  const title = d.toLocaleString(undefined, {
    weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
  });
  const secs = Math.floor((t - nowMs) / 1000);
  if (secs <= 0) return {label: `Ended ${monthDay(d, now)}`, cls: 'ends-over', title};
  if (secs < 3600) {
    return {label: `${Math.max(1, Math.floor(secs / 60))}m left · ${dayWord(d, now)} ${clock(d)}`, cls: 'ends-red', title};
  }
  if (secs < 86400) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return {label: `${h}h ${m}m left · ${dayWord(d, now)} ${clock(d)}`, cls: secs < 7200 ? 'ends-red' : 'ends-yellow', title};
  }
  const dd = Math.floor(secs / 86400);
  const hh = Math.floor((secs % 86400) / 3600);
  return {label: `${dd}d ${hh}h left · ${DAYS[d.getDay()]} ${monthDay(d, now)}`, cls: 'ends-ok', title};
}
