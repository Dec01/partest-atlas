"""Разбор контракта: без сети, на документе."""

from __future__ import annotations

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import openapi
from partest_atlas.collect.openapi import build, norm_path, parse


@pytest.fixture()
def документ():
    return {
        "paths": {
            "/api/v1/items": {
                "get": {"operationId": "list", "tags": ["Items"], "responses": {"200": {}}},
                "post": {
                    "operationId": "create",
                    "tags": ["Items"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
                        },
                    },
                    "responses": {"201": {}, "400": {}},
                },
            },
            "/api/v1/items/{itemId}": {
                "parameters": [{"name": "itemId", "in": "path", "required": True}],
                "get": {"operationId": "one", "responses": {"200": {}, "404": {}}},
                "delete": {"operationId": "drop", "deprecated": True, "responses": {"204": {}}},
            },
        },
        "components": {
            "schemas": {
                "Item": {
                    "required": ["name", "code"],
                    "properties": {"name": {}, "code": {}, "note": {}},
                }
            }
        },
    }


def test_every_declared_operation_is_taken(документ):
    assert len(parse(документ)) == 4


@pytest.mark.parametrize(
    ("путь", "ожидалось"),
    [
        ("/items/{itemId}", "/items/{id}"),
        ("/a/{x}/b/{y}", "/a/{id}/b/{id}"),
        ("/items", "/items"),
        ("", ""),
    ],
)
def test_identifiers_are_normalised(путь, ожидалось):
    """Иначе `/items/1` и `/items/2` — разные операции, и карта пухнет на каждый прогон."""
    assert norm_path(путь) == ожидалось


def test_the_raw_path_is_kept_next_to_the_normalised_one(документ):
    op = next(o for o in parse(документ) if o["operationId"] == "one")
    assert op["path"] == "/api/v1/items/{id}"
    assert op["rawPath"] == "/api/v1/items/{itemId}"


def test_path_level_parameters_reach_every_operation_under_it(документ):
    """Потерять их значит объявить обязательный параметр необязательным."""
    for op in parse(документ):
        if op["rawPath"].endswith("{itemId}"):
            assert [p["name"] for p in op["params"]] == ["itemId"]
            assert op["params"][0]["required"] is True


def test_a_referenced_body_is_resolved(документ):
    op = next(o for o in parse(документ) if o["operationId"] == "create")
    assert op["body"]["schema"] == "Item"
    assert op["body"]["requiredFields"] == ["code", "name"]
    assert set(op["body"]["fields"]) == {"name", "code", "note"}
    assert op["body"]["required"] is True


def test_no_body_is_none_not_an_empty_object(документ):
    """Пустой объект читался бы как «тело есть, но пустое» — другое утверждение."""
    op = next(o for o in parse(документ) if o["operationId"] == "list")
    assert op["body"] is None


def test_declared_response_codes_are_sorted(документ):
    op = next(o for o in parse(документ) if o["operationId"] == "create")
    assert op["responses"] == ["201", "400"]


def test_deprecation_is_carried(документ):
    op = next(o for o in parse(документ) if o["operationId"] == "drop")
    assert op["deprecated"] is True


def test_an_empty_document_parses_to_nothing():
    assert parse({}) == []


def test_a_path_that_is_not_an_object_is_skipped():
    """Кривой контракт не должен ронять сбор: наблюдение важнее аккуратности источника."""
    assert parse({"paths": {"/x": None, "/y": "строка"}}) == []


def test_keys_that_are_not_methods_are_ignored():
    """`summary`, `servers` и прочее на уровне пути — не операции."""
    doc = {"paths": {"/x": {"summary": "про путь", "get": {"responses": {}}}}}
    assert [o["method"] for o in parse(doc)] == ["GET"]


def test_a_circular_reference_does_not_hang():
    """Взаимные ссылки в схемах разворачиваются бесконечно, если не ограничить глубину."""
    doc = {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/A"}}}
                    },
                    "responses": {},
                }
            }
        },
        "components": {"schemas": {"A": {"$ref": "#/components/schemas/A"}}},
    }
    body = parse(doc)[0]["body"]
    # Главное — разбор завершился. А результат честный: схема названа, поля не
    # выдуманы. «Тело есть, схема A, перечислить её поля не удалось» — именно то,
    # что произошло, и пустой список полей тут не враньё, а отсутствие сведений.
    assert body["schema"] == "A"
    assert body["fields"] == []


def test_a_huge_schema_is_clipped():
    """Схемы бывают в сотни полей; артефакт от них растёт на мегабайты, а читают их всё
    равно не целиком."""
    свойства = {f"поле{i}": {} for i in range(200)}
    doc = {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Big"}}}
                    },
                    "responses": {},
                }
            }
        },
        "components": {"schemas": {"Big": {"properties": свойства}}},
    }
    assert len(parse(doc)[0]["body"]["fields"]) == openapi.FIELDS_LIMIT


# --- сводка ----------------------------------------------------------------


def test_the_summary_agrees_with_the_operations_under_it(документ):
    doc = build(документ)
    assert doc["stats"]["operations"] == len(doc["operations"])
    assert doc["stats"]["paths"] == 2
    assert doc["stats"]["deprecated"] == 1
    assert doc["stats"]["tags"] == ["Items"]


def test_the_summary_says_when_the_contract_has_no_descriptions(документ):
    """Именно по этому числу видно, что описания надо искать в другом источнике,
    а не что их собрали и они пусты."""
    assert build(документ)["stats"]["withDescription"] == 0


def test_descriptions_are_counted_where_they_exist():
    doc = {"paths": {"/x": {"get": {"summary": "есть описание", "responses": {}}}}}
    assert build(doc)["stats"]["withDescription"] == 1


def test_the_artifact_it_writes_validates_clean(tmp_path, документ):
    path = tmp_path / "openapi.json"
    artifacts.write(path, openapi.KIND, build(документ), collector="partest-atlas/openapi")
    assert artifacts.validate(artifacts.read(path)) == []


def test_the_kind_is_one_the_contract_knows():
    assert openapi.KIND in artifacts.KINDS
    assert artifacts.filename(openapi.KIND) == "openapi.json"


# --- источник и info: аддитивно, схема та же --------------------------------------


def test_info_carries_title_and_version_as_text():
    doc = {"openapi": "3.0.0", "info": {"title": "Склад", "version": 2}, "paths": {}}
    assert build(doc)["info"] == {"title": "Склад", "version": "2"}


def test_no_info_is_none_not_an_empty_string():
    assert build({"paths": {}})["info"] == {"title": None, "version": None}
    assert build({"info": "кривое"})["info"] == {"title": None, "version": None}


def test_build_without_a_source_has_no_source_key():
    """Код, который строит артефакт сам (демо, тесты), получает прежнюю форму плюс `info`."""
    assert "source" not in build({"paths": {}})


def test_build_carries_the_source_it_is_given():
    источник = {"service": "a", "type": "local", "location": "spec.json"}
    assert build({"paths": {}}, source=источник)["source"] == источник


@pytest.mark.parametrize(
    ("адрес", "ожидалось"),
    [
        ("https://u:p@host.example:8443/v3/api-docs?token=x#frag", "https://host.example:8443/v3/api-docs"),
        ("http://host/v3/api-docs", "http://host/v3/api-docs"),
        ("https://user@host/docs?apikey=1", "https://host/docs"),
        ("https://[::1]:9000/docs?x=1", "https://[::1]:9000/docs"),
        # Сырые `/ ? #` в пароле: граница userinfo не видна — закрыто на отказ, а не догадка
        # (находка security 2: раньше утекал пароль целиком или его начало).
        ("https://user:pa/ss@h.example/spec.json", "https://<redacted>"),
        ("https://user:pa#ss@h/x", "https://<redacted>"),
        ("https://user:pa?ss@h/x", "https://<redacted>"),
    ],
)
def test_the_url_source_loses_credentials_query_and_fragment(адрес, ожидалось):
    """Очистка адреса — `partest.redact.redact_url` у владельца, своей копии нет."""
    assert openapi.source_of("s", "url", адрес)["location"] == ожидалось


def test_there_is_no_own_copy_of_url_redaction():
    assert not hasattr(openapi, "redact_url")


def test_the_url_source_is_redacted_and_the_local_one_kept_as_declared():
    assert openapi.source_of("s", "url", "https://u:p@h/x?t=1") == {
        "service": "s", "type": "url", "location": "https://h/x",
    }
    assert openapi.source_of("s", "file", "docs/openapi.yaml") == {
        "service": "s", "type": "local", "location": "docs/openapi.yaml",
    }


def test_the_artifact_with_source_and_info_still_validates(tmp_path, документ):
    path = tmp_path / "openapi.json"
    payload = build(документ, source=openapi.source_of("s", "local", "spec.json"))
    artifacts.write(path, openapi.KIND, payload, collector="partest-atlas/openapi")
    assert artifacts.validate(artifacts.read(path)) == []
