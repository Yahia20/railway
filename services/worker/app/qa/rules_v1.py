"""Rubric v1.0 word lists: every question is decided by these lists and by timestamps. No model.
THE list, not examples: a word that is not here does not count."""
import re
from .rules import LISTS as V02, _m

LISTS = {k: v for k, v in V02.items() if k != 'survey'}
LISTS.update({
    # 2 recap: a confirming question (the line must also contain ؟ or ?)
    'recap': ['صحيح', 'صح', 'مظبوط', 'تقصد', 'قصدك', 'يعني حضرتك', 'حضرتك تبي', 'حضرتك عايز', 'حضرتك عاوز',
              'حضرتك محتاج', 'حضرتك حابب', 'حضرتك حابه'],
    # 3 probing: a question about the trip details (the line must also contain ؟ or ?)
    'probe': ['متى', 'امتى', 'إمتى', 'تاريخ', 'التاريخ', 'كم شخص', 'كم فرد', 'كم مسافر', 'عدد الأشخاص',
              'عدد الافراد', 'عدد المسافرين', 'كم بالغ', 'كم ليلة', 'كم يوم', 'وين', 'فين', 'الوجهة', 'الجنسية',
              'مطار الإقلاع', 'من أي مطار'],
    # trip request: turns on questions 3 and 29-32
    'trip': ['احجز', 'أحجز', 'حجز', 'بحجز', 'رحلة', 'رحلات', 'سفر', 'أسافر', 'نسافر', 'بكج', 'باكج', 'باقة',
             'عرض', 'العرض', 'عروض', 'طيران', 'تذكرة', 'تذاكر', 'فندق', 'فنادق', 'كم السعر', 'كم التكلفة',
             'بكم'],
    'unprofessional': ['اقرا فوق', 'اقرأ فوق', 'قلت لك', 'قلتلك', 'مش شغلي', 'مو شغلي', 'شوف بنفسك',
                       'مش فاضي', 'مو فاضي'],
    'negative': ['زفت', 'سيئة', 'سيئين', 'ما أنصحك', 'مش بنصحك', 'ما بنصحك', 'نصابين', 'حرامية', 'فاشلة',
                 'تعبانة', 'دايما متأخرين', 'مش كويسة'],
    'impolite': ['غبي', 'غبية', 'حمار', 'تافه', 'انت اللي غلطت', 'انتي اللي غلطتي', 'الغلط منك', 'مش مشكلتي',
                 'مو مشكلتي', 'اسكت', 'قليل الذوق'],
    'transfer': ['هحولك', 'حولتك', 'أحولك', 'أحول لك', 'زميلي', 'زميلتي', 'الزميل', 'الزميلة',
                 'القسم المختص', 'هيتواصل معك', 'سيتواصل معك'],
    'alternative': ['بديل', 'بدل', 'ممكن نشوف', 'نقدر نشوف', 'اقترح', 'أقترح', 'المتاح', 'المتوفر', 'متاح في',
                    'متاح يوم', 'تاريخ تاني', 'تاريخ ثاني', 'فندق تاني', 'فندق ثاني', 'خيار تاني', 'خيار ثاني',
                    'اختيار تاني', 'اختيار ثاني', 'عندنا', 'يوجد لدينا'],
    'objection_answer': ['خصم', 'أقل', 'أرخص', 'ارخص', 'نخفض', 'تخفيض', 'شامل', 'يشمل', 'بديل', 'عرض تاني',
                         'عرض ثاني', 'باقة تانية', 'باقة ثانية', 'نشيل'],
})

_KEYS = {k: [(_m(p), p) for p in v if _m(p)] for k, v in LISTS.items()}


def hit(list_name, text):
    """First phrase of the list found in text, or None. Short words must stand alone (optional و/ف/ب/ل prefix)."""
    t = ' ' + _m(text) + ' '
    for key, phrase in _KEYS[list_name]:
        if len(key) >= 4:
            if key in t:
                return phrase
        elif re.search(r'\s[وفبل]?' + re.escape(key) + r'\s', t):
            return phrase
    return None


def is_question(raw):
    return '؟' in raw or '?' in raw
