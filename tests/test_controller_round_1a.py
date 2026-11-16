"""Проверки краевых случаев CLI и артефактов: каждый случай покрыт тестом.

Номера в именах тестов не ставятся; суть случая названа в docstring.
"""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap

import pytest

from partest_atlas import artifacts, cli
from partest_atlas import config as cfgmod
from partest_atlas.adapters import postgres
from partest_atlas.artifacts import Scope
from partest_atlas.collect import db_effects, db_schema
from partest_atlas.plugins import db_effects as effects_plugin

СПЕЦИФИКАЦИЯ = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1"},
    "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}},
}

МОДУЛИ = ("confpartest",)


@pytest.fixture
def проект(tmp_path, monkeypatch):
    корень = tmp_path / "project"
    корень.mkdir()
    (корень / "atlas.yaml").write_text("artifacts_dir: atlas\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for имя in ("ATLAS_SYSTEM", "ATLAS_ENV", "DATABASE_URL", "PARTEST_TLS_VERIFY"):
        monkeypatch.delenv(имя, raising=False)
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield корень
    for имя in МОДУЛИ:
        sys.modules.pop(имя, None)


def _run(capsys, *args):
    for имя in МОДУЛИ:
        sys.modules.pop(имя, None)
    код = cli.main([str(a) for a in args])
    out, err = capsys.readouterr()
    return код, out, err


def _spec(корень, документ=None, swagger=None):
    (корень / "spec.json").write_text(json.dumps(документ or СПЕЦИФИКАЦИЯ, ensure_ascii=False), encoding="utf-8")
    (корень / "confpartest.py").write_text(
        f"swagger_files = {swagger or {'shop': ['local', 'spec.json']}!r}\n", encoding="utf-8")


def _artifact(корень, kind="contract.openapi"):
    return корень / "atlas" / "default" / "default" / artifacts.filename(kind)


# --- security 1: строка подключения не утекает -------------------------------------


def test_a_broken_dsn_does_not_leak_the_password(проект, capsys, monkeypatch):
    pytest.importorskip("psycopg")
    monkeypatch.setenv("DATABASE_URL", "app:S3cr3tPass@db.internal/prod")
    код, _, err = _run(capsys, "collect", "db-schema", "--root", проект)
    assert код == 1
    assert "S3cr3tPass" not in err and "DATABASE_URL" in err


def test_the_dsn_path_gets_a_connect_timeout(monkeypatch):
    """Недоступный хост (VPN не поднят) держал команду минутами (разбор г)."""
    psycopg = pytest.importorskip("psycopg")
    увидено = {}

    def connect(dsn, **kw):
        увидено.update(kw)
        raise psycopg.OperationalError("нет маршрута")

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")
    with pytest.raises(postgres.ConnectionFailed):
        with postgres.connect({}):
            pass
    assert увидено["connect_timeout"] == postgres.DEFAULT_CONNECT_TIMEOUT_SEC


def test_a_timeout_in_the_dsn_is_left_alone(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    увидено = {}
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kw: (увидено.update(kw), (_ for _ in ()).throw(OSError()))[0])
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db?connect_timeout=3")
    with pytest.raises(postgres.ConnectionFailed):
        with postgres.connect({}):
            pass
    assert "connect_timeout" not in увидено


# --- security 3: чужой confpartest не исполняется ------------------------------------


def test_a_foreign_confpartest_earlier_on_sys_path_is_never_imported(проект, capsys, tmp_path):
    чужой = tmp_path / "planted"
    чужой.mkdir()
    метка = tmp_path / "executed.txt"
    (чужой / "confpartest.py").write_text(
        f"open({str(метка)!r}, 'w').write('x')\nswagger_files = {{}}\n", encoding="utf-8")
    _spec(проект)
    sys.path[:0] = [str(чужой), str(проект)]  # проект в пути, но позже чужого

    код, _, err = _run(capsys, "collect", "openapi", "--root", проект)

    assert код == 0, err
    assert not метка.exists(), "чужой confpartest исполнился"


# --- security 5: всё, что приходит в артефакт извне, — через redact ----------------------


def test_strings_from_the_spec_go_through_redact(проект, capsys):
    документ = {**СПЕЦИФИКАЦИЯ, "paths": {"/items": {"get": {
        "description": "admin password=hunter2 for tests", "responses": {"200": {}}}}}}
    _spec(проект, документ)
    assert _run(capsys, "collect", "openapi", "--root", проект)[0] == 0
    assert "hunter2" not in _artifact(проект).read_text(encoding="utf-8")


def test_every_artifact_write_redacts_its_payload(tmp_path):
    путь = tmp_path / "db_schema.json"
    artifacts.write(путь, "db.schema", {"databases": [{"label": "token=abc123secret"}],
                                         "note": {"Bearer xyz.secret": 1}})
    текст = путь.read_text(encoding="utf-8")
    assert "abc123secret" not in текст and "xyz.secret" not in текст


def test_control_characters_from_the_spec_are_not_printed(проект, capsys):
    _spec(проект, {**СПЕЦИФИКАЦИЯ, "info": {"title": "Shop\x1b[2J", "version": "1"}})
    код, out, _ = _run(capsys, "collect", "openapi", "--root", проект)
    assert код == 0 and "\x1b" not in out


# --- security 6: блок только на чтение не остаётся открытым -------------------------------


def test_read_only_rolls_back_when_its_own_statements_fail():
    запросы = []

    class Курсор:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            запросы.append(sql)
            if sql == postgres.SET_READ_ONLY_SQL:
                raise RuntimeError("сбой")

    class Соединение:
        def cursor(self):
            return Курсор()

    with pytest.raises(RuntimeError):
        with postgres.read_only(Соединение()):
            pytest.fail("блок выполнился")
    assert запросы[-1] == postgres.END_SQL


# --- разбор 1: таблица — пара (схема, имя) ---------------------------------------------


def _col(schema, table, name="id"):
    return {"table_schema": schema, "table_name": table, "column_name": name, "ordinal_position": 1,
            "data_type": "integer", "is_nullable": "NO", "column_default": None,
            "character_maximum_length": None}


def test_same_named_tables_in_two_schemas_keep_their_own_rows_and_keys():
    doc = db_schema.build(
        [_col("public", "users"), _col("audit", "users"), _col("audit", "log", "user_id")],
        foreign_keys=[{"table_schema": "audit", "table_name": "log", "column_name": "user_id",
                       "ref_schema": "audit", "ref_table": "users", "ref_column": "id",
                       "delete_rule": "CASCADE"}],
        row_counts=[{"table_schema": "public", "table_name": "users", "rows": 10},
                    {"table_schema": "audit", "table_name": "users", "rows": 99}],
        schemas=["public", "audit"],
    )
    tables = {(t["schema"], t["name"]): t for t in doc["databases"][0]["tables"]}
    assert tables[("public", "users")]["rows"] == 10
    assert tables[("audit", "users")]["rows"] == 99
    fk = tables[("audit", "log")]["columns"][0]["fk"]
    assert fk == {"table": "users", "column": "id", "onDelete": "CASCADE", "schema": "audit"}


def test_exclude_by_schema_and_name():
    doc = db_schema.build([_col("public", "users"), _col("audit", "users")], exclude=["audit.USERS"])
    assert [(t["schema"], t["name"]) for t in doc["databases"][0]["tables"]] == [("public", "users")]


def test_the_fk_query_joins_by_constraint_schema():
    sql = " ".join(db_schema.FOREIGN_KEYS_SQL.split())
    assert "rc.constraint_schema = tc.constraint_schema" in sql
    assert "kcu.constraint_schema = tc.constraint_schema" in sql
    assert "constraint_column_usage" not in sql


def test_effects_of_same_named_tables_in_two_schemas_stay_apart():
    before = {"public.users": {"table_schema": "public", "table_name": "users", "ins": 0, "upd": 0, "del": 0},
              "audit.users": {"table_schema": "audit", "table_name": "users", "ins": 1000, "upd": 0, "del": 0}}
    after = {"public.users": {"table_schema": "public", "table_name": "users", "ins": 2, "upd": 0, "del": 0},
             "audit.users": {"table_schema": "audit", "table_name": "users", "ins": 1000, "upd": 0, "del": 0}}
    rows = db_effects.diff(before, after)
    assert [(r["schema"], r["table"], r["insert"]) for r in rows] == [("public", "users", 2)]
    assert db_effects.snapshot_key({"table_schema": "audit", "table_name": "users"}) == "audit.users"
    assert "ORDER BY" in db_effects.STATS_SQL


# --- разбор 4: вывод на Windows не падает после записи ----------------------------------


def test_output_in_a_narrow_code_page_does_not_crash_after_writing(проект, monkeypatch, capsys):
    _spec(проект, swagger={"магазин": ["local", "spec.json"]})
    поток = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", поток)
    sys.modules.pop("confpartest", None)
    код = cli.main(["--lang", "en", "collect", "openapi", "--root", str(проект)])
    assert код == 0
    assert _artifact(проект).is_file()


def test_the_help_has_no_arrow_a_code_page_lacks():
    import inspect

    assert "→" not in inspect.getsource(cli._parser)


# --- разбор 5: ATLAS_DB_EFFECTS_ALL — дельта прогона ------------------------------------


def test_all_means_one_session_delta_even_for_a_small_suite(monkeypatch):
    monkeypatch.setenv(effects_plugin.ALL_ENV, "1")
    assert effects_plugin.choose_mode(3, 12) == db_effects.SESSION
    monkeypatch.delenv(effects_plugin.ALL_ENV)
    assert effects_plugin.choose_mode(3, 12) == db_effects.TEST


# --- решение по договору 3: keep_runs --------------------------------------------------------


def _record(run_id, started, table="t"):
    return {"table": table, "run": {"id": run_id, "startedAt": started}}


def test_keep_runs_leaves_the_last_n_runs_by_start_time(tmp_path):
    путь = tmp_path / "db_effects.json"
    for i in range(5):
        artifacts.append_records(путь, "db.effects", "records",
                                 [_record(f"r{i}", f"2026-01-0{i + 1}T00:00:00+00:00")], keep_runs=3)
    assert [r["run"]["id"] for r in artifacts.read(путь)["records"]] == ["r2", "r3", "r4"]


def test_keep_runs_compares_times_not_strings():
    """`+03:00` раньше `+00:00` того же часа — сравнивается время, а не текст."""
    records = [_record("a", "2026-01-01T10:00:00+03:00"), _record("b", "2026-01-01T08:00:00+00:00")]
    assert [r["run"]["id"] for r in artifacts.keep_last_runs(records, 1)] == ["b"]


def test_unlabelled_records_go_first_and_zero_keeps_everything():
    # Одна и та же связь (таблица `t`): старую запись не держит и правило «последняя на связь».
    records = [{"table": "t"}, _record("a", "2026-01-01T00:00:00+00:00")]
    assert artifacts.keep_last_runs(records, 1) == [records[1]]
    assert artifacts.keep_last_runs(records, 0) == records


def test_keep_runs_is_read_from_the_plugin_section(monkeypatch, tmp_path):
    (tmp_path / "atlas.yaml").write_text("db_effects:\n  keep_runs: 0\n", encoding="utf-8")
    monkeypatch.setenv(effects_plugin.ENABLE_ENV, "1")
    monkeypatch.setattr(effects_plugin, "load", lambda *a, **kw: cfgmod.load(tmp_path))

    class Опции:
        dist, tx = "no", []

    class Конфиг:
        option = Опции()

        def addinivalue_line(self, *a):
            pass

    effects_plugin._state.clear()
    effects_plugin.pytest_configure(Конфиг())
    try:
        assert effects_plugin._state["keep_runs"] == 0
    finally:
        effects_plugin._state.clear()


# --- решение по договору 2: правило имени области -------------------------------------------


@pytest.mark.parametrize("имя", ["Склад", "a b", "..", ".", "a..b", "a/b", "", "x:y"])
def test_scope_refuses_names_the_reader_would_not_show(имя):
    with pytest.raises(ValueError):
        Scope(system=имя)


@pytest.mark.parametrize("имя", ["default", "shop", "pre-prod", "v1.2", "a_b"])
def test_scope_accepts_portable_names(имя):
    assert Scope(system=имя).system == имя


def test_validate_names_a_bad_scope_instead_of_raising():
    doc = {"version": 2, "kind": "db.schema", "scope": {"system": "../x", "env": "e"},
           "collected_at": "2026-01-01T00:00:00+00:00", "collector": ""}
    assert any("scope.system" in p for p in artifacts.validate(doc))


def test_discover_scopes_skips_directories_with_bad_names(tmp_path):
    artifacts.write(tmp_path / "shop" / "test" / "db_schema.json", "db.schema", {},
                    scope=Scope("shop", "test"))
    (tmp_path / "Склад" / "test").mkdir(parents=True)
    (tmp_path / "Склад" / "test" / "db_schema.json").write_text("{}", encoding="utf-8")
    assert artifacts.discover_scopes(tmp_path) == [Scope("shop", "test")]


# --- решение по договору 6: договор atlas-config --------------------------------------------


def test_unknown_keys_of_own_sections_are_named_and_foreign_sections_left_alone(tmp_path):
    (tmp_path / "atlas.yaml").write_text(textwrap.dedent("""
        database:
          schemaz: auto
        reports:
          anything: 1
        db_effects:
          keep_run: 3
    """), encoding="utf-8")
    assert sorted(cfgmod.load(tmp_path).unknown_keys()) == ["database.schemaz", "db_effects.keep_run"]


def test_collect_names_an_unknown_key(проект, capsys):
    (проект / "atlas.yaml").write_text("artifacts_dir: atlas\nproject:\n  nmae: x\n", encoding="utf-8")
    _spec(проект)
    код, _, err = _run(capsys, "collect", "openapi", "--root", проект)
    assert код == 0 and "project.nmae" in err


@pytest.mark.parametrize("путь", ["\\\\\\\\server\\\\share\\\\atlas", "//server/share/atlas"])
def test_a_unc_artifacts_dir_is_refused_without_showing_it(tmp_path, путь):
    (tmp_path / "atlas.yaml").write_text(f"artifacts_dir: '{путь}'\n", encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError) as отказ:
        cfgmod.load(tmp_path)
    assert "UNC" in str(отказ.value) and "server" not in str(отказ.value)


def test_a_section_that_is_not_an_object_is_a_configuration_error(tmp_path):
    (tmp_path / "atlas.yaml").write_text("database: 5\n", encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError, match="database"):
        cfgmod.load(tmp_path)


def test_a_bad_number_names_the_key_not_the_value():
    with pytest.raises(ValueError) as отказ:
        cfgmod.non_negative({"settle_sec": "секрет42"}, "settle_sec", 1.0, float, where="db_effects")
    assert "db_effects.settle_sec" in str(отказ.value) and "секрет42" not in str(отказ.value)


# --- qa: при неудаче прежний артефакт не тронут ---------------------------------------------


def test_a_failed_collect_leaves_the_previous_artifact_byte_for_byte(проект, capsys):
    _spec(проект)
    assert _run(capsys, "collect", "openapi", "--root", проект)[0] == 0
    было = _artifact(проект).read_bytes()
    (проект / "spec.json").write_text("{}", encoding="utf-8")  # не OpenAPI

    код, _, _ = _run(capsys, "collect", "openapi", "--root", проект)

    assert код == 1
    assert _artifact(проект).read_bytes() == было


# --- qa: живая база — по желанию ----------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("ATLAS_TEST_PG_DSN"), reason="ATLAS_TEST_PG_DSN не задан")
def test_read_only_against_a_live_postgres():
    """Набор проверяет `read_only` подменой соединения; эта — на живой базе, если задан DSN
    своей (не стенда!) базы: запись внутри блока отклоняет сама база."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    for autocommit in (False, True):
        with psycopg.connect(os.environ["ATLAS_TEST_PG_DSN"], row_factory=dict_row,
                             autocommit=autocommit) as conn:
            with postgres.read_only(conn), conn.cursor() as cur:
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    cur.execute("CREATE TEMP TABLE atlas_probe (x int)")
