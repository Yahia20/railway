"""Render the call evaluation as a standalone HTML page.

Generated from the JSON rather than hand-written so the Arabic survives: the
Windows console mangles it, the files do not.

    python scripts/build_report.py
"""
from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT = ROOT / "docs" / "samples" / "call-1782914722-transcript.json"
EVALUATION = ROOT / "docs" / "samples" / "call-1782914722-evaluation.json"
OUT = ROOT / "docs" / "call-evaluation.html"

MODULE_LABELS = {
    "module1_reception": ("Reception", 0.15),
    "module2_offer": ("Offer quality", 0.25),
    "module3_objections": ("Objection handling", 0.25),
    "module4_followup": ("Follow-up", 0.20),
    "module5_closing": ("Closing", 0.15),
}

CRITERION_LABELS = {
    "greeting": ("Greeting", 25),
    "understanding_confirmation": ("Confirmed the need", 25),
    "missing_info_request": ("Asked for missing info", 25),
    "next_step_transition": ("Transition to next step", 25),
    "attitude": ("Attitude", 25),
    "offer_completeness": ("Offer completeness", 25),
    "value_selling": ("Value selling", 25),
    "alternative_offer": ("Alternative when rejected", 25),
    "price_objection": ("Price too expensive", 25),
    "competitor_objection": ("Cheaper elsewhere", 25),
    "thinking_time_objection": ("Needs time to think", 25),
    "unavailable_service_objection": ("Service unavailable", 25),
    "timing": ("Follow-up timing", 40),
    "frequency": ("Follow-up frequency", 30),
    "message_quality": ("Follow-up quality", 30),
    "payment_request": ("Payment request", 30),
    "next_steps_confirmation": ("Next steps confirmed", 20),
    "thank_you": ("Thanked the customer", 20),
    "booking_steps": ("Booking steps explained", 20),
    "service_review_request": ("Asked for a review", 10),
}

NULL_REASON = {
    "module3_objections": "No objection was ever raised — the agent was not tested on this.",
    "module4_followup": "A call is continuous; follow-up happens afterwards. Needs the chat history to judge.",
    "module5_closing": "The call ended at requirement-gathering. Closing was never attempted.",
}


def e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def ar(text: str, tag: str = "span", cls: str = "ar") -> str:
    return f'<{tag} class="{cls}" dir="rtl" lang="ar">{e(text)}</{tag}>'


def mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def build() -> str:
    tr = json.loads(TRANSCRIPT.read_text(encoding="utf-8"))
    ev = json.loads(EVALUATION.read_text(encoding="utf-8"))
    p1, p2, score = ev["pass1"], ev["pass2"], ev["score"]

    # ---- module scorecard -------------------------------------------------
    rows = []
    for key, (label, weight) in MODULE_LABELS.items():
        module = p2["modules"].get(key, {})
        value = score["modules"].get(key)
        breakdown = module.get("breakdown") or {}

        if value is None:
            head = (
                f'<tr class="m null">'
                f'<th scope="row">{e(label)}<span class="w">{weight:.0%}</span></th>'
                f'<td class="sc"><span class="chip-null">not exercised</span></td>'
                f'<td class="why">{e(NULL_REASON.get(key, "Did not arise in this conversation."))}</td>'
                f"</tr>"
            )
            rows.append(head)
            continue

        band = "hi" if value >= 85 else "mid" if value >= 70 else "lo"
        rows.append(
            f'<tr class="m">'
            f'<th scope="row">{e(label)}<span class="w">{weight:.0%}</span></th>'
            f'<td class="sc"><b class="{band}">{value:.0f}</b></td>'
            f'<td class="bar"><i style="--p:{value}%"></i></td>'
            f"</tr>"
        )
        for name, raw in breakdown.items():
            clabel, cap = CRITERION_LABELS.get(name, (name, 25))
            if raw is None:
                rows.append(
                    f'<tr class="c"><td>{e(clabel)}</td>'
                    f'<td class="sc"><span class="dash">—</span></td>'
                    f'<td class="why">not applicable</td></tr>'
                )
            else:
                full = "full" if raw == cap else "part" if raw else "zero"
                rows.append(
                    f'<tr class="c"><td>{e(clabel)}</td>'
                    f'<td class="sc"><span class="{full}">{raw:g}<i>/{cap}</i></span></td>'
                    f'<td class="why"></td></tr>'
                )

    # ---- evidence ---------------------------------------------------------
    ev_items = []
    for item in p2.get("evidence") or []:
        clabel = CRITERION_LABELS.get(item.get("criterion", ""), (item.get("criterion", ""), 0))[0]
        ev_items.append(
            f'<li><div class="ev-h"><span class="tag">{e(clabel)}</span>'
            f'<span class="ts">{e(item.get("timestamp") or "")}</span></div>'
            f'{ar(item.get("quote", ""), "blockquote", "ar q")}'
            f'<p class="ef">{e(item.get("effect", ""))}</p></li>'
        )

    # ---- transcript -------------------------------------------------------
    segs = []
    for s in tr["segments"]:
        if not s["text"]:
            continue
        segs.append(
            f'<li><span class="t">{mmss(s["start_sec"])}</span>'
            f'{ar(s["text"], "p", "ar line")}</li>'
        )

    summary = p2.get("summary") or {}
    trip = p1.get("trip") or {}
    travelers = trip.get("travelers") or {}
    dest = (trip.get("destinations") or [{}])[0]
    commercial = p1.get("commercial") or {}
    usage = ev.get("usage") or {}
    total_tokens = sum(u.get("total_tokens", 0) for u in usage.values())

    warnings_html = ""
    if ev.get("warnings"):
        items = "".join(f"<li>{e(w)}</li>" for w in ev["warnings"])
        warnings_html = (
            f'<div class="note"><span class="nt">Validator output</span>'
            f"<ul class=\"warn\">{items}</ul>"
            f"<p>Caught automatically. The model's own module total disagreed with its "
            f"criterion breakdown, so the breakdown was used — the model never gets to "
            f"report the final number.</p></div>"
        )

    return TEMPLATE.format(
        final=f"{score['final']:.1f}",
        level=e(score["level"]),
        weight_pct=f"{score['weight_applied']:.0%}",
        stage=e(p2.get("stage_reached", "")),
        rows="\n".join(rows),
        evidence="\n".join(ev_items),
        transcript="\n".join(segs),
        strength=ar(summary.get("top_strength", ""), "p", "ar"),
        weakness=ar(summary.get("top_weakness", ""), "p", "ar"),
        recommendation=ar(summary.get("top_recommendation", ""), "p", "ar"),
        summary_ar=ar(p1.get("summary_ar", ""), "p", "ar"),
        customer=ar((p1.get("customer") or {}).get("name") or "—"),
        agent=ar((p2.get("participants") or {}).get("agent_name") or "—"),
        destination=ar(dest.get("name") or "—"),
        nights=e(trip.get("nights") or "—"),
        pax=e(travelers.get("total") or "—"),
        groups=e(trip.get("group_count") or "—"),
        temp=e(commercial.get("lead_temperature") or "unknown"),
        buying=e(commercial.get("buying_stage") or "unknown"),
        duration=mmss(tr["duration_sec"]),
        rate=e(tr["sample_rate"]),
        n_seg=e(len(tr["segments"])),
        tokens=f"{total_tokens:,}",
        warnings=warnings_html,
    )


TEMPLATE = """<title>Call evaluation — q-3009-0500000000</title>
<style>
:root {{
  --paper:#eef2f0; --card:#ffffff; --sunk:#f5f8f7;
  --ink:#101d1b; --body:#3c4b48; --mute:#687875; --faint:#93a29f;
  --rule:#dbe4e1; --rule2:#e7edeb;
  --amber:#a85f18; --amber-bg:#f7ead9;
  --live:#0c7a5f; --live-bg:#dcf0e8;
  --warn:#9a7412; --warn-bg:#f6ecd2;
  --gap:#a8403a; --gap-bg:#f7e2e0;
  --mono:ui-monospace,"Cascadia Mono","JetBrains Mono",Consolas,"SF Mono",Menlo,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,ui-serif,serif;
  --arab:"Segoe UI","Geeza Pro","Noto Naskh Arabic",Tahoma,"Traditional Arabic",serif;
}}
@media (prefers-color-scheme:dark) {{
  :root {{
    --paper:#0c1413; --card:#131e1c; --sunk:#0f1817;
    --ink:#e8efec; --body:#bcc9c6; --mute:#8c9c98; --faint:#63736f;
    --rule:#243230; --rule2:#1c2827;
    --amber:#d99446; --amber-bg:#33260f;
    --live:#3ec99b; --live-bg:#0e2a22;
    --warn:#d6ab3c; --warn-bg:#2c2410;
    --gap:#e2726a; --gap-bg:#2e1614;
  }}
}}
:root[data-theme="light"] {{
  --paper:#eef2f0; --card:#ffffff; --sunk:#f5f8f7;
  --ink:#101d1b; --body:#3c4b48; --mute:#687875; --faint:#93a29f;
  --rule:#dbe4e1; --rule2:#e7edeb;
  --amber:#a85f18; --amber-bg:#f7ead9; --live:#0c7a5f; --live-bg:#dcf0e8;
  --warn:#9a7412; --warn-bg:#f6ecd2; --gap:#a8403a; --gap-bg:#f7e2e0;
}}
:root[data-theme="dark"] {{
  --paper:#0c1413; --card:#131e1c; --sunk:#0f1817;
  --ink:#e8efec; --body:#bcc9c6; --mute:#8c9c98; --faint:#63736f;
  --rule:#243230; --rule2:#1c2827;
  --amber:#d99446; --amber-bg:#33260f; --live:#3ec99b; --live-bg:#0e2a22;
  --warn:#d6ab3c; --warn-bg:#2c2410; --gap:#e2726a; --gap-bg:#2e1614;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--body);
  font-family:var(--sans); font-size:15.5px; line-height:1.55;
  -webkit-font-smoothing:antialiased; }}
.ar {{ font-family:var(--arab); font-size:1.08em; line-height:1.85; }}
:focus-visible {{ outline:2px solid var(--amber); outline-offset:3px; }}
.wrap {{ max-width:1000px; margin:0 auto; padding:0 clamp(1rem,4vw,2rem) 4rem; }}

header.top {{ padding:2.6rem 0 1.4rem; }}
.kick {{ font-family:var(--mono); font-size:.67rem; letter-spacing:.22em;
  text-transform:uppercase; color:var(--amber); margin:0 0 .8rem; }}
h1 {{ font-size:clamp(1.7rem,4vw,2.5rem); font-weight:640; letter-spacing:-.028em;
  line-height:1.06; color:var(--ink); margin:0 0 .5rem; text-wrap:balance; }}
.file {{ font-family:var(--mono); font-size:.8rem; color:var(--mute);
  overflow-wrap:anywhere; margin:0; }}

.meta {{ display:flex; flex-wrap:wrap; gap:.4rem; margin-top:1.3rem;
  padding-top:1.2rem; border-top:1px solid var(--rule); }}
.mc {{ font-family:var(--mono); font-size:.72rem; padding:.3rem .55rem; border-radius:3px;
  background:var(--card); border:1px solid var(--rule); color:var(--body); }}
.mc b {{ color:var(--ink); font-weight:600; }}

/* ---- score hero ---- */
.hero {{ display:grid; gap:1rem; margin:2.2rem 0 0;
  grid-template-columns:minmax(0,1fr); }}
@media (min-width:760px) {{ .hero {{ grid-template-columns:auto minmax(0,1fr); align-items:stretch; }} }}
.big {{ background:var(--card); border:1px solid var(--rule); border-radius:4px;
  border-top:3px solid var(--amber); padding:1.4rem 1.7rem; display:flex;
  flex-direction:column; justify-content:center; }}
.big .n {{ font-size:4.2rem; font-weight:600; line-height:.92; color:var(--ink);
  letter-spacing:-.045em; font-variant-numeric:tabular-nums; }}
.big .lv {{ font-family:var(--mono); font-size:.78rem; letter-spacing:.1em;
  text-transform:uppercase; color:var(--live); margin-top:.5rem; }}
.caveat {{ background:var(--amber-bg); border:1px solid var(--rule);
  border-left:3px solid var(--amber); border-radius:4px; padding:1.1rem 1.3rem; }}
.caveat .t {{ font-family:var(--mono); font-size:.64rem; letter-spacing:.16em;
  text-transform:uppercase; color:var(--amber); display:block; margin-bottom:.45rem; }}
.caveat .pct {{ font-size:2rem; font-weight:620; color:var(--ink); line-height:1;
  letter-spacing:-.03em; font-variant-numeric:tabular-nums; }}
.caveat p {{ margin:.5rem 0 0; font-size:.88rem; max-width:52ch; }}

section {{ padding-top:3rem; }}
.shd {{ display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap;
  padding-bottom:.6rem; margin-bottom:1rem; border-bottom:2px solid var(--ink); }}
.shd h2 {{ font-size:clamp(1.1rem,2.3vw,1.4rem); font-weight:620; letter-spacing:-.02em;
  color:var(--ink); margin:0; }}
.shd .cnt {{ font-family:var(--mono); font-size:.7rem; color:var(--faint); margin-left:auto; }}
.deck {{ font-family:var(--serif); font-size:1rem; line-height:1.6; max-width:66ch;
  margin:0 0 1.3rem; }}
.deck strong {{ color:var(--ink); font-weight:600; }}

/* ---- scorecard ---- */
.tblwrap {{ overflow-x:auto; border:1px solid var(--rule); border-radius:4px; background:var(--card); }}
table {{ width:100%; border-collapse:collapse; font-size:.87rem; }}
tr.m {{ border-top:1px solid var(--rule); }}
tr.m:first-child {{ border-top:none; }}
tr.m th {{ text-align:left; padding:.75rem .9rem .7rem; color:var(--ink); font-weight:620;
  font-size:.95rem; white-space:nowrap; }}
tr.m th .w {{ font-family:var(--mono); font-size:.66rem; color:var(--faint);
  font-weight:500; margin-left:.5rem; }}
tr.m td {{ padding:.75rem .9rem .7rem; vertical-align:middle; }}
td.sc {{ width:5.5rem; text-align:right; font-variant-numeric:tabular-nums; }}
td.sc b {{ font-size:1.25rem; font-weight:620; letter-spacing:-.02em; }}
b.hi {{ color:var(--live); }} b.mid {{ color:var(--amber); }} b.lo {{ color:var(--gap); }}
td.bar {{ width:40%; }}
td.bar i {{ display:block; height:6px; border-radius:3px; background:var(--rule2); position:relative; }}
td.bar i::after {{ content:""; position:absolute; inset:0 auto 0 0; width:var(--p);
  border-radius:3px; background:var(--amber); }}
tr.c {{ background:var(--sunk); }}
tr.c td {{ padding:.3rem .9rem .3rem 1.9rem; font-size:.79rem; color:var(--mute);
  border-top:1px solid var(--rule2); }}
tr.c td.sc span {{ font-family:var(--mono); font-size:.78rem; }}
span.full {{ color:var(--live); }} span.part {{ color:var(--amber); }}
span.zero {{ color:var(--gap); }} span.dash {{ color:var(--faint); }}
tr.c td.sc i {{ font-style:normal; color:var(--faint); font-size:.68rem; }}
td.why {{ font-size:.79rem; color:var(--mute); }}
tr.null th, tr.null td {{ opacity:.95; }}
.chip-null {{ font-family:var(--mono); font-size:.66rem; letter-spacing:.06em;
  text-transform:uppercase; padding:.22rem .5rem; border-radius:3px; white-space:nowrap;
  color:var(--faint); border:1px dashed var(--rule);
  background:repeating-linear-gradient(45deg,transparent,transparent 4px,var(--sunk) 4px,var(--sunk) 8px); }}
tr.null td.why {{ color:var(--body); }}

/* ---- runs ---- */
.runs {{ display:grid; gap:.8rem; grid-template-columns:minmax(0,1fr); }}
@media (min-width:700px) {{ .runs {{ grid-template-columns:repeat(3,1fr); }} }}
.run {{ background:var(--card); border:1px solid var(--rule); border-radius:4px;
  padding:1rem 1.1rem; border-top:3px solid var(--c); }}
.run h4 {{ margin:0 0 .15rem; font-size:.83rem; font-weight:620; color:var(--ink); }}
.run .v {{ font-size:2rem; font-weight:620; color:var(--c); letter-spacing:-.03em;
  line-height:1.1; font-variant-numeric:tabular-nums; }}
.run p {{ margin:.35rem 0 0; font-size:.79rem; color:var(--mute); line-height:1.45; }}

/* ---- evidence ---- */
ul.ev {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.7rem; }}
ul.ev li {{ background:var(--card); border:1px solid var(--rule); border-radius:4px;
  padding:.85rem 1.05rem; }}
.ev-h {{ display:flex; align-items:baseline; gap:.6rem; margin-bottom:.5rem; }}
.tag {{ font-family:var(--mono); font-size:.66rem; letter-spacing:.08em; text-transform:uppercase;
  color:var(--amber); border:1px solid var(--amber); border-radius:3px; padding:.14rem .42rem; }}
.ts {{ font-family:var(--mono); font-size:.72rem; color:var(--faint); margin-left:auto; }}
blockquote.q {{ margin:0; padding:.5rem .9rem; background:var(--sunk); border-radius:3px;
  border-right:3px solid var(--amber); color:var(--ink); }}
p.ef {{ margin:.55rem 0 0; font-size:.83rem; color:var(--mute); }}

/* ---- coaching ---- */
.coach {{ display:grid; gap:.8rem; grid-template-columns:minmax(0,1fr); }}
@media (min-width:820px) {{ .coach {{ grid-template-columns:repeat(3,1fr); }} }}
.cc {{ background:var(--card); border:1px solid var(--rule); border-radius:4px;
  padding:1rem 1.1rem; border-top:3px solid var(--c); }}
.cc .t {{ font-family:var(--mono); font-size:.63rem; letter-spacing:.15em;
  text-transform:uppercase; color:var(--c); display:block; margin-bottom:.5rem; }}
.cc p {{ margin:0; color:var(--ink); }}

/* ---- transcript ---- */
ol.tr {{ list-style:none; margin:0; padding:0; background:var(--card);
  border:1px solid var(--rule); border-radius:4px; max-height:30rem; overflow-y:auto; }}
ol.tr li {{ display:grid; grid-template-columns:3.6rem minmax(0,1fr); gap:.7rem;
  padding:.55rem .9rem; border-top:1px solid var(--rule2); align-items:start; }}
ol.tr li:first-child {{ border-top:none; }}
ol.tr .t {{ font-family:var(--mono); font-size:.71rem; color:var(--faint);
  padding-top:.35rem; font-variant-numeric:tabular-nums; }}
ol.tr p.line {{ margin:0; color:var(--ink); }}

.note {{ background:var(--card); border:1px solid var(--rule); border-left:3px solid var(--warn);
  border-radius:3px; padding:.9rem 1.15rem; margin:1.1rem 0 0; }}
.note .nt {{ display:block; font-family:var(--mono); font-size:.63rem; letter-spacing:.16em;
  text-transform:uppercase; color:var(--warn); margin-bottom:.45rem; }}
.note p {{ margin:.5rem 0 0; font-size:.85rem; max-width:70ch; }}
ul.warn {{ margin:0; padding-left:1.1rem; font-family:var(--mono); font-size:.76rem; }}

.profile {{ display:grid; gap:.5rem; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  background:var(--card); border:1px solid var(--rule); border-radius:4px; padding:1rem 1.1rem; }}
.pf {{ display:flex; flex-direction:column; gap:.15rem; }}
.pf .k {{ font-family:var(--mono); font-size:.64rem; letter-spacing:.12em;
  text-transform:uppercase; color:var(--faint); }}
.pf .v {{ color:var(--ink); font-weight:560; }}

footer {{ margin-top:3.2rem; padding-top:1.1rem; border-top:2px solid var(--ink);
  font-family:var(--mono); font-size:.74rem; color:var(--mute);
  display:flex; flex-wrap:wrap; gap:.5rem 1.4rem; }}
footer b {{ color:var(--ink); font-weight:600; }}
@media (prefers-reduced-motion:reduce) {{ * {{ transition:none !important; animation:none !important; }} }}
</style>

<div class="wrap">
<header class="top">
  <p class="kick">TravelGate · Sales quality · Rubric v1.0.0</p>
  <h1>Call evaluation</h1>
  <p class="file">q-3009-0500000000-20260701-170522-1782914722.226.wav</p>
  <div class="meta">
    <span class="mc"><b>{duration}</b> duration</span>
    <span class="mc"><b>{rate} Hz</b> mono</span>
    <span class="mc"><b>{n_seg}</b> ASR segments</span>
    <span class="mc">diarization <b>none</b></span>
    <span class="mc">stage reached <b>{stage}</b></span>
    <span class="mc">2026-07-01 17:05 <b>+03</b></span>
  </div>
</header>

<div class="hero">
  <div class="big">
    <div class="n">{final}</div>
    <div class="lv">{level}</div>
  </div>
  <div class="caveat">
    <span class="t">Read this number with its denominator</span>
    <div class="pct">{weight_pct}</div>
    <p>Only {weight_pct} of the rubric was actually exercised. Three of the five
    modules scored <em>null</em> — not zero — because the situations they measure
    never arose. Scoring them as full marks, which the original rubric does,
    would put this call at <strong>87.9 “Excellent”</strong> despite no price
    ever being quoted.</p>
  </div>
</div>

<section>
  <div class="shd"><h2>What the agent was actually graded on</h2>
    <span class="cnt">5 modules · 20 criteria</span></div>
  <p class="deck">Grey hatched rows are <strong>not failures</strong>. They are
  situations the conversation never created, so there is nothing to grade. The
  weighted score above is computed over the modules that remain.</p>
  <div class="tblwrap"><table>{rows}</table></div>
  {warnings}
</section>

<section>
  <div class="shd"><h2>The same call, scored three times</h2></div>
  <p class="deck">The first live run of the judge scored this call
  <strong>perfect</strong>. Finding out why is what the test was for.</p>
  <div class="runs">
    <div class="run" style="--c:var(--gap)">
      <h4>Judge, first attempt</h4><div class="v">100.0</div>
      <p>Nulled two criteria it should have scored, so Module 2 collapsed to a
      single perfect criterion. Also claimed an offer was presented while its
      own notes said none was.</p>
    </div>
    <div class="run" style="--c:var(--warn)">
      <h4>After the first fix</h4><div class="v">59.4</div>
      <p>Over-corrected: zeroed the quote promise for lacking a deadline, and
      zeroed value selling for lacking a price. Both were partly earned.</p>
    </div>
    <div class="run" style="--c:var(--live)">
      <h4>Current</h4><div class="v">{final}</div>
      <p>Sub-points now score independently. Hand-scored for comparison:
      <b>83.8</b> — same band, two judgement calls apart.</p>
    </div>
  </div>
</section>

<section>
  <div class="shd"><h2>Evidence behind every deduction</h2>
    <span class="cnt">quotes verified against the transcript</span></div>
  <p class="deck">Each quote is checked to exist verbatim in the transcript before
  the score is stored. A fabricated citation in a coaching report is worse than no
  report — the agent disproves it once and discounts every score after that.</p>
  <ul class="ev">{evidence}</ul>
</section>

<section>
  <div class="shd"><h2>Coaching output</h2><span class="cnt">Arabic, as the rubric requires</span></div>
  <div class="coach">
    <div class="cc" style="--c:var(--live)"><span class="t">Top strength</span>{strength}</div>
    <div class="cc" style="--c:var(--gap)"><span class="t">Top weakness</span>{weakness}</div>
    <div class="cc" style="--c:var(--amber)"><span class="t">Recommendation</span>{recommendation}</div>
  </div>
</section>

<section>
  <div class="shd"><h2>What the customer wants</h2><span class="cnt">pass 1 · extraction</span></div>
  <p class="deck">A separate model call that never sees the agent's score, and whose
  output never influences it.</p>
  <div class="profile">
    <div class="pf"><span class="k">Customer</span><span class="v">{customer}</span></div>
    <div class="pf"><span class="k">Agent</span><span class="v">{agent}</span></div>
    <div class="pf"><span class="k">Destination</span><span class="v">{destination}</span></div>
    <div class="pf"><span class="k">Nights</span><span class="v">{nights}</span></div>
    <div class="pf"><span class="k">Travellers</span><span class="v">{pax}</span></div>
    <div class="pf"><span class="k">Sub-groups</span><span class="v">{groups}</span></div>
    <div class="pf"><span class="k">Lead temp</span><span class="v">{temp}</span></div>
    <div class="pf"><span class="k">Buying stage</span><span class="v">{buying}</span></div>
  </div>
  <div class="note" style="border-left-color:var(--live)">
    <span class="nt" style="color:var(--live)">Summary</span>
    {summary_ar}
  </div>
</section>

<section>
  <div class="shd"><h2>Transcript</h2><span class="cnt">Cohere Transcribe Arabic 07-2026</span></div>
  <p class="deck">The recording is a single mixed channel, so no speaker labels
  exist. Who is the agent and who is the customer is <strong>inferred from
  content</strong>, not measured — the one real weakness in this pipeline, and a
  free fix if the PBX records two channels.</p>
  <ol class="tr">{transcript}</ol>
</section>

<footer>
  <span>judge <b>deepseek-chat</b></span>
  <span>prompt <b>pass2-agent-quality-v1</b></span>
  <span>rubric <b>1.0.0</b></span>
  <span>ASR <b>cohere-transcribe-arabic 07-2026</b></span>
  <span><b>{tokens}</b> tokens both passes</span>
</footer>
</div>
"""


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"written: {OUT}  ({OUT.stat().st_size:,} bytes)")
