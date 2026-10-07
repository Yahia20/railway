"""Rubric v0.2: word lists decide what they can; the model only answers questions that need reading."""
import re
from datetime import datetime, timedelta
from .common import norm, display_body, business_seconds, own_text
from .rules import hit
from .score import Thread, R, fmt, weight, TITLES, GREETING_Q_RE

KIND = {**{k: 'ai' for k in (2, 3, 13, 15, 16, 18, 24, 25, 26, 30, 32)},
        **{k: 'computer' for k in (1, 4, 5, 6, 9, 10, 12, 14, 17, 19, 20, 22, 23, 27, 29, 31)},
        21: 'human', 28: 'human', 7: 'calls', 8: 'calls', 11: 'calls'}


def _text(m):
    return m['role'] in ('customer', 'agent') and m['type'] not in ('audio', 'image', 'document', 'reaction', 'video')


def own_words(body):
    """The customer's own text: drop a quoted ">>Name: ..." first line (Bitrix reply-quote) and URLs."""
    if body.startswith('>>'):
        body = body.split('\n', 1)[1] if '\n' in body else ''
    return re.sub(r'\S*https?://\S+|\S+\.(?:app|com|net)/\S*', ' ', body)


def hints(t):
    """Line numbers (1-based, as rendered) the model must answer about. Chosen by code, not by the model."""
    m = t['messages']
    qs = []
    for i, x in enumerate(m):
        body = own_words(display_body(x))
        # A question is a customer line with "?" in the customer's OWN words, and some words besides
        # the mark: a bare "؟؟؟" is a nudge for a reply, which item 14 already measures.
        if x['role'] == 'customer' and _text(x) and ('؟' in body or '?' in body) \
                and norm(body) and not GREETING_Q_RE.match(norm(body)):
            qs.append(i + 1)
    return {
        'questions': qs[:8],
        'unavailable': [i + 1 for i, x in enumerate(m) if x['role'] == 'agent' and hit('unavailable', display_body(x))],
        'objection': [i + 1 for i, x in enumerate(m) if x['role'] == 'customer' and hit('objection', own_text(x))],
    }


def hint_text(t):
    h = hints(t)
    f = lambda xs: ', '.join(f'[{n}]' for n in xs) or '(none)'
    return (f"\n\nCUSTOMER QUESTIONS: {f(h['questions'])}\nUNAVAILABLE LINES: {f(h['unavailable'])}"
            f"\nOBJECTION LINES: {f(h['objection'])}")


def first_hit(th, lst, role, after=-1, only=None):
    for i, x in enumerate(th.m):
        if i <= after or x['role'] != role or (only is not None and i not in only):
            continue
        p = hit(lst, own_text(x), own_text(x))
        if p:
            return i, p
    return None, None


def per_line(th, lines, answers, label_none, label_yes, label_no):
    """lines are 1-based; each needs a valid agent quote after it."""
    if not lines:
        return R('na', label_none)
    # Keys may come back as "11", "[11]" or "line 11": the digits are the line number.
    answers = {re.sub(r'\D', '', str(k)): v for k, v in answers.items()} if isinstance(answers, dict) else {}
    ok, missing = [], []
    for n in lines:
        q = answers.get(str(n))
        (ok if th.find(q, 'agent', after=n - 1) is not None else missing).append((n, q))
    return ok, missing


def score_thread(t, a):
    th = Thread(t)
    m, at = th.m, th.at
    out = {}
    first2, last3 = set(th.agent_idx[:2]), set(th.agent_idx[-3:])

    # 1 greeting + identity, both inside the agent's first two lines
    gi, gp = first_hit(th, 'greeting', 'agent', only=first2)
    ii, ip = first_hit(th, 'identity', 'agent', only=first2)
    out[1] = (R('yes', f'ترحيب ("{gp}") وتعريف ("{ip}")') if gi is not None and ii is not None else
              R('no', 'مفيش ترحيب في أول رسالتين' if gi is None else 'مفيش اسم الشركة في أول رسالتين'))
    # 2 recap (model)
    q = (a.get('q2') or {}).get('answer_quote')
    out[2] = R('yes', 'كرّر الطلب', [q]) if th.find(q, 'agent') is not None else R('no', 'ما كرّرش طلب العميل')
    # booking facts (model, customer lines only)
    b = a.get('booking') or {}
    pos = {k: th.find(b.get(k + '_quote'), 'customer') for k in ('destination', 'date', 'travellers')}
    first_agent = th.agent_idx[0] if th.agent_idx else len(m)
    # 3 probing: a trip request that was missing something before the agent's first line
    if pos['destination'] is None:
        out[3] = R('na', 'العميل ما ذكرش وجهة')
    else:
        missing = [k for k, i in pos.items() if i is None or i > first_agent]
        if not missing:
            out[3] = R('na', 'الطلب كان كامل')
        else:
            q = (a.get('q3') or {}).get('answer_quote')
            names = {'destination': 'الوجهة', 'date': 'التاريخ', 'travellers': 'عدد الأفراد'}
            miss = '، '.join(names[k] for k in missing)
            out[3] = (R('yes', f'كان ناقص {miss} وسأل', [q]) if th.find(q, 'agent') is not None
                      else R('no', f'كان ناقص {miss} وما سألش'))
    # 4 assurance / 5 closing
    i, p = first_hit(th, 'assure', 'agent')
    out[4] = R('yes', f'قال "{p}"') if i is not None else R('no', 'مفيش كلمة من قايمة الطمأنة')
    i, p = first_hit(th, 'closing', 'agent', only=last3)
    out[5] = R('yes', f'قال "{p}"') if i is not None else R('no', 'مفيش جملة ختام في آخر 3 رسايل')
    # 9 empathy
    ci, cp = first_hit(th, 'complaint', 'customer')
    if ci is None:
        out[9] = R('na', 'العميل ما اشتكاش')
    else:
        i, p = first_hit(th, 'apology', 'agent', after=ci)
        out[9] = R('yes', f'العميل قال "{cp}" والموظف قال "{p}"') if i is not None else \
            R('no', f'العميل قال "{cp}" ومفيش اعتذار')
    # 12 re-explaining
    ci, cp = first_hit(th, 'not_understood', 'customer')
    if ci is None:
        out[12] = R('na', 'العميل ما قالش إنه مش فاهم')
    else:
        nxt = next((j for j in th.agent_idx if j > ci), None)
        prev = [th.n[j] for j in th.agent_idx if j < ci]
        ok = nxt is not None and th.n[nxt] not in prev
        out[12] = R('yes' if ok else 'no', f'العميل قال "{cp}"' + (' والموظف شرح' if ok else ' ومفيش شرح جديد'))
    # 13 / 15 / 16 violations (model)
    for qn, label in ((13, 'كلام مش مهني'), (15, 'كلام سلبي عن مورّد أو الشركة'), (16, 'قلّة أدب')):
        v = (a.get(f'q{qn}') or {}).get('violation_quote')
        out[qn] = R('no', label, [v]) if th.find(v, 'agent') is not None else R('yes', 'مفيش')
    # 18 transfer (model, only when a second agent wrote)
    agents = {x['agent_ext'] for x in m if x['role'] == 'agent'}
    if len(agents) < 2:
        out[18] = R('na', 'موظف واحد بس')
    else:
        q = (a.get('q18') or {}).get('answer_quote')
        out[18] = R('yes', 'قال للعميل', [q]) if th.find(q, 'agent') is not None else R('no', 'ما قالش للعميل إنه هيتحول')
    # 20 documents
    vi = next((i for i, x in enumerate(m) if x['role'] == 'customer' and hit('visa', own_text(x))), None)
    if vi is None:
        out[20] = R('na', 'العميل ما سألش عن تأشيرة')
    else:
        i, p = first_hit(th, 'documents', 'agent')
        out[20] = R('yes', f'ذكر "{p}"') if i is not None else R('no', 'العميل سأل عن تأشيرة وما ذكرش أي مستند')
    # 23 next step
    i, p = first_hit(th, 'next_step', 'agent')
    out[23] = R('yes', f'قال "{p}"') if i is not None else R('no', 'ما قالش إيه اللي هيحصل بعد كده')
    # 24 / 25 / 32 per-line answers (lines chosen by code)
    h = hints(t)
    r = per_line(th, h['unavailable'], a.get('q24'), '', '', '')
    out[24] = R('na', 'الموظف ما قالش إن حاجة مش متاحة') if isinstance(r, dict) else (
        R('yes', 'عرض بديل', [r[0][0][1]]) if r[0] else R('no', 'قال مش متاح وما عرضش بديل'))
    r = per_line(th, h['questions'], a.get('q25'), '', '', '')
    if isinstance(r, dict):
        out[25] = R('na', 'العميل ما سألش (مفيش علامة استفهام)')
    else:
        ok, miss = r
        out[25] = (R('yes', f'{len(ok)} سؤال، كلهم اتردّ عليهم') if not miss else
                   R('no', f'{len(miss)} من {len(ok) + len(miss)} سؤال من غير رد',
                     [display_body(m[n - 1])[:120] for n, _ in miss[:2]]))
    r = per_line(th, h['objection'], a.get('q32'), '', '', '')
    # 26 contradiction (model)
    q26 = a.get('q26') or {}
    ia = th.find(q26.get('quote_a'), 'agent')
    ib = th.find(q26.get('quote_b'), 'agent') if q26.get('quote_b') else None
    out[26] = (R('no', 'كلام عكس بعض أو اعتراف بغلط', [q26.get('quote_a'), q26.get('quote_b')])
               if ia is not None and (ib is not None or not q26.get('quote_b')) else R('yes', 'مفيش تناقض'))
    # 27 promise kept
    pi, pp = first_hit(th, 'promise', 'agent')
    if pi is None:
        out[27] = R('na', 'الموظف ما وعدش يرجع')
    else:
        limit = at[pi] + timedelta(days=3)
        ok = any(j > pi and at[j] <= limit for j in th.agent_idx) or \
            any(at[pi] < datetime.fromisoformat(x['at']) <= limit for x in t['later_agent_msgs'])
        out[27] = R('yes' if ok else 'no', f'وعد ("{pp}") ' + ('وبعت بعدها' if ok else 'وما رجعش خلال 3 أيام'))
    # sales, gated on a complete booking request from the customer
    if any(v is None for v in pos.values()):
        for qn in (29, 30, 31, 32):
            out[qn] = R('na', 'مش طلب حجز كامل (وجهة + تاريخ + عدد)')
    else:
        pr = next((i for i in th.agent_idx if re.search(r'\d', th.n[i]) and hit('currency', display_body(m[i]))), None)
        out[29] = R('yes', 'ذكر سعر') if pr is not None else R('no', 'ما ذكرش سعر برقم وعملة')
        raw_opts = (a.get('q30') or {}).get('option_quotes')
        raw_opts = raw_opts if isinstance(raw_opts, list) else []
        opts = {norm(o) for o in raw_opts if th.find(o, 'agent') is not None}
        out[30] = R('yes', f'{len(opts)} اختيارات') if len(opts) >= 2 else R('no', 'اختيار واحد أو مفيش')
        i, p = first_hit(th, 'book_ask', 'agent')
        out[31] = R('yes', f'قال "{p}"') if i is not None else R('no', 'ما طلبش يأكد الحجز')
        out[32] = R('na', 'العميل ما اعترضش') if isinstance(r, dict) else (
            R('yes', 'ردّ على الاعتراض') if r[0] else R('no', 'العميل اعترض ومفيش رد'))
    # computed timing + name, shared with v0.1
    from .score import judge_computer
    comp, _ = judge_computer(th, {'q27': {}})
    for qn in (6, 14, 17, 21, 22, 28, 7, 8, 11):
        out[qn] = comp[qn]
    # 10 "one moment" -> next agent line within 10 business minutes (v0.2 word list)
    waits = [i for i in th.agent_idx if hit('wait', display_body(m[i]))]
    if not waits:
        out[10] = R('na', 'الموظف ما قالش "لحظة"')
    else:
        worst = 0
        for i in waits:
            nxt = next((j for j in th.agent_idx if j > i), None)
            worst = max(worst, business_seconds(at[i], at[nxt]) if nxt is not None else float('inf'))
        out[10] = R('yes' if worst <= 600 else 'no', 'رجع خلال 10 دقايق' if worst <= 600 else
                    ('ما رجعش' if worst == float('inf') else f'رجع بعد {fmt(worst)}'))
    # 19 survey: not counted until the company confirms a chat survey exists
    said = any(hit('survey', display_body(m[i])) for i in th.agent_idx)
    out[19] = R('na', 'مستني تأكيد إن فيه استبيان' + (' (اتذكر في الشات ده)' if said else ''))
    earned = possible = 0
    for qn, res in out.items():
        if res['ans'] in ('yes', 'no'):
            possible += weight(qn)
            earned += weight(qn) if res['ans'] == 'yes' else 0
    return {'items': {k: out[k] for k in sorted(out)}, 'earned': earned, 'possible': possible,
            'score': round(100 * earned / possible, 1) if possible else None}
