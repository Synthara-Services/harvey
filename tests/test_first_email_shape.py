"""Every first email observes, suggests and invites, in English and in
Albanian. Follow-ups end with the same reply-or-book invitation. The
configured booking link is the only link an email carries: the model
writes a placeholder, the writer fills in the link, and the pre-send gate
holds any other link.
"""

from types import SimpleNamespace

import pytest

from harvey.agents.writer import BOOKING_PLACEHOLDER, Writer
from harvey.gate import configured_booking_url, pre_send_check
from harvey.integrations.mail_provider import with_footer
from harvey.models.prospect import BUSINESS_EMAIL_SOURCE, Prospect
from tests.test_booking_link import BOOKING_URL, RecordingBrain, make_cfg
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider,
    _now_iso,
    make_sender,
    seed_prospect,
    state,
)

SIGNATURE = "Jane Roe\nFounder, Example Co\nexample.com\n1 Example Street, Tirana, Albania"

DRAFTS = {
    "en": (
        "Hotel Mira's website takes bookings only by phone or email.\n\n"
        "A site with direct booking and online payment would let guests book "
        "at night too, and Example Co builds exactly this for hotels like yours.\n\n"
        "Reply here, book a free scoping call of 30 minutes, or see example.com:\n"
        "{{booking_url}}"
    ),
    "sq": (
        "Faqja e Hotel Mira-s i merr rezervimet vetëm me telefon ose email.\n\n"
        "Një faqe me rezervim të drejtpërdrejtë dhe pagesë online do t'ju sillte "
        "rezervime edhe natën, dhe Example Co ndërton pikërisht këtë për hotele si i juaji.\n\n"
        "Më ktheni përgjigje këtu, rezervoni një telefonatë falas prej 30 minutash, "
        "ose shihni example.com:\n"
        "{{booking_url}}"
    ),
}
# (country, language code, language name)
LANGUAGES = [("DE", "en", "English"), ("AL", "sq", "Albanian")]


class DraftBrain(RecordingBrain):
    """Returns a draft in the prospect's language, and records the prompt."""

    def __init__(self, body):
        super().__init__()
        self.body = body

    async def think_json(self, prompt, **_k):
        self.prompts.append(prompt)
        if "3-email" in prompt:
            return [
                {"step": n, "subject": s, "body": self.body, "delay_days": d}
                for n, s, d in ((1, "a", 0), (2, "b", 3), (3, "c", 4))
            ]
        return {"subject": "direct booking", "body": self.body}


def business(country):
    return Prospect(
        company="Hotel Mira", email="info@hotelmira.al", email_status="verified",
        source=BUSINESS_EMAIL_SOURCE, country=country, industry="hotels",
    )


def make_writer(body, booking_url=BOOKING_URL):
    brain = DraftBrain(body)
    writer = Writer(brain=brain, state=None, config=make_cfg(booking_url=booking_url))
    writer.skills = ""
    return writer, brain


def links_in(body):
    return [line for line in body.splitlines() if "http" in line]


def flat(text):
    return " ".join(text.split())


# ── the prompts ──


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_the_first_email_prompt_asks_to_observe_suggest_and_invite(country, code, name):
    writer, brain = make_writer(DRAFTS[code])
    await writer._write_personal_email(business(country))
    prompt = flat(brain.prompts[0])
    assert "Under 110 words before the signature, in three parts" in prompt
    assert "One specific observation from the FACTS above" in prompt
    assert "concrete suggestion tied to that observation" in prompt
    assert "Example Co builds exactly this for businesses like theirs" in prompt
    assert "only when the product knowledge says it is live" in prompt
    assert ("reply to this email, book the free scoping call of 30 minutes at "
            "the booking link, or visit our website") in prompt
    assert f"Put {BOOKING_PLACEHOLDER} on its own line" in prompt
    assert "no competitor names" in prompt
    assert f"Write it in {name}." in prompt
    assert BOOKING_URL not in prompt, "the model never sees the link itself"


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_follow_ups_end_with_the_reply_or_book_invitation(country, code, name):
    writer, brain = make_writer(DRAFTS[code])
    await writer._write_sequence([business(country)])
    prompt = flat(brain.prompts[0])
    assert "- Email 1: Under 110 words before the signature" in prompt
    invitation = ("It ends with one invitation naming every way in: reply to this "
                  "email, or book the free scoping call of 30 minutes at the "
                  "booking link.")
    assert prompt.count(invitation) == 2, "email 2 and email 3"
    assert f"Write all three emails in {name}" in prompt
    assert BOOKING_URL not in prompt


def test_the_writer_prompt_file_allows_the_booking_link_and_nothing_else():
    brain = RecordingBrain()
    text = brain.load_prompt("writer", persona_company="Example Co")
    assert "Email 1 MUST be under 110 words before the signature" in text
    assert "write {{booking_url}} on its own line" in text
    assert "Never write any other URL" in text
    assert "No links in email 1" not in text
    assert "Never name a competitor." in text


# ── the rendered emails ──


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_the_first_email_carries_the_booking_link_once_and_passes_the_gate(
        country, code, name):
    writer, _ = make_writer(DRAFTS[code])
    draft = await writer._write_personal_email(business(country))
    body = draft["body"]
    assert body.startswith("Përshëndetje,\n\n" if code == "sq" else "Hello Hotel Mira team,\n\n")
    assert links_in(body) == [BOOKING_URL], "exactly the configured link, on its own line"
    assert BOOKING_PLACEHOLDER not in body
    assert "example.com" in body, "the website as plain text"
    result = pre_send_check(
        "info@hotelmira.al", draft["subject"], with_footer(body, SIGNATURE),
        business(country), booking_url=BOOKING_URL,
    )
    assert result.ok, result.reasons


@pytest.mark.parametrize("country,code,name", LANGUAGES)
@pytest.mark.asyncio
async def test_every_step_of_the_sequence_carries_the_booking_link_once(country, code, name):
    writer, _ = make_writer(DRAFTS[code])
    steps = await writer._write_sequence([business(country)])
    assert len(steps) == 3
    for step in steps:
        assert links_in(step.body) == [BOOKING_URL]
        assert step.body.startswith("{{greeting}}\n\n")


@pytest.mark.asyncio
async def test_a_draft_that_forgot_the_placeholder_gets_the_link_as_its_last_line():
    writer, _ = make_writer("Saw you take bookings by phone. Reply here or book a call.")
    draft = await writer._write_personal_email(business("DE"))
    assert draft["body"].endswith("Reply here or book a call.\n" + BOOKING_URL)


@pytest.mark.asyncio
async def test_a_second_placeholder_never_makes_a_second_link():
    writer, _ = make_writer(
        "Book at {{booking_url}} or reply here.\n{{ booking_url }}")
    draft = await writer._write_personal_email(business("DE"))
    assert links_in(draft["body"]) == [BOOKING_URL]
    assert "Book at or reply here." in draft["body"]


@pytest.mark.parametrize("bad", [
    "See https://example.com/work for what we built.\n{{booking_url}}",
    "Book here: https://calendly.com/someone-else",
])
@pytest.mark.asyncio
async def test_a_draft_with_any_other_link_is_dropped(bad):
    writer, _ = make_writer(bad)
    assert await writer._write_personal_email(business("DE")) is None
    assert await writer._write_sequence([business("DE")]) == []


@pytest.mark.asyncio
async def test_without_a_booking_link_the_invitation_offers_a_reply_and_no_link():
    writer, brain = make_writer(DRAFTS["en"], booking_url="")
    draft = await writer._write_personal_email(business("DE"))
    prompt = flat(brain.prompts[0])
    assert "No booking link is configured: write no link." in prompt
    assert f"Put {BOOKING_PLACEHOLDER} on its own line" not in prompt
    assert links_in(draft["body"]) == []
    assert BOOKING_PLACEHOLDER not in draft["body"]
    assert draft["body"].endswith("or see example.com:")


# ── the gate ──


def gate(body, booking_url=BOOKING_URL, kind="sequence"):
    return pre_send_check(
        "info@hotelmira.al", "direct booking", with_footer(body, SIGNATURE),
        business("AL"), kind=kind, booking_url=booking_url,
    )


@pytest.mark.parametrize("kind", ["sequence", "reply"])
def test_the_gate_passes_the_booking_link_and_a_plain_website(kind):
    assert gate(f"Reply here, or book a call:\n{BOOKING_URL}", kind=kind).ok
    assert gate(f"Book a call at {BOOKING_URL}.", kind=kind).ok, "a full stop after it"
    assert gate("No link at all, just example.com.", kind=kind).ok


@pytest.mark.parametrize("body", [
    "See https://example.com for more.",
    f"Book here: {BOOKING_URL}x",
    f"Book here: {BOOKING_URL}/other",
    "Book here: http://calendar.example.com/book/abc123",
])
def test_the_gate_holds_any_link_but_the_booking_link(body):
    result = gate(body)
    assert not result.ok
    assert any(r.startswith("a link other than the booking link") for r in result.reasons)


def test_the_gate_holds_a_second_booking_link():
    result = gate(f"Book here: {BOOKING_URL}\nOr here: {BOOKING_URL}")
    assert result.reasons == ["too many links (2 > 1)"]


def test_without_a_configured_booking_link_every_link_is_held():
    assert not gate(f"Book here: {BOOKING_URL}", booking_url="").ok


def test_the_booking_link_comes_from_product_offer():
    assert configured_booking_url(make_cfg()) == BOOKING_URL
    assert configured_booking_url(make_cfg(booking_url="  ")) == ""
    assert configured_booking_url(SimpleNamespace(product=SimpleNamespace())) == ""


@pytest.mark.asyncio
async def test_the_sender_holds_the_link_the_config_does_not_name(state, monkeypatch):  # noqa: F811
    good = await seed_prospect(state, email="ana@acme.com", status="queued")
    bad = await seed_prospect(state, email="ben@acme.com", status="queued")
    for pid, email, link in ((good, "ana@acme.com", BOOKING_URL),
                             (bad, "ben@acme.com", "https://example.com/elsewhere")):
        await state.add_outbox_item(
            prospect_id=pid, to_email=email, kind="sequence", step=1,
            campaign_id="c1", subject="direct booking",
            body=f"Reply here, or book a call:\n{link}",
            send_at=_now_iso(), status="approved",
        )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    monkeypatch.setattr(sender.config.product, "offer",
                        SimpleNamespace(booking_url=BOOKING_URL), raising=False)
    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["ana@acme.com"]
    held = await state.get_outbox(status="failed")
    assert [i["to_email"] for i in held] == ["ben@acme.com"]
    assert held[0]["error"].startswith("gate: a link other than the booking link")


def test_the_shipped_skills_give_the_writer_the_same_link_rule():
    from tests.test_skills_voice import skills_for
    for agent in ("writer", "handler"):
        text = skills_for(agent)
        assert "Never in a first email or a follow-up" not in text
        assert "zero in email 1" not in text
    assert "that link is the booking link you were given" in skills_for("writer")
