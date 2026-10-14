"""Tests for the outgoing footer and for reading only the new part of a reply.

A reply usually quotes the email it answers. Anything in that quote (our own
footer included) must never decide the prospect's intent, or a quoted
opt-out phrase turns an interested reply into a permanent opt-out.
"""

import pytest

from harvey.integrations.mail_provider import strip_quoted_reply, with_footer

FOOTER = (
    "Jane Roe\n"
    "Founder, Example Co\n"
    "example.com\n"
    "1 Example Street, Tirana, Albania\n"
    "Not relevant for you? Just say so and I won't write again."
)


# ── footer ──


def test_with_footer_appends_after_a_blank_line():
    assert with_footer("Short note. Question?", FOOTER) == "Short note. Question?\n\n" + FOOTER


def test_with_footer_never_doubles():
    once = with_footer("Short note. Question?", FOOTER)
    assert with_footer(once, FOOTER) == once


def test_with_footer_empty_is_a_no_op():
    assert with_footer("Short note.", "") == "Short note."


def test_strips_our_footer_exact_match():
    text = "Yes, send me the details.\n\n" + FOOTER
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


def test_strips_our_footer_across_windows_line_endings():
    text = ("Yes, send me the details.\n\n" + FOOTER).replace("\n", "\r\n")
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


# ── quoted lines ──


def test_drops_lines_starting_with_quote_marker():
    text = (
        "Good point.\n"
        '> Not relevant for you? Reply "remove me" and I won\'t write again.\n'
        ">> an older quote\n"
        "Talk soon."
    )
    assert strip_quoted_reply(text) == "Good point.\nTalk soon."


# ── quoted-reply headers ──


@pytest.mark.parametrize("header", [
    # English (Gmail)
    "On Wed, 14 Oct 2026 at 10:02, Jane Roe <jane@example.com> wrote:",
    # Albanian
    "Më 14 tet 2026, në 10:02, Jane Roe <jane@example.com> shkroi:",
    # Italian (Gmail)
    "Il giorno mer 14 ott 2026 alle ore 10:02 Jane Roe <jane@example.com> ha scritto:",
    # German, bare label
    "Am 14.10.2026 um 10:02 schrieb:",
    # German as Gmail and Apple Mail write it: the name sits between
    # "schrieb" and the colon
    "Am Mi., 14. Okt. 2026 um 10:02 Uhr schrieb Jane Roe <jane@example.com>:",
])
def test_cuts_everything_from_the_quote_header(header):
    text = (
        "Can we talk Thursday?\n\n"
        f"{header}\n"
        "Hi Ana, quick question about your booking flow.\n"
        'Not relevant for you? Reply "remove me" and I won\'t write again.'
    )
    assert strip_quoted_reply(text) == "Can we talk Thursday?"


def test_cuts_a_quote_header_wrapped_over_two_lines():
    # Gmail's plain-text part wraps a long attribution line.
    text = (
        "Can we talk Thursday?\n\n"
        "On Wed, 14 Oct 2026 at 10:02, Jane Roe <\n"
        "jane@example.com> wrote:\n\n"
        "> Hi Ana"
    )
    assert strip_quoted_reply(text) == "Can we talk Thursday?"


def test_keeps_the_prospects_line_above_an_unwrapped_header():
    text = (
        "On second thought, Thursday works.\n"
        "On Wed, 14 Oct 2026 at 10:02, Jane Roe <jane@example.com> wrote:\n"
        "> Hi Ana"
    )
    assert strip_quoted_reply(text) == "On second thought, Thursday works."


@pytest.mark.parametrize("from_label,sent_label", [
    ("From", "Sent"),         # English
    ("Nga", "Dërguar"),       # Albanian
    ("Da", "Inviato"),        # Italian
    ("Von", "Gesendet"),      # German
])
def test_cuts_everything_from_the_outlook_header_block(from_label, sent_label):
    text = (
        "Can we talk Thursday?\n\n"
        "________________________________\n"
        f"{from_label}: Jane Roe <jane@example.com>\n"
        f"{sent_label}: Wednesday, October 14, 2026 10:02 AM\n"
        "To: Ana <ana@example.org>\n"
        "Subject: quick question\n\n"
        "Hi Ana, quick question about your booking flow.\n"
        'Not relevant for you? Reply "remove me" and I won\'t write again.'
    )
    assert strip_quoted_reply(text) == "Can we talk Thursday?"


def test_keeps_prospect_lines_that_only_look_like_headers():
    # No date or address on the "wrote:" line, and no Sent line after
    # "From:": these are the prospect's own words, not a quote.
    text = (
        "Call me after 10:00.\n"
        "As my colleague wrote:\n"
        "we need this live before the season.\n"
        "From: our side, budget is fine."
    )
    assert strip_quoted_reply(text) == text


def test_reply_that_is_only_a_quote_comes_back_empty():
    text = (
        "On Wed, 14 Oct 2026 at 10:02, Jane Roe <jane@example.com> wrote:\n"
        "> Hi Ana"
    )
    assert strip_quoted_reply(text) == ""
