"""Плагин шины: молчит выключенным и различает три исхода, а не два."""

from __future__ import annotations

import pytest

from partest_atlas.plugins import bus_flow as plugin


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
    monkeypatch.delenv(plugin.ENABLE_ENV, raising=False)
    monkeypatch.setattr(
        plugin, "load", lambda *a, **kw: pytest.fail("конфигурация прочитана на выключенном плагине")
    )
    plugin.pytest_configure(ФейковыйConfig())
    assert plugin._state == {}


def test_it_registers_its_marker_even_when_off(monkeypatch):
    monkeypatch.delenv(plugin.ENABLE_ENV, raising=False)
    cfg = ФейковыйConfig()
    plugin.pytest_configure(cfg)
    assert any(plugin.MARKER in " ".join(map(str, a)) for a in cfg.строки)


def test_not_configured_is_an_ordinary_line(capsys):
    """Проект шину не описал — это не поломка и кричать об этом нечего."""
    plugin._note("читатель шины не настроен")
    out = capsys.readouterr().out
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" not in out
    assert "не настроен" in out


def test_a_misconfiguration_shouts_and_says_where_to_fix(capsys):
    """Тот самый случай: клиент отверг ключ, а сборщик сообщал «брокер недоступен»."""
    plugin._note("Unrecognized configs: {'api_version_auto_timeout_ms'}", misconfigured=True)
    out = capsys.readouterr().out
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" in out
    assert "чинить здесь" in out


def test_an_unavailable_broker_is_worded_as_the_stand(capsys):
    plugin._note("источник недоступен (OSError: нет маршрута)")
    out = capsys.readouterr().out
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" not in out
    assert "недоступен" in out


def test_the_three_outcomes_produce_three_different_notes(capsys):
    """Ради чего всё и написано: по выводу должно быть видно, куда идти чинить."""
    plugin._note("читатель шины не настроен")
    один = capsys.readouterr().out
    plugin._note("bus_flow.reader кривой", misconfigured=True)
    два = capsys.readouterr().out
    plugin._note("источник недоступен (timeout)")
    три = capsys.readouterr().out
    assert len({один, два, три}) == 3
