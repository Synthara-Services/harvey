"""The skills the writer and handler load speak to the sender as "you", and
every email states the configured call length, never another one.

Since the prompts open with the persona, a skill that still says "Harvey"
contradicts who is writing. A sample sentence such as "grab 15 minutes:
[LINK]" is copied by the model, so the skills carry no fill-in examples and
no call length of their own; the length comes from product.offer.
"""

import re

import pytest

from harvey.gate import BRACKET_PLACEHOLDER_RE
from harvey.models.conversation import Conversation, Message
from tests.test_booking_link import BOOKING_URL, Env, RecordingBrain, make_cfg, prospect

# Written by the trainer per install and gitignored, so not part of the repo.
LOCAL_SKILLS = {"product_knowledge", "competitive_intel"}
CALL_LENGTH_RE = re.compile(r"\b(\d+)\s*-?\s*min(?:ute)?s?\b", re.IGNORECASE)
AGENTS = ["writer", "handler"]


class ShippedSkillsBrain(RecordingBrain):
    """Only the skill files shipped in the repo, so every machine sees the same."""

    def load_skill(self, name):
        return "" if name in LOCAL_SKILLS else super().load_skill(name)


def skills_for(agent):
    text = ShippedSkillsBrain().load_skills_for_agent(agent)
    assert text  # the skills really loaded
    return text


# ── the skill files ──


@pytest.mark.parametrize("agent", AGENTS)
def test_skills_never_call_the_sender_harvey(agent):
    assert "Harvey" not in skills_for(agent)


def test_skills_carry_no_instantly_wording():
    assert "Instantly" not in skills_for("writer")


@pytest.mark.parametrize("agent", AGENTS)
def test_skills_show_no_bracket_placeholders(agent):
    assert BRACKET_PLACEHOLDER_RE.findall(skills_for(agent)) == []


@pytest.mark.parametrize("agent", AGENTS)
def test_skills_name_no_call_length_of_their_own(agent):
    assert CALL_LENGTH_RE.findall(skills_for(agent)) == []


# ── the rendered prompts ──


async def writer_prompts(meeting_duration="40 minutes"):
    from harvey.agents.writer import Writer
    brain = ShippedSkillsBrain()
    writer = Writer(brain=brain, state=None, config=make_cfg(meeting_duration=meeting_duration))
    writer.skills = brain.load_skills_for_agent("writer")
    await writer._write_personal_email(prospect())
    await writer._write_sequence([prospect()])
    assert len(brain.prompts) == 2   # the first email and the sequence
    return brain.prompts


async def reply_prompt(intent, meeting_duration="40 minutes"):
    from harvey.agents.handler import Handler
    brain = ShippedSkillsBrain()
    handler = Handler(brain=brain, state=None, env=Env(),
                      config=make_cfg(meeting_duration=meeting_duration))
    handler.skills = brain.load_skills_for_agent("handler")
    convo = Conversation(id="c1", prospect_id="p1", thread=[
        Message(sender="prospect", content="Maybe, what would it involve?"),
    ])
    await handler._generate_response(intent, convo.thread[-1].content, prospect(), convo)
    return brain.prompts[0]


@pytest.mark.asyncio
async def test_writer_prompts_carry_the_configured_call_length_only():
    for prompt in await writer_prompts():
        assert "a call of 40 minutes" in prompt
        assert set(CALL_LENGTH_RE.findall(prompt)) == {"40"}
        assert BOOKING_URL not in prompt


@pytest.mark.parametrize("intent", ["interested", "question", "objection", "wrong_person", "not_interested"])
@pytest.mark.asyncio
async def test_every_reply_carries_the_configured_call_length_only(intent):
    prompt = await reply_prompt(intent)
    assert "40 minutes" in prompt
    assert set(CALL_LENGTH_RE.findall(prompt)) == {"40"}
    # The link itself still goes only to someone who showed interest.
    assert (BOOKING_URL in prompt) == (intent == "interested")


@pytest.mark.asyncio
async def test_without_a_configured_length_no_length_is_given():
    for prompt in await writer_prompts(meeting_duration=""):
        assert CALL_LENGTH_RE.findall(prompt) == []
    assert "If it mentions a call" not in await reply_prompt("question", meeting_duration="")
