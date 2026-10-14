"""Prospect-list export — the list itself is the product.

Produces a CSV that drops straight into Instantly, Smartlead, or any
sequencer (email / first_name / last_name / company_name / personalization
columns), plus Harvey's own quality columns (score, email_status, signals)
so a human can eyeball what they're getting.
"""

import csv
import io
import logging

from harvey.state import StateManager

logger = logging.getLogger("harvey.export")

# Column order chosen so the file imports cleanly into cold-email tools
# without remapping; extra columns are simply available as custom variables.
CSV_COLUMNS = [
    "email", "first_name", "last_name", "company_name", "title",
    "website", "linkedin_url", "industry", "personalization",
    "email_status", "score", "seniority", "source", "status", "created_at",
]

DEFAULT_EMAIL_STATUSES = ["verified", "risky"]


async def collect_prospects(
    state: StateManager,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
) -> list[dict]:
    """Fetch prospects matching the filters as export-ready dicts.

    Default view is the *deliverable* list (verified + risky emails).
    ``include_all`` disables every filter for a full dump.
    """
    import aiosqlite

    where = []
    params: list = []
    if not include_all:
        wanted = email_statuses if email_statuses is not None else DEFAULT_EMAIL_STATUSES
        if wanted:
            placeholders = ",".join("?" for _ in wanted)
            where.append(f"email != '' AND email_status IN ({placeholders})")
            params.extend(wanted)
        if min_score > 0:
            where.append("score >= ?")
            params.append(min_score)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"status IN ({placeholders})")
            params.extend(statuses)

    sql = "SELECT * FROM prospects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY score DESC, created_at DESC"

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

    out = []
    for r in rows:
        out.append({
            "email": r.get("email", ""),
            "first_name": r.get("first_name", ""),
            "last_name": r.get("last_name", ""),
            "company_name": r.get("company", ""),
            "title": r.get("title", ""),
            "website": r.get("source_url", ""),
            "linkedin_url": r.get("linkedin_url", ""),
            "industry": r.get("industry", ""),
            "personalization": r.get("personalization_notes", ""),
            "email_status": r.get("email_status", ""),
            "score": r.get("score", 0),
            "seniority": r.get("seniority", ""),
            "source": r.get("source", ""),
            "status": r.get("status", ""),
            "created_at": r.get("created_at", ""),
        })
    return out


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


async def export_prospects_csv(
    state: StateManager,
    out_path: str | None = None,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
) -> tuple[int, str]:
    """Export prospects to CSV. Returns (row_count, csv_text).

    Writes to ``out_path`` when given; caller handles printing/serving.
    """
    rows = await collect_prospects(
        state,
        email_statuses=email_statuses,
        min_score=min_score,
        statuses=statuses,
        include_all=include_all,
    )
    text = to_csv(rows)
    if out_path:
        with open(out_path, "w", newline="") as f:
            f.write(text)
        logger.info(f"Exported {len(rows)} prospects to {out_path}")
    return len(rows), text


# ── Call list: businesses with no website ──
# No website means no address Harvey can email, but the listing still has a
# phone, and for anyone who builds websites this is the warmest list there is.

CALL_LIST_COLUMNS = ["name", "category", "city", "phone", "rating", "reviews"]


def _latest(code: str) -> str:
    """SQL for a company's most recent value of one signal."""
    return (
        "(SELECT o.value_num FROM observations o "
        f"WHERE o.company_id = c.id AND o.signal_code = '{code}' "
        "ORDER BY o.observed_at DESC, o.rowid DESC LIMIT 1)"
    )


async def collect_call_list(state: StateManager) -> list[dict]:
    """Every company observed with no website, by city, most reviewed first.

    Rating and reviews are the latest observation of each, so a later
    discovery run updates them rather than adding a second row.
    """
    import aiosqlite

    sql = f"""
        SELECT c.name, c.industry AS category, c.location AS city, c.phone,
               {_latest('REVIEW_RATING')} AS rating,
               {_latest('REVIEW_COUNT')} AS reviews
        FROM companies c
        WHERE COALESCE(c.website, '') = ''
          AND EXISTS (SELECT 1 FROM observations o
                      WHERE o.company_id = c.id AND o.signal_code = 'NO_WEBSITE')
        ORDER BY c.location, reviews DESC, c.name
    """
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

    return [
        {
            "name": r["name"] or "",
            "category": r["category"] or "",
            "city": r["city"] or "",
            "phone": r["phone"] or "",
            "rating": "" if r["rating"] is None else f"{r['rating']:g}",
            "reviews": "" if r["reviews"] is None else int(r["reviews"]),
        }
        for r in rows
    ]


async def export_call_list_csv(
    state: StateManager, out_path: str | None = None
) -> tuple[int, str]:
    """Export the call list to CSV. Returns (row_count, csv_text).

    The file is UTF-8 with a byte-order mark, so spreadsheet apps show
    "Durrës" and "Shkodër" as written.
    """
    rows = await collect_call_list(state)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CALL_LIST_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    text = buf.getvalue()
    if out_path:
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            f.write(text)
        logger.info(f"Exported {len(rows)} businesses to call to {out_path}")
    return len(rows), text
