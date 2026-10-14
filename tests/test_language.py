"""Prospects in Albania or Kosovo get Albanian, everyone else English.

Covers: recording the country on evidence only, the email language that
follows from it (never mixed within a sequence), the Albanian opt-out line,
answering replies in Albanian or English, stripping both opt-out lines when
reading a reply, the Albanian hard checks, and the v12 migration.
"""

from types import SimpleNamespace

import aiosqlite
import pytest

from harvey.integrations.mail_provider import footer_to_strip, outreach_footer, strip_quoted_reply
from harvey.language import (
    country_from_evidence,
    is_albanian_escalation,
    is_albanian_opt_out,
    language_for_country,
    reply_language,
)
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.company import Company
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.test_booking_link import Env, RecordingBrain, make_cfg
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg,
    FakeProvider,
    StubBrain,
    make_handler,
    make_sender,
    state,
)

SIGNATURE = "Jane Roe\nFounder, Example Co\nexample.com\n1 Example Street, Tirana, Albania"
OPT_OUT_EN = "Not relevant for you? Just say so and I won't write again."
OPT_OUT_SQ = "Nuk ju intereson? Mjafton të ma thoni dhe nuk do t'ju shkruaj më."


def use_footers(monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "signature", SIGNATURE, raising=False)
    monkeypatch.setattr(Cfg.channels.email, "opt_out_line", OPT_OUT_EN, raising=False)
    monkeypatch.setattr(Cfg.channels.email, "opt_out_line_sq", OPT_OUT_SQ, raising=False)


# ── the country, on evidence only ──


@pytest.mark.parametrize("evidence,country", [
    ({"linkedin_url": "https://al.linkedin.com/in/ana-doe"}, "AL"),
    ({"domains": ("ana@hotel-vlora.al",)}, "AL"),
    ({"domains": ("https://www.beispiel.de/kontakt",)}, "DE"),
    ({"location": "Tirana, Albania"}, "AL"),
    ({"location": "Tiranë"}, "AL"),
    ({"location": "Prishtinë, Kosovë"}, "XK"),
    ({"location": "Prizren"}, "XK"),
    ({"location": "Skopje, North Macedonia"}, "MK"),
])
def test_country_is_recorded_from_evidence(evidence, country):
    assert country_from_evidence(**evidence) == country


@pytest.mark.parametrize("evidence", [
    {},
    {"domains": ("ana@studio.me",)},          # .me is sold worldwide
    {"domains": ("ana@company.com",)},
    {"linkedin_url": "https://www.linkedin.com/in/ana-doe"},
    {"location": "Berat Kaya"},               # a first name, not the town
    {"location": "Fier"},                     # also a French word
    {"location": "Mitrovica"},                # a town in Kosovo and in Serbia
    {"location": "Birmingham, AL"},           # Alabama, not Albania
    {"location": "Albania and Kosovo"},       # two countries: no evidence
])
def test_no_clear_evidence_means_no_country(evidence):
    assert country_from_evidence(**evidence) == ""


def test_the_persons_linkedin_country_beats_the_company():
    assert country_from_evidence(
        linkedin_url="https://al.linkedin.com/in/ana", location="Berlin, Germany",
        domains=("ana@firma.de",),
    ) == "AL"
    assert country_from_evidence(location="Prizren", domains=("ana@firma.de",)) == "XK"


@pytest.mark.parametrize("country,language", [
    ("AL", "sq"), ("XK", "sq"), ("al", "sq"), ("MK", "en"), ("DE", "en"), ("", "en"),
])
def test_albania_and_kosovo_get_albanian_everyone_else_english(country, language):
    assert language_for_country(country) == language


@pytest.mark.asyncio
async def test_the_scout_records_the_country_when_it_saves_a_prospect():
    from harvey.agents.scout import Scout

    saved = []

    class Store:
        async def get_company(self, company_id):
            return Company(id=company_id, name="Acme", domain="acme.com",
                           location="Prishtina, Kosovo")

        async def add_prospect(self, prospect):
            saved.append(prospect)
            return "p1"

    scout = object.__new__(Scout)
    scout.state = Store()
    await scout._add_prospect(Prospect(first_name="A", last_name="B", title="CEO",
                                       linkedin_url="https://al.linkedin.com/in/ab"))
    await scout._add_prospect(Prospect(first_name="C", last_name="D", title="CEO",
                                       company_id="c1", email="c@acme.com"))
    await scout._add_prospect(Prospect(first_name="E", last_name="F", title="CEO",
                                       email="e@company.com"))
    assert [p.country for p in saved] == ["AL", "XK", ""]


# ── outreach in the prospect's language ──


def test_the_albanian_opt_out_line_goes_under_the_same_signature():
    cfg = SimpleNamespace(signature=SIGNATURE, opt_out_line=OPT_OUT_EN, opt_out_line_sq=OPT_OUT_SQ)
    assert outreach_footer(cfg, "sq") == SIGNATURE + "\n" + OPT_OUT_SQ
    assert outreach_footer(cfg, "en") == SIGNATURE + "\n" + OPT_OUT_EN
    assert outreach_footer(cfg) == SIGNATURE + "\n" + OPT_OUT_EN
    # Without an Albanian line configured, an opt-out line is still there.
    cfg.opt_out_line_sq = ""
    assert outreach_footer(cfg, "sq") == SIGNATURE + "\n" + OPT_OUT_EN


def prospect(country, email="ana@example.org", industry="Tourism"):
    return Prospect(first_name="Ana", last_name="Doe", title="Founder", company="Acme",
                    email=email, email_status="verified", industry=industry, country=country)


def test_campaigns_never_mix_languages():
    from harvey.agents.writer import Writer
    writer = Writer(brain=None, state=None, config=Cfg())
    batches = writer._group_prospects([
        prospect("AL", "a@x.al"), prospect("DE", "b@x.de"), prospect("XK", "c@x.com"), prospect("", "d@x.com"),
    ])
    langs = {key: {language_for_country(p.country) for p in group} for key, group in batches.items()}
    assert langs == {"Tourism-sq-outreach": {"sq"}, "Tourism-outreach": {"en"}}


@pytest.mark.parametrize("country,language", [("AL", "Albanian"), ("XK", "Albanian"), ("DE", "English"), ("", "English")])
@pytest.mark.asyncio
async def test_writer_prompts_name_the_prospects_language(country, language):
    from harvey.agents.writer import Writer
    brain = RecordingBrain()
    writer = Writer(brain=brain, state=None, config=make_cfg())
    writer.skills = ""
    await writer._write_personal_email(prospect(country))
    await writer._write_sequence([prospect(country)])
    first, sequence = brain.prompts
    assert f"- Write it in {language}." in first
    assert f"- Write all three emails in {language}." in sequence


@pytest.mark.asyncio
async def test_every_outreach_email_carries_the_opt_out_line_in_its_language(state, monkeypatch):
    use_footers(monkeypatch)
    ids = {c: await state.add_prospect(prospect(c, email=f"{c or 'xx'}@example.org"))
           for c in ("AL", "DE")}
    campaign = Campaign(id="", name="t", channel="email", status="draft", prospect_ids=list(ids.values()),
                        sequence=[EmailStep(step=1, subject="hi", body="First.", delay_days=0),
                                  EmailStep(step=2, subject="again", body="Second.", delay_days=3)])
    campaign.id = await state.add_campaign(campaign)
    await make_sender(state, FakeProvider())._run_native()

    staged = await state.get_outbox(status="pending_review")
    by_prospect = {}
    for item in staged:
        by_prospect.setdefault(item["prospect_id"], []).append(item["body"])
    assert len(staged) == 4
    assert all(b.endswith("\n" + OPT_OUT_SQ) for b in by_prospect[ids["AL"]])
    assert all(b.endswith("\n" + OPT_OUT_EN) for b in by_prospect[ids["DE"]])


@pytest.mark.asyncio
async def test_the_personal_first_email_carries_the_albanian_line(state, monkeypatch):
    from harvey.agents.writer import Writer
    from tests.test_outbox_native import WriterBrain
    use_footers(monkeypatch)
    pid = await state.add_prospect(prospect("AL"))
    writer = Writer(brain=WriterBrain(), state=state, config=Cfg())
    writer.skills = ""
    await writer._personalize_first_emails("c1", [await state.get_prospect(pid)])
    [item] = await state.get_outbox(status="pending_review")
    assert item["body"].endswith("\n" + OPT_OUT_SQ)


# ── replies ──


@pytest.mark.parametrize("text,language", [
    ("Po, më intereson. Kur mund të flasim?", "sq"),
    ("Faleminderit, jam i interesuar. Kur mund te flasim?", "sq"),
    ("Pershendetje, nuk kemi nevoje per kete tani.", "sq"),
    ("Sounds interesting, can we talk Thursday?", "en"),
    ("Thanks Ardit, we are not interested.", "en"),
    ("Klingt interessant, können wir am Donnerstag sprechen?", "en"),
    ("Sì, ci interessa. Possiamo parlare giovedì?", "en"),
    ("", "en"),
])
def test_replies_are_answered_in_albanian_or_english(text, language):
    assert reply_language(text) == language


async def reply_prompt(text):
    from harvey.agents.handler import Handler
    brain = RecordingBrain()
    handler = Handler(brain=brain, state=None, config=make_cfg(), env=Env())
    convo = Conversation(id="c1", prospect_id="p1", thread=[Message(sender="prospect", content=text)])
    await handler._generate_response("question", text, prospect("AL"), convo)
    return brain.prompts[0]


@pytest.mark.parametrize("text,language", [
    ("Faleminderit, sa kushton nje version i pare?", "Albanian"),
    ("What would a first version cost?", "English"),
    ("Was würde eine erste Version kosten?", "English"),
])
@pytest.mark.asyncio
async def test_the_reply_prompt_names_the_language(text, language):
    assert f"Write the reply in {language}." in await reply_prompt(text)


def test_reading_a_reply_strips_both_opt_out_lines():
    cfg = SimpleNamespace(signature=SIGNATURE, opt_out_line=OPT_OUT_EN, opt_out_line_sq=OPT_OUT_SQ)
    for line in (OPT_OUT_SQ, OPT_OUT_EN, OPT_OUT_SQ.replace("ë", "e")):
        text = f"Po, me intereson.\n\n{SIGNATURE}\n{line}"
        assert strip_quoted_reply(text, footer_to_strip(cfg)) == "Po, me intereson."


# ── the Albanian hard checks ──


@pytest.mark.parametrize("text", [
    "Ju lutem mos më shkruani më.", "mos me shkruani", "Mos na shkruani.", "mos më shkruaj",
    "Mos më dërgoni më email.", "mos me dergoni", "mos më dërgo", "mos na dergoni",
    "Mos më kontaktoni.", "mos me kontakto", "mos na kontaktoni",
    "Më hiqni nga lista.", "me hiqni nga lista juaj", "Hiqmë nga lista.", "hiqni nga listës",
    "Çregjistrohem.", "cregjistrohem", "Ju lutem më çregjistroni.",
    "Fshini të dhënat e mia.", "fshini te dhenat e mia", "fshij te dhenat",
])
def test_albanian_opt_outs_are_honored(text):
    assert is_albanian_opt_out(text)


@pytest.mark.parametrize("text", [
    "Mund të më shkruani të hënën.",          # you can write to me on Monday
    "Na dërgoni ofertën, ju lutem.",          # send us the offer
    "Lista e çmimeve do të ndihmonte.",       # a price list would help
    "Të dhënat tona janë të sigurta.",        # our data is safe
    "Mos u shqetësoni, s'ka problem.",        # don't worry
])
def test_ordinary_albanian_is_not_an_opt_out(text):
    assert not is_albanian_opt_out(text)


@pytest.mark.parametrize("text", [
    "Do të flas me avokatin tim.", "avokati im do t'ju kontaktojë", "Do të marr një avokat.",
    "Do t'ju padis.", "do te bej padi", "Kjo do të jetë një padi.", "Ju kemi paditur.",
    "Kjo do të përfundojë në gjykatë.", "ne gjykate", "Gjykata do të vendosë.",
    "Do t'ju çoj në gjyq.",
])
def test_albanian_legal_threats_go_to_a_human(text):
    assert is_albanian_escalation(text)


@pytest.mark.parametrize("text", [
    "Kjo është e padiskutueshme.", "Çmimi është padiskutueshëm i mirë.",
    "Më pëlqen avokadoja.", "Gjykimi juaj është i mirë.",
])
def test_ordinary_albanian_is_not_escalated(text):
    assert not is_albanian_escalation(text)


@pytest.mark.parametrize("body,status", [
    ("Ju lutem mos më shkruani më.", "opted_out"),
    ("Do t'ju padis nëse vazhdoni.", "needs_human"),
])
@pytest.mark.asyncio
async def test_the_handler_applies_the_albanian_checks(state, body, status):
    from harvey.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import seed_prospect
    pid = await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email="jane@acme.com",
                                       subject="Re: hi", body=body)]
    await make_handler(state, provider, intent="interested")._run_native()
    if status == "opted_out":
        assert (await state.get_prospect(pid)).status == "opted_out"
    else:
        assert (await state.get_conversations_by_status("needs_human"))[0].intent == "escalate"
    assert [i for i in await state.get_outbox() if i["kind"] == "reply"] == []


# ── the v12 migration ──


@pytest.mark.asyncio
async def test_existing_prospects_keep_working_after_the_country_migration(tmp_path):
    from harvey.state import MIGRATIONS
    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:11]:          # a database from before v12
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 11")
        await db.execute(
            "INSERT INTO prospects (id, first_name, last_name, title, email) "
            "VALUES ('old', 'Old', 'Row', 'CEO', 'old@example.org')"
        )
        await db.commit()

    sm = StateManager(db_path)
    await sm.init_db()
    old = await sm.get_prospect("old")
    assert old.first_name == "Old" and old.country == ""
    assert language_for_country(old.country) == "en"

    new_id = await sm.add_prospect(prospect("AL", email="new@example.al"))
    assert (await sm.get_prospect(new_id)).country == "AL"
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == len(MIGRATIONS)
