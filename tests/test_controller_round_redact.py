"""Узкий раунд security после 7f55a85: исключение для идентификаторов не обходит маскировку."""

from __future__ import annotations

import json

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import container_logs, db_schema, openapi, ui_flow
from partest_atlas.collect.container_logs import Rule


# --- 1. исключение для идентификаторов — только «чистая» строка ------------------------


@pytest.mark.parametrize("путь", ["/v1/users/{id}/password:reset", "/api/orders/{id}", "public.users"])
def test_1_a_clean_identifier_string_is_kept(путь):
    assert artifacts.redact_payload({"path": путь})["path"] == путь


@pytest.mark.parametrize("путь", ["/app;jsessionid=ABC123SECRET/orders/{id}", "/x;password=hunter2",
                                  "user@host", "a b token=abc123secret"])
def test_1_an_identifier_with_unsafe_characters_is_masked_as_text(путь):
    from partest.redact import redact_text

    assert artifacts.redact_payload({"path": путь})["path"] == redact_text(путь)


def test_1_a1_ui_flow_matrix_parameters_do_not_reach_the_artifact(tmp_path):
    """A1: `path = urlsplit(url).path` несёт matrix-параметры `;jsessionid=…`."""
    links = ui_flow.attribute(
        [{"ts": 0, "screen": "#/orders", "control": "save"}],
        [{"ts": 10, "url": "https://h/app;jsessionid=ABC123SECRET/orders/7;password=hunter2", "method": "GET"}],
    )
    путь = tmp_path / "ui_flow.json"
    artifacts.write(путь, ui_flow.KIND, ui_flow.build(links))
    текст = путь.read_text(encoding="utf-8")
    assert "hunter2" not in текст


def test_1_a2_container_log_findings_are_masked(tmp_path):
    """A2: `found.table`/`found.topic` — словарь из захваченного текста журналов."""
    правило = Rule(kind="table", pattern=r'relation "([^"]+)"')
    found = container_logs.scan_text('ERROR: relation "token=abc123secret" does not exist', [правило])
    путь = tmp_path / "container_logs.json"
    artifacts.write(путь, container_logs.KIND, container_logs.build({"api": found}))
    assert "abc123secret" not in путь.read_text(encoding="utf-8")


def test_1_lists_under_identifier_keys_still_count_against_the_budget(monkeypatch):
    monkeypatch.setattr(artifacts, "REDACT_MAX_NODES", 50)
    with pytest.raises(artifacts.PayloadTooLarge):
        artifacts.redact_payload({"table": list(range(200))})


# --- 2. разбор линейный, а не квадратичный ---------------------------------------------------


class _СчётныйСписок(list):
    """Список, который считает элементы, отданные при обходе."""

    отдано = 0

    def __iter__(self):
        for item in super().__iter__():
            type(self).отдано += 1
            yield item


class _СчётныйСловарь(dict):
    """Словарь, который считает ключи, отданные при обходе."""

    отдано = 0

    def __iter__(self):
        for key in super().__iter__():
            type(self).отдано += 1
            yield key


def test_2_aliased_lists_are_cut_before_they_are_walked():
    """Один и тот же огромный список под каждой операцией и в каждом пути: срез — до обхода.

    Проверяется работа, а не время: сколько элементов разбор вытащил из размноженных
    списков и словарей. Срез до обхода — не больше предела на каждое место, где список
    встречается; обход целиком — по 200 000 на место."""
    _СчётныйСписок.отдано = _СчётныйСловарь.отдано = 0
    огромный = _СчётныйСписок({"name": f"p{i}", "in": "query"} for i in range(200_000))
    ответы = _СчётныйСловарь((str(i), {}) for i in range(200_000))
    свойства = _СчётныйСловарь((f"f{i}", {}) for i in range(200_000))
    обязательные = _СчётныйСписок(f"f{i}" for i in range(200_000))
    op = {"parameters": огромный, "responses": ответы,
          "requestBody": {"content": {"application/json": {"schema": {"properties": свойства,
                                                                      "required": обязательные}}}}}
    пути, методы = 30, len(openapi.METHODS)
    документ = {"paths": {f"/p{i}": {"parameters": огромный, **{m: op for m in openapi.METHODS}}
                          for i in range(пути)}}
    _СчётныйСписок.отдано = _СчётныйСловарь.отдано = 0

    ops = openapi.parse(документ)

    операций = пути * методы
    assert len(ops) == операций
    # Параметры: общие — раз на путь, свои — раз на операцию; обязательные поля тела.
    assert _СчётныйСписок.отдано <= openapi.PARAMS_LIMIT * (пути + операций) \
        + openapi.FIELDS_LIMIT * операций
    # Коды ответов и свойства тела — раз на операцию.
    assert _СчётныйСловарь.отдано <= (openapi.RESPONSES_LIMIT + openapi.FIELDS_LIMIT) * операций
    assert all(len(o["params"]) <= openapi.PARAMS_LIMIT for o in ops)
    assert all(len(o["responses"]) <= openapi.RESPONSES_LIMIT for o in ops)


# --- 3. локальное место — тоже через redact_url -----------------------------------------------


def test_3_a_signed_storage_location_loses_its_signature():
    источник = openapi.source_of("s", "local", "s3://bucket/spec.yaml?X-Amz-Signature=deadbeefsecret")
    assert "deadbeefsecret" not in источник["location"]
    assert openapi.source_of("s", "local", "spec/openapi.json")["location"] == "spec/openapi.json"


# --- 4. чужая запись не блокирует файл ----------------------------------------------------------


def test_4_a_foreign_record_with_a_list_nodeid_does_not_block_writing(tmp_path):
    путь = tmp_path / "db_effects.json"
    путь.write_text(json.dumps({
        "version": 2, "kind": "db.effects", "scope": {"system": "default", "env": "default"},
        "collected_at": "x", "collector": "",
        "records": [{"nodeid": ["x"], "table": {"a": 1}, "phase": ["p"]}],
    }), encoding="utf-8")
    artifacts.append_records(путь, "db.effects", "records",
                             [{"nodeid": "t", "table": "u", "run": {"id": "r", "startedAt": "2026-01-01T00:00:00+00:00"}}],
                             keep_runs=1)
    assert len(artifacts.read(путь)["records"]) == 2


# --- 5. ui_flow: не записан — строка, а не INTERNALERROR ----------------------------------------


def test_5_ui_flow_reports_a_failed_write(monkeypatch, tmp_path, capsys):
    from partest_atlas.config import load
    from partest_atlas.plugins import ui_flow as plugin

    (tmp_path / "atlas.yaml").write_text("artifacts_dir: atlas\n", encoding="utf-8")
    (tmp_path / "atlas" / "default" / "default" / "ui_flow.json").mkdir(parents=True)
    plugin._state.clear()
    plugin._state.update(config=load(tmp_path), links=[{
        "screen": "/", "control": "c", "action": "click", "method": "GET", "path": "/p",
        "status": 200, "gapMs": 1, "confidence": "direct"}])
    try:
        plugin.pytest_sessionfinish(None, 0)
    finally:
        plugin._state.clear()
    assert "не записан" in capsys.readouterr().out


# --- 6. секретные ключи по окончанию ---------------------------------------------------------------


@pytest.mark.parametrize("ключ", ["db_password", "passwordHash", "x-api-key", "refreshToken", "PG_DSN",
                                  "aws_credentials", "session_cookie", "user_pwd", "SSH_PRIVATE_KEY",
                                  "client_secret", "password"])
def test_6_secret_keys_by_ending(ключ):
    assert artifacts.is_secret_key(ключ)


@pytest.mark.parametrize("ключ", ["name", "table", "token_count", "sessions_total", "description"])
def test_6_ordinary_keys_are_not_secret(ключ):
    assert not artifacts.is_secret_key(ключ)


def test_6_a_secret_suffix_column_keeps_no_default():
    def col(name):
        return {"table_schema": "public", "table_name": "u", "column_name": name,
                "ordinal_position": 1, "data_type": "text", "is_nullable": "YES",
                "column_default": "'hunter2'::text", "character_maximum_length": None}

    doc = db_schema.build([col("db_password"), col("api_token"), col("note")])
    cols = {c["name"]: c for c in doc["databases"][0]["tables"][0]["columns"]}
    assert cols["db_password"]["default"] is None and cols["api_token"]["default"] is None
    assert cols["note"]["default"] == "'hunter2'::text"


def test_6_a_number_under_a_secret_key_is_a_count_not_a_secret():
    """`found.table.user_session: 3` — сколько раз таблица встретилась, а не секрет."""
    out = artifacts.redact_payload({"found": {"table": {"user_session": 3}}, "api_token": "abc"})
    assert out["found"]["table"]["user_session"] == 3
    assert out["api_token"] != "abc"
