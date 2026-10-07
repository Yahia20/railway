"""What is broken in the chat pipeline right now, and why — one page.

WHY THIS EXISTS. On 2026-10-04/05 19,955 Bitrix deals were modified at once,
workflow 04's capped pull filled with old deals, and for two nights no new
deal reached the database. Nothing said so. `nightly_health` had been writing
`failed` every night since 09-29 into a table nobody reads, so a real outage
looked exactly like the noise around it. A check that nobody sees is not a
check.

EACH CHECK ANSWERS FOUR QUESTIONS, in Arabic, for the person who owns the
pipeline: what is happening (`what`, measured, with numbers), why it happens
(`why`, the mechanism and the workflow that owns it), what to do (`fix`), and
how bad (`status`). A status without a cause is an alarm; this is a diagnosis.

STATUSES. `fail` — something is losing or corrupting data now. `warn` — degraded
or about to be. `off` — deliberately switched off by a person; not an outage,
but it must stay visible or it is forgotten (the judge was off for three weeks
with nobody asking). `ok`. `error` — the check itself could not run, which is
never folded into `ok`.

Every check is separately fallible for the same reason /report's panels are: a
database one migration behind must still show the checks that work.

Read-only, like everything in this service (rule 11).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import db

log = logging.getLogger("worker.status")

RIYADH = timezone(timedelta(hours=3))

# How long the chat API may stay silent before it is an outage. Customers write
# around the clock (2,000-4,000 messages a day, some every hour), so an hour of
# silence is unusual and three is a stopped webhook.
INGEST_WARN = timedelta(hours=1)
INGEST_FAIL = timedelta(hours=3)

# Workflow 09 runs every minute; customer links die after ~20 minutes.
MEDIA_SCAN_FAIL = timedelta(minutes=10)

# Workflow 04 runs at 03:20 Riyadh every night.
DEALS_STALE_FAIL = timedelta(hours=26)

# A conversation whose deal is not in `deals` a day after it started means the
# nightly pull missed it. A few are normal (a deal created after 03:20); a
# fifth is not.
DEAL_MISSING_WARN = 0.10
DEAL_MISSING_FAIL = 0.20

LOW_BALANCE_USD = 3

_RANK = {"fail": 0, "error": 1, "warn": 2, "off": 3, "ok": 4}


def _ago(td: timedelta | None) -> str:
    if td is None:
        return "مفيش"
    s = int(td.total_seconds())
    if s < 120:
        return f"{s} ثانية"
    if s < 7200:
        return f"{s // 60} دقيقة"
    if s < 172800:
        return f"{s // 3600} ساعة"
    return f"{s // 86400} يوم"


def _riyadh(ts: datetime | None) -> str | None:
    return ts.astimezone(RIYADH).strftime("%Y-%m-%d %H:%M") if ts else None


def _check(key: str, title: str) -> dict[str, Any]:
    return {"key": key, "title": title, "status": "ok", "what": "", "why": "",
            "fix": "", "since": None}


# ---------------------------------------------------------------------------
# 1 · Ingest — are chats arriving and being stored? (workflow 01c)
# ---------------------------------------------------------------------------

SQL_INGEST = """
SELECT max(received_at)                                         AS last_in,
       now() - max(received_at)                                 AS age,
       count(*) FILTER (WHERE received_at > now() - interval '24 hours') AS n24,
       count(*) FILTER (WHERE received_at > now() - interval '24 hours'
                          AND process_error IS NOT NULL)        AS rejected24,
       count(*) FILTER (WHERE received_at > now() - interval '24 hours'
                          AND received_at < now() - interval '10 minutes'
                          AND processed_at IS NULL
                          AND process_error IS NULL)            AS stuck24
FROM raw_events
WHERE source = 'bitrix_chat_api'
"""


def check_ingest() -> dict[str, Any]:
    c = _check("ingest", "استلام الشاتات (01c)")
    r = db.one(SQL_INGEST)
    age = r["age"]
    c["since"] = _riyadh(r["last_in"])
    c["what"] = (f"آخر رسالة وصلت من {_ago(age)}. "
                 f"آخر 24 ساعة: {r['n24']} رسالة، "
                 f"{r['rejected24']} اترفضت، {r['stuck24']} وصلت ومتخزنتش.")
    if age is None or age > INGEST_FAIL:
        c["status"] = "fail"
        c["why"] = ("مفيش رسايل بتوصل لـ n8n. يا الـ webhook بتاع Bitrix/Cultiv "
                    "وقف يبعت، يا n8n نفسه واقع، يا workflow 01c اتقفل.")
        c["fix"] = ("افتح n8n وشوف 01c شغال وآخر execution امتى. لو n8n شغال "
                    "ومفيش executions، المشكلة عند اللي بيبعت (Cultiv).")
    elif r["stuck24"]:
        c["status"] = "fail"
        c["why"] = ("n8n استلم رسايل ومكملش تخزينها. ده معناه إن 01c وقع في "
                    "النص (غالباً خطأ في الداتابيز).")
        c["fix"] = "افتح executions بتاعة 01c في n8n وشوف الـ error."
    elif age > INGEST_WARN:
        c["status"] = "warn"
        c["why"] = "سكوت أطول من المعتاد. ممكن يكون هدوء طبيعي، وممكن بداية عطل."
        c["fix"] = "لو عدّت 3 ساعات هتبقى عطل. راجع n8n."
    elif r["rejected24"]:
        c["status"] = "warn"
        c["why"] = ("فيه رسايل وصلت من غير رقم محادثة أو رقم deal، فمفيش مكان "
                    "تتخزن فيه. السبب عند اللي بيبعت.")
        c["fix"] = "عيّنة منهم في raw_events.process_error."
    return c


# ---------------------------------------------------------------------------
# 2 · Media — are files kept before their links die? (workflow 09)
# ---------------------------------------------------------------------------

SQL_MEDIA = "SELECT *, now() - last_scan_at AS scan_age FROM v_media_health"

SQL_MEDIA_REJECTED_24H = """
SELECT coalesce(last_error, last_outcome) AS reason, count(*) AS n
FROM media_fetch_jobs
WHERE status = 'rejected' AND updated_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC LIMIT 3
"""


def check_media() -> dict[str, Any]:
    c = _check("media", "حفظ الصور والصوت (09)")
    r = db.one(SQL_MEDIA)
    c["since"] = _riyadh(r["last_scan_at"])
    c["what"] = (f"{r['stored']} ملف متخزن، {r['queued']} مستني، "
                 f"آخر فحص من {_ago(r['scan_age'])}. "
                 f"لينكات عملاء في خطر: {r['customer_links_at_risk']}.")
    if r["mode"] != "on":
        c["status"] = "off"
        c["why"] = "الأرشيف مقفول بإيد (media_archive_config.mode)."
        c["fix"] = "UPDATE media_archive_config SET value = 'on' WHERE key = 'mode';"
        return c
    if r["scan_age"] is None or r["scan_age"] > MEDIA_SCAN_FAIL:
        c["status"] = "fail"
        c["why"] = ("workflow 09 مش بيشتغل. لينكات واتساب بتموت بعد حوالي 20 "
                    "دقيقة، فكل صورة أو فويس بيجي دلوقتي هيضيع.")
        c["fix"] = "افتح n8n واتأكد إن 09 Active وإن آخر execution من دقيقة."
        return c
    if r["customer_links_at_risk"]:
        c["status"] = "fail"
        c["why"] = ("فيه ملفات عملاء عمرها أكتر من 15 دقيقة ولسه ما اتنزلتش. "
                    "يا الـ worker بطيء، يا الـ bucket مش بيرد.")
        c["fix"] = "شوف v_media_health و media_fetch_jobs.last_error."
        return c
    rejected = db.rows(SQL_MEDIA_REJECTED_24H)
    if rejected:
        c["status"] = "warn"
        c["why"] = ("ملفات اترفضت آخر 24 ساعة: "
                    + "؛ ".join(f"{x['n']} × {x['reason']}" for x in rejected))
        c["fix"] = "403 معناها اللينك محتاج صلاحية؛ 400 لينك بايظ من المصدر."
    return c


# ---------------------------------------------------------------------------
# 3 · Deals — did last night's Bitrix pull bring the new deals? (workflow 04)
# ---------------------------------------------------------------------------

SQL_DEALS = """
SELECT
  (SELECT max(updated_at) FROM deals WHERE origin = 'bitrix')         AS last_write,
  (SELECT now() - max(updated_at) FROM deals WHERE origin = 'bitrix') AS age,
  (SELECT max(created_at_src) FROM deals WHERE origin = 'bitrix')     AS newest_deal,
  count(*) FILTER (WHERE i.external_deal_id IS NOT NULL)              AS with_deal_id,
  count(*) FILTER (WHERE i.external_deal_id IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM deals d
                                     WHERE d.bitrix_deal_id = i.external_deal_id))
                                                                      AS deal_missing
FROM interactions i
WHERE i.external_source = 'bitrix_chat_api'
  AND i.started_at BETWEEN now() - interval '3 days' AND now() - interval '1 day'
"""


def check_deals() -> dict[str, Any]:
    c = _check("deals", "سحب الـ deals من Bitrix (04)")
    r = db.one(SQL_DEALS)
    n, missing = r["with_deal_id"] or 0, r["deal_missing"] or 0
    share = missing / n if n else 0.0
    c["since"] = _riyadh(r["last_write"])
    c["what"] = (f"محادثات من يوم لـ 3 أيام فاتوا: {missing} من {n} الـ deal "
                 f"بتاعها مش عندنا ({share:.0%}). أحدث deal عندنا اتعمل "
                 f"{_riyadh(r['newest_deal']) or '—'}. آخر سحب من {_ago(r['age'])}.")
    if r["age"] is None or r["age"] > DEALS_STALE_FAIL:
        c["status"] = "fail"
        c["why"] = "workflow 04 ما اشتغلش آخر ليلة (بيشتغل 3:20 الفجر)."
        c["fix"] = "افتح n8n وشوف execution بتاعة 04 والـ error بتاعها."
    elif share >= DEAL_MISSING_FAIL:
        c["status"] = "fail"
        c["why"] = ("04 اشتغل بس ما جابش الـ deals الجديدة. السبب الأشهر: "
                    "تعديل جماعي في Bitrix خلّى عدد الـ deals المتعدّلة أكبر "
                    "من حد السحب (5,000). من غير الـ deal المحادثة بتفضل من غير "
                    "موبايل وعميل وموظف.")
        c["fix"] = ("اتأكد إن الـ worker فيه ترتيب الأحدث الأول (DEAL_ORDER). "
                    "وشوف مين عدّل deals كتير في Bitrix.")
    elif share >= DEAL_MISSING_WARN:
        c["status"] = "warn"
        c["why"] = "نسبة أعلى من الطبيعي من المحادثات من غير deal."
        c["fix"] = "راقبها الليلة الجاية؛ لو زادت هتبقى نفس مشكلة الحد."
    return c


# ---------------------------------------------------------------------------
# 4 · Identity — are conversations becoming customers? (workflow 03)
# ---------------------------------------------------------------------------

SQL_IDENTITY = """
SELECT count(*)                                                     AS total,
       count(*) FILTER (WHERE customer_phone_e164 IS NOT NULL)      AS with_phone,
       count(*) FILTER (WHERE customer_phone_e164 IS NOT NULL
                          AND customer_id IS NULL)                  AS phone_unlinked,
       (SELECT max(updated_at) FROM customers)                      AS last_customer_write
FROM interactions
WHERE external_source = 'bitrix_chat_api'
  AND started_at BETWEEN now() - interval '3 days' AND now() - interval '1 day'
"""


def check_identity() -> dict[str, Any]:
    c = _check("identity", "ربط المحادثة بالعميل (03)")
    r = db.one(SQL_IDENTITY)
    c["since"] = _riyadh(r["last_customer_write"])
    c["what"] = (f"محادثات من يوم لـ 3 أيام: {r['total']}، فيها موبايل "
                 f"{r['with_phone']}، منهم {r['phone_unlinked']} لسه من غير عميل.")
    if r["phone_unlinked"]:
        c["status"] = "fail"
        c["why"] = ("03 ما ربطش محادثات عندها موبايل بعميل. 03 بيشتغل 3:40 الفجر "
                    "بعد 04؛ لو ما اشتغلش، مفيش عملاء جداد ولا أسماء.")
        c["fix"] = "افتح n8n وشوف execution بتاعة 03."
    elif r["total"] and r["with_phone"] < r["total"] * 0.5:
        c["status"] = "warn"
        c["why"] = ("أقل من نص المحادثات عندها موبايل. الموبايل بييجي من الـ deal "
                    "في Bitrix، فده غالباً نفس مشكلة سحب الـ deals.")
        c["fix"] = "صلّح فحص الـ deals الأول."
    return c


# ---------------------------------------------------------------------------
# 5 · Judge — is evaluation running, and if not, who stopped it? (workflow 01d)
# ---------------------------------------------------------------------------

SQL_GATE = "SELECT * FROM v_pipeline_gate WHERE provider = 'deepseek'"

SQL_JUDGE = """
SELECT count(*) FILTER (WHERE status = 'pending')                         AS pending,
       count(*) FILTER (WHERE status = 'dead_letter'
                          AND updated_at > now() - interval '24 hours')   AS dead24,
       (SELECT count(*) FROM agent_evaluations
         WHERE created_at > now() - interval '24 hours')                  AS evaluated24,
       (SELECT max(created_at) FROM agent_evaluations)                    AS last_eval,
       (SELECT left(last_error, 160) FROM chat_eval_jobs
         WHERE status = 'dead_letter' ORDER BY updated_at DESC LIMIT 1)   AS last_error
FROM chat_eval_jobs
"""


def check_judge() -> dict[str, Any]:
    c = _check("judge", "تقييم الشاتات بالـ AI (01d)")
    g = db.one(SQL_GATE)
    j = db.one(SQL_JUDGE)
    c["since"] = _riyadh(j["last_eval"])
    c["what"] = (f"آخر تقييم {_riyadh(j['last_eval']) or 'مفيش'}. "
                 f"آخر 24 ساعة: {j['evaluated24']} اتقيّم، {j['dead24']} فشل. "
                 f"مستني: {j['pending']}. رصيد DeepSeek: {g.get('balance_usd')}$.")
    if not g.get("may_run"):
        reason = g.get("reason") or ""
        if "disabled" in reason:
            c["status"] = "off"
            c["why"] = "التقييم مقفول بإيد (provider_budgets.enabled = false)."
            c["fix"] = ("UPDATE provider_budgets SET enabled = true "
                        "WHERE provider = 'deepseek';")
        else:
            c["status"] = "fail"
            c["why"] = f"البوابة مانعة الصرف: {reason}."
            c["fix"] = "اشحن رصيد DeepSeek أو ارفع الحد في provider_budgets."
        return c
    if j["dead24"]:
        c["status"] = "warn"
        c["why"] = f"تقييمات فشلت نهائي آخر 24 ساعة. آخر خطأ: {j['last_error']}"
        c["fix"] = "افتح executions بتاعة 01d في n8n."
    elif j["pending"] and not j["evaluated24"]:
        c["status"] = "fail"
        c["why"] = ("البوابة مفتوحة وفيه شغل مستني، ومفيش ولا تقييم آخر 24 ساعة. "
                    "يا 01d مقفول في n8n، يا بيقع قبل ما يخلص.")
        c["fix"] = "افتح n8n وشوف 01d Active وآخر executions بالليل."
    if g.get("balance_usd") is not None and float(g["balance_usd"]) < LOW_BALANCE_USD:
        if c["status"] == "ok":
            c["status"] = "warn"
        c["why"] = (c["why"] + " " if c["why"] else "") + "رصيد DeepSeek قرب يخلص."
    return c


# ---------------------------------------------------------------------------
# 6 · Retention — is the 90-day purge actually purging? (workflow 04)
# ---------------------------------------------------------------------------

SQL_RETENTION = """
SELECT count(*)                        AS overdue,
       min(started_at)                 AS oldest
FROM interactions
WHERE external_source = 'bitrix_chat_api'
  AND started_at < now() - interval '90 days'
  AND content_purged_at IS NULL
"""


def check_retention() -> dict[str, Any]:
    c = _check("retention", "المسح بعد 90 يوم (04)")
    r = db.one(SQL_RETENTION)
    c["what"] = f"محادثات عدّت 90 يوم ولسه ما اتمسحتش: {r['overdue']}."
    if r["overdue"]:
        c["status"] = "fail"
        c["since"] = _riyadh(r["oldest"])
        c["why"] = ("purge_raw_content() في 04 مش شغالة. فضلت بتقع من 09-16 لـ "
                    "10-01 لما 023 مسحت جدول transcripts.")
        c["fix"] = "شغّل SELECT purge_raw_content(); وشوف الـ error."
    return c


# ---------------------------------------------------------------------------
# 7 · Roster — is someone selling who has no name here?
# ---------------------------------------------------------------------------

SQL_ROSTER = """
SELECT bitrix_user_id, deals, latest_deal_at
FROM v_roster_gaps
WHERE latest_deal_at > now() - interval '14 days'
ORDER BY deals DESC
"""


def check_roster() -> dict[str, Any]:
    c = _check("roster", "موظفين من غير اسم")
    rows = db.rows(SQL_ROSTER)
    c["what"] = (f"يوزرات Bitrix عندهم deals آخر أسبوعين ومش في جدول الموظفين: "
                 f"{len(rows)}" + (" (" + "، ".join(
                     f"{x['bitrix_user_id']}: {x['deals']} deal" for x in rows) + ")"
                     if rows else "") + ".")
    if rows:
        c["status"] = "warn"
        c["why"] = ("موظف جديد اتضاف في Bitrix. شاتاته مش هتتحسب له في التقييم "
                    "لحد ما يتسمّى.")
        c["fix"] = ("ضيفه في local-reports/agent_roster.json وشغّل "
                    "scripts/seed_agents.py --apply.")
    return c


# ---------------------------------------------------------------------------
# 8 · QA scorecard — is tonight's grading running? (workflow 10)
# ---------------------------------------------------------------------------

SQL_QA = """
SELECT g.mode, g.may_run, g.reason,
       (SELECT count(*) FROM v_qa_due)                                         AS due,
       (SELECT count(*) FROM qa_evaluations
         WHERE status = 'scored' AND evaluated_at > now() - interval '26 hours') AS graded26,
       (SELECT count(*) FROM qa_evaluations
         WHERE status = 'failed' AND updated_at > now() - interval '26 hours')   AS failed26,
       (SELECT max(evaluated_at) FROM qa_evaluations WHERE status = 'scored')    AS last_graded,
       (SELECT left(reason, 160) FROM qa_evaluations WHERE status = 'failed'
         ORDER BY updated_at DESC LIMIT 1)                                       AS last_error
FROM v_qa_gate g
"""


def check_qa() -> dict[str, Any]:
    c = _check("qa", "تقييم الموظفين بشيت الجودة (10)")
    r = db.one(SQL_QA)
    c["since"] = _riyadh(r["last_graded"])
    c["what"] = (f"آخر 26 ساعة: {r['graded26']} شات اتقيّم، {r['failed26']} فشل. "
                 f"مستني تقييم: {r['due']}.")
    if r["mode"] != "on":
        c["status"] = "off"
        c["why"] = "التقييم بشيت الجودة مقفول (qa_config.mode = off)."
        c["fix"] = "UPDATE qa_config SET value = 'on' WHERE key = 'mode';"
    elif not r["may_run"]:
        c["status"] = "fail"
        c["why"] = f"مفتوح بس البوابة مانعة الصرف: {r['reason']}."
        c["fix"] = "اشحن رصيد DeepSeek، أو استنى أول فحص للرصيد الليلة."
    elif r["due"] and not r["graded26"]:
        c["status"] = "fail"
        c["why"] = ("مفتوح وفيه شاتات مستنية، ومفيش ولا تقييم آخر 26 ساعة. يا workflow 10 "
                    "مش Active في n8n، يا بيقع قبل ما يخلص.")
        c["fix"] = "افتح n8n واتأكد إن 10 · Chat QA scorecard شغال، وشوف آخر execution."
    elif r["failed26"]:
        c["status"] = "warn"
        c["why"] = f"شاتات فشل تقييمها وهتتعاد لوحدها. آخر خطأ: {r['last_error']}"
        c["fix"] = "لو الرقم بيزيد، افتح executions بتاعة 10 في n8n."
    return c


CHECKS: list[Callable[[], dict[str, Any]]] = [
    check_ingest, check_media, check_deals, check_identity,
    check_judge, check_qa, check_retention, check_roster,
]


def build() -> dict[str, Any]:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except db.DatabaseUnavailable:
            raise
        except Exception as exc:  # one broken check must not hide the others
            log.exception("status check %s failed", fn.__name__)
            key = fn.__name__.removeprefix("check_")
            out.append({"key": key, "title": key, "status": "error",
                        "what": "", "why": f"الفحص نفسه وقع: {exc}",
                        "fix": "", "since": None})
    out.sort(key=lambda c: _RANK.get(c["status"], 9))
    worst = out[0]["status"] if out else "ok"
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "overall": worst, "checks": out}
