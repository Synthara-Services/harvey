"""Contact, about and team pages, found the way a visitor finds them.

Fixed English paths (/contact, /about, /team) miss most sites in Albania
and Kosovo: their pages are /sq/kontakt/, /sq/rreth-nesh/ or /al/kontakt,
and the homepage itself often redirects to a language prefix. So the
homepage's own links come first, matched on their text or path in English
and Albanian, then the fixed paths, under the language prefix when the
site has one. The caller sets how many pages that may be.
"""

import re
from urllib.parse import urljoin, urlparse

from harvey.language import fold

SECTION_PATTERNS = {
    "contact": re.compile(r"\b(contact\w*|kontakt\w*)\b"),
    "about": re.compile(r"\b(about|rreth nesh|kush jemi|per ne)\b"),
    "team": re.compile(r"\b(team|our team|staff|people|leadership|ekip\w*|staf\w*)\b"),
}
LANGUAGE_PREFIXES = frozenset({"sq", "al", "en", "it", "de", "sr", "mk", "fr", "es"})
LINK_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""",
                     re.I | re.S)
SKIP_SUFFIX_RE = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?)$", re.I)


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _words(text: str) -> str:
    """Folded text with tags and punctuation as spaces: "Na Kontaktoni",
    "/sq/rreth-nesh/" and "Rreth Nesh" all become plain words."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text)).split())


def section_links(html: str, page_url: str, kinds: tuple[str, ...]) -> list[str]:
    """Internal links on a page whose text or path names one of ``kinds``,
    in the order of ``kinds``, each once."""
    by_kind: dict[str, list[str]] = {k: [] for k in kinds}
    home_host = _host(page_url)
    for href, text in LINK_RE.findall(html or ""):
        href = href.strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        if _host(url) != home_host or SKIP_SUFFIX_RE.search(urlparse(url).path):
            continue
        words = f"{_words(text)} {_words(urlparse(url).path)}"
        for kind in kinds:
            if SECTION_PATTERNS[kind].search(words):
                if url not in by_kind[kind]:
                    by_kind[kind].append(url)
                break
    return [url for kind in kinds for url in by_kind[kind]]


def language_prefix(url: str) -> str:
    """"/sq" for https://site.al/sq/..., "" when the path has no language."""
    first = (urlparse(url).path.strip("/").split("/") or [""])[0].lower()
    return f"/{first}" if first in LANGUAGE_PREFIXES else ""


def candidate_pages(base: str, home_url: str, home_html: str,
                    static_paths, kinds: tuple[str, ...], budget: int) -> list[str]:
    """Pages to try, best first, at most ``budget`` of them: the homepage's
    own matching links, then the fixed paths under the site's language
    prefix, then the fixed paths at the root."""
    base = base.rstrip("/")
    prefix = language_prefix(home_url)
    urls = section_links(home_html, home_url or base, kinds)
    if prefix:
        urls += [f"{base}{prefix}{p}" for p in static_paths]
    urls += [f"{base}{p}" for p in static_paths]

    seen = {home_url.rstrip("/"), base}
    out = []
    for url in urls:
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= budget:
            break
    return out
