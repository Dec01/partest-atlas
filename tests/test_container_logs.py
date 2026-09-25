"""Разбор логов: без контейнеров, на тексте."""

from __future__ import annotations

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import container_logs as cl
from partest_atlas.collect.container_logs import Rule, listener_to_topic, scan_text


ТАБЛИЦА = Rule(kind="table", pattern=r'referenced from table "([a-z_]+)"', known_only=True)
СЛУШАТЕЛЬ = Rule(kind="listener", pattern=r"\b(\w+(?:Listener|Consumer))\.consume\b")
КЛАСС = Rule(kind="class", pattern=r"\b(c\.p\.[\w.]+)\b")


def test_a_table_name_is_taken_from_an_error_line():
    text = 'ERROR: update or delete violates constraint, referenced from table "task"'
    assert scan_text(text, [ТАБЛИЦА], {"table": {"task"}}) == {"table": {"task": 1}}


def test_known_only_drops_what_is_not_in_the_dictionary():
    """Без словаря правило «слово с точками — это топик» тащит каждое имя класса."""
    text = 'referenced from table "task" and referenced from table "выдуманная"'
    found = scan_text(text, [ТАБЛИЦА], {"table": {"task"}})
    assert found == {"table": {"task": 1}}


def test_without_known_only_the_dictionary_is_not_consulted():
    правило = Rule(kind="table", pattern=r'table "([a-z_]+)"', known_only=False)
    text = 'table "чужая_таблица"' .replace("чужая_таблица", "unknown_one")
    assert scan_text(text, [правило], {"table": {"task"}}) == {"table": {"unknown_one": 1}}


def test_repeats_are_counted_not_deduplicated():
    """Сколько раз контейнер задел таблицу — это и есть сила связи на карте."""
    text = 'referenced from table "task" ... referenced from table "task"'
    assert scan_text(text, [ТАБЛИЦА], {"table": {"task"}}) == {"table": {"task": 2}}


def test_a_disabled_rule_never_runs():
    выключено = Rule(kind="table", pattern=r'table "(\w+)"', enabled=False)
    assert cl.rules_from({"rules": [{"kind": "table", "pattern": "x", "enabled": False}]}) == []
    assert выключено.enabled is False


def test_a_rule_without_a_pattern_is_dropped():
    assert cl.rules_from({"rules": [{"kind": "table"}]}) == []


def test_value_pattern_filters_the_match_itself():
    правило = Rule(kind="class", pattern=r"\b([\w.]+Service)\b", value_pattern=r"^c\.p\.")
    text = "c.p.p.reference.services.ClientService и org.other.FakeService"
    assert scan_text(text, [правило]) == {"class": {"c.p.p.reference.services.ClientService": 1}}


def test_nothing_found_gives_nothing_not_empty_buckets():
    """Пустая корзина читалась бы как «искали и не нашли» наравне с «нашли ноль раз»."""
    assert scan_text("тут ничего нет", [ТАБЛИЦА], {"table": {"task"}}) == {}


def test_empty_text_is_not_an_error():
    assert scan_text("", [ТАБЛИЦА], {"table": {"task"}}) == {}


def test_results_are_ordered():
    """Два одинаковых снятия обязаны дать одинаковый артефакт."""
    правило = Rule(kind="table", pattern=r'table "(\w+)"')
    text = 'table "я" table "а" table "б"'
    assert list(scan_text(text, [правило])["table"]) == ["а", "б", "я"]


# --- слушатель → топик -----------------------------------------------------


@pytest.mark.parametrize(
    ("класс", "топик"),
    [
        ("ExcelImportRequestedListener", "excel.import.requested"),
        ("ExcelImportRequestedConsumer", "excel.import.requested"),
        ("ExcelImportRequestedHandler", "excel.import.requested"),
        ("ComputeResults", "compute-results"),
    ],
)
def test_a_listener_class_resolves_to_a_real_topic(класс, топик):
    assert listener_to_topic(класс, [топик]) == топик


def test_a_listener_without_a_matching_topic_resolves_to_nothing():
    """Угадать имя, которого нет, значило бы записать в карту несуществующую связь."""
    assert listener_to_topic("СовсемДругойListener", ["compute-results"]) is None


def test_framework_internals_are_dropped_rather_than_stored():
    """В логах их десятки, и все — внутренние классы фреймворка."""
    text = "ListenerConsumer.consume ClassicKafkaConsumer.consume"
    assert scan_text(text, [СЛУШАТЕЛЬ], {"topic": {"compute-results"}}) == {}


def test_a_resolved_listener_becomes_a_topic_not_a_listener():
    """Перевод — это вывод, а не находка, и в артефакте он лежит там же, где находки."""
    text = "ComputeResultsListener.consume ComputeResultsListener.consume"
    found = scan_text(text, [СЛУШАТЕЛЬ], {"topic": {"compute-results"}})
    assert found == {"topic": {"compute-results": 2}}
    assert "listener" not in found


# --- словари из соседних артефактов ---------------------------------------


def test_dictionaries_are_read_from_neighbouring_artifacts():
    схема = {"databases": [{"tables": [{"name": "task"}, {"name": "client"}]}]}
    шина = {"records": [{"topic": "compute-tasks"}, {"topic": "compute-tasks"}]}
    known = cl.known_values(схема, шина)
    assert known["table"] == {"task", "client"}
    assert known["topic"] == {"compute-tasks"}


def test_missing_neighbours_are_not_an_error():
    """Соседей ещё не снимали — словарь пуст, правила с `known_only` ничего не дадут.
    Это рабочее состояние, а не отказ собирать."""
    known = cl.known_values({}, {})
    assert known == {"table": set(), "topic": set()}


# --- содержание артефакта --------------------------------------------------


def test_containers_without_findings_are_left_out():
    doc = cl.build({"пустой": {}, "полезный": {"table": {"task": 1}}})
    assert [c["name"] for c in doc["containers"]] == ["полезный"]


def test_stats_count_distinct_values_across_containers():
    doc = cl.build(
        {
            "а": {"table": {"task": 1}, "topic": {"т": 1}},
            "б": {"table": {"task": 5, "client": 2}},
        }
    )
    assert doc["stats"] == {"containers": 2, "tables": 2, "topics": 1}


def test_the_artifact_it_writes_validates_clean(tmp_path):
    doc = cl.build({"а": {"table": {"task": 1}}})
    path = tmp_path / "container_logs.json"
    artifacts.write(path, cl.KIND, doc, collector="partest-atlas/container_logs")
    assert artifacts.validate(artifacts.read(path)) == []


def test_the_kind_is_one_the_contract_knows():
    assert cl.KIND in artifacts.KINDS
    assert artifacts.filename(cl.KIND) == "container_logs.json"


# --- источник журналов называет проект -------------------------------------


def test_no_source_configured_is_not_an_error():
    """Проект журналы не описал — законное состояние."""
    assert cl.open_source({}) is None


def test_a_malformed_source_is_refused_by_name():
    with pytest.raises(cl.SourceNotConfigured, match="модуль:функция"):
        cl.open_source({"source": "простоимя"})


def test_an_unimportable_source_says_so():
    with pytest.raises(cl.SourceNotConfigured, match="не импортируется"):
        cl.open_source({"source": "нет_модуля:журналы"})


def test_the_project_source_named_in_config_is_used(monkeypatch):
    """Сборщику нужен текст, а не инструмент: у одного проекта панель управления
    контейнерами, у другого `docker logs`, у третьего файл."""
    import sys
    import types

    m = types.ModuleType("журналы_проекта")
    m.достать = lambda: {"контейнер": "referenced from table \"task\""}
    monkeypatch.setitem(sys.modules, "журналы_проекта", m)

    источник = cl.open_source({"source": "журналы_проекта:достать"})
    найдено = {
        имя: scan_text(текст, [ТАБЛИЦА], {"table": {"task"}})
        for имя, текст in источник().items()
    }
    assert найдено == {"контейнер": {"table": {"task": 1}}}
