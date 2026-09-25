"""Контракт артефактов: формат читает чужая программа, и сломать его можно необратимо.

Набор устроен парами. Сначала — что владелец формата пишет сам, обязано проходить
проверку без единого замечания (иначе неверна проверка, а не запись). Потом — по одному
случаю на каждое расхождение, которое проверка обязана найти. Второе без первого
бессмысленно: функция, всегда возвращающая пустой список, прошла бы половину набора.
"""

from __future__ import annotations

import json

import pytest

from partest_atlas import artifacts
from partest_atlas.artifacts import KINDS, SCHEMA_VERSION, Scope


@pytest.fixture()
def artifact(tmp_path):
    path = tmp_path / "db_schema.json"
    artifacts.write(path, "db.schema", {"tables": [{"name": "t"}]}, collector="проба")
    return path


# --- что обязано проходить -------------------------------------------------


def test_what_the_owner_writes_validates_clean(artifact):
    """Без этого всё остальное — проверка чего-то, чего никто не производит."""
    assert artifacts.validate(artifacts.read(artifact)) == []


def test_write_carries_the_whole_header(artifact):
    doc = artifacts.read(artifact)
    assert doc["version"] == SCHEMA_VERSION
    assert doc["kind"] == "db.schema"
    assert doc["scope"] == {"system": "default", "env": "default"}
    assert doc["collector"] == "проба"
    assert doc["collected_at"]
    assert doc["tables"] == [{"name": "t"}]


def test_an_empty_collector_name_is_allowed(tmp_path):
    """Сборщик не обязан называться. Но ключ обязан быть — отсутствие и пустота
    отвечают на разные вопросы."""
    path = tmp_path / "bus_flow.json"
    artifacts.write(path, "bus.flow", {"records": []})
    assert artifacts.validate(artifacts.read(path)) == []


# --- снисходительность чтения ----------------------------------------------


def test_a_missing_file_reads_as_empty(tmp_path):
    """Карта обязана подниматься на проекте, где ещё ничего не собрано."""
    assert artifacts.read(tmp_path / "нет.json") == {}


def test_a_corrupt_file_reads_as_empty(tmp_path):
    path = tmp_path / "db_schema.json"
    path.write_text("{это не json", encoding="utf-8")
    assert artifacts.read(path) == {}


def test_but_validate_calls_an_empty_document_a_problem(tmp_path):
    """Снисходительность чтения и строгость проверки — разные задачи.

    Если бы `validate` тоже прощал пустоту, битый артефакт читался бы как «сходится», и
    это был бы ровно тот зелёный, который никто не выдавал.
    """
    path = tmp_path / "db_schema.json"
    path.write_text("{битый", encoding="utf-8")
    assert artifacts.validate(artifacts.read(path)) != []


# --- расхождения, которые проверка обязана находить ------------------------


@pytest.mark.parametrize(
    ("случай", "порча", "след"),
    [
        ("нет версии", lambda d: d.pop("version"), "version"),
        ("нет вида", lambda d: d.pop("kind"), "kind"),
        ("нет области", lambda d: d.pop("scope"), "scope"),
        ("нет времени", lambda d: d.pop("collected_at"), "collected_at"),
        ("нет сборщика", lambda d: d.pop("collector"), "collector"),
        ("неизвестный вид", lambda d: d.update(kind="выдуманный"), "kind"),
        ("версия строкой", lambda d: d.update(version="2"), "version"),
        ("область не объект", lambda d: d.update(scope=[]), "scope"),
        ("пустая система", lambda d: d.update(scope={"system": "", "env": "e"}), "scope.system"),
        ("сборщик числом", lambda d: d.update(collector=7), "collector"),
    ],
)
def test_validate_finds(artifact, случай, порча, след):
    doc = artifacts.read(artifact)
    порча(doc)
    problems = artifacts.validate(doc)
    assert problems, случай
    assert any(след in p for p in problems), (случай, problems)


def test_a_newer_schema_is_refused_rather_than_read_blind(artifact):
    """Артефакт новее нас читать вслепую нельзя: мы не знаем, что в нём изменилось."""
    doc = artifacts.read(artifact)
    doc["version"] = SCHEMA_VERSION + 1
    assert any("новее" in p for p in artifacts.validate(doc))


def test_a_bool_is_not_a_version(artifact):
    """`True == 1` в Python, поэтому наивная проверка на int это пропустит."""
    doc = artifacts.read(artifact)
    doc["version"] = True
    assert artifacts.validate(doc) != []


# --- виды и имена файлов ---------------------------------------------------


def test_every_kind_has_a_filename():
    for kind in KINDS:
        assert artifacts.filename(kind).endswith(".json")


def test_an_unknown_kind_fails_where_it_is_named():
    """Ошибка на месте, а не позже: иначе она всплывёт при чтении у потребителя."""
    with pytest.raises(ValueError):
        artifacts.filename("выдуманный")


def test_filenames_are_unique():
    """Два вида с одним именем файла затирали бы друг друга молча."""
    assert len(set(KINDS.values())) == len(KINDS)


# --- дописывание записей ---------------------------------------------------


def test_append_collapses_exact_repeats(tmp_path):
    path = tmp_path / "bus_flow.json"
    artifacts.append_records(path, "bus.flow", "records", [{"a": 1}, {"a": 1}, {"b": 2}])
    artifacts.append_records(path, "bus.flow", "records", [{"a": 1}, {"c": 3}])
    assert artifacts.read(path)["records"] == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_append_of_nothing_does_not_create_a_file(tmp_path):
    path = tmp_path / "bus_flow.json"
    artifacts.append_records(path, "bus.flow", "records", [])
    assert not path.exists()


def test_append_refuses_to_merge_with_another_collectors_file(tmp_path):
    """Файл другого вида не сливается с новым: иначе два сборщика молча смешались бы."""
    path = tmp_path / "bus_flow.json"
    artifacts.write(path, "db.schema", {"records": [{"чужое": True}]})
    artifacts.append_records(path, "bus.flow", "records", [{"своё": True}])
    doc = artifacts.read(path)
    assert doc["kind"] == "bus.flow"
    assert doc["records"] == [{"своё": True}]


# --- область ---------------------------------------------------------------


def test_scope_defaults_keep_the_single_project_case_unchanged():
    assert Scope().as_dict() == {"system": "default", "env": "default"}


def test_scope_from_env(monkeypatch):
    monkeypatch.setenv("ATLAS_SYSTEM", "с")
    monkeypatch.setenv("ATLAS_ENV", "к")
    assert Scope.from_env().as_dict() == {"system": "с", "env": "к"}


def test_scope_lays_artifacts_out_by_system_and_env(tmp_path):
    path = Scope("с", "к").path(tmp_path, "bus.flow")
    assert path == tmp_path / "с" / "к" / "bus_flow.json"


def test_scope_from_a_document_without_one_is_the_default():
    """Артефакт версии 1 области не знал — он обязан читаться без миграции."""
    assert Scope.from_doc({"version": 1, "kind": "bus.flow"}).as_dict() == {
        "system": "default",
        "env": "default",
    }


# --- атомарность -----------------------------------------------------------


def test_a_failed_write_leaves_no_debris(tmp_path, monkeypatch):
    """Прерванный прогон не должен оставить ни битого артефакта, ни временных файлов."""
    path = tmp_path / "bus_flow.json"

    def boom(*a, **kw):
        raise RuntimeError("прогон прерван")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(RuntimeError):
        artifacts.write(path, "bus.flow", {"records": []})

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_failed_rewrite_keeps_the_previous_artifact(tmp_path, monkeypatch):
    """Хуже отсутствующего артефакта только наполовину переписанный."""
    path = tmp_path / "bus_flow.json"
    artifacts.append_records(path, "bus.flow", "records", [{"было": True}])

    def boom(*a, **kw):
        raise RuntimeError("прогон прерван")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(RuntimeError):
        artifacts.write(path, "bus.flow", {"records": [{"стало": True}]})

    assert artifacts.read(path)["records"] == [{"было": True}]
    assert [p.name for p in tmp_path.iterdir()] == ["bus_flow.json"]
