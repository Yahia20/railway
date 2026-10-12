"""The graded chat and the rules, as HTML fragments for the dashboard.

The console shows the whole conversation beside the grade (it reads
/conversations itself), so a fragment carries only the verdicts and the
messages that prove them — the same masked text the model was graded on.
Everything is escaped here; the page inserts the fragment as-is.
"""
from __future__ import annotations

import html
from typing import Any

from .scorecard import BANDS, WEIGHTS
from .texts import BOOKING, CHAT_TYPES, ITEMS, LOSS_REASONS, RED_FLAGS, ROLES

e = html.escape
_SOURCE = {"sonnet": "Claude Sonnet", "deepseek": "DeepSeek"}


def band(pct: float | None) -> str:
    if pct is None:
        return ""
    return "good" if pct >= BANDS["good_min"] else "mid" if pct >= BANDS["mid_min"] else "bad"


def _bubble(ev: dict[str, Any] | None) -> str:
    if not ev:
        return ""
    role = ev.get("by") or "system"
    return (f'<div class="ev {e(role)}"><span class="who">{e(ROLES.get(role, role))} · رسالة {int(ev["line"])}</span>'
            f'<span dir="rtl">{e(ev.get("text") or "")}</span></div>')


def _proof(label: str, lines: list[int], evidence: dict[str, Any], note: str = "") -> str:
    bubbles = "".join(_bubble(evidence.get(str(n))) for n in lines)
    if not bubbles and not note:
        return ""
    head = f"{label}: {note}" if note else label
    return f'<div class="proof"><span class="pl">{e(head)}</span>{bubbles}</div>'


def _item(key: str, it: dict[str, Any], row: dict[str, Any], evidence: dict[str, Any]) -> str:
    name, rule, _ = ITEMS[key]
    value = float(it["value"])
    pct = round(value * 100)
    ok = value >= 1.0
    note = ""
    if key == "followup":
        note = f"{row.get('followup_attempts') or 0} محاولة متابعة"
    proof = _proof("الدليل" if ok else "فين المشكلة", it.get("lines") or [], evidence, note)
    return (f'<div class="item"><div class="ih"><span>{e(name)} <span class="w">وزن {WEIGHTS[key]}</span></span>'
            f'<span class="is {band(pct)}">{pct}%</span></div>'
            f'<ul class="qs"><li class="{"yes" if ok else "no"}"><span class="mark">{"✓" if ok else "✗"}</span>'
            f'<div><b>{e(rule)}</b>{proof}</div></li></ul></div>')


def chat_html(row: dict[str, Any]) -> str:
    """One graded chat: score, red flag, loss reason, then each item that applied."""
    detail = row.get("detail") or {}
    evidence = detail.get("evidence") or {}
    items = row.get("items") or {}
    score = None if row.get("score") is None else float(row["score"])
    ctype = CHAT_TYPES.get(row.get("chat_type") or "", "نوع غير معروف")
    shown = "—" if score is None else f"{round(score)}%"
    ready = " · عميل جاهز" if row.get("qualified") else ""

    parts = [f'<div class="chat-head"><span class="label">{e(ctype)}{ready}</span>'
             f'<span class="sc {band(score)}">{shown}</span></div>']

    if detail.get("purged"):
        parts.append('<p class="na">نص الرسايل اتمسح بعد 90 يوم. الدرجة والبنود باقية.</p>')

    if row.get("red_flag"):
        booking = "".join(_bubble(detail.get("booking", {}).get(k)) for k in BOOKING)
        parts.append(f'<div class="item crit-box"><div class="ih"><span>⚠ {e(RED_FLAGS[row["red_flag"]])}</span></div>'
                     f'{booking}</div>')

    loss = row.get("loss_reason")
    if loss:
        src = _SOURCE.get(row.get("loss_reason_source") or "", "")
        note = detail.get("loss_note")
        parts.append(f'<div class="item"><div class="ih"><span>سبب ما حجزش</span>'
                     f'<span>{e(LOSS_REASONS.get(loss, loss))}</span></div>'
                     f'{_bubble(detail.get("loss_evidence"))}'
                     f'<span class="pl">{e("من " + src) if src else ""}{e(" · " + note) if note else ""}</span></div>')

    order = sorted((k for k in WEIGHTS if k in items), key=lambda k: (float(items[k]["value"]) >= 1.0, -WEIGHTS[k]))
    parts += [_item(k, items[k], row, evidence) for k in order]

    na = [ITEMS[k][0] for k in WEIGHTS if k not in items]
    if na:
        parts.append(f'<p class="na">مش منطبق على الشات ده: {e("، ".join(na))}</p>')
    return f'<div class="chat">{"".join(parts)}</div>'


def rules_html() -> str:
    """The scorecard as the owner approved it: weights, what passes, when it applies."""
    total = sum(WEIGHTS.values())
    blocks = [
        f'<div class="ritem"><div class="rh"><span>{e(name)}</span><span class="w big">وزن {WEIGHTS[k]}</span></div>'
        f'<span class="rule">{e(rule)}</span><span class="en-name">بيتحسب لما: {e(applies)}</span></div>'
        for k, (name, rule, applies) in ITEMS.items()
    ]
    blocks.append(
        '<div class="ritem crit-r"><div class="rh"><span>علامة حمرا</span></div>'
        f'<span class="rule">{e(RED_FLAGS["no_offer"])}: العميل قال الوجهة والتاريخ وعدد الأفراد، '
        'وما اتبعتلوش عرض، وما اتحوّلش لزميل، ومش مستنيين رده. لو الموظف بعت مرفق من غير ما يقول إيه هو، '
        'الشات بيروح للمراجعة بدل العلامة.</span></div>')
    reasons = "".join(f"<li><b>{e(ar)}</b> <span class=\"en-name\">{e(en)}</span></li>"
                      for en, ar in LOSS_REASONS.items())
    blocks.append(
        '<div class="ritem out"><div class="rh"><span>سبب ما حجزش</span></div>'
        '<span class="rule">«بطّل يرد» بيتقسم بالكود: لو آخر رسالة من العميل ومحدش رد = «العميل كتب ومحدش رد»؛ '
        'غير كده لو الموظف تابع أقل من مرتين = «مفيش متابعة كفاية»، مرتين أو أكتر = «بطّل يرد بعد متابعتين».</span>'
        f'<ul>{reasons}</ul></div>')
    blocks.append(
        '<div class="ritem out">'
        f'<p class="en-name">مجموع الأوزان {total}. درجة الشات = مجموع (الوزن × النتيجة) للبنود اللي اتطبقت '
        '÷ مجموع أوزانها × 100. البند اللي ما اتطبقش بيطلع من البسط والمقام.</p>'
        '<p class="en-name">كل شات بيتقرا 3 مرات: البند بيتحسب لو اتطبق في مرتين من التلاتة، ونتيجته الوسيط. '
        'مفيش غلطة لوحدها بتصفّر الشات.</p>'
        f'<p class="en-name">الألوان: {BANDS["good_min"]} وفوق أخضر، {BANDS["mid_min"]} لـ '
        f'{BANDS["good_min"] - 1} أصفر، تحت {BANDS["mid_min"]} أحمر. درجة الموظف = متوسط درجات شاتاته، '
        'واقراها أسبوعياً مش شات بشات.</p></div>')
    return "".join(blocks)
