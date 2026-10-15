"""#21: the scout's searches rotate through every country and industry
across rounds, instead of only ever searching the first country."""

import os
import sys
import tempfile
import types
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.agents.scout as scout_module
from harvey.agents.scout import SEARCH_CURSOR_KEY, Scout
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager

ICP = SimpleNamespace(
    industries=["tour operators", "hotels"],
    geography=["Albania", "Kosovo", "North Macedonia"],
    titles=["Owner"], company_size="", hiring_signals=["Owner"])


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def make_scout(state):
    scout = object.__new__(Scout)
    scout.state = state
    scout.config = SimpleNamespace(icp=ICP)
    scout.env = SimpleNamespace(serper_api_key="")
    scout._queries_this_cycle = 0
    scout._seen_domains = set()
    scout._seen_prospect_keys = set()
    scout.queries = []

    async def web_search(query):
        scout._queries_this_cycle += 1
        scout.queries.append(query)
        return []
    scout._web_search = web_search
    return scout


def test_pairs_go_country_by_country_in_icp_order(state):
    assert make_scout(state)._search_pairs() == [
        ("tour operators", "Albania"), ("hotels", "Albania"),
        ("tour operators", "Kosovo"), ("hotels", "Kosovo"),
        ("tour operators", "North Macedonia"), ("hotels", "North Macedonia"),
    ]


@pytest.mark.asyncio
async def test_the_rotation_carries_on_wraps_and_survives_a_restart(state):
    items = ["a", "b", "c"]
    scout = make_scout(state)
    assert await scout._next_in_rotation("x", items, 2) == ["a", "b"]
    assert await make_scout(state)._next_in_rotation("x", items, 2) == ["c", "a"]
    assert await scout._next_in_rotation("x", items, 5) == ["b", "c", "a"]
    assert await scout._next_in_rotation("y", items, 1) == ["a"], "rotations are separate"
    assert await scout._next_in_rotation("x", [], 2) == []


@pytest.mark.asyncio
async def test_a_shorter_icp_list_does_not_break_the_rotation(state):
    await state.set_setting(SEARCH_CURSOR_KEY.format(name="x"), "7")
    assert await make_scout(state)._next_in_rotation("x", ["a", "b"], 1) == ["b"]


@pytest.mark.asyncio
async def test_company_searches_reach_every_country(state):
    countries = set()
    for _ in range(3):
        scout = make_scout(state)
        await scout._find_companies_via_search()
        countries |= {c for c in ICP.geography for q in scout.queries if c in q}
    assert countries == {"Albania", "Kosovo", "North Macedonia"}


@pytest.mark.asyncio
async def test_each_round_searches_the_next_two_pairs(state):
    first, second = make_scout(state), make_scout(state)
    await first._find_companies_via_search()
    await second._find_companies_via_search()
    assert first.queries[0] == '"tour operators" "Albania" company'
    assert second.queries[0] == '"tour operators" "Kosovo" company'
    assert all(len(s.queries) <= scout_module.MAX_QUERIES_PER_CYCLE
               for s in (first, second))


@pytest.mark.asyncio
async def test_directory_searches_rotate_industries_and_countries(state):
    """#50: like the company searches, each directory search names a country."""
    first, second = make_scout(state), make_scout(state)
    await first._find_companies_via_directories()
    await second._find_companies_via_directories()
    assert first.queries == ['"tour operators" "Albania" company directory list',
                             '"hotels" "Albania" company directory list']
    assert second.queries == ['"tour operators" "Kosovo" company directory list',
                              '"hotels" "Kosovo" company directory list']


@pytest.mark.asyncio
async def test_job_boards_rotate_the_countries(state, monkeypatch):
    locations = []

    def scrape_jobs(**kw):
        locations.append(kw["location"])
        return None

    monkeypatch.setitem(sys.modules, "jobspy",
                        types.SimpleNamespace(scrape_jobs=scrape_jobs))
    scout = make_scout(state)
    scout._signal_role_keywords = lambda: ["Owner"]
    for _ in range(4):
        await scout._prospect_via_hiring_boards()
    assert locations == ["Albania", "Kosovo", "North Macedonia", "Albania"]


@pytest.mark.parametrize("name", ["prospects.csv", "call_list.csv"])
def test_repo_root_exports_are_gitignored(name):
    lines = (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    assert f"/{name}" in lines
