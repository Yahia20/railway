/* Customer screens: the list, one customer's story, one chat with its grade. */
import {
  $, $$, html, raw, icon, n, NUM, day, time, ago, money, chip, band, empty, kpi, errorBox, custName, initials,
  stageTag, promiseTag, flagTag, lossTag, dealLink, state, api, getOverview, getCustomer, PAGE_SIZE,
} from '../core.js';
import { periodControl, wirePeriod } from '../period.js';

const FILTERS = {
  all: [() => true, 'الكل'],
  qualified: [(c) => c.qualified, 'جاهزين'],
  won: [(c) => c.won > 0, 'ليهم ديل مكسوب'],
  low: [(c) => band(c.last_score) === 'band-low', 'آخر درجة ضعيفة'],
  ungraded: [(c) => c.last_score == null, 'لسه ما اتقيّموش'],
  repeat: [(c) => c.chats > 1, 'رجعوا أكتر من مرة'],
};

/* ------------------------------------------------------------ the list */
export async function screenCustomers(view, { rerender }) {
  const ov = await getOverview();
  const all = ov.customers || [];
  const q = state.custQ.trim();
  const qd = q.replace(/\D/g, '');
  const list = all.filter(FILTERS[state.custFilter][0]).filter((c) => !q
    || (qd && c.phone_tail && c.phone_tail.endsWith(qd.slice(-4)))
    || (c.display_name || '').includes(q)
    || (c.last_agent || '').toLowerCase().includes(q.toLowerCase()));
  const shown = list.slice(0, state.custShown);
  const withDeal = all.filter((c) => c.deals > 0).length;
  view.innerHTML = String(html`
    <div class="page">
      <div class="page__head"><div><h1>العملاء</h1><p>كل عميل كلّمنا في الفترة. اضغط على العميل تشوف حكايته كاملة.</p></div>${periodControl()}</div>
      <section class="kpis">
        ${kpi('عملاء', html`<span class="num">${n(all.length)}</span>${all.length >= 1000 ? html`<small>+</small>` : ''}`)}
        ${kpi('رجعوا أكتر من مرة', html`<span class="num">${n(all.filter((c) => c.chats > 1).length)}</span>`)}
        ${kpi('ليهم ديل', html`<span class="num">${n(withDeal)}</span>`)}
        ${kpi('من غير ديل', html`<span class="num">${n(all.length - withDeal)}</span>`, '', all.length - withDeal ? 'kpi--bad' : '')}
        ${kpi('ديلز مكسوبة', html`<span class="num">${n(all.reduce((t, c) => t + c.won, 0))}</span>`)}
      </section>
      <section class="panel">
        <div class="panel__head">
          <div class="filters" role="group" aria-label="فلتر">${Object.entries(FILTERS).map(([k, [, t]]) => html`<button type="button" data-f="${k}" aria-pressed="${state.custFilter === k}">${t}</button>`)}</div>
          <span><label class="sr" for="cq">فلتر القائمة</label><input type="search" id="cq" placeholder="فلتر بالاسم، آخر 4 أرقام، أو الموظف" value="${state.custQ}"></span>
        </div>
        <p class="hint"><span class="num">${n(list.length)}</span> عميل${all.length >= 1000 ? ' (أول 1000 بس في الفترة، استخدم البحث فوق للباقي)' : ''}. آخر درجة = درجة آخر شات اتقيّم، مش متوسط.</p>
        ${shown.length ? html`<div class="rows rows--cust4">
          <div class="row row--head"><span>العميل</span><span>شاتات</span><span>آخر تواصل</span><span>آخر موظف</span><span>آخر درجة</span><span>ديلز</span><span>كسب</span><span>قيمة</span></div>
          ${shown.map((c) => html`<a class="row" href="#/customer/${c.customer_id}">
            <span class="name">${custName(c)}${c.qualified ? html` <span class="tag tag--open">جاهز</span>` : ''}</span>
            <span class="n" data-l="شاتات">${n(c.chats)}</span>
            <span data-l="آخر تواصل">${ago(c.last_at)}</span>
            <span class="hint" data-l="آخر موظف">${c.last_agent || '—'}</span>
            <span data-l="آخر درجة">${chip(c.last_score)}</span>
            <span class="n" data-l="ديلز">${n(c.deals)}</span>
            <span class="n" data-l="كسب" style="${c.won ? 'color:var(--good);font-weight:700' : ''}">${n(c.won)}</span>
            <span class="n" data-l="قيمة">${money(c.won_amount)}</span>
          </a>`)}</div>` : empty('مفيش عملاء بالفلتر ده. جرّب فلتر تاني أو البحث في كل العملاء من فوق.')}
        ${list.length > shown.length ? html`<button class="btn more" id="more" type="button">اعرض ${n(Math.min(PAGE_SIZE, list.length - shown.length))} كمان</button>` : ''}
      </section>
    </div>`);
  wirePeriod(rerender);
  $$('[data-f]', view).forEach((b) => b.addEventListener('click', () => { state.custFilter = b.dataset.f; state.custShown = PAGE_SIZE; rerender(); }));
  const cq = $('#cq');
  let t;
  cq.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      state.custQ = cq.value; state.custShown = PAGE_SIZE;
      await rerender();
      const el = $('#cq'); el.focus(); el.setSelectionRange(el.value.length, el.value.length);
    }, 220);
  });
  const more = $('#more');
  if (more) more.addEventListener('click', () => { state.custShown += PAGE_SIZE; rerender(); });
}

/* ------------------------------------------------------------ one customer */
function timeline(rec) {
  const c = rec.customer;
  const events = [];
  (rec.chats || []).forEach((ch) => events.push({ at: ch.started_at, kind: ch.red_flag ? 'crit' : 'chat', ch }));
  (rec.deals || []).forEach((d) => {
    if (d.created_at_src) events.push({ at: d.created_at_src, kind: 'deal', d });
    if (d.stage_semantic === 'S' && d.close_date) events.push({ at: d.close_date, kind: 'won', d });
    if (d.stage_semantic === 'F' && d.close_date) events.push({ at: d.close_date, kind: 'lost', d });
  });
  (rec.promises || []).forEach((f) => events.push({ at: f.started_at, kind: 'promise', f }));
  events.sort((x, y) => new Date(y.at) - new Date(x.at));
  let lastDay = '';
  return events.map((e) => {
    const dd = day(e.at);
    const head = dd !== lastDay ? html`<div class="tl-day">${dd}</div>` : '';
    lastDay = dd;
    if (e.kind === 'chat' || e.kind === 'crit') {
      const ch = e.ch;
      return html`${head}<div class="ev ev--${e.kind}"><span class="ev__dot">${icon(e.kind === 'crit' ? 'alert' : 'chat')}</span>
        <a class="ev__body" href="#/chat/${ch.interaction_id}/${c.customer_id}">
          <span class="ev__top"><b>شات مع ${ch.agent_name || 'موظف غير معروف'}</b>${ch.qa_status === 'scored' ? chip(ch.score) : html`<span class="tag">ما اتقيّمش</span>`}</span>
          <span class="ev__meta"><span class="num">${time(ch.started_at)}</span><span>${n(ch.message_count)} رسالة</span>${ch.deal_id ? html`<span class="num">ديل ${ch.deal_id}</span>` : ''}${flagTag(ch.red_flag)}${lossTag(ch.loss_reason)}</span>
          ${ch.summary_ar ? html`<span class="ev__text">${ch.summary_ar}</span>` : ''}
        </a></div>`;
    }
    if (e.kind === 'promise') {
      const f = e.f;
      return html`${head}<div class="ev ev--promise"><span class="ev__dot">${icon('promise')}</span><div class="ev__body">
        <span class="ev__top"><b>وعد من ${f.agent_name || 'الموظف'}</b>${promiseTag(f.kept)}</span>
        <span class="ev__text">«${f.promise_text || ''}»</span>
        ${f.delivery_text ? html`<span class="ev__meta"><span>نفّذه: «${f.delivery_text}»</span></span>` : ''}</div></div>`;
    }
    const d = e.d;
    const label = e.kind === 'won' ? 'الديل اتكسب' : e.kind === 'lost' ? 'الديل اتقفل خسران' : 'ديل اتفتح';
    return html`${head}<div class="ev ev--${e.kind === 'won' ? 'won' : 'deal'}"><span class="ev__dot">${icon(e.kind === 'won' ? 'won' : 'deal')}</span><div class="ev__body">
      <span class="ev__top"><b>${label}</b>${dealLink(d.deal_id)}</span>
      <span class="ev__meta">${d.title ? html`<span>${d.title}</span>` : ''}${d.agent_name ? html`<span>${d.agent_name}</span>` : ''}${e.kind === 'won' && d.amount ? html`<span class="num">${NUM.format(d.amount)} ${d.currency || ''}</span>` : ''}</span></div></div>`;
  });
}

const NAME_SOURCE = { crm_contact: 'الاسم من Bitrix', deal_title: 'الاسم من عنوان الديل', ai_extracted: 'الاسم من الشات', manual: 'الاسم متسجّل يدوي' };

export async function screenCustomer(view, _ctx, id) {
  const rec = await getCustomer(id);
  const c = rec.customer;
  if (c.merged_into) { location.replace('#/customer/' + c.merged_into); return; }
  const s = rec.summary;
  const name = c.display_name || 'عميل من غير اسم';
  const phone = c.phone || '';
  const masked = phone ? phone.slice(0, 5) + ' ••• ' + phone.slice(-4) : '—';
  const events = timeline(rec);
  const ask = (rec.asks || [])[0];
  view.innerHTML = String(html`
    <div class="page">
      <p class="crumbs"><a href="#/customers">العملاء</a><span>›</span><span>${name}</span></p>
      <div class="who"><span class="avatar" aria-hidden="true">${c.display_name ? initials(c.display_name) : '؟'}</span>
        <div><h1>${name}</h1>
          <div class="who__meta">
            <span class="phone-box"><span class="phone" id="ph">${masked}</span>
              ${phone ? html`<button class="btn btn--ghost btn--sm" id="reveal" type="button" aria-pressed="false" title="اظهر الرقم">${icon('eye')}<span class="sr">اظهر الرقم</span></button>
              <button class="btn btn--ghost btn--sm" id="copy" type="button" title="انسخ الرقم">${icon('copy')}<span class="sr">انسخ الرقم</span></button>` : ''}</span>
            ${c.city ? html`<span>${c.city}</span>` : ''}
            <span>أول تواصل ${day(c.first_seen_at)}</span><span>آخر تواصل ${ago(c.last_seen_at)}</span>
            ${c.name_source ? html`<span class="tag" title="مصدر الاسم">${NAME_SOURCE[c.name_source] || c.name_source}</span>` : ''}
          </div></div></div>
      <section class="kpis">
        ${kpi('محادثات', html`<span class="num">${n(s.chats)}</span>`, `${n(s.graded)} منها اتقيّمت`)}
        ${kpi('آخر درجة', chip(s.last_score), s.agents.length ? s.agents.join('، ') : '—')}
        ${kpi('ديلز', html`<span class="num">${n(s.deals)}</span>`, `${n(s.won)} مكسوب${s.won_amount ? ` · ${money(s.won_amount)} ريال` : ''}`)}
        ${kpi('اتساب من غير عرض', html`<span class="num">${n(s.red_flags)}</span>`, '', s.red_flags ? 'kpi--bad' : '')}
        ${kpi('وعود ما اتنفّذتش', html`<span class="num">${n(s.broken_promises)}</span>`, '', s.broken_promises ? 'kpi--bad' : '')}
      </section>
      <div class="grid-side">
        <section class="panel"><div class="panel__head"><h2>الخط الزمني</h2><span>كل حاجة حصلت مع العميل، الأحدث فوق</span></div>
          ${events.length ? html`<div class="timeline">${events}</div>` : empty('مفيش محادثات أو ديلز مسجلة للعميل ده.')}</section>
        <aside class="facts">
          <section class="panel"><h3>الطلب</h3>
            ${ask ? html`<dl>${[['الوجهة', ask.destination], ['التاريخ', ask.date], ['عدد الأفراد', ask.travellers]].filter(([, v]) => v).map(([k, v]) => html`<div class="fact"><dt>${k}</dt><dd class="quote">«${v}»</dd></div>`)}</dl><p class="hint">كلام العميل نفسه، من شات ${day(ask.started_at)}</p>`
              : html`<p class="hint">ما قالش الوجهة والتاريخ والعدد في أي شات اتقيّم.</p>`}</section>
          <section class="panel"><h3>الديلز</h3>
            ${(rec.deals || []).length ? html`<div>${rec.deals.map((d) => html`<div class="deal">${dealLink(d.deal_id)}${stageTag(d.stage_semantic)}<small>${[d.title, d.agent_name, d.amount ? `${NUM.format(d.amount)} ${d.currency || ''}` : '', day(d.created_at_src)].filter(Boolean).join(' · ')}</small></div>`)}</div>`
              : html`<p class="hint">مفيش ديلز مربوطة بالعميل ده.</p>`}</section>
          <section class="panel"><h3>الوعود</h3>
            ${(rec.promises || []).length ? html`<div>${rec.promises.map((f) => html`<div class="deal"><span>${f.promise_text || 'وعد'}</span>${promiseTag(f.kept)}<small>${[f.agent_name, day(f.started_at)].filter(Boolean).join(' · ')}</small></div>`)}</div>`
              : html`<p class="hint">مفيش وعود في شاتاته المتقيّمة.</p>`}</section>
        </aside>
      </div>
    </div>`);
  const rv = $('#reveal');
  if (rv) rv.addEventListener('click', () => { const on = rv.getAttribute('aria-pressed') !== 'true'; rv.setAttribute('aria-pressed', on); $('#ph').textContent = on ? phone : masked; });
  const cp = $('#copy');
  if (cp) cp.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(phone); cp.title = 'اتنسخ'; cp.classList.add('btn--primary'); setTimeout(() => cp.classList.remove('btn--primary'), 900); }
    catch { const el = $('#ph'); el.textContent = phone; getSelection().selectAllChildren(el); }
  });
}

/* ------------------------------------------------------------ one chat */
const ROLE = { customer: 'العميل', agent: 'الموظف', bot: 'البوت', system: 'رسالة آلية' };
const MEDIA_WORD = { image: 'صورة', audio: 'رسالة صوتية', video: 'فيديو' };

function media(m) {
  return (m.media || []).map((x) => {
    if (x.access_url && x.kind === 'image') return html`<img src="${x.access_url}" alt="${x.name || 'صورة'}" loading="lazy" data-mid="${x.id || ''}">`;
    if (x.access_url && x.kind === 'audio') return html`<audio controls preload="none" src="${x.access_url}" data-mid="${x.id || ''}"></audio>`;
    if (x.access_url && x.kind === 'video') return html`<video controls preload="none" src="${x.access_url}" data-mid="${x.id || ''}"></video>`;
    if (x.access_url) return html`<a href="${x.access_url}" target="_blank" rel="noopener noreferrer">${x.name || 'ملف'}</a>`;
    return html`<span class="media-miss">${MEDIA_WORD[x.kind] || 'ملف'}${x.reason ? ': ' + x.reason : ''}</span>`;
  });
}

export async function screenChat(view, _ctx, iid, cid) {
  const [conv, frag] = await Promise.allSettled([
    api(`/conversations/${encodeURIComponent(iid)}`), api(`/dashboard/chat/${encodeURIComponent(iid)}`)]);
  if (conv.status === 'rejected' && frag.status === 'rejected') throw conv.reason;
  const th = conv.status === 'fulfilled' ? (conv.value.threads || [])[0] : null;
  view.innerHTML = String(html`
    <div class="page">
      <p class="crumbs">${cid ? html`<a href="#/customer/${cid}">ملف العميل</a><span>›</span>` : html`<a href="#/agents">الموظفين</a><span>›</span>`}<span>الشات</span></p>
      <div class="page__head"><div><h1>الشات${th ? html` · ${day(th.started_at)}` : ''}</h1>
        <p>${th ? html`<span class="num">${time(th.started_at)} – ${time(th.ended_at)}</span> · ${n((th.messages || []).length)} رسالة` : ''}${th && th.deal_id ? html` · ${dealLink(th.deal_id)}` : ''}</p></div></div>
      <div class="convo-grid">
        <section class="panel"><div class="panel__head"><h2>المحادثة</h2><span>زي ما وصلت من Bitrix</span></div>
          ${th ? (th.content_status !== 'available' ? empty('محتوى الشات اتمسح بعد 90 يوم.') : html`<div class="thread">${(th.messages || []).map((m, i) => html`
            <div class="msg ${m.sender && m.sender.role}"><span class="t">${i + 1} · ${ROLE[m.sender && m.sender.role] || ''} · <span class="num">${time(m.at)}</span></span>${m.text ? html`<span>${m.text}</span>` : ''}${media(m)}</div>`)}</div>`)
            : errorBox(conv.reason)}</section>
        <section class="panel"><div class="panel__head"><h2>التقييم</h2><a href="#/rules">القواعد</a></div>
          ${frag.status === 'fulfilled' ? html`<div class="qa">${raw(frag.value)}</div>`
            : frag.reason && frag.reason.status === 404 ? empty('الشات ده لسه ما اتقيّمش. التقييم بيشتغل بالليل على الشاتات اللي عدّى عليها 3 أيام وعدّت الفلتر.') : errorBox(frag.reason)}</section>
      </div>
    </div>`);
  // A presigned media link lives five minutes; ask for a fresh one when it dies.
  $$('[data-mid]', view).forEach((el) => {
    if (!el.dataset.mid) return;
    el.addEventListener('error', async () => {
      if (el.dataset.retried) return;
      el.dataset.retried = '1';
      try { el.src = (await api(`/conversations/media/${el.dataset.mid}/link`)).access_url; }
      catch { el.replaceWith(Object.assign(document.createElement('span'), { className: 'media-miss', textContent: 'الملف مش متاح' })); }
    });
  });
}
