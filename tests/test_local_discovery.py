"""#50: the scout's web discovery finds local businesses in our countries,
not global brands, aggregators and directories.

On 2026-10-15 the web and directory searches added bbc.com, expedia.co.jp,
arounddeal.com, goafricaonline.com, worldcafemap.com and nearly a hundred
more like them, and searched site:g2.com and site:clutch.co for "cafés".
marriott.com and enterprise.com came in through Serper Maps, which stays as
it is, but the backfill must not prospect them.
"""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.scout import Scout
from harvey.models.company import Company
from tests.test_business_email import fake_site, verified  # noqa: F401 (fixture)

COUNTRIES = ["Albania", "Kosovo", "North Macedonia"]
ICP = SimpleNamespace(
    industries=["tour operators", "cafés"], geography=COUNTRIES,
    titles=["Owner"], company_size="", hiring_signals=["Owner"])

TODAYS_JUNK = [
    "bbc.com", "expedia.co.jp", "enterprise.com", "marriott.com",
    "arounddeal.com", "goafricaonline.com", "worldcafemap.com",
    "tourradar.com", "booking.com", "xk.kompass.com", "search.infobelpro.com",
    "tripadvisor.ca", "hotels.cn", "dnb.com", "legal500.com",
    "pmc.ncbi.nlm.nih.gov", "visitalbania.co.il", "g2.com", "clutch.co",
]
LOCAL = ["albaniantrip.com", "nikapharm.com", "piazza.al", "hotelvila.com.al",
         "meridian-ks.com", "kafe-skopje.mk", "booking.ela-beautysalon.com"]

# Homepages: three local businesses, each tied a different way, and two
# sites with no tie to our countries.
SITES = {
    "https://albaniantrip.com": "<title>Albanian Trip</title><p>Day tours from Vlorë.</p>",
    "https://nikapharm.com": "<title>NikaPharm</title><p>Call us: +383 44 123 456</p>",
    "https://kafenejaime.com": (
        "<title>Kafeneja Ime</title><p>Mirë se vini. Ne ofrojmë kafe dhe ëmbëlsira "
        "për ju dhe familjen tuaj. Na kontaktoni, jemi këtu çdo ditë.</p>"),
    "https://lifefitness.com": "<title>Life Fitness</title><p>Gym equipment worldwide.</p>",
    "https://cntravellerme.com": "<title>CN Traveller</title><p>The best hotels this year.</p>",
}


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        from harvey.state import StateManager
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def make_scout(state, pages=None, results=None):
    """A scout whose web search returns ``results`` and whose fetches serve
    ``pages``; queries and fetched URLs are recorded."""
    scout = object.__new__(Scout)
    scout.state = state
    scout.config = SimpleNamespace(icp=ICP)
    scout.env = SimpleNamespace(serper_api_key="", reoon_api_key="key")
    scout._queries_this_cycle = 0
    scout._seen_domains = set()
    scout._seen_prospect_keys = set()
    scout.queries = []
    scout._fetch, scout.fetched = fake_site(pages or {})

    async def web_search(query):
        scout._queries_this_cycle += 1
        scout.queries.append(query)
        return list(results or [])
    scout._web_search = web_search
    return scout


@pytest.mark.asyncio
async def test_every_web_and_directory_query_names_one_of_our_countries(state):
    directory_countries = set()
    for _ in range(4):
        scout = make_scout(state)
        await scout._find_companies_via_search()
        directories = make_scout(state)
        await directories._find_companies_via_directories()
        directory_countries |= {c for q in directories.queries for c in COUNTRIES if c in q}
        for query in scout.queries + directories.queries:
            assert any(c in query for c in COUNTRIES), query
            assert "g2.com" not in query and "clutch.co" not in query, query
    assert directory_countries == set(COUNTRIES)


@pytest.mark.parametrize("domain", TODAYS_JUNK)
def test_global_brands_aggregators_and_directories_are_never_candidates(state, domain):
    assert not make_scout(state)._is_candidate_domain(domain)


@pytest.mark.parametrize("domain", LOCAL)
def test_local_businesses_stay_candidates(state, domain):
    assert make_scout(state)._is_candidate_domain(domain)


@pytest.mark.asyncio
async def test_a_found_site_must_show_a_tie_to_our_countries(state):
    results = [(f"{url}/", "") for url in SITES]
    scout = make_scout(state, SITES, results)
    found = await scout._find_companies_via_search()
    assert [c["domain"] for c in found] == ["albaniantrip.com", "nikapharm.com",
                                           "kafenejaime.com"]


@pytest.mark.asyncio
async def test_a_directory_page_link_must_show_a_tie_too(state):
    page = "https://kerko.com/en/sherbim/gastronomi-hoteleri"
    pages = {
        page: ('<a href="https://www.marriott.com/tirana">Tirana Marriott</a>'
               '<a href="https://lifefitness.com/">Life Fitness</a>'
               '<a href="https://cafeprishtina.com/">Cafe Prishtina</a>'),
        "https://cafeprishtina.com": "<title>Cafe Prishtina</title><p>Rr. Nëna Terezë, Prishtinë</p>",
        **SITES,
    }
    scout = make_scout(state, pages, [(page, "")])
    found = await scout._find_companies_via_directories()
    assert [c["domain"] for c in found] == ["cafeprishtina.com"]
    assert not any("marriott" in url for url in scout.fetched)


async def _company(state, domain, source, name):
    return await state.add_company(Company(name=name, domain=domain, source=source,
                                           industry="hotels"))


@pytest.mark.asyncio
async def test_the_backfill_never_prospects_a_global_brand(state, verified):
    await _company(state, "marriott.com", "discover:serper_maps", "Tirana Marriott")
    await _company(state, "enterprise.com", "discover:serper_maps", "Enterprise Durrës")
    await _company(state, "bbc.com", "web_search", "BBC Home")
    scout = make_scout(state, {
        "https://marriott.com": '<a href="mailto:info@marriott.com">x</a> Tirana',
        "https://enterprise.com": '<a href="mailto:info@enterprise.com">x</a> Durrës',
        "https://bbc.com": '<a href="mailto:info@bbc.com">x</a> Albania',
    })
    assert await scout._prospect_via_known_companies() == 0
    assert scout.fetched == [] and verified == []


@pytest.mark.asyncio
async def test_the_backfill_checks_a_web_found_company_for_a_tie(state, verified):
    await _company(state, "lifefitness.com", "web_search", "Life Fitness")
    await _company(state, "hotelvila.com", "discover:serper_maps", "Hotel Vila")
    scout = make_scout(state, {
        "https://lifefitness.com": '<p>Gym equipment.</p><a href="mailto:info@lifefitness.com">x</a>',
        # A Maps listing is in the searched city by construction: no tie needed.
        "https://hotelvila.com": '<p>Rooms and breakfast.</p><a href="mailto:info@hotelvila.com">x</a>',
    })
    assert await scout._prospect_via_known_companies() == 1
    assert [p.email for p in await state.get_prospects_by_status("new")] == ["info@hotelvila.com"]
    assert verified == ["info@hotelvila.com"]
