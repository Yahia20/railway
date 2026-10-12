/* The period picker every list screen shows: a preset or a custom [from, to). */
import { $, html, day, toDate, state, periodParams, setPeriod } from './core.js';

const PRESETS = [['all', 'من بداية التقييم'], ['7', 'آخر 7 أيام'], ['30', 'آخر 30 يوم'], ['custom', 'فترة مخصوصة']];

export function periodControl() {
  const p = state.period;
  const ps = periodParams();
  const ov = state.overview;
  const shown = ov ? `${day(ov.from)} – ${day(new Date(toDate(ov.to).getTime() - 86400000))}` : '';
  return html`
    <div class="period" id="period">
      <label for="preset">الفترة</label>
      <select id="preset">${PRESETS.map(([v, t]) => html`<option value="${v}" ${p.preset === v ? 'selected' : ''}>${t}</option>`)}</select>
      <span id="custom" ${p.preset === 'custom' ? '' : 'hidden'}>
        <label class="sr" for="from">من</label><input type="date" id="from" value="${ps.start || ''}">
        <label class="sr" for="to">لـ</label><input type="date" id="to" value="${ps.end || ''}">
        <button class="btn btn--sm" id="applyP" type="button">اعرض</button>
      </span>
      <span>${shown}</span>
    </div>`;
}

/** Wire the picker rendered by periodControl(); `rerender` runs after a change. */
export function wirePeriod(rerender) {
  const sel = $('#preset');
  if (!sel) return;
  const set = (p) => { setPeriod(p); rerender(); };
  sel.addEventListener('change', () => {
    if (sel.value === 'custom') { $('#custom').hidden = false; return; }
    set({ preset: sel.value });
  });
  const apply = $('#applyP');
  if (apply) apply.addEventListener('click', () => {
    const s = $('#from').value, e = $('#to').value;
    if (s && e && e > s) set({ preset: 'custom', start: s, end: e });
  });
}
