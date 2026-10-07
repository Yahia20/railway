"""For each mistake: the messages that show it, taken from the chat by the same rules that scored it."""
import re
from datetime import datetime, timedelta
from .common import display_body, business_seconds, norm, RIYADH, own_text, split_quote
from .rules import hit
from .score import Thread, fmt
from .score_v02 import hints


def _msg(th, i):
    m = th.m[i]
    quoted, own = split_quote(display_body(m)) if m['role'] == 'customer' else (None, display_body(m))
    return (m['role'], th.at[i].astimezone(RIYADH).strftime('%m-%d %H:%M'), own, quoted)


def _after(th, i, role='agent'):
    return next((j for j, x in enumerate(th.m) if j > i and x['role'] == role), None)


def _first(th, lst, role, after=-1):
    return next((j for j, x in enumerate(th.m) if j > after and x['role'] == role and hit(lst, own_text(x))), None)


def _gap(th, a, b):
    return fmt(business_seconds(th.at[a], th.at[b])).replace('صفر (ردّ قبل ما يبدأ وقت الشغل)', 'صفر')


def evidence(q, t, item):
    """(note, [messages]) for mistake q. Each message is (role, time, text)."""
    th = Thread(t)
    A = th.agent_idx
    cust = [i for i, x in enumerate(th.m) if x['role'] == 'customer']
    out, note = [], None
    if q == 1:
        out = A[:2]
        note = 'أول رسالتين من الموظف'
    elif q in (2, 3, 4):
        before = [i for i in cust if not A or i < A[0]][-2:]
        out = before + A[:1]
        note = 'طلب العميل، وأول رد من الموظف'
        if q == 3:
            note = item['why'].replace(' وما سألش', '') + '، والموظف ما سألش عنه. طلب العميل وأول رد من الموظف'
    elif q in (5, 23, 31):
        out = A[-2:]
        note = 'آخر رسايل الموظف في المحادثة'
    elif q == 6:
        note = 'اسم العميل مسجّل عندنا، ومفيش ولا رسالة من الموظف فيها اسمه'
    elif q == 9:
        c = _first(th, 'complaint', 'customer')
        out = [c] + ([_after(th, c)] if c is not None and _after(th, c) is not None else [])
        note = 'شكوى العميل، ورد الموظف بعدها'
    elif q == 10:
        w = next((i for i in A if hit('wait', display_body(th.m[i]))), None)
        n = _after(th, w) if w is not None else None
        out = [w, n]
        note = f'قال "لحظة"، ورجع بعد {_gap(th, w, n)} (بنحسب وقت الدوام بس، من 9 الصبح لـ 9 بالليل)' if n is not None else 'قال "لحظة" وما رجعش'
    elif q == 12:
        c = _first(th, 'not_understood', 'customer')
        out = [c, _after(th, c)]
        note = 'العميل قال إنه مش فاهم، ورد الموظف بعدها'
    elif q in (13, 15, 16):
        quote = (item.get('quotes') or [None])[0]
        out = [th.find(quote, 'agent')]
    elif q == 14:
        worst, pair = -1, None
        prev = None
        for i, x in enumerate(th.m):
            if x['role'] not in ('customer', 'agent'):
                continue
            if A and i > A[0] and x['role'] == 'customer' and prev == 'agent':
                n = _after(th, i)
                if n is not None:
                    g = business_seconds(th.at[i], th.at[n])
                    if g > worst:
                        worst, pair = g, (i, n)
            prev = x['role']
        if pair:
            out = list(pair)
            note = f'العميل كتب، والموظف رد بعد {fmt(worst)} (بنحسب وقت الدوام بس، من 9 الصبح لـ 9 بالليل)'
    elif q == 17:
        out = [cust[0], A[0]]
        note = f'العميل كتب، وأول رد من الموظف جه بعد {_gap(th, cust[0], A[0])} (بنحسب وقت الدوام بس، من 9 الصبح لـ 9 بالليل)'
    elif q == 18:
        first_ext = th.m[A[0]]['agent_ext']
        second = next(i for i in A if th.m[i]['agent_ext'] != first_ext)
        last_first = max(i for i in A if i < second and th.m[i]['agent_ext'] == first_ext)
        out = [last_first, second]
        note = 'آخر رسالة من الموظف الأول، وأول رسالة من الموظف التاني'
    elif q == 20:
        v = _first(th, 'visa', 'customer')
        out = [v] + [i for i in A if i > v][:1]
        note = 'سؤال العميل عن التأشيرة، ورد الموظف'
    elif q == 24:
        u = hints(t)['unavailable'][0] - 1
        out = [u] + [i for i in A if i > u][:1]
        note = 'الموظف قال إن الطلب مش متاح، وده اللي كتبه بعدها' if len(out) > 1 else 'الموظف قال إن الطلب مش متاح، وما كتبش حاجة بعدها'
    elif q == 25:
        missing = [th.find(x, 'customer') for x in item.get('quotes') or []]
        missing = [i for i in missing if i is not None][:2]
        for i in missing:
            out.append(i)
            n = _after(th, i)
            if n is not None:
                out.append(n)
        note = 'سؤال العميل، واللي الموظف كتبه بعده'
    elif q == 26:
        out = [th.find(x, 'agent') for x in item.get('quotes') or []]
        note = 'رسالتين من الموظف عكس بعض'
    elif q == 27:
        p = _first(th, 'promise', 'agent')
        out = [p]
        later = [j for j in A if j > p]
        note = (f'وعد العميل، وأول رسالة بعدها كانت بعد أكتر من 3 أيام' if later
                else 'وعد العميل، وما بعتش أي رسالة بعدها')
    elif q in (29, 30):
        ask = next((i for i in cust if re.search(r'سعر|كم|بكم|تكلف', norm(own_text(th.m[i])))), cust[0])
        out = [ask] + [i for i in A if i > ask][:1]
        note = 'طلب العميل، ورد الموظف' if q == 29 else 'طلب العميل، ورد الموظف (اختيار واحد أو من غير اختيارات)'
    elif q == 33:
        cq, aq = (item.get('quotes') or [None, None])[:2]
        out = [th.find(cq, 'customer'), th.find(aq[:40] if aq else aq, 'agent')]
        note = 'العميل قال المعلومة، وبعدها الموظف سأل عنها تاني'
    elif q == 32:
        o = hints(t)['objection'][0] - 1
        out = [o] + [i for i in A if i > o][:1]
        note = 'اعتراض العميل، ورد الموظف بعده'
    msgs = [_msg(th, i) for i in out if i is not None]
    return note, msgs


def evidence_no_extra(q, t, item):
    """Mistakes added in v0.5 (34-38): the agent line(s) the check found."""
    th = Thread(t)
    out = [th.find(x[:60], 'agent') for x in (item.get('quotes') or []) if x]
    notes = {34: 'الرسالة اللي فيها الغلط', 35: 'الوعد', 36: 'الرسالتين اللي ما بيطلعوش مع بعض',
             37: 'الرسالة', 38: 'الرسالة'}
    return notes.get(q), [_msg(th, i) for i in out if i is not None]


def evidence_yes(q, t, item):
    """What shows the agent DID it. (note, messages); messages may be empty when the proof is an absence."""
    th = Thread(t)
    A = th.agent_idx
    cust = [i for i, x in enumerate(th.m) if x['role'] == 'customer']
    out, note = [], None
    if q == 1:
        out, note = A[:2], 'أول رسالتين من الموظف'
    elif q in (2, 3, 18, 30):
        out = [th.find(x[:60], 'agent') for x in (item.get('quotes') or [])[:2] if x]
        if q == 30:
            note = 'عرض ' + (item.get('why') or '')
    elif q == 4:
        out = [_first(th, 'assure', 'agent')]
    elif q == 5:
        out = [next((i for i in A[-3:] if hit('closing', display_body(th.m[i]))), None)]
    elif q == 6:
        note = 'ناداه باسمه'
    elif q == 9:
        c = _first(th, 'complaint', 'customer')
        out = [c, _first(th, 'apology', 'agent', after=c)]
    elif q == 10:
        w = next((i for i in A if hit('wait', display_body(th.m[i]))), None)
        out = [w, _after(th, w) if w is not None else None]
    elif q == 12:
        c = _first(th, 'not_understood', 'customer')
        out = [c, _after(th, c)]
    elif q in (13, 15, 16, 26, 34, 35, 36, 37, 38):
        note = 'مفيش'
    elif q in (14, 17):
        note, msgs = evidence(q, t, item)
        return note, msgs
    elif q == 20:
        v = _first(th, 'visa', 'customer')
        out = [v, _first(th, 'documents', 'agent', after=v)]
    elif q == 23:
        out = [_first(th, 'next_step', 'agent')]
    elif q == 24:
        u = hints(t)['unavailable'][0] - 1
        out = [u] + [th.find(x[:60], 'agent') for x in (item.get('quotes') or [])[:1] if x]
    elif q == 25:
        for n in hints(t)['questions'][:2]:
            out += [n - 1, _after(th, n - 1)]
        note = item.get('why')
    elif q == 27:
        p = _first(th, 'promise', 'agent')
        out = [p, next((j for j in A if j > p), None)] if p is not None else []
    elif q == 29:
        out = [next((i for i in A if re.search(r'\d', display_body(th.m[i])) and hit('currency', display_body(th.m[i]))), None)]
    elif q == 31:
        out = [_first(th, 'book_ask', 'agent')]
    elif q == 32:
        o = hints(t)['objection'][0] - 1
        out = [o, next((i for i in A if i > o), None)]
    elif q == 33:
        note = 'ما سألش عن حاجة العميل قالها قبل كده'
    seen, msgs = set(), []
    for i in out:
        if i is not None and i not in seen:
            seen.add(i)
            msgs.append(_msg(th, i))
    return note, msgs
