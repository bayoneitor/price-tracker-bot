"""The shipped catalogs must not lose structure the source string carries.

A multi-line message whose translation collapsed onto one line renders as two
sentences run together — "I am still waiting for a target priceSend /cancel
first…" — and nothing in the test suite noticed, because every test asserts on
substrings that survive the damage. Seventeen strings shipped that way.

Placeholders are the same class of defect with a louder failure: a msgstr that
drops `{count}` or renames it raises KeyError at render time, in whichever locale
the reader happens to have.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

LOCALE_DIR = Path("src/price_tracker/locale")
LOCALES = ("en", "es_ES", "it_IT")
PLACEHOLDER = re.compile(r"\{(\w+)")


def _translated(locale: str) -> list[tuple[str, str]]:
    """Every entry with both a source string and a translation."""
    with (LOCALE_DIR / locale / "LC_MESSAGES" / "messages.po").open("rb") as handle:
        catalog = read_po(handle, locale=locale)
    return [
        (message.id, message.string)
        for message in catalog
        if isinstance(message.id, str)
        and isinstance(message.string, str)
        and message.id
        and message.string
    ]


@pytest.mark.parametrize("locale", LOCALES)
def test_line_breaks_survive_translation(locale: str) -> None:
    broken = [
        msgid for msgid, msgstr in _translated(locale) if msgid.count("\n") != msgstr.count("\n")
    ]
    assert not broken, f"{locale}: line breaks lost in {len(broken)} messages: {broken[:3]}"


@pytest.mark.parametrize("locale", LOCALES)
def test_placeholders_survive_translation(locale: str) -> None:
    mismatched = [
        msgid
        for msgid, msgstr in _translated(locale)
        if set(PLACEHOLDER.findall(msgid)) != set(PLACEHOLDER.findall(msgstr))
    ]
    assert not mismatched, f"{locale}: placeholders differ in {mismatched[:3]}"


@pytest.mark.parametrize("locale", LOCALES)
def test_html_tags_survive_translation(locale: str) -> None:
    """Messages are sent with parse_mode=HTML; an unbalanced tag is a Telegram error."""
    tags = re.compile(r"</?(b|i|code|a)\b")
    mismatched = [
        msgid
        for msgid, msgstr in _translated(locale)
        if sorted(tags.findall(msgid)) != sorted(tags.findall(msgstr))
    ]
    assert not mismatched, f"{locale}: HTML tags differ in {mismatched[:3]}"


@pytest.mark.parametrize("locale", LOCALES)
def test_nothing_is_left_fuzzy(locale: str) -> None:
    """A fuzzy entry is a guess pybabel made from a similar string, not a translation."""
    with (LOCALE_DIR / locale / "LC_MESSAGES" / "messages.po").open("rb") as handle:
        catalog = read_po(handle, locale=locale)
    fuzzy = [m.id for m in catalog if m.id and "fuzzy" in m.flags]
    assert not fuzzy, f"{locale}: {len(fuzzy)} fuzzy entries: {fuzzy[:3]}"
