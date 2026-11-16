"""Сводный раунд контролёров по кандидату 0.3.0 (25733d7..45f7d6d): каждая находка — тестом.

Номер пункта сводки — в docstring теста.
"""

from __future__ import annotations

import io
import json
import sys
import textwrap

import pytest

from partest_atlas import artifacts, cli
from partest_atlas import config as cfgmod
from partest_atlas.artifacts import Scope
from partest_atlas.collect import db_schema, openapi
from partest_atlas.plugins import db_effects as effects_plugin

СПЕЦИФИКАЦИЯ = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1"},
    "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}},
}


@pytest.fixture
def проект(tmp_path, monkeypatch):
    корень = tmp_path / "project"
    корень.mkdir()
    (корень / "atlas.yaml").write_text("artifacts_dir: atlas\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for имя in ("ATLAS_SYSTEM", "ATLAS_ENV", "DATABASE_URL", "PARTEST_TLS_VERIFY", "ATLAS_RUN_ID"):
        monkeypatch.delenv(имя, raising=False)
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield корень
    sys.modules.pop("confpartest", None)


def _run(capsys, *args):
    sys.modules.pop("confpartest", None)
    код = cli.main([str(a) for a in args])
    out, err = capsys.readouterr()
    return код, out, err


def _spec(корень, документ=None, swagger=None, name="spec.json"):
    (корень / name).write_text(json.dumps(документ or СПЕЦИФИКАЦИЯ, ensure_ascii=False), encoding="utf-8")
    (корень / "confpartest.py").write_text(
        f"swagger_files = {swagger or {'shop': ['local', name]}!r}\n", encoding="utf-8")


def _artifact(корень, kind="contract.openapi"):
    return корень / "atlas" / "default" / "default" / artifacts.filename(kind)


# --- 1. правило области: перевод строки не проходит --------------------------------------


@pytest.mark.parametrize("имя", ["qa\n", "qa\r\n", "\nqa"])
def test_1_a_newline_in_a_scope_name_is_refused(имя):
    assert not artifacts.valid_scope_part(имя)
    with pytest.raises(ValueError):
        Scope(system=имя)


def test_1_the_cli_refuses_a_newline_in_system(проект, capsys):
    _spec(проект)
    код, _, err = _run(capsys, "collect", "openapi", "--root", проект, "--system", "qa\n")
    assert код == 1 and "--system" in err


# --- 2. check не падает на каталоге не по правилу ---------------------------------------


def test_2_check_names_a_bad_scope_directory_instead_of_crashing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    путь = tmp_path / "Система" / "x" / "db_schema.json"
    путь.parent.mkdir(parents=True)
    путь.write_text(json.dumps({"version": 2, "kind": "db.schema",
                                "scope": {"system": "default", "env": "x"},
                                "collected_at": "2026-01-01T00:00:00+00:00", "collector": ""}),
                    encoding="utf-8")
    код = cli.main(["check", str(tmp_path)])
    out = capsys.readouterr().out
    assert код == cli.EXIT_PROBLEMS
    assert "каталог области не по правилу" in out


# --- 3. печатается замаскированное, без Cc и Cf -------------------------------------------


def test_3_collect_prints_the_masked_title_and_no_bidi_controls(проект, capsys):
    _spec(проект, {**СПЕЦИФИКАЦИЯ, "info": {"title": "Shop password=Hunter2xyz \u202e", "version": "1"}})
    код, out, _ = _run(capsys, "collect", "openapi", "--root", проект)
    assert код == 0
    assert "Hunter2xyz" not in out and "\u202e" not in out
    код, out, _ = _run(capsys, "--json", "collect", "openapi", "--root", проект)
    assert код == 0
    assert "Hunter2xyz" not in out and "\u202e" not in out
    assert json.loads(out)["kind"] == "contract.openapi"


def test_3_show_survives_a_scope_that_is_not_an_object(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    путь = tmp_path / "db_schema.json"
    путь.write_text(json.dumps({"version": 2, "kind": "db.schema\u202e", "scope": "default",
                                "collected_at": "x", "collector": ""}), encoding="utf-8")
    код = cli.main(["show", str(путь)])
    out = capsys.readouterr().out
    assert код == 0
    assert "не объект" in out and "\u202e" not in out


# --- 4. бомба якорей YAML ---------------------------------------------------------------------


def test_4_a_yaml_anchor_bomb_does_not_become_a_huge_artifact(проект, capsys):
    pytest.importorskip("yaml")
    уровни = ["a0: &a0 [" + ", ".join(["xxxxxxxxxx"] * 10) + "]"]
    for i in range(1, 8):
        уровни.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
    yaml_text = "openapi: 3.0.0\ninfo: {title: bomb, version: '1'}\n" + "\n".join(уровни) + textwrap.dedent("""
        paths:
          /p:
            get:
              tags: *a7
              description: *a7
              summary: *a7
              operationId: *a7
              parameters: *a7
              responses: {"200": {description: ok}}
    """)
    (проект / "bomb.yaml").write_text(yaml_text, encoding="utf-8")
    (проект / "confpartest.py").write_text("swagger_files = {'b': ['local', 'bomb.yaml']}\n",
                                           encoding="utf-8")
    код, _, err = _run(capsys, "collect", "openapi", "--root", проект)
    if код == 0:
        assert _artifact(проект).stat().st_size < 200_000
    else:
        assert not _artifact(проект).exists()


def test_4_parse_caps_text_and_keeps_only_string_tags():
    op = openapi.parse({"paths": {"/p": {"get": {
        "operationId": ["x"] * 10_000, "summary": "s" * 50_000, "description": 42,
        "tags": ["ok", ["nested"], 5], "parameters": [{"name": "n" * 9000}, "junk"],
    }}}})[0]
    assert op["operationId"] == ""  # список — не текст
    assert len(op["summary"]) == openapi.TEXT_LIMIT and op["description"] == "42"
    assert op["tags"] == ["ok"]
    assert [len(p["name"]) for p in op["params"]] == [openapi.TEXT_LIMIT]


def test_4_redaction_refuses_beyond_its_budget_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "REDACT_MAX_NODES", 100)
    путь = tmp_path / "db_schema.json"
    with pytest.raises(artifacts.PayloadTooLarge):
        artifacts.write(путь, "db.schema", {"databases": list(range(500))})
    assert not путь.exists()


def test_4_redaction_refuses_beyond_its_depth(tmp_path):
    глубоко: dict = {}
    узел = глубоко
    for _ in range(artifacts.REDACT_MAX_DEPTH + 5):
        узел["x"] = {}
        узел = узел["x"]
    with pytest.raises(artifacts.PayloadTooLarge):
        artifacts.redact_payload(глубоко)


# --- 5. UNC в любом написании -------------------------------------------------------------------


# publish-ok: заведомо несуществующие UNC-пути для проверки отказа до обращения к сети
@pytest.mark.parametrize("путь", [r"/\host\share", r"\/host/share", "//host/share", r"\\host\share"])
def test_5_unc_in_any_spelling_is_refused(tmp_path, путь):
    assert artifacts.is_unc(путь)
    (tmp_path / "atlas.yaml").write_text(f"artifacts_dir: '{путь}'\n", encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.load(tmp_path)
    with pytest.raises(ValueError):
        artifacts.discover_scopes(путь)
    with pytest.raises(ValueError):
        artifacts.inspect(путь + "/db_schema.json", "db.schema")


@pytest.mark.parametrize("путь", ["atlas", "C:/data/atlas", "/srv/atlas", "../atlas"])
def test_5_ordinary_paths_are_not_unc(путь):
    assert not artifacts.is_unc(путь)


# --- 6. чужой файл и время прогона -----------------------------------------------------------


@pytest.mark.parametrize("stamp", ["9999-12-31T23:59:59+00:00", "0001-01-01T00:00:00+05:00",
                                   "2999-01-01T00:00:00+00:00", "кривое", 42])
def test_6_a_hostile_start_time_is_unlabelled_not_a_crash(stamp):
    records = [{"table": "t", "run": {"id": "x", "startedAt": stamp}},
               {"table": "t", "run": {"id": "y", "startedAt": "2026-01-01T00:00:00+00:00"}}]
    assert artifacts.keep_last_runs(records, 1)[-1]["run"]["id"] == "y"


def test_6_only_dict_records_are_taken_from_the_previous_file(tmp_path):
    путь = tmp_path / "db_effects.json"
    путь.write_text(json.dumps({"version": 2, "kind": "db.effects", "scope": {"system": "default", "env": "default"},
                                "collected_at": "x", "collector": "", "records": [1, "s", None, {"table": "a"}]}),
                    encoding="utf-8")
    artifacts.append_records(путь, "db.effects", "records", [{"table": "b"}])
    assert artifacts.read(путь)["records"] == [{"table": "a"}, {"table": "b"}]


# --- 7. секретные ключи и совпавшие ключи ------------------------------------------------------


def test_7_values_under_secret_keys_are_masked_whole():
    from partest.redact import REDACTED

    out = artifacts.redact_payload({"password": "plain", "nested": [{"apiKey": "k", "ok": "v"}],
                                    "client_secret": {"deep": "x"}})
    assert out["password"] == REDACTED and out["client_secret"] == REDACTED
    assert out["nested"][0] == {"apiKey": REDACTED, "ok": "v"}


def test_7_keys_that_collide_after_masking_are_not_merged():
    out = artifacts.redact_payload({"token=abc111secret": 1, "token=def222secret": 2})
    assert sorted(out.values()) == [1, 2]
    assert len(out) == 2


# --- 8. идентификаторы не маскируются ---------------------------------------------------------------


def test_8_identifiers_survive_redaction():
    payload = {"operations": [{"method": "POST", "path": "/v1/users/{id}/password:reset",
                               "rawPath": "/v1/users/{userId}/password:reset", "service": "token=x"}],
               "found": {"table": {"password=1": 1}}}
    out = artifacts.redact_payload(payload)
    assert out["operations"][0]["path"] == "/v1/users/{id}/password:reset"
    assert out["operations"][0]["rawPath"] == "/v1/users/{userId}/password:reset"
    # Строка с `=` под ключом-идентификатором и словарь под ним — общим проходом
    # (узкий раунд security): `service: token=x` и ключ `password=1` маскируются.
    assert "x" not in out["operations"][0]["service"].replace("***", "")
    assert out["found"]["table"] != {"password=1": 1}


def test_8_nodeid_is_still_masked():
    out = artifacts.redact_payload({"nodeid": "t.py::test[token=abc123secret]"})
    assert "abc123secret" not in out["nodeid"]


def test_8_a_secret_column_keeps_its_type_but_not_its_default():
    def col(name, default):
        return {"table_schema": "public", "table_name": "u", "column_name": name,
                "ordinal_position": 1, "data_type": "text", "is_nullable": "YES",
                "column_default": default, "character_maximum_length": None}

    doc = db_schema.build([col("password", "'hunter2'::text"), col("note", "'x'::text")])
    cols = {c["name"]: c for c in doc["databases"][0]["tables"][0]["columns"]}
    assert cols["password"]["default"] is None and cols["password"]["type"] == "text"
    assert cols["note"]["default"] == "'x'::text"


# --- 9. ATLAS_RUN_ID ---------------------------------------------------------------------------------


def test_9_the_run_id_is_sanitised(monkeypatch):
    monkeypatch.setattr(artifacts, "_run", {})
    monkeypatch.setenv(artifacts.RUN_ID_ENV, "ci/42 <script>" + "x" * 100)
    ident = artifacts.run_label()["id"]
    assert len(ident) == 64 and set(ident) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
    assert ident.startswith("ci_42__script_")


# --- 10, 11. прогон — это run.id; последняя запись связи остаётся --------------------------------------


def _rec(run_id, started, nodeid="a", table="t"):
    return {"nodeid": nodeid, "table": table, "run": {"id": run_id, "startedAt": started}}


def test_10_a_run_is_its_id_and_its_freshness_the_latest_start():
    records = [_rec("r1", "2026-01-01T00:00:00+00:00", "x"), _rec("r2", "2026-01-02T00:00:00+00:00", "y"),
               _rec("r1", "2026-01-03T00:00:00+00:00", "z")]
    kept = artifacts.keep_last_runs(records, 1)
    assert [r["run"]["id"] for r in kept if r["nodeid"] != "y"] == ["r1", "r1"]


def test_11_debug_runs_do_not_push_out_other_tests_links():
    """Прогон 0 — весь набор (a и b), затем двадцать отладочных `pytest -k a`."""
    records = [_rec("r0", "2026-01-01T00:00:00+00:00", "a"), _rec("r0", "2026-01-01T00:00:00+00:00", "b")]
    for i in range(1, 21):
        records.append(_rec(f"r{i}", f"2026-01-02T00:00:{i:02d}+00:00", "a"))
    kept = artifacts.keep_last_runs(records, 3)
    assert {"r18", "r19", "r20"} <= {r["run"]["id"] for r in kept}
    assert any(r["nodeid"] == "b" for r in kept), "связь теста b вытеснена отладкой a"
    assert not any(r["run"]["id"] == "r5" for r in kept)


# --- 14. службы -----------------------------------------------------------------------------------------


ВТОРАЯ = {"openapi": "3.0.3", "info": {"title": "Stock", "version": "2"},
          "paths": {"/stock": {"get": {"responses": {"200": {"description": "ok"}}}}}}


def test_14_a_service_removed_from_swagger_files_leaves_the_artifact(проект, capsys):
    _spec(проект, swagger={"shop": ["local", "spec.json"], "stock": ["local", "stock.json"]})
    (проект / "stock.json").write_text(json.dumps(ВТОРАЯ), encoding="utf-8")
    assert _run(capsys, "collect", "openapi", "--root", проект)[0] == 0
    (проект / "confpartest.py").write_text("swagger_files = {'shop': ['local', 'spec.json']}\n", encoding="utf-8")

    assert _run(capsys, "collect", "openapi", "--root", проект, "--service", "shop")[0] == 0

    doc = artifacts.read(_artifact(проект))
    assert [s["service"] for s in doc["services"]] == ["shop"]
    assert {op["service"] for op in doc["operations"]} == {"shop"}


def test_14_services_carry_their_own_time_and_follow_swagger_files_order(проект, capsys):
    _spec(проект, swagger={"stock": ["local", "stock.json"], "shop": ["local", "spec.json"]})
    (проект / "stock.json").write_text(json.dumps(ВТОРАЯ), encoding="utf-8")
    assert _run(capsys, "collect", "openapi", "--root", проект)[0] == 0
    было = {s["service"]: s["collectedAt"] for s in artifacts.read(_artifact(проект))["services"]}

    assert _run(capsys, "collect", "openapi", "--root", проект, "--service", "shop")[0] == 0

    doc = artifacts.read(_artifact(проект))
    assert [s["service"] for s in doc["services"]] == ["stock", "shop"]
    assert [op["service"] for op in doc["operations"]] == ["stock", "shop"]
    stock = next(s for s in doc["services"] if s["service"] == "stock")
    assert stock["collectedAt"] == было["stock"]


def test_14_kept_services_are_deduplicated_and_only_strings(tmp_path):
    путь = tmp_path / "default" / "default" / "openapi.json"
    artifacts.write(путь, "contract.openapi", {
        "operations": [{"service": "a", "path": "/1"}, {"service": "a", "path": "/2"}, {"service": 5}],
        "services": [{"service": "a", "info": {}}, {"service": "a", "info": {"dup": 1}}, {"service": 5}, "junk"],
    }, scope=Scope())
    kept = openapi._kept_services(путь, Scope(), "b", ["a", "b"], [])
    assert [k["service"] for k in kept] == ["a"]
    assert [op["path"] for op in kept[0]["_operations"]] == ["/1", "/2"]


# --- 15. откуда взят относительный путь -----------------------------------------------------------------


def test_15_resolved_from_and_a_note_when_two_files_differ(проект, capsys, tmp_path):
    _spec(проект)
    (tmp_path / "spec.json").write_text(json.dumps({**СПЕЦИФИКАЦИЯ, "info": {"title": "cwd", "version": "0"}}),
                                       encoding="utf-8")
    код, _, err = _run(capsys, "collect", "openapi", "--root", проект)
    assert код == 0
    assert artifacts.read(_artifact(проект))["source"]["resolvedFrom"] == "cwd"
    assert "различаются" in err


# --- 17. голый collect ----------------------------------------------------------------------------------


def test_17_bare_collect_runs_everything_configured_and_skips_the_rest(проект, capsys):
    _spec(проект)
    код, out, err = _run(capsys, "collect", "--root", проект)
    assert код == 0
    assert "contract.openapi" in out
    assert "db-schema не настроен" in err and "container-logs не настроен" in err


def test_17_bare_collect_fails_with_1_when_a_configured_one_fails(проект, capsys):
    _spec(проект)
    (проект / "atlas.yaml").write_text("artifacts_dir: atlas\ndatabase:\n  dsn_env: NO_SUCH_VAR_X\n",
                                       encoding="utf-8")
    код, out, err = _run(capsys, "--json", "collect", "--root", проект)
    assert код == 1
    results = json.loads(out)["results"]
    assert [r["collector"] for r in results] == ["openapi", "db-schema"]
    assert "error" in results[1] and "kind" in results[0]


# --- 18. операций 0 и ошибка в --json ---------------------------------------------------------------------


def test_18_a_spec_without_paths_is_a_warning_not_a_failure(проект, capsys):
    _spec(проект, {"openapi": "3.0.0", "info": {"title": "x", "version": "1"}})
    код, _, err = _run(capsys, "collect", "openapi", "--root", проект)
    assert код == 0 and "операций 0" in err


def test_18_json_failure_is_an_error_object(проект, capsys):
    код, out, _ = _run(capsys, "--json", "collect", "openapi", "--root", проект)
    assert код == 1
    assert "confpartest" in json.loads(out)["error"]


# --- 19. inspect ------------------------------------------------------------------------------------------


def test_19_inspect_refuses_an_unknown_kind_even_for_a_missing_file(tmp_path):
    with pytest.raises(ValueError):
        artifacts.inspect(tmp_path / "нет.json", "no.such.kind")


def test_19_inspect_reads_a_bom_and_calls_nan_broken(tmp_path):
    doc = {"version": 2, "kind": "db.schema", "scope": {"system": "default", "env": "default"},
           "collected_at": "x", "collector": "", "databases": []}
    путь = tmp_path / "db_schema.json"
    путь.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode())
    assert artifacts.inspect(путь, "db.schema").state == artifacts.OK
    путь.write_text(json.dumps({**doc, "stats": float("nan")}), encoding="utf-8")
    assert artifacts.inspect(путь, "db.schema").state == artifacts.BROKEN


# --- 20. пустая область — «не задано» --------------------------------------------------------------------


def test_20_an_empty_system_flag_means_not_set(проект, capsys, monkeypatch):
    _spec(проект)
    monkeypatch.setenv("ATLAS_SYSTEM", "")
    код, _, err = _run(capsys, "collect", "openapi", "--root", проект, "--system", "")
    assert код == 0, err
    assert _artifact(проект).is_file()


# --- 21. неизвестные ключи у всех плагинов; значение database.key не печатается ---------------------------


def test_21_bus_flow_and_ui_flow_name_unknown_keys(monkeypatch, tmp_path, capsys):
    from partest_atlas.plugins import bus_flow as bus_plugin
    from partest_atlas.plugins import ui_flow as ui_plugin

    (tmp_path / "atlas.yaml").write_text("bus_flow:\n  readr: x\nui_flow:\n  windw: 1\n", encoding="utf-8")

    class Конфиг:
        def addinivalue_line(self, *a):
            pass

    for plugin, env in ((bus_plugin, "ATLAS_BUS_FLOW"), (ui_plugin, "ATLAS_UI_FLOW")):
        monkeypatch.setenv(env, "1")
        monkeypatch.setattr(plugin, "load", lambda *a, **kw: cfgmod.load(tmp_path))
        plugin._state.clear()
        plugin.pytest_configure(Конфиг())
        plugin._state.clear()
    out = capsys.readouterr().out
    assert "bus_flow.readr" in out and "ui_flow.windw" in out


def test_21_a_bad_database_key_is_named_without_its_value():
    with pytest.raises(ValueError) as отказ:
        db_schema.key_from({"key": ["секрет42"]})
    assert "database.key" in str(отказ.value) and "секрет42" not in str(отказ.value)


# --- 22. stdout после main — прежний ---------------------------------------------------------------------


def test_22_stdout_errors_are_restored_after_main(monkeypatch, capsys):
    поток = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", поток)
    cli.main(["check", "нет-такого-пути.json"])
    assert поток.errors == "strict"


# --- 13. фазы в режиме session — одна строка --------------------------------------------------------------


def test_13_phases_in_session_mode_say_they_are_not_written(monkeypatch, capsys):
    effects_plugin._state.clear()
    effects_plugin._state.update(phases=True)
    effects_plugin._phases_unused("режим session")
    effects_plugin._state.clear()
    assert "фазы не пишутся" in capsys.readouterr().out


def test_13_a_failed_phase_boundary_ends_the_window_without_phases(capsys):
    from partest_atlas.collect.db_effects import DbEffects

    probe = DbEffects(conn=None)
    probe._phased = [{"phase": "setup"}]

    def сломан(*a, **kw):
        raise OSError("связь")

    probe.checkpoint = сломан

    class Тест:
        nodeid, name = "t.py::a", "a"

    effects_plugin._state.clear()
    effects_plugin._state.update(config=object(), phased=(Тест.nodeid, probe))
    effects_plugin._phase_boundary(Тест(), "call")
    try:
        assert "phased" not in effects_plugin._state
        assert probe._phased == []
    finally:
        effects_plugin._state.clear()
