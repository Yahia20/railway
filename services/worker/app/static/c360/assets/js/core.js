/* Shared by every screen: escaping, formatting, the keyed API, the period,
 * score bands and small widgets. Nothing here knows a screen. */

export const KEY_STORE = 'c360_worker_key';      // shared with /report, /status, /conversations
const PERIOD_STORE = 'c360_period';
export const THEME_STORE = 'c360_theme';
export const ASSETS = '/dashboard/assets/';
export const LOGO = ASSETS + 'logo/travelgate-logo.png';
const BITRIX_DEAL = 'https://travelgate.bitrix24.ae/crm/deal/details/';
export const PAGE_SIZE = 100;

/* ------------------------------------------------------------ escaping */
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
export const raw = (s) => new Raw(String(s));
const flat = (v) => (v == null || v === false ? '' : v instanceof Raw ? v.s
  : Array.isArray(v) ? v.map(flat).join('') : esc(v));
/** Tagged template: every interpolation is escaped unless wrapped in raw(). */
export const html = (strings, ...vals) => raw(strings.reduce((a, s, i) => a + s + (i < vals.length ? flat(vals[i]) : ''), ''));
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

export const store = {
  get(k, area = sessionStorage) { try { return area.getItem(k); } catch { return null; } },
  set(k, v, area = sessionStorage) { try { v == null ? area.removeItem(k) : area.setItem(k, v); } catch { /* private mode */ } },
};

/* ------------------------------------------------------------ formatting */
export const NUM = new Intl.NumberFormat('en-US');
export const n = (v) => (v == null ? '—' : NUM.format(v));
const DAY = new Intl.DateTimeFormat('ar-EG-u-nu-latn', { timeZone: 'Asia/Riyadh', day: 'numeric', month: 'long' });
const DAY_SHORT = new Intl.DateTimeFormat('ar-EG-u-nu-latn', { timeZone: 'Asia/Riyadh', day: 'numeric', month: 'short' });
const TIME = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Riyadh', hour: '2-digit', minute: '2-digit' });
const YMD = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Riyadh' });
export const toDate = (v) => (v instanceof Date ? v : new Date(/^\d{4}-\d{2}-\d{2}$/.test(v) ? v + 'T12:00:00+03:00' : v));
export const day = (v) => (v ? DAY.format(toDate(v)) : '—');
export const dayShort = (v) => (v ? DAY_SHORT.format(toDate(v)) : '—');
export const time = (v) => (v ? TIME.format(toDate(v)) : '');
export const ymd = (d) => YMD.format(d);
export const pct = (v) => (v == null ? '—' : Math.round(v) + '%');
export function ago(v) {
  if (!v) return '—';
  const days = Math.floor((Date.now() - toDate(v).getTime()) / 86400000);
  if (days <= 0) return 'النهاردة';
  if (days === 1) return 'امبارح';
  return days < 30 ? `من ${days} يوم` : day(v);
}
export function duration(sec) {
  if (sec == null) return '—';
  if (sec < 3600) return Math.round(sec / 60) + ' د';
  if (sec < 86400) return (sec / 3600).toFixed(1) + ' س';
  return (sec / 86400).toFixed(1) + ' يوم';
}
export const money = (v) => (v ? NUM.format(Math.round(v)) : '—');

/* ------------------------------------------------------------ meta & bands
 * Item names, loss reasons and band thresholds come from the server
 * (app/qa/v4/texts.py) with every payload; the page keeps no copy. */
let META = { items: [], loss_reasons: {}, red_flags: {}, chat_types: {}, bands: null };
export const setMeta = (m) => { if (m) META = m; };
export const meta = () => META;
export const itemName = (k) => (META.items.find((x) => x.key === k) || {}).name || k;
export const lossName = (k) => META.loss_reasons[k] || k || '—';
export const flagName = (k) => META.red_flags[k] || k;

export function band(score) {
  const b = META.bands;
  if (score == null || !b) return '';
  return score < b.mid_min ? 'band-low' : score < b.good_min ? 'band-mid' : 'band-high';
}
export function bandWord(score) {
  return { 'band-low': 'ضعيف', 'band-mid': 'متوسط', 'band-high': 'كويس' }[band(score)] || '';
}
export const chip = (s) => (s == null
  ? html`<span class="chip chip--none">لسه</span>`
  : html`<span class="chip ${band(s)}" title="${bandWord(s)}">${Math.round(s)}%</span>`);

/* ------------------------------------------------------------ small widgets */
// U+2066/U+2069 isolate the masked number so it reads ••0014 inside Arabic text.
export const custName = (c) => c.display_name || c.customer_name || `عميل ⁦••${c.phone_tail || (c.phone || c.customer_phone || '').slice(-4)}⁩`;
export const initials = (s) => (s || '؟').trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join('');
export const stageTag = (sem) => (sem === 'S' ? html`<span class="tag tag--won">مكسوب</span>`
  : sem === 'F' ? html`<span class="tag tag--lost">خسران</span>`
    : sem === 'P' ? html`<span class="tag tag--open">مفتوح</span>` : '');
export const promiseTag = (kept) => (kept ? html`<span class="tag tag--ok">اتنفّذ</span>`
  : html`<span class="tag tag--bad">ما اتنفّذش</span>`);
export const flagTag = (f) => (f ? html`<span class="tag tag--bad">${flagName(f)}</span>` : '');
export const lossTag = (r) => (!r ? '' : r === 'Booked' ? html`<span class="tag tag--ok">${lossName(r)}</span>`
  : html`<span class="tag">${lossName(r)}</span>`);
export const dealLink = (id) => (id ? html`<a href="${BITRIX_DEAL + encodeURIComponent(id) + '/'}" target="_blank" rel="noopener noreferrer">ديل ${id}</a>` : '');

const ICON = {
  chat: '<path d="M4 5h16v11H8l-4 4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
  deal: '<path d="M4 7h16v12H4zM9 7V5h6v2" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
  won: '<path d="M5 12l4 4 10-10" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
  promise: '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 8v4l3 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  alert: '<path d="M12 4l9 16H3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M12 10v4M12 17v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  copy: '<rect x="8" y="8" width="11" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 15V5h10" fill="none" stroke="currentColor" stroke-width="2"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="2"/>',
  moon: '<path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
  out: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
};
export const icon = (k) => raw(`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICON[k]}</svg>`);

export const skeleton = () => html`<div class="page" aria-busy="true"><div class="skel" style="height:34px;width:40%"></div><div class="kpis">${[1, 2, 3, 4].map(() => html`<div class="skel" style="height:84px"></div>`)}</div><div class="skel" style="height:220px"></div></div>`;
export const empty = (msg) => html`<div class="state">${msg}</div>`;
export const errorBox = (err) => html`<div class="state state--err" role="alert"><b>ما قدرناش نجيب البيانات.</b><span>${err.status === 503 ? 'قاعدة البيانات مش متاحة دلوقتي. جرّب كمان دقيقة.' : err.status === 404 ? 'الحاجة دي مش موجودة، أو اتمسحت بعد 90 يوم.' : err.message}</span><button class="btn btn--sm" type="button" data-retry>جرّب تاني</button></div>`;
export const kpi = (label, value, sub = '', cls = '') => html`<div class="kpi ${cls}"><span class="kpi__label">${label}</span><span class="kpi__value">${value}</span>${sub ? html`<span class="kpi__sub">${sub}</span>` : ''}</div>`;

/* ------------------------------------------------------------ API */
export class ApiError extends Error { constructor(status, msg) { super(msg || `HTTP ${status}`); this.status = status; } }
export const key = () => store.get(KEY_STORE) || '';
let onUnauthorized = () => {};
export const whenUnauthorized = (fn) => { onUnauthorized = fn; };

export async function api(path) {
  let res;
  try {
    res = await fetch(path, { headers: { 'X-API-Key': key() }, cache: 'no-store' });
  } catch {
    throw new ApiError(0, 'مفيش اتصال بالسيرفر. اتأكد من النت وجرّب تاني.');
  }
  if (res.status === 401) { onUnauthorized('المفتاح غلط أو انتهى. ادخله تاني.'); throw new ApiError(401, 'المفتاح غلط'); }
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail; } catch { /* not json */ }
    throw new ApiError(res.status, detail);
  }
  return (res.headers.get('content-type') || '').includes('json') ? res.json() : res.text();
}

export const qs = (o) => { const s = new URLSearchParams(Object.entries(o).filter(([, v]) => v)).toString(); return s ? '?' + s : ''; };

/* ------------------------------------------------------------ state & period */
export const state = {
  period: readPeriod(), overview: null, overviewFor: null,
  agentChats: new Map(), customers: new Map(),
  custFilter: 'all', custQ: '', custShown: PAGE_SIZE, agentSort: 'score',
};
export function resetCache() {
  state.overview = null; state.overviewFor = null; state.agentChats.clear(); state.customers.clear();
}

function readPeriod() {
  try { return JSON.parse(store.get(PERIOD_STORE)) || { preset: 'all' }; } catch { return { preset: 'all' }; }
}
export function setPeriod(p) { state.period = p; store.set(PERIOD_STORE, JSON.stringify(p)); }
/** The selected period as [start, end) Riyadh days; {} = the server default (since grading began). */
export function periodParams() {
  const p = state.period;
  const today = new Date();
  const tomorrow = ymd(new Date(today.getTime() + 86400000));
  if (p.preset === '7') return { start: ymd(new Date(today.getTime() - 6 * 86400000)), end: tomorrow };
  if (p.preset === '30') return { start: ymd(new Date(today.getTime() - 29 * 86400000)), end: tomorrow };
  if (p.preset === 'custom' && p.start && p.end) return { start: p.start, end: p.end };
  return {};
}

export async function getOverview() {
  const sig = JSON.stringify(periodParams());
  if (state.overview && state.overviewFor === sig) return state.overview;
  const data = await api('/dashboard/overview' + qs(periodParams()));
  setMeta(data.meta);
  state.overview = data; state.overviewFor = sig; state.agentChats.clear();
  return data;
}
export async function getAgentChats(id) {
  if (!state.agentChats.has(id)) state.agentChats.set(id, await api(`/dashboard/agent/${encodeURIComponent(id)}/chats` + qs(periodParams())));
  return state.agentChats.get(id);
}
export async function getCustomer(id) {
  if (!state.customers.has(id)) {
    const rec = await api(`/dashboard/customer/${encodeURIComponent(id)}`);
    setMeta(rec.meta);
    state.customers.set(id, rec);
  }
  return state.customers.get(id);
}

/** Team mean of each item (0-100), weighted by the chats where it applied. */
export function teamItems(ov) {
  return meta().items.map((m) => {
    let sum = 0, w = 0;
    (ov.agents || []).forEach((a) => {
      const v = a.items && a.items[m.key], k = (a.items_n && a.items_n[m.key]) || 0;
      if (v != null && k) { sum += v * k; w += k; }
    });
    return { key: m.key, name: m.name, weight: m.weight, value: w ? (100 * sum) / w : null };
  });
}
