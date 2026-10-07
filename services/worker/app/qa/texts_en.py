"""English wording for the page. Chat messages themselves stay in Arabic: they are the evidence."""
import re

Q_EN = {
    1: ('Correct greeting', "The agent's first two messages include a greeting and the company name"),
    6: ("Used the customer's name", "The customer's first name appears in an agent message (when we know it)"),
    17: ('First reply within 10 minutes', 'Counted in working hours (9 AM to 9 PM)'),
    14: ('Other replies within 10 minutes', 'No reply took longer than 10 working minutes'),
    10: ('Came back within 10 minutes after "one moment"', 'When the agent said "one moment" or "seconds"'),
    33: ('Did not ask for something the customer already said', 'Destination, date or number of travellers'),
    2: ("Confirmed the customer's request with a question",
        'A question with "?" that repeats a detail, e.g. "You want Russia for two people?"'),
    3: ('Asked for missing trip details', 'Destination, date or number of travellers, when missing'),
    13: ('Professional language', 'No curt or unprofessional wording'),
    12: ('Explained again when the customer did not understand', 'When the customer said they did not understand'),
    9: ('Apologised when the customer complained', 'When the customer complained'),
    4: ('Reassured the customer', 'A phrase like "أبشر", "تحت أمرك", "من عيوني"'),
    16: ('Polite with the customer', 'No insults, sarcasm or blaming the customer'),
    15: ('No negative talk about airlines, hotels or the company', 'Nothing negative about a supplier or Travel Gate'),
    26: ('No contradicting information', 'Price, date, time, or available / not available'),
    25: ("Answered all the customer's questions", 'Every customer message with "?" has a reply after it'),
    18: ('Told the customer when handing over', 'When another agent continued the chat'),
    20: ('Listed the required documents', 'When the customer asked about a visa'),
    24: ('Offered an alternative', 'When the agent said the request is not available'),
    32: ("Answered the customer's objection", 'When the customer said it is expensive or they will think'),
    23: ('Told the customer what happens next', 'e.g. "I will send you the offer within an hour"'),
    27: ('Came back as promised', 'Within 3 days of the promise'),
    29: ('Stated the price', 'A number with a currency, in booking requests'),
    30: ('Offered more than one option', 'Two or more options, in booking requests'),
    5: ('Closed the chat properly', "A closing phrase in the agent's last 3 messages"),
    31: ('Asked the customer to confirm the booking', 'In booking requests'),
    34: ('No spelling or formatting errors', 'An unfilled template or brackets, or a word wrong in every dialect'),
    35: ('Unauthorized promise', 'Guaranteed something outside their control, e.g. "the visa is guaranteed"'),
    36: ('Money or booking error', "The agent's own numbers do not add up, or wrong booking details confirmed"),
    37: ('Customer data', "Shared another customer's data, or asked for a card number, CVV or verification code"),
    38: ('Payment outside the company', 'Asked the customer to pay into a personal account or to a person'),
    21: ('Took the correct action', 'Reviewed by the QA manager'),
    28: ('Followed the correct procedures', 'Reviewed by the QA manager'),
    22: ('Logged on Bitrix', 'Not measured yet'),
}

ITEM_EN_SHORT = {
    1: 'Greeting, personalization and customer name', 2: 'Response time',
    3: 'Understands the request and reads the whole chat', 4: 'Probing questions',
    5: 'Clear and professional writing', 6: 'Empathy and positive tone', 7: 'Correct and complete information',
    8: 'Procedure, documents and handover', 9: 'Solution and alternatives', 10: 'Handling objections',
    11: 'Expectations and next steps', 12: 'Sales opportunity', 13: 'Closing and call to action',
    14: 'Confirms customer understanding', 15: 'Spelling and formatting',
}

NAMES = {'الوجهة': 'destination', 'التاريخ': 'date', 'عدد الأفراد': 'number of travellers'}


def dur(s):
    """'35 دقيقة' -> '35 minutes'."""
    s = s.replace('صفر (ردّ قبل ما يبدأ وقت الشغل)', '0 minutes (replied before working hours started)')
    s = s.replace('صفر', '0 minutes')
    s = re.sub(r'([\d.]+) ثانية', r'\1 seconds', s)
    s = re.sub(r'([\d.]+) دقيقة', r'\1 minutes', s)
    s = re.sub(r'([\d.]+) ساعة', r'\1 hours', s)
    return s


H_AR = ' (بنحسب وقت الدوام بس، من 9 الصبح لـ 9 بالليل)'
H_EN = ' (working hours only, 9 AM to 9 PM)'

FIXED = {
    'أول رسالتين من الموظف': "The agent's first two messages",
    'طلب العميل، وأول رد من الموظف': "The customer's request and the agent's first reply",
    'آخر رسايل الموظف في المحادثة': "The agent's last messages in the chat",
    'اسم العميل مسجّل عندنا، ومفيش ولا رسالة من الموظف فيها اسمه':
        "We have the customer's name, and no agent message uses it",
    'شكوى العميل، ورد الموظف بعدها': "The customer's complaint and the agent's reply",
    'قال "لحظة" وما رجعش': 'Said "one moment" and never came back',
    'العميل قال إنه مش فاهم، ورد الموظف بعدها': "The customer said they did not understand, and the agent's reply",
    'آخر رسالة من الموظف الأول، وأول رسالة من الموظف التاني':
        "The first agent's last message and the second agent's first message",
    'سؤال العميل عن التأشيرة، ورد الموظف': "The customer's visa question and the agent's reply",
    'الموظف قال إن الطلب مش متاح، وده اللي كتبه بعدها': 'The agent said it is not available, and wrote this next',
    'الموظف قال إن الطلب مش متاح، وما كتبش حاجة بعدها': 'The agent said it is not available, and wrote nothing after',
    'سؤال العميل، واللي الموظف كتبه بعده': "The customer's question and what the agent wrote next",
    'رسالتين من الموظف عكس بعض': 'Two agent messages that contradict each other',
    'وعد العميل، وأول رسالة بعدها كانت بعد أكتر من 3 أيام': 'The promise; the next message came more than 3 days later',
    'وعد العميل، وما بعتش أي رسالة بعدها': 'The promise; no message was sent after it',
    'طلب العميل، ورد الموظف': "The customer's request and the agent's reply",
    'طلب العميل، ورد الموظف (اختيار واحد أو من غير اختيارات)':
        "The customer's request and the agent's reply (one option or none)",
    'اعتراض العميل، ورد الموظف بعده': "The customer's objection and the agent's reply",
    'العميل قال المعلومة، وبعدها الموظف سأل عنها تاني': 'The customer gave the detail, then the agent asked for it again',
    'الرسالة اللي فيها الغلط': 'The message with the error', 'الوعد': 'The promise',
    'الرسالتين اللي ما بيطلعوش مع بعض': 'The two messages that do not add up', 'الرسالة': 'The message',
    'ناداه باسمه': "Used the customer's name", 'مفيش': 'None found',
    'ما سألش عن حاجة العميل قالها قبل كده': 'Did not ask for anything the customer already said',
}


def note_en(ar):
    """Translate an evidence note. Raises if a note has no English form, so nothing Arabic slips through."""
    if not ar:
        return ar
    if ar in FIXED:
        return FIXED[ar]
    m = re.fullmatch(r'العميل كتب، وأول رد من الموظف جه بعد (.+?)' + re.escape(H_AR), ar)
    if m:
        return f"The customer wrote; the agent's first reply came after {dur(m.group(1))}{H_EN}"
    m = re.fullmatch(r'العميل كتب، والموظف رد بعد (.+?)' + re.escape(H_AR), ar)
    if m:
        return f'The customer wrote; the agent replied after {dur(m.group(1))}{H_EN}'
    m = re.fullmatch(r'قال "لحظة"، ورجع بعد (.+?)' + re.escape(H_AR), ar)
    if m:
        return f'Said "one moment" and came back after {dur(m.group(1))}{H_EN}'
    m = re.fullmatch(r'كان ناقص (.+)، والموظف ما سألش عنه\. طلب العميل وأول رد من الموظف', ar)
    if m:
        missing = ', '.join(NAMES.get(x.strip(), x) for x in m.group(1).split('،'))
        return f"Missing: {missing}; the agent did not ask. The customer's request and the agent's first reply"
    m = re.fullmatch(r'عرض (\d+) اختيارات', ar)
    if m:
        return f'Offered {m.group(1)} options'
    m = re.fullmatch(r'(\d+) سؤال، كلهم اتردّ عليهم', ar)
    if m:
        return f'{m.group(1)} questions, all answered'
    raise KeyError(f'no English for note: {ar!r}')
