"""Соединение называет проект, а не выбирает пакет."""

from __future__ import annotations

import contextlib
import sys
import types

import pytest

from partest_atlas.adapters import postgres
from partest_atlas.adapters.postgres import (
    ConnectionNotConfigured,
    NotReadOnly,
    connect,
    discover_schemas,
    read_only,
    schemas_from,
)


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


# --- только чтение — механизмом -------------------------------------------------------


class Соединение:
    """Отвечает на запросы режима; *ответ* — что база скажет на `SHOW transaction_read_only`."""

    def __init__(self, ответ=("on",)):
        self.ответ = ответ
        self.запросы = []

    def cursor(self):
        conn = self

        class Курсор:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def execute(self_, sql, params=None):
                conn.запросы.append(sql)

            def fetchone(self_):
                return conn.ответ

        return Курсор()


def test_read_only_sets_the_transaction_mode_checks_it_and_rolls_back():
    conn = Соединение()
    with read_only(conn):
        conn.запросы.append("работа")
    assert conn.запросы == [
        postgres.BEGIN_READ_ONLY_SQL,
        postgres.SET_READ_ONLY_SQL,
        postgres.CHECK_READ_ONLY_SQL,
        "работа",
        postgres.END_SQL,
    ]


def test_read_only_accepts_a_dict_row_too():
    conn = Соединение(ответ={"transaction_read_only": "on"})
    with read_only(conn):
        pass


@pytest.mark.parametrize("ответ", [("off",), None, ()], ids=["off", "нет-строки", "пустая"])
def test_without_confirmation_nothing_runs(ответ):
    conn = Соединение(ответ=ответ)
    with pytest.raises(NotReadOnly, match="только чтения"):
        with read_only(conn):
            pytest.fail("блок выполнился без подтверждения режима")
    assert conn.запросы[-1] == postgres.END_SQL


def test_the_block_is_rolled_back_even_when_the_work_fails():
    conn = Соединение()
    with pytest.raises(RuntimeError):
        with read_only(conn):
            raise RuntimeError("сборщик упал")
    assert conn.запросы[-1] == postgres.END_SQL


def test_the_mode_is_never_set_for_the_session():
    """Настройка сессии пережила бы нас на соединении из пула проекта."""
    for sql in (postgres.BEGIN_READ_ONLY_SQL, postgres.SET_READ_ONLY_SQL):
        assert "SESSION" not in sql.upper()
        assert "DEFAULT_TRANSACTION" not in sql.upper()


@pytest.mark.parametrize(
    ("значение", "схемы"),
    [
        (None, ["public"]),
        ([], ["public"]),
        ("", ["public"]),
        ("public", ["public"]),
        (["a", "b"], ["a", "b"]),
        ("auto", None),
        ("AUTO", None),
        (["auto"], None),
    ],
)
def test_schemas_from_config(значение, схемы):
    """Строка — одна схема, а не её буквы: `schemas: public` давал `['p', 'u', …]`."""
    раздел = {} if значение is None else {"schemas": значение}
    assert schemas_from(раздел) == схемы


@pytest.mark.parametrize("значение", [5, {"a": 1}, ["a", 1], ["a", ""]])
def test_a_bad_schemas_value_is_named(значение):
    with pytest.raises(ValueError, match="database.schemas"):
        schemas_from({"schemas": значение})


def test_discover_schemas_reads_inside_a_read_only_transaction():
    class СоСхемами(Соединение):
        def cursor(self):
            курсор = super().cursor()
            курсор.fetchall = lambda: [{"schema_name": "shop"}, ("billing",)]
            return курсор

    conn = СоСхемами()
    assert discover_schemas(conn) == ["shop", "billing"]
    assert conn.запросы[:3] == [
        postgres.BEGIN_READ_ONLY_SQL, postgres.SET_READ_ONLY_SQL, postgres.CHECK_READ_ONLY_SQL,
    ]
    assert conn.запросы[3] == postgres.SCHEMAS_SQL
    assert conn.запросы[-1] == postgres.END_SQL
