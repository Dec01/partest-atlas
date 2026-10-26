"""`partest-atlas collect`: наблюдения без прогона тестов — без кода в проекте.

Каждая подкоманда проверяется на временном проекте: `atlas.yaml`, `confpartest.py`,
модули проекта рядом. Базы нет — её место занимает функция соединения проекта
(`database.connect`), которая отвечает как PostgreSQL и записывает, что у неё спросили.
Сеть — только локальный `http.server` на 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
import textwrap

import pytest

from partest_atlas import artifacts, cli, lang
from partest_atlas.adapters import postgres
from partest_atlas.collect import db_effects, db_schema

СПЕЦИФИКАЦИЯ = {
    "openapi": "3.0.3",
    "info": {"title": "Магазин", "version": "1.4.0"},
    "paths": {
        "/items/{itemId}": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/items": {"post": {"responses": {"201": {"description": "created"}}}},
    },
}

#: Модули проекта, которые пишутся во временный каталог. Импортированный модуль остаётся в
#: `sys.modules`, и следующий тест получил бы чужой — поэтому их убирают до и после.
МОДУЛИ = ("confpartest", "atlas_probe_db", "atlas_probe_logs")

ПРОКСИ = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture
def проект(tmp_path, monkeypatch):
    """Корень проекта; текущий каталог — другой, чтобы ничего не держалось на нём."""
    корень = tmp_path / "проект"
    корень.mkdir()
    (корень / "atlas.yaml").write_text("artifacts_dir: atlas\n", encoding="utf-8")
    чужой = tmp_path / "где-то"
    чужой.mkdir()
    monkeypatch.chdir(чужой)
    for имя in ("ATLAS_SYSTEM", "ATLAS_ENV", "DATABASE_URL", "PARTEST_TLS_VERIFY", *ПРОКСИ):
        monkeypatch.delenv(имя, raising=False)
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    for имя in МОДУЛИ:
        sys.modules.pop(имя, None)
    yield корень
    for имя in МОДУЛИ:
        sys.modules.pop(имя, None)


@pytest.fixture
def запустить(capsys):
    def run(*аргументы):
        # Плагин partest в этом же процессе подгружает свой `confpartest` на каждом тесте
        # (после фикстур), поэтому чужие модули убираются прямо перед вызовом.
        for имя in МОДУЛИ:
            sys.modules.pop(имя, None)
        код = cli.main([str(a) for a in аргументы])
        out, err = capsys.readouterr()
        return код, out, err

    return run


def _confpartest(корень, swagger, extra=""):
    (корень / "confpartest.py").write_text(f"swagger_files = {swagger!r}\n{extra}", encoding="utf-8")


def _спецификация(корень, имя="spec/openapi.json", документ=None):
    путь = корень / имя
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(json.dumps(документ or СПЕЦИФИКАЦИЯ, ensure_ascii=False), encoding="utf-8")
    return путь


def _артефакт(корень, kind, system="default", env="default"):
    return корень / "atlas" / system / env / artifacts.filename(kind)


def _одна_строка(err):
    строки = [s for s in err.splitlines() if s.strip()]
    assert len(строки) == 1, err
    return строки[0]


# --- collect openapi: локальный файл ------------------------------------------


def test_openapi_from_a_local_spec_writes_the_contract(проект, запустить):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)

    код, out, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi"))
    assert artifacts.validate(doc) == []
    assert doc["kind"] == "contract.openapi"
    assert doc["collector"] == "partest-atlas/openapi"
    assert doc["stats"]["operations"] == 2
    assert doc["info"] == {"title": "Магазин", "version": "1.4.0"}
    # Путь — как объявлен, а не разрешённый: полный путь на машине сборки карте не нужен.
    assert doc["source"] == {"service": "магазин", "type": "local", "location": "spec/openapi.json"}
    assert "contract.openapi" in out and "операций 2" in out


def test_a_relative_local_path_is_taken_from_the_confpartest_directory(проект, запустить, tmp_path):
    """Текущий каталог — другой (фикстура), и файл с тем же именем там есть, но не тот."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)
    обманка = {**СПЕЦИФИКАЦИЯ, "info": {"title": "не тот", "version": "0"}}
    _спецификация(tmp_path / "где-то", документ=обманка)

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    assert artifacts.read(_артефакт(проект, "contract.openapi"))["info"]["title"] == "Магазин"


def test_a_yaml_spec_is_read_too(проект, запустить):
    yaml = pytest.importorskip("yaml")
    _confpartest(проект, {"магазин": ["file", "openapi.yaml"]})
    (проект / "openapi.yaml").write_text(yaml.safe_dump(СПЕЦИФИКАЦИЯ, allow_unicode=True), encoding="utf-8")

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    assert artifacts.read(_артефакт(проект, "contract.openapi"))["stats"]["paths"] == 2


def test_without_root_the_atlas_yaml_directory_is_the_project(проект, запустить, monkeypatch):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)
    (проект / "tests").mkdir()
    monkeypatch.chdir(проект / "tests")

    код, _, err = запустить("collect", "openapi")

    assert код == 0, err
    assert _артефакт(проект, "contract.openapi").is_file()


def test_the_scope_comes_from_flags_over_the_environment(проект, запустить, monkeypatch):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)
    monkeypatch.setenv("ATLAS_SYSTEM", "склад")
    monkeypatch.setenv("ATLAS_ENV", "тест")

    код, _, err = запустить("collect", "openapi", "--root", проект, "--env", "препрод")

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi", "склад", "препрод"))
    assert doc["scope"] == {"system": "склад", "env": "препрод"}


@pytest.mark.parametrize("значение", ["..", "../чужое", "a/b", "a\\b", "C:"])
def test_a_scope_that_would_leave_the_artifacts_directory_is_refused(проект, запустить, значение):
    """Пишем только в свой каталог артефактов: `..` или разделитель вывели бы запись наружу."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)

    код, out, err = запустить("collect", "openapi", "--root", проект, "--system", значение)

    assert код == 2
    assert "--system" in _одна_строка(err)
    assert not (проект / "atlas").exists()


def test_several_specs_need_a_service_and_say_which_exist(проект, запустить):
    """Выбрать первую молча значило бы снять не тот контракт и не сказать об этом."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"], "склад": ["local", "other.json"]})
    _спецификация(проект)

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 2
    assert "магазин" in строка and "склад" in строка and "--service" in строка
    assert not _артефакт(проект, "contract.openapi").exists()


def test_the_named_service_is_taken(проект, запустить):
    _confpartest(проект, {"склад": ["local", "нет.json"], "магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)

    код, _, err = запустить("collect", "openapi", "--root", проект, "--service", "магазин")

    assert код == 0, err
    assert artifacts.read(_артефакт(проект, "contract.openapi"))["source"]["service"] == "магазин"


def test_an_unknown_service_is_refused_by_name(проект, запустить):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})

    код, _, err = запустить("collect", "openapi", "--root", проект, "--service", "склад")

    assert код == 2
    assert "склад" in _одна_строка(err)


def test_no_confpartest_is_one_line_and_code_2(проект, запустить):
    код, out, err = запустить("collect", "openapi", "--root", проект)

    assert код == 2
    assert "confpartest" in _одна_строка(err)
    assert out == ""


def test_a_confpartest_without_swagger_files_says_what_is_expected(проект, запустить):
    (проект / "confpartest.py").write_text("x = 1\n", encoding="utf-8")

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 2
    assert "swagger_files" in _одна_строка(err)


def test_a_missing_spec_file_says_where_the_path_is_taken_from(проект, запустить):
    _confpartest(проект, {"магазин": ["local", "нет/openapi.json"]})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 2
    assert "нет/openapi.json" in строка and "confpartest.py" in строка


def test_a_document_that_is_not_openapi_is_refused(проект, запустить):
    """Типичный случай: адрес отдал JSON с ошибкой или страницу входа."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект, документ={"error": "unauthorized"})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 2
    assert "OpenAPI" in _одна_строка(err)
    assert not _артефакт(проект, "contract.openapi").exists()


def test_json_output_is_for_a_machine(проект, запустить):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)

    код, out, err = запустить("--json", "collect", "openapi", "--root", проект)

    assert код == 0, err
    отчёт = json.loads(out)
    assert отчёт["kind"] == "contract.openapi"
    assert отчёт["stats"]["operations"] == 2
    assert отчёт["info"]["version"] == "1.4.0"
    assert отчёт["path"].endswith("openapi.json")


# --- collect openapi: адрес --------------------------------------------------------


@pytest.fixture
def сервер():
    import http.server
    import threading

    тело = json.dumps(СПЕЦИФИКАЦИЯ, ensure_ascii=False).encode("utf-8")
    увидено = []

    class Ответ(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            увидено.append({"path": self.path, "auth": self.headers.get("Authorization")})
            if self.path.startswith("/v3/api-docs"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(тело)))
                self.end_headers()
                self.wfile.write(тело)
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ответ)
    поток = threading.Thread(target=srv.serve_forever, daemon=True)
    поток.start()
    yield f"127.0.0.1:{srv.server_address[1]}", увидено
    srv.shutdown()
    srv.server_close()


def test_openapi_from_a_url_keeps_no_credentials_and_no_query(проект, запустить, сервер):
    адрес, увидено = сервер
    _confpartest(проект, {"магазин": ["url", f"http://robot:s3cr3t@{адрес}/v3/api-docs?access_token=abc#x"]})

    код, out, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi"))
    assert doc["source"] == {"service": "магазин", "type": "url", "location": f"http://{адрес}/v3/api-docs"}
    assert doc["info"]["title"] == "Магазин"
    сырой = _артефакт(проект, "contract.openapi").read_text(encoding="utf-8")
    for секрет in ("s3cr3t", "robot", "access_token", "abc"):
        assert секрет not in сырой and секрет not in out and секрет not in err
    # Учётные данные ушли заголовком, а не в адресе.
    assert увидено[0]["auth"] == "Basic " + base64.b64encode(b"robot:s3cr3t").decode()


def test_a_url_answering_404_is_one_line_without_secrets(проект, запустить, сервер):
    адрес, _ = сервер
    _confpartest(проект, {"магазин": ["url", f"http://robot:s3cr3t@{адрес}/нет?token=abc"]})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 2
    assert "404" in строка
    assert "s3cr3t" not in строка and "token=abc" not in строка
    assert not _артефакт(проект, "contract.openapi").exists()


def test_an_unreachable_url_is_one_line_without_secrets(проект, запустить):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        порт = s.getsockname()[1]
    _confpartest(проект, {"магазин": ["url", f"http://robot:s3cr3t@127.0.0.1:{порт}/v3?token=abc"]})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 2
    assert f"127.0.0.1:{порт}" in строка
    assert "s3cr3t" not in строка and "token=abc" not in строка


# --- collect db-schema: база — функция проекта, отвечающая как PostgreSQL -------------


def _база(корень, *, read_only="on", schemas=("shop",), columns=True):
    """Модуль проекта с функцией соединения. Пишет каждый запрос в `ЗАПРОСЫ`."""
    (корень / "atlas_probe_db.py").write_text(textwrap.dedent(f'''
        import contextlib

        ЗАПРОСЫ = []
        РЕЖИМ = {read_only!r}
        СХЕМЫ = {list(schemas)!r}
        КОЛОНКИ = {columns!r}


        class Курсор:
            def __init__(self):
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                ЗАПРОСЫ.append((" ".join(sql.split()), params))
                if "transaction_read_only" in sql:
                    self.rows = [{{"transaction_read_only": РЕЖИМ}}]
                elif "PRIMARY KEY" in sql:
                    self.rows = [{{"table_schema": "shop", "table_name": "orders", "column_name": "id"}}]
                elif "FOREIGN KEY" in sql:
                    self.rows = []
                elif "information_schema.tables" in sql:
                    self.rows = [{{"schema_name": s}} for s in СХЕМЫ]
                elif "information_schema.columns" in sql:
                    self.rows = [
                        {{"table_schema": "shop", "table_name": "orders", "column_name": "id",
                          "ordinal_position": 1, "data_type": "bigint", "is_nullable": "NO",
                          "column_default": None, "character_maximum_length": None}},
                    ] if КОЛОНКИ else []
                elif "pg_stat_user_tables" in sql:
                    self.rows = [{{"table_name": "orders", "rows": 7}}]
                else:
                    self.rows = []

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.rows[0] if self.rows else None


        class Соединение:
            def cursor(self):
                return Курсор()


        @contextlib.contextmanager
        def соединить():
            yield Соединение()
    '''), encoding="utf-8")


def _atlas_yaml(корень, text):
    (корень / "atlas.yaml").write_text("artifacts_dir: atlas\n" + textwrap.dedent(text), encoding="utf-8")


def _запросы():
    return sys.modules["atlas_probe_db"].ЗАПРОСЫ


def test_db_schema_auto_takes_every_non_system_schema_and_the_configured_key(проект, запустить):
    _база(проект, schemas=("shop", "billing"))
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: auto
          key: orders-db
    """)

    код, out, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "db.schema"))
    assert artifacts.validate(doc) == []
    база = doc["databases"][0]
    assert база["key"] == "orders-db"
    assert база["schemas"] == ["shop", "billing"]
    assert [t["name"] for t in база["tables"]] == ["orders"]
    assert база["tables"][0]["rows"] == 7
    колонки = [p for sql, p in _запросы() if "information_schema.columns" in sql]
    assert колонки == [{"schemas": ["shop", "billing"]}]
    assert "таблиц 1" in out


def test_without_a_key_the_database_is_still_primary(проект, запустить):
    _база(проект)
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: [shop]
    """)

    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 0, err
    assert artifacts.read(_артефакт(проект, "db.schema"))["databases"][0]["key"] == "primary"


def test_every_query_runs_inside_a_confirmed_read_only_transaction(проект, запустить):
    """Только чтение держит база: каждый `SELECT` — между `BEGIN … READ ONLY` и `ROLLBACK`,
    после `SET TRANSACTION READ ONLY` и ответа базы `on`."""
    _база(проект)
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: auto
    """)

    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 0, err
    запросы = [sql for sql, _ in _запросы()]
    assert запросы[:3] == [
        postgres.BEGIN_READ_ONLY_SQL,
        postgres.SET_READ_ONLY_SQL,
        postgres.CHECK_READ_ONLY_SQL,
    ]
    в_блоке = False
    подтверждено = False
    выборок = 0
    for sql in запросы:
        if sql == postgres.BEGIN_READ_ONLY_SQL:
            в_блоке, подтверждено = True, False
        elif sql == postgres.CHECK_READ_ONLY_SQL:
            подтверждено = в_блоке
        elif sql == postgres.END_SQL:
            в_блоке = False
        elif sql.startswith("SELECT"):
            assert в_блоке and подтверждено, f"выборка вне транзакции только на чтение: {sql[:60]}"
            выборок += 1
    assert выборок == 5  # схемы, колонки, первичные ключи, внешние ключи, строки
    assert not в_блоке, "блок не закрыт откатом"


def test_a_database_that_does_not_confirm_read_only_is_not_queried(проект, запустить):
    _база(проект, read_only="off")
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: [shop]
    """)

    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 2
    assert "только чтения" in _одна_строка(err)
    assert not any(sql.startswith("SELECT") for sql, _ in _запросы())
    assert _запросы()[-1][0] == postgres.END_SQL
    assert not _артефакт(проект, "db.schema").exists()


def test_selected_schemas_without_tables_are_a_loud_error(проект, запустить):
    """Таблицы переехали в другую схему — раньше это был пустой артефакт и «таблиц нет»."""
    _база(проект, columns=False)
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: public
    """)

    код, _, err = запустить("collect", "db-schema", "--root", проект)

    строка = _одна_строка(err)
    assert код == 2
    assert "нет ни одной таблицы" in строка and "public" in строка and "auto" in строка
    assert not _артефакт(проект, "db.schema").exists()


def test_auto_with_no_non_system_schema_is_a_loud_error(проект, запустить):
    _база(проект, schemas=())
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: auto
    """)

    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 2
    assert "auto" in _одна_строка(err)


def test_db_schema_without_a_connection_names_the_variable(проект, запустить):
    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 2
    assert "DATABASE_URL" in _одна_строка(err)


def test_a_bad_schemas_value_is_a_configuration_error(проект, запустить):
    _база(проект)
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: {a: 1}
    """)

    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 2
    assert "database.schemas" in _одна_строка(err)


def test_a_query_that_is_not_a_single_select_is_refused_before_it_is_sent(monkeypatch):
    """Вторая линия после транзакции только на чтение: проверка partest до отправки."""
    отправлено = []

    class Курсор:
        def execute(self, sql, params=None):
            отправлено.append(sql)

        def fetchall(self):
            return []

    with pytest.raises(ValueError):
        db_schema._select(Курсор(), "DELETE FROM orders", {})
    assert отправлено == []


def test_every_sql_constant_is_a_single_select():
    """Запросы сборщиков — константы; проверка partest держит их одиночными `SELECT`."""
    from partest.sideeffects import assert_read_only_sql

    for sql in (
        db_schema.COLUMNS_SQL,
        db_schema.PRIMARY_KEYS_SQL,
        db_schema.FOREIGN_KEYS_SQL,
        db_schema.APPROX_ROWS_SQL,
        db_effects.STATS_SQL,
        db_effects.ACTIVE_SESSIONS_SQL,
        postgres.SCHEMAS_SQL,
    ):
        assert_read_only_sql(sql)


# --- collect container-logs ----------------------------------------------------------


def _журналы(корень, тело='return {"api": \'ERROR: referenced from table "orders"\', "worker": "тихо"}'):
    (корень / "atlas_probe_logs.py").write_text(f"def журналы():\n    {тело}\n", encoding="utf-8")


ПРАВИЛА = """
    container_logs:
      source: "atlas_probe_logs:журналы"
      rules:
        - kind: table
          pattern: 'referenced from table "([a-z_]+)"'
          known_only: true
"""


def test_container_logs_without_a_source_is_refused_with_code_2(проект, запустить):
    код, out, err = запустить("collect", "container-logs", "--root", проект)

    assert код == 2
    assert "container_logs.source" in _одна_строка(err)
    assert out == ""
    assert not _артефакт(проект, "containers.logs").exists()


def test_container_logs_without_rules_is_refused(проект, запустить):
    _журналы(проект)
    _atlas_yaml(проект, """
        container_logs:
          source: "atlas_probe_logs:журналы"
    """)

    код, _, err = запустить("collect", "container-logs", "--root", проект)

    assert код == 2
    assert "rules" in _одна_строка(err)


def test_container_logs_from_the_project_source_use_the_schema_dictionary(проект, запустить):
    _журналы(проект)
    _atlas_yaml(проект, ПРАВИЛА)
    artifacts.write(
        _артефакт(проект, "db.schema"),
        "db.schema",
        {"databases": [{"key": "primary", "tables": [{"name": "orders"}]}]},
    )

    код, out, err = запустить("collect", "container-logs", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "containers.logs"))
    assert artifacts.validate(doc) == []
    assert doc["containers"] == [{"name": "api", "found": {"table": {"orders": 1}}}]
    assert doc["collector"] == "partest-atlas/container_logs"


def test_a_source_that_returns_no_mapping_is_refused(проект, запустить):
    _журналы(проект, тело='return "один текст на всех"')
    _atlas_yaml(проект, ПРАВИЛА)

    код, _, err = запустить("collect", "container-logs", "--root", проект)

    assert код == 2
    assert "str" in _одна_строка(err)


# --- язык вывода -------------------------------------------------------------------------


def test_lang_en_switches_collect_errors(проект, запустить):
    код, _, err = запустить("--lang", "en", "collect", "container-logs", "--root", проект)

    assert код == 2
    assert "is not declared" in _одна_строка(err)


def test_the_locale_picks_english_without_the_key(проект, запустить, monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 2
    assert "there is no confpartest.py" in _одна_строка(err)


def test_the_key_wins_over_the_locale(проект, запустить, monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    код, _, err = запустить("--lang", "ru", "collect", "openapi", "--root", проект)

    assert код == 2
    assert "нет confpartest.py" in _одна_строка(err)


def test_check_speaks_english_including_the_problems(tmp_path, запустить, monkeypatch):
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    путь = tmp_path / "db_schema.json"
    путь.write_text('{"version": 2}', encoding="utf-8")

    код, out, _ = запустить("--lang", "en", "check", путь)

    assert код == 2
    assert "PROBLEMS" in out and "no 'kind' key" in out and "files read: 1" in out
    assert "нет ключа" not in out


def test_help_is_in_the_chosen_language(запустить, monkeypatch, capsys):
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    with pytest.raises(SystemExit):
        cli.main(["--lang", "en", "--help"])
    out = capsys.readouterr().out
    assert "collect an observation that needs no test run" in out
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "снять наблюдение без прогона тестов" in capsys.readouterr().out


def test_an_unknown_language_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as выход:
        cli.main(["--lang", "fr", "check", "x"])
    assert выход.value.code == 2


def test_the_language_is_restored_after_the_call(tmp_path, запустить):
    """Код, который зовёт функции пакета сам, получает прежний язык."""
    было = lang.current()
    запустить("--lang", "en", "check", tmp_path / "нет.json")
    assert lang.current() == было


@pytest.mark.parametrize(
    ("аргументы", "язык"),
    [
        (["--lang", "en", "check", "x"], "en"),
        (["--lang=en", "show", "x"], "en"),
        (["--json", "--lang", "ru", "check"], "ru"),
        # После подкоманды и после `--` — чужие аргументы: `wrap -- prog --lang en`.
        (["wrap", "--", "prog", "--lang", "en"], None),
        (["check", "--lang", "en"], None),
        (["--lang", "fr", "check"], None),
    ],
)
def test_the_language_is_read_only_before_the_subcommand(аргументы, язык):
    assert cli._pre_lang(аргументы) == язык


def test_a_foreign_confpartest_already_imported_is_refused(проект, capsys):
    """partest берёт уже импортированный `confpartest`; чужой дал бы чужой контракт молча."""
    import types

    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)
    чужой = types.ModuleType("confpartest")
    чужой.__file__ = str(проект.parent / "чужой" / "confpartest.py")
    чужой.swagger_files = {"чужой": ["local", "x.json"]}
    sys.modules["confpartest"] = чужой

    код = cli.main(["collect", "openapi", "--root", str(проект)])

    err = capsys.readouterr().err
    assert код == 2
    assert "не тот confpartest" in _одна_строка(err)
    assert not _артефакт(проект, "contract.openapi").exists()


def test_a_log_the_source_could_not_read_is_unread_not_empty(проект, запустить):
    _журналы(проект, тело='return {"api": \'referenced from table "orders"\\n\', "cron": None}')
    _atlas_yaml(проект, ПРАВИЛА)

    код, _, err = запустить("collect", "container-logs", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "containers.logs"))
    assert doc["logs"] == [
        {"name": "api", "read": True, "lines": 1},
        {"name": "cron", "read": False, "lines": 0},
    ]
    assert doc["stats"]["unread"] == 1
