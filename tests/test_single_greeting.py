"""#41: an email to a business's own inbox opens with its greeting exactly
once. The writer's sequence prompt asks for {{greeting}} at the top, the
model writes it, and the writer must recognise that line instead of
putting a second one in front of it. The pre-send gate holds any body
whose first two lines are the same.
"""

import pytest

from harvey.agents.writer import Writer
from harvey.gate import pre_send_check
from harvey.greeting import with_greeting
from harvey.models.prospect import BUSINESS_EMAIL_SOURCE, Prospect
from tests.test_booking_link import RecordingBrain, make_cfg
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider,
    _now_iso,
    make_sender,
    seed_prospect,
    state,
)

# What the model returned for a business sequence on 2026-10-15: it starts
# every body with {{greeting}}, as the prompt asks.
STEPS = [
    {"step": 1, "subject": "pyetjet për ilaçe", "delay_days": 0,
     "body": "{{greeting}}\n\nTe {{company}}, pyetjet vijnë me telefon apo në WhatsApp?"},
    {"step": 2, "subject": "porosi nga celulari", "delay_days": 3,
     "body": "{{greeting}}\n\nShumë klientë do të porosisnin nga celulari."},
    {"step": 3, "subject": "e mbyll këtu", "delay_days": 4,
     "body": "{{greeting}}\n\nNëse {{company}} vendos, mjafton një përgjigje."},
]


class SequenceBrain(RecordingBrain):
    async def think_json(self, prompt, **_k):
        self.prompts.append(prompt)
        return [dict(s) for s in STEPS]


def business(country):
    return Prospect(
        company="Nova Pharm", email="info@novapharm.org", email_status="verified",
        source=BUSINESS_EMAIL_SOURCE, country=country, industry="pharmacy",
    )


def top_lines(body, n=2):
    return [line for line in body.splitlines() if line.strip()][:n]


@pytest.mark.parametrize("body", [
    "{{greeting}}\n\nText.",
    "{{ greeting }}\n\nText.",
    "{{greeting}}\n\n{{greeting}}\n\nText.",
    "{{greeting}}\n\nPërshëndetje,\n\nText.",
    "Hello Nova Pharm team,\n\n{{greeting}}\n\nText.",
])
def test_every_greeting_line_at_the_top_is_replaced_by_one(body):
    assert with_greeting(body, "{{greeting}}") == "{{greeting}}\n\nText."


def test_a_first_line_that_is_not_a_greeting_stays():
    assert with_greeting("Te {{company}}, ju shkruaj.", "Përshëndetje,") == (
        "Përshëndetje,\n\nTe {{company}}, ju shkruaj.")


@pytest.mark.parametrize("country,greeting", [
    ("XK", "Përshëndetje,"),
    ("DE", "Hello Nova Pharm team,"),
])
@pytest.mark.asyncio
async def test_each_step_renders_the_greeting_once(country, greeting):
    writer = Writer(brain=SequenceBrain(), state=None, config=make_cfg())
    writer.skills = ""
    steps = await writer._write_sequence([business(country)])
    assert len(steps) == 3
    sender = make_sender(None, FakeProvider())
    for step in steps:
        assert step.body.count("{{greeting}}") == 1
        body = sender._render(step.body, business(country))
        assert body.count(greeting) == 1
        assert top_lines(body, 1) == [greeting]
        result = pre_send_check("info@novapharm.org", step.subject, body, business(country))
        assert result.ok, result.reasons


@pytest.mark.parametrize("body", [
    "Përshëndetje,\n\nPërshëndetje,\n\nShumë klientë do të porosisnin.",
    "Hello Albanian Trip team,\n\nHello Albanian Trip team,\n\nA lot of tour operators.",
    "Hi Ana,\n  \nhi ana,\nText.",
])
def test_the_gate_holds_a_body_whose_first_two_lines_are_the_same(body):
    result = pre_send_check("info@novapharm.org", "porosi", body, business("XK"))
    assert not result.ok
    assert any(r.startswith("the first two lines are the same") for r in result.reasons)


def test_the_gate_passes_a_greeting_followed_by_text():
    result = pre_send_check(
        "info@novapharm.org", "porosi",
        "Përshëndetje,\n\nShumë klientë do të porosisnin.\n\nShumë klientë do të porosisnin.",
        business("XK"))
    assert result.ok, result.reasons


@pytest.mark.asyncio
async def test_the_drain_holds_a_doubled_greeting(state):  # noqa: F811
    pid = await seed_prospect(state, status="contacted")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", kind="sequence", step=2,
        campaign_id="c1", subject="porosi",
        body="Përshëndetje,\n\nPërshëndetje,\n\nShumë klientë do të porosisnin.",
        send_at=_now_iso(), status="approved",
    )
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()

    held = await state.get_outbox(status="failed")
    assert len(held) == 1
    assert held[0]["error"] == "gate: the first two lines are the same: 'Përshëndetje,'"
    assert provider.sent == []
