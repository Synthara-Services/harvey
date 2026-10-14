"""A business's own public address, for companies with no named contact.

Small businesses in Albania and Kosovo rarely list staff. What their site
does show is info@, kontakt@ or office@ on their own domain, or a Gmail
address. This picks the best one from pages Harvey has already fetched:
mailto links first, then addresses hidden by Cloudflare's email
protection (common on these sites), then the page text.
"""

import re
from html import unescape

from harvey.hosts import is_free_mail, is_third_party_host

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"""href\s*=\s*["']mailto:([^"'?]+)""", re.I)
# Cloudflare replaces each address with a hex string, in a data attribute
# or in a link to /cdn-cgi/l/email-protection#<hex>.
CF_RE = re.compile(r"""data-cfemail\s*=\s*["']([0-9a-fA-F]+)["']"""
                   r"""|/cdn-cgi/l/email-protection#([0-9a-fA-F]+)""")
# "logo@2x.png" looks like an address.
FILE_SUFFIX_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js)$", re.I)

# Role mailboxes a business reads, best first. Albanian equivalents
# included: kontakt, zyra (office), rezervime (bookings), shitje (sales),
# recepsion (reception).
PREFERRED_LOCALS = (
    "info", "contact", "kontakt", "office", "zyra", "hello", "reservations",
    "reservation", "rezervime", "rezervimet", "booking", "bookings", "sales",
    "shitje", "reception", "recepsion", "recepsioni",
)
# Never written to: nobody reads them, or writing there is not outreach.
BLOCKED_LOCALS = frozenset({
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "careers", "career", "jobs", "job", "hr", "privacy", "abuse",
    "postmaster", "webmaster", "hostmaster", "mailer-daemon", "unsubscribe",
})


def decode_cloudflare(hex_string: str) -> str:
    """Cloudflare's obfuscation: the first byte is the key, XORed into the rest."""
    try:
        data = bytes.fromhex(hex_string)
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    return "".join(chr(b ^ data[0]) for b in data[1:])


def extract_emails(html: str) -> list[str]:
    """Every address on a page, in the order of preference: mailto links,
    Cloudflare-protected addresses, then plain text. Lowercase, unique."""
    html = html or ""
    found = [unescape(m).strip() for m in MAILTO_RE.findall(html)]
    found += [decode_cloudflare(a or b) for a, b in CF_RE.findall(html)]
    found += EMAIL_RE.findall(unescape(html))
    out: list[str] = []
    for addr in found:
        addr = addr.strip().strip(".,;:").lower()
        if not EMAIL_RE.fullmatch(addr) or FILE_SUFFIX_RE.search(addr):
            continue
        if addr not in out:
            out.append(addr)
    return out


def _label(host: str) -> str:
    """The name part of a domain: "berdicacement" for berdicacement.al."""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "net", "org", "co", "edu", "gov"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else host


def is_own_domain(email_domain: str, site_domain: str) -> bool:
    """The address is on the business's own domain, a subdomain of it, or a
    sibling whose name is part of the site's (berdica.al for
    berdicacement.al): the same business, not another company."""
    email_domain, site_domain = email_domain.lower(), site_domain.lower()
    if email_domain == site_domain or email_domain.endswith("." + site_domain):
        return True
    if is_third_party_host(email_domain) or is_free_mail(email_domain):
        return False
    a, b = _label(email_domain), _label(site_domain)
    return len(a) >= 5 and len(b) >= 5 and (a in b or b in a)


def pick_business_email(pages: list[str], domain: str) -> str:
    """The best public address for the business at ``domain``, or "".

    An on-domain role mailbox first (info@, kontakt@, ...), then any other
    on-domain address, then a free-mail address the site shows. Never a
    no-reply, jobs or abuse mailbox, and never an address on another
    company's or a platform's domain.
    """
    domain = (domain or "").lower()
    if not domain or is_third_party_host(domain):
        return ""
    candidates: list[str] = []
    for html in pages:
        for addr in extract_emails(html):
            if addr not in candidates:
                candidates.append(addr)

    def rank(addr: str):
        local, _, host = addr.partition("@")
        if local in BLOCKED_LOCALS or local.startswith("noreply"):
            return None
        if is_own_domain(host, domain):
            if local in PREFERRED_LOCALS:
                return (0, PREFERRED_LOCALS.index(local))
            return (1, 0)
        if is_free_mail(host):
            return (2, 0)
        return None

    ranked = [(r, i, a) for i, a in enumerate(candidates)
              if (r := rank(a)) is not None]
    return min(ranked)[2] if ranked else ""
