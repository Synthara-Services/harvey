"""Reaching small local businesses through their own public address (#34).

Fixtures are modelled on three sites from the 2026-10-14 round:
hmc.com.al, berdicacement.al and bestseller.al. All redirect to a language
prefix, link their pages as "Kontakt", "Na Kontaktoni" and "Rreth Nesh",
and hide addresses behind Cloudflare's email protection. Berdica's
addresses are on berdica.al, a sibling of its website's domain.
"""

import csv
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import harvey.agents.scout as scout_module
from harvey.agents.scout import Scout
from harvey.agents.sender import Sender
from harvey.agents.writer import Writer
from harvey.business_email import (
    decode_cloudflare, extract_emails, is_own_domain, pick_business_email,
)
from harvey.collectors import discover as D
from harvey.collectors.profile import ProfileCollector
from harvey.config import load_config
from harvey.gate import pre_send_check
from harvey.greeting import business_greeting, team_name, with_greeting
from harvey.hosts import is_free_mail, is_third_party_host
from harvey.integrations import email_finder as ef
from harvey.models.campaign import EmailStep
from harvey.models.company import Company
from harvey.models.prospect import BUSINESS_EMAIL_SOURCE, Prospect
from harvey.signals import seed_signal_catalog
from harvey.site_links import candidate_pages, language_prefix, section_links
from harvey.state import StateManager


def cf(address: str, key: int = 0x5A) -> str:
    """Encode an address the way Cloudflare's email protection does."""
    return f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in address)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


# ── Which address ──


def test_cloudflare_protected_addresses_are_decoded():
    assert decode_cloudflare(cf("sales@berdica.al")) == "sales@berdica.al"
    assert decode_cloudflare("zz") == ""
    html = (f'<a href="/cdn-cgi/l/email-protection#{cf("info@hmc.com.al")}">x</a>'
            f'<span data-cfemail="{cf("office@hmc.com.al")}">[email protected]</span>')
    assert extract_emails(html) == ["info@hmc.com.al", "office@hmc.com.al"]


def test_mailto_comes_before_text_and_files_are_not_addresses():
    html = ('<p>Write to arben@site.al</p><img src="logo@2x.png">'
            '<a href="mailto:Info@Site.al?subject=Hi">mail us</a>')
    assert extract_emails(html) == ["info@site.al", "arben@site.al"]


@pytest.mark.parametrize("pages,domain,expected", [
    # an on-domain role mailbox beats everything, a placeholder on another
    # domain is never taken (hmc.com.al)
    (['<a href="mailto:jane@example.com">x</a> info@hmc.com.al'], "hmc.com.al",
     "info@hmc.com.al"),
    # a sibling domain of the website is the same business (berdica.al)
    ([f'<span data-cfemail="{cf("headoffice@berdica.al")}"></span>'
      f'<span data-cfemail="{cf("sales@berdica.al")}"></span>'], "berdicacement.al",
     "sales@berdica.al"),
    # another on-domain address when there is no role mailbox (bestseller.al)
    (["porosiweb@bestseller.al"], "bestseller.al", "porosiweb@bestseller.al"),
    # Albanian role words count as role mailboxes
    (["arben@hotel.al rezervime@hotel.al"], "hotel.al", "rezervime@hotel.al"),
    # the role order: info before sales
    (["sales@x-shop.al info@x-shop.al"], "x-shop.al", "info@x-shop.al"),
    # a free-mail address the site shows, when there is nothing on-domain
    (["Na shkruani: dyqani.mira@gmail.com"], "dyqanimira.al", "dyqani.mira@gmail.com"),
    # on-domain beats free-mail
    (["mira@yahoo.com kontakt@dyqanimira.al"], "dyqanimira.al", "kontakt@dyqanimira.al"),
    # never these mailboxes
    (["noreply@site.al careers@site.al jobs@site.al hr@site.al privacy@site.al "
      "abuse@site.al postmaster@site.al webmaster@site.al no-reply@site.al"],
     "site.al", ""),
    # never another company's or a platform's domain
    (["info@webagency.al hello@wix.com support@beacons.ai"], "restorantmira.al", ""),
    # a platform's own site is not the business's
    (["info@beacons.ai"], "beacons.ai", ""),
])
def test_pick_business_email(pages, domain, expected):
    assert pick_business_email(pages, domain) == expected


@pytest.mark.parametrize("email_domain,site,own", [
    ("hmc.com.al", "hmc.com.al", True),
    ("mail.hmc.com.al", "hmc.com.al", True),
    ("berdica.al", "berdicacement.al", True),
    ("webagency.al", "restorantmira.al", False),
    ("gmail.com", "gmail-shop.al", False),
    ("abc.al", "abcdefgh.al", False),          # too short to be a sibling
])
def test_is_own_domain(email_domain, site, own):
    assert is_own_domain(email_domain, site) is own


@pytest.mark.parametrize("host,third_party", [
    ("beacons.ai", True), ("linktr.ee", True), ("mira.wixsite.com", True),
    ("mira.netlify.app", True), ("mira.business.site", True), ("mira.carrd.co", True),
    ("instagram.com", True), ("www.booking.com", True),
    ("hmc.com.al", False), ("bestseller.al", False),
])
def test_platform_hosts(host, third_party):
    assert is_third_party_host(host) is third_party


def test_free_mail():
    assert is_free_mail("gmail.com") and is_free_mail("yahoo.it") and is_free_mail("hotmail.com")
    assert not is_free_mail("hmc.com.al")


@pytest.mark.parametrize("url", [
    "https://beacons.ai/mira", "https://mira.wixsite.com/home", "https://mira.netlify.app",
    "https://mira.business.site", "https://mira.carrd.co", "https://linktr.ee/mira",
])
def test_platform_pages_count_as_no_website_in_discovery(url):
    assert D.own_website(url) == ""


# ── Reoon ──


def reoon_response(**fields):
    def handler(request):
        assert request.url.params["mode"] == "power"
        return httpx.Response(200, json=fields)
    return handler


@pytest.fixture
def reoon(monkeypatch):
    real = httpx.AsyncClient

    def install(**fields):
        monkeypatch.setattr(ef.httpx, "AsyncClient", lambda **kw: real(
            transport=httpx.MockTransport(reoon_response(**fields)), **kw))
    return install


@pytest.mark.asyncio
async def test_a_deliverable_role_address_is_verified_not_risky(reoon):
    # The live answer for info@hmc.com.al on 2026-10-14.
    reoon(status="role_account", is_role_account=True, is_deliverable=True,
          is_safe_to_send=True, is_catch_all=False)
    verdict = await ef.verify_reoon("info@hmc.com.al", "key")
    assert ef._status_from_verdict(verdict) == "verified"


@pytest.mark.asyncio
async def test_power_mode_safe_is_verified(reoon):
    # Power mode has no "valid": a good address, a Gmail one too, is "safe".
    reoon(status="safe", is_free_email=True, is_deliverable=True, is_catch_all=False)
    verdict = await ef.verify_reoon("dyqani.mira@gmail.com", "key")
    assert ef._status_from_verdict(verdict) == "verified"


@pytest.mark.parametrize("verdict,status", [
    ({"status": "role_account", "deliverable": True, "catch_all": True}, "risky"),
    ({"status": "role_account", "deliverable": False}, "invalid"),
    ({"status": "role_account"}, None),
    ({"status": "disposable"}, "invalid"),
    ({"status": "spamtrap"}, "invalid"),
    ({"status": "inbox_full"}, None),
    ({"status": "valid"}, "verified"),
    ({"status": "catch_all"}, "risky"),
])
def test_reoon_statuses(verdict, status):
    assert ef._status_from_verdict(verdict) == status


@pytest.mark.asyncio
async def test_verify_address_uses_the_mx_and_reoon(reoon, monkeypatch):
    async def mx(domain):
        return "mail.hmc.com.al"
    monkeypatch.setattr(ef, "get_mx_host", mx)
    reoon(status="role_account", is_deliverable=True, is_safe_to_send=True)
    env = SimpleNamespace(reoon_api_key="key", zerobounce_api_key="")
    assert await ef.verify_address("info@hmc.com.al", env=env) == "verified"


@pytest.mark.asyncio
async def test_verify_address_without_mx_is_invalid(monkeypatch):
    async def no_mx(domain):
        return None
    monkeypatch.setattr(ef, "get_mx_host", no_mx)
    assert await ef.verify_address("info@nowhere.al") == "invalid"


# ── Names ──


def test_email_patterns_fold_albanian_letters():
    assert ef.build_email("{f}.{l}", "Gëzim", "Çela", "x.al") == "gezim.cela@x.al"
    assert ef.build_email("{f}", "Miljan", "Miljević", "x.rs") == "miljan@x.rs"


def test_team_cards_accept_albanian_names():
    from bs4 import BeautifulSoup
    html = ('<div class="team-member"><h3>Gëzim Çela</h3><p>Pronar</p></div>'
            '<div class="team-member"><h3>Ana 2024</h3><p>Owner</p></div>')
    members = object.__new__(Scout)._team_members_from(BeautifulSoup(html, "html.parser"))
    assert members == [{"first_name": "Gëzim", "last_name": "Çela", "title": "Pronar"}]


def test_names_keep_apostrophes_and_hyphens():
    from bs4 import BeautifulSoup
    html = "<div class='team-member'><h3>Sean O'Neil-Hoxha</h3><p>Owner</p></div>"
    members = object.__new__(Scout)._team_members_from(BeautifulSoup(html, "html.parser"))
    assert members[0]["last_name"] == "O'Neil-Hoxha"
    parsed = object.__new__(Scout)._parse_linkedin_url(
        "https://al.linkedin.com/in/x", "Sean O'Neil - Owner at Hotel Mira")
    assert (parsed["first_name"], parsed["last_name"]) == ("Sean", "O'Neil")
    people = ProfileCollector.__new__(ProfileCollector).detect_people(
        "<h3>Sean O'Neil</h3><p>Owner</p>")
    assert people[0]["last_name"] == "O'Neil"


def test_profile_people_accept_albanian_names():
    html = "<h3>Gëzim Çela</h3><p>Owner and founder</p>"
    people = ProfileCollector.__new__(ProfileCollector).detect_people(html)
    assert [(p["first_name"], p["last_name"]) for p in people] == [("Gëzim", "Çela")]


def test_a_business_address_is_a_valid_prospect_without_a_name():
    business = Prospect(email="info@hmc.com.al", company="HMC", source=BUSINESS_EMAIL_SOURCE)
    assert business.is_business_address and business.is_valid()
    assert not Prospect(email="info@hmc.com.al", company="HMC").is_valid()


# ── Finding the pages ──

HMC_HOME = """
<a href="https://hmc.com.al/sq/kontakt/">Kontakt</a>
<a href="https://hmc.com.al/sq/kush-jemi-ne/">Kush jemi ne</a>
<a href="https://www.trustpilot.com/review/hmc.com.al">Staff were great</a>
<a href="mailto:info@hmc.com.al">info</a>
<a href="/sq/sherbimet/">Shërbimet</a>
"""
BESTSELLER_HOME = '<a href="/al/kontakt">Na Kontaktoni</a><a href="/al/products/x">Tavolinë</a>'
BERDICA_HOME = ('<a href="https://berdicacement.al/sq/rreth-nesh/">Rreth Nesh</a>'
                '<a href="https://berdicacement.al/sq/kontakt/">KONTAKT</a>')


def test_section_links_read_albanian_link_text_and_paths():
    assert section_links(HMC_HOME, "https://hmc.com.al/sq/", ("team", "about", "contact")) == [
        "https://hmc.com.al/sq/kush-jemi-ne/", "https://hmc.com.al/sq/kontakt/"]
    assert section_links(BESTSELLER_HOME, "https://bestseller.al/al", ("contact",)) == [
        "https://bestseller.al/al/kontakt"]
    assert section_links(BERDICA_HOME, "https://berdicacement.al/sq/", ("about", "contact")) == [
        "https://berdicacement.al/sq/rreth-nesh/", "https://berdicacement.al/sq/kontakt/"]


def test_language_prefix():
    assert language_prefix("https://hmc.com.al/sq/") == "/sq"
    assert language_prefix("https://bestseller.al/al") == "/al"
    assert language_prefix("https://site.al/") == ""
    assert language_prefix("https://site.al/products/x") == ""


def test_candidate_pages_put_the_sites_own_links_first_within_the_budget():
    pages = candidate_pages("https://bestseller.al", "https://bestseller.al/al",
                            BESTSELLER_HOME, ("/team", "/about", "/kontakt"),
                            ("team", "about", "contact"), budget=4)
    assert pages == ["https://bestseller.al/al/kontakt", "https://bestseller.al/al/team",
                     "https://bestseller.al/al/about", "https://bestseller.al/team"]


# ── The scout ──


class FakeResponse:
    def __init__(self, url, text):
        self.url = url
        self.text = text
        self.status_code = 200


def fake_site(pages: dict, redirects: dict | None = None):
    """A _fetch serving ``pages`` by URL; every request is recorded."""
    calls = []
    redirects = redirects or {}

    async def fetch(url, **kw):
        calls.append(url)
        url = redirects.get(url, url)
        return FakeResponse(url, pages[url]) if url in pages else None

    return fetch, calls


def make_scout(state, fetch, verdicts=None, monkeypatch=None):
    scout = object.__new__(Scout)
    scout.state = state
    scout.env = SimpleNamespace(reoon_api_key="key")
    scout.config = SimpleNamespace(icp=SimpleNamespace(
        titles=["Owner", "Founder", "CEO"], industries=["restaurants"]))
    scout._seen_prospect_keys = set()
    scout._seen_domains = set()
    scout._queries_this_cycle = 0
    scout._fetch = fetch
    return scout


@pytest.fixture
def verified(monkeypatch):
    checked = []

    async def verify(email, env=None):
        checked.append(email)
        return "verified"
    monkeypatch.setattr(scout_module, "verify_address", verify)
    return checked


BERDICA_PAGES = {
    "https://berdicacement.al/sq/": BERDICA_HOME,
    "https://berdicacement.al/sq/rreth-nesh/": "<h2>Historia jonë</h2>",
    "https://berdicacement.al/sq/kontakt/": (
        f'<a href="/cdn-cgi/l/email-protection#{cf("headoffice@berdica.al")}">a</a>'
        f'<a href="/cdn-cgi/l/email-protection#{cf("sales@berdica.al")}">b</a>'),
}
BERDICA_REDIRECT = {"https://berdicacement.al": "https://berdicacement.al/sq/"}


@pytest.mark.asyncio
async def test_a_site_that_names_nobody_gives_one_business_prospect(state, verified):
    company_id = await state.add_company(Company(
        name="Berdica Cement", domain="berdicacement.al", location="Tirana",
        industry="Building materials"))
    fetch, calls = fake_site(BERDICA_PAGES, BERDICA_REDIRECT)
    scout = make_scout(state, fetch)

    contacts = await scout._contacts_from_company(
        company_id, "berdicacement.al", "Berdica Cement", "Building materials")

    assert len(contacts) == 1
    p = contacts[0]
    assert p.email == "sales@berdica.al" and p.email_status == "verified"
    assert p.is_business_address and p.company_id == company_id
    assert p.first_name == "" and p.last_name == "" and p.title == ""
    assert verified == ["sales@berdica.al"]
    # The homepage's own Albanian links were read first, under /sq/.
    assert calls[:3] == ["https://berdicacement.al", "https://berdicacement.al/sq/rreth-nesh/",
                         "https://berdicacement.al/sq/kontakt/"]
    assert len(calls) <= 4 + scout_module.SITE_PAGE_BUDGET


@pytest.mark.asyncio
async def test_a_named_owner_means_no_business_prospect(state, verified, monkeypatch):
    pages = dict(BERDICA_PAGES)
    pages["https://berdicacement.al/sq/rreth-nesh/"] = (
        '<div class="team-member"><h3>Gëzim Çela</h3><p>Owner</p></div>')
    fetch, _ = fake_site(pages, BERDICA_REDIRECT)
    scout = make_scout(state, fetch)

    async def resolve(first, last, domain):
        return f"{first}@{domain}", "verified"
    scout._resolve_email = resolve

    contacts = await scout._contacts_from_company(
        "c1", "berdicacement.al", "Berdica Cement", "Building materials")
    assert [(c.first_name, c.last_name) for c in contacts] == [("Gëzim", "Çela")]
    assert not any(c.is_business_address for c in contacts)
    assert verified == []


@pytest.mark.asyncio
async def test_a_platform_domain_is_never_read(state, verified):
    fetch, calls = fake_site({})
    scout = make_scout(state, fetch)
    assert await scout._contacts_from_company("c1", "beacons.ai", "Mira", "cafés") == []
    assert calls == [] and verified == []


@pytest.mark.asyncio
async def test_the_backfill_saves_one_prospect_per_company_once(state, verified):
    company_id = await state.add_company(Company(
        name="Berdica Cement", domain="berdicacement.al", location="Tirana",
        industry="Building materials"))
    fetch, _ = fake_site(BERDICA_PAGES, BERDICA_REDIRECT)
    scout = make_scout(state, fetch)

    assert await scout._prospect_via_known_companies() == 1
    scout._seen_prospect_keys = set()
    assert await scout._prospect_via_known_companies() == 0

    saved = await state.get_prospects_by_status("new")
    assert len(saved) == 1
    assert saved[0].company_id == company_id and saved[0].is_business_address
    assert saved[0].country == "AL"
    assert await state.count_writable_prospects() == 1


# ── The profile collector ──


@pytest.mark.asyncio
async def test_profile_follows_the_homepage_links_within_its_budget():
    c = ProfileCollector.__new__(ProfileCollector)
    fetched = []

    async def get(url):
        fetched.append(url)
        return "<form>" + "x" * 600 if url.endswith("/al/kontakt") else ""
    c._get = get
    url, html = await c._first_present(
        "https://bestseller.al", ("/contact", "/contact-us", "/kontakt"),
        "https://bestseller.al/al", BESTSELLER_HOME, ("contact",), budget=6)
    assert url == "https://bestseller.al/al/kontakt"
    assert fetched == ["https://bestseller.al/al/kontakt"]

    fetched.clear()
    await c._first_present("https://site.al", ("/a", "/b", "/c", "/d", "/e", "/f", "/g"),
                           "https://site.al/sq/", "", ("contact",), budget=6)
    assert len(fetched) == 6


@pytest.mark.asyncio
async def test_profile_never_reads_a_platform_domain():
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed = {"TECH_STACK", "CONTACT_FOUND"}
    fetched = []

    async def get(url):
        fetched.append(url)
        return "<html>" + "x" * 600
    c._get = get
    assert await c.profile_company("c1", "beacons.ai") == []
    assert fetched == []


# ── Greetings, the writer, the sender and the gate ──


@pytest.mark.parametrize("company,language,expected", [
    ("Hotel Mira", "sq", "Përshëndetje,"),
    ("Hotel Mira", "en", "Hello Hotel Mira team,"),
    ("Amazing Albania - Travel Agency", "en", "Hello Amazing Albania team,"),
    ("Restorant Kripë dhe Piper (Sauna & Grill) Tirana", "en",
     "Hello Restorant Kripë dhe Piper team,"),
    ("Restorant Tradicional Kripë dhe Piper Tirana", "en", "Hello,"),
    ("Hotel 2000", "en", "Hello,"),
    ("", "en", "Hello,"),
])
def test_business_greeting(company, language, expected):
    assert business_greeting(company, language) == expected


def test_team_name():
    assert team_name("Amazing Albania - Travel Agency") == "Amazing Albania"


def test_a_model_written_greeting_is_replaced():
    assert with_greeting("Hi Arben,\n\nI saw your menu.", "Përshëndetje,") == (
        "Përshëndetje,\n\nI saw your menu.")
    assert with_greeting("I saw your menu.", "Hello,") == "Hello,\n\nI saw your menu."


class FakeBrain:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def load_prompt(self, name, **kw):
        return ""

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


@pytest.mark.asyncio
@pytest.mark.parametrize("country,greeting", [("AL", "Përshëndetje,"), ("", "Hello Hotel Mira team,")])
async def test_the_first_email_to_a_business_greets_the_business(state, country, greeting):
    company_id = await state.add_company(Company(
        name="Hotel Mira", domain="hotelmira.al", location="Sarandë", industry="Hotel"))
    await state.add_observations([
        {"company_id": company_id, "signal_code": "NO_ONLINE_BOOKING", "value_num": 1.0},
        {"company_id": company_id, "signal_code": "REVIEW_RATING", "value_num": 4.6},
        {"company_id": company_id, "signal_code": "REVIEW_COUNT", "value_num": 212.0},
    ])
    brain = FakeBrain({"subject": "rezervimet online",
                       "body": "Hi Arben,\n\nI saw you take bookings by phone only. Is that on purpose?"})
    writer = Writer(brain, state, load_config())
    writer.skills = ""
    prospect = Prospect(email="info@hotelmira.al", company="Hotel Mira", company_id=company_id,
                        industry="Hotel", country=country, source=BUSINESS_EMAIL_SOURCE)

    draft = await writer._write_personal_email(prospect)

    assert draft["body"].startswith(greeting + "\n\n")
    assert "Arben" not in draft["body"]
    prompt = brain.prompts[0]
    assert "- Name:" not in prompt and "own inbox" in prompt
    assert "no way to book or order online" in prompt
    assert "Google rating 4.6 from 212 reviews" in prompt
    assert "Where they are: Sarandë" in prompt


def test_business_prospects_get_their_own_batches():
    writer = Writer(None, None, load_config())
    people = Prospect(first_name="A", last_name="B", title="Owner", email="a@x.al",
                      industry="Hotel", country="AL")
    business = Prospect(email="info@y.al", company="Y", industry="Hotel", country="AL",
                        source=BUSINESS_EMAIL_SOURCE)
    batches = writer._group_prospects([people, business])
    assert sorted(batches) == ["Hotel-sq-business-outreach", "Hotel-sq-outreach"]
    assert all(len(v) == 1 for v in batches.values())


def test_a_business_sequence_opens_with_the_greeting_variable():
    steps = Writer._business_sequence([
        EmailStep(step=1, subject="pyetje", body="Hi {{first_name}},\n\n{{company}} ..."),
        EmailStep(step=2, subject="ndjekje", body="Just following up."),
    ])
    assert [s.body.split("\n")[0] for s in steps] == ["{{greeting}}", "{{greeting}}"]
    assert Writer._business_sequence(
        [EmailStep(step=1, subject="x", body="Thanks {{ first_name }}.")]) == []


@pytest.mark.parametrize("country,greeting", [("AL", "Përshëndetje,"), ("XK", "Përshëndetje,"),
                                               ("", "Hello Hotel Mira team,")])
def test_the_sender_renders_the_business_greeting(country, greeting):
    business = Prospect(email="info@hotelmira.al", company="Hotel Mira", country=country,
                        source=BUSINESS_EMAIL_SOURCE)
    sender = object.__new__(Sender)
    body = sender._render("{{greeting}}\n\nA note for {{company}}.", business)
    assert body == f"{greeting}\n\nA note for Hotel Mira."
    assert pre_send_check("info@hotelmira.al", "pyetje", body).ok


def test_a_persons_merge_variable_stays_unfilled_for_a_business_and_the_gate_stops_it():
    business = Prospect(email="info@hotelmira.al", company="Hotel Mira",
                        source=BUSINESS_EMAIL_SOURCE)
    body = object.__new__(Sender)._render("Hi {{first_name}},\n\nQuick one.", business)
    assert "{{first_name}}" in body
    assert not pre_send_check("info@hotelmira.al", "pyetje", body).ok


def test_a_person_is_still_greeted_by_name():
    person = Prospect(first_name="Arben", last_name="Hoxha", title="Owner", email="arben@x.al")
    sender = object.__new__(Sender)
    assert sender._render("Hi {{first_name}},", person) == "Hi Arben,"
    assert sender._render("{{greeting}}", person) == "Hi Arben,"


@pytest.mark.parametrize("body,ok", [
    ("Përshëndetje,\n\nKemi parë faqen tuaj.", True),
    ("Hello,\n\nQuick question.", True),
    ("Hello Hotel Mira team,\n\nQuick question.", True),
    ("Hi ,\n\nQuick question.", False),
    ("Përshëndetje ,\n\nPyetje.", False),
    ("Hello {{first_name}},\n\nx", False),
    ("Hello [Name],\n\nx", False),
])
def test_the_gate_passes_a_greeting_without_a_name_and_holds_placeholders(body, ok):
    assert pre_send_check("info@hotelmira.al", "pyetje", body).ok is ok


# ── harvey export --business ──


@pytest.mark.asyncio
async def test_export_lists_business_addresses_of_every_status(state, tmp_path):
    from harvey.export import export_prospects_csv
    await state.add_prospect(Prospect(email="info@a.al", company="A", email_status="verified",
                                      source=BUSINESS_EMAIL_SOURCE))
    await state.add_prospect(Prospect(email="kontakt@b.al", company="B", email_status="guess",
                                      source=BUSINESS_EMAIL_SOURCE))
    await state.add_prospect(Prospect(first_name="C", last_name="D", title="Owner",
                                      email="c@e.al", company="E", email_status="verified"))
    out = tmp_path / "business.csv"
    count, _ = await export_prospects_csv(state, out_path=str(out), business_only=True)
    assert count == 2
    with open(out, newline="") as f:
        rows = list(csv.DictReader(f))
    assert sorted(r["email"] for r in rows) == ["info@a.al", "kontakt@b.al"]
    assert {r["source"] for r in rows} == {BUSINESS_EMAIL_SOURCE}


def test_harvey_export_business_flag(tmp_path, capsys):
    from harvey import cli
    out = tmp_path / "b.csv"
    args = SimpleNamespace(call_list=False, business=True, out=str(out), email_status="",
                           min_score=0, status="", all=False)
    cli.cmd_export(args)
    assert out.exists()
    assert "businesses' own addresses" in capsys.readouterr().out
