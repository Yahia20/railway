"""Chat quality scorecard v6: v5.2 with every fix from the 2026-11-01 review and the owner's decisions of
2026-11-03/04. Approved for production by the QA owner on 2026-11-05.

This file is the engine of record. It was generated from the trial's qa_score_v5.py by
local-reports/v52-fix-2026-11-01/apply_fixes.py and moved here unchanged except for three things: the excluded
agents left this public file for agents.qa_excluded (034), times are read from each message's full timestamp
when it has one so a chat across New Year keeps its order, and this docstring.

In the worker, app.qa.v6.engine builds the chat, asks DeepSeek, and calls score_chat(). The command line is kept
for replaying stored answers without paying again:

  python -m app.qa.v6.scorecard chats.json votes_dir alerts_dir --obs 2026-11-03T12:00:00 --year 2026 > scores.json

  chats.json                 [{id, employee, date, messages: [{role, date, time, at, text, reply_to, sender, attachment}]}]
  <votes_dir>/{id}_{k}.json  prompt.txt answers, k = 0..2, each {"out": {...}}
  <alerts_dir>/{id}.json     prompt_alerts.txt answer: {"out": {loss_reason, alerts}}
  --obs                      observation end, Riyadh wall time (drives "pending")
"""
import collections
import datetime as dt
import json
import os
import re
import sys

# ---------------------------------------------------------------- rubric
# item -> checks. weight in points of 100. Every check is yes / no / partial / pending / review / n/a.
RUBRIC = [
    {'id': '1', 'ar': 'فهم الطلب', 'en': 'Understanding the request', 'checks': [
        {'key': 'probe', 'w': 10, 'ar': 'سأل عن التفاصيل الناقصة', 'en': 'Asked for the missing trip details',
         'rule_ar': 'الوجهة أو التاريخ أو عدد الأفراد اللي كانوا ناقصين لما الموظف دخل الشات. بيتحسب بس السؤال عن اللي كان ناقص فعلاً. لو كل التفاصيل كانت موجودة: مش منطبق.',
         'rule_en': 'Destination, date or number of travellers that were still missing when the agent joined. Only a question about a genuinely missing detail counts. All details present: not applicable.'},
        {'key': 'noask', 'w': 5, 'ar': 'ما سألش عن حاجة العميل قالها', 'en': 'Did not re-ask details the customer had given',
         'rule_ar': 'لو العميل قال الوجهة أو التاريخ أو العدد، والموظف سأل عنها تاني (أو بعت فورم بيطلبها) = ما عملش.',
         'rule_en': 'If the customer had given destination, date or travellers and the agent asked for it again (or sent a form asking for it) = fail.'}]},
    {'id': '2', 'ar': 'العرض', 'en': 'The offer', 'checks': [
        {'key': 'offer', 'w': 15, 'ar': 'بعت عرض للعميل المؤهل', 'en': 'Sent an offer to a qualified customer',
         'rule_ar': 'العميل المؤهل = طلب مبيعات + وجهة + تاريخ + عدد أفراد. مش منطبق لو الشات اتحوّل لقسم تاني، أو الموظف سأل سؤال والعميل ماردش، أو الطلب نفسه مش متاح (منتج / تأشيرة / مورّد)، أو المرفق مش واضح (مراجعة يدوية).',
         'rule_en': 'Qualified = sales request + destination + date + travellers. Not applicable if the chat was transferred, the agent asked a question the customer never answered, the product itself was unavailable (product / visa / supplier), or an attachment could not be identified (manual review).'},
        {'key': 'match', 'w': 5, 'ar': 'العرض ما تعارضش مع الطلب', 'en': 'The offer did not conflict with the request',
         'rule_ar': 'الوجهة أو التاريخ أو العدد في العرض مش مخالفين للي العميل طلبه. (ده بيكشف التعارض بس، مش اكتمال العرض.)',
         'rule_en': 'Destination, date or travellers in the offer do not contradict what the customer asked for. (Detects conflict only, not completeness.)'}]},
    {'id': '3', 'ar': 'المتابعة', 'en': 'Follow-up', 'checks': [
        {'key': 'followup', 'w': 15, 'ar': 'تابع العميل الصامت 3 مرات', 'en': 'Followed up a silent customer three times',
         'rule_ar': 'لما العميل يسكت بعد العرض أو بعد آخر رسالة من الموظف من غير سبب واضح. المحاولة = رسالة (أو رسايل متتالية) من الموظف؛ محاولة جديدة لو العميل رد بينهم أو فرق 4 ساعات. عمل = 3 محاولات، ومحاولة أو اتنين = جزء من الدرجة. لو لسه ما عداش 24 ساعة من بداية الصمت: لسه. لو العميل قفل بنفسه: مش منطبق. لو قال هيرجع في ميعاد: نستنى الميعاد. لو الشات اتحوّل والقسم التاني رد: مش منطبق، ولو ما ردش: مراجعة.',
         'rule_en': "When the customer goes silent after the offer or after the agent's last message with no stated reason. An attempt = one agent message or a run of consecutive ones; a new attempt needs a customer message in between or a 4-hour gap. Pass = three attempts; one or two = part of the points. Under 24 hours since the silence started: pending. Customer closed the request: not applicable. Customer named a time to come back: wait for it. Transferred and the other department replied: not applicable; if it did not: review."}]},
    {'id': '4', 'ar': 'الالتزام بالوعد', 'en': 'Keeping promises', 'checks': [
        {'key': 'promise', 'w': 10, 'ar': 'رجع للعميل زي ما وعد', 'en': 'Came back as promised',
         'rule_ar': 'كل وعد ("هرسلك العرض"، "اشيك وارجعلك") بيتحسب لوحده. عمل = الحاجة اللي وعد بيها اتبعتت. مش منطبق لو الوعد كان مستني تفاصيل من العميل ما بعتهاش، أو حاجة خارجية ما حصلتش، أو العميل قفل الطلب قبل الموعد. لسه لو الموعد ما جاش.',
         'rule_en': 'Every promise ("I will send the offer", "I will check and come back") is judged separately. Pass = the promised thing was sent. Not applicable if the promise waited for details the customer never sent, an external event that never happened, or the customer closed the request before it was due. Pending if not yet due.'}]},
    {'id': '5', 'ar': 'الرد على طلبات العميل', 'en': "Answering the customer's requests", 'checks': [
        {'key': 'answered', 'w': 10, 'ar': 'رد على كل طلبات العميل', 'en': "Answered every request",
         'rule_ar': 'كل طلب أو سؤال له موضوع واضح (سعر، تفاصيل، مواعيد، إمكانية). "؟" لوحدها أو "أبغى أستفسر" مش طلب. السؤال المرتد ("الجنسية إيه؟") مش رد، بس لو العميل ما جاوبش عليه الطلب بيبقى مش منطبق. الدرجة = نسبة الطلبات اللي اتردّ عليها.',
         'rule_en': 'Every request or question with a clear subject (price, details, dates, feasibility). A bare "?" or "I want to ask" is not a request. Asking the customer back is not an answer, but if the customer never replied to it the request becomes not applicable. Score = share of requests answered.'}]},
    {'id': '6', 'ar': 'التعامل مع الاعتراض', 'en': 'Objection handling', 'checks': [
        {'key': 'objection', 'w': 5, 'ar': 'رد على اعتراض العميل', 'en': "Handled the customer's objection",
         'rule_ar': 'غالي / لقيت أرخص / هفكر / مش عاجبني / مش واثق. رد كويس = حسب النوع (سبب أو بديل للسعر، سؤال أو سبب للمنافس، خطوة جاية للتأجيل، بديل للمنتج، إثبات أو خطوة جاية للثقة). رد ضعيف = نص درجة. تطنيش = صفر. الخصم مسموح. اعتراضين ورا بعض = حلقة واحدة.',
         'rule_en': 'Expensive / found cheaper / will think / do not like it / do not trust. Good = by type (reason or alternative for price; question or reason for competitor; next step for delay; alternative for product; proof or next step for trust). Weak = half. Ignored = zero. Discounts are allowed. Consecutive objections = one episode.'}]},
    {'id': '7', 'ar': 'دقة المعلومة', 'en': 'Information consistency', 'checks': [
        {'key': 'correct', 'w': 8, 'ar': 'مفيش تناقض أو غلطة حسابية', 'en': 'No contradiction or calculation error',
         'rule_ar': 'معلومتين عكس بعض عن نفس الحاجة (سعر، تاريخ، متاح/مش متاح)، أو أرقام ما بتطلعش مع بعض، أو تأكيد بيانات حجز غلط. (ده مش فحص لصحة المعلومة نفسها: ده محتاج مصدر بيانات.)',
         'rule_en': 'Two opposite statements about the same fact (price, date, availability), numbers that do not add up, or booking details confirmed wrongly. (This is not a check of factual accuracy, which needs a data source.)'}]},
    {'id': '8', 'ar': 'سرعة الرد', 'en': 'Response time', 'checks': [
        {'key': 'speed_first', 'w': 4, 'ar': 'أول رد من الموظف جه بسرعة', 'en': 'First agent reply came quickly',
         'rule_ar': 'من آخر رسالة للعميل قبل دخول الموظف لحد أول رسالة من الموظف، بدقايق الدوام (10 الصبح لـ 1 بالليل). 15 دقيقة أو أقل = درجة كاملة، 4 ساعات أو أكتر = صفر، وبينهم بالتدريج.',
         'rule_en': "From the customer's last message before the agent joined to the agent's first message, in working minutes (10:00 to 01:00). 15 minutes or less = full, 4 hours or more = zero, linear in between."},
        {'key': 'speed_next', 'w': 3, 'ar': 'باقي الردود جت في وقتها', 'en': 'Later replies came in time',
         'rule_ar': 'أطول انتظار للعميل جوه الشات بعد أول رد (من رسالة العميل لرد الموظف عليها)، بدقايق الدوام. 30 دقيقة أو أقل = درجة كاملة، 4 ساعات أو أكتر = صفر.',
         'rule_en': "The longest wait inside the chat after the first reply (customer message to the agent's reply), in working minutes. 30 minutes or less = full, 4 hours or more = zero."}]},
    {'id': '9', 'ar': 'الاحترافية', 'en': 'Professionalism', 'checks': [
        {'key': 'polite', 'w': 5, 'ar': 'رد محترم من غير تطنيش', 'en': 'Respectful, no brush-off',
         'rule_ar': 'مفيش شتيمة أو تريقة أو لوم، ومفيش "اقرا فوق" / "مش شغلي". الإحالة لجهة مختصة بعد شرح إن الخدمة مش عندنا مش تطنيش. أي مخالفة بتروح لمراجعة بشرية، ومفيش خصم أوتوماتيك.',
         'rule_en': 'No insult, sarcasm or blame, and no "read above" / "not my job". Referring the customer to the right authority after explaining we do not offer the service is not a brush-off. Any violation goes to human review; nothing is deducted automatically.'},
        {'key': 'positive', 'w': 2, 'ar': 'ما اتكلمش وحش عن الشركة أو الموردين', 'en': 'No negative talk about the company or suppliers',
         'rule_ar': 'كلام سلبي عن طيران أو فندق أو مورّد أو ترافل جيت. المعلومة أو التحذير المفيد مش كلام سلبي.',
         'rule_en': 'Negative talk about an airline, hotel, supplier or Travel Gate. A fact or a useful warning is not negative talk.'},
        {'key': 'spelling', 'w': 3, 'ar': 'مفيش قالب ناقص أو غلط إملاء', 'en': 'No broken template or spelling error',
         'rule_ar': 'قالب ما اتملاش أو مكرر، رسالة لعميل تاني، نص مشوّه. اللهجة والهمزة وغلطة حرف واحد (نوفبر، الرحلع) مش محسوبين.',
         'rule_en': 'Unfilled or scrambled template, a message meant for another customer, garbled text. Dialect spelling, hamza and a one-letter typo (نوفبر، الرحلع) are not counted.'}]},
]
CHECKS = {c['key']: c for it in RUBRIC for c in it['checks']}
WEIGHTS = {k: c['w'] for k, c in CHECKS.items()}
assert sum(WEIGHTS.values()) == 100
VIOLATION_CHECKS = ('correct', 'polite', 'positive', 'spelling', 'match')   # owner decision 2026-11-03: never deducted automatically, any flag -> human review
WORK_START, WORK_END = 10, 25         # working window 10:00 -> 01:00 next day (owner decision 2026-10-27; was 09:00 since 2026-09-27)
SPEED_FIRST = (15, 240)               # working minutes: full credit at or under 15, zero at or over 240, linear between
SPEED_NEXT = (30, 240)
UNAVAILABLE_REASONS = ('Product not Available', 'visa issue', 'Supplier')
FIELDS = ('destination', 'date', 'travellers')
FOLLOWUP_GAP_H = 4          # a new attempt needs a customer message in between or this gap
FOLLOWUP_WINDOW_H = 24      # owner decision 2026-11-03: 24 hours without coming back = did not follow up (was 72)
FOLLOWUP_REQUIRED = 3       # owner decision 2026-11-03: three follow-ups (was 2); fewer earn their share
TRANSFER_ACCEPT_MIN = 240   # working minutes for the other department to reply before the handover counts
ANSWER_DUE_H = 24           # a request younger than this at observation end -> pending
OBJECTION_DUE_H = 24
PROMISE_DEFAULT_DUE_H = 24
THIN_WEIGHT = 30            # a chat judged on less than this many points is "thin"
MIN_CHATS_FOR_COLOUR = 10   # agent colour (green/yellow/red) only from this many chats
# Agents whose chats are left out of the review entirely (owner decision 2026-10-17). In production that is
# agents.qa_excluded (034), set from the git-ignored roster: this repository is public and holds no staff names.
# The command line takes no names either; pass it chats that are already filtered.
EXCLUDED_AGENTS = ()


def excluded(name):
    return any(re.search(p, name or '', re.I) for p in EXCLUDED_AGENTS)
ALERT_KINDS = ('wrong_customer', 'contradiction', 'misleading', 'no_consent', 'calc_error',   # whole-chat (call 2)
               'impatience', 'complaint', 'form_repeat', 'sent_away', 'no_alternative')      # single-message (call 2)
CUSTOMER_ALERTS = ('impatience', 'complaint')
TWO_MESSAGE_ALERTS = ('contradiction', 'misleading', 'calc_error')
ALERT_B_ROLE = {'form_repeat': 'customer', 'no_alternative': 'customer'}
JEV_SURE = 0.70             # jev signal at or above this = alert (validated 2026-10-17: impatience 10/10, form 4/4, sent_away 5/5 at 0.70)
JEV_KINDS = {'impatience': 'impatience', 'complaint': 'complaint', 'form': 'form_repeat', 'sent_away': 'sent_away', 'no_alternative': 'no_alternative'}

GOOD = {
    'price': lambda e: (e['reason'] or e['alternative']) and sum(bool(v) for v in e.values()) >= 2,
    'competitor': lambda e: e['question'] or e['reason'],
    'delay': lambda e: e['next_step'],
    'product': lambda e: e['alternative'],
    'trust': lambda e: e['reason'] or e['next_step'],
}
ELEMENTS = ('acknowledge', 'reason', 'alternative', 'question', 'next_step', 'discount')
STOP_WORDS = {'؟', '?', 'الو', 'الوو', 'عندكم', 'هلا', 'تمام', 'اوكي', 'اوك', 'ماشي', 'ok', 'شكرا', 'طيب', 'ممكن', 'لو', 'سمحت', 'سمحتي', 'من', 'فضلك', 'يا', 'اخي', 'اختي'}
# an Arabic question often has no question mark
QUESTION = re.compile(r'[؟?]|^(ممكن|هل)\s|(^|\s)و?(كم|بكم|متى|متي|وش|ايش|إيش|اين|أين|فين|ليش|مين)(\s|$)|\sولا\s')
REFINES_PLACE = re.compile(r'مدن|مدينه|مدينة|منطقه|منطقة|\bcity|\bcities', re.I)     # a city inside a country already given is precision, not a re-ask
ASKS = re.compile(r'[؟?]|ابي|ابغى|ابغي|أبي|أبغى|ممكن|كم|سعر|اسعار|أسعار|ارسل|أرسل|ابعت|عايز|عاوز|محتاج|هل|want|need|send|price|how|please', re.I)
CALL_WORDS = re.compile(r'اتصل|اتصال|مكالم|كلمتك|رنيت|\bcall|\bphone', re.I)
GREETING_WORDS = {'السلام', 'عليكم', 'وعليكم', 'ورحمه', 'الله', 'وبركاته', 'اهلا', 'وسهلا', 'مرحبا', 'حياك', 'صباح', 'مساء', 'الخير', 'النور', 'كيف', 'الحال'}
THANKS_WORDS = {'ملصق', 'شكرا', 'لك', 'لكم', 'جزيلا', 'تمام', 'اوكي', 'اوك', 'ماشي', 'طيب', 'ok', 'thanks', 'thank', 'you', 'مشكور', 'مشكورين', 'جزاك', 'الله', 'خير', 'يعطيك', 'العافيه', 'حبيبي'}
ATTACHMENT = '[مرفق]'
OTHER_TITLES = {'job_seeker': 'بيدور على شغل', 'supplier': 'مورّد بيعرض خدمة', 'wrong_number': 'رقم غلط', 'general': 'سؤال عام', 'no_request': 'مفيش طلب واضح'}
OPENER = re.compile(r'^(ا|أ)(بغ[ىي]|بي|حب|ريد) (ا|أ)ستفسر$|^عندي (سؤال|استفسار)$|^(ممكن|ابي) (ا|أ)س(أ|ا)ل$')


def norm(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def plain_words(text):
    return norm(re.sub(r'[^\w\s]', ' ', text or '')).replace('أ', 'ا').replace('إ', 'ا').replace('ة', 'ه').replace('ً', '').split()


def is_greeting(text):
    words = plain_words(text)
    return bool(words) and all(w in GREETING_WORDS or w.startswith('هلا') for w in words)


def only_thanks(text):
    words = plain_words(text)
    return not words or all(w in THANKS_WORDS for w in words)


def is_attachment(m):
    return m['text'].startswith(ATTACHMENT)


def review(why, lines=()):
    """Cannot be judged from what we hold: not a deduction and not an exemption. A person looks at it."""
    return {'v': None, 'status': 'review', 'lines': [x for x in lines if x], 'd': {'why': why}}


def local_time(value):
    return dt.datetime.fromisoformat(value) if value else None


def stamps(chat, year):
    out, last, prev = [], None, None
    for m in chat['messages']:
        carried = not (m.get('date') or '').strip('?')
        day = (m.get('date') or '').strip('?') or last
        last = day
        try:
            if m.get('at'):           # the full local timestamp, year included: a chat across New Year keeps its order
                t = dt.datetime.fromisoformat(m['at']).replace(second=0, microsecond=0, tzinfo=None)
                out.append(t)
                prev, last = t, t.strftime('%m-%d')
                continue
            mo, da = map(int, day[-5:].split('-'))
            hh, mi = map(int, m['time'].split(':'))
            t = dt.datetime(year, mo, da, hh, mi)
            if carried and prev and t < prev:      # date missing and the clock went backwards: it is the next day
                t += dt.timedelta(days=1)
                last = t.strftime('%m-%d')
            out.append(t)
            prev = t
        except (AttributeError, ValueError):
            out.append(None)
    return out


def verify(chat, quote, line, role):
    """The quote must sit inside message[line] and, when a role is given, be said by that role."""
    msgs = chat['messages']
    if not quote or not isinstance(line, int) or not 1 <= line <= len(msgs):
        return None
    m = msgs[line - 1]
    if norm(quote) not in norm((m.get('reply_to') or '') + ' ' + m['text']):
        return None
    if role and m['role'] != role:
        return None
    return line


def hours(st, a, b):
    return (st[b - 1] - st[a - 1]).total_seconds() / 3600 if st[a - 1] and st[b - 1] else None


def age_h(st, line, obs):
    """Hours between message <line> and the observation end (None if undated)."""
    return (obs - st[line - 1]).total_seconds() / 3600 if st[line - 1] else None


def is_real_request(q):
    t = norm(re.sub(r'[^\w\s]', ' ', q or '')).replace('أ', 'ا').replace('إ', 'ا')
    if not t or OPENER.match(t):
        return False
    return any(w not in STOP_WORDS for w in t.split())


MASKS = ('[اسم العميل]', '[رقم]', '[لينك]', '[مرفق]')
BRAND_VARIANTS = ('ترفل جيت', 'ترافيل جيت', 'ترافل جيت', 'ترافل قيت')


def mask_artifact(q):
    """A q34 'spelling error' that is really our own masking ("بإذن [اسم العميل]") or a brand-name variant: ignore."""
    if not q:
        return False
    if any(m in q for m in MASKS):
        return True
    return norm(q) in BRAND_VARIANTS


def due_hours(q):
    if not q:
        return PROMISE_DEFAULT_DUE_H
    q = q.replace('أ', 'ا').replace('إ', 'ا')
    if re.search(r'دقيق|دقايق|لحظ|ثوان|نص ساع|نصف ساع', q):
        return 1
    m = re.search(r'(\d+)\s*ساع', q)
    if m:
        return int(m.group(1))
    if 'ساعتين' in q:
        return 2
    if 'ساع' in q:
        return 1
    if re.search(r'بكر|غد', q):
        return 36
    if re.search(r'اليوم|الليل|المسا', q):
        return 12
    if 'يومين' in q:
        return 48
    if 'اسبوع' in q:
        return 7 * 24
    return PROMISE_DEFAULT_DUE_H


def work_minutes(a, b):
    """Minutes between datetimes a and b that fall inside the working window (10:00-01:00). None if undated."""
    if a is None or b is None or b <= a:
        return 0 if a and b else None
    total, t = 0.0, a
    while t < b:
        day0 = dt.datetime(t.year, t.month, t.day)
        ws = day0 + dt.timedelta(hours=WORK_START)
        we = day0 + dt.timedelta(hours=WORK_END)
        tail = day0 + dt.timedelta(hours=WORK_END - 24)
        if t < tail:
            end = min(b, tail)
            total += (end - t).total_seconds() / 60
            t = end
            continue
        if t < ws:
            t = ws
            continue
        end = min(b, we)
        total += (end - t).total_seconds() / 60
        t = end
    return total


def decay(minutes, full, zero):
    if minutes is None:
        return None
    if minutes <= full:
        return 1.0
    if minutes >= zero:
        return 0.0
    return round(1 - (minutes - full) / (zero - full), 3)


def group_attempts(chat, st, lines):
    msgs = chat['messages']
    attempts = []
    for ln in sorted(set(lines)):
        if attempts:
            prev = attempts[-1]
            customer_between = any(msgs[i - 1]['role'] == 'customer' for i in range(prev + 1, ln))
            gap = hours(st, prev, ln)
            if not customer_between and gap is not None and gap < FOLLOWUP_GAP_H:
                continue
        attempts.append(ln)
    return attempts


# ---------------------------------------------------------------- one run
def score_run(chat, out, st, obs, reason=None, impatience=(), reason_ok=False):
    """Return {check: {'v': 0..1 | None, 'status': yes|no|partial|pending|na, 'lines': [...], 'd': {...}}} for one model run,
    plus facts the chat-level step needs (follow-up attempts, last-request status). reason = the loss reason from call 2."""
    msgs = chat['messages']
    n = len(msgs)
    v = lambda q, ln, role: verify(chat, q, ln, role)
    unverified = lambda q, ln: bool(q) and not v(q, ln, None)     # a quote that is not in that message proves nothing either way
    R = {}
    facts = {}

    ctype = out.get('chat_type', {}).get('type')
    # emojis, stickers and files only: the customer never said what they want, whatever the bot guessed
    if not any(plain_words(re.sub(r'\[[^\]]*\]', ' ', m['text'])) for m in msgs if m['role'] == 'customer'):
        ctype = 'other'
    sales = ctype == 'sales'
    sc = out.get('scope') or {}
    out_scope = sc.get('scope') == 'out' and bool(v(sc.get('scope_quote'), sc.get('scope_line'), 'customer'))
    facts['out_scope'] = out_scope
    was_sales = sales
    if out_scope:          # Umrah / Hajj, bus tickets, visas for nationalities we do not serve (a person reviews it, owner decision 2026-11-03)
        sales = False
    agent_lines = [i for i, m in enumerate(msgs, 1) if m['role'] == 'agent']
    first_agent = agent_lines[0] if agent_lines else None
    # the opening run ends at the first agent message that follows a customer message
    opening_end = next((i for i in agent_lines[1:] if any(msgs[j - 1]['role'] == 'customer' for j in range(first_agent + 1, i))), n + 1)
    last_customer = max((i for i, m in enumerate(msgs, 1) if m['role'] == 'customer'), default=0)
    attachment_after = lambda line: next((i for i in agent_lines if i > line and is_attachment(msgs[i - 1])), None)
    last_agent = agent_lines[-1] if agent_lines else None
    humans = [i for i, m in enumerate(msgs, 1) if m['role'] in ('agent', 'customer')]
    last_human = humans[-1] if humans else None

    b = out.get('booking', {})
    booking = {f: v(b.get(f + '_quote'), b.get(f + '_line'), 'customer') for f in FIELDS}
    bad_fields = {f for f in FIELDS if unverified(b.get(f + '_quote'), b.get(f + '_line'))}
    bad_fields |= {p.get('asks_for') for p in out.get('probes', []) if unverified(p.get('quote'), p.get('line'))}
    probes_all = [(v(p.get('quote'), p.get('line'), 'agent'), p.get('asks_for'), bool(p.get('refines'))) for p in out.get('probes', [])]
    probes_all = [(ln, f, rf) for ln, f, rf in probes_all if ln and f in FIELDS]
    probes = [(ln, f) for ln, f, rf in probes_all]
    facts['type'] = ctype
    facts['booking'] = booking

    # 2 offer (needed by the probe rule: an offer sent without asking for a detail the customer never gave means
    # the agent had it from outside the chat, e.g. the hidden website form)
    o = out.get('offer', {})
    offer = v(o.get('offer_quote'), o.get('offer_line'), 'agent')

    # 1a probe: fields missing when the agent joined
    if sales and first_agent:
        asked = {f for ln, f in probes}
        form_first = msgs[0]['role'] == 'system'          # the website form arrived as a hidden system line: the agent had its details
        missing = [f for f in FIELDS if not (booking[f] and booking[f] < first_agent)
                   and not (form_first and offer and f not in asked and not booking[f])]
        if missing:
            sat, ev = [], []
            for f in missing:
                if booking[f] and booking[f] < opening_end:      # customer volunteered it right after the greeting
                    sat.append(f); ev.append(booking[f])
                elif any(g == f and not (booking[f] and booking[f] < ln) for ln, g in probes):   # a question after the answer earns nothing
                    sat.append(f); ev += [ln for ln, g in probes if g == f][:1]
            val = len(sat) / len(missing)
            R['probe'] = {'v': val, 'status': 'yes' if val == 1 else ('partial' if val > 0 else 'no'),
                          'lines': sorted(set(ev)), 'd': {'missing': missing, 'satisfied': sat}}
            if any(f in bad_fields for f in missing if f not in sat):
                R['probe'] = review('unverified_quote')
        else:
            R['probe'] = {'v': None, 'status': 'na', 'lines': [], 'd': {'why': 'all_given'}}
    # 1b noask: asked for something already given
    re_asked = []
    if sales and probes:
        known = {f: booking[f] for f in FIELDS if booking[f]}
        # a precision question is not a re-ask: the model flags it (refines); the code also recognises an approximate date
        # ("بداية الشهر", "الشهر الجاي", a weekday only) and a question about the return date when only the departure was given
        approx_date = not re.search(r'\d|[٠-٩]|يناير|فبراير|مارس|ابريل|أبريل|مايو|يونيو|يوليو|اغسطس|أغسطس|سبتمبر|اكتوبر|أكتوبر|نوفمبر|ديسمبر|محرم|صفر|ربيع|جمادى|رجب|شعبان|رمضان|شوال|ذو', b.get('date_quote') or '')
        re_asked = [(ln, f) for ln, f, rf in probes_all if f in known and ln > known[f] and not rf
                    and not (f == 'destination' and REFINES_PLACE.search(msgs[ln - 1]['text']))
                    and not (f == 'date' and (approx_date or re.search(r'العود|الرجوع|رجوع|عوده|عودة', msgs[ln - 1]['text'])))]
        if known:
            R['noask'] = {'v': 0.0 if re_asked else 1.0, 'status': 'no' if re_asked else 'yes',
                          'lines': sorted({x for ln, f in re_asked for x in (known[f], ln)}), 'd': {'re_asked': re_asked}}
    elif sales and any(booking.values()):
        R['noask'] = {'v': 1.0, 'status': 'yes', 'lines': [], 'd': {}}

    # 2 offer
    transferred = v(out.get('q18', {}).get('answer_quote'), out.get('q18', {}).get('answer_line'), 'agent')
    probe_lines = {ln for ln, f in probes}
    back_lines = {v(it.get('asked_back_quote'), it.get('asked_back_line'), 'agent') for it in out.get('requests', [])} - {None}
    # any agent question the customer never answered suspends the offer and the requests: the agent may ask, then chase
    open_questions = [i for i in agent_lines if i > last_customer and i not in {ln for ln, f in re_asked}
                      and (i in probe_lines or i in back_lines or QUESTION.search(msgs[i - 1]['text']))]
    waiting = not offer and bool(open_questions)
    qualified = sales and all(booking.values())
    facts.update(offer=offer, transferred=transferred, waiting=bool(waiting), qualified=qualified)

    def transfer_accepted(line):
        """The other department is seen to reply in this chat within 4 working hours."""
        who = msgs[line - 1].get('sender')
        for i in agent_lines:
            if i > line and msgs[i - 1].get('sender') not in (None, who):
                wm = work_minutes(st[line - 1], st[i - 1])
                return wm is not None and wm <= TRANSFER_ACCEPT_MIN
        return False
    transfer_ok = bool(transferred) and transfer_accepted(transferred)

    if out_scope and was_sales and all(booking.values()) and not offer:
        R['offer'] = review('out_scope')
    elif qualified and transferred and not offer and not transfer_ok:
        R['offer'] = review('transfer_unconfirmed', [transferred])
    if qualified and not transferred and not o.get('attachment_unclear'):
        if offer:
            R['offer'] = {'v': 1.0, 'status': 'yes', 'lines': [offer], 'd': {}}
        elif unverified(o.get('offer_quote'), o.get('offer_line')):
            R['offer'] = review('unverified_quote')
        elif reason in UNAVAILABLE_REASONS:       # owner decision 2026-11-04: "not available" is never taken on the agent's word
            R['offer'] = review('exemption_unverified')
        elif waiting:
            R['offer'] = {'v': None, 'status': 'na', 'lines': [last_agent], 'd': {'why': 'waiting'}}
        else:
            qual_line = max(booking.values())
            age = age_h(st, qual_line, obs)
            if age is not None and age < ANSWER_DUE_H:
                R['offer'] = {'v': None, 'status': 'pending', 'lines': [qual_line], 'd': {}}
            elif attachment_after(qual_line):     # the decisive evidence may be inside a file we cannot read
                R['offer'] = review('attachment', [qual_line, attachment_after(qual_line)])
            else:
                R['offer'] = {'v': 0.0, 'status': 'no', 'lines': [qual_line], 'd': {}}
    elif qualified and o.get('attachment_unclear'):
        R['offer'] = review('attachment')
    if offer:
        mc = v(o.get('mismatch_customer_quote'), o.get('mismatch_customer_line'), 'customer')
        ma = v(o.get('mismatch_agent_quote'), o.get('mismatch_agent_line'), 'agent')
        if ma and mc:
            R['match'] = {'v': 0.0, 'status': 'no', 'lines': [mc, ma], 'd': {}}
        else:
            R['match'] = {'v': 1.0, 'status': 'yes', 'lines': [offer], 'd': {}}

    # 5 requests (needed before follow-up: decides "agent never replied")
    reqs = {}
    for it in out.get('requests', []):
        src = v(it.get('source_quote'), it.get('line'), 'customer')
        if not src or src in impatience or not is_real_request(it.get('source_quote')):
            continue
        if not ASKS.search(it.get('source_quote')) and (src in booking.values() or len(plain_words(it.get('source_quote'))) <= 3):
            continue              # the customer gave a detail ("موريشيوس", "من جدة"), which asks for nothing
        root = it.get('repeat_of') if isinstance(it.get('repeat_of'), int) and it.get('repeat_of') in reqs else src
        ans = v(it.get('answer_quote'), it.get('answer_line'), 'agent')
        back = v(it.get('asked_back_quote'), it.get('asked_back_line'), 'agent')
        r = reqs.setdefault(root, {'line': root, 'quote': it.get('source_quote'), 'answer': None, 'back': None, 'last': root, 'unknown': False, 'bot': None})
        r['unknown'] = r['unknown'] or unverified(it.get('answer_quote'), it.get('answer_line'))
        bot = v(it.get('bot_answer_quote'), it.get('bot_answer_line'), 'bot')
        r['bot'] = r['bot'] or (bot if bot and first_agent and bot < first_agent else None)
        r['answer'] = r['answer'] or (ans if ans and ans > root else None)
        r['back'] = r['back'] or (back if back and back > root else None)
        r['last'] = max(r['last'], src)
    answered, unanswered, awaiting, pending, handed, unknown = [], [], [], [], [], []
    for r in reqs.values():
        if r['answer']:
            answered.append(r)
        elif r['bot']:
            continue                      # the bot answered before the agent joined: nothing to charge
        elif transferred and transferred > r['line']:
            if not transfer_ok:           # a handover is not an answer; a person checks whether the other department replied
                handed.append(r)
        elif r['back'] and r['back'] not in {ln for ln, f in re_asked}:
            awaiting.append(r)            # the agent asked for something needed first: not an answer, not a failure
            # (a form that re-asks details the customer already gave is NOT a legitimate ask-back: the request stays unanswered)
        else:
            age = age_h(st, r['last'], obs)
            if age is not None and age < ANSWER_DUE_H:
                pending.append(r)
            elif open_questions:
                awaiting.append(r)        # the agent's last word was a question the customer never answered
            elif r['unknown'] or attachment_after(r['last']):
                unknown.append(r)         # a failed quote, or a file that may hold the answer
            else:
                unanswered.append(r)
    judged = answered + unanswered
    if judged:
        val = len(answered) / len(judged)
        R['answered'] = {'v': val, 'status': 'yes' if val == 1 else ('partial' if val > 0 else 'no'),
                         'lines': [r['line'] for r in unanswered] + ([r['answer'] for r in answered][:1] if not unanswered else []),
                         'd': {'answered': len(answered), 'unanswered': [r['line'] for r in unanswered], 'awaiting': [r['line'] for r in awaiting]}}
    elif pending:
        R['answered'] = {'v': None, 'status': 'pending', 'lines': [r['line'] for r in pending], 'd': {}}
    elif handed or unknown:
        R['answered'] = review('transfer_unconfirmed' if handed else 'unverified_or_attachment', [r['line'] for r in handed + unknown])
    elif awaiting:
        R['answered'] = {'v': None, 'status': 'na', 'lines': [r['back'] for r in awaiting], 'd': {'why': 'awaiting'}}
    facts['agent_never_replied'] = bool(last_human and msgs[last_human - 1]['role'] == 'customer'
                                        and any(r['last'] == last_human or r['line'] == last_human for r in unanswered))

    # 3 follow-up. Silence start = the offer; else the last agent message that is not itself a follow-up
    # (the question or answer the customer never replied to); else nothing (the customer spoke last -> "answered" covers it).
    fus = sorted({v(x.get('quote'), x.get('line'), 'agent') for x in out.get('followup', [])} - {None})
    if offer:
        start = offer
    elif fus:
        start = max((i for i in agent_lines if i < fus[0]), default=None) or fus[0]
    elif last_human and msgs[last_human - 1]['role'] == 'agent':
        start = last_agent
    else:
        start = None
    # the conversation went on after that message, so the silence starts later
    last_talk = max((i for i, m in enumerate(msgs, 1) if m['role'] == 'customer' and not only_thanks(m['text'])), default=0)     # thanks or a sticker does not restart the chase
    after_customer = [i for i in agent_lines if i > last_talk]
    if start and last_talk > start and after_customer:
        start = after_customer[0]
    closure = out.get('closure') or {}
    customer_after = lambda line: [i for i in range(line + 1, n + 1) if msgs[i - 1]['role'] == 'customer']
    closed = v(closure.get('closed_quote'), closure.get('closed_line'), 'customer')
    if closed and (only_thanks(closure.get('closed_quote')) or any(is_real_request(msgs[i - 1]['text']) and not only_thanks(msgs[i - 1]['text']) for i in customer_after(closed))):
        closed = None             # thanks alone does not close, and a customer who went on asking did not close
    next_step = v(closure.get('next_step_quote'), closure.get('next_step_line'), 'customer')
    if next_step and any(not only_thanks(msgs[i - 1]['text']) for i in customer_after(next_step)):
        next_step = None
    if next_step:
        start = next_step         # an agreed time reschedules the obligation, it does not erase it
    # the start message seeds the grouping: a message less than 4 h after the unanswered question (no customer reply between)
    # is a continuation of it, not a first follow-up attempt
    attempts = [ln for ln in group_attempts(chat, st, [start] + [ln for ln in fus if ln > start]) if ln != start] if start else []
    facts['attempts'] = attempts
    facts['silence_start'] = start
    silence_age = age_h(st, start, obs) if start else None
    if next_step:
        silence_age = silence_age - due_hours(closure.get('next_step_quote')) if silence_age is not None else None
    R['followup'] = {'v': None, 'status': 'na', 'lines': [], 'd': {'attempts': len(attempts), 'start': start}}
    phone_after = lambda line: st[line - 1] is not None and (
        any(CALL_WORDS.search(m['text']) for m in msgs[line:] if m['role'] in ('agent', 'customer'))
        or any(local_time(c) >= st[line - 1] for c in chat.get('calls', [])))
    if out_scope:
        R['followup'] = review('out_scope')
        R['followup']['d']['attempts'] = len(attempts)
    elif transferred:          # the sender keeps the obligation until the other department is seen to reply
        R['followup'] = {'v': None, 'status': 'na', 'lines': [transferred], 'd': {'why': 'transfer_accepted', 'attempts': len(attempts)}} if transfer_ok \
            else review('transfer_unconfirmed', [transferred])
        R['followup']['d']['attempts'] = len(attempts)
    elif closed:
        R['followup'] = {'v': None, 'status': 'na', 'lines': [closed], 'd': {'why': 'customer_closed', 'attempts': len(attempts)}}
    elif start and not (last_human and msgs[last_human - 1]['role'] == 'customer' and not next_step and facts['agent_never_replied']):
        if len(attempts) >= FOLLOWUP_REQUIRED:
            R['followup'] = {'v': 1.0, 'status': 'yes', 'lines': attempts, 'd': {'attempts': len(attempts), 'start': start}}
        elif silence_age is not None and silence_age < FOLLOWUP_WINDOW_H:
            R['followup'] = {'v': None, 'status': 'pending', 'lines': [start], 'd': {'attempts': len(attempts), 'start': start}}
        elif any(unverified(x.get('quote'), x.get('line')) for x in out.get('followup', [])):
            R['followup'] = review('unverified_quote', [start])
            R['followup']['d'].update(attempts=len(attempts), start=start)
        elif phone_after(start):   # the follow-up may have happened by phone, which the chat cannot show
            R['followup'] = review('phone_possible', [start])
            R['followup']['d'].update(attempts=len(attempts), start=start)
        elif attempts:             # each attempt earns its share of the three; the loss label stays "No follow-up"
            R['followup'] = {'v': round(len(attempts) / FOLLOWUP_REQUIRED, 3), 'status': 'partial', 'lines': [start] + attempts, 'd': {'attempts': len(attempts), 'start': start}}
        else:
            R['followup'] = {'v': 0.0, 'status': 'no', 'lines': [start], 'd': {'attempts': 0, 'start': start}}
    # (the chat-level step keeps follow-up only when the loss reason is a silent customer)

    # 4 promises
    kept, failed, pend, na, unsure = [], [], [], [], []
    seen = {}
    for p in out.get('promises', []):
        ln = v(p.get('promise_quote'), p.get('line'), 'agent')
        if not ln or ln == transferred:     # "the department will call you" is a handover, not this agent's promise
            continue
        root = p.get('repeat_of') if isinstance(p.get('repeat_of'), int) and p.get('repeat_of') in seen else ln
        e = seen.setdefault(root, {'line': root, 'delivery': None, 'closed': None, 'cond': p.get('promise_condition'),
                                   'cond_met': None, 'due_h': due_hours(p.get('due_quote')), 'unknown': False})
        e['unknown'] = e['unknown'] or unverified(p.get('delivery_quote'), p.get('delivery_line'))
        e['delivery'] = e['delivery'] or v(p.get('delivery_quote'), p.get('delivery_line'), 'agent')
        e['closed'] = e['closed'] or v(p.get('closed_quote'), p.get('closed_line'), 'customer')
        e['cond_met'] = e['cond_met'] or v(p.get('condition_met_quote'), p.get('condition_met_line'), None)
    for e in seen.values():
        ln = e['line']
        if e['delivery'] and e['delivery'] > ln:
            kept.append(e); continue
        if e['cond'] and not e['cond_met']:
            na.append(e); continue
        due_from = e['cond_met'] if e['cond'] else ln
        if e['closed'] and e['closed'] > ln:
            gap = hours(st, due_from, e['closed'])
            if gap is None or gap <= e['due_h']:
                na.append(e); continue
        age = age_h(st, due_from, obs)
        if age is not None and age < e['due_h']:
            pend.append(e)
        elif e['unknown'] or attachment_after(due_from):
            unsure.append(e)
        else:
            failed.append(e)
    if kept or failed:
        val = len(kept) / (len(kept) + len(failed))
        R['promise'] = {'v': val, 'status': 'yes' if val == 1 else ('partial' if val > 0 else 'no'),
                        'lines': sorted({x for e in failed for x in (e['line'],)} | {x for e in kept for x in (e['line'], e['delivery'])}),
                        'd': {'kept': len(kept), 'failed': [e['line'] for e in failed]}}
    elif pend:
        R['promise'] = {'v': None, 'status': 'pending', 'lines': [e['line'] for e in pend], 'd': {}}
    elif unsure:
        R['promise'] = review('unverified_or_attachment', [e['line'] for e in unsure])
    elif na:
        R['promise'] = {'v': None, 'status': 'na', 'lines': [e['line'] for e in na], 'd': {'why': 'no_chance'}}

    # 6 objections -> episodes
    objs = []
    first_file = next((i for i in agent_lines if is_attachment(msgs[i - 1])), None)
    quoted_at = min([x for x in (offer, first_file) if x], default=None)
    for it in out.get('objections', []):
        src = v(it.get('source_quote'), it.get('line'), 'customer')
        if not src:
            continue
        if it.get('objection_type') in ('price', 'competitor', 'product') and not (quoted_at and src > quoted_at):
            continue              # nothing was offered yet, so this is a request, not an objection
        el = {k: v(it.get(k + '_quote'), it.get(k + '_line'), 'agent') for k in ELEMENTS}
        objs.append({'line': src, 'type': it.get('objection_type'), 'el': el,
                     'unknown': any(unverified(it.get(k + '_quote'), it.get(k + '_line')) for k in ELEMENTS)})
    objs.sort(key=lambda x: x['line'])
    episodes = []
    for ob in objs:
        if episodes and not any(msgs[i - 1]['role'] == 'agent' for i in range(episodes[-1]['lines'][-1] + 1, ob['line'])):
            ep = episodes[-1]
            ep['lines'].append(ob['line']); ep['types'].add(ob['type'])
            for k in ELEMENTS:
                ep['el'][k] = ep['el'][k] or ob['el'][k]
        else:
            episodes.append({'lines': [ob['line']], 'types': {ob['type']}, 'el': dict(ob['el']), 'unknown': ob['unknown']})
    vals, lines, pend_ep, unsure_ep = [], [], 0, []
    for ep in episodes:
        el = {k: ep['el'][k] for k in ELEMENTS}
        if not any(el.values()):
            age = age_h(st, ep['lines'][-1], obs)
            if ep['lines'][-1] >= (last_agent or 0) and age is not None and age < OBJECTION_DUE_H:
                pend_ep += 1; continue
            if ep['unknown']:
                unsure_ep += ep['lines']; continue
            vals.append(0.0)
        else:
            vals.append(1.0 if any(t in GOOD and GOOD[t](el) for t in ep['types']) else 0.5)
        lines += ep['lines'] + [x for x in el.values() if x]
    if vals:
        val = sum(vals) / len(vals)
        R['objection'] = {'v': val, 'status': 'yes' if val == 1 else ('partial' if val > 0 else 'no'),
                          'lines': sorted(set(lines)), 'd': {'episodes': len(vals), 'good': sum(1 for x in vals if x == 1), 'weak': sum(1 for x in vals if x == .5)}}
    elif pend_ep:
        R['objection'] = {'v': None, 'status': 'pending', 'lines': [], 'd': {}}
    elif unsure_ep:
        R['objection'] = review('unverified_quote', unsure_ep)

    # 8 response time (code only, working minutes 10:00-01:00)
    if first_agent:
        before = [i for i in range(1, first_agent) if msgs[i - 1]['role'] == 'customer']
        if before:
            # routing and bot time is the queue's, not the agent's: the clock starts when the deal reached this agent
            began, queue = st[before[-1] - 1], None
            assigned = local_time(chat.get('assigned_at'))
            if assigned and began and st[first_agent - 1] and assigned > began:
                queue = work_minutes(began, min(assigned, st[first_agent - 1]))
                began = min(assigned, st[first_agent - 1])
            wm = work_minutes(began, st[first_agent - 1])
            val = decay(wm, *SPEED_FIRST)
            if val is not None:
                R['speed_first'] = {'v': val, 'status': 'yes' if val == 1 else ('no' if val == 0 else 'partial'),
                                    'lines': [before[-1], first_agent], 'd': {'minutes': round(wm), 'queue_minutes': round(queue) if queue is not None else None}}
        worst, worst_pair = None, None
        waiting_lines = {r['line'] for r in reqs.values()} | {r['last'] for r in reqs.values()} | {ob['line'] for ob in objs}
        for i in range(first_agent + 1, n + 1):
            # only a customer message that asks for something (a verified request / objection, or a question mark) starts a timed wait
            if msgs[i - 1]['role'] != 'customer':
                continue
            prev = next((j for j in range(i - 1, 0, -1) if msgs[j - 1]['role'] in ('agent', 'customer')), None)
            answers_agent = prev and msgs[prev - 1]['role'] == 'agent' and QUESTION.search(msgs[prev - 1]['text'])   # an answer to the agent's question waits for a reply too
            if not answers_agent and (not (i in waiting_lines or re.search('[؟?]', msgs[i - 1]['text'])) or not is_real_request(msgs[i - 1]['text'])):
                continue
            nxt, greeted = None, False
            for j in range(i + 1, n + 1):
                role = msgs[j - 1]['role']
                if role == 'agent' and is_greeting(msgs[j - 1]['text']):
                    greeted = True        # a bare greeting does not end the wait
                elif role == 'agent':
                    nxt = j
                    break
                elif role == 'customer' and not greeted:
                    break
            if nxt:
                wm = work_minutes(st[i - 1], st[nxt - 1])
                if wm is not None and (worst is None or wm > worst):
                    worst, worst_pair = wm, [i, nxt]
        if worst is not None:
            val = decay(worst, *SPEED_NEXT)
            R['speed_next'] = {'v': val, 'status': 'yes' if val == 1 else ('no' if val == 0 else 'partial'),
                               'lines': worst_pair, 'd': {'minutes': round(worst)}}
    elif sales and any(m['role'] == 'customer' for m in msgs):
        R['speed_first'] = {'v': 0.0, 'status': 'no', 'lines': [], 'd': {'minutes': None}}

    # 7 + 9 violations (only when the agent wrote something)
    if first_agent:
        wrong = []
        for k in ('q26', 'q36'):
            a = v(out.get(k, {}).get('quote_a'), out.get(k, {}).get('line_a'), None)
            if a:
                wrong += [x for x in (a, v(out.get(k, {}).get('quote_b'), out.get(k, {}).get('line_b'), None)) if x]
        R['correct'] = {'v': 0.0 if wrong else 1.0, 'status': 'no' if wrong else 'yes', 'lines': wrong, 'd': {}}
        for key, qs in (('polite', ('q13', 'q16')), ('positive', ('q15',)), ('spelling', ('q34',))):
            bad = [x for x in (v(out.get(q, {}).get('violation_quote'), out.get(q, {}).get('violation_line'), 'agent') for q in qs
                               if not (key == 'spelling' and mask_artifact(out.get(q, {}).get('violation_quote')))) if x]
            R[key] = {'v': 0.0 if bad else 1.0, 'status': 'no' if bad else 'yes', 'lines': bad, 'd': {}}
    return R, facts


# ---------------------------------------------------------------- combine runs
def combine(runs):
    """One algorithm for all checks: applicability by majority of runs; value = median of the runs where it applied;
    evidence from the median run. Violation checks fail only when every run flags them; a split vote -> review."""
    k = len(runs)
    final = {}
    for key in WEIGHTS:
        rs = [r[key] for r in runs if key in r]
        if len(rs) * 2 <= k:
            continue
        if key in VIOLATION_CHECKS:
            flags = sum(1 for r in rs if r['v'] == 0.0)
            if flags:                 # any flag goes to a person; nothing is deducted automatically
                base = next(r for r in rs if r['v'] == 0.0)
                final[key] = {'v': None, 'status': 'review', 'lines': base['lines'], 'd': {'why': 'violation', 'votes': f'{flags}/{len(rs)}'}}
            else:
                final[key] = next(r for r in rs if r['v'] == 1.0)
            continue
        statuses = collections.Counter(r['status'] for r in rs)
        top = statuses.most_common(1)[0][0]
        if top in ('na', 'pending', 'review') and statuses[top] * 2 > len(rs):
            final[key] = next(r for r in rs if r['status'] == top)
            continue
        scored = [r for r in rs if r['v'] is not None]
        if not scored:
            final[key] = rs[0]
            continue
        vals = sorted(r['v'] for r in scored)
        if vals[-1] - vals[0] >= 0.5:     # three readings by one model share their mistakes, so a real split goes to a person
            low = next(r for r in scored if r['v'] == vals[0])
            final[key] = {'v': None, 'status': 'review', 'lines': low['lines'], 'd': {'why': 'votes_disagree', 'attempts': low['d'].get('attempts'), 'start': low['d'].get('start')}}
            continue
        med = vals[len(vals) // 2]
        final[key] = next(r for r in scored if r['v'] == med)
    return final


def median_int(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def score_chat(chat, votes, al, obs, year, second=None, jev=None):
    st = stamps(chat, year)
    lr = (al or {}).get('loss_reason') or {}
    q41_reason = lr.get('reason')
    impatience = {a.get('line_a') for a in (al or {}).get('alerts') or []
                  if a.get('kind') == 'impatience' and verify(chat, a.get('quote_a'), a.get('line_a'), 'customer')}
    # a stated reason exempts only with the customer's own words (owner decision 2026-11-04: not the agent's "not available")
    reason_ok = bool(verify(chat, lr.get('reason_quote'), lr.get('reason_line'), 'customer'))
    runs, facts = zip(*(score_run(chat, out, st, obs, q41_reason, impatience, reason_ok) for out in votes))
    checks = combine(list(runs))
    # second opinion (the Sonnet run of the same prompt): a violation it finds that the flash votes missed -> review, not fail
    if second:
        R2, _ = score_run(chat, second, st, obs, q41_reason)
        for key in VIOLATION_CHECKS + ('match',):
            if key in R2 and R2[key]['v'] == 0.0 and checks.get(key, {}).get('status') == 'yes':
                checks[key] = {'v': None, 'status': 'review', 'lines': R2[key]['lines'], 'd': {'votes': 'sonnet'}}
        sp = checks.get('spelling')
        if sp and sp['status'] == 'review' and sp['d'].get('votes', '').endswith('/3') and 'spelling' in R2 and R2['spelling']['v'] == 0.0:
            checks['spelling'] = {'v': 0.0, 'status': 'no', 'lines': sorted(set(sp['lines']) | set(R2['spelling']['lines'])), 'd': {'votes': 'flash 3/3 + sonnet'}}
    attempts = median_int([len(f['attempts']) for f in facts])
    never = sum(1 for f in facts if f['agent_never_replied']) * 2 > len(facts)
    ctype = collections.Counter(f['type'] for f in facts).most_common(1)[0][0]
    out_scope = sum(1 for f in facts if f['out_scope']) * 2 > len(facts)
    transferred = sum(1 for f in facts if f['transferred']) * 2 > len(facts)
    types = collections.Counter(f['type'] for f in facts)
    other_kinds = collections.Counter((o.get('chat_type') or {}).get('other_kind') for o in votes if (o.get('chat_type') or {}).get('type') == 'other')
    closed_by_customer = (checks.get('followup') or {}).get('d', {}).get('why') == 'customer_closed'

    # loss reason: call 2's label; a silent customer is split by the follow-up rule; out-of-scope requests are their own label (Bitrix: Other);
    # an after-sales chat is about an existing booking, so a silent customer there is not a lost sale
    reason = q41_reason
    if out_scope:
        reason = 'Out of scope'
    elif ctype == 'after_sales' and reason in ('No Response', None):
        reason = 'Booked'
    elif ctype == 'other' and reason in ('No Response', 'No follow-up', 'No Response (agent)', None):
        reason = 'Other'
    elif transferred and reason in ('No Response', 'No follow-up', 'No Response (agent)'):
        reason = 'Other'          # a chat handed to another department is not "No follow-up"
    elif closed_by_customer and reason in ('No Response', 'No follow-up', 'No Response (agent)'):
        reason = 'Customer changed mind'
    elif reason in ('No Response', 'No follow-up', 'No Response (agent)'):
        reason = 'No Response (agent)' if never else ('No Response' if attempts >= FOLLOWUP_REQUIRED else 'No follow-up')
    # follow-up is judged only when the customer went silent without a stated reason
    if reason not in ('No Response', 'No follow-up') and 'followup' in checks and checks['followup']['status'] in ('yes', 'no', 'partial', 'pending'):
        if checks['followup']['status'] in ('no', 'partial') and not reason_ok and ctype != 'after_sales':
            checks['followup'] = review('exemption_unverified', checks['followup']['lines'])     # a label with no quote cannot exempt 15 points
        else:
            checks['followup'] = {'v': None, 'status': 'na', 'lines': [], 'd': {'why': 'reason_stated', 'attempts': attempts}}
    if 'followup' in checks and checks['followup']['status'] == 'review':
        checks['followup']['d']['attempts'] = attempts
    # a job seeker or a supplier is not a real ask: not counted on the agent, a title is all it gets (owner decision 2026-11-01)
    title = None
    if ctype == 'other':
        title = OTHER_TITLES.get(other_kinds.most_common(1)[0][0] if other_kinds else None, OTHER_TITLES['no_request'])
        checks = {}
    if 'followup' in checks and checks['followup']['status'] == 'na' and not checks['followup']['d'].get('why'):
        del checks['followup']

    alerts = []
    first_agent = next((i for i, m in enumerate(chat['messages'], 1) if m['role'] == 'agent'), None)
    seen = set()
    for a in (al or {}).get('alerts') or []:
        kind = a.get('kind')
        if kind not in ALERT_KINDS or (kind, a.get('line_a')) in seen:
            continue
        if kind == 'impatience' and ('complaint', a.get('line_a')) in {(x.get('kind'), x.get('line_a')) for x in al['alerts']}:
            continue              # one message, one alert
        if not verify(chat, a.get('quote_a'), a.get('line_a'), 'customer' if kind in CUSTOMER_ALERTS else 'agent'):
            continue
        lines = [a['line_a']]
        if a.get('quote_b') and verify(chat, a['quote_b'], a.get('line_b'), ALERT_B_ROLE.get(kind, 'agent')):
            lines.append(a['line_b'])
        elif kind in TWO_MESSAGE_ALERTS:
            continue                      # both halves must be verbatim
        seen.add((kind, a['line_a']))
        rec = {'kind': kind, 'lines': lines, 'why': a.get('why_ar')}
        if kind in CUSTOMER_ALERTS:
            rec['before_agent'] = first_agent is None or a['line_a'] < first_agent
        alerts.append(rec)
    # jev per-message signals: same kinds, merged by (kind, line); a form counts only if the customer had already given a detail
    given = [ln for f in facts for ln in f['booking'].values() if ln]
    first_given = min(given) if given else None
    for row in jev or []:
        for sig, kind in JEV_KINDS.items():
            if row.get(sig, 0) < JEV_SURE or (kind, row['line']) in seen:
                continue
            if kind == 'form_repeat' and not (first_given and first_given < row['line']):
                continue
            # jev sees one message at a time: a "no" or "go elsewhere" that the agent continues in the very next message
            # (an alternative, a question) is not "with no alternative" (C37 [14]->[15], C39 [20]->[21])
            if kind in ('sent_away', 'no_alternative') and row['line'] < len(chat['messages']) and chat['messages'][row['line']]['role'] == 'agent':
                continue
            seen.add((kind, row['line']))
            rec = {'kind': kind, 'lines': [row['line']], 'src': 'jev'}
            if kind == 'form_repeat':
                rec['lines'].append(first_given)
            if kind in CUSTOMER_ALERTS:
                rec['before_agent'] = first_agent is None or row['line'] < first_agent
            alerts.append(rec)
    alerts.sort(key=lambda a: (a['lines'][0] if a.get('lines') else 0))
    # the alerts call saw a wrong-customer offer or a wrong sum and the check said yes: a person decides
    if ctype != 'other':
        for a in alerts:
            k = {'wrong_customer': 'match', 'calc_error': 'correct'}.get(a['kind'])
            if k and checks.get(k, {}).get('status', 'yes') == 'yes':
                checks[k] = review('alert_' + a['kind'], a.get('lines', []))
    if ctype == 'other' and len(types) > 1:
        alerts.append({'kind': 'type_disputed'})
    for k, c in checks.items():
        if c['status'] == 'review':
            alerts.append({'kind': 'review', 'check': k, 'lines': c['lines'], 'votes': c['d'].get('votes'), 'review_why': c['d'].get('why')})
    applicable = {k: c for k, c in checks.items() if c['v'] is not None}
    total = sum(WEIGHTS[k] for k in applicable)
    earned = sum(WEIGHTS[k] * c['v'] for k, c in applicable.items())
    score = round(100 * earned / total) if total else None
    if any(f['offer'] is None and f['qualified'] for f in facts) and 'offer' in checks and checks['offer']['d'].get('why') == 'attachment_unclear':
        alerts.append({'kind': 'attachment_unclear'})
    if ctype == 'sales' and not any(m['role'] == 'agent' for m in chat['messages']):
        alerts.append({'kind': 'no_agent'})

    return {'agent': chat.get('employee'), 'agents': chat.get('agents') or [chat.get('employee')], 'attribution': chat.get('attribution', 'single'),
            'outcome': chat.get('outcome'), 'title': title, 'input_hash': chat.get('input_hash'),
            'date': chat.get('date'), 'type': ctype, 'scope': 'out' if out_scope else 'in', 'score': score, 'earned': round(earned, 2),
            'total': total, 'thin': total <= THIN_WEIGHT, 'checks': checks, 'loss_reason': reason,
            'reason_line': lr.get('reason_line') if verify(chat, lr.get('reason_quote'), lr.get('reason_line'), None) else None,
            'followups': attempts, 'alerts': alerts}


def load_alerts(path):
    out = {}
    if path and os.path.isdir(path):
        for f in os.listdir(path):
            d = json.load(open(os.path.join(path, f), encoding='utf-8'))
            out[f[:-5]] = d.get('out') or d
    return out


def main():
    args = sys.argv[1:]
    obs = dt.datetime.fromisoformat(args[args.index('--obs') + 1]) if '--obs' in args else dt.datetime.now()
    year = int(args[args.index('--year') + 1]) if '--year' in args else obs.year
    pos = [a for i, a in enumerate(args) if not a.startswith('--') and (i == 0 or not args[i - 1].startswith('--'))]
    chats_path, votes_dir = pos[:2]
    alerts_dir = pos[2] if len(pos) > 2 else None
    second_dir = args[args.index('--second') + 1] if '--second' in args else None
    chats = json.load(open(chats_path, encoding='utf-8'))
    alerts = load_alerts(alerts_dir)
    jev = collections.defaultdict(list)
    if '--jev' in args:
        for l in open(args[args.index('--jev') + 1], encoding='utf-8'):
            row = json.loads(l)
            jev[row['cid']].append(row)
    results = {}
    for chat in chats:
        cid = chat['id']
        if excluded(chat.get('employee')):
            continue
        votes = []
        for k in range(9):   # every vote present for this chat (3 by default; 5 for the stability set)
            p = f'{votes_dir}/{cid}_{k}.json'
            if os.path.exists(p):
                votes.append(json.load(open(p, encoding='utf-8'))['out'])
        if not votes:
            continue
        second = None
        if second_dir and os.path.exists(f'{second_dir}/{cid}.json'):
            second = json.load(open(f'{second_dir}/{cid}.json', encoding='utf-8'))['out']
        results[cid] = score_chat(chat, votes, alerts.get(cid), obs, year, second, jev.get(cid))
    agents = {}
    by = collections.defaultdict(list)
    for cid, r in results.items():
        if r['score'] is not None and r['attribution'] == 'single':
            by[r['agent']].append(r)
    for a, rs in by.items():
        tot = sum(r['total'] for r in rs)
        earned = sum(r['earned'] for r in rs)
        agents[a] = {'chats': len(rs), 'points': tot, 'score': round(100 * earned / tot) if tot else None,
                     'mean_of_chats': round(sum(r['score'] for r in rs if r['score'] is not None) / max(1, sum(1 for r in rs if r['score'] is not None))),
                     'coloured': len(rs) >= MIN_CHATS_FOR_COLOUR, 'thin_chats': sum(1 for r in rs if r['thin'])}
    json.dump({'obs_end': obs.isoformat(), 'chats': results, 'agents': agents, 'rubric': RUBRIC,
               'params': {'FOLLOWUP_GAP_H': FOLLOWUP_GAP_H, 'FOLLOWUP_WINDOW_H': FOLLOWUP_WINDOW_H, 'ANSWER_DUE_H': ANSWER_DUE_H,
                          'PROMISE_DEFAULT_DUE_H': PROMISE_DEFAULT_DUE_H, 'THIN_WEIGHT': THIN_WEIGHT, 'MIN_CHATS_FOR_COLOUR': MIN_CHATS_FOR_COLOUR}},
              sys.stdout, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
