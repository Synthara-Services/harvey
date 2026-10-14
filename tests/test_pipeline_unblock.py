"""The 2026-10-14 deadlock: new prospects the writer can never write for
(no deliverable email) must not stop the scout from prospecting."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.scout import Scout, looks_like_person_name
from harvey.main import decide_next_action
from harvey.models.prospect import Prospect
from harvey.state import DELIVERABLE_EMAIL_STATUSES, StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


async def add(state, i, **kw):
    kw.setdefault("title", "Founder")
    kw.setdefault("source", "web_search")
    await state.add_prospect(Prospect(
        first_name=f"Person{i}", last_name="Test",
        linkedin_url=f"https://al.linkedin.com/in/person-{i}", **kw))


async def decide(state):
    return await decide_next_action(None, state, None)


@pytest.mark.asyncio
async def test_tonight_11_new_prospects_without_email_means_prospect(state):
    for i in range(11):
        await add(state, i)
    summary = await state.get_state_summary()
    assert summary["prospects"]["new"] == 11
    assert summary["writable_prospects"] == 0
    assert await decide(state) == "prospect"


@pytest.mark.asyncio
async def test_a_guessed_email_does_not_count_either(state):
    await add(state, 1, email="a@example.al", email_status="guess")
    await add(state, 2, email="b@example.al", email_status="invalid")
    await add(state, 3, email="c@example.al")          # no status: a guess
    assert await decide(state) == "prospect"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", DELIVERABLE_EMAIL_STATUSES)
async def test_a_deliverable_email_still_means_write_campaign(state, status):
    for i in range(11):
        await add(state, i)
    await add(state, 99, email="owner@example.al", email_status=status)
    assert await state.count_writable_prospects() == 1
    assert await decide(state) == "write_campaign"


@pytest.mark.asyncio
async def test_only_new_prospects_count(state):
    await add(state, 1, email="a@example.al", email_status="verified", status="queued")
    assert await state.count_writable_prospects() == 0
    assert await decide(state) == "prospect"


def test_the_writer_and_the_decision_share_one_rule():
    import harvey.agents.writer as writer
    assert writer.DELIVERABLE_EMAIL_STATUSES is DELIVERABLE_EMAIL_STATUSES
    assert DELIVERABLE_EMAIL_STATUSES == ("verified", "risky")


@pytest.mark.asyncio
async def test_the_scout_no_longer_runs_the_linkedin_profile_search():
    ran = []
    scout = object.__new__(Scout)
    scout.brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    scout.config = SimpleNamespace(channels=SimpleNamespace(
        linkedin=SimpleNamespace(enabled=False)))
    scout.env = SimpleNamespace(linkedin_email="")

    async def log_action(**kw):
        return None

    scout.state = SimpleNamespace(log_action=log_action)
    for name in ("_prospect_via_hunter", "_prospect_via_known_companies",
                 "_prospect_via_hiring_boards", "_prospect_via_profile_search",
                 "_prospect_via_company_discovery", "_prospect_via_linkedin"):
        async def strategy(name=name):
            ran.append(name)
            return 0
        setattr(scout, name, strategy)

    await scout.run()
    assert "_prospect_via_profile_search" not in ran
    assert "_prospect_via_known_companies" in ran


@pytest.mark.parametrize("snippet,url,expected", [
    # Tonight's three: the phrase is rejected, the URL slug decides.
    ("As Founder of Ave Tour DMC", "https://www.linkedin.com/in/eva-rama-31800a22",
     ("Eva", "Rama")),
    ("Tourism Entrepreneur & Security Professional",
     "https://al.linkedin.com/in/alnderimcelohoxhaj", None),
    ("Albania Tour", "https://al.linkedin.com/in/ani-murati-2b53281a2",
     ("Ani", "Murati")),
    # A real name in the snippet still wins, in any alphabet.
    ("Miljan Miljević - Founder & Inbound Tour Operator",
     "https://rs.linkedin.com/in/miljan-m", ("Miljan", "Miljević")),
    ("Brikela Bejko - Founder & CEO @ Sky Tour Albania",
     "https://al.linkedin.com/in/brikela-bejko-575162178", ("Brikela", "Bejko")),
])
def test_linkedin_results_parse_people_not_phrases(snippet, url, expected):
    parsed = object.__new__(Scout)._parse_linkedin_url(url, snippet)
    if expected is None:
        assert parsed is None
    else:
        assert (parsed["first_name"], parsed["last_name"]) == expected


@pytest.mark.parametrize("first,last,ok", [
    ("As", "Founder", False),
    ("Tourism", "Entrepreneur", False),
    ("Albania", "Tour", False),
    ("Head", "Sales", False),
    ("Ilir", "Kena", True),
    ("Gëzim", "Çela", True),
    ("Ilir", "Shkodra", True),          # a surname, not only a city
    ("Jean-Luc", "O'Neil", True),
    ("A", "Kena", False),
    ("Ilir", "K3na", False),
])
def test_looks_like_person_name(first, last, ok):
    assert looks_like_person_name(first, last) is ok
