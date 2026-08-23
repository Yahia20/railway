"""Pass-1 quotes are checked against the conversation, like pass-2's always were.

Two pass-1 fields become actions, not reports:

- `real_ask.is_real_inquiry` puts a salesperson on the phone;
- `promises_made_by_agent` becomes a row in `follow_ups`, and later the basis
  for telling an agent he broke a promise.

Both rest on a quote the model supplies. Until now nothing checked that the
quote was ever said, so an alert could be raised on a sentence nobody uttered —
and the agent can prove it, which costs more trust than the alert was worth.

The verdict goes alongside the model's answer in `pass1_validation`. The
model's own fields are never overwritten: keeping both is what lets you tell a
hallucination from a validator bug afterwards.
"""
import copy

from app.evaluate import judge


CONVERSATION = (
    "[00:03] AGENT: السلام عليكم ترافل جيت مع خالد\n"
    "[00:07] CUSTOMER: عليكم السلام، أبغى عرض لتركيا لعائلة أربعة أشخاص\n"
    "[00:21] AGENT: أبشر، أحسب لك وأرسل لك العرض خلال ساعتين\n"
)


class StubClient:
    def __init__(self, payload, model="stub-model"):
        self.model = model
        self._payload = payload
        self.prompts: list[str] = []

    def complete_json(self, prompt, **_):
        self.prompts.append(prompt)
        return copy.deepcopy(self._payload), {"prompt_tokens": 7, "completion_tokens": 3}


def _payload(**overrides):
    payload = {
        "schema_version": "1.0",
        "intent": "price_inquiry",
        "real_ask": {
            "is_real_inquiry": True,
            "products": ["package"],
            "evidence": [{"quote": "أبغى عرض لتركيا لعائلة أربعة أشخاص", "timestamp": None}],
        },
        "promises_made_by_agent": [
            {"promise": "أحسب لك وأرسل لك العرض خلال ساعتين",
             "timestamp": None, "due_hint": "ساعتين"},
        ],
    }
    payload.update(overrides)
    return payload


def _validate(**overrides):
    return judge.validate_pass1(_payload(**overrides), CONVERSATION)


# ── real_ask ────────────────────────────────────────────────────────────────

def test_a_real_quote_validates():
    v = _validate()
    assert v["real_ask_quote_valid"] is True
    assert v["promises"] == [{"index": 0, "quote_valid": True}]
    assert v["validator_version"] == "span-v2"


def test_a_fabricated_real_ask_quote_fails():
    v = _validate(real_ask={
        "is_real_inquiry": True, "products": ["visa"],
        "evidence": [{"quote": "أبغى تأشيرة شنغن مستعجلة"}]})
    assert v["real_ask_quote_valid"] is False


def test_one_invented_quote_among_several_fails_the_field():
    """This flag books a phone call. Two thirds true is not true."""
    v = _validate(real_ask={
        "is_real_inquiry": True, "products": ["package"],
        "evidence": [{"quote": "أبغى عرض لتركيا لعائلة أربعة أشخاص"},
                     {"quote": "وأبغى أدفع اليوم"}]})
    assert v["real_ask_quote_valid"] is False


def test_a_real_inquiry_claimed_with_no_quote_at_all_fails():
    """The prompt says: if you cannot quote it, the answer is false."""
    v = _validate(real_ask={"is_real_inquiry": True, "products": [], "evidence": []})
    assert v["real_ask_quote_valid"] is False


def test_no_inquiry_and_no_quote_is_null_not_false():
    """`null` means the field was absent, never that it failed."""
    v = _validate(real_ask={"is_real_inquiry": False, "products": [], "evidence": []})
    assert v["real_ask_quote_valid"] is None


def test_a_missing_real_ask_block_is_null():
    payload = _payload()
    del payload["real_ask"]
    assert judge.validate_pass1(payload, CONVERSATION)["real_ask_quote_valid"] is None


# ── promises ────────────────────────────────────────────────────────────────

def test_promises_are_reported_by_index():
    v = _validate(promises_made_by_agent=[
        {"promise": "أحسب لك وأرسل لك العرض خلال ساعتين"},
        {"promise": "هرسل لك الفيزا بكرة الصبح"},          # never said
        {"promise": ""},
    ])
    assert v["promises"] == [
        {"index": 0, "quote_valid": True},
        {"index": 1, "quote_valid": False},
        {"index": 2, "quote_valid": False},
    ]


def test_a_promise_stitched_across_an_asr_gap_is_not_verbatim():
    gapped = "AGENT: أرسل لك العرض [[ASR_GAP]] AGENT: خلال ساعتين"
    v = judge.validate_pass1(
        _payload(promises_made_by_agent=[{"promise": "أرسل لك العرض خلال ساعتين"}]),
        gapped)
    assert v["promises"] == [{"index": 0, "quote_valid": False}]


def test_no_promises_is_an_empty_list_not_null():
    assert _validate(promises_made_by_agent=[])["promises"] == []


def test_an_empty_promise_is_not_rescued_by_a_legacy_quote_field():
    """`promise` is the schema field. `quote` is a legacy shape.

    `item.get("promise") or item.get("quote")` let a present-but-empty `promise`
    fall through to `quote`, so the validator checked a different string from
    the one the follow-up row is built from and reported the promise as verified.
    The fallback is for an ABSENT key, not an empty value.
    """
    v = _validate(promises_made_by_agent=[
        {"promise": "", "quote": "أحسب لك وأرسل لك العرض خلال ساعتين"},
    ])
    assert v["promises"] == [{"index": 0, "quote_valid": False}]


def test_the_legacy_quote_shape_still_validates_when_promise_is_absent():
    v = _validate(promises_made_by_agent=[
        {"quote": "أحسب لك وأرسل لك العرض خلال ساعتين"},
    ])
    assert v["promises"] == [{"index": 0, "quote_valid": True}]


# ── intent evidence ─────────────────────────────────────────────────────────

def test_intent_evidence_is_null_while_the_schema_has_no_such_field():
    assert _validate()["intent_evidence_valid"] is None


def test_intent_evidence_is_validated_as_soon_as_the_schema_grows_one():
    """Guards the day someone adds the field: it must start being checked, not
    keep reporting null forever."""
    good = _validate(intent_evidence=[{"quote": "أبغى عرض لتركيا"}])
    bad = _validate(intent_evidence=[{"quote": "أبغى ألغي الحجز"}])
    assert good["intent_evidence_valid"] is True
    assert bad["intent_evidence_valid"] is False


# ── arabic orthography ──────────────────────────────────────────────────────

def test_a_genuine_quote_survives_an_alef_spelling_drift():
    """ASR spells hamza inconsistently. Rejecting a real quote over one glyph
    teaches the model that quoting is pointless."""
    v = judge.validate_pass1(
        _payload(promises_made_by_agent=[{"promise": "احسب لك وارسل لك العرض خلال ساعتين"}]),
        CONVERSATION)
    assert v["promises"] == [{"index": 0, "quote_valid": True}]


# ── wiring ──────────────────────────────────────────────────────────────────

def test_run_pass1_attaches_the_verdict_without_touching_the_answer():
    client = StubClient(_payload(real_ask={
        "is_real_inquiry": True, "products": ["package"],
        "evidence": [{"quote": "كلام لم يقله أحد"}]}))
    result = judge.run_pass1(CONVERSATION, client=client)

    assert result.validation["real_ask_quote_valid"] is False
    assert result.payload["pass1_validation"] == result.validation
    # The model's own claim is preserved exactly as returned.
    assert result.payload["real_ask"]["is_real_inquiry"] is True
    assert result.payload["real_ask"]["evidence"][0]["quote"] == "كلام لم يقله أحد"
    assert result.prompt_version == "pass1-customer-v5"


def test_the_evaluate_endpoint_exposes_the_flags(monkeypatch):
    """The alert rules read `pass1.pass1_validation` by these exact names."""
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, "worker_api_key", "k", raising=False)
    monkeypatch.setattr(main.settings, "deepseek_api_key", "sk-test", raising=False)
    monkeypatch.setattr(main.judge, "DeepSeekClient",
                        lambda *a, **kw: StubClient(_payload()))

    body = TestClient(main.app).post(
        "/evaluate",
        json={"conversation": CONVERSATION, "input_type": "call_transcript",
              "run_pass1": True, "run_pass2": False},
        headers={"X-API-Key": "k"},
    ).json()

    validation = body["pass1"]["pass1_validation"]
    assert set(validation) == {"real_ask_quote_valid", "promises",
                               "intent_evidence_valid", "validator_version"}
    assert validation["real_ask_quote_valid"] is True
    assert validation["promises"] == [{"index": 0, "quote_valid": True}]
    assert body["pass1"]["payload"]["pass1_validation"] == validation
