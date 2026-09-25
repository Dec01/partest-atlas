"""Плагин эффектов: молчит, пока не включён, и по-разному сообщает о двух бедах."""

from __future__ import annotations

import pytest

from partest_atlas.collect import db_effects
from partest_atlas.plugins import db_effects as plugin


@pytest.fixture(autouse=True)
def чисто():
    plugin._state.clear()
    yield
    plugin._state.clear()


class ФейковыйConfig:
    def __init__(self):
        self.строки = []

    def addinivalue_line(self, *a):
        self.строки.append(a)


def test_it_does_nothing_until_switched_on(monkeypatch):
    """Точка входа грузит модуль у каждого, кто поставил пакет. Обычный прогон не должен
    платить за это ни временем, ни соединениями."""
    monkeypatch.delenv(plugin.ENABLE_ENV, raising=False)
    monkeypatch.setattr(
        plugin, "load", lambda *a, **kw: pytest.fail("конфигурация прочитана на выключенном плагине")
    )
    plugin.pytest_configure(ФейковыйConfig())
    assert plugin._state == {}


def test_it_registers_its_marker_even_when_off(monkeypatch):
    """Иначе `--strict-markers` у потребителя ругался бы на маркер, который мы же и
    предлагаем ставить."""
    monkeypatch.delenv(plugin.ENABLE_ENV, raising=False)
    cfg = ФейковыйConfig()
    plugin.pytest_configure(cfg)
    assert any(plugin.MARKER in " ".join(map(str, a)) for a in cfg.строки)


@pytest.mark.parametrize(
    ("отобрано", "предел", "режим"),
    [
        (0, 12, db_effects.SESSION),
        (1, 12, db_effects.TEST),
        (12, 12, db_effects.TEST),
        (13, 12, db_effects.SESSION),
        (2000, 12, db_effects.SESSION),
    ],
)
def test_mode_is_chosen_by_how_many_tests_are_measured(отобрано, предел, режим):
    """Поштучный замер требует выдержки в секунды — на полном прогоне это часы."""
    assert plugin.choose_mode(отобрано, предел) == режим


def test_zero_selected_still_measures_the_session():
    """Мерить поштучно нечего, но общая дельта прогона осмысленна."""
    assert plugin.choose_mode(0, 12) == db_effects.SESSION


def test_a_misconfiguration_says_it_is_ours(monkeypatch, capsys):
    """Чинится в `atlas.yaml`, а не на стенде — и сообщение обязано это сказать."""
    plugin._note("database.connect=кривое", misconfigured=True)
    out = capsys.readouterr().out
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" in out
    assert "чинить здесь" in out


def test_an_unavailable_source_is_worded_differently(capsys):
    plugin._note("источник недоступен (OSError: нет маршрута)")
    out = capsys.readouterr().out
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" not in out
    assert "выключен" in out


def test_wanted_by_marker(monkeypatch):
    monkeypatch.delenv(plugin.ALL_ENV, raising=False)
    plugin._state["include"] = []
    assert plugin._wanted("файл.py::тест", True) is True
    assert plugin._wanted("файл.py::тест", False) is False


def test_wanted_by_mask(monkeypatch):
    monkeypatch.delenv(plugin.ALL_ENV, raising=False)
    plugin._state["include"] = ["*крупные*"]
    assert plugin._wanted("тесты/крупные/файл.py::т", False) is True
    assert plugin._wanted("тесты/мелкие/файл.py::т", False) is False


def test_the_all_switch_measures_everything(monkeypatch):
    monkeypatch.setenv(plugin.ALL_ENV, "1")
    plugin._state["include"] = []
    assert plugin._wanted("любой", False) is True
