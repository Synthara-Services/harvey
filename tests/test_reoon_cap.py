"""The daily Reoon cap: at most usage.reoon_daily_checks checks a day, of
any kind, then no new verification until tomorrow."""

import logging
import os
import tempfile
from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.agents.scout as scout_module
import harvey.integrations.email_finder as ef
from harvey.agents.scout import Scout
from harvey.config import UsageConfig, load_config
from harvey.integrations.email_finder import REOON_CHECKS_KEY, ReoonBudget
from harvey.models.company import Company
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.fixture
def reoon_calls(monkeypatch):
    calls = []

    async def verify_reoon(email, api_key):
        calls.append(email)
        return {"status": "safe", "catch_all": False, "deliverable": True}

    async def mx(domain):
        return "mail.example.al"

    monkeypatch.setattr(ef, "verify_reoon", verify_reoon)
    monkeypatch.setattr(ef, "get_mx_host", mx)
    return calls


ENV = SimpleNamespace(reoon_api_key="key", zerobounce_api_key="")


def test_the_default_is_20_checks_a_day():
    assert UsageConfig().reoon_daily_checks == 20
    from harvey.paths import PROJECT_ROOT
    assert load_config(str(PROJECT_ROOT / "harvey.yaml")).usage.reoon_daily_checks == 20


def test_a_negative_cap_is_rejected():
    with pytest.raises(ValidationError):
        UsageConfig(reoon_daily_checks=-1)


@pytest.mark.asyncio
async def test_the_budget_counts_down_and_survives_a_restart(state):
    budget = ReoonBudget(state, 2)
    assert await budget.take() and await budget.take()
    assert not await budget.take()
    assert await ReoonBudget(state, 2).left() == 0
    assert await ReoonBudget(state, 3).left() == 1


@pytest.mark.asyncio
async def test_a_new_day_brings_a_new_budget(state, monkeypatch):
    budget = ReoonBudget(state, 1)
    assert await budget.take() and not await budget.take()

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date.fromordinal(date.today().toordinal() + 1)

    monkeypatch.setattr(ef, "date", Tomorrow)
    assert await budget.take()


@pytest.mark.asyncio
async def test_every_kind_of_check_draws_on_the_same_budget(state, reoon_calls):
    budget = ReoonBudget(state, 2)
    # a business's own address
    assert await ef.verify_address("info@hotelmira.al", env=ENV,
                                   reoon_budget=budget) == "verified"
    # a named person's address
    result = await ef.find_email("Arben", "Hoxha", "hotelmira.al", env=ENV,
                                 state=state, reoon_budget=budget)
    assert result.status == "verified"
    assert len(reoon_calls) == 2
    key = REOON_CHECKS_KEY.format(day=date.today().isoformat())
    assert await state.get_setting(key) == "2"


@pytest.mark.asyncio
async def test_no_reoon_call_once_the_cap_is_reached(state, reoon_calls, monkeypatch):
    async def no_smtp(email):
        return None
    monkeypatch.setattr(ef, "verify_email_smtp", no_smtp)
    budget = ReoonBudget(state, 1)
    await ef.verify_address("info@a.al", env=ENV, reoon_budget=budget)
    assert await ef.verify_address("info@b.al", env=ENV, reoon_budget=budget) == "guess"
    assert reoon_calls == ["info@a.al"]


@pytest.mark.asyncio
async def test_big_providers_draw_on_it_too(state, reoon_calls, monkeypatch):
    async def gmail_mx(domain):
        return "gmail-smtp-in.l.google.com"
    monkeypatch.setattr(ef, "get_mx_host", gmail_mx)
    budget = ReoonBudget(state, 1)
    assert await ef.verify_address("mira@gmail.com", env=ENV, reoon_budget=budget) == "verified"
    assert await ef.verify_address("arta@gmail.com", env=ENV, reoon_budget=budget) == "guess"
    assert reoon_calls == ["mira@gmail.com"]


# ── The scout ──


def make_scout(state, cap, key="key"):
    scout = object.__new__(Scout)
    scout.state = state
    scout.env = SimpleNamespace(reoon_api_key=key)
    scout.config = SimpleNamespace(
        usage=UsageConfig(reoon_daily_checks=cap),
        icp=SimpleNamespace(titles=["Owner"], industries=["hotels"]))
    scout._seen_prospect_keys = set()
    scout._seen_domains = set()
    scout._queries_this_cycle = 0
    return scout


async def use_up(state, n):
    for _ in range(n):
        await state.increment_setting(REOON_CHECKS_KEY.format(day=date.today().isoformat()))


@pytest.mark.asyncio
async def test_the_scout_says_once_a_day_that_the_cap_is_reached(state, caplog):
    scout = make_scout(state, cap=2)
    assert await scout._can_verify()
    await use_up(state, 2)
    with caplog.at_level(logging.INFO, logger="harvey.scout"):
        assert not await scout._can_verify()
        assert not await scout._can_verify()
    lines = [r.message for r in caplog.records if "Reoon checks are used" in r.message]
    assert lines == ["Scout: today's 2 Reoon checks are used; "
                     "no new addresses are verified until tomorrow."]


@pytest.mark.asyncio
async def test_without_reoon_there_is_nothing_to_ration(state):
    scout = make_scout(state, cap=0, key="")
    assert await scout._can_verify()


@pytest.mark.asyncio
async def test_a_spent_cap_leaves_companies_for_tomorrow(state):
    await state.add_company(Company(name="Hotel Mira", domain="hotelmira.al"))
    scout = make_scout(state, cap=1)
    await use_up(state, 1)
    crawled = []

    async def crawl(domain):
        crawled.append(domain)
        return [], []
    scout._crawl_site = crawl

    assert await scout._prospect_via_known_companies() == 0
    assert crawled == []
    # Not marked checked: still waiting for tomorrow's backfill.
    assert [c["domain"] for c in await state.companies_needing_prospects()] == ["hotelmira.al"]
    assert await scout._contacts_from_company("c1", "hotelmira.al", "Hotel Mira", "hotels") == []
    assert crawled == []


@pytest.mark.asyncio
async def test_the_scout_hands_its_budget_to_every_check(state, monkeypatch):
    scout = make_scout(state, cap=5)
    seen = {}

    async def verify_address(email, env=None, reoon_budget=None):
        seen["business"] = reoon_budget
        return "verified"

    async def find_email(first, last, domain, **kw):
        seen["person"] = kw.get("reoon_budget")
        return ef.EmailResult(email=f"{first}@{domain}", status="verified")

    monkeypatch.setattr(scout_module, "verify_address", verify_address)
    monkeypatch.setattr(scout_module, "find_email", find_email)
    await scout._business_contact("c1", "hotelmira.al", "Hotel Mira", "hotels",
                                  ["info@hotelmira.al"])
    await scout._resolve_email("Arben", "Hoxha", "hotelmira.al")
    assert seen["business"].daily_cap == 5 and seen["person"].daily_cap == 5
