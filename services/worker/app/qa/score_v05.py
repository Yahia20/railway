"""Rubric v0.5 = v0.4 + scorecard item 15 (spelling/formatting) + the four critical errors the scorecard names.
Critical errors carry no weight: they are flagged for management review, whatever the score."""
import re
from collections import Counter
from .common import display_body
from .score import Thread, R
from . import score_v04
from .rules_v1 import _m

CATEGORIES = [c if c[0] != 15 else (15, c[1], c[2], {34: 0.03}) for c in score_v04.CATEGORIES]
WEIGHT = Counter()
for _, _, _, parts in CATEGORIES:
    for q, w in parts.items():
        WEIGHT[q] += w
CRITICAL = {15, 16, 26, 35, 36, 37, 38}

TEMPLATE_RE = re.compile(r'\{[^{}\n]{1,30}\}|\[[^\[\]\n]{1,30}\]|\bX{3,}\b|\bx{3,}\b|�')
SENSITIVE = ['cvv', 'رقم البطاقة', 'رقم الكارت', 'الرقم السري', 'كلمة السر', 'كلمة المرور', 'otp', 'كود التحقق',
             'رمز التحقق']
PERSONAL_PAY = ['حسابي الشخصي', 'حسابي الخاص', 'على حسابي', 'حولي على رقمي', 'ادفع لي كاش']


def _raw_agent(th):
    """Agent text exactly as typed (attachments excluded), for template / list checks."""
    return [(i, th.m[i]['body']) for i in th.agent_idx if th.m[i]['type'] in ('text', None, 'quick_reply')]


def _has(text, phrases):
    t = ' ' + _m(text) + ' '
    return next((p for p in phrases if _m(p) in t), None)


def _violation(th, a, key, extra=None):
    """'no' when the model's quote is a real agent line, or when the code's own check fires."""
    q = (a.get(key) or {}).get('violation_quote')
    if th.find(q, 'agent') is not None:
        return R('no', 'لقينا جملة', [q])
    if extra:
        found = extra()
        if found:
            return R('no', 'لقينا جملة', [found])
    return R('yes', 'مفيش')


def extra_items(t, a):
    th = Thread(t)
    raw = _raw_agent(th)
    out = {}
    out[34] = _violation(th, a, 'q34', lambda: next((b[:160] for _, b in raw if TEMPLATE_RE.search(b)), None))
    out[35] = _violation(th, a, 'q35')
    q36 = a.get('q36') or {}
    ia, ib = th.find(q36.get('quote_a'), 'agent'), th.find(q36.get('quote_b'), 'agent')
    out[36] = (R('no', 'أرقام أو بيانات حجز ما بتطلعش مع بعض', [q36.get('quote_a'), q36.get('quote_b')])
               if ia is not None and ib is not None else R('yes', 'مفيش'))
    out[37] = _violation(th, a, 'q37', lambda: next((b[:160] for _, b in raw if _has(b, SENSITIVE)), None))
    out[38] = _violation(th, a, 'q38', lambda: next((b[:160] for _, b in raw if _has(b, PERSONAL_PAY)), None))
    return out


def finish(items):
    earned = possible = 0.0
    for q, r in items.items():
        if r['ans'] in ('yes', 'no') and WEIGHT[q]:
            possible += WEIGHT[q]
            earned += WEIGHT[q] if r['ans'] == 'yes' else 0
    critical = sorted(q for q in CRITICAL if items.get(q, {}).get('ans') == 'no')
    return {'items': dict(sorted(items.items())), 'score': round(100 * earned / possible, 1) if possible else None,
            'critical': critical}


def score_thread(t, a):
    items = score_v04.score_thread(t, a)['items']
    items.update(extra_items(t, a))
    return finish(items)


def majority(results):
    out = {}
    for q in results[0]['items']:
        ans, n = Counter(r['items'][q]['ans'] for r in results).most_common(1)[0]
        if n < 2:
            ans = 'no'
        out[q] = next((r['items'][q] for r in results if r['items'][q]['ans'] == ans), R('no', 'التشغيلات اختلفت'))
    return finish(out)


def category_scores(items):
    out = {}
    for n, _, _, parts in CATEGORIES:
        poss = sum(w for q, w in parts.items() if items.get(q, {}).get('ans') in ('yes', 'no'))
        earn = sum(w for q, w in parts.items() if items.get(q, {}).get('ans') == 'yes')
        out[n] = round(5 * earn / poss, 2) if poss else None
    return out
