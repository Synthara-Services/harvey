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
from harvey.language import PLACES, fold

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
# Albanian: fatura (invoice), ankesa (complaints).
BLOCKED_LOCALS = frozenset({
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "careers", "career", "jobs", "job", "hr", "privacy", "abuse",
    "postmaster", "webmaster", "hostmaster", "mailer-daemon", "unsubscribe",
    "dpo", "gdpr", "legal", "billing", "invoice", "fatura", "ankesa",
})

# A domain name that is a place or a generic word says nothing about which
# business it belongs to: info@albania.al on beautifulalbania.al is not the
# business's own address. Places come from harvey/language.py.
GENERIC_LABELS = frozenset(
    word for names in PLACES.values() for name in names for word in name.split()
) | frozenset({
    "albanian", "balkan", "balkans", "europe", "shqip",
    "hotel", "hotels", "hostel", "travel", "travels", "tour", "tours",
    "tourism", "turizem", "restaurant", "restorant", "restaurants", "cafe",
    "bar", "food", "shop", "store", "dyqan", "market", "online", "group",
    "grup", "home", "house", "info", "mail", "email", "web", "site",
    "digital", "media", "design", "studio", "auto", "cars", "rent",
    "dental", "clinic", "klinika", "beauty", "fitness", "service",
    "services", "company", "business", "invest", "trade", "global",
    "international",
})

# A web credit in a footer: the designer's address follows it, not the
# business's. Matched on folded text (no ë or ç).
WEB_CREDIT_RE = re.compile(
    r"\b(designed by|developed by|powered by|website by|realizuar nga|"
    r"zhvilluar nga|punuar nga)\b"
)
# Where a credit ends: a separator, or the business's own contact details.
CREDIT_END_RE = re.compile(
    r"[|•·©]|\b(contact|kontakt\w*|e-?mail|tel|phone|telefon\w*|adres\w*)\b")
CREDIT_WINDOW = 80


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


def _with_addresses_inline(html: str) -> str:
    """The page with each mailto link and Cloudflare-protected address
    replaced by the address itself, so text order shows where it sits."""
    html = re.sub(
        r"""<a\b[^>]*?href\s*=\s*["']mailto:([^"'?]+)[^>]*>.*?</a>""",
        lambda m: f" {unescape(m.group(1))} ", html, flags=re.I | re.S)
    html = re.sub(
        r"""<(a|span)\b[^>]*?(?:data-cfemail\s*=\s*["']([0-9a-fA-F]+)["']|"""
        r"""/cdn-cgi/l/email-protection#([0-9a-fA-F]+))[^>]*>.*?</\1>""",
        lambda m: f" {decode_cloudflare(m.group(2) or m.group(3))} ", html,
        flags=re.I | re.S)
    return html


def credit_addresses(html: str) -> set[str]:
    """Addresses that sit in a web credit ("Designed by ...", "Realizuar
    nga ..."): the web designer's, not the business's."""
    text = re.sub(r"<[^>]+>", " ", _with_addresses_inline(html or ""))
    text = " ".join(fold(unescape(text)).split())
    found = set()
    for credit in WEB_CREDIT_RE.finditer(text):
        window = text[credit.end():credit.end() + CREDIT_WINDOW]
        end = CREDIT_END_RE.search(window)
        if end:
            window = window[:end.start()]
        found.update(a.strip(".,;:") for a in EMAIL_RE.findall(window))
    return found


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
    if min(a, b, key=len) in GENERIC_LABELS:
        return False
    return len(a) >= 5 and len(b) >= 5 and (a in b or b in a)


def pick_business_email(pages: list[str], domain: str) -> str:
    """The best public address for the business at ``domain``, or "".

    An on-domain role mailbox first (info@, kontakt@, ...), then any other
    on-domain address, then a free-mail address the site shows, unless it
    sits in a web credit. Never a no-reply, jobs or abuse mailbox, and never
    an address on another company's or a platform's domain.
    """
    domain = (domain or "").lower()
    if not domain or is_third_party_host(domain):
        return ""
    candidates: list[str] = []
    credits: set[str] = set()
    for html in pages:
        credits |= credit_addresses(html)
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
        if is_free_mail(host) and addr not in credits:
            return (2, 0)
        return None

    ranked = [(r, i, a) for i, a in enumerate(candidates)
              if (r := rank(a)) is not None]
    return min(ranked)[2] if ranked else ""
