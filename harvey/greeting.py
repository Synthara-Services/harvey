"""How an email opens.

A prospect that is a business's own inbox (info@, kontakt@) has no name,
so every email to it greets the business: "Përshëndetje," in Albanian,
"Hello <business> team," or "Hello," in English. Code sets that line, not
the model, so no email to a business opens with a made-up person.
"""

import re

GREETING_SQ = "Përshëndetje,"

# A greeting line, so one a model wrote anyway can be replaced: English
# and Albanian (with or without ë), with or without a name after it.
GREETING_LINE_RE = re.compile(
    r"^\s*(\{\{\s*greeting\s*\}\}|hi|hello|hey|dear|greetings|"
    r"good (morning|afternoon|day)|p[eë]rsh[eë]ndetje|tungjatjeta|"
    r"mir[eë]dita|i nderuar|e nderuar|t[eë] nderuar)\b[^\n]{0,60}$",
    re.I,
)


def team_name(company: str) -> str:
    """The business's name as a greeting can use it: "Amazing Albania" from
    "Amazing Albania - Travel Agency", or "" when it is too long or odd."""
    name = re.split(r"\s[-–—|·]\s|\s*\(", company or "")[0].strip(" ,.")
    if (not name or len(name) > 40 or len(name.split()) > 4
            or any(ch.isdigit() for ch in name)):
        return ""
    return name


def business_greeting(company: str, language: str) -> str:
    """The opening line of every email to a business's own inbox."""
    if language == "sq":
        return GREETING_SQ
    name = team_name(company)
    return f"Hello {name} team," if name else "Hello,"


def person_greeting(first_name: str, language: str) -> str:
    return f"Përshëndetje {first_name}," if language == "sq" else f"Hi {first_name},"


def with_greeting(body: str, greeting: str) -> str:
    """``body`` opening with ``greeting``, replacing any greeting line it
    already had."""
    lines = (body or "").strip().split("\n")
    if lines and GREETING_LINE_RE.match(lines[0]):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    rest = "\n".join(lines).strip()
    return f"{greeting}\n\n{rest}" if rest else greeting
