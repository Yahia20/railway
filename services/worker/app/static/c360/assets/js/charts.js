/* Charts: inline SVG and HTML bars, right-to-left, every value labelled or
 * reachable by tooltip (data-tip) and keyboard focus. */
import { NUM, esc, html, raw, dayShort, meta } from './core.js';

const niceMax = (v) => { if (v <= 5) return 5; const p = 10 ** Math.floor(Math.log10(v)); return Math.ceil(v / p) * p; };
const noData = () => html`<p class="hint">مفيش بيانات في الفترة دي.</p>`;

/** Vertical bars, one series. Labels only on the latest and the highest bar. */
export function barChart(rows, { valueKey, labelKey, tipFn, height = 170 }) {
  if (!rows.length) return noData();
  const W = 560, H = height, L = 8, R = 34, T = 18, B = 26;      // RTL: the value axis sits on the right
  const max = niceMax(Math.max(...rows.map((r) => r[valueKey])));
  const step = (W - L - R) / rows.length;
  const bw = Math.max(4, Math.min(28, step - 6));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const peak = rows.reduce((a, r, i) => (r[valueKey] > rows[a][valueKey] ? i : a), 0);
  const every = Math.ceil(rows.length / 8);
  let s = '';
  [0, max / 2, max].forEach((t) => { s += `<line class="${t ? 'grid' : 'base'}" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${W - R + 6}" y="${y(t) + 4}" text-anchor="start">${NUM.format(t)}</text>`; });
  rows.forEach((r, i) => {
    const v = r[valueKey];
    const cx = W - R - step * (i + 0.5);                          // RTL: oldest on the right
    const x = cx - bw / 2, top = y(v), bot = y(0);
    const rad = Math.min(4, bw / 2, bot - top);
    const d = v > 0 ? `M${x},${bot}V${top + rad}Q${x},${top} ${x + rad},${top}H${x + bw - rad}Q${x + bw},${top} ${x + bw},${top + rad}V${bot}Z` : '';
    s += `<rect class="hit" x="${cx - step / 2}" y="${T}" width="${step}" height="${H - T - B}" tabindex="0" data-tip="${esc(tipFn(r))}"/>`;
    if (d) s += `<path class="bar" d="${d}"/>`;
    if (i === peak || i === rows.length - 1) s += `<text class="lbl" x="${cx}" y="${top - 5}" text-anchor="middle">${NUM.format(v)}</text>`;
    if (i % every === 0 || i === rows.length - 1) s += `<text x="${cx}" y="${H - 8}" text-anchor="middle">${esc(dayShort(r[labelKey]))}</text>`;
  });
  return raw(`<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="رسم أعمدة">${s}</svg>`);
}

/** One line on a 0-100 scale, with the two band edges as dashed references. */
export function lineChart(rows, { valueKey, labelKey, tipFn, height = 170 }) {
  if (!rows.length) return noData();
  const W = 560, H = height, L = 16, R = 34, T = 16, B = 26;
  const y = (v) => T + (H - T - B) * (1 - v / 100);
  const step = rows.length > 1 ? (W - L - R - 10) / (rows.length - 1) : 0;
  const x = (i) => (rows.length > 1 ? W - R - 10 - step * i : (W - R + L) / 2);
  const every = Math.ceil(rows.length / 8);
  const b = meta().bands;
  let s = '';
  [0, ...(b ? [b.mid_min, b.good_min] : []), 100].forEach((t) => {
    const cls = t === 0 ? 'base' : t === 100 ? 'grid' : 'ref';
    s += `<line class="${cls}" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${W - R + 6}" y="${y(t) + 4}" text-anchor="start">${t}</text>`;
  });
  const pts = rows.map((r, i) => [x(i), y(r[valueKey] ?? 0)]);
  s += `<path class="line" d="M${pts.map((p) => p.join(',')).join('L')}"/>`;
  rows.forEach((r, i) => {
    const [px, py] = pts[i];
    const last = i === rows.length - 1;
    const w = Math.max(step, 16);
    s += `<rect class="hit" x="${px - w / 2}" y="${T}" width="${w}" height="${H - T - B}" tabindex="0" data-tip="${esc(tipFn(r))}"/>`;
    s += `<circle class="dot" cx="${px}" cy="${py}" r="${last ? 5 : 4}"/>`;
    if (last) s += `<text class="lbl" x="${px}" y="${py - 9}" text-anchor="middle">${Math.round(r[valueKey])}%</text>`;
    if (i % every === 0 || last) s += `<text x="${px}" y="${H - 8}" text-anchor="middle">${esc(dayShort(r[labelKey]))}</text>`;
  });
  return raw(`<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="رسم خطي لمتوسط الدرجة">${s}</svg>`);
}

/** HTML horizontal bars on a 0..max scale, with an optional team marker. */
export function hbars(items, { max, fmt, bandFn = () => '', href, mark }) {
  return html`<div class="hbars">${items.map((it) => {
    const width = it.value == null ? 0 : Math.max(0, Math.min(100, (it.value / max) * 100));
    const tag = href ? 'a' : 'div';
    const m = mark ? mark(it) : null;
    const markEl = m != null ? html`<i class="hbar__mark" style="inset-inline-start:calc(${(m / max) * 100}% - 1px)" aria-hidden="true"></i>` : '';
    return html`<${raw(tag)} class="hbar" ${href ? raw(`href="${esc(href(it))}"`) : ''} data-tip="${it.tip || ''}">
      <span class="hbar__label">${it.label}${it.sub ? html` <small>${it.sub}</small>` : ''}</span>
      <span class="hbar__track"><i class="hbar__fill ${bandFn(it.value)}" style="width:${width}%"></i>${markEl}</span>
      <span class="hbar__val">${it.value == null ? '—' : fmt(it.value)}</span>
    </${raw(tag)}>`;
  })}</div>`;
}
