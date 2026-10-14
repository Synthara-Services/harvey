"""The call list: businesses with no website, which Harvey cannot email but
someone can phone."""

import csv
import os
import tempfile

import pytest
import pytest_asyncio

from harvey.export import (
    CALL_LIST_COLUMNS, collect_call_list, export_call_list_csv,
)
from harvey.models.company import Company
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


async def add(state, name, city, *, phone="", website="", category="Restaurant",
              no_website=True, rating=None, reviews=None, at=None):
    cid = await state.add_company(Company(
        name=name, location=city, phone=phone, website=website,
        domain=website.split("//")[-1].strip("/") if website else "",
        industry=category, external_id=f"serper_maps:{name}"))
    rows = []
    if no_website:
        rows.append({"company_id": cid, "signal_code": "NO_WEBSITE", "value_num": 1.0})
    if rating is not None:
        rows.append({"company_id": cid, "signal_code": "REVIEW_RATING",
                     "value_num": rating, "observed_at": at})
    if reviews is not None:
        rows.append({"company_id": cid, "signal_code": "REVIEW_COUNT",
                     "value_num": reviews, "observed_at": at})
    await state.add_observations(rows)
    return cid


@pytest.mark.asyncio
async def test_only_businesses_with_no_website_are_listed(state):
    await add(state, "No Site Taverna", "Tirana", phone="+355 69 000 0002",
              rating=4.9, reviews=270)
    await add(state, "Own Site Grill", "Tirana", website="https://ownsitegrill.al",
              no_website=False, rating=4.8, reviews=1906)
    rows = await collect_call_list(state)
    assert [r["name"] for r in rows] == ["No Site Taverna"]
    assert rows[0] == {"name": "No Site Taverna", "category": "Restaurant",
                       "city": "Tirana", "phone": "+355 69 000 0002",
                       "rating": "4.9", "reviews": 270}


@pytest.mark.asyncio
async def test_a_business_that_since_has_a_website_is_left_out(state):
    await add(state, "Found Later", "Tirana", website="https://foundlater.al",
              no_website=True)
    assert await collect_call_list(state) == []


@pytest.mark.asyncio
async def test_sorted_by_city_then_most_reviewed(state):
    await add(state, "Small", "Tirana", reviews=12)
    await add(state, "Big", "Tirana", reviews=900)
    await add(state, "Unrated", "Tirana")
    await add(state, "Coast", "Durrës", reviews=40)
    rows = await collect_call_list(state)
    assert [r["name"] for r in rows] == ["Coast", "Big", "Small", "Unrated"]


@pytest.mark.asyncio
async def test_a_business_without_a_phone_is_still_listed(state):
    await add(state, "No Phone", "Vlorë", rating=4.2, reviews=8)
    rows = await collect_call_list(state)
    assert rows[0]["phone"] == "" and rows[0]["reviews"] == 8


@pytest.mark.asyncio
async def test_the_latest_rating_and_review_count_win(state):
    cid = await add(state, "Rerun", "Tirana", rating=4.1, reviews=100,
                    at="2026-10-01 10:00:00")
    await state.add_observations([
        {"company_id": cid, "signal_code": "REVIEW_RATING", "value_num": 4.6,
         "observed_at": "2026-10-14 10:00:00"},
        {"company_id": cid, "signal_code": "REVIEW_COUNT", "value_num": 130,
         "observed_at": "2026-10-14 10:00:00"},
        {"company_id": cid, "signal_code": "NO_WEBSITE", "value_num": 1.0},
    ])
    rows = await collect_call_list(state)
    assert len(rows) == 1, "one row per business, however often it was seen"
    assert rows[0]["rating"] == "4.6" and rows[0]["reviews"] == 130


@pytest.mark.asyncio
async def test_the_csv_has_the_columns_and_keeps_albanian_letters(state, tmp_path):
    await add(state, "Taverna Ë", "Shkodër", phone="+355 69 000 0009",
              category="Albanian restaurant", rating=5.0, reviews=33)
    out = tmp_path / "call_list.csv"
    count, _ = await export_call_list_csv(state, out_path=str(out))
    assert count == 1
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "a BOM, so spreadsheet apps read UTF-8"
    with open(out, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == CALL_LIST_COLUMNS
    assert rows[0] == {"name": "Taverna Ë", "category": "Albanian restaurant",
                       "city": "Shkodër", "phone": "+355 69 000 0009",
                       "rating": "5", "reviews": "33"}


def test_harvey_export_call_list_writes_the_file(tmp_path, capsys):
    from harvey import cli

    out = tmp_path / "calls.csv"
    args = type("A", (), dict(call_list=True, out=str(out), email_status="",
                              min_score=0, status="", all=False))()
    cli.cmd_export(args)
    assert out.exists()
    with open(out, encoding="utf-8-sig", newline="") as f:
        assert next(csv.reader(f)) == CALL_LIST_COLUMNS
    assert "business(es) with no website" in capsys.readouterr().out
