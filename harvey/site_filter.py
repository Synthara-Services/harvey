"""Which sites a web search turns up are local businesses in our countries.

A web search for "cafés Kosovo" returns far more global brands, booking and
travel aggregators, business directories and news or research pages than
local businesses: on 2026-10-15 nearly all of the 102 companies the scout's
web and directory searches added were such sites (#50). So a domain found
on the web is a candidate only when it is none of those and does not end in
another country's domain, and a company found there is kept only when its
own site shows a tie to one of the ICP countries.

Serper Maps and the other discovery providers are not filtered here: their
listings come from a search for a city, so they are in it by construction.
"""

import re

from bs4 import BeautifulSoup

from harvey.collectors.discover import is_junk, normalize_domain
from harvey.language import ALBANIAN, DOMAIN_COUNTRY, PLACES, country_from_evidence, fold, reply_language

# Global brands, aggregators, directories and media sites, matched on the
# name a domain is registered under, so every country's variant counts
# (expedia.co.jp, xk.kompass.com) but a business's own subdomain does not
# (booking.ela-beautysalon.com). discover.is_junk and DIRECTORY_NAME catch
# more by pattern.
BRANDS_AND_DIRECTORIES = frozenset({
    # travel, booking and rental aggregators
    "booking", "expedia", "hotels", "trivago", "agoda", "kayak", "skyscanner",
    "momondo", "lastminute", "trip", "tripadvisor", "airbnb", "vrbo",
    "getyourguide", "viator", "klook", "tourradar", "travelstride",
    "rentalcars", "discovercars", "priceline", "hostelworld", "edreams",
    "opodo", "magpie", "hotelscombined",
    # global hotel and car-rental chains
    "marriott", "hilton", "hyatt", "ihg", "accor", "radissonhotels",
    "wyndhamhotels", "bestwestern", "choicehotels", "meliahotelsinternational",
    "hertz", "enterprise", "avis", "sixt", "europcar", "alamo",
    # business directories and company data
    "kompass", "infobel", "infobelpro", "dnb", "emis", "arounddeal",
    "zoominfo", "crunchbase", "cybo", "wheree", "dfave", "goafricaonline",
    "worldcafemap", "europages", "opencorporates", "f6s", "ibisworld",
    "6wresearch", "merkandi", "kerko", "cylex", "worldplaces",
    # rankings and treatment marketplaces
    "legal500", "chambers", "lawzana", "globallawlists", "thelawyersglobal",
    "dentaldepartures", "dentagama", "whatclinic",
    # media and reference
    "bbc", "cnn", "nytimes", "theguardian", "reuters", "bloomberg",
    "cntraveller", "cntravellerme", "lonelyplanet", "timeout", "researchgate",
    "europa", "lemon8-app",
    # directories, guides and marketplaces for our countries, found by the
    # web searches on 2026-10-15
    "loc", "kosovodentalrating", "localgymsandfitness", "daypassgyms",
    "jiujitsuspot", "streetgyms", "indoorclimbing", "caffeatlas", "zoomy",
    "europeancoffeetrip", "europeantravelagencies", "monde-du-voyage",
    "answerharbor", "towobo", "wholesalemanagers", "dentavacation", "dentalby",
    "medicaltourismco", "my-carrentals", "rentascar", "rentcareasy",
    "macedonia-hotels", "hello-macedonia", "balkan-hotel", "yourwaytoalbania",
    "expatsinalbania", "albanian-wall", "thingstodoinnorthmacedonia",
    "northmacedonia-timeless", "beinkosovo", "macedoniatourism", "tripwis",
    "ichgcp",
})
DIRECTORY_NAME = re.compile(r"directory|finder|listings?|database|\d+best|best\d+")
# Second-level endings under a country code (expedia.co.jp, hotelvila.com.al).
SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "gv"})

# Two-letter endings sold worldwide, so they say nothing about a country.
GENERIC_TLDS = frozenset({
    "io", "co", "ai", "me", "tv", "fm", "eu", "ly", "to", "cc", "gg", "so", "ws", "sh",
})
PHONE_CODES = {
    "AL": "355", "XK": "383", "MK": "389", "ME": "382", "RS": "381",
    "DE": "49", "IT": "39", "AT": "43", "CH": "41",
}
# Countries where a page written in Albanian is a tie.
ALBANIAN_SPEAKING = {"AL", "XK", "MK"}


def _labels(domain: str) -> list[str]:
    return normalize_domain(domain).split(".")


def registered_name(domain: str) -> str:
    """The name a domain is registered under: "expedia" for
    www.expedia.co.jp, "kompass" for xk.kompass.com."""
    labels = _labels(domain)
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else labels[0]


def is_brand_or_directory(domain: str) -> bool:
    """A global brand, an aggregator, a directory or a media site: never a
    local business to prospect, whatever search or listing found it."""
    if not domain or is_junk(normalize_domain(domain)):
        return True
    name = registered_name(domain)
    return name in BRANDS_AND_DIRECTORIES or bool(DIRECTORY_NAME.search(name))


def icp_countries(geography) -> set[str] | None:
    """ISO codes of the ICP countries and cities, or None when the list is
    empty or names a place this module does not know, so nothing can be
    judged against it."""
    codes = set()
    for place in geography or []:
        code = country_from_evidence(location=str(place))
        if not code:
            return None
        codes.add(code)
    return codes or None


def is_foreign_country_domain(domain: str, countries: set[str] | None) -> bool:
    """The domain ends in another country's code (expedia.co.jp, hotels.cn)."""
    if not countries:
        return False
    tld = _labels(domain)[-1]
    if len(tld) != 2 or tld in GENERIC_TLDS:
        return False
    return DOMAIN_COUNTRY.get(tld) not in countries


def shows_tie(domain: str, pages: list[str], countries: set[str] | None) -> bool:
    """Whether the site shows a tie to one of ``countries``: their domain
    ending, one of their place names, a phone number with their country
    code, or (for Albania, Kosovo and North Macedonia) Albanian text.
    True when the countries are unknown, since nothing can be judged."""
    if not countries:
        return True
    if DOMAIN_COUNTRY.get(_labels(domain)[-1]) in countries:
        return True
    text, phones = _visible_text_and_phones(pages)
    folded = fold(text)
    for code in countries:
        names = PLACES.get(code, [])
        if names and re.search(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b", folded):
            return True
        prefix = PHONE_CODES.get(code)
        if prefix and re.search(rf"(?:\+|\b00)\s?{prefix}(?=[\s\d().\-/])", phones):
            return True
    return bool(countries & ALBANIAN_SPEAKING) and reply_language(text) == ALBANIAN


def _visible_text_and_phones(pages: list[str]) -> tuple[str, str]:
    """The pages' visible text, and that text with their tel: links."""
    texts, tels = [], []
    for html in pages:
        if not html:
            continue
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            texts.append(soup.get_text(" "))
            tels.extend(str(a.get("href", "")) for a in soup.select('a[href^="tel:"]'))
        except Exception:
            continue
    text = " ".join(texts)
    return text, " ".join([text, *tels])
