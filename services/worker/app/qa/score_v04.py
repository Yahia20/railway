"""Rubric v0.4 = v0.3 + the QA scorecard (xlsx): its weights, a separate critical-error flag,
and "reads the whole conversation" (does not re-ask what the customer already said). No new model calls."""
from collections import Counter
import re
from .common import display_body, norm
from .score import Thread, R
from . import score_v03
from .rules_v1 import hit as hit_v1, LISTS as V1
from . import rules_v1

rules_v1.LISTS.update({
    'ask_date': ['متى', 'امتى', 'إمتى', 'تاريخ السفر', 'التاريخ', 'موعد السفر', 'تاريخ الذهاب', 'تاريخ الرحلة'],
    'ask_travellers': ['كم شخص', 'كم فرد', 'كم مسافر', 'عدد الأشخاص', 'عدد الافراد', 'عدد المسافرين', 'كم بالغ',
                       'عدد البالغين'],
    'ask_destination': ['وين تبي', 'وين حابب', 'فين', 'الوجهة', 'لأي دولة', 'أي دولة'],
    'ask_return': ['العودة', 'العوده', 'الرجوع', 'ترجع', 'ترجعي', 'هتخلص', 'هتخلصي', 'تخلص', 'تخلصي'],
})
rules_v1._KEYS = {k: [(rules_v1._m(p), p) for p in v if rules_v1._m(p)] for k, v in rules_v1.LISTS.items()}

# The scorecard's 15 chat checks and their weights, split over the questions that measure them.
CATEGORIES = [
    (1, 'Professional greeting + personalization, cst name', 0.08, {1: 0.05, 6: 0.03}),
    (2, 'Fast and appropriate response time', 0.07, {17: 0.04, 14: 0.02, 10: 0.01}),
    (3, 'Understands the request and reads the full conversation context', 0.08, {33: 0.05, 2: 0.03}),
    (4, 'Asks only relevant probing questions', 0.07, {3: 0.07}),
    (5, 'Clear, concise and professional writing', 0.08, {13: 0.05, 12: 0.03}),
    (6, 'Empathy + positive tone', 0.05, {9: 0.02, 4: 0.01, 16: 0.01, 15: 0.01}),
    (7, 'Correct and complete information', 0.10, {26: 0.05, 25: 0.05}),
    (8, 'Correct procedure / documentation / handover', 0.08, {18: 0.04, 20: 0.04}),
    (9, 'Relevant solution + alternative options when applicable', 0.08, {24: 0.08}),
    (10, 'Handles objections and concerns professionally', 0.06, {32: 0.06}),
    (11, 'Sets clear expectations, timelines and next steps', 0.07, {23: 0.04, 27: 0.03}),
    (12, 'Sales opportunity: appropriate upsell/cross-sell without pressure', 0.05, {29: 0.02, 30: 0.03}),
    (13, 'Strong written closing / call-to-action', 0.06, {5: 0.03, 31: 0.03}),
    (14, 'Confirms customer understanding and required action', 0.04, {2: 0.04}),
    (15, 'No spelling/grammar/formatting issues that affect professionalism', 0.03, {}),
]
WEIGHT = Counter()
for _, _, _, parts in CATEGORIES:
    for q, w in parts.items():
        WEIGHT[q] += w
CRITICAL = {15, 16, 26}   # a "no" here is flagged for management review, whatever the score


def has_day(quote):
    """A specific day: "25/9", "5/10/2026", "12 اكتوبر", "اكتوبر 12" — not a month alone, not "4 اشخاص"."""
    raw = (quote or '').translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    if re.search(r'\d+\s*[/\-.]\s*\d+', raw):  # norm() would erase the slash, so check the raw text
        return True
    q = norm(quote or '')
    months = '|'.join(m for m in score_v03.MONTHS_DAYS[:17])
    people = r'(?!\s*(?:اشخاص|شخص|افراد|فرد|بالغ|نفر|مسافر))'
    return bool(re.search(rf'\d+\s*(?:{months})', q) or re.search(rf'(?:{months})\s*\d+{people}', q))


def reask(t, a):
    """33: did the agent ask for something the customer had already given? (destination / date / travellers)"""
    a = score_v03.sanitize(t, a)
    th = Thread(t)
    b = a.get('booking') or {}
    pos = {k: th.find(b.get(k + '_quote'), 'customer') for k in ('destination', 'date', 'travellers')}
    if all(v is None for v in pos.values()):
        return R('na', 'العميل ما قالش تفاصيل الرحلة')
    asks = {'destination': 'ask_destination', 'date': 'ask_date', 'travellers': 'ask_travellers'}
    for i in th.agent_idx:
        body = display_body(th.m[i])
        if not ('؟' in body or '?' in body) or hit_v1('recap', body):
            continue  # only real questions; a confirmation ("... صحيح؟") is not a re-ask
        for k, lst in asks.items():
            if k == 'date' and (not has_day(b.get('date_quote')) or hit_v1('ask_return', body)):
                continue  # a month alone invites "which day?"; asking the RETURN date is a new question
            if pos[k] is not None and pos[k] < i and hit_v1(lst, body):
                names = {'destination': 'الوجهة', 'date': 'التاريخ', 'travellers': 'عدد الأفراد'}
                return R('no', f'سأل عن {names[k]} والعميل كان قالها', [b.get(k + '_quote'), body[:160]])
    return R('yes', 'ما سألش عن حاجة العميل قالها')


def finish(items):
    earned = possible = 0.0
    for q, r in items.items():
        if r['ans'] in ('yes', 'no') and WEIGHT[q]:
            possible += WEIGHT[q]
            earned += WEIGHT[q] if r['ans'] == 'yes' else 0
    critical = [q for q in CRITICAL if items.get(q, {}).get('ans') == 'no']
    return {'items': dict(sorted(items.items())), 'score': round(100 * earned / possible, 1) if possible else None,
            'critical': critical}


def score_thread(t, a):
    res = score_v03.score_thread(t, a)
    items = res['items']
    items[33] = reask(t, a)
    for q in (21, 22, 28):  # human review / not measurable: outside the score
        items[q] = R('na', items[q]['why'])
    return finish(items)


def majority(results):
    out = {}
    for q in results[0]['items']:
        votes = Counter(r['items'][q]['ans'] for r in results)
        ans, n = votes.most_common(1)[0]
        if n < 2:
            ans = 'no'
        out[q] = next((r['items'][q] for r in results if r['items'][q]['ans'] == ans), R('no', 'التشغيلات اختلفت'))
    return finish(out)


def category_scores(items):
    """Each scorecard check on its own 0-5 scale: share of its weight earned. None when nothing applied."""
    out = {}
    for n, _, _, parts in CATEGORIES:
        poss = sum(w for q, w in parts.items() if items.get(q, {}).get('ans') in ('yes', 'no'))
        earn = sum(w for q, w in parts.items() if items.get(q, {}).get('ans') == 'yes')
        out[n] = round(5 * earn / poss, 2) if poss else None
    return out
