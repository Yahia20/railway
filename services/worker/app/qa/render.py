"""One graded chat as HTML — the approved page's own rendering, moved in.

chat_html / question_li / bubbles / mask / rules_html are build_v6.py's
functions with the file loading taken out and the Arabic strings kept; the
dashboard asks for one chat at a time instead of building one 8 MB page. The
English half of the trial page is not here yet.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from .common import RIYADH, display_body, split_quote
from .evidence import evidence, evidence_no_extra, evidence_yes
from .score_v05 import CATEGORIES, CRITICAL, WEIGHT, category_scores
from .texts import CRITICAL_ONLY, ITEM_AR, OUTSIDE, Q

e = html.escape

U = dict(chat='شات', of5='من 5', proof='الدليل', critical='غلطة خطيرة', crit_box='الغلطات الخطيرة',
         crit_none='مفيش ✓', crit_review='⚠ الغلطات الخطيرة: محتاجة مراجعة من الإدارة',
         na='مش منطبق على الشات ده:', convo='اعرض المحادثة كلها', replying='ردًا على:',
         roles={'customer': 'العميل', 'agent': 'الموظف', 'bot': 'البوت', 'system': 'رسالة آلية'},
         crit_title='الغلطات الخطيرة', crit_tag='بتتعلّم لوحدها',
         crit_desc='أي واحدة منهم بتتبعت للإدارة تراجعها، مهما كانت درجة الشات.', outside='مش داخل في الدرجة',
         score_line=['الدرجة = مجموع أوزان الأسئلة اللي الموظف عملها ÷ مجموع أوزان الأسئلة اللي اتسألت في الشات × 100.',
                     'درجة الموظف = متوسط درجات شاتاته.'],
         sep='، ')


def mask(text: str, words: list[str]) -> str:
    text = re.sub(r'\S+@\S+\.\S+', '[إيميل]', text)
    text = re.sub(r'\+?\d[\d\s\-]{7,}\d', '[رقم]', text)
    text = re.sub(r'https?://\S+|\S*token=\S+|\S+\.(?:app|com|net|sa)/\S*', '[لينك]', text)
    for w in words:
        text = re.sub(rf'(?<![\w]){re.escape(w)}(?![\w])', '[اسم العميل]', text)
    return text


def pct(w: float) -> str:
    return f'{round(w * 100)}%'


def band(s: float) -> str:
    return 'good' if s >= 70 else 'bad' if s < 50 else 'mid'


def bubbles(msgs, words, limit=260) -> str:
    out = []
    for role, when, text, quoted in msgs:
        re_ = f'<span class="re" dir="rtl">{U["replying"]} «{e(mask(quoted, words)[:140])}»</span>' if quoted else ''
        out.append(f'<div class="ev {role}"><span class="who">{U["roles"][role]} · {when}</span>{re_}'
                   f'<span dir="rtl">{e(mask(text, words)[:limit])}{"…" if len(text) > limit else ""}</span></div>')
    return ''.join(out)


def question_li(q: int, r: dict, t: dict, words: list[str], wq: float | None = None) -> str:
    ok = r['ans'] == 'yes'
    note, msgs = evidence_yes(q, t, r) if ok else (evidence_no_extra if q >= 34 else evidence)(q, t, r)
    wq = WEIGHT[q] if wq is None else wq
    w = f'<span class="w">{pct(wq)}</span>' if wq else ''
    crit = f'<span class="imp">{U["critical"]}</span>' if (q in CRITICAL and not ok) else ''
    proof = (f'<div class="proof"><span class="pl">{U["proof"]}{": " + e(mask(note, words)) if note else ""}</span>'
             f'{bubbles(msgs, words)}</div>') if (note or msgs) else ''
    return (f'<li class="{r["ans"]}"><span class="mark">{"✓" if ok else "✗"}</span><div>'
            f'<b>{e(Q[q][0])}</b>{w}{crit}{proof}</div></li>')


def chat_html(k: int, t: dict[str, Any], m: dict[str, Any]) -> str:
    """Chat k of an agent: critical box, the 15 items with their questions and
    evidence, what did not apply, and the whole conversation (names, numbers
    and links masked)."""
    words = [w for w in ((t.get('customer_name') or {}).get('name') or '').split() if len(w) >= 3]
    items = {int(q): r for q, r in m['items'].items()}
    cats = category_scores(items)
    blocks, na = [], []
    for n, _, w, parts in CATEGORIES:
        qs = [q for q in parts if items.get(q, {}).get('ans') in ('yes', 'no')]
        na += [q for q in parts if items.get(q, {}).get('ans') == 'na']
        if not qs:
            continue
        qs.sort(key=lambda q: (items[q]['ans'] == 'yes', -parts[q]))
        blocks.append(f'''<div class="item"><div class="ih"><span>{n}. {e(ITEM_AR[n])} <span class="w">{pct(w)}</span></span>
<span class="is {band(cats[n] * 20)}">{cats[n]:.1f} {U["of5"]}</span></div>
<ul class="qs">{''.join(question_li(q, items[q], t, words, parts[q]) for q in qs)}</ul></div>''')
    crit_lis = ''.join(question_li(q, items[q], t, words) for q in CRITICAL_ONLY
                       if items.get(q, {}).get('ans') == 'no')
    crit_block = (f'<div class="item crit-box ok"><div class="ih"><span>{U["crit_box"]}</span><span>{U["crit_none"]}</span></div></div>'
                  if all(items.get(q, {}).get('ans') != 'no' for q in CRITICAL) else
                  f'<div class="item crit-box"><div class="ih"><span>{U["crit_review"]}</span></div><ul class="qs">{crit_lis}</ul></div>')
    na_txt = U['sep'].join(e(Q[q][0]) for q in sorted(set(na)))
    date = datetime.fromisoformat(t['started_at']).astimezone(RIYADH).strftime('%Y-%m-%d')
    convo = ''.join(
        f'<div class="msg {x["role"]}"><span class="who">{U["roles"][x["role"]]} · '
        f'{datetime.fromisoformat(x["at"]).astimezone(RIYADH).strftime("%H:%M")}</span>'
        f'{(chr(60) + "span class=re>" + U["replying"] + " «" + e(mask(qt, words)[:160]) + "»</span>") if qt else ""}'
        f'{e(mask(own, words)[:1500])}</div>'
        for x in t['messages']
        for qt, own in [split_quote(display_body(x)) if x['role'] == 'customer' else (None, display_body(x))])
    return f'''<div class="chat">
<div class="chat-head"><span class="label">{U["chat"]} {k} · {date}</span><span class="sc {band(m["score"])}">{round(m["score"])}%</span></div>
{crit_block}{''.join(blocks)}
{f'<p class="na">{U["na"]} {na_txt}</p>' if na_txt else ''}
<details class="convo"><summary>{U["convo"]}</summary><div class="msgs" dir="rtl">{convo}</div></details>
</div>'''


def rules_html() -> str:
    h = []
    for n, en, w, parts in CATEGORIES:
        lis = ''.join(f'<li><b>{e(Q[q][0])}</b> <span class="w">{pct(parts[q])}</span>'
                      f'<span class="rule">{e(Q[q][1])}</span></li>' for q in parts)
        h.append(f'<div class="ritem"><div class="rh"><span>{n}. {e(ITEM_AR[n])}</span><span class="w big">{pct(w)}</span></div>'
                 f'<div class="en-name">{e(en)}</div><ul>{lis}</ul></div>')
    crit = [15, 16, 26] + CRITICAL_ONLY
    h.append(f'<div class="ritem crit-r"><div class="rh"><span>{U["crit_title"]}</span><span class="w big">{U["crit_tag"]}</span></div>'
             f'<div class="en-name">{U["crit_desc"]}</div><ul>'
             + ''.join(f'<li><b>{e(Q[q][0])}</b><span class="rule">{e(Q[q][1])}</span></li>' for q in crit)
             + '</ul></div>')
    h.append(f'<div class="ritem out"><div class="rh"><span>{U["outside"]}</span></div><ul>'
             + ''.join(f'<li><b>{e(Q[q][0])}</b><span class="rule">{e(Q[q][1])}</span></li>' for q in OUTSIDE)
             + '</ul></div>')
    h.append('<div class="ritem out">' + ''.join(f'<p class="en-name">{s}</p>' for s in U['score_line']) + '</div>')
    return ''.join(h)
