"""Shared by the judge run and the scorer: one rendering, one normaliser."""
import re
from datetime import datetime, timedelta, timezone

RIYADH = timezone(timedelta(hours=3))
BUSINESS_START, BUSINESS_END = 9, 21  # hours, Riyadh

ROLE_AR = {'customer': 'العميل', 'agent': 'الموظف', 'bot': 'البوت', 'system': 'نظام'}
TYPE_AR = {'audio': '[رسالة صوتية]', 'image': '[صورة]', 'document': '[ملف]',
           'reaction': '[تفاعل]', 'video': '[فيديو]'}


def display_body(m):
    """What a message says, with attachments (whose URLs carry a Bitrix token) replaced."""
    if m['role'] == 'system':
        return '[رسالة آلية - المحتوى محجوب]'
    if m['type'] in TYPE_AR:
        return TYPE_AR[m['type']]
    body = m['body'].strip()
    if re.match(r'https?://', body) and ' ' not in body:
        return '[مرفق]'
    body = re.sub(r'https?://\S*token=\S+', '[مرفق]', body)
    return body


def split_quote(body):
    """A customer reply to an earlier message arrives as ">>Sender: quoted text" + newline + own words.
    Returns (quoted or None, own words). The quoted part is someone else's text, never the customer's."""
    if not body.startswith('>>'):
        return None, body
    head, _, rest = body.partition('\n')
    quoted = head[2:].split(':', 1)[1].strip() if ':' in head else head[2:].strip()
    return quoted, rest.strip()


def own_text(m):
    """The words this sender actually wrote."""
    body = display_body(m)
    return split_quote(body)[1] if m['role'] == 'customer' else body


def agent_letters(thread):
    """Human agents in order of first appearance: أ, ب, ج ..."""
    order = []
    for m in thread['messages']:
        if m['role'] == 'agent' and m['agent_ext'] not in order:
            order.append(m['agent_ext'])
    return {ext: 'أبجدهوز'[i] for i, ext in enumerate(order)}


def render_transcript(thread):
    letters = agent_letters(thread)
    multi = len(letters) > 1
    lines = []
    for i, m in enumerate(thread['messages'], 1):
        t = datetime.fromisoformat(m['at']).astimezone(RIYADH).strftime('%m-%d %H:%M')
        who = ROLE_AR[m['role']]
        if m['role'] == 'agent' and multi:
            who += f" ({letters[m['agent_ext']]})"
        lines.append(f'[{i}] {t} {who}: {display_body(m)}')
    return '\n'.join(lines)


_DIACRITICS = re.compile(r'[ؐ-ًؚ-ٰٟۖ-ۭـ]')


def norm(s):
    """Loose-but-deterministic text key for checking a quote is really there."""
    s = _DIACRITICS.sub('', s or '')
    s = re.sub('[أإآٱ]', 'ا', s).replace('ى', 'ي').replace('ة', 'ه').replace('ؤ', 'و').replace('ئ', 'ي')
    s = s.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    s = re.sub(r'[^\w]+', ' ', s.lower())
    return ' '.join(s.split())


def business_seconds(a, b):
    """Seconds between two datetimes that fall inside 09:00-21:00 Riyadh."""
    if b <= a:
        return 0.0
    a, b = a.astimezone(RIYADH), b.astimezone(RIYADH)
    total, day = 0.0, a.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < b:
        s = max(a, day.replace(hour=BUSINESS_START))
        e = min(b, day.replace(hour=BUSINESS_END))
        if e > s:
            total += (e - s).total_seconds()
        day += timedelta(days=1)
    return total
