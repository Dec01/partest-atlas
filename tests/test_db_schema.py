"""Сборщик схемы: форма артефакта проверяется без базы.

Это и есть смысл правки при переносе. В прототипе опрос и лепка были одним куском, и
проверить форму можно было только на живой базе — то есть на практике не проверять.
Здесь `build` принимает уже прочитанные строки, и весь разбор — обычный тест.
"""

from __future__ import annotations

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import db_schema


def col(table, name, *, type_="integer", nullable="NO", default=None, length=None, schema="public"):
    return {
        "table_schema": schema,
        "table_name": table,
        "column_name": name,
        "ordinal_position": 1,
        "data_type": type_,
        "is_nullable": nullable,
        "column_default": default,
        "character_maximum_length": length,
    }


@pytest.fixture()
def rows():
    return {
        "columns": [
            col("users", "id"),
            col("users", "email", type_="character varying", length=255, nullable="YES"),
            col("orders", "id"),
            col("orders", "user_id"),
        ],
        "primary_keys": [
            {"table_schema": "public", "table_name": "users", "column_name": "id"},
            {"table_schema": "public", "table_name": "orders", "column_name": "id"},
        ],
        "foreign_keys": [
            {
                "table_schema": "public",
                "table_name": "orders",
                "column_name": "user_id",
                "ref_table": "users",
                "ref_column": "id",
                "delete_rule": "CASCADE",
            }
        ],
        "row_counts": [{"table_name": "users", "rows": 42}],
    }


def _build(rows, **kw):
    return db_schema.build(
        rows["columns"], rows["primary_keys"], rows["foreign_keys"], rows["row_counts"], **kw
    )


def test_tables_are_grouped_and_ordered(rows):
    tables = _build(rows)["databases"][0]["tables"]
    assert [t["name"] for t in tables] == ["orders", "users"]


def test_primary_key_is_marked(rows):
    tables = {t["name"]: t for t in _build(rows)["databases"][0]["tables"]}
    assert tables["users"]["columns"][0]["pk"] is True
    assert tables["users"]["columns"][1]["pk"] is False


def test_foreign_key_carries_target_and_delete_rule(rows):
    tables = {t["name"]: t for t in _build(rows)["databases"][0]["tables"]}
    user_id = next(c for c in tables["orders"]["columns"] if c["name"] == "user_id")
    assert user_id["fk"] == {"table": "users", "column": "id", "onDelete": "CASCADE"}


def test_a_column_without_a_foreign_key_says_none_not_empty(rows):
    """`None` и `{}` читаются по-разному: второе — «связь есть, но пустая»."""
    tables = {t["name"]: t for t in _build(rows)["databases"][0]["tables"]}
    assert tables["users"]["columns"][0]["fk"] is None


def test_row_counts_are_attached_and_absent_stays_none(rows):
    tables = {t["name"]: t for t in _build(rows)["databases"][0]["tables"]}
    assert tables["users"]["rows"] == 42
    # У `orders` статистики нет. Ноль здесь был бы враньём: «таблица пуста» и
    # «мы не смотрели» — разные утверждения.
    assert tables["orders"]["rows"] is None


def test_excluded_tables_do_not_appear(rows):
    tables = _build(rows, exclude=["ORDERS"])["databases"][0]["tables"]
    assert [t["name"] for t in tables] == ["users"]


def test_exclusion_is_case_insensitive_on_both_sides(rows):
    assert _build(rows, exclude=["Orders"])["stats"]["tables"] == 1


def test_stats_agree_with_the_tables_under_them(rows):
    doc = _build(rows)
    tables = doc["databases"][0]["tables"]
    assert doc["stats"]["tables"] == len(tables)
    assert doc["stats"]["columns"] == sum(len(t["columns"]) for t in tables)
    assert doc["stats"]["foreignKeys"] == 1


def test_an_empty_database_builds_a_valid_document():
    """Пустая схема — законное наблюдение, а не отказ собирать."""
    doc = db_schema.build([])
    assert doc["databases"][0]["tables"] == []
    assert doc["stats"] == {"tables": 0, "columns": 0, "foreignKeys": 0}


@pytest.mark.parametrize(
    ("data_type", "length", "ожидалось"),
    [
        ("character varying", 255, "varchar(255)"),
        ("timestamp without time zone", None, "timestamp"),
        ("timestamp with time zone", None, "timestamptz"),
        ("double precision", None, "float8"),
        ("integer", None, "int4"),
        ("bigint", None, "int8"),
        ("boolean", None, "bool"),
        ("jsonb", None, "jsonb"),
    ],
)
def test_types_are_shortened_for_a_table_cell(data_type, length, ожидалось):
    assert db_schema.short_type(
        {"data_type": data_type, "character_maximum_length": length}
    ) == ожидалось


def test_a_long_default_is_clipped_not_dropped():
    """Обрезать — не то же самое, что выбросить: начало выражения говорит, что это."""
    long = "'" + "x" * 500 + "'::text"
    doc = db_schema.build([col("t", "c", default=long)])
    value = doc["databases"][0]["tables"][0]["columns"][0]["default"]
    assert len(value) == db_schema.DEFAULT_CLIP
    assert value.startswith("'xxx")


def test_no_default_is_none_not_empty_string():
    doc = db_schema.build([col("t", "c", default="")])
    assert doc["databases"][0]["tables"][0]["columns"][0]["default"] is None


def test_the_artifact_it_writes_validates_clean(tmp_path, rows, monkeypatch):
    """Сборщик обязан производить документ, проходящий проверку собственного контракта."""
    payload = _build(rows)
    path = tmp_path / "db_schema.json"
    artifacts.write(path, db_schema.KIND, payload, collector="partest-atlas/db_schema")
    assert artifacts.validate(artifacts.read(path)) == []


def test_the_kind_is_one_the_contract_knows():
    assert db_schema.KIND in artifacts.KINDS
    assert artifacts.filename(db_schema.KIND) == "db_schema.json"


# --- соединение приходит из конфигурации, а не из кода ---------------------


def test_connect_refuses_a_malformed_target():
    with pytest.raises(RuntimeError, match="модуль:функция"):
        with db_schema._connect({"connect": "простоимя"}):
            pass


def test_connect_says_which_variable_was_empty(monkeypatch):
    monkeypatch.delenv("ПРОБНАЯ_СТРОКА", raising=False)
    with pytest.raises(RuntimeError, match="ПРОБНАЯ_СТРОКА"):
        with db_schema._connect({"dsn_env": "ПРОБНАЯ_СТРОКА"}):
            pass


def test_connect_uses_the_project_callable_named_in_config(monkeypatch):
    """Ради чего перенос и делался: путь к функции проекта — в конфигурации, не в коде."""
    import contextlib
    import sys
    import types

    module = types.ModuleType("проектный_модуль")

    @contextlib.contextmanager
    def соединение():
        yield "соединение проекта"

    module.соединение = соединение
    monkeypatch.setitem(sys.modules, "проектный_модуль", module)

    with db_schema._connect({"connect": "проектный_модуль:соединение"}) as conn:
        assert conn == "соединение проекта"
