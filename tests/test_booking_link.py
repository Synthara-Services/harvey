"""The booking link reaches replies to interested prospects, and only those.

Links in cold email from a new domain hurt deliverability, so the
configured booking_url must never reach the writer's first email or
follow-ups. It goes into the handler's reply when someone shows interest.
"""

from types import SimpleNamespace

import pytest

from harvey.brain import Brain
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect

BOOKING_URL = "https://calendar.example.com/book/abc123"


def make_cfg(booking_url=BOOKING_URL):
    class Cfg:
        class persona:
            name = "Jane Roe"; email = "jane@example.com"; company = "Example Co"
            role = "Founder"; tone = "direct"

        class product:
            name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
            objection_responses = {}
            offer = SimpleNamespace(
                booking_method="calendar_link", booking_url=booking_url,
                meeting_duration="30 minutes", meeting_owner="Jane Roe",
            )

        class channels:
            class email:
                enabled = True
                provider = "smtp"
                max_daily_sends = 50
                send_to_risky = False
                require_approval = True
                max_bounce_rate = 0.05

            class linkedin:
                enabled = False

    return Cfg()


class Env:
    instantly_api_key = ""


class RecordingBrain:
    """The real prompt and skill files; records what would go to the model."""

    load_prompt = Brain.load_prompt
    load_skill = Brain.load_skill
    load_skills_for_agent = Brain.load_skills_for_agent

    def __init__(self):
        self.prompts = []

    async def think(self, prompt, **_k):
        self.prompts.append(prompt)
        return "Glad to hear it."

    async def think_json(self, prompt, **_k):
        self.prompts.append(prompt)
        if "3-email" in prompt:
            return [
                {"step": 1, "subject": "a", "body": "Hi {{first_name}}.", "delay_days": 0},
                {"step": 2, "subject": "b", "body": "Again {{first_name}}.", "delay_days": 3},
                {"step": 3, "subject": "c", "body": "Last one {{first_name}}.", "delay_days": 4},
            ]
        return {"subject": "booking flow", "body": "Saw you book by phone. Worth a look?"}


def prospect():
    return Prospect(
        first_name="Ana", last_name="Doe", title="Founder", company="Acme",
        email="ana@example.org", email_status="verified",
    )


async def reply_prompt(intent, booking_url=BOOKING_URL):
    from harvey.agents.handler import Handler
    brain = RecordingBrain()
    handler = Handler(brain=brain, state=None, config=make_cfg(booking_url), env=Env())
    handler.skills = brain.load_skills_for_agent("handler")
    convo = Conversation(
        id="c1", prospect_id="p1",
        thread=[Message(sender="prospect", content="Sounds interesting, can we talk Thursday?")],
    )
    await handler._generate_response(intent, convo.thread[-1].content, prospect(), convo)
    assert len(brain.prompts) == 1
    return brain.prompts[0]


@pytest.mark.asyncio
async def test_interested_reply_offers_the_call_with_the_exact_link():
    prompt = await reply_prompt("interested")
    assert BOOKING_URL in prompt
    assert "a call of 30 minutes with Jane Roe" in prompt
    assert "on its own line" in prompt


def test_a_reply_with_the_link_and_signature_passes_the_pre_send_gate():
    from harvey.gate import pre_send_check
    from harvey.integrations.mail_provider import with_footer
    body = with_footer(
        f"Glad to hear it. Pick any time that suits you:\n{BOOKING_URL}",
        "Jane Roe\nFounder, Example Co\nexample.com\n1 Example Street, Tirana, Albania",
    )
    result = pre_send_check(
        "ana@example.org", "Re: booking flow", body, prospect(), kind="reply"
    )
    assert result.ok, result.reasons


@pytest.mark.parametrize("intent", ["question", "objection", "wrong_person", "not_interested"])
@pytest.mark.asyncio
async def test_other_replies_never_carry_the_link(intent):
    assert BOOKING_URL not in await reply_prompt(intent)


@pytest.mark.asyncio
async def test_without_a_link_the_interested_reply_suggests_a_next_step():
    prompt = await reply_prompt("interested", booking_url="")
    assert "Suggests a specific next step" in prompt
    assert "booking link" not in prompt


@pytest.mark.asyncio
async def test_writer_prompts_never_carry_the_link():
    from harvey.agents.writer import Writer
    brain = RecordingBrain()
    writer = Writer(brain=brain, state=None, config=make_cfg())
    writer.skills = brain.load_skills_for_agent("writer")

    first = await writer._write_personal_email(prospect())
    sequence = await writer._write_sequence([prospect()])

    assert first and len(sequence) == 3   # both prompts really went out
    assert len(brain.prompts) == 2
    for prompt in brain.prompts:
        assert BOOKING_URL not in prompt
