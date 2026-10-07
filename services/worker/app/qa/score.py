"""Rubric v0.1: turn quotes + timestamps into yes / no / n.a. and a score. No model here."""
import json, re
from datetime import datetime, timedelta
from .common import norm, display_body, business_seconds, own_text

RED = {15, 16, 17, 18, 19, 21, 22, 24, 25, 26, 27, 28}

TITLES = {
    1: 'التحية الصح', 2: 'كرّر طلب العميل', 3: 'سأل أسئلة يفهم بيها الطلب', 4: 'طمّن العميل',
    5: 'القفل الصح', 6: 'نادى العميل باسمه', 7: 'الثقة وسرعة الكلام', 8: 'ما قاطعش العميل',
    9: 'اتعاطف مع العميل', 10: 'رجع بعد "لحظة"', 11: 'الـ mute', 12: 'شرح تاني لما العميل ما فهمش',
    13: 'كلامه مهني ومحترم', 14: 'ما سابش العميل مستني', 15: 'ما اتكلمش وحش عن طيران/فندق/الشركة',
    16: 'مؤدب مع العميل', 17: 'أول رد خلال 5 دقايق', 18: 'حوّل المحادثة صح', 19: 'ذكر الاستبيان',
    20: 'قال على المستندات', 21: 'عمل الإجراء الصح', 22: 'سجّل على Bitrix', 23: 'وضّح اللي هيحصل بعد كده',
    24: 'عرض حل بديل', 25: 'جاوب على كل الأسئلة', 26: 'إدّى معلومة صح', 27: 'تابع مع العميل',
    28: 'مشي على الإجراءات السليمة', 29: 'قال السعر', 30: 'عرض أكتر من اختيار',
    31: 'طلب تأكيد الحجز', 32: 'ردّ على اعتراض العميل',
}
KIND = {**{k: 'ai' for k in (1, 2, 3, 4, 5, 9, 12, 13, 15, 16, 18, 20, 23, 24, 25, 26, 29, 30, 31, 32)},
        **{k: 'computer' for k in (6, 10, 14, 17, 19, 22, 27)},
        21: 'human', 28: 'human', 7: 'calls', 8: 'calls', 11: 'calls'}

WAIT_RE = re.compile(r'لحظ|ثواني|ثانيه|دقيقه|دقايق|دقائق|خليك معي|خليك معاي|انتظرني|انتظر|اتاكد|اشيك|بشيك|اتحقق')
GREETING_Q_RE = re.compile(r'^(كيف|شلون|وش)? ?(الحال|حالك|حالكم|كيفك|اخبارك|شلونك|علومك)')
SURVEY_RE = re.compile(r'قيم|تقييم|استبيان|رايك في الخدمه')


def weight(q):
    return 2 if q in RED else 1


class Thread:
    def __init__(self, t):
        self.t = t
        self.m = t['messages']
        self.n = [norm(own_text(x)) for x in self.m]
        self.at = [datetime.fromisoformat(x['at']) for x in self.m]
        self.agent_idx = [i for i, x in enumerate(self.m) if x['role'] == 'agent']

    def find(self, quote, role, after=-1, only=None):
        """Index of the first message of `role` after `after` that contains the quote, else None."""
        if not isinstance(quote, str) or not quote.strip():
            return None
        q = norm(quote)
        raw = quote.strip()  # punctuation-only quotes ("؟؟") vanish under norm()
        for i, x in enumerate(self.m):
            if i <= after or x['role'] != role or (only is not None and i not in only):
                continue
            if (q and q in self.n[i]) or (not q and raw in display_body(x)):
                return i
        return None


def R(ans, why, quotes=()):
    return {'ans': ans, 'why': why, 'quotes': [q for q in quotes if q]}


def pair(th, item, trig_role='customer', ans_role='agent'):
    """Trigger quote must exist (else n.a.); answer quote must exist AFTER it (else no)."""
    tq, aq = item.get('trigger_quote'), item.get('answer_quote')
    ti = th.find(tq, trig_role)
    if ti is None:
        return R('na', 'الشرط مش موجود')
    ai = th.find(aq, ans_role, after=ti)
    if ai is None:
        return R('no', 'مفيش جملة من الموظف بعدها', [tq])
    return R('yes', 'لقينا الجملة', [tq, aq])


def single(th, item, only=None):
    aq = item.get('answer_quote')
    i = th.find(aq, 'agent', only=only)
    return R('yes', 'لقينا الجملة', [aq]) if i is not None else R('no', 'مفيش جملة', [])


def violation(th, item):
    vq = item.get('violation_quote')
    i = th.find(vq, 'agent')
    return R('no', 'لقينا جملة غلط', [vq]) if i is not None else R('yes', 'مفيش غلط', [])


def judge_ai(th, a):
    out = {}
    first2 = set(th.agent_idx[:2])
    last3 = set(th.agent_idx[-3:])
    q1 = a.get('q1') or {}
    g = th.find(q1.get('greeting_quote'), 'agent', only=first2)
    ident = th.find(q1.get('identity_quote'), 'agent', only=first2)
    out[1] = (R('yes', 'ترحيب وتعريف', [q1.get('greeting_quote'), q1.get('identity_quote')])
              if g is not None and ident is not None else
              R('no', 'ناقص الترحيب' if g is None else 'ناقص التعريف بالاسم أو الشركة',
                [q1.get('greeting_quote') if g is not None else None]))
    out[2] = pair(th, a.get('q2') or {})
    q3 = a.get('q3') or {}
    out[3] = pair(th, q3) if q3.get('missing') else R('na', 'الطلب كامل أو مش حجز')
    out[4] = single(th, a.get('q4') or {})
    out[5] = single(th, a.get('q5') or {}, only=last3)
    out[9] = pair(th, a.get('q9') or {})
    out[12] = pair(th, a.get('q12') or {})
    out[13] = violation(th, a.get('q13') or {})
    out[15] = violation(th, a.get('q15') or {})
    out[16] = violation(th, a.get('q16') or {})
    out[18] = single(th, a.get('q18') or {})
    q20 = a.get('q20') or {}
    ti = th.find(q20.get('trigger_quote'), 'customer')
    if ti is None:
        ti = th.find(q20.get('trigger_quote'), 'agent')
    if ti is None:
        out[20] = R('na', 'مفيش فيزا أو سفر برّه')
    else:
        out[20] = single(th, q20)
        out[20]['quotes'] = [q20.get('trigger_quote')] + out[20]['quotes']
    out[23] = pair(th, a.get('q23') or {})
    out[24] = pair(th, a.get('q24') or {}, trig_role='agent')
    qs = [q for q in ((a.get('q25') or {}).get('questions') or []) if isinstance(q, dict)]
    valid, missing = [], []
    for q in qs:
        if GREETING_Q_RE.match(norm(q.get('question_quote') or '')):
            continue  # "كيف الحال" is a greeting, not a question
        qi = th.find(q.get('question_quote'), 'customer')
        if qi is None:
            continue
        ai = th.find(q.get('answer_quote'), 'agent', after=qi)
        (valid if ai is not None else missing).append(q)
    if not valid and not missing:
        out[25] = R('na', 'العميل ما سألش')
    elif missing:
        out[25] = R('no', f'{len(missing)} سؤال من غير رد', [m.get('question_quote') for m in missing[:2]])
    else:
        out[25] = R('yes', f'{len(valid)} سؤال كلهم اتردّ عليهم', [])
    q26 = a.get('q26') or {}
    ia = th.find(q26.get('quote_a'), 'agent')
    ib = th.find(q26.get('quote_b'), 'agent') if q26.get('quote_b') else None
    if ia is not None and (ib is not None or not q26.get('quote_b')):
        out[26] = R('no', 'كلام عكس بعض أو اعتراف بغلط', [q26.get('quote_a'), q26.get('quote_b')])
    else:
        out[26] = R('yes', 'مفيش تناقض', [])
    # Booking gate for sales questions: all three facts quoted from the customer.
    b = a.get('booking') or {}
    booking = all(th.find(b.get(k), 'customer') is not None
                  for k in ('destination_quote', 'date_quote', 'travellers_quote'))
    if not booking:
        for q in (29, 30, 31, 32):
            out[q] = R('na', 'مش طلب حجز كامل (وجهة + تاريخ + عدد)')
    else:
        q29 = a.get('q29') or {}
        i = th.find(q29.get('answer_quote'), 'agent')
        out[29] = (R('yes', 'ذكر سعر', [q29.get('answer_quote')])
                   if i is not None and re.search(r'\d', norm(q29.get('answer_quote'))) else R('no', 'ما ذكرش سعر'))
        opts = [o for o in ((a.get('q30') or {}).get('option_quotes') or []) if th.find(o, 'agent') is not None]
        out[30] = R('yes', f'{len(set(map(norm, opts)))} اختيارات', opts[:2]) if len(set(map(norm, opts))) >= 2 else R('no', 'اختيار واحد أو مفيش')
        out[31] = single(th, a.get('q31') or {})
        out[32] = pair(th, a.get('q32') or {})
    return out


def judge_computer(th, a):
    t, m, at = th.t, th.m, th.at
    out = {}
    # 6 name
    name = (t.get('customer_name') or {}).get('name') or ''
    first = next((w for w in name.split() if re.search('[؀-ۿ]', w) and len(norm(w)) >= 2), None)
    if not first:
        out[6] = R('na', 'اسم العميل مش معروف بالعربي')
    else:
        hit = any(norm(first) in th.n[i].split() for i in th.agent_idx)
        out[6] = R('yes' if hit else 'no', 'ناداه باسمه' if hit else 'ما ناداهوش باسمه')
    # 10 wait then return
    waits = [i for i in th.agent_idx if WAIT_RE.search(th.n[i])]
    if not waits:
        out[10] = R('na', 'الموظف ما قالش "لحظة"')
    else:
        worst = 0
        for i in waits:
            nxt = next((j for j in th.agent_idx if j > i), None)
            gap = business_seconds(at[i], at[nxt]) if nxt is not None else float('inf')
            worst = max(worst, gap)
        ok = worst <= 600
        out[10] = R('yes' if ok else 'no', 'رجع خلال 10 دقايق' if ok else
                    ('ما رجعش' if worst == float('inf') else f'رجع بعد {round(worst/60)} دقيقة'))
    # 17 first response and 14 later gaps
    humans = [i for i, x in enumerate(m) if x['role'] in ('customer', 'agent')]
    if not humans or m[humans[0]]['role'] != 'customer' or not th.agent_idx:
        out[17] = R('na', 'الموظف هو اللي بدأ')
    else:
        g = business_seconds(at[humans[0]], at[th.agent_idx[0]])
        out[17] = R('yes' if g <= 300 else 'no', f'أول رد بعد {fmt(g)} من وقت الشغل')
    gaps = []
    first_agent = th.agent_idx[0] if th.agent_idx else None
    # A customer "burst" starts at a customer message whose previous human message was the agent's.
    prev_human = None
    for i, x in enumerate(m):
        if x['role'] not in ('customer', 'agent'):
            continue
        if (first_agent is not None and i > first_agent and x['role'] == 'customer'
                and prev_human == 'agent'):
            nxt = next((j for j in th.agent_idx if j > i), None)
            if nxt is not None:
                gaps.append(business_seconds(at[i], at[nxt]))
        prev_human = x['role']
    if not gaps:
        out[14] = R('na', 'مفيش ردود بعد أول رد')
    else:
        w = max(gaps)
        out[14] = R('yes' if w <= 600 else 'no', f'أطول تأخير {fmt(w)} من وقت الشغل')
    # 18 gate: more than one human agent
    agents = {x['agent_ext'] for x in m if x['role'] == 'agent'}
    # 19 survey: counted only once the company confirms a chat survey exists
    said = any(SURVEY_RE.search(th.n[i]) for i in th.agent_idx)
    out[19] = R('na', 'مستني تأكيد إن فيه استبيان' + (' (اتذكر في الشات ده)' if said else ''))
    out[22] = R('na', 'لسه ما بيتقاسش')
    out[21] = R('na', 'مدير الجودة')
    out[28] = R('na', 'مدير الجودة')
    for q in (7, 8, 11):
        out[q] = R('na', 'للمكالمات بس')
    # 27 promise kept within 3 days
    pq = (a.get('q27') or {}).get('promise_quote')
    pi = th.find(pq, 'agent')
    if pi is None:
        out[27] = R('na', 'الموظف ما وعدش يرجع')
    else:
        limit = at[pi] + timedelta(days=3)
        later_here = any(j > pi and at[j] <= limit for j in th.agent_idx)
        later_else = any(at[pi] < datetime.fromisoformat(x['at']) <= limit for x in t['later_agent_msgs'])
        ok = later_here or later_else
        out[27] = R('yes' if ok else 'no', 'بعت بعد الوعد' if ok else 'ما رجعش خلال 3 أيام', [pq])
    return out, len(agents) > 1


def fmt(s):
    if s == 0:
        return 'صفر (ردّ قبل ما يبدأ وقت الشغل)'
    if s < 60:
        return f'{int(s)} ثانية'
    if s < 3600:
        return f'{round(s/60)} دقيقة'
    return f'{round(s/3600, 1)} ساعة'


def score_thread(t, answer):
    th = Thread(t)
    res = judge_ai(th, answer)
    comp, multi = judge_computer(th, answer)
    res.update(comp)
    if not multi:
        res[18] = R('na', 'موظف واحد بس')
    earned = possible = 0
    for q, r in res.items():
        if r['ans'] in ('yes', 'no'):
            possible += weight(q)
            earned += weight(q) if r['ans'] == 'yes' else 0
    return {'items': {q: res[q] for q in sorted(res)}, 'earned': earned, 'possible': possible,
            'score': round(100 * earned / possible, 1) if possible else None}
