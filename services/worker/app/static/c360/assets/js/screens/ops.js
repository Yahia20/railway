/* Screens that are not about one period: the promise queue, the rules, operations. */
import { html, raw, n, day, time, empty, kpi, custName, promiseTag, api, qs, periodParams } from '../core.js';
import { periodControl, wirePeriod } from '../period.js';

/* ------------------------------------------------------------ promises
 * Every promise an agent made in a graded chat (the scorecard's promise item):
 * kept = the promised thing was sent; broken = it was not, though the agent had
 * a real chance (not waiting on the customer, not closed by the customer). */
export async function screenPromises(view, { rerender }) {
  const data = await api('/dashboard/promises' + qs(periodParams()));
  const t = data.totals || {};
  const rows = data.rows || [];
  view.innerHTML = String(html`
    <div class="page">
      <div class="page__head"><div><h1>الوعود</h1><p>كل وعد الموظف قاله للعميل في الشاتات المتقيّمة. اللي ما اتنفّذش فوق.</p></div>${periodControl()}</div>
      <section class="kpis">
        ${kpi('ما اتنفّذتش', html`<span class="num">${n(t.broken)}</span>`, 'كان يقدر ينفّذ وما نفّذش', t.broken ? 'kpi--bad' : '')}
        ${kpi('اتنفّذت', html`<span class="num">${n(t.kept)}</span>`)}
        ${kpi('كل الوعود', html`<span class="num">${n(t.promises)}</span>`)}
        ${kpi('نسبة التنفيذ', html`<span class="num">${t.promises ? Math.round((100 * t.kept) / t.promises) + '%' : '—'}</span>`)}
      </section>
      <section class="panel"><div class="panel__head"><h2>الوعود</h2><span>${n(rows.length)} وعد. اضغط تشوف الشات.</span></div>
        ${rows.length ? html`<div class="rows rows--promises">
          <div class="row row--head"><span>الحالة</span><span>العميل</span><span>الموظف</span><span>الوعد</span><span>التاريخ</span></div>
          ${rows.map((r) => html`<a class="row" href="#/chat/${r.interaction_id}${r.customer_id ? '/' + r.customer_id : ''}">
            <span>${promiseTag(r.kept)}</span>
            <span class="name">${custName(r)}</span>
            <span class="hint" data-l="الموظف">${r.agent_name || '—'}</span>
            <span class="wide" data-l="الوعد">«${r.promise_text || '—'}»${r.delivery_text ? html`<span class="sub">نفّذه: «${r.delivery_text}»</span>` : ''}</span>
            <span class="n" data-l="التاريخ">${day(r.started_at)}</span>
          </a>`)}</div>`
          : empty('مفيش وعود في الشاتات المتقيّمة في الفترة دي.')}
      </section>
    </div>`);
  wirePeriod(rerender);
}

/* ------------------------------------------------------------ rules */
export async function screenRules(view) {
  const frag = await api('/dashboard/rules');
  view.innerHTML = String(html`<div class="page">
    <div class="page__head"><div><h1>قواعد التقييم</h1><p>شيت الجودة بتاع الشاتات: 9 بنود بأوزانهم، وكل حكم ليه دليل من الشات نفسه. الموديل بينسخ الجمل بس، والكود هو اللي بيحكم ويحسب.</p></div></div>
    <div class="qa">${raw(frag)}</div></div>`);
}

/* ------------------------------------------------------------ operations */
const WORD = { ok: ['شغال', 'ok'], warn: ['محتاج نظرة', 'warn'], bad: ['واقف', 'bad'], fail: ['واقف', 'bad'], off: ['مقفول', 'act'], error: ['الفحص وقع', 'bad'] };
const tag = (s) => { const w = WORD[s] || [s, '']; return html`<span class="tag tag--${w[1]}">${w[0]}</span>`; };

export async function screenOps(view) {
  const st = await api('/status/data');
  view.innerHTML = String(html`<div class="page">
    <div class="page__head"><div><h1>التشغيل</h1><p>حالة كل جزء في الـ pipeline دلوقتي. آخر فحص ${day(st.generated_at)} ${time(st.generated_at)}.</p></div>
      <div class="filters"><a class="btn btn--sm" href="/status" target="_blank" rel="noopener">صفحة الحالة</a><a class="btn btn--sm" href="/report" target="_blank" rel="noopener">التقرير الكامل</a><a class="btn btn--sm" href="/spend" target="_blank" rel="noopener">الصرف</a><a class="btn btn--sm" href="/dashboard/v05" target="_blank" rel="noopener">تقييم التجربة (15 بند)</a></div></div>
    <p class="summary">الحالة العامة: ${tag(st.overall)}</p>
    <div class="grid2">${(st.checks || []).map((c) => html`<section class="check">
      <div class="check__top"><b>${c.title}</b>${tag(c.status)}</div>
      <p>${c.what}</p>${c.why ? html`<p class="why">${c.why}</p>` : ''}${c.fix ? (/^[ -~]+$/.test(c.fix) ? html`<p><code>${c.fix}</code></p>` : html`<p><b>الحل:</b> ${c.fix}</p>`) : ''}
      ${c.since ? html`<span class="hint">من ${c.since}</span>` : ''}</section>`)}</div></div>`);
}
