"""`atlas.yaml`: отсутствие — рабочее состояние, битый файл — ошибка."""

from __future__ import annotations

import pytest

from partest_atlas import config as cfgmod


def write(tmp_path, text: str):
    (tmp_path / "atlas.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_no_file_is_a_working_empty_configuration(tmp_path):
    """Проект, который ещё ничего не настроил, обязан работать: артефакты в `atlas/`,
    сборщиков ни одного."""
    cfg = cfgmod.load(tmp_path)
    assert cfg.raw == {}
    assert cfg.artifacts_dir == tmp_path / "atlas"
    assert cfg.section("database") == {}


def test_a_broken_file_is_an_error_not_an_empty_configuration(tmp_path):
    """Иначе неработающая настройка выдавалась бы за отсутствующую — и сбор молча
    не запускался бы там, где его настраивали."""
    write(tmp_path, "project: [это\n  не: yaml")
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.load(tmp_path)


def test_a_file_that_is_not_an_object_is_refused(tmp_path):
    write(tmp_path, "- список\n- вместо\n- объекта")
    with pytest.raises(cfgmod.ConfigError, match="объект"):
        cfgmod.load(tmp_path)


def test_an_empty_file_is_an_empty_configuration(tmp_path):
    """Пустой файл — не битый: человек завёл его и ещё не заполнил."""
    write(tmp_path, "")
    assert cfgmod.load(tmp_path).raw == {}


def test_it_is_found_from_a_nested_directory(tmp_path):
    """Сборщик зовут и из корня, и из каталога тестов, и вручную откуда придётся."""
    write(tmp_path, "artifacts_dir: свои_артефакты\n")
    deep = tmp_path / "src" / "tests" / "глубоко"
    deep.mkdir(parents=True)
    assert cfgmod.load(deep).artifacts_dir == tmp_path / "свои_артефакты"


def test_artifacts_dir_is_relative_to_the_file_not_to_the_caller(tmp_path):
    """Иначе путь зависел бы от того, откуда позвали, и артефакты расползались бы."""
    write(tmp_path, "artifacts_dir: вывод\n")
    deep = tmp_path / "а" / "б"
    deep.mkdir(parents=True)
    assert cfgmod.load(deep).artifacts_dir == tmp_path / "вывод"


def test_an_absolute_artifacts_dir_is_left_alone(tmp_path):
    target = (tmp_path / "внешний").resolve()
    write(tmp_path, f"artifacts_dir: {target.as_posix()}\n")
    assert cfgmod.load(tmp_path).artifacts_dir == target


def test_scope_comes_from_the_environment_not_from_the_file(tmp_path, monkeypatch):
    """Файл один на все контуры; контур — различие окружения, а не файла."""
    write(tmp_path, "scope:\n  system: изфайла\n  env: изфайла\n")
    monkeypatch.setenv("ATLAS_SYSTEM", "из_окружения")
    monkeypatch.setenv("ATLAS_ENV", "контур")
    assert cfgmod.load(tmp_path).scope.as_dict() == {
        "system": "из_окружения",
        "env": "контур",
    }


def test_path_for_lays_the_artifact_out_by_scope(tmp_path, monkeypatch):
    write(tmp_path, "artifacts_dir: atlas\n")
    monkeypatch.setenv("ATLAS_SYSTEM", "с")
    monkeypatch.setenv("ATLAS_ENV", "к")
    assert cfgmod.load(tmp_path).path_for("db.schema") == tmp_path / "atlas" / "с" / "к" / "db_schema.json"


def test_env_reads_the_variable_the_file_named(monkeypatch):
    monkeypatch.setenv("ИМЯ_ИЗ_ФАЙЛА", "значение")
    assert cfgmod.env("ИМЯ_ИЗ_ФАЙЛА") == "значение"


def test_an_empty_variable_falls_back_to_the_default(monkeypatch):
    """Пустая строка в окружении — не значение. Иначе `DSN=""` выглядел бы заданным."""
    monkeypatch.setenv("ПУСТАЯ", "")
    assert cfgmod.env("ПУСТАЯ", "по_умолчанию") == "по_умолчанию"
