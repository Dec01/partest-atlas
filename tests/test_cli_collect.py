"""`partest-atlas collect`: наблюдения без прогона тестов — без кода в проекте.

Каждая подкоманда проверяется на временном проекте: `atlas.yaml`, `confpartest.py`,
модули проекта рядом. Базы нет — её место занимает функция соединения проекта
(`database.connect`), которая отвечает как PostgreSQL и записывает, что у неё спросили.
Сеть — только локальный `http.server` на 127.0.0.1.
"""

from __future__ import annotations

import json
import socket
import sys
import textwrap
import zipfile

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
    assert doc["source"] == {"service": "магазин", "type": "local", "location": "spec/openapi.json",
                             "resolvedFrom": "confpartest"}
    assert "contract.openapi" in out and "операций 2" in out


def test_a_relative_local_path_falls_back_to_the_confpartest_directory(проект, запустить):
    """Текущий каталог — другой (фикстура), и файла от него нет: берётся от `confpartest.py`."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    assert artifacts.read(_артефакт(проект, "contract.openapi"))["info"]["title"] == "Магазин"


def test_a_relative_local_path_existing_from_the_cwd_is_taken_from_there(проект, запустить, tmp_path):
    """Правило partest (как у его покрытия): путь, существующий от текущего каталога,
    сохраняет смысл. Иначе контракт карты и покрытие partest читали бы разные файлы."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)
    от_cwd = {**СПЕЦИФИКАЦИЯ, "info": {"title": "от текущего", "version": "0"}}
    _спецификация(tmp_path / "где-то", документ=от_cwd)

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi"))
    assert doc["info"]["title"] == "от текущего"
    assert doc["source"]["location"] == "spec/openapi.json"  # как объявлен


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
    monkeypatch.setenv("ATLAS_SYSTEM", "warehouse")
    monkeypatch.setenv("ATLAS_ENV", "test")

    код, _, err = запустить("collect", "openapi", "--root", проект, "--env", "preprod")

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi", "warehouse", "preprod"))
    assert doc["scope"] == {"system": "warehouse", "env": "preprod"}


def test_a_bad_scope_in_the_environment_is_named_without_its_value(проект, запустить, monkeypatch):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)
    monkeypatch.setenv("ATLAS_SYSTEM", "Склад секретный")

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 1 and "ATLAS_SYSTEM" in строка and "секретный" not in строка


@pytest.mark.parametrize("значение", ["..", "../чужое", "a/b", "a\\b", "C:", "Склад", "a b", "a..b"])
def test_a_scope_that_would_leave_the_artifacts_directory_is_refused(проект, запустить, значение):
    """Пишем только в свой каталог артефактов: `..` или разделитель вывели бы запись наружу.
    Правило имени — у владельца формата (`artifacts.Scope`), то же, что у читающей
    программы: `Склад` она не показала бы (найдено при разборе)."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)

    код, out, err = запустить("collect", "openapi", "--root", проект, "--system", значение)

    assert код == 1
    assert "--system" in _одна_строка(err)
    assert not (проект / "atlas").exists()


ВТОРАЯ = {
    "openapi": "3.0.3",
    "info": {"title": "Склад", "version": "2.0"},
    "paths": {"/stock": {"get": {"responses": {"200": {"description": "ok"}}}}},
}


def test_without_service_every_service_is_collected_into_one_artifact(проект, запустить):
    """Одна область — один `contract.openapi`, а служб несколько: операции всех служб с
    ключом `service`, по службе — запись в `services` (найдено при разборе)."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"], "склад": ["local", "stock.json"]})
    _спецификация(проект)
    _спецификация(проект, "stock.json", ВТОРАЯ)

    код, out, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi"))
    assert artifacts.validate(doc) == []
    assert [s["service"] for s in doc["services"]] == ["магазин", "склад"]
    assert sorted((op["service"], op["path"]) for op in doc["operations"]) == [
        ("магазин", "/items"), ("магазин", "/items/{id}"), ("склад", "/stock"),
    ]
    assert doc["stats"]["operations"] == 3
    assert doc["services"][1]["info"] == {"title": "Склад", "version": "2.0"}
    assert doc["services"][1]["stats"]["operations"] == 1
    # Верхние `info`/`source` — только у одной службы: у нескольких их брать неоткуда.
    assert "info" not in doc and "source" not in doc
    assert "служб 2" in out


def test_with_service_only_that_service_is_replaced(проект, запустить):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"], "склад": ["local", "stock.json"]})
    _спецификация(проект)
    _спецификация(проект, "stock.json", ВТОРАЯ)
    assert запустить("collect", "openapi", "--root", проект)[0] == 0
    обновлённая = {**ВТОРАЯ, "info": {"title": "Склад", "version": "3.0"},
                   "paths": {"/stock": ВТОРАЯ["paths"]["/stock"], "/stock/{id}": ВТОРАЯ["paths"]["/stock"]}}
    _спецификация(проект, "stock.json", обновлённая)

    код, _, err = запустить("collect", "openapi", "--root", проект, "--service", "склад")

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi"))
    assert [s["service"] for s in doc["services"]] == ["магазин", "склад"]
    assert doc["services"][1]["info"]["version"] == "3.0"
    assert sorted((op["service"], op["path"]) for op in doc["operations"]) == [
        ("магазин", "/items"), ("магазин", "/items/{id}"), ("склад", "/stock"), ("склад", "/stock/{id}"),
    ]


def test_a_single_service_keeps_info_and_source_on_top(проект, запустить):
    _confpartest(проект, {"склад": ["local", "нет.json"], "магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект)

    код, _, err = запустить("collect", "openapi", "--root", проект, "--service", "магазин")

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi"))
    assert doc["source"]["service"] == "магазин"
    assert doc["info"]["title"] == "Магазин"
    assert [s["service"] for s in doc["services"]] == ["магазин"]
    assert {op["service"] for op in doc["operations"]} == {"магазин"}


def test_one_failing_service_writes_nothing(проект, запустить):
    """Частичный контракт выглядел бы как полный."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"], "склад": ["local", "нет.json"]})
    _спецификация(проект)

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 1 and "склад" in _одна_строка(err)
    assert not _артефакт(проект, "contract.openapi").exists()


def test_an_unknown_service_is_refused_by_name(проект, запустить):
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})

    код, _, err = запустить("collect", "openapi", "--root", проект, "--service", "склад")

    assert код == 1
    assert "склад" in _одна_строка(err)


def test_no_confpartest_is_one_line_and_code_1(проект, запустить):
    код, out, err = запустить("collect", "openapi", "--root", проект)

    assert код == 1
    assert "confpartest" in _одна_строка(err)
    assert out == ""


def test_a_confpartest_without_swagger_files_says_what_is_expected(проект, запустить):
    (проект / "confpartest.py").write_text("x = 1\n", encoding="utf-8")

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 1
    assert "swagger_files" in _одна_строка(err)


def test_a_missing_spec_file_says_where_the_path_is_taken_from(проект, запустить):
    _confpartest(проект, {"магазин": ["local", "нет/openapi.json"]})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 1
    assert "нет/openapi.json" in строка and "не получена" in строка


def test_a_document_that_is_not_openapi_is_refused(проект, запустить):
    """Типичный случай: адрес отдал JSON с ошибкой или страницу входа."""
    _confpartest(проект, {"магазин": ["local", "spec/openapi.json"]})
    _спецификация(проект, документ={"error": "unauthorized"})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 1
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


def test_http_url_userinfo_is_refused_before_request_without_secrets(проект, запустить, сервер):
    адрес, увидено = сервер
    _confpartest(проект, {"магазин": ["url", f"http://robot:s3cr3t@{адрес}/v3/api-docs?access_token=abc#x"]})

    код, out, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 1
    assert "Basic Auth requires an https:// URL" in строка
    for секрет in ("s3cr3t", "robot", "access_token", "abc"):
        assert секрет not in строка and секрет not in out
    assert увидено == []
    assert not _артефакт(проект, "contract.openapi").exists()


def test_openapi_from_a_public_http_url_keeps_no_query(проект, запустить, сервер):
    адрес, увидено = сервер
    _confpartest(проект, {"магазин": ["url", f"http://{адрес}/v3/api-docs?access_token=abc#x"]})

    код, out, err = запустить("collect", "openapi", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "contract.openapi"))
    assert doc["source"] == {"service": "магазин", "type": "url", "location": f"http://{адрес}/v3/api-docs"}
    assert doc["info"]["title"] == "Магазин"
    сырой = _артефакт(проект, "contract.openapi").read_text(encoding="utf-8")
    for секрет in ("access_token", "abc"):
        assert секрет not in сырой and секрет not in out and секрет not in err
    assert увидено == [{"path": "/v3/api-docs?access_token=abc", "auth": None}]


def test_a_url_answering_404_is_one_line_without_secrets(проект, запустить, сервер):
    адрес, увидено = сервер
    _confpartest(проект, {"магазин": ["url", f"http://{адрес}/нет?token=abc"]})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 1
    assert "404" in строка
    assert "token" not in строка and "abc" not in строка
    assert len(увидено) == 1
    assert увидено[0]["path"].endswith("?token=abc")
    assert увидено[0]["auth"] is None
    assert not _артефакт(проект, "contract.openapi").exists()


def test_an_unreachable_url_is_one_line_without_secrets(проект, запустить):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        порт = s.getsockname()[1]
    _confpartest(проект, {"магазин": ["url", f"http://127.0.0.1:{порт}/v3?token=abc"]})

    код, _, err = запустить("collect", "openapi", "--root", проект)

    строка = _одна_строка(err)
    assert код == 1
    assert f"127.0.0.1:{порт}" in строка
    assert "token" not in строка and "abc" not in строка


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

    assert код == 1
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
    assert код == 1
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

    assert код == 1
    assert "auto" in _одна_строка(err)


def test_db_schema_without_a_connection_names_the_variable(проект, запустить):
    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 1
    assert "DATABASE_URL" in _одна_строка(err)


def test_a_bad_schemas_value_is_a_configuration_error(проект, запустить):
    _база(проект)
    _atlas_yaml(проект, """
        database:
          connect: "atlas_probe_db:соединить"
          schemas: {a: 1}
    """)

    код, _, err = запустить("collect", "db-schema", "--root", проект)

    assert код == 1
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


def test_container_logs_without_a_source_is_refused_with_code_1(проект, запустить):
    код, out, err = запустить("collect", "container-logs", "--root", проект)

    assert код == 1
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

    assert код == 1
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

    assert код == 1
    assert "str" in _одна_строка(err)


# --- collect specs -----------------------------------------------------------------------


def _требования_docx(корень, *разделы, имя="docs/requirements.docx"):
    путь = корень / имя
    путь.parent.mkdir(parents=True, exist_ok=True)
    куски = []
    for заголовок, стиль, текст in разделы:
        куски.append(f'<w:p><w:pPr><w:pStyle w:val="{стиль}"/></w:pPr>'
                     f'<w:r><w:t>{заголовок}</w:t></w:r></w:p>')
        куски.append(f'<w:p><w:r><w:t>{текст}</w:t></w:r></w:p>')
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{"".join(куски)}</w:body></w:document>')
    with zipfile.ZipFile(путь, "w") as архив:
        архив.writestr("word/document.xml", xml)
    return путь


def _настройки_specs(корень, файл="docs/requirements.docx", synonyms=""):
    _atlas_yaml(корень, f"""
        project:
          api_prefix: /api/v1
        specs:
          files: [{файл}]
          heading_styles: {{Heading1: 1, Heading2: 2}}
          prefix: REQ
          min_chars: 0
        {synonyms}
    """)


def test_specs_from_a_real_docx_write_api_and_table_anchors(проект, запустить):
    _требования_docx(
        проект,
        ("1 Общие", "Heading1", "Полный путь /api/v1/orders/1 меняет заказы"),
        ("1.1 Деталь", "Heading2", "Относительный путь /orders/2"),
    )
    _настройки_specs(проект, synonyms="  synonyms: {заказы: orders}")
    область = artifacts.Scope(system="library", env="qa")
    artifacts.write(
        _артефакт(проект, "db.schema", область.system, область.env),
        "db.schema",
        {"databases": [{"key": "primary", "tables": [{"name": "orders"}]}]},
        scope=область,
    )

    код, out, err = запустить(
        "collect", "specs", "--root", проект, "--system", область.system, "--env", область.env,
    )

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "specs.requirements", область.system, область.env))
    assert artifacts.validate(doc, kind="specs.requirements", scope=область) == []
    assert doc["collector"] == "partest-atlas/specs"
    assert doc["stats"] == {"requirements": 2, "sources": 1, "withAnchors": 2}
    общее, деталь = doc["requirements"]
    assert общее["source"] == "docs/requirements.docx"
    assert общее["anchors"] == {"api": ["/api/v1/orders/{id}"], "table": ["orders"]}
    # Корень orders известен по полному пути из другого раздела того же документа;
    # само имя orders совпадает с таблицей из снятой схемы.
    assert деталь["anchors"] == {"api": ["/api/v1/orders/{id}"], "table": ["orders"]}
    assert "требований 2" in out and "источников 1" in out and "с якорями 2" in out


def test_specs_api_root_from_one_docx_anchors_a_relative_path_in_another(проект, запустить):
    _требования_docx(
        проект, ("1 Источник", "Heading1", "Полный путь /api/v1/orders/1"), имя="docs/a.docx",
    )
    _требования_docx(
        проект, ("2 Потребитель", "Heading1", "Относительный путь /orders/2"), имя="docs/b.docx",
    )
    _atlas_yaml(проект, """
        project:
          api_prefix: /api/v1
        specs:
          files: [docs/a.docx, docs/b.docx]
          heading_styles: {Heading1: 1}
    """)

    код, _, err = запустить("collect", "specs", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "specs.requirements"))
    assert [row["anchors"] for row in doc["requirements"]] == [
        {"api": ["/api/v1/orders/{id}"]},
        {"api": ["/api/v1/orders/{id}"]},
    ]


def test_bare_collect_includes_configured_specs_without_a_schema(проект, запустить):
    _требования_docx(проект, ("1 Запрос", "Heading1", "Вызов /api/v1/items/1"))
    _настройки_specs(проект)

    код, out, err = запустить("--json", "collect", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "specs.requirements"))
    assert doc["requirements"][0]["anchors"] == {"api": ["/api/v1/items/{id}"]}
    assert "table" not in doc["requirements"][0]["anchors"]
    результат = json.loads(out)["results"]
    assert len(результат) == 1
    assert результат[0]["collector"] == "specs"
    assert результат[0]["kind"] == "specs.requirements"
    assert результат[0]["stats"] == {"requirements": 1, "sources": 1, "withAnchors": 1}
    assert результат[0]["path"].endswith("specs.json")


def test_specs_file_object_overrides_source_and_prefix(проект, запустить):
    _требования_docx(
        проект, ("17 Структура", "Heading1", "Описание структуры"), имя="docs/custom.docx",
    )
    _atlas_yaml(проект, """
        specs:
          files:
            - file: docs/custom.docx
              label: Структура данных
              prefix: DATA
          heading_styles: {Heading1: 1}
          prefix: REQ
    """)

    код, _, err = запустить("collect", "specs", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "specs.requirements"))
    assert artifacts.validate(doc) == []
    assert [(row["id"], row["source"]) for row in doc["requirements"]] == [
        ("DATA-17", "Структура данных"),
    ]


def test_specs_files_mix_strings_and_objects_with_defaults(проект, запустить):
    for имя, номер in (("a.docx", 1), ("b.docx", 2), ("c.docx", 3)):
        _требования_docx(
            проект, (f"{номер} Раздел", "Heading1", "Текст требования"), имя=f"docs/{имя}",
        )
    _atlas_yaml(проект, """
        specs:
          files:
            - docs/a.docx
            - file: docs/b.docx
            - file: docs/c.docx
              label: Третий документ
          heading_styles: {Heading1: 1}
          prefix: PLAN
    """)

    код, _, err = запустить("collect", "specs", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "specs.requirements"))
    assert [(row["id"], row["source"]) for row in doc["requirements"]] == [
        ("PLAN-1", "docs/a.docx"),
        ("PLAN-2", "docs/b.docx"),
        ("PLAN-3", "Третий документ"),
    ]


def test_specs_accepts_an_absolute_local_docx(проект, запустить):
    путь = _требования_docx(проект, ("4 Локальный", "Heading1", "Текст требования"))
    _atlas_yaml(проект, f"""
        specs:
          files: [{json.dumps(str(путь))}]
          heading_styles: {{Heading1: 1}}
    """)

    код, _, err = запустить("collect", "specs", "--root", проект)

    assert код == 0, err
    doc = artifacts.read(_артефакт(проект, "specs.requirements"))
    assert [row["id"] for row in doc["requirements"]] == ["REQ-4"]


def test_specs_duplicate_local_ids_get_deterministic_across_file_suffixes(проект, запустить):
    for имя, текст in (("a.docx", "Первый документ"), ("b.docx", "Второй документ")):
        _требования_docx(проект, ("1 Требование", "Heading1", текст), имя=f"docs/{имя}")
    _atlas_yaml(проект, """
        specs:
          files: [docs/a.docx, docs/b.docx]
          heading_styles: {Heading1: 1}
          prefix: REQ
    """)

    код, _, err = запустить("collect", "specs", "--root", проект)

    assert код == 0, err
    путь = _артефакт(проект, "specs.requirements")
    doc = artifacts.read(путь)
    assert [(row["id"], row["text"]) for row in doc["requirements"]] == [
        ("REQ-1", "Первый документ"), ("REQ-1~2", "Второй документ"),
    ]

    assert запустить("collect", "specs", "--root", проект)[0] == 0
    assert [row["id"] for row in artifacts.read(путь)["requirements"]] == ["REQ-1", "REQ-1~2"]


def test_a_missing_specs_file_does_not_replace_the_previous_artifact(проект, запустить):
    _требования_docx(проект, ("1 Старое", "Heading1", "Текст требования"))
    _настройки_specs(проект)
    assert запустить("collect", "specs", "--root", проект)[0] == 0
    путь = _артефакт(проект, "specs.requirements")
    было = путь.read_bytes()
    _настройки_specs(проект, файл="docs/missing.docx")

    код, out, err = запустить("collect", "specs", "--root", проект)

    assert код == 1
    assert out == ""
    строка = _одна_строка(err)
    assert "specs.files" in строка and "docs/missing.docx" in строка
    assert путь.read_bytes() == было


def test_a_broken_second_docx_preserves_the_previous_specs_artifact_bytes(проект, запустить):
    for имя, номер in (("a.docx", 1), ("b.docx", 2)):
        _требования_docx(
            проект, (f"{номер} Требование", "Heading1", "Текст требования"), имя=f"docs/{имя}",
        )
    _atlas_yaml(проект, """
        specs:
          files: [docs/a.docx, docs/b.docx]
          heading_styles: {Heading1: 1}
    """)
    assert запустить("collect", "specs", "--root", проект)[0] == 0
    path = _артефакт(проект, "specs.requirements")
    before = path.read_bytes()
    (проект / "docs/b.docx").write_bytes(b"not a zip archive")

    код, out, err = запустить("collect", "specs", "--root", проект)

    assert код == 1
    assert out == ""
    assert "docs/b.docx" in _одна_строка(err)
    assert path.read_bytes() == before


# --- язык вывода -------------------------------------------------------------------------


def test_lang_en_switches_collect_errors(проект, запустить):
    код, _, err = запустить("--lang", "en", "collect", "container-logs", "--root", проект)

    assert код == 1
    assert "is not declared" in _одна_строка(err)


def test_the_locale_picks_english_without_the_key(проект, запустить, monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    код, _, err = запустить("collect", "openapi", "--root", проект)

    assert код == 1
    assert "there is no confpartest.py" in _одна_строка(err)


def test_the_key_wins_over_the_locale(проект, запустить, monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    код, _, err = запустить("--lang", "ru", "collect", "openapi", "--root", проект)

    assert код == 1
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
    assert код == 1
    assert "другой confpartest" in _одна_строка(err)
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
