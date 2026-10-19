"""Secrets never reach a log line.

httpx logs every request at INFO with its full URL ("HTTP Request: GET
https://...?key=..."), and Harvey prints INFO to the terminal. Reoon and
ZeroBounce take their API key only as a query parameter, so without this
filter the key would be printed on every check. Keys go in a header
wherever the API takes one; this filter covers the ones that don't.
"""

import logging
import re

REDACTED = "***REDACTED***"

# A query parameter whose name says it carries a secret: key=, api_key=,
# apikey=, access_token=, client_secret=, password=, signature=, sig=,
# code= (an OAuth code) and the like.
SECRET_PARAM_RE = re.compile(
    r"(?P<name>[?&](?:[^=&#\s]*(?:key|token|secret|passw|pwd|auth|signature|credential)"
    r"[^=&#\s]*|sig|code)=)(?P<value>[^&#\s\"'<>]+)",
    re.IGNORECASE,
)

# The loggers that write request URLs: httpx logs each request from this
# exact logger, so a filter on it runs before any handler sees the line.
URL_LOGGERS = ("httpx",)


def redact(text: str) -> str:
    """``text`` with the value of every secret-looking query parameter hidden."""
    return SECRET_PARAM_RE.sub(lambda m: m["name"] + REDACTED, text)


class RedactSecrets(logging.Filter):
    """Hides secret-looking query parameters in a log record's message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is logging's to report, not ours
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


def install_log_redaction() -> None:
    """Puts one RedactSecrets on each logger that writes request URLs."""
    for name in URL_LOGGERS:
        log = logging.getLogger(name)
        if not any(isinstance(f, RedactSecrets) for f in log.filters):
            log.addFilter(RedactSecrets())
