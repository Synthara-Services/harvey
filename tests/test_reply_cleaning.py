"""Tests for the outgoing footer and for reading only the new part of a reply.

A reply usually quotes the email it answers. Anything in that quote (our own
footer included) must never decide the prospect's intent, or a quoted
opt-out phrase turns an interested reply into a permanent opt-out.
"""

from types import SimpleNamespace

import pytest

from harvey.integrations.mail_provider import (
    outreach_footer,
    reply_footer,
    strip_quoted_reply,
    with_footer,
)

SIGNATURE = (
    "Jane Roe\n"
    "Founder, Example Co\n"
    "example.com\n"
    "1 Example Street, Building 4, Tirana, Albania"
)
OPT_OUT_LINE = "Not relevant for you? Just say so and I won't write again."
FOOTER = SIGNATURE + "\n" + OPT_OUT_LINE


# ── footer ──


def test_outreach_carries_signature_and_opt_out_line():
    cfg = SimpleNamespace(signature=SIGNATURE, opt_out_line=OPT_OUT_LINE)
    assert outreach_footer(cfg) == FOOTER


def test_reply_carries_the_signature_alone():
    cfg = SimpleNamespace(signature=SIGNATURE, opt_out_line=OPT_OUT_LINE)
    assert reply_footer(cfg) == SIGNATURE


def test_footer_parts_are_optional():
    assert outreach_footer(SimpleNamespace(signature=SIGNATURE, opt_out_line="")) == SIGNATURE
    assert outreach_footer(SimpleNamespace(signature="", opt_out_line=OPT_OUT_LINE)) == OPT_OUT_LINE
    assert outreach_footer(SimpleNamespace()) == ""
    assert reply_footer(SimpleNamespace()) == ""


def test_with_footer_appends_after_a_blank_line():
    assert with_footer("Short note. Question?", FOOTER) == "Short note. Question?\n\n" + FOOTER


def test_with_footer_never_doubles():
    once = with_footer("Short note. Question?", FOOTER)
    assert with_footer(once, FOOTER) == once


def test_with_footer_empty_is_a_no_op():
    assert with_footer("Short note.", "") == "Short note."


def test_strips_our_footer_as_sent():
    text = "Yes, send me the details.\n\n" + FOOTER
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


def test_strips_our_footer_across_windows_line_endings():
    text = ("Yes, send me the details.\n\n" + FOOTER).replace("\n", "\r\n")
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


def test_strips_footer_lines_with_blank_lines_between_them():
    # Outlook often double-spaces a plain-text block when it quotes it.
    text = "Yes, send me the details.\n\n" + FOOTER.replace("\n", "\n\n")
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


def test_strips_footer_lines_a_client_rewrapped():
    text = (
        "Yes, send me the details.\n\n"
        "Jane Roe\n"
        "Founder, Example Co\n"
        "example.com\n"
        "1 Example Street, Building 4,\n"
        "Tirana, Albania\n"
        "Not relevant for you? Just say so and I\n"
        "won't write again."
    )
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


def test_strips_footer_lines_a_client_joined():
    text = (
        "Yes, send me the details.\n\n"
        "Jane Roe Founder, Example Co\n"
        "example.com 1 Example Street, Building 4, Tirana, Albania\n"
        "Not relevant for you? Just say so and I won't write again."
    )
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


def test_strips_footer_lines_despite_spacing_case_and_curly_quotes():
    text = (
        "Yes, send me the details.\n\n"
        "  jane roe \n"
        "Founder, Example Co\n"
        "Not relevant for you?  Just say so and I won’t write again."
    )
    assert strip_quoted_reply(text, FOOTER) == "Yes, send me the details."


def test_keeps_prospect_lines_that_only_contain_part_of_a_footer_line():
    text = (
        "Jane Roe, thanks for writing.\n"
        "Not relevant for you? It is, actually.\n"
        "Tirana, Albania is close to us."
    )
    assert strip_quoted_reply(text, FOOTER) == text


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
    # Serbian Cyrillic (Gmail web, then Gmail mobile without "у")
    "уто, 14. окт 2026. у 10:02 Jane Roe <jane@example.com> је написао/ла:",
    "уто, 14. окт 2026. 10:02 Jane Roe <jane@example.com> је написао/ла:",
    # Serbian Latin, as Montenegrin and Serbian users on Gmail mobile get it
    "uto, 14. okt 2026. 10:02 Jane Roe <jane@example.com> je napisao/la:",
    # Latin date with the Cyrillic verb, seen in real Gmail output
    "sre, 14. okt 2026. 10:02 Jane Roe <jane@example.com> је написао/ла:",
    # Macedonian (older Gmail for Android, then Gmail mobile)
    'На 14.10.2026 10:02, "Jane Roe" <jane@example.com> напиша:',
    "На сре., 14 окт. 2026 г. 10:02, Jane Roe <jane@example.com> напиша:",
])
def test_cuts_everything_from_the_quote_header(header):
    text = (
        "Can we talk Thursday?\n\n"
        f"{header}\n"
        "Hi Ana, quick question about your booking flow.\n"
        'Not relevant for you? Reply "remove me" and I won\'t write again.'
    )
    assert strip_quoted_reply(text) == "Can we talk Thursday?"


@pytest.mark.parametrize("first,second", [
    ("On Wed, 14 Oct 2026 at 10:02, Jane Roe <", "jane@example.com> wrote:"),
    # Serbian Gmail often leaves "је" / "je" on the first line
    ("сре, 14. окт 2026. у 10:02 Jane Roe <jane@example.com> је", "написао/ла:"),
    ("sre, 14. okt 2026. 10:02 Jane Roe <jane@example.com> je", "napisao/la:"),
    ("На сре., 14 окт. 2026 г. 10:02, Jane Roe <jane@example.com>", "напиша:"),
])
def test_cuts_a_quote_header_wrapped_over_two_lines(first, second):
    # Gmail's plain-text part wraps a long attribution line.
    text = (
        "Can we talk Thursday?\n\n"
        f"{first}\n"
        f"{second}\n\n"
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
    ("Od", "Poslato"),        # Serbian Latin (also Montenegro)
    ("Од", "Послато"),        # Serbian Cyrillic
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


@pytest.mark.parametrize("from_label,date_label,reply_to", [
    ("From", "Date", ""),
    ("Von", "Datum", ""),
    ("Da", "Data", ""),
    # with a Reply-To on the original, Outlook for Mac adds a line between
    ("From", "Date", "Reply to: Jane Roe <jane@example.com>\n"),
])
def test_cuts_everything_from_the_outlook_for_mac_header_block(from_label, date_label, reply_to):
    text = (
        "Can we talk Thursday?\n\n"
        f"{from_label}: Jane Roe <jane@example.com>\n"
        f"{reply_to}"
        f"{date_label}: Wednesday, 14 October 2026 at 10:02\n"
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


def test_keeps_a_from_and_date_pair_without_a_real_date():
    text = (
        "Happy to meet.\n"
        "From: our side, Ana and me.\n"
        "Date: whenever suits you."
    )
    assert strip_quoted_reply(text) == text


def test_keeps_a_wrote_line_after_a_line_with_numbers():
    # "Ok, 2 points." is not the first half of a wrapped Gmail header.
    text = "Ok, 2 points.\nAs my colleague wrote:\nwe need this live before the season."
    assert strip_quoted_reply(text) == text


def test_reply_that_is_only_a_quote_comes_back_empty():
    text = (
        "On Wed, 14 Oct 2026 at 10:02, Jane Roe <jane@example.com> wrote:\n"
        "> Hi Ana"
    )
    assert strip_quoted_reply(text) == ""
