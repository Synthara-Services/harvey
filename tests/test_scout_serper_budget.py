"""The scout's daily Serper budget: Serper first while it lasts, then the
free search engines, so most credits stay with Maps discovery."""

import os
import tempfile
from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.agents.scout as scout_module
from harvey.agents.scout import Scout
from harvey.config import UsageConfig
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def make_scout(state, budget=20, serper_key="k"):
    scout = object.__new__(Scout)
    scout.state = state
    scout.config = SimpleNamespace(
        usage=UsageConfig(scout_serper_daily_searches=budget))
    scout.env = SimpleNamespace(serper_api_key=serper_key)
    scout._queries_this_cycle = 0
    scout.calls = []

    async def serper(query, api_key):
        scout.calls.append("serper")
        return [("https://serper.example/" + query, "")]

    async def free(name):
        scout.calls.append(name)
        return [(f"https://{name}.example/", "")]

    scout._search_serper = serper
    scout._search_duckduckgo = lambda q: free("duckduckgo")
    scout._search_bing = lambda q: free("bing")
    scout._search_google = lambda q: free("google")
    return scout


async def search(scout, times):
    for i in range(times):
        scout._queries_this_cycle = 0     # one search per cycle
        await scout._web_search(f"q{i}")


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(scout_module, "SEARCH_DELAY", (0, 0))


def test_the_default_budget_is_20_searches_a_day():
    assert UsageConfig().scout_serper_daily_searches == 20


def test_a_negative_budget_is_rejected():
    with pytest.raises(ValidationError):
        UsageConfig(scout_serper_daily_searches=-1)


@pytest.mark.asyncio
async def test_serper_is_used_until_the_budget_is_spent_then_the_free_engines(state):
    scout = make_scout(state, budget=2)
    await search(scout, 4)
    assert scout.calls == ["serper", "serper", "duckduckgo", "duckduckgo"]


@pytest.mark.asyncio
async def test_the_count_survives_a_restart(state):
    await search(make_scout(state, budget=2), 1)
    restarted = make_scout(state, budget=2)
    await search(restarted, 2)
    assert restarted.calls == ["serper", "duckduckgo"]


@pytest.mark.asyncio
async def test_a_new_day_brings_a_new_budget(state, monkeypatch):
    scout = make_scout(state, budget=1)
    await search(scout, 2)
    assert scout.calls == ["serper", "duckduckgo"]

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date.fromordinal(date.today().toordinal() + 1)

    monkeypatch.setattr(scout_module, "date", Tomorrow)
    scout.calls = []
    await search(scout, 2)
    assert scout.calls == ["serper", "duckduckgo"]


@pytest.mark.asyncio
async def test_a_budget_of_zero_keeps_the_scout_off_serper(state):
    scout = make_scout(state, budget=0)
    await search(scout, 3)
    assert "serper" not in scout.calls
    key = scout_module.SERPER_SEARCHES_KEY.format(day=date.today().isoformat())
    assert await state.get_setting(key, "") == ""


@pytest.mark.asyncio
async def test_without_a_key_nothing_is_counted(state):
    scout = make_scout(state, budget=5, serper_key="")
    await search(scout, 2)
    assert scout.calls == ["duckduckgo", "duckduckgo"]
    key = scout_module.SERPER_SEARCHES_KEY.format(day=date.today().isoformat())
    assert await state.get_setting(key, "") == ""


@pytest.mark.asyncio
async def test_a_failed_serper_call_still_counts(state):
    scout = make_scout(state, budget=1)

    async def failing(query, api_key):
        scout.calls.append("serper")
        return []

    scout._search_serper = failing
    await search(scout, 2)
    assert scout.calls == ["serper", "duckduckgo", "duckduckgo"]
