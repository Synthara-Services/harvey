"""Contact data model — a person at a company."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


# `source` of a prospect that is a business's own public address (info@,
# kontakt@, a Gmail on its site) rather than a named person. It has no name,
# and every email to it greets the business.
BUSINESS_EMAIL_SOURCE = "business_email"


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Prospect(BaseModel):
    id: str = ""
    company_id: str = ""       # FK to companies table
    first_name: str = ""       # required for a valid contact
    last_name: str = ""        # required for a valid contact
    email: str = ""
    email_verified: bool = False
    # verified / risky (catch-all) / guess (never auto-sent) / invalid / ""
    email_status: str = ""
    phone: str = ""
    phone_verified: bool = False
    linkedin_url: str = ""
    title: str = ""            # required for a valid contact
    seniority: str = ""        # c_suite, vp, director, manager, individual
    department: str = ""
    source: str = ""           # how we found them
    source_url: str = ""       # where we found the info
    status: str = "new"        # new/contacted/replied/meeting/closed/lost
    score: int = 0
    personalization_notes: str = ""
    # legacy fields kept for backwards compat with existing code
    company: str = ""
    industry: str = ""
    company_size: str = ""
    # ISO code, recorded only on evidence (see harvey/language.py); "" when
    # unknown, which means English.
    country: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        """Emails are dedup keys — normalize so 'Jane@X.com' == 'jane@x.com'."""
        return (v or "").strip().lower()

    @field_validator("linkedin_url")
    @classmethod
    def _strip_linkedin(cls, v: str) -> str:
        return (v or "").strip()

    @property
    def is_business_address(self) -> bool:
        return self.source == BUSINESS_EMAIL_SOURCE

    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def is_valid(self) -> bool:
        """A person needs a name and title; a business address needs the
        address and the business it belongs to."""
        if self.is_business_address:
            return bool(self.email and self.company)
        return bool(self.first_name and self.last_name and self.title)
