"""Плагин UI: молчит выключенным, и тест не знает, включён ли сбор."""

from __future__ import annotations

import pytest

from partest_atlas.collect import ui_flow
from partest_atlas.plugins import ui_flow as plugin


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


class ФейковаяСтраница:
    def __init__(self, действия=()):
        self.действия = list(действия)
        self.инжектов = 0
        self.подписок = []

    def add_init_script(self, script):
        self.инжектов += 1

    def evaluate(self, expr):
        return self.действия

    def on(self, event, handler):
        self.подписок.append(event)


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


def test_the_recorder_survives_navigation_by_init_script():
    """Без `add_init_script` слушатель исчезал бы после первого перехода, и это
    выглядело бы как «пользователь ничего не делал»."""
    page = ФейковаяСтраница()
    ui_flow.Recorder().attach(page)
    assert page.инжектов == 1
    assert "response" in page.подписок


def test_a_page_that_refuses_evaluation_does_not_break_the_run():
    """Сбор карты — не то, ради чего гоняют UI-тесты."""

    class Упрямая(ФейковаяСтраница):
        def evaluate(self, expr):
            raise RuntimeError("страница закрыта")

    recorder = ui_flow.Recorder()
    recorder.attach(Упрямая())
    assert recorder.drain(Упрямая()) == []


def test_drain_clears_the_buffers():
    """Иначе связи прошлого теста приписались бы следующему."""
    page = ФейковаяСтраница([{"type": "click", "ts": 0, "control": "кнопка", "screen": "э"}])
    recorder = ui_flow.Recorder().attach(page)
    recorder.calls.append({"ts": 10, "url": "/api/v1/x", "method": "GET", "status": 200})
    assert len(recorder.drain(page)) == 1
    assert recorder.drain(page) == []


def test_the_dummy_looks_like_a_recorder_and_does_nothing():
    """Тест не должен обрастать ветками ради карты."""
    пустышка = plugin._Пустышка()
    assert пустышка.drain(ФейковаяСтраница()) == []
