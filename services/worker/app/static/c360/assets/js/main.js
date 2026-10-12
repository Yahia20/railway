/* Customer 360 console — boot, shell and router.
 *
 * Routes: #/  #/agents  #/agent/<id>  #/customers  #/customer/<id>
 *         #/chat/<interaction>[/<customer>]  #/promises  #/rules  #/ops
 *
 * The page holds no data: every number is fetched with X-API-Key from the
 * worker's keyed /dashboard/* routes (app/dashboard.py). */
import {
  $, $$, html, icon, n, custName, errorBox, skeleton, store, state, api, qs, key, resetCache,
  whenUnauthorized, getOverview, KEY_STORE, THEME_STORE, LOGO,
} from './core.js';
import { screenOverview, screenAgents, screenAgent } from './screens/team.js';
import { screenCustomers, screenCustomer, screenChat } from './screens/customers.js';
import { screenPromises, screenRules, screenOps } from './screens/ops.js';

const root = $('#root');

/* ------------------------------------------------------------ theme */
function applyTheme() {
  const t = store.get(THEME_STORE, localStorage);
  if (t === 'dark' || t === 'light') document.documentElement.setAttribute('data-theme', t);
  else document.documentElement.removeAttribute('data-theme');
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  store.set(THEME_STORE, cur === 'dark' ? 'light' : 'dark', localStorage);
  applyTheme();
}

/* ------------------------------------------------------------ key gate */
function logout(msg) {
  store.set(KEY_STORE, null);
  resetCache();
  renderGate(msg);
}
whenUnauthorized(logout);

function renderGate(msg = '') {
  root.innerHTML = String(html`
    <main class="gate">
      <form class="gate__card" id="gate" autocomplete="off">
        <img src="${LOGO}" width="120" height="84" alt="Travel Gate">
        <h1>Customer 360</h1>
        <p>الصفحة دي بتعرض شاتات وأرقام عملاء حقيقية. ادخل مفتاح الوصول (WORKER_API_KEY). بيتحفظ في التاب ده بس، ويتمسح لما تقفله.</p>
        <label class="sr" for="keyIn">مفتاح الوصول</label>
        <input type="password" id="keyIn" placeholder="مفتاح الوصول" spellcheck="false" required>
        <div class="err" id="gateErr" role="alert">${msg}</div>
        <button class="btn btn--primary" type="submit">ادخل</button>
      </form>
    </main>`);
  $('#keyIn').focus();
  $('#gate').addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = $('#keyIn').value.trim();
    if (!v) return;
    store.set(KEY_STORE, v);
    $('#gateErr').textContent = 'بيتأكد من المفتاح…';
    try { await getOverview(); mountShell(); route(); } catch (err) {
      if (err.status !== 401) $('#gateErr').textContent = err.message;
    }
  });
}

/* ------------------------------------------------------------ shell */
const NAV = [['#/', 'نظرة عامة', 'overview'], ['#/agents', 'الموظفين', 'agents'], ['#/customers', 'العملاء', 'customers'],
  ['#/promises', 'الوعود', 'promises'], ['#/rules', 'قواعد التقييم', 'rules'], ['#/ops', 'التشغيل', 'ops']];

function mountShell() {
  root.innerHTML = String(html`
    <header class="shell">
      <div class="shell__in">
        <a class="brand" href="#/"><img src="${LOGO}" width="74" height="52" alt="Travel Gate"><span><b>Customer 360</b><small>العملاء والموظفين في مكان واحد</small></span></a>
        <nav class="nav" aria-label="الأقسام">${NAV.map(([h, t, k]) => html`<a href="${h}" data-k="${k}">${t}</a>`)}</nav>
        <div class="tools">
          <div class="search" role="search">
            <label class="sr" for="q">ابحث عن عميل</label>
            <input type="search" id="q" placeholder="ابحث: آخر 4 أرقام، رقم ديل، أو اسم" autocomplete="off">
            <div class="results" id="qres" hidden></div>
          </div>
          <button class="btn btn--ghost btn--sm" id="theme" type="button" title="بدّل الوضع الداكن">${icon('moon')}<span class="sr">الوضع الداكن</span></button>
          <button class="btn btn--ghost btn--sm" id="out" type="button" title="اخرج وامسح المفتاح">${icon('out')}<span class="sr">خروج</span></button>
        </div>
      </div>
    </header>
    <div id="view"></div>`);
  $('#theme').addEventListener('click', toggleTheme);
  $('#out').addEventListener('click', () => logout('خرجت. ادخل المفتاح تاني لما تحب ترجع.'));
  wireSearch();
}

function wireSearch() {
  const input = $('#q'), box = $('#qres');
  let timer = null, seq = 0;
  const close = () => { box.hidden = true; };
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const my = ++seq;
      box.hidden = false; box.innerHTML = '<p>بيدوّر…</p>';
      try {
        const rows = await api('/dashboard/customers/search' + qs({ q }));
        if (my !== seq) return;
        box.innerHTML = rows.length ? String(html`${rows.map((r) => html`
          <a href="#/customer/${r.customer_id}"><span>${custName(r)}</span><span class="hint">${n(r.chats)} شات · ${n(r.deals)} ديل</span></a>`)}`)
          : '<p>مفيش عميل بالبحث ده.</p>';
      } catch (err) { if (my === seq) box.innerHTML = String(html`<p>${err.message}</p>`); }
    }, 280);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
    if (e.key === 'Enter') { const a = $('a', box); if (a) { location.hash = a.getAttribute('href'); close(); } }
  });
  box.addEventListener('click', (e) => { if (e.target.closest('a')) { close(); input.value = ''; } });
  document.addEventListener('click', (e) => { if (!e.target.closest('.search')) close(); });
}

/* ------------------------------------------------------------ tooltips
 * Any element with data-tip; keyboard focus shows it too. */
const tip = document.createElement('div');
tip.className = 'tip'; tip.hidden = true; tip.setAttribute('role', 'tooltip');
document.body.appendChild(tip);
function showTip(el, x, y) {
  tip.textContent = el.getAttribute('data-tip'); tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(Math.max(8, x - r.width / 2), innerWidth - r.width - 8) + 'px';
  tip.style.top = Math.max(8, y - r.height - 12) + 'px';
}
const tipTarget = (e) => { const el = e.target.closest && e.target.closest('[data-tip]'); return el && el.getAttribute('data-tip') ? el : null; };
document.addEventListener('pointermove', (e) => { const el = tipTarget(e); if (el) showTip(el, e.clientX, e.clientY); else tip.hidden = true; });
document.addEventListener('focusin', (e) => { const el = tipTarget(e); if (el) { const r = el.getBoundingClientRect(); showTip(el, r.left + r.width / 2, r.top); } });
document.addEventListener('focusout', () => { tip.hidden = true; });

/* ------------------------------------------------------------ router */
const ROUTES = [
  [/^#?\/?$/, 'overview', screenOverview],
  [/^#\/agents$/, 'agents', screenAgents],
  [/^#\/agent\/([\w-]+)$/, 'agents', screenAgent],
  [/^#\/customers$/, 'customers', screenCustomers],
  [/^#\/customer\/([\w-]+)$/, 'customers', screenCustomer],
  [/^#\/chat\/([\w-]+)(?:\/([\w-]+))?$/, 'customers', screenChat],
  [/^#\/promises$/, 'promises', screenPromises],
  [/^#\/rules$/, 'rules', screenRules],
  [/^#\/ops$/, 'ops', screenOps],
];
let routing = 0;

async function route() {
  const view = $('#view');
  if (!view) return;
  const h = location.hash || '#/';
  const hit = ROUTES.find(([re]) => re.test(h)) || ROUTES[0];
  const args = (h.match(hit[0]) || []).slice(1).filter(Boolean);
  $$('.nav a').forEach((a) => (a.dataset.k === hit[1] ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  const my = ++routing;
  const keepScroll = view.dataset.route === h;     // a re-render of the same screen keeps its place
  view.dataset.route = h;
  if (!keepScroll) view.innerHTML = String(skeleton());
  try {
    await hit[2](view, { rerender: route }, ...args);
    if (my !== routing) return;
    if (!keepScroll) window.scrollTo(0, 0);
    const t = $('h1', view);
    document.title = (t ? t.textContent.trim() + ' · ' : '') + 'Customer 360';
  } catch (err) {
    if (my !== routing || err.status === 401) return;
    view.innerHTML = String(html`<div class="page">${errorBox(err)}</div>`);
    const b = $('[data-retry]', view);
    if (b) b.addEventListener('click', () => { resetCache(); view.dataset.route = ''; route(); });
  }
}
window.addEventListener('hashchange', route);

/* ------------------------------------------------------------ boot */
applyTheme();
if (key()) { mountShell(); route(); } else { renderGate(); }
