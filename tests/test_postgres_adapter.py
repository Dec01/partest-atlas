"""Соединение называет проект, а не выбирает пакет."""

from __future__ import annotations

import contextlib
import sys
import types

import pytest

from partest_atlas.adapters.postgres import ConnectionNotConfigured, connect


def test_a_malformed_target_is_refused_by_name():
    with pytest.raises(ConnectionNotConfigured, match="модуль:функция"):
        with connect({"connect": "простоимя"}):
            pass


def test_an_unimportable_target_says_so_instead_of_failing_later():
    with pytest.raises(ConnectionNotConfigured, match="не импортируется"):
        with connect({"connect": "нет_такого_модуля:соединение"}):
            pass


def test_an_empty_variable_is_named_in_the_error(monkeypatch):
    """Сообщение обязано называть переменную: иначе «опрашивать нечего» отправляет
    человека искать по всей конфигурации."""
    monkeypatch.delenv("ПРОБНАЯ_СТРОКА", raising=False)
    with pytest.raises(ConnectionNotConfigured, match="ПРОБНАЯ_СТРОКА"):
        with connect({"dsn_env": "ПРОБНАЯ_СТРОКА"}):
            pass


def test_the_default_variable_is_named_too(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConnectionNotConfigured, match="DATABASE_URL"):
        with connect({}):
            pass


def test_the_project_callable_named_in_config_is_used(monkeypatch):
    """Ради чего перенос и делался: путь к функции проекта — в конфигурации, не в коде."""
    module = types.ModuleType("проектный_модуль")

    @contextlib.contextmanager
    def соединение():
        yield "соединение проекта"

    module.соединение = соединение
    monkeypatch.setitem(sys.modules, "проектный_модуль", module)

    with connect({"connect": "проектный_модуль:соединение"}) as conn:
        assert conn == "соединение проекта"


def test_a_misconfiguration_is_its_own_class():
    """Вызывающему нужно отличать «настроено неверно» от «база недоступна»: первое
    чинится в файле, второе на стенде."""
    assert issubclass(ConnectionNotConfigured, RuntimeError)
