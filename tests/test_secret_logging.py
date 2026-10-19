"""#55: an API key never reaches a log line.

httpx logs every request at INFO with its full URL, and Harvey prints INFO
to the terminal. Reoon and ZeroBounce take their key only in the URL, so
harvey.redact hides it in httpx's request line; Hunter, Serper and Gmail
keep their secrets in headers or a POST body, out of the URL altogether.
"""

import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import harvey.integrations.email_finder as ef
import harvey.integrations.gmail as gmail
from harvey.agents.scout import Scout
from harvey.redact import REDACTED, RedactSecrets, install_log_redaction, redact

KEY = "rk-7f3a9c2e5b1d4068"

# What each service answers, by host; anything else gets {}.
ANSWERS = {
    "oauth2.googleapis.com": {"access_token": "at-SECRET", "expires_in": 3600},
    "google.serper.dev": {"organic": []},
}


@pytest.fixture
def sent(monkeypatch, caplog):
    """Every httpx client answers locally and keeps the requests it sent;
    httpx's request lines are captured as Harvey's terminal prints them."""
    real = httpx.AsyncClient
    requests = []

    def answer(request):
        requests.append(request)
        return httpx.Response(200, json=ANSWERS.get(request.url.host, {}))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(answer), **kw))
    caplog.set_level(logging.INFO, logger="httpx")
    return requests


def logged(caplog) -> str:
    """Every httpx line, as the record says it and as the handler wrote it."""
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert lines and all(line.startswith("HTTP Request:") for line in lines)
    return "\n".join(lines) + "\n" + caplog.text


# ── the keys that can only travel in the URL ──


@pytest.mark.parametrize("call,param,kept", [
    (lambda: ef.verify_reoon("info@hotelmira.al", KEY), "key", "mode=power"),
    (lambda: ef.verify_zerobounce("info@hotelmira.al", KEY), "api_key", "validate?"),
], ids=["reoon", "zerobounce"])
@pytest.mark.asyncio
async def test_a_logged_url_never_contains_the_key(sent, caplog, call, param, kept):
    await call()
    [request] = sent
    query = parse_qs(urlsplit(str(request.url)).query)
    assert query[param] == [KEY], "the service still gets its key"
    text = logged(caplog)
    assert KEY not in text
    assert f"{param}={REDACTED}" in text
    assert "email=info%40hotelmira.al" in text and kept in text, "the rest stays readable"


# ── the keys that travel in a header or a body ──


@pytest.mark.parametrize("call", [
    lambda: ef.verify_hunter("info@hotelmira.al", KEY),
    lambda: ef._hunter_domain_pattern("hotelmira.al", KEY),
    lambda: ef.hunter_domain_people("hotelmira.al", KEY),
], ids=["verify", "domain-pattern", "domain-people"])
@pytest.mark.asyncio
async def test_hunter_sends_its_key_in_a_header(sent, caplog, call):
    await call()
    [request] = sent
    assert request.headers["X-API-KEY"] == KEY
    assert KEY not in str(request.url)
    assert KEY not in logged(caplog)


@pytest.mark.asyncio
async def test_serper_sends_its_key_in_a_header(sent, caplog):
    scout = object.__new__(Scout)
    await scout._search_serper("hotels in vlora", KEY)
    [request] = sent
    assert request.headers["X-API-KEY"] == KEY
    assert KEY not in str(request.url)
    assert KEY not in logged(caplog)


@pytest.mark.asyncio
async def test_gmail_keeps_its_secrets_out_of_the_url(sent, caplog, monkeypatch):
    monkeypatch.setattr(gmail, "_load_token", lambda: {"refresh_token": "rt-SECRET"})
    env = SimpleNamespace(gmail_client_id="cid", gmail_client_secret="cs-SECRET")
    provider = gmail.GmailProvider(None, env)
    await provider._request("GET", "/messages", params={"q": "in:inbox", "maxResults": 5})
    refresh, call = sent
    assert refresh.method == "POST"
    assert "cs-SECRET" in refresh.content.decode() and "rt-SECRET" in refresh.content.decode()
    assert call.headers["Authorization"] == "Bearer at-SECRET"
    text = logged(caplog)
    for secret in ("cs-SECRET", "rt-SECRET", "at-SECRET"):
        assert all(secret not in str(r.url) for r in sent)
        assert secret not in text


# ── the filter ──


@pytest.mark.parametrize("url,secret", [
    ("https://emailverifier.reoon.com/api/v1/verify?email=a%40b.al&key=K-1&mode=power", "K-1"),
    ("https://api.zerobounce.net/v2/validate?email=a%40b.al&api_key=K-2", "K-2"),
    ("https://api.example.com/v1?apikey=K-3", "K-3"),
    ("https://api.example.com/v1?API_KEY=K-4&q=hotels", "K-4"),
    ("https://api.example.com/v1?access_token=K-5", "K-5"),
    ("https://api.example.com/v1?client_id=public&client_secret=K-6", "K-6"),
    ("https://api.example.com/v1?password=K-7", "K-7"),
    ("https://bucket.example.com/f?X-Amz-Signature=K-8", "K-8"),
    ("https://api.example.com/v1?sig=K-9", "K-9"),
    ("http://localhost:8765/?state=s&code=K-10", "K-10"),
])
def test_secret_looking_parameters_are_hidden(url, secret):
    line = f'HTTP Request: GET {url} "HTTP/1.1 200 OK"'
    clean = redact(line)
    assert secret not in clean
    assert REDACTED in clean
    assert clean.endswith('"HTTP/1.1 200 OK"')


@pytest.mark.parametrize("url", [
    "https://emailverifier.reoon.com/api/v1/verify?email=a%40b.al&mode=power",
    "https://nominatim.openstreetmap.org/search?q=Tirana&format=json&limit=1",
    "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=in%3Ainbox&maxResults=5",
    "https://api.hunter.io/v2/domain-search?domain=hotelmira.al&limit=1",
])
def test_other_parameters_are_left_alone(url):
    assert redact(url) == url


def test_importing_harvey_puts_one_filter_on_httpx():
    def ours():
        return [f for f in logging.getLogger("httpx").filters if isinstance(f, RedactSecrets)]
    assert len(ours()) == 1, "installed when harvey is imported"
    install_log_redaction()
    assert len(ours()) == 1, "installing again adds no second filter"


def test_a_record_with_arguments_is_redacted_once_formatted():
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               'HTTP Request: %s %s "%s"', ("GET", httpx.URL(
                                   "https://api.zerobounce.net/v2/validate",
                                   params={"api_key": KEY}), "HTTP/1.1 200 OK"), None)
    assert RedactSecrets().filter(record)
    assert KEY not in record.getMessage()
    assert f"api_key={REDACTED}" in record.getMessage()
