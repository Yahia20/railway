"""Every human-readable word about the scorecard, in one place.

The dashboard page has no copy of these: `/dashboard/overview` ships `meta()`
and the page renders from it, so renaming an item is an edit here only.
"""
from __future__ import annotations

from typing import Any

from .scorecard import BANDS, WEIGHTS

# key -> (name, what passes, when it applies)
ITEMS: dict[str, tuple[str, str, str]] = {
    "probe": ("سأل عن تفاصيل الرحلة",
              "الموظف سأل العميل عن الوجهة أو التاريخ أو عدد الأفراد.",
              "شات مبيعات، والعميل ما قالش الوجهة والتاريخ وعدد الأفراد قبل أول رد من الموظف."),
    "offer": ("بعت عرض لعميل جاهز",
              "العميل قال الوجهة والتاريخ وعدد الأفراد، والموظف بعت سعر أو برنامج أو مرفق قال إنه العرض.",
              "العميل جاهز، وما اتحوّلش لزميل، ومش مستنيين رده على سؤال، والمرفق مفهوم."),
    "match": ("العرض مطابق للطلب",
              "العرض ما فيهوش وجهة أو تاريخ أو عدد أفراد عكس اللي العميل قاله.",
              "لما يتبعت عرض."),
    "followup": ("تابع مرتين على الأقل",
                 "بعد العرض (أو آخر سؤال ما اتردّش عليه) الموظف رجع للعميل مرتين على الأقل. "
                 "المحاولة الجديدة لازم قبلها رد من العميل أو 4 ساعات.",
                 "العميل بطّل يرد (سبب الخسارة: مفيش رد أو مفيش متابعة)."),
    "promise": ("نفّذ اللي وعد بيه",
                "الموظف وعد يبعت حاجة وبعتها فعلاً.",
                "فيه وعد، وكان فيه فرصة حقيقية ينفّذه: مش مستني حاجة من العميل ما جاتش، "
                "والعميل ما قفلش الطلب خلال ساعتين من الوعد."),
    "objection": ("رد على اعتراض العميل",
                  "كل اعتراض: كويس = 100%، ضعيف = 50%، اتجاهل = صفر. السعر: سبب أو بديل + حاجة تانية. "
                  "منافس: سؤال أو سبب. تأجيل: خطوة جاية. المنتج: بديل. الثقة: سبب أو خطوة جاية. "
                  "الخصم مسموح، وبيتحسب رد بس مش كفاية لوحده.",
                  "العميل اعترض مرة على الأقل."),
    "answered": ("جاوب على كل طلبات العميل",
                 "كل حاجة العميل سأل عنها أو طلبها اتردّ عليها في رسالة بعدها. «مش متاح» رد.",
                 "العميل طلب حاجة مرة على الأقل."),
    "correct": ("معلومات صح",
                "ما قالش معلومتين عكس بعض، وأرقامه وبيانات الحجز بتطلع مع بعض.",
                "كل شات."),
    "professional": ("كلام مهني ومحترم",
                     "من غير تهرّب من المساعدة، ولا كلام وحش عن طيران أو فندق أو الشركة، "
                     "ولا قلة ذوق، ولا أخطاء كتابة شكلها وحش.",
                     "كل شات."),
}

LOSS_REASONS: dict[str, str] = {
    "Booked": "حجز",
    "Expensive Price": "السعر غالي",
    "Prefers a Competitor": "راح لمنافس",
    "Booked online": "حجز بنفسه أونلاين",
    "Product not Available": "المطلوب مش متاح عندنا",
    "visa issue": "مشكلة فيزا",
    "Supplier": "مشكلة من المورّد",
    "Customer changed mind": "غيّر رأيه أو أجّل",
    "Other": "سبب تاني",
    "No Response": "بطّل يرد بعد متابعتين",
    "No follow-up": "مفيش متابعة كفاية",
    "No Response (agent)": "العميل كتب ومحدش رد",
}

# The Bitrix list value for each reason (UF_CRM_CANCEL_REASON). "No follow-up"
# and "No Response (agent)" do not exist there yet.
BITRIX_ENUM: dict[str, int] = {
    "Expensive Price": 872, "Prefers a Competitor": 876, "Booked online": 878,
    "Product not Available": 870, "visa issue": 3460, "Supplier": 3456,
    "Customer changed mind": 750, "Other": 760, "No Response": 880,
}

RED_FLAGS: dict[str, str] = {
    "no_offer": "عميل جاهز ما خدش عرض",
    "review_attachment": "عميل جاهز والمرفق مش واضح إنه العرض: محتاج مراجعة",
}

CHAT_TYPES: dict[str, str] = {"sales": "مبيعات", "visa": "فيزا", "after_sales": "بعد الحجز", "other": "تاني"}

BOOKING: dict[str, str] = {"destination": "الوجهة", "date": "التاريخ", "travellers": "عدد الأفراد"}

ROLES: dict[str, str] = {"customer": "العميل", "agent": "الموظف", "bot": "البوت", "system": "رسالة آلية"}


def meta() -> dict[str, Any]:
    """What the dashboard needs to label anything the scorecard returns."""
    return {
        "items": [{"key": k, "name": ITEMS[k][0], "rule": ITEMS[k][1], "applies": ITEMS[k][2],
                   "weight": WEIGHTS[k]} for k in WEIGHTS],
        "loss_reasons": LOSS_REASONS,
        "red_flags": RED_FLAGS,
        "chat_types": CHAT_TYPES,
        "bands": BANDS,
    }
