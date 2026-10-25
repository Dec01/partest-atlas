"""Язык вывода: ключ важнее локали, локаль важнее умолчания, умолчание — русский."""

from __future__ import annotations

import pytest

from partest_atlas import lang


@pytest.fixture(autouse=True)
def прежний_язык():
    было = lang.current()
    yield
    lang.set_lang(было)


@pytest.mark.parametrize(
    ("имя", "язык"),
    [
        ("ru_RU.UTF-8", "ru"),
        ("Russian_Russia", "ru"),
        ("ru", "ru"),
        ("en_US.UTF-8", "en"),
        ("de_DE", "en"),
        ("English_United States", "en"),
        # Локаль контейнера и конвейера: вывод тех, кто уже на него опирается, не меняется.
        ("C", "ru"),
        ("C.UTF-8", "ru"),
        ("POSIX", "ru"),
        ("", "ru"),
        (None, "ru"),
    ],
)
def test_the_language_follows_the_locale_name(имя, язык):
    assert lang.from_locale_name(имя) == язык


def test_lc_all_wins_over_lang():
    assert lang.detect({"LC_ALL": "en_US.UTF-8", "LANG": "ru_RU.UTF-8"}) == "en"


def test_lc_messages_wins_over_lang():
    assert lang.detect({"LC_MESSAGES": "ru_RU.UTF-8", "LANG": "en_US.UTF-8"}) == "ru"


def test_an_empty_variable_is_skipped():
    assert lang.detect({"LC_ALL": "", "LANG": "en_GB.UTF-8"}) == "en"


def test_no_locale_variables_given_is_the_default():
    assert lang.detect({}) == lang.DEFAULT == "ru"


def test_an_unknown_language_is_refused_not_silently_russian():
    with pytest.raises(ValueError, match="unknown language"):
        lang.set_lang("fr")


def test_the_pair_follows_the_current_language():
    lang.set_lang("en")
    assert lang.t("да", "yes") == "yes"
    lang.set_lang("ru")
    assert lang.t("да", "yes") == "да"
