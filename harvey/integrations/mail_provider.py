"""Mail provider abstraction — Harvey owns its sending, Instantly optional.

A MailProvider gives Harvey full inbox control: send, read replies,
thread. Two native providers ship:

- ``gmail``  — a Google Workspace / Gmail mailbox via the Gmail REST API
  (recommended: a real mailbox on a dedicated secondary domain is the
  most deliverable way to send <50 cold emails/day).
- ``smtp``   — any mailbox that speaks SMTP + IMAP (AgentMail, Fastmail,
  a Workspace app-mailbox, self-hosted). Configuration via .env.

``instantly`` remains supported through the legacy Sender/Handler path —
it isn't a MailProvider because Instantly owns sequencing/sending itself.
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from harvey.language import fold

logger = logging.getLogger("harvey.mail")

NATIVE_PROVIDERS = ("gmail", "smtp")


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""     # RFC Message-ID (or provider id) of the sent mail
    thread_ref: str = ""     # provider thread handle (Gmail threadId, etc.)
    error: str = ""


@dataclass
class InboundMessage:
    provider_id: str         # provider-unique id, used for reply dedup
    from_email: str
    subject: str = ""
    body: str = ""
    message_id: str = ""     # RFC Message-ID header
    in_reply_to: str = ""    # RFC In-Reply-To header (bounce/thread matching)
    thread_ref: str = ""
    date: str = ""
    is_bounce: bool = False
    headers: dict = field(default_factory=dict)


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
BOUNCE_SUBJECTS = (
    "delivery status notification", "undeliverable", "returned mail",
    "delivery failure", "failure notice", "could not be delivered",
    "delivery incomplete", "address not found",
)


def looks_like_bounce(from_email: str, subject: str) -> bool:
    f = (from_email or "").lower()
    s = (subject or "").lower()
    return any(m in f for m in BOUNCE_SENDERS) or any(m in s for m in BOUNCE_SUBJECTS)


def _opt_out_line(email_cfg, language: str) -> str:
    """The opt-out line in the prospect's language, else the English one."""
    english = (getattr(email_cfg, "opt_out_line", "") or "").strip()
    if language == "sq":
        return (getattr(email_cfg, "opt_out_line_sq", "") or "").strip() or english
    return english


def outreach_footer(email_cfg, language: str = "en") -> str:
    """Signature plus opt-out line: first emails and follow-ups."""
    parts = (
        (getattr(email_cfg, "signature", "") or "").strip(),
        _opt_out_line(email_cfg, language),
    )
    return "\n".join(p for p in parts if p)


def footer_to_strip(email_cfg) -> str:
    """Everything we may have put under an email, in every language: when
    reading a reply, all of it is ours and none of it is the prospect's."""
    parts = (
        getattr(email_cfg, "signature", "") or "",
        getattr(email_cfg, "opt_out_line", "") or "",
        getattr(email_cfg, "opt_out_line_sq", "") or "",
    )
    return "\n".join(p.strip() for p in parts if p.strip())


def reply_footer(email_cfg) -> str:
    """Signature only: someone who wrote back already chose to talk."""
    return (getattr(email_cfg, "signature", "") or "").strip()


def with_footer(body: str, footer: str) -> str:
    """Append the configured footer after a blank line, once."""
    body = (body or "").rstrip()
    footer = (footer or "").strip()
    if not footer or body.endswith(footer):
        return body
    return f"{body}\n\n{footer}"


def _norm_line(line: str) -> str:
    """A line as compared with our footer: case, spacing, quote marks and
    Albanian ë/ç (some clients flatten them to e/c) ignored."""
    line = line.replace("’", "'").replace("‘", "'")
    return fold(" ".join(line.split()).casefold())


def _drop_footer_lines(lines: list[str], footer: str) -> list[str]:
    """Remove our footer line by line.

    Mail clients re-wrap long lines and add blank lines when they quote,
    so the footer rarely survives as one block. A run of reply lines is
    ours when, joined, it reads as one or more consecutive footer lines.
    """
    parts = [p for p in (_norm_line(l) for l in footer.split("\n")) if p]
    if not parts:
        return lines
    runs = {
        " ".join(parts[a:b])
        for a in range(len(parts))
        for b in range(a + 1, len(parts) + 1)
    }
    kept: list[str] = []
    i = 0
    while i < len(lines):
        joined, end = "", None
        for j in range(i, len(lines)):
            piece = _norm_line(lines[j])
            if not piece:
                break
            joined = f"{joined} {piece}".strip()
            if joined in runs:
                end = j
            elif not any(r.startswith(joined + " ") for r in runs):
                break
        if end is None:
            kept.append(lines[i])
            i += 1
        else:
            i = end + 1
    return kept


# Where a quoted copy of an earlier email starts, in the languages Harvey's
# prospects write in (English, Albanian, Italian, German, Serbian in both
# scripts, Macedonian). Either an attribution line ending in "wrote:"
# (German puts the name between "schrieb" and the colon) or an Outlook
# From/Sent header block. Serbian Gmail writes "је написао/ла:" and often
# wraps "је" onto the line before, so only the second word is matched.
QUOTE_ATTRIBUTION_RE = re.compile(
    r"\b(wrote|shkroi|ha scritto|schrieb|написао/ла|napisao/la|напиша)\b[^\n]*:\s*$",
    re.IGNORECASE,
)
# A Gmail attribution wrapped over two lines starts with one of these:
# a lead word, or (Serbian) a weekday and date such as "сре, 26. феб 2025".
QUOTE_ATTRIBUTION_OPENER_RE = re.compile(
    r"^\s*(On|Më|Me|Il|Am|На)\s|^\s*\w{3}\.?,\s\d{1,2}\.?\s\w{3,4}\.?\s\d{4}",
    re.IGNORECASE,
)
# (From, Sent) labels. Outlook for Windows and the web write "Sent";
# Outlook for Mac writes "Date" (Datum, Data) and may put a Reply-To line
# between the two.
OUTLOOK_HEADER_LABELS = (
    ("from:", "sent:"),
    ("nga:", "dërguar:"),
    ("da:", "inviato:"),
    ("von:", "gesendet:"),
    ("od:", "poslato:"),
    ("од:", "послато:"),
    ("from:", "date:"),
    ("von:", "datum:"),
    ("da:", "data:"),
)
QUOTE_SEPARATOR_RE = re.compile(r"^\s*(_{5,}|-{5,}.*)\s*$")


def _quote_start(lines: list[str]) -> int | None:
    """Index of the first line of a quoted-reply header, or None."""
    for i, line in enumerate(lines):
        if QUOTE_ATTRIBUTION_RE.search(line):
            prev = lines[i - 1] if i else ""
            wrapped = bool(
                not QUOTE_ATTRIBUTION_OPENER_RE.match(line)
                and QUOTE_ATTRIBUTION_OPENER_RE.match(prev)
            )
            header = f"{prev} {line}" if wrapped else line
            # A real attribution carries a date or an address; a prospect's
            # own "as my colleague wrote:" does not.
            if re.search(r"[@\d]", header):
                return i - 1 if wrapped else i
        label = line.strip().lstrip("*").lower()
        for from_label, sent_label in OUTLOOK_HEADER_LABELS:
            # A real Sent/Date line carries the date; a prospect's own
            # "Date: whenever suits you" does not.
            if label.startswith(from_label) and any(
                nxt.strip().lstrip("*").lower().startswith(sent_label)
                and re.search(r"\d", nxt)
                for nxt in lines[i + 1:i + 4]
            ):
                return i
    return None


def strip_quoted_reply(text: str, footer: str = "") -> str:
    """The part of a reply the prospect actually wrote.

    Drops our own signature and opt-out line (line by line, wherever they
    appear), lines starting with ">", and everything from a quoted-reply
    header onward, so a phrase quoted from our own email can never decide
    the prospect's intent.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    footer = (footer or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = _drop_footer_lines(text.split("\n"), footer)
    cut = _quote_start(lines)
    if cut is not None:
        lines = lines[:cut]
    lines = [line for line in lines if not line.lstrip().startswith(">")]
    while lines and (not lines[-1].strip() or QUOTE_SEPARATOR_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines).strip()


class MailProvider(ABC):
    """A mailbox Harvey can send from and read replies out of."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether required credentials are present (no network)."""

    @abstractmethod
    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        """Send one plain-text email. Reply threading when refs are given."""

    @abstractmethod
    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Fetch recent inbound messages (dedup is the caller's job)."""

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Cheap end-to-end credential check. Returns (ok, detail)."""


def get_mail_provider(config, env) -> MailProvider | None:
    """Build the configured native provider, or None for instantly/unknown."""
    provider = (config.channels.email.provider or "").strip().lower()
    if provider == "gmail":
        from harvey.integrations.gmail import GmailProvider
        return GmailProvider(config, env)
    if provider == "smtp":
        from harvey.integrations.smtp_mail import SmtpImapProvider
        return SmtpImapProvider(config, env)
    return None
