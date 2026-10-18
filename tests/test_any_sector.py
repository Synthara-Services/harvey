"""#45: past projects are examples, never a limit on who we serve. Every
writer prompt says we build for every kind of business in any sector, and
that with no matching past project the email still makes a concrete
suggestion and offers a first version of it, naming no product (#52).
"""

import pytest

from harvey.agents.writer import Writer
from harvey.models.prospect import BUSINESS_EMAIL_SOURCE, Prospect
from tests.test_booking_link import RecordingBrain, make_cfg

RULE = (
    "Example Co builds websites, mobile apps, online shops, booking systems, "
    "automations and AI assistants for every kind of business, in any sector. "
    "Past projects are examples, never a limit. When no past project matches "
    "the business, still make a concrete suggestion for that business type and "
    "offer to build a first version of it, without naming a product. Never say "
    "or imply that we only work with certain sectors."
)


def flat(text):
    return " ".join(text.split())


def business(country):
    return Prospect(company="Klinika Dentare Arta", email="info@arta.al",
                    email_status="verified", source=BUSINESS_EMAIL_SOURCE,
                    country=country, industry="dental clinic")


@pytest.mark.parametrize("country", ["AL", "DE"])
@pytest.mark.asyncio
async def test_every_writer_prompt_carries_the_rule_once(country):
    brain = RecordingBrain()
    writer = Writer(brain=brain, state=None, config=make_cfg())
    writer.skills = ""
    await writer._write_personal_email(business(country))
    await writer._write_sequence([business(country)])
    assert len(brain.prompts) == 2
    for prompt in brain.prompts:
        assert flat(prompt).count(RULE) == 1


@pytest.mark.parametrize("country", ["AL", "DE"])
@pytest.mark.asyncio
async def test_with_no_matching_product_the_first_email_still_suggests(country):
    brain = RecordingBrain()
    writer = Writer(brain=brain, state=None, config=make_cfg())
    writer.skills = ""
    await writer._write_personal_email(business(country))
    await writer._write_sequence([business(country)])
    for prompt in brain.prompts:
        assert ("When none matches, name none: the suggestion stands on its own. "
                "Past projects are examples, never a limit on who we work with.") in flat(prompt)
