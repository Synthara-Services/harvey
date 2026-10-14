"""Hosts that are never a business's own: social profiles, booking and
review platforms, link-in-bio pages and free site builders, plus the
free-mail providers small businesses publish as their address.

A business whose only "website" is one of these has no website of its
own. Its address must never be guessed on the platform's domain, and the
platform's pages say nothing about the business.
"""

import re

# Matched against a normalised hostname (lowercase, no www.).
THIRD_PARTY_SITE = re.compile(
    # social, messaging and marketplaces
    r"(^|\.)(facebook|fb|instagram|tiktok|linkedin|twitter|x|youtube|whatsapp|"
    r"booking|airbnb|tripadvisor|google)\.[a-z.]+$"
    r"|^(wa\.me|t\.me|g\.page|goo\.gl)$"
    # link-in-bio pages
    r"|(^|\.)(linktr\.ee|beacons\.ai|linkin\.bio|lnk\.bio|bio\.link|taplink\.cc|"
    r"campsite\.bio|solo\.to|tap\.bio|many\.link)$"
    # free site builders and free hosting subdomains
    r"|(^|\.)(wixsite\.com|business\.site|carrd\.co|netlify\.app|vercel\.app|"
    r"github\.io|pages\.dev|web\.app|firebaseapp\.com|herokuapp\.com|"
    r"wordpress\.com|blogspot\.com|weebly\.com|webflow\.io|webnode\.[a-z.]+|"
    r"jimdosite\.com|site123\.me|square\.site|godaddysites\.com|"
    r"mystrikingly\.com|my\.canva\.site|notion\.site)$",
    re.I,
)

# Free-mail providers: an address there belongs to whoever published it.
FREE_MAIL = re.compile(
    r"^(gmail\.com|googlemail\.com|icloud\.com|me\.com|mac\.com|aol\.com|"
    r"gmx\.[a-z.]+|web\.de|proton\.me|protonmail\.com|yandex\.[a-z.]+|"
    r"mail\.ru|zoho\.com|(yahoo|ymail|hotmail|outlook|live|msn)\.[a-z.]+)$",
    re.I,
)


def is_third_party_host(host: str) -> bool:
    """True for a social, booking, link-in-bio or free-builder host."""
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return bool(host) and bool(THIRD_PARTY_SITE.search(host))


def is_free_mail(domain: str) -> bool:
    return bool(FREE_MAIL.match((domain or "").strip().lower()))
