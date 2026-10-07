"""Rubric v0.3 = v0.2 + two code checks + survey removed + first reply 10 min + majority of 3 runs."""
import re
from collections import Counter
from .common import norm, display_body, business_seconds
from .score import Thread, R, fmt, weight
from . import score_v02

MONTHS_DAYS = ['يناير', 'فبراير', 'مارس', 'ابريل', 'مايو', 'يونيو', 'يوليو', 'اغسطس', 'سبتمبر', 'اكتوبر',
               'نوفمبر', 'ديسمبر', 'رمضان', 'شوال', 'ذو القعده', 'ذو الحجه', 'محرم', 'السبت', 'الاحد',
               'الاثنين', 'الثلاثاء', 'الاربعاء', 'الخميس', 'الجمعه', 'january', 'february', 'march', 'april',
               'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december',
               'اربعاء', 'خميس', 'جمعه', 'ثلاثاء']


def valid_date(quote):
    """A date must carry a number, a month name or a weekday. "خلال الشهر القادم" does not."""
    q = norm(quote or '')
    if not q:
        return False
    if any(w in q for w in MONTHS_DAYS):
        return True
    # a bare number is a date unless it is a duration ("10 ايام", "3 ليالي", "اسبوعين")
    return bool(re.search(r'\d', q)) and not re.search(r'يوم|ايام|ليال|ليله|اسبوع|اسابيع', q)


def sanitize(t, a):
    """Apply the v0.3 code checks to a model answer before scoring."""
    a = dict(a or {})
    th = Thread(t)
    # 2: the recap line must be a question
    q2 = (a.get('q2') or {}).get('answer_quote')
    i = th.find(q2, 'agent')
    if i is None or not ('؟' in display_body(t['messages'][i]) or '?' in display_body(t['messages'][i])):
        a['q2'] = {'answer_quote': None}
    # booking date must be a real date
    b = dict(a.get('booking') or {})
    if not valid_date(b.get('date_quote')):
        b['date_quote'] = None
    a['booking'] = b
    return a


def score_thread(t, a):
    res = score_v02.score_thread(t, sanitize(t, a))
    items = res['items']
    items.pop(19, None)  # survey removed
    # 17: first reply within 10 business minutes
    th = Thread(t)
    m, at = th.m, th.at
    humans = [i for i, x in enumerate(m) if x['role'] in ('customer', 'agent')]
    if not humans or m[humans[0]]['role'] != 'customer' or not th.agent_idx:
        items[17] = R('na', 'الموظف هو اللي بدأ')
    else:
        g = business_seconds(at[humans[0]], at[th.agent_idx[0]])
        items[17] = R('yes' if g <= 600 else 'no', f'أول رد بعد {fmt(g)} من وقت الشغل')
    return finish(items)


def finish(items):
    earned = possible = 0
    for q, r in items.items():
        if r['ans'] in ('yes', 'no'):
            possible += weight(q)
            earned += weight(q) if r['ans'] == 'yes' else 0
    return {'items': dict(sorted(items.items())), 'earned': earned, 'possible': possible,
            'score': round(100 * earned / possible, 1) if possible else None}


def majority(results):
    """Per question, the answer at least 2 of 3 runs gave (3 runs never all differ on yes/no/na... if they do, 'no')."""
    out = {}
    for q in results[0]['items']:
        votes = Counter(r['items'][q]['ans'] for r in results)
        ans, n = votes.most_common(1)[0]
        if n < 2:
            ans = 'no'
        out[q] = next(r['items'][q] for r in results if r['items'][q]['ans'] == ans) if any(
            r['items'][q]['ans'] == ans for r in results) else R('no', 'التشغيلات اختلفت')
    return finish(out)
