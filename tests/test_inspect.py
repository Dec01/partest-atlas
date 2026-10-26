"""Владелец формата решает об артефакте сам: вид — по имени файла, область — по каталогу.

Раньше эти правила жили у потребителя (сервер карты держал свои `inspect`,
`discover_scopes` и таблицу ключей полезной нагрузки). Повторённое правило расходится
молча, поэтому они здесь.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from partest_atlas import artifacts, cli
from partest_atlas.artifacts import KINDS, PAYLOAD_KEYS, SCHEMA_VERSION, Scope
from partest_atlas.collect import (
    container_logs,
    db_schema,
    history,
    openapi,
    specs,
    ui_flow,
)


def _записать(путь, kind, payload=None, **kw):
    artifacts.write(путь, kind, payload if payload is not None else {}, **kw)
    return путь


# --- collected_at ----------------------------------------------------------------


def test_collected_at_is_written_as_given(tmp_path):
    путь = _записать(tmp_path / "db_schema.json", "db.schema", collected_at="2026-01-02T03:04:05+00:00")
    assert artifacts.read(путь)["collected_at"] == "2026-01-02T03:04:05+00:00"


def test_collected_at_from_a_datetime_is_iso_with_a_zone(tmp_path):
    когда = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=3)))
    путь = _записать(tmp_path / "db_schema.json", "db.schema", collected_at=когда)
    assert artifacts.read(путь)["collected_at"] == "2026-01-02T03:04:05+03:00"
    наивное = _записать(tmp_path / "x" / "db_schema.json", "db.schema",
                        collected_at=datetime(2026, 1, 2, 3, 4, 5))
    assert artifacts.read(наивное)["collected_at"][19:] != ""  # пояс дописан


@pytest.mark.parametrize("значение", ["", "   ", 5])
def test_a_bad_collected_at_is_refused_before_writing(tmp_path, значение):
    путь = tmp_path / "db_schema.json"
    with pytest.raises(ValueError, match="collected_at"):
        _записать(путь, "db.schema", collected_at=значение)
    assert not путь.exists()


def test_append_records_passes_collected_at(tmp_path):
    путь = tmp_path / "db_effects.json"
    artifacts.append_records(путь, "db.effects", "records", [{"table": "t"}],
                             collected_at="2026-01-02T03:04:05+00:00")
    assert artifacts.read(путь)["collected_at"] == "2026-01-02T03:04:05+00:00"


# --- validate: вид по имени, область по каталогу, схема 1 -----------------------------


def _документ(kind="db.schema", system="default", env="default", version=SCHEMA_VERSION):
    return {"version": version, "kind": kind, "scope": {"system": system, "env": env},
            "collected_at": "2026-01-01T00:00:00+00:00", "collector": ""}


def test_the_kind_must_match_the_file_it_lies_in():
    assert artifacts.validate(_документ("db.schema"), kind="db.schema") == []
    problems = artifacts.validate(_документ("db.schema"), kind="db.effects")
    assert len(problems) == 1 and "db_effects.json" in problems[0]


def test_the_scope_must_match_the_directory():
    assert artifacts.validate(_документ(system="shop", env="test"), scope=Scope("shop", "test")) == []
    problems = artifacts.validate(_документ(system="shop", env="test"), scope=Scope("shop", "prod"))
    assert len(problems) == 1 and "shop/prod" in problems[0]


def test_without_kind_and_scope_validate_is_as_before():
    assert artifacts.validate(_документ("bus.flow", "a", "b")) == []


def test_a_version_1_artifact_is_refused_with_advice():
    """Обещание «v1 читаются без миграции» противоречило `validate` — теперь одно правило:
    схема 1 не годится, её снимают заново."""
    документ = _документ(version=1)
    problems = artifacts.validate(документ)
    assert any("снимите артефакт заново" in p for p in problems)


# --- ключ полезной нагрузки -------------------------------------------------------------


def test_every_kind_but_load_profile_declares_its_payload_key():
    assert set(PAYLOAD_KEYS) == set(KINDS) - {"load.profile"}


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("db.schema", db_schema.build([])),
        ("contract.openapi", openapi.build({"paths": {}})),
        ("specs.requirements", specs.build([])),
        ("ui.flow", ui_flow.build([])),
        ("containers.logs", container_logs.build({})),
    ],
)
def test_the_declared_key_is_the_one_the_collector_writes(kind, payload):
    assert PAYLOAD_KEYS[kind] in payload


def test_list_kinds_use_their_key(tmp_path):
    assert PAYLOAD_KEYS["db.effects"] == PAYLOAD_KEYS["bus.flow"] == "records"
    путь = tmp_path / "runs.json"
    history.append(путь, {"a": 1})
    assert PAYLOAD_KEYS["runner.history"] in artifacts.read(путь)


# --- inspect -----------------------------------------------------------------------------


def test_inspect_missing(tmp_path):
    вывод = artifacts.inspect(tmp_path / "db_schema.json", "db.schema")
    assert вывод.state == artifacts.MISSING and not вывод.usable and вывод.doc == {}


@pytest.mark.parametrize("текст", ["", "{битый", "[]"])
def test_inspect_broken(tmp_path, текст):
    путь = tmp_path / "db_schema.json"
    путь.write_text(текст, encoding="utf-8")
    вывод = artifacts.inspect(путь, "db.schema")
    assert вывод.state == artifacts.BROKEN and вывод.problems


def test_inspect_newer(tmp_path):
    путь = tmp_path / "db_schema.json"
    путь.write_text(json.dumps(_документ(version=SCHEMA_VERSION + 1)), encoding="utf-8")
    вывод = artifacts.inspect(путь, "db.schema")
    assert вывод.state == artifacts.NEWER and вывод.doc == {}


def test_inspect_invalid_for_a_foreign_kind_and_a_foreign_scope(tmp_path):
    путь = _записать(tmp_path / "db_effects.json", "db.schema", {"databases": []}, scope=Scope("a", "b"))
    assert artifacts.inspect(путь, "db.effects").state == artifacts.INVALID
    вывод = artifacts.inspect(путь, "db.schema", scope=Scope("a", "c"))
    assert вывод.state == artifacts.INVALID and вывод.doc == {}


def test_inspect_ok_counts_records_and_hands_the_document(tmp_path):
    путь = _записать(tmp_path / "db_effects.json", "db.effects", {"records": [{}, {}]},
                     scope=Scope("a", "b"), collector="c")
    вывод = artifacts.inspect(путь, "db.effects", scope=Scope("a", "b"))
    assert вывод.state == artifacts.OK and вывод.usable
    assert вывод.records == 2 and вывод.collector == "c" and вывод.version == SCHEMA_VERSION
    assert вывод.doc["records"] == [{}, {}]
    assert вывод.warnings == []


def test_inspect_warns_when_the_payload_key_is_absent(tmp_path):
    путь = _записать(tmp_path / "db_effects.json", "db.effects", {})
    вывод = artifacts.inspect(путь, "db.effects")
    assert вывод.state == artifacts.OK and вывод.records is None
    assert any("records" in w for w in вывод.warnings)


def test_states_are_machine_codes():
    assert artifacts.STATES == ("ok", "missing", "broken", "newer", "invalid")


# --- области на диске ---------------------------------------------------------------------


def test_discover_scopes_finds_directories_with_a_known_artifact(tmp_path):
    _записать(tmp_path / "shop" / "test" / "db_schema.json", "db.schema")
    _записать(tmp_path / "shop" / "prod" / "runs.json", "runner.history")
    (tmp_path / "shop" / "пусто").mkdir()
    (tmp_path / "склад" / "test").mkdir(parents=True)
    (tmp_path / "склад" / "test" / "чужое.json").write_text("{}", encoding="utf-8")
    assert artifacts.discover_scopes(tmp_path) == [Scope("shop", "prod"), Scope("shop", "test")]
    assert artifacts.discover_scopes(tmp_path / "нет") == []


def test_scope_and_kind_from_the_path(tmp_path):
    путь = tmp_path / "atlas" / "shop" / "test" / "db_schema.json"
    assert artifacts.scope_of(путь) == Scope("shop", "test")
    assert artifacts.kind_of(путь) == "db.schema"
    assert artifacts.scope_of(tmp_path / "чужое.json") is None
    assert artifacts.kind_of(tmp_path / "чужое.json") is None


# --- check сверяет вид и область --------------------------------------------------------------


@pytest.fixture
def русская_локаль(monkeypatch):
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")


def test_check_catches_a_file_moved_to_another_scope(tmp_path, capsys, русская_локаль):
    _записать(tmp_path / "shop" / "prod" / "db_schema.json", "db.schema", scope=Scope("shop", "test"))
    assert cli.main(["check", str(tmp_path)]) == cli.EXIT_PROBLEMS
    assert "shop/prod" in capsys.readouterr().out


def test_check_catches_a_kind_under_a_foreign_name(tmp_path, capsys, русская_локаль):
    _записать(tmp_path / "db_effects.json", "db.schema")
    assert cli.main(["check", str(tmp_path / "db_effects.json")]) == cli.EXIT_PROBLEMS
    assert "db_effects.json" in capsys.readouterr().out


def test_check_of_a_well_laid_out_directory_is_clean(tmp_path, capsys, русская_локаль):
    _записать(tmp_path / "shop" / "test" / "db_schema.json", "db.schema", scope=Scope("shop", "test"))
    assert cli.main(["check", str(tmp_path)]) == 0


# --- метка прогона ----------------------------------------------------------------------------


def test_the_run_label_is_one_per_process(monkeypatch):
    monkeypatch.setattr(artifacts, "_run", {})
    первая = artifacts.run_label()
    assert set(первая) == {"id", "startedAt"} and первая["id"]
    assert artifacts.run_label() == первая


def test_the_run_id_comes_from_the_environment_when_given(monkeypatch):
    monkeypatch.setattr(artifacts, "_run", {})
    monkeypatch.setenv(artifacts.RUN_ID_ENV, "ci-1234")
    assert artifacts.run_label()["id"] == "ci-1234"


# --- публичный интерфейс ---------------------------------------------------------------------


@pytest.mark.parametrize("module", ["partest_atlas.artifacts", "partest_atlas.config",
                                    "partest_atlas.collect.history"])
def test_every_public_name_exists(module):
    import importlib

    mod = importlib.import_module(module)
    assert mod.__all__
    for name in mod.__all__:
        assert hasattr(mod, name), f"{module}.{name}"
