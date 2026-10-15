"""Deterministic pre-send gate — the last line before an email leaves.

LLM instructions are a floor, not a guarantee. Every outgoing email passes
these non-LLM checks at send time; a failure blocks the send and records
why, so a bad generation can never reach a prospect. This is the pattern
the surviving commercial AI-SDR vendors converged on after the 2025
autonomous-agent flameouts: constraints in the prompt, verification in
code.
"""

import re
from dataclasses import dataclass, field

# Spam-filter and AI-tell phrases that must never appear in outgoing mail.
# Mirrors prompts/writer.md; enforced here because prompts can be ignored.
BANNED_PHRASES = (
    "act now", "buy now", "click here", "click below", "limited time",
    "limited offer", "exclusive deal", "special offer", "risk-free",
    "no obligation", "100% free", "make money", "double your",
    "guaranteed results", "winner", "congratulations",
    "i hope this finds you well", "hope this email finds you well",
    "i hope this email finds you", "picture this", "what if i told you",
    "in today's fast-paced world", "game-changer", "game changer",
    "cutting-edge solution", "revolutionize your", "unlock your",
    "supercharge your", "as an ai", "i'm an ai", "i am an ai",
    "as a language model",
)

MAX_SUBJECT_LEN = 90
MAX_BODY_WORDS = 220        # hard ceiling; sequences aim far lower
MAX_LINKS = 1

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# Template variables a render left behind: {{first_name}}, {{ First Name }},
# {company}, {First Name}.
MERGE_TAG_RE = re.compile(
    r"\{\{\s*[^\W\d_][^{}\n]{0,40}\}\}|\{\s*[^\W\d_][^{}\n]{0,40}\}"
)
# Square-bracket placeholders a model copied from an example: [LINK],
# [Name], [Company], [your calendar link]. They open with a letter, so a
# footnote such as "[1]" is not one.
BRACKET_PLACEHOLDER_RE = re.compile(r"\[\s*[^\W\d_][^\[\]\n]{0,40}\]")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# A greeting whose name rendered empty: "Hi ," or "Përshëndetje ,". A
# greeting with no name at all ("Hello," "Përshëndetje,") is fine: that is
# how an email to a business's own inbox opens.
EMPTY_NAME_GREETING_RE = re.compile(
    r"(?im)^\s*(hi|hey|hello|dear|p[eë]rsh[eë]ndetje|i nderuar|e nderuar|"
    r"t[eë] nderuar)\s+[,!:]"
)

DELIVERABLE_STATUSES = {"verified", "risky"}


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


def pre_send_check(
    to_email: str,
    subject: str,
    body: str,
    prospect=None,
    allow_risky: bool = False,
    kind: str = "sequence",
) -> GateResult:
    """Run every deterministic check. Returns ok=False with reasons on any hit."""
    reasons: list[str] = []
    subject = (subject or "").strip()
    body = (body or "").strip()
    to_email = (to_email or "").strip().lower()

    # Recipient sanity
    if not EMAIL_RE.match(to_email):
        reasons.append(f"invalid recipient address '{to_email}'")
    if prospect is not None:
        if (prospect.email or "").strip().lower() != to_email:
            reasons.append("recipient does not match the prospect record")
        allowed = DELIVERABLE_STATUSES if allow_risky else {"verified"}
        # Replies go back to someone who emailed US — deliverability proven.
        if kind == "sequence" and (prospect.email_status or "guess") not in allowed:
            reasons.append(
                f"email_status '{prospect.email_status or 'guess'}' is not deliverable"
            )

    # Content sanity
    if not subject:
        reasons.append("empty subject")
    if not body:
        reasons.append("empty body")
    if len(subject) > MAX_SUBJECT_LEN:
        reasons.append(f"subject too long ({len(subject)} > {MAX_SUBJECT_LEN})")
    word_count = len(body.split())
    if word_count > MAX_BODY_WORDS:
        reasons.append(f"body too long ({word_count} words > {MAX_BODY_WORDS})")

    # Unfilled placeholders, the classic mass-mail embarrassment. A reply's
    # subject is "Re: " plus the prospect's own subject, which mail systems
    # often tag ("[EXTERNAL]"), so brackets there are theirs, not ours.
    leftover = MERGE_TAG_RE.findall(subject + " " + body)
    leftover += BRACKET_PLACEHOLDER_RE.findall(body)
    if kind != "reply":
        leftover += BRACKET_PLACEHOLDER_RE.findall(subject)
    if leftover:
        reasons.append(f"unfilled placeholders: {sorted(set(leftover))[:3]}")
    if EMPTY_NAME_GREETING_RE.search(body):
        reasons.append("greeting with an empty name")
    # The same line twice at the top: a greeting added twice.
    top = [line.strip() for line in body.splitlines() if line.strip()][:2]
    if len(top) == 2 and top[0].casefold() == top[1].casefold():
        reasons.append(f"the first two lines are the same: '{top[0][:40]}'")

    # Banned phrases (spam triggers + AI tells)
    lowered = (subject + " " + body).lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        reasons.append(f"banned phrases: {hits[:3]}")

    # Link budget: cold email with links goes to spam
    links = URL_RE.findall(body)
    if len(links) > MAX_LINKS:
        reasons.append(f"too many links ({len(links)} > {MAX_LINKS})")

    # No HTML — Harvey sends plain text only
    if re.search(r"<\s*(html|body|div|table|img|a)\b", body, re.IGNORECASE):
        reasons.append("HTML markup in body (plain text only)")

    return GateResult(ok=not reasons, reasons=reasons)
