"""Плагин UI: молчит выключенным, и тест не знает, включён ли сбор."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import ui_flow
from partest_atlas.config import Config
from partest_atlas.plugins import ui_flow as plugin

pytest_plugins = ["pytester"]


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


def test_drain_surfaces_page_evaluation_failure():
    """Плагин перехватит ошибку; молча выдать неполный снимок за полный нельзя."""

    class Упрямая(ФейковаяСтраница):
        def evaluate(self, expr):
            raise RuntimeError("страница закрыта")

    page = Упрямая()
    recorder = ui_flow.Recorder().attach(page)
    with pytest.raises(RuntimeError, match="страница закрыта"):
        recorder.drain(page)


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


# --- сквозная provenance и пустой текущий документ ---------------------------


def _настроить_прогон(pytester, monkeypatch):
    корень = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [корень, os.getenv("PYTHONPATH")])))
    monkeypatch.setenv(plugin.ENABLE_ENV, "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for имя in ("ATLAS_SYSTEM", "ATLAS_ENV"):
        monkeypatch.delenv(имя, raising=False)
    pytester.makefile(".yaml", atlas="artifacts_dir: atlas\n")


def _тест_страницы(pytester, endpoint=None):
    actions = [{"type": "click", "ts": 0, "control": "save", "screen": "/screen"}] if endpoint else []
    call = (
        f"    recorder.calls.append({{'ts': 10, 'url': {endpoint!r}, 'method': 'GET', 'status': 200}})"
        if endpoint else ""
    )
    pytester.makepyfile(test_flow=f"""
import pytest


class Page:
    def __init__(self, actions):
        self.actions = actions

    def add_init_script(self, script):
        pass

    def evaluate(self, expr):
        return self.actions

    def on(self, event, handler):
        pass


@pytest.mark.parametrize("variant", ["a", "b"])
def test_screen(atlas_ui_flow, variant):
    recorder = atlas_ui_flow(Page({actions!r}))
{call}
""")


def _путь_артефакта(pytester):
    return Config(root=pytester.path, raw={"artifacts_dir": "atlas"}).path_for(ui_flow.KIND)


def _запустить(pytester, monkeypatch, run_id, *args):
    monkeypatch.setenv(artifacts.RUN_ID_ENV, run_id)
    return pytester.runpytest_subprocess(
        "-p", "no:partest_atlas_ui_flow", "-p", "partest_atlas.plugins.ui_flow",
        "-p", "no:asyncio", "-p", "no:cacheprovider", *args,
    )


def _прогнать(pytester, monkeypatch, run_id, *args, passed):
    result = _запустить(pytester, monkeypatch, run_id, *args)
    result.assert_outcomes(passed=passed)
    doc = artifacts.read(_путь_артефакта(pytester))
    assert artifacts.validate(doc, kind=ui_flow.KIND) == []
    return doc


def test_real_plugin_partial_a_writes_current_variant_for_later_merge(pytester, monkeypatch):
    """Плагин пишет текущий прогон; сохранение B делает последующий явный merge."""
    _настроить_прогон(pytester, monkeypatch)
    _тест_страницы(pytester, "/api/old")
    initial = _прогнать(pytester, monkeypatch, "run-full", passed=2)
    base = "test_flow.py::test_screen"
    assert {(r["nodeid"], r["variant"], r["run"]["id"]) for r in initial["links"]} == {
        (base, base + "[a]", "run-full"), (base, base + "[b]", "run-full"),
    }

    _тест_страницы(pytester, "/api/new")
    current = _прогнать(
        pytester, monkeypatch, "run-partial-a", "test_flow.py::test_screen[a]", passed=1,
    )
    assert [(r["variant"], r["path"], r["run"]["id"]) for r in current["links"]] == [
        (base + "[a]", "/api/new", "run-partial-a"),
    ]

    touched = {r["variant"] for r in current["links"]}
    preserved = [r for r in initial["links"] if r["variant"] not in touched]
    merged = ui_flow.build(preserved + current["links"])
    assert {(r["variant"], r["path"]) for r in merged["links"]} == {
        (base + "[a]", "/api/new"), (base + "[b]", "/api/old"),
    }


def test_real_plugin_empty_partial_writes_empty_current_file_for_later_merge(pytester, monkeypatch):
    """Пустой partial не удаляет B на диске сам: B сохраняет явный merge вызывающего."""
    _настроить_прогон(pytester, monkeypatch)
    _тест_страницы(pytester, "/api/old")
    initial = _прогнать(pytester, monkeypatch, "run-full", passed=2)
    base = "test_flow.py::test_screen"

    _тест_страницы(pytester)
    current = _прогнать(
        pytester, monkeypatch, "run-empty-a", "test_flow.py::test_screen[a]", passed=1,
    )
    assert current["links"] == []
    assert current["stats"] == {"links": 0, "screens": 0, "endpoints": 0, "direct": 0}

    selected = {base + "[a]"}
    preserved = [r for r in initial["links"] if r["variant"] not in selected]
    merged = ui_flow.build(preserved + current["links"])
    assert [(r["variant"], r["path"]) for r in merged["links"]] == [(base + "[b]", "/api/old")]


def test_real_plugin_full_empty_replaces_the_previous_file(pytester, monkeypatch):
    _настроить_прогон(pytester, monkeypatch)
    _тест_страницы(pytester, "/api/old")
    assert _прогнать(pytester, monkeypatch, "run-full-old", passed=2)["links"]

    _тест_страницы(pytester)
    current = _прогнать(pytester, monkeypatch, "run-full-empty", passed=2)

    assert current["links"] == []
    assert current["stats"] == {"links": 0, "screens": 0, "endpoints": 0, "direct": 0}


def _прежний_файл(pytester, monkeypatch):
    _настроить_прогон(pytester, monkeypatch)
    _тест_страницы(pytester, "/api/old")
    assert _прогнать(pytester, monkeypatch, "run-previous", passed=2)["links"]
    path = _путь_артефакта(pytester)
    return path, path.read_bytes()


def test_real_plugin_collect_only_preserves_previous_file_bytes(pytester, monkeypatch):
    path, before = _прежний_файл(pytester, monkeypatch)

    result = _запустить(pytester, monkeypatch, "run-collect-only", "--collect-only")

    assert result.ret == 0
    assert "тесты не исполнялись" in result.stdout.str()
    assert path.read_bytes() == before


def test_real_plugin_collection_error_preserves_previous_file_bytes(pytester, monkeypatch):
    path, before = _прежний_файл(pytester, monkeypatch)
    pytester.makepyfile(test_flow="def broken(:\n")

    result = _запустить(pytester, monkeypatch, "run-collection-error")

    assert result.ret == pytest.ExitCode.INTERRUPTED
    assert "тесты не исполнялись" in result.stdout.str()
    assert path.read_bytes() == before


def test_real_plugin_tests_failed_writes_current_links(pytester, monkeypatch):
    path, _ = _прежний_файл(pytester, monkeypatch)
    _тест_страницы(pytester, "/api/new")
    pytester.makepyfile(test_failure="def test_failure():\n    assert False\n")

    result = _запустить(
        pytester, monkeypatch, "run-tests-failed",
        "test_flow.py::test_screen[a]", "test_failure.py",
    )

    result.assert_outcomes(passed=1, failed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    doc = artifacts.read(path)
    assert artifacts.validate(doc, kind=ui_flow.KIND) == []
    assert [(r["variant"], r["path"], r["run"]["id"]) for r in doc["links"]] == [
        ("test_flow.py::test_screen[a]", "/api/new", "run-tests-failed"),
    ]


def test_real_plugin_drain_failure_preserves_previous_file_bytes(pytester, monkeypatch):
    path, before = _прежний_файл(pytester, monkeypatch)
    pytester.makepyfile(test_flow="""
class Page:
    def add_init_script(self, script):
        pass

    def evaluate(self, expr):
        raise RuntimeError("drain failed")

    def on(self, event, handler):
        pass


def test_screen(atlas_ui_flow):
    atlas_ui_flow(Page())
""")

    result = _запустить(pytester, monkeypatch, "run-drain-failure")

    result.assert_outcomes(passed=1)
    assert "не все страницы дочитаны" in result.stdout.str()
    assert path.read_bytes() == before


def test_real_plugin_tests_failed_and_drain_failure_preserve_previous_file_bytes(pytester, monkeypatch):
    path, before = _прежний_файл(pytester, monkeypatch)
    pytester.makepyfile(test_flow="""
class Page:
    def add_init_script(self, script):
        pass

    def evaluate(self, expr):
        raise RuntimeError("drain failed")

    def on(self, event, handler):
        pass


def test_screen(atlas_ui_flow):
    atlas_ui_flow(Page())
""")
    pytester.makepyfile(test_failure="def test_failure():\n    assert False\n")

    result = _запустить(pytester, monkeypatch, "run-tests-failed-drain-failure")

    result.assert_outcomes(passed=1, failed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "не все страницы дочитаны" in result.stdout.str()
    assert path.read_bytes() == before


def test_disabled_plugin_preserves_previous_file_bytes(pytester, monkeypatch):
    path, before = _прежний_файл(pytester, monkeypatch)
    monkeypatch.delenv(plugin.ENABLE_ENV)
    _тест_страницы(pytester)

    result = _запустить(pytester, monkeypatch, "run-disabled")

    result.assert_outcomes(passed=2)
    assert path.read_bytes() == before
