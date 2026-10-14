"""Which language Harvey writes to a prospect in.

Albanian for prospects in Albania or Kosovo, English for everyone else and
for anyone whose country is unknown. A country is recorded only on
evidence, never guessed:

- the prospect's LinkedIn profile is on a country subdomain (al.linkedin.com),
- a place name in the company's or the job's location ("Tirana, Albania"),
- the company's web or email domain ends in a country code only that
  country's businesses use (.al, .de).

Replies are answered in the language the prospect wrote in when that is
Albanian or English, otherwise in English.
"""

import re
from urllib.parse import urlparse

ALBANIAN = "sq"
ENGLISH = "en"
LANGUAGE_BY_COUNTRY = {"AL": ALBANIAN, "XK": ALBANIAN}
LANGUAGE_NAMES = {ALBANIAN: "Albanian", ENGLISH: "English"}

# Country-code domains only that country's businesses use. Not .me
# (Montenegro), sold worldwide as a personal domain. Kosovo has no
# country-code domain in use.
DOMAIN_COUNTRY = {
    "al": "AL", "mk": "MK", "rs": "RS", "de": "DE",
    "it": "IT", "at": "AT", "ch": "CH",
}
# LinkedIn serves a public profile on the member's country subdomain.
LINKEDIN_COUNTRY = {
    "al": "AL", "mk": "MK", "rs": "RS", "me": "ME", "de": "DE",
    "it": "IT", "at": "AT", "ch": "CH",
}
# Place names, matched as whole words on folded text (no ë or ç). Only
# names that mean the place and nothing else: no "Berat" (also a first
# name), no "Fier" (a French word), no "Mitrovica" (also a town in Serbia).
PLACES = {
    "AL": [
        "albania", "shqiperi", "shqiperia", "tirana", "tirane", "durres",
        "vlore", "vlora", "shkoder", "shkodra", "elbasan", "korce", "korca",
        "sarande", "saranda", "gjirokaster", "gjirokastra",
    ],
    "XK": [
        "kosovo", "kosova", "kosove", "prishtina", "prishtine", "pristina",
        "prizren", "gjakova", "gjakove", "ferizaj", "gjilan", "gjilane",
    ],
    "MK": ["north macedonia", "skopje"],
    "RS": ["serbia", "belgrade", "beograd"],
    "ME": ["montenegro", "podgorica"],
    "DE": ["germany", "deutschland"],
    "IT": ["italy", "italia"],
    "AT": ["austria", "osterreich"],
    "CH": ["switzerland", "schweiz", "suisse", "svizzera"],
}
_PLACE_RES = {
    country: re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")
    for country, names in PLACES.items()
}


def fold(text: str) -> str:
    """Lowercase, with Albanian ë and ç folded to e and c (and ö, ü to o, u),
    so a word matches whether or not it was typed with them."""
    return (text or "").lower().translate(str.maketrans("ëçöüé", "ecoue"))


def _country_from_domain(value: str) -> str:
    host = value.rsplit("@", 1)[-1] if "@" in value else urlparse(
        value if "//" in value else f"//{value}"
    ).hostname or ""
    return DOMAIN_COUNTRY.get(host.lower().rstrip(".").rsplit(".", 1)[-1], "")


def _country_from_linkedin(url: str) -> str:
    host = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()
    if host.endswith(".linkedin.com"):
        return LINKEDIN_COUNTRY.get(host.split(".")[0], "")
    return ""


def _country_from_location(location: str) -> str:
    folded = fold(location)
    found = {c for c, pattern in _PLACE_RES.items() if pattern.search(folded)}
    return found.pop() if len(found) == 1 else ""


def country_from_evidence(
    linkedin_url: str = "", location: str = "", domains: tuple[str, ...] = ()
) -> str:
    """ISO code of the prospect's country, or "" without clear evidence.

    The person's own LinkedIn country comes first, then a location that
    names exactly one country, then the company's domain.
    """
    return (
        _country_from_linkedin(linkedin_url)
        or _country_from_location(location)
        or next((c for d in domains if (c := _country_from_domain(d))), "")
    )


def language_for_country(country: str) -> str:
    return LANGUAGE_BY_COUNTRY.get((country or "").upper(), ENGLISH)


# Words common in Albanian and rare in English, compared on folded text.
_ALBANIAN_WORDS = {
    "dhe", "nuk", "eshte", "jane", "jam", "jemi", "jeni", "kemi", "keni",
    "kam", "nje", "edhe", "shume", "qe", "kjo", "ky", "por", "duhet",
    "mund", "juaj", "faleminderit", "flm", "pershendetje", "miredita",
    "mirembrema", "sepse", "mire", "interesuar", "takim", "ju", "na",
}
_ENGLISH_WORDS = {
    "the", "and", "is", "are", "we", "you", "to", "of", "for", "can",
    "would", "thanks", "thank", "please", "not", "our", "this", "that",
    "with", "have", "it", "be", "will", "interested", "sounds",
}


def reply_language(text: str) -> str:
    """Albanian when the reply is written in Albanian, otherwise English."""
    words = re.findall(r"[a-z]+", fold(text))
    albanian = sum(w in _ALBANIAN_WORDS for w in words)
    english = sum(w in _ENGLISH_WORDS for w in words)
    if "ë" in (text or "").lower():
        albanian += 2
    return ALBANIAN if albanian >= 2 and albanian > english else ENGLISH


# Hard checks in Albanian, matched on folded text so "më" and "me" both
# count, with the inflected forms of each word but nothing longer:
# "padi" (lawsuit) must not match "padiskutueshem" (indisputable).
ALBANIAN_OPT_OUT_PATTERNS = (
    r"\bmos\s+(?:me|na)\s+shkrua(?:j|ni)\b",           # mos më/na shkruaj/shkruani
    r"\bmos\s+(?:me|na)\s+dergo(?:j|ni)?\b",           # mos më/na dërgo/dërgoni
    r"\bmos\s+(?:me|na)\s+kontakto(?:j|ni)?\b",        # mos më/na kontakto/kontaktoni
    r"\bmos\s+(?:me|na)\s+shqeteso(?:j|ni)?\b",        # mos më/na shqetëso/shqetësoni
    r"\bmos\s+(?:me|na)\s+bezdis(?:ni)?\b",            # mos më/na bezdis/bezdisni
    # (më) hiqni/fshini nga lista, hiqmë/fshimë nga lista
    r"\b(?:(?:me|na)\s+)?(?:hiq|fshi)(?:ni|j)?(?:me|na)?\s+nga\s+list(?:a|e|es|en)\b",
    # hiqni/hiqeni/fshini/fshijeni emailin/adresën tim/time/tonë
    r"\b(?:hiq|fshi)(?:ni|eni|jeni)?\s+(?:e-?mailin|email-in|adresen)\s+(?:tim|time|tone|tona)\b",
    r"\bc['’]?regjistro\w*",                           # çregjistrohem, ç'regjistrohem, çregjistroni
    r"\bc['’]?abono\w*",                               # çabonohem, çabonoj (not abonohem)
    r"\bfshi(?:j|ni)?\s+te\s+dhenat\b",                # fshini të dhënat (e mia)
)
ALBANIAN_ESCALATION_PATTERNS = (
    r"\bavokat(?:i|in|it|e|et|eve|ja|en|es|ia)?\b",    # avokat, avokati, avokatët, ...
    r"\bpadi(?:a|ne|se|te|ve|s|sa|sim|sni|t|tur)?\b",  # padi, padia, padinë, padis, paditur
    r"\bgjykat(?:a|e|en|es|at|ave)?\b",                # gjykatë, gjykata, gjykatën, ...
    r"\bgjyq(?:i|in|it|e|et)?\b",                      # gjyq ("në gjyq": to court)
    r"\bgjyqesor(?:e|i|in|it|et|ja)?\b",               # gjyqësor ("procedim gjyqësor")
    r"\bngacmim(?:i|in|it|e|et|eve)?\b",               # ngacmim (harassment)
    # Komisioneri, Albania's data protection authority (not komision/komisioni)
    r"\bkomisioner(?:i|in|it|e|et)?\b",
    # Agjencia për Informim dhe Privatësi, Kosovo's data protection authority
    r"\bagjenci(?:a|ne|se|ja)?\s+per\s+informim\s+dhe\s+privates\w*",
)
_OPT_OUT_RE = re.compile("|".join(ALBANIAN_OPT_OUT_PATTERNS))
_ESCALATION_RE = re.compile("|".join(ALBANIAN_ESCALATION_PATTERNS))


def is_albanian_opt_out(text: str) -> bool:
    return bool(_OPT_OUT_RE.search(fold(text)))


def is_albanian_escalation(text: str) -> bool:
    return bool(_ESCALATION_RE.search(fold(text)))
