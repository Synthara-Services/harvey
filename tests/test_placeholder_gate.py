"""The pre-send gate holds any email that still carries an unfilled placeholder.

A model that copies "[LINK]" from an example, or a template variable that was
never rendered, must never reach a prospect, whatever kind of email it is.
"""

import pytest

from harvey.gate import pre_send_check
from harvey.models.prospect import Prospect
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider,
    _now_iso,
    make_sender,
    seed_prospect,
    state,
)

PLACEHOLDERS = [
    "[LINK]", "[Name]", "[Company]", "[First Name]", "[your calendar link]",
    "{{first_name}}", "{{ First Name }}", "{company}", "{First Name}",
]
KINDS = ["sequence", "reply"]   # first emails and follow-ups are "sequence"

SIGNATURE = "Jane Roe\nFounder, Example Co\nexample.com\n1 Example Street, Tirana, Albania"


def prospect():
    return Prospect(first_name="Ana", last_name="Doe", title="Founder",
                    email="ana@example.org", email_status="verified")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_placeholder_in_the_body_is_held(placeholder, kind):
    result = pre_send_check(
        "ana@example.org", "booking flow",
        f"Hi {placeholder}, saw you take bookings by phone. Worth a look?",
        prospect(), kind=kind,
    )
    assert not result.ok
    assert any("placeholder" in r for r in result.reasons)


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_placeholder_in_a_sequence_subject_is_held(placeholder):
    result = pre_send_check(
        "ana@example.org", f"quick idea for {placeholder}", "Short note. Question?",
        prospect(),
    )
    assert not result.ok


@pytest.mark.parametrize("kind", KINDS)
def test_ordinary_text_still_passes(kind):
    body = (
        "Glad to hear it. Pick any time that suits you:\n"
        "https://calendar.example.com/book/abc123\n"
        "Our numbers come from the 2025 season [1].\n\n" + SIGNATURE
    )
    result = pre_send_check("ana@example.org", "Re: booking flow", body, prospect(),
                            kind=kind, booking_url="https://calendar.example.com/book/abc123")
    assert result.ok, result.reasons


def test_a_reply_keeps_the_tag_the_prospects_mail_system_put_on_the_subject():
    result = pre_send_check(
        "ana@example.org", "Re: [EXTERNAL] booking flow", "Thanks, happy to.",
        prospect(), kind="reply",
    )
    assert result.ok, result.reasons


@pytest.mark.parametrize("kind,step", [
    ("sequence", 1),   # first email
    ("sequence", 2),   # follow-up
    ("reply", 1),
])
@pytest.mark.asyncio
async def test_drain_holds_each_kind_of_email(state, kind, step):  # noqa: F811
    pid = await seed_prospect(state, status="contacted" if kind == "reply" else "queued")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", kind=kind, step=step,
        campaign_id="c1" if kind == "sequence" else "",
        conversation_id="v1" if kind == "reply" else "",
        subject="Re: hi" if kind == "reply" else "hello",
        body="Here is my calendar if you want to grab time: [LINK]",
        send_at=_now_iso(), status="approved",
    )
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()

    held = await state.get_outbox(status="failed")
    assert len(held) == 1
    assert held[0]["error"].startswith("gate: unfilled placeholders")
    assert provider.sent == []
