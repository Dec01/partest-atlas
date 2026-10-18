"""Плагин шины: молчит выключенным и различает три исхода, а не два."""

from __future__ import annotations

import os
from pathlib import Path

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


def test_an_xdist_worker_stays_silent(monkeypatch):
    """Смещения снимает контроллер: окна воркеров видели бы те же события по разу на
    воркер, и файл дописывали бы несколько процессов без блокировки."""
    monkeypatch.setenv(plugin.ENABLE_ENV, "1")
    monkeypatch.setattr(
        plugin, "load", lambda *a, **kw: pytest.fail("воркер прочитал конфигурацию")
    )
    cfg = ФейковыйConfig()
    cfg.workerinput = {}
    plugin.pytest_configure(cfg)
    assert plugin._state == {}


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


# --- числа в atlas.yaml и xdist ------------------------------------------

pytest_plugins = ["pytester"]


def _включить(monkeypatch, tmp_path, yaml_text):
    from partest_atlas.config import load

    (tmp_path / "atlas.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv(plugin.ENABLE_ENV, "1")
    monkeypatch.setattr(plugin, "load", lambda *a, **kw: load(tmp_path))


def test_a_zero_settle_is_zero(monkeypatch, tmp_path):
    """Как у `db_effects`: `or` подменял ноль умолчанием."""
    _включить(monkeypatch, tmp_path, "bus_flow:\n  settle_sec: 0\n")
    plugin.pytest_configure(ФейковыйConfig())
    assert plugin._state["settle"] == 0.0


@pytest.mark.parametrize("значение", ["abc", "-1", ".nan", ".inf"])
def test_a_bad_settle_leaves_the_plugin_off(monkeypatch, tmp_path, capsys, значение):
    """Ошибка конфигурации, а не INTERNALERROR; и без недостроенного состояния, на котором
    следующий хук упал бы сам."""
    _включить(monkeypatch, tmp_path, f"bus_flow:\n  settle_sec: {значение}\n")
    plugin.pytest_configure(ФейковыйConfig())
    assert plugin._state == {}
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" in capsys.readouterr().out
    plugin.pytest_sessionstart(None)  # не падает: плагин выключен


ЧИТАТЕЛЬ = """
import contextlib
from pathlib import Path

КАТАЛОГ = Path(__file__).resolve().parent


class Читатель:
    def list_topics(self):
        return ["заказы"]

    def end_offsets(self, topic):
        return {0: len(list(КАТАЛОГ.glob("событие-*.txt")))}


@contextlib.contextmanager
def открыть():
    yield Читатель()
"""


def test_under_xdist_the_controller_writes_bus_flow(pytester, monkeypatch):
    """Воркеры молчат, а смещения на весь прогон снимает контроллер. Часы HTTP живут в
    воркерах, поэтому `afterWrite` и `http` в записи контроллера — `false` и нули."""
    pytest.importorskip("xdist")
    from partest_atlas import artifacts
    from partest_atlas.collect import bus_flow
    from partest_atlas.config import load

    корень = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [корень, os.getenv("PYTHONPATH")])))
    monkeypatch.setenv(plugin.ENABLE_ENV, "1")
    for имя in ("ATLAS_SYSTEM", "ATLAS_ENV"):
        monkeypatch.delenv(имя, raising=False)
    monkeypatch.setattr(bus_flow.time, "sleep", lambda s: None)
    pytester.makepyfile(читатель_шины=ЧИТАТЕЛЬ)
    pytester.syspathinsert()
    pytester.makefile(".yaml", atlas="artifacts_dir: atlas\nbus_flow:\n  reader: читатель_шины:открыть\n")
    pytester.makepyfile(test_события="""
import uuid
from pathlib import Path

def test_a():
    Path(f"событие-{uuid.uuid4().hex}.txt").write_text("1")

def test_b():
    Path(f"событие-{uuid.uuid4().hex}.txt").write_text("1")
""")
    result = pytester.runpytest_inprocess(
        "-p", "no:partest_atlas_bus_flow", "-p", "partest_atlas.plugins.bus_flow",
        "-p", "no:asyncio", "-p", "no:cacheprovider", "-p", "no:benchmark", "-n", "2",
    )
    result.assert_outcomes(passed=2)
    doc = artifacts.read(load(pytester.path).path_for(bus_flow.KIND))
    assert [(r["topic"], r["events"], r["scope"]) for r in doc["records"]] == [
        ("заказы", 2, bus_flow.SESSION)
    ]
    assert doc["records"][0]["afterWrite"] is False
    assert doc["records"][0]["http"] == {"requests": 0, "writes": 0}
