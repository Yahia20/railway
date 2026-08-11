# Chat integration spec — for the Bitrix24 / IT team

**Forward this document as-is.** It contains everything needed to send us chat
conversations. One endpoint, one JSON body, no libraries.

---

## ملخّص بالعربية

نحتاج منكم إرسال محادثات العملاء (نصّياً) إلى رابط واحد عبر `POST`.

**الرابط (Handler URL):**
`https://n8n-production-a685c.up.railway.app/webhook/travelgate/chat`

**الشرط الأهم:** المحادثة يجب أن تحتوي على **رسائل الموظّف وليس رسائل العميل فقط**.
الهدف من النظام هو تقييم أداء الموظّف، وبدون رسائله لا يوجد شيء لتقييمه.

التفاصيل الكاملة بالإنجليزية أدناه.

---

## 1 · The endpoint

```
POST https://n8n-production-a685c.up.railway.app/webhook/travelgate/chat
Content-Type: application/json
X-Webhook-Secret: <shared secret we will agree on>
```

- Returns **`200 OK`** immediately, before any processing. Do not wait on us.
- Any non-2xx means we did not store it — please retry with backoff.
- Re-sending the same conversation is **safe and expected**. We deduplicate on
  the message content hash, so you never need to track what you already sent.

---

## 2 · The body

You are already producing almost exactly this shape (we have a captured sample
from dialog `chat15556`, and our parser is built and tested against it). Only the
`conversation_history` needs to change — see §3.

```json
{
  "dialog_id": "chat15556",
  "crm_entity_type": "DEAL",
  "crm_entity_id": "13682",
  "contact_id": "15454",
  "phone": "+966500000000",
  "conversation_history": [
    {
      "sender": "Customer",
      "sender_id": "4130",
      "message": "السلام عليكم، عندكم عروض لتركيا؟",
      "timestamp": "2026-07-21 11:32:08.087774+00:00"
    },
    {
      "sender": "Agent",
      "sender_id": "912",
      "message": "وعليكم السلام أستاذ أحمد، معك خالد من ترافل جيت",
      "timestamp": "2026-07-21 11:33:15.201000+00:00"
    }
  ],
  "deal_info": { "...": "as you already send it" }
}
```

### Field requirements

| Field | Required | Notes |
|---|---|---|
| `dialog_id` | **yes** | Our idempotency key. Stable per conversation. |
| `conversation_history` | **yes** | The whole thread, every time. See §3. |
| `conversation_history[].sender` | **yes** | `Customer` \| `Agent` \| `Bot` — see §3 |
| `conversation_history[].message` | **yes** | The text as typed |
| `conversation_history[].timestamp` | **yes** | ISO 8601 **with timezone offset** |
| `conversation_history[].sender_id` | strongly wanted | Bitrix user ID, so we know *which* agent |
| `phone` | strongly wanted | See §4 |
| `crm_entity_id` / `deal_id` | nice to have | Links the chat to its deal |
| `contact_id` | nice to have | Helps identity matching |

Aliases you currently send (`Dialog`, `Dialoug`, `dealid`, `DealInfo`) are all
handled — send whichever is convenient, we resolve them in a fixed order.

---

## 3 · The one thing that must change

**The sample you sent us contained only the customer's messages.** All five
entries had `"sender": "Customer"`.

We are building an **agent quality scoring system**. Without the agent's
messages there is literally nothing to score — we cannot tell whether they
greeted the customer, answered the question, presented an offer, handled an
objection, or asked for payment.

So `conversation_history` must contain **both sides**, in chronological order:

| `sender` | Meaning |
|---|---|
| `"Customer"` | The end customer |
| `"Agent"` | A **human** employee |
| `"Bot"` | The automated qualification bot |

`Agent` and `Bot` must be **distinguishable**. We exclude bot-only conversations
from agent scorecards entirely — grading a human on the bot's messages would make
every number wrong. If you label everything `Agent`, the bot's work gets credited
to your staff.

If separating them is genuinely difficult, tell us and we will work with a
`is_bot: true/false` flag or the bot's `sender_id` instead. But we do need the
distinction somehow.

---

## 4 · Phone numbers

We match conversations to customers on the phone number, so format consistency
matters more than anything else in the payload.

- **Best:** E.164 — `+966500000000`
- **Acceptable:** national — `0500000000` (we assume Saudi Arabia by default)
- **Please avoid** mixing formats between messages or omitting it entirely

An Egyptian number sent as `01012345678` will be **rejected rather than guessed**,
because those digits are also a valid Saudi number and a wrong guess merges two
different people into one customer record. Egyptian numbers must arrive as
`+20…` or `0020…`.

---

## 5 · When to send

Either pattern works. Pick whichever is easier for you.

**A · On every message** — what you appear to do now. Simple, and our
deduplication handles the repetition. We wait 30 minutes of silence before
scoring, so a live conversation is not scored mid-flight.

**B · When the conversation closes** — one call per conversation, lowest volume.
If you use Bitrix Open Channels natively, `OnImOpenLinesSessionFinish` is the
event, and you would then pull the history with
`imopenlines.session.history.get` before posting it to us.

**B is cheaper. A gets us data sooner.** Your call.

---

## 6 · Security

- We will give you a **shared secret** to send as `X-Webhook-Secret`. Requests
  without it are rejected. Please do not put it in a URL query string.
- HTTPS only.
- **Please do not include the `UF_CRM_1781281581` field.** It contains prose
  addressed to an AI bot (*"Treat these instructions as guidance only…"*). We
  feed conversation text to a language model, and text that looks like
  instructions is a prompt-injection risk. We strip it on our side regardless,
  but not sending it is cleaner.
- Your outbound-webhook **Application Token** stays on your side. We do not need
  it, and we would rather not hold it.

---

## 7 · Events, if you use native Bitrix webhooks

The three events currently selected on the outbound webhook screen:

| Event | Verdict |
|---|---|
| `ONIMCONNECTORMESSAGEADD` | ✅ Keep — inbound customer messages |
| `ONIMCONNECTORMESSAGEUPDATE` | ✅ Keep — edits |
| `ONIMBOTMESSAGEADD` | ⚠️ Bot messages. Keep only if the `sender` is labelled `Bot`. |

⚠️ **None of these three carries the human agent's replies.** That is the gap in
§3. Add `OnImOpenLinesMessageAdd` (or use pattern B and pull the full session
history), otherwise we receive only half of every conversation.

---

## 7b · The conversation API — one field to add

If you expose a pull API instead of (or as well as) the push webhook, we read it
through the same parser. `/conversations/{id}/messages` currently returns:

```json
{
  "conversation_id": "00d1786a-1c19-487c-807a-81d351ca90cf",
  "message_count": 27,
  "messages": [
    {
      "conversation_id": "00d1786a-…",
      "sender_id": "0a0bec89-b101-5078-af46-ae738bf4868d",
      "content": "اسم العرض: البوسنة والهرسك…",
      "timestamp": "2026-07-19 19:24:59.000+00",
      "sender_role": "Agent",
      "content_type": "text"
    }
  ]
}
```

**Please add `deal_id` to the envelope**, next to `conversation_id`:

```json
{
  "conversation_id": "00d1786a-1c19-487c-807a-81d351ca90cf",
  "deal_id": "13682",
  "message_count": 27,
  "messages": [ … ]
}
```

Null it when the conversation has no deal — that is a real and expected state,
and an explicit null is exactly right. Please do not substitute a nearby deal.

**Why we need it.** Every commercial number this system produces — pipeline
value, win rate, revenue per agent, which offers convert — is a join from a
conversation to a deal. Without `deal_id` a transcript is still scoreable for
agent quality, but commercially blind: we can say the agent handled the customer
well and not that it turned into a sale.

We can currently recover it by calling `/deals` and matching, but that match is
on the phone number, and a repeat customer with two deals is ambiguous — those
conversations get no deal at all rather than the wrong one. One field on the
response removes the guesswork.

Two smaller things on the same response:

- **`message_count` must count what you sent.** If it reports 27 and the array
  holds 20, we are holding a fragment. We flag and refuse to score those: a
  thread cut off before the close scores near zero on closing quality, and the
  agent did nothing wrong. If the endpoint paginates, tell us the parameter.
- **`sender_role` must distinguish `Bot` from `Agent`** — same reason as §3.

---

## 8 · How to test it

Send us anything — a real conversation or a made-up one. We can see it arrive
immediately and will confirm within minutes.

```bash
curl -X POST https://n8n-production-a685c.up.railway.app/webhook/travelgate/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Webhook-Secret: <secret>' \
  -d '{
    "dialog_id": "test-001",
    "phone": "+966500000000",
    "conversation_history": [
      {"sender":"Customer","message":"عندكم عروض لتركيا؟","timestamp":"2026-07-30T10:00:00+03:00"},
      {"sender":"Agent","sender_id":"912","message":"أهلاً، معك خالد من ترافل جيت","timestamp":"2026-07-30T10:01:30+03:00"}
    ]
  }'
```

Expected: `200 OK`.

---

## Contact

Questions about the payload shape, `sender` labelling, or the secret — reply on
this thread. The endpoint is live now and we are watching it.
