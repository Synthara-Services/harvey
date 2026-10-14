"""Every model call that writes to a prospect speaks as the configured persona.

The prompt files used to open with "You are Harvey", so a reply or a cold
email could be written as Harvey while it went out from someone else's
mailbox. These render the real prompt files and check who they say is
writing.
"""

import pytest

from harvey.models.conversation import Conversation, Message
from tests.test_booking_link import Env, RecordingBrain, make_cfg, prospect

PERSONA_OPENING = "You are Jane Roe, Founder at Example Co,"


@pytest.mark.parametrize("intent", ["interested", "question", "objection", "wrong_person"])
@pytest.mark.asyncio
async def test_reply_prompt_opens_with_the_persona(intent):
    from harvey.agents.handler import Handler
    brain = RecordingBrain()
    handler = Handler(brain=brain, state=None, config=make_cfg(), env=Env())
    handler.skills = brain.load_skills_for_agent("handler")
    convo = Conversation(id="c1", prospect_id="p1", thread=[
        Message(sender="harvey", content="Saw you take bookings by phone. Worth a look?"),
        Message(sender="prospect", content="Maybe, what would it involve?"),
    ])
    await handler._generate_response(intent, convo.thread[-1].content, prospect(), convo)

    prompt = brain.prompts[0]
    assert prompt.startswith(PERSONA_OPENING)
    assert "You are Harvey" not in prompt
    assert "{{persona_" not in prompt
    # Our own message in the history carries the persona's name.
    assert "Jane Roe: Saw you take bookings by phone." in prompt
    assert "Harvey:" not in prompt


@pytest.mark.asyncio
async def test_writer_prompts_open_with_the_persona():
    from harvey.agents.writer import Writer
    brain = RecordingBrain()
    writer = Writer(brain=brain, state=None, config=make_cfg())
    writer.skills = brain.load_skills_for_agent("writer")

    await writer._write_personal_email(prospect())
    await writer._write_sequence([prospect()])

    assert len(brain.prompts) == 2   # the first email and the sequence
    for prompt in brain.prompts:
        assert prompt.startswith(PERSONA_OPENING)
        assert "You are Harvey" not in prompt
        assert "{{persona_" not in prompt
