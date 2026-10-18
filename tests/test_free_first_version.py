"""#52: every sequence offers to build a first version of the suggestion for
the business at no cost, so they can see it working before any talk of
price. Email 1 says it in two sentences after the suggestion, email 2
repeats it in one beside a past project, and email 3 ends with it in one
line before the invitation.

One rule for words that mean free, in English and in Albanian: the body
may carry them, for the free first version and the free scoping call, and
a subject line never does. The writer drops a draft whose subject does,
and the pre-send gate holds one.
"""

import pytest

from harvey.agents.sender import Sender
from harvey.agents.writer import FIRST_VERSION_OFFER, Writer
from harvey.gate import free_word, pre_send_check
from harvey.integrations.mail_provider import with_footer
from tests.test_booking_link import BOOKING_URL, RecordingBrain, make_cfg
from tests.test_first_email_shape import LANGUAGES, SIGNATURE, business, flat

# The words the offer stays close to, with the business's name in them.
OFFERS = {
    "en": ("If you'd like, we'll build a first version of this for Hotel Mira at "
           "no cost, so you can see it working. If it proves useful, we talk about "
           "price afterwards."),
    "sq": ("Nëse doni, e ndërtojmë një version të parë për Hotel Mira pa pagesë, "
           "që ta shihni vetë si funksionon. Nëse ju duket i dobishëm, flasim "
           "pastaj për çmimin."),
}

# A sequence in the new shape, as the model writes it, in each language.
SEQUENCES = {
    "en": [
        ("direct booking",
         "Your website takes bookings only by phone.\n\n"
         "A site with direct booking would let guests book at night too. If you'd "
         "like, we'll build a first version of it for {{company}} at no cost, so "
         "you can see it working. If it proves useful, we talk about price "
         "afterwards.\n\n"
         "Reply here, book the free scoping call of 30 minutes, or see example.com:\n"
         "{{booking_url}}"),
        ("booking in three languages",
         "Vlora Tours, a booking platform we built, takes direct bookings in three "
         "languages.\n\nThe offer stands: a first version for {{company}} at no "
         "cost, and the price only if it proves useful.\n\n"
         "Reply here, or book the free scoping call:\n{{booking_url}}"),
        ("leaving it here",
         "Leaving it here for now.\n\n"
         "The free first version for {{company}} still stands. Reply here, or "
         "book the call:\n{{booking_url}}"),
    ],
    "sq": [
        ("rezervime direkte",
         "Faqja juaj merr rezervime vetëm me telefon.\n\n"
         "Një faqe me rezervim online do t'ju sillte rezervime edhe natën. Nëse "
         "doni, e ndërtojmë një version të parë për {{company}} pa pagesë, që ta "
         "shihni vetë si funksionon. Nëse ju duket i dobishëm, flasim pastaj për "
         "çmimin.\n\n"
         "Më ktheni përgjigje, rezervoni telefonatën falas prej 30 minutash, ose "
         "shihni example.com:\n{{booking_url}}"),
        ("rezervime në tri gjuhë",
         "Vlora Tours, platforma e rezervimeve që ndërtuam, merr rezervime direkte "
         "në tri gjuhë.\n\nOferta mbetet: një version i parë për {{company}} pa "
         "pagesë, dhe çmimi vetëm nëse ju duket i dobishëm.\n\n"
         "Më ktheni përgjigje, ose rezervoni telefonatën:\n{{booking_url}}"),
        ("po e lë këtu",
         "Po e lë këtu për tani.\n\n"
         "Versioni i parë falas për {{company}} mbetet. Më ktheni përgjigje, ose "
         "rezervoni telefonatën:\n{{booking_url}}"),
    ],
}


class SequenceBrain(RecordingBrain):
    """Returns ``steps`` as the sequence and the first one as email 1."""

    def __init__(self, steps):
        super().__init__()
        self.steps = steps

    async def think_json(self, prompt, **_k):
        self.prompts.append(prompt)
        if "3-email" in prompt:
            return [
                {"step": n, "subject": s, "body": b, "delay_days": d}
                for n, (s, b), d in zip((1, 2, 3), self.steps, (0, 3, 4))
            ]
        subject, body = self.steps[0]
        return {"subject": subject, "body": body.replace("{{company}}", "Hotel Mira")}


def make_writer(steps):
    brain = SequenceBrain(steps)
    writer = Writer(brain=brain, state=None, config=make_cfg())
    writer.skills = ""
    return writer, brain


def gate(subject, body, kind="sequence", country="AL"):
    return pre_send_check(
        "info@hotelmira.al", subject, with_footer(body, SIGNATURE),
        business(country), kind=kind, booking_url=BOOKING_URL,
    )


# ── the prompts ──


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_the_first_email_offers_a_first_version_after_the_suggestion(
        country, code, name):
    writer, brain = make_writer(SEQUENCES[code])
    await writer._write_personal_email(business(country))
    prompt = flat(brain.prompts[0])
    assert OFFERS[code] in prompt, "the words to stay close to, in the email's language"
    other = "sq" if code == "en" else "en"
    assert OFFERS[other] not in prompt
    assert "builds exactly this" not in prompt
    suggestion = prompt.index("2. One sentence with a concrete suggestion")
    offer = prompt.index("3. In two sentences, the offer to build a first version "
                         "of the suggestion for Hotel Mira at no cost")
    invitation = prompt.index("4. One invitation naming every way in")
    assert suggestion < offer < invitation
    assert "talk about the price together only if they find it useful" in prompt


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_every_email_of_the_sequence_carries_the_offer(country, code, name):
    writer, brain = make_writer(SEQUENCES[code])
    await writer._write_sequence([business(country)])
    prompt = flat(brain.prompts[0])
    assert FIRST_VERSION_OFFER[code].format(name="{{company}}") in prompt, "email 1"
    assert ("- Email 2: Follow-up 3 days later. Different angle: one past project "
            "of ours from the product knowledge as the example") in prompt
    assert ("Then the offer from email 1 again in one sentence: a first version "
            "for {{company}} at no cost, and the price only if it proves useful. "
            "Under 75 words.") in prompt
    assert ("Under 40 words. Gracious, not guilt-tripping. It ends with the offer "
            "in one line (the first version for {{company}} at no cost still "
            "stands), then one invitation") in prompt


def test_the_writer_prompt_has_one_rule_for_free():
    text = RecordingBrain().load_prompt("writer", persona_company="Example Co")
    never = next(line for line in text.splitlines() if line.startswith('- "guarantee"'))
    assert '"free"' not in never and '"no cost"' not in never
    assert '"guarantee"' in never and '"risk-free"' in never
    assert ('Words for free of charge ("free", "no cost", "at no cost", and in '
            'Albanian "falas" and "pa pagesë") may appear in the body, for the free '
            'first version and the free scoping call. Never in a subject line.') in text
    assert 'the word "free" may appear once' not in text
    rule = next(line for line in text.splitlines() if line.startswith("19. "))
    assert "a preview or a clickable mockup" in rule
    assert "never promise a finished product" in rule
    assert 'no "guarantee", no pressure' in rule


def test_prompts_and_skills_are_read_as_utf8(tmp_path, monkeypatch):
    """The Albanian offer reaches the model as written on every platform,
    Windows included, where the default encoding is not UTF-8."""
    import harvey.brain as brain_module
    text = f"{OFFERS['sq']} — çmimi"
    (tmp_path / "offer.md").write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(brain_module, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(brain_module, "SKILLS_DIR", tmp_path)
    brain = RecordingBrain()
    assert brain.load_prompt("offer") == text
    assert brain.load_skill("offer") == text


# ── the rendered emails ──


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_a_rendered_sequence_with_the_offer_passes_the_gate(country, code, name):
    writer, _ = make_writer(SEQUENCES[code])
    steps = await writer._write_sequence([business(country)])
    assert [s.step for s in steps] == [1, 2, 3]
    for step in steps:
        subject = Sender._render(None, step.subject, business(country))
        body = Sender._render(None, step.body, business(country))
        assert "Hotel Mira" in body, "the offer names the business"
        assert any(w in body for w in ("no cost", "free", "pa pagesë", "falas"))
        result = gate(subject, body, country=country)
        assert result.ok, (step.step, result.reasons)


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_a_rendered_first_email_with_the_offer_passes_the_gate(country, code, name):
    writer, _ = make_writer(SEQUENCES[code])
    draft = await writer._write_personal_email(business(country))
    offer = {"en": "for Hotel Mira at no cost", "sq": "për Hotel Mira pa pagesë"}[code]
    assert offer in flat(draft["body"])
    assert gate(draft["subject"], draft["body"], country=country).ok


@pytest.mark.parametrize("country,code,name,subject", [
    ("DE", "en", "English", "free first version"),
    ("AL", "sq", "Albanian", "version pa pagesë"),
])
@pytest.mark.asyncio
async def test_the_writer_drops_a_draft_with_free_in_the_subject(country, code, name, subject):
    steps = [(subject, SEQUENCES[code][0][1]), *SEQUENCES[code][1:]]
    writer, _ = make_writer(steps)
    assert await writer._write_personal_email(business(country)) is None
    assert await writer._write_sequence([business(country)]) == []
    later = [*SEQUENCES[code][:2], (subject, SEQUENCES[code][2][1])]
    writer, _ = make_writer(later)
    assert await writer._write_sequence([business(country)]) == [], "any step's subject"


# ── the gate ──


@pytest.mark.parametrize("subject", [
    "free first version", "Free preview", "a first version at no cost",
    "no charge", "version pa pagesë", "version pa pagese", "Version Pa Pagesë",
    "provë falas", "gratis për ju", "pa kosto",
])
def test_the_gate_holds_a_word_for_free_in_the_subject(subject):
    result = gate(subject, "Reply here.")
    assert not result.ok
    assert any(r.startswith("a word for free in the subject") for r in result.reasons)


@pytest.mark.parametrize("subject", [
    "direct booking", "freelance help", "carefree stays", "rezervime direkte",
])
def test_the_gate_passes_a_subject_with_no_word_for_free(subject):
    assert not free_word(subject)
    assert gate(subject, "Reply here.").ok


def test_a_reply_keeps_the_prospects_own_subject():
    assert gate("Re: free first version?", "Glad to. Reply here with a time.",
                kind="reply").ok


@pytest.mark.parametrize("body", [
    "If you'd like, we'll build a first version for Hotel Mira at no cost. "
    "Reply here, or book the free scoping call.",
    "Nëse doni, e ndërtojmë një version të parë pa pagesë. Rezervoni telefonatën falas.",
])
def test_a_word_for_free_in_the_body_passes_the_gate(body):
    assert gate("direct booking", body).ok
