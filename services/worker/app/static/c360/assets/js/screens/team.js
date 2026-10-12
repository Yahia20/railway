/* Team screens: the overview, the agents table, one agent. */
import {
  $$, html, icon, n, day, time, pct, ago, duration, money, chip, band, empty, kpi, custName,
  flagTag, lossTag, lossName, stageTag, itemName, meta, state, getOverview, getAgentChats, teamItems,
} from '../core.js';
import { barChart, lineChart, hbars } from '../charts.js';
import { periodControl, wirePeriod } from '../period.js';

const winRate = (a) => (a.deals ? (100 * a.won) / a.deals : null);
const countBars = (rows) => hbars(rows, { max: Math.max(1, ...rows.map((r) => r.value)), fmt: n });

function weakest(a) {
  const e = Object.entries(a.items || {}).sort((x, y) => x[1] - y[1])[0];
  return e && e[1] < 1 ? `${itemName(e[0])} (${Math.round(e[1] * 100)}%)` : '';
}

function chatRow(c, { showAgent = true } = {}) {
  return html`<a class="row" href="#/chat/${c.interaction_id}${c.customer_id ? '/' + c.customer_id : ''}">
    <span>${day(c.started_at)}<span class="sub">${time(c.started_at)}</span></span>${chip(c.score)}
    <span class="act__meta">${showAgent && c.agent_name ? html`<span>${c.agent_name}</span>` : ''}
      ${c.customer_id ? html`<span>${custName(c)}</span>` : ''}${flagTag(c.red_flag)}${lossTag(c.loss_reason)}</span>
    <span>${stageTag(c.stage_semantic)}</span></a>`;
}

/* ------------------------------------------------------------ overview */
export async function screenOverview(view, { rerender }) {
  const ov = await getOverview();
  const t = ov.totals || {};
  const agents = ov.agents || [];
  const acts = [
    ...(ov.red_flag_chats || []).map((c) => ({ kind: 'flag', at: c.started_at, c })),
    ...(ov.broken_promises || []).map((f) => ({ kind: 'promise', at: f.started_at, f })),
  ].sort((a, b) => new Date(b.at) - new Date(a.at));
  const items = teamItems(ov).filter((x) => x.value != null).sort((a, b) => a.value - b.value);
  const losses = (ov.loss_reasons || []).map((r) => ({ label: lossName(r.loss_reason), value: r.chats }));
  const types = (ov.chat_types || []).map((r) => ({ label: meta().chat_types[r.chat_type] || 'غير معروف', value: r.chats }));

  view.innerHTML = String(html`
    <div class="page">
      <div class="page__head">
        <div><h1>نظرة عامة</h1><p>آخر تقييم ${t.last_graded ? `${day(t.last_graded)} الساعة ${time(t.last_graded)}` : '—'}</p></div>
        ${periodControl()}
      </div>
      <p class="summary">اتقيّم <b class="num">${n(t.graded)}</b> شات لـ <b class="num">${n(t.agents)}</b> موظف، متوسط الدرجة <b class="num">${pct(t.avg_score)}</b>.
        ${t.red_flags ? html`<b class="num">${n(t.red_flags)}</b> عميل جاهز ما خدش عرض. ` : 'مفيش عميل جاهز اتساب من غير عرض. '}
        <b class="num">${n(t.qualified)}</b> عميل جاهز، حجز منهم <b class="num">${n(t.booked)}</b>. مستني تقييم: <b class="num">${n(t.waiting)}</b>.</p>
      <section class="kpis" aria-label="أرقام الفترة">
        ${kpi('شاتات اتقيّمت', html`<span class="num">${n(t.graded)}</span>`, `من ${n(t.handled)} شات اتعامل معاها الفريق`)}
        ${kpi('متوسط الدرجة', chip(t.avg_score), 'متوسط درجات الشاتات، مش شات بشات')}
        ${kpi('عميل جاهز ما خدش عرض', html`<span class="num">${n(t.red_flags)}</span>`, 'علامة حمرا', t.red_flags ? 'kpi--bad' : '')}
        ${kpi('عملاء جاهزين', html`<span class="num">${n(t.qualified)}</span>`, 'قالوا الوجهة والتاريخ والعدد')}
        ${kpi('حجزوا في الشات', html`<span class="num">${n(t.booked)}</span>`)}
        ${kpi('مستني تقييم', html`<span class="num">${n(t.waiting)}</span>`, 'بيتقيّم بالليل بعد 3 أيام هدوء')}
      </section>

      <section class="panel panel--act" aria-labelledby="h-act">
        <div class="panel__head"><h2 id="h-act"><span class="count num">${acts.length}</span>محتاج تصرف</h2><span>عملاء جاهزين ما خدوش عرض، ووعود الموظف ما نفّذهاش</span></div>
        ${acts.length ? html`<div class="rows rows--act">${acts.slice(0, 12).map((a) => (a.kind === 'flag' ? html`
          <a class="row" href="#/chat/${a.c.interaction_id}${a.c.customer_id ? '/' + a.c.customer_id : ''}">
            <span class="ev__dot" style="background:var(--bad-tint);color:var(--bad)">${icon('alert')}</span>
            <span class="act__what"><b>${meta().red_flags[a.c.red_flag] || a.c.red_flag}</b>
              <span class="act__meta"><span>${custName(a.c)}</span><span>${a.c.agent_name || 'موظف غير معروف'}</span><span>${day(a.c.started_at)}</span>${a.c.deal_id ? html`<span class="num">ديل ${a.c.deal_id}</span>` : ''}</span></span>
            ${chip(a.c.score)}
          </a>` : html`
          <a class="row" href="#/chat/${a.f.interaction_id}${a.f.customer_id ? '/' + a.f.customer_id : ''}">
            <span class="ev__dot" style="background:var(--accent-tint);color:var(--accent-ink)">${icon('promise')}</span>
            <span class="act__what"><b>${a.f.promise_text || 'وعد للعميل'}</b>
              <span class="act__meta"><span>${custName(a.f)}</span><span>${a.f.agent_name || ''}</span><span>${day(a.f.started_at)}</span></span></span>
            <span class="tag tag--bad">ما اتنفّذش</span>
          </a>`))}</div>${acts.length > 12 ? html`<p class="hint">و${n(acts.length - 12)} غيرهم. <a href="#/promises">كل الوعود</a></p>` : ''}`
          : empty('مفيش حاجة مستنية تصرف في الفترة دي.')}
      </section>

      <div class="grid2">
        <section class="panel"><div class="panel__head"><h2>كام شات اتقيّم كل يوم</h2><span>حسب يوم بداية الشات</span></div>
          ${barChart(ov.daily || [], { valueKey: 'graded', labelKey: 'day', tipFn: (r) => `${day(r.day)}: ${n(r.graded)} شات` })}</section>
        <section class="panel"><div class="panel__head"><h2>متوسط الدرجة كل يوم</h2><span>الخطوط المتقطعة عند حدود الألوان</span></div>
          ${lineChart(ov.daily || [], { valueKey: 'avg_score', labelKey: 'day', tipFn: (r) => `${day(r.day)}: متوسط ${pct(r.avg_score)} على ${n(r.graded)} شات` })}</section>
      </div>

      <div class="grid2">
        <section class="panel"><div class="panel__head"><h2>ترتيب الفريق</h2><a href="#/agents">كل التفاصيل</a></div>
          ${agents.length ? hbars(agents.map((a) => ({ id: a.agent_id, label: a.full_name, sub: `${a.chats} شات`, value: a.score,
            tip: `${a.full_name}: ${pct(a.score)} على ${a.chats} شات، ${a.red_flags} علامة حمرا` })),
          { max: 100, fmt: pct, bandFn: band, href: (it) => '#/agent/' + it.id }) : empty('مفيش موظفين اتقيّموا في الفترة دي.')}</section>
        <section class="panel"><div class="panel__head"><h2>البنود الأضعف عند الفريق</h2><a href="#/rules">القواعد</a></div>
          <p class="hint">نسبة النجاح في كل بند، على الشاتات اللي البند اتطبّق فيها. الأضعف فوق.</p>
          ${items.length ? hbars(items.map((x) => ({ label: x.name, sub: `وزن ${x.weight}`, value: x.value })), { max: 100, fmt: pct, bandFn: band }) : empty('لسه مفيش تقييم.')}</section>
      </div>

      <div class="grid2">
        <section class="panel"><div class="panel__head"><h2>ليه ما حجزوش</h2><span>سبب الخسارة لكل شات اتقيّم</span></div>
          ${losses.length ? countBars(losses) : empty('لسه مفيش أسباب.')}</section>
        <section class="panel"><div class="panel__head"><h2>العملاء جايين ليه</h2><span>نوع الشات</span></div>
          ${types.length ? countBars(types) : empty('لسه مفيش تقييم.')}</section>
      </div>

      <section class="panel"><div class="panel__head"><h2>عملاء جاهزين ما حجزوش</h2><span>قالوا الوجهة والتاريخ والعدد ومحجزوش. الأحدث فوق.</span></div>
        ${(ov.qualified_lost || []).length ? html`<div class="rows rows--chats4">${ov.qualified_lost.slice(0, 30).map((c) => chatRow(c))}</div>
          ${ov.qualified_lost.length > 30 ? html`<p class="hint">أول 30 من ${n(ov.qualified_lost.length)}.</p>` : ''}`
          : empty('مفيش في الفترة دي.')}
      </section>
    </div>`);
  wirePeriod(rerender);
}

/* ------------------------------------------------------------ agents */
const SORTS = {
  score: [(a, b) => (b.score ?? -1) - (a.score ?? -1), 'بالدرجة'],
  chats: [(a, b) => b.chats - a.chats, 'بعدد الشاتات'],
  flags: [(a, b) => b.red_flags - a.red_flags, 'بالعلامات الحمرا'],
  won: [(a, b) => b.won - a.won, 'بالمكسوب'],
  rate: [(a, b) => (winRate(b) ?? -1) - (winRate(a) ?? -1), 'بنسبة الكسب'],
};

export async function screenAgents(view, { rerender }) {
  const ov = await getOverview();
  const agents = (ov.agents || []).slice().sort(SORTS[state.agentSort][0]);
  const items = meta().items;
  const winners = agents.filter((a) => a.deals >= 5).sort((a, b) => winRate(b) - winRate(a)).slice(0, 12);
  view.innerHTML = String(html`
    <div class="page">
      <div class="page__head"><div><h1>الموظفين</h1><p>اضغط على أي موظف تشوف بنوده وشاتاته وعملاءه.</p></div>${periodControl()}</div>
      <section class="panel">
        <div class="panel__head">
          <div class="filters" role="group" aria-label="ترتيب">
            ${Object.entries(SORTS).map(([k, [, t]]) => html`<button type="button" data-sort="${k}" aria-pressed="${state.agentSort === k}">${t}</button>`)}
          </div>
          <span>«اتقيّم» = الشاتات اللي عدّت الفلتر من كل اللي اتعامل معاها</span>
        </div>
        ${agents.length ? html`<div class="rows rows--agents4">
          <div class="row row--head"><span>الموظف</span><span>الدرجة</span><span>اتقيّم</span><span>حمرا</span><span>جاهزين</span><span>ديلز</span><span>كسب</span><span>قيمة المكسوب</span><span>أول رد</span><span>أضعف بند</span></div>
          ${agents.map((a) => html`<a class="row" href="#/agent/${a.agent_id}">
            <span class="name">${a.full_name}</span>
            <span data-l="الدرجة">${chip(a.score)}</span>
            <span class="n" data-l="اتقيّم">${n(a.chats)} / ${n(a.handled)}</span>
            <span class="n" data-l="حمرا" style="${a.red_flags ? 'color:var(--bad);font-weight:700' : ''}">${n(a.red_flags)}</span>
            <span class="n" data-l="جاهزين">${n(a.qualified)}</span>
            <span class="n" data-l="ديلز">${n(a.deals)}</span>
            <span class="n" data-l="كسب">${n(a.won)}${a.deals ? html` <small>${winRate(a).toFixed(1)}%</small>` : ''}</span>
            <span class="n" data-l="قيمة المكسوب">${money(a.won_amount)}</span>
            <span class="n" data-l="أول رد">${duration(a.first_response_sec)}</span>
            <span class="hint wide" data-l="أضعف بند">${weakest(a)}</span>
          </a>`)}</div>` : empty('مفيش موظفين اتقيّموا في الفترة دي. غيّر الفترة من فوق.')}
        <p class="hint">الديلز والمكسوب عن كل التاريخ في Bitrix مش الفترة المختارة بس، لأن الديل ممكن يتفتح في شهر ويتكسب في شهر تاني. «أول رد» = الوسيط على شاتاته المتقيّمة.</p>
      </section>
      <div class="grid2">
        <section class="panel"><div class="panel__head"><h2>نسبة الكسب</h2><span>الموظفين اللي عندهم 5 ديلز أو أكتر</span></div>
          ${winners.length ? hbars(winners.map((a) => ({ id: a.agent_id, label: a.full_name, sub: `${a.won}/${a.deals}`, value: winRate(a) })),
          { max: Math.max(10, ...winners.map(winRate)), fmt: (v) => v.toFixed(1) + '%', href: (it) => '#/agent/' + it.id }) : empty('مفيش حد عنده 5 ديلز.')}</section>
        <section class="panel"><div class="panel__head"><h2>مصفوفة البنود</h2><span>نسبة النجاح لكل موظف. «—» = البند ما اتطبّقش.</span></div>
          <div class="matrix-wrap"><table class="matrix">
            <thead><tr><th class="nm">الموظف</th>${items.map((m, i) => html`<th data-tip="${m.name}">${i + 1}</th>`)}</tr></thead>
            <tbody>${agents.map((a) => html`<tr><td class="nm"><a href="#/agent/${a.agent_id}">${a.full_name}</a></td>${items.map((m) => {
              const v = a.items && a.items[m.key];
              return v == null ? html`<td class="c na">—</td>`
                : html`<td class="c ${band(100 * v)}" data-tip="${a.full_name} · ${m.name}: ${Math.round(100 * v)}% على ${(a.items_n || {})[m.key] || 0} شات">${Math.round(100 * v)}</td>`;
            })}</tr>`)}</tbody>
          </table></div>
          <p class="hint">${items.map((m, i) => `${i + 1}. ${m.name}`).join(' · ')}</p></section>
      </div>
    </div>`);
  wirePeriod(rerender);
  $$('[data-sort]', view).forEach((b) => b.addEventListener('click', () => { state.agentSort = b.dataset.sort; rerender(); }));
}

/* ------------------------------------------------------------ one agent */
export async function screenAgent(view, { rerender }, id) {
  const ov = await getOverview();
  const ranked = (ov.agents || []).slice().sort(SORTS.score[0]);
  const a = ranked.find((x) => x.agent_id === id);
  if (!a) {
    view.innerHTML = String(html`<div class="page"><p class="crumbs"><a href="#/agents">الموظفين</a></p>${empty('الموظف ده ما اتقيّمش في الفترة المختارة. جرّب «من بداية التقييم».')}${periodControl()}</div>`);
    wirePeriod(rerender);
    return;
  }
  const chats = (await getAgentChats(id)).slice().reverse();
  const team = new Map(teamItems(ov).map((t) => [t.key, t.value]));
  const custs = (ov.customers || []).filter((c) => c.last_agent === a.full_name).slice(0, 15);
  const trend = chats.slice().reverse().map((c) => ({ day: c.started_at, avg_score: c.score })).filter((r) => r.avg_score != null);
  const losses = Object.entries(a.losses || {}).sort((x, y) => y[1] - x[1]).map(([k, v]) => ({ label: lossName(k), value: v }));
  const rate = winRate(a);
  view.innerHTML = String(html`
    <div class="page">
      <p class="crumbs"><a href="#/agents">الموظفين</a><span>›</span><span>${a.full_name}</span></p>
      <div class="page__head">
        <div class="who"><span class="avatar" aria-hidden="true">${a.full_name.split(/\s+/).slice(0, 2).map((w) => w[0]).join('')}</span>
          <div><h1>${a.full_name}</h1><div class="who__meta"><span>الترتيب <b class="num">#${ranked.indexOf(a) + 1}</b> من ${ranked.length}</span>${a.team ? html`<span>${a.team}</span>` : ''}${chip(a.score)}</div></div></div>
        ${periodControl()}
      </div>
      <section class="kpis">
        ${kpi('شاتات اتقيّمت', html`<span class="num">${n(a.chats)}</span>`, `من ${n(a.handled)} اتعامل معاها، ${n(a.customers)} عميل`)}
        ${kpi('عميل جاهز ما خدش عرض', html`<span class="num">${n(a.red_flags)}</span>`, '', a.red_flags ? 'kpi--bad' : '')}
        ${kpi('عملاء جاهزين', html`<span class="num">${n(a.qualified)}</span>`, `حجز منهم ${n(a.booked)} في الشات`)}
        ${kpi('ديلز', html`<span class="num">${n(a.deals)}</span>`, `${n(a.open)} مفتوح · ${n(a.lost)} خسران`)}
        ${kpi('مكسوب', html`<span class="num">${n(a.won)}</span>${rate != null ? html`<small>${rate.toFixed(1)}%</small>` : ''}`, a.won_amount ? `${money(a.won_amount)} ريال` : 'نسبة الكسب من الديلز')}
        ${kpi('أول رد (الوسيط)', html`<span class="num">${duration(a.first_response_sec)}</span>`)}
      </section>
      <div class="grid2">
        <section class="panel"><div class="panel__head"><h2>البنود التسعة</h2><span>الخط الأسود = متوسط الفريق</span></div>
          ${hbars(meta().items.map((m) => {
            const v = a.items ? a.items[m.key] : null, tv = team.get(m.key);
            return { label: m.name, sub: `وزن ${m.weight}`, value: v == null ? null : 100 * v, team: tv,
              tip: v == null ? 'ما اتطبّقش في شاتاته' : `${Math.round(100 * v)}% على ${(a.items_n || {})[m.key]} شات · الفريق ${tv == null ? '—' : Math.round(tv) + '%'}` };
          }), { max: 100, fmt: pct, bandFn: band, mark: (it) => it.team })}</section>
        <section class="panel"><div class="panel__head"><h2>درجة كل شات</h2><span>من الأقدم للأحدث</span></div>
          ${lineChart(trend, { valueKey: 'avg_score', labelKey: 'day', tipFn: (r) => `${day(r.day)}: ${pct(r.avg_score)}` })}
          <h3>ليه عملاءه ما حجزوش</h3>
          ${losses.length ? countBars(losses) : empty('مفيش.')}
        </section>
      </div>
      <div class="grid2">
        <section class="panel"><div class="panel__head"><h2>شاتاته المتقيّمة</h2><span>الأحدث فوق. اضغط تشوف الشات والدليل.</span></div>
          ${chats.length ? html`<div class="rows rows--chats4">${chats.map((c) => chatRow(c, { showAgent: false }))}</div>` : empty('مفيش شاتات اتقيّمت له في الفترة دي.')}</section>
        <section class="panel"><div class="panel__head"><h2>عملاؤه في الفترة</h2><span>آخر موظف كلّمهم</span></div>
          ${custs.length ? html`<div class="rows">${custs.map((c) => html`<a class="row" style="grid-template-columns:minmax(0,1fr) auto" href="#/customer/${c.customer_id}"><span class="name">${custName(c)}<span class="sub">${ago(c.last_at)} · ${n(c.chats)} شات</span></span>${chip(c.last_score)}</a>`)}</div>` : empty('مفيش.')}</section>
      </div>
    </div>`);
  wirePeriod(rerender);
}
