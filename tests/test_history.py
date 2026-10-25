"""История прогонов: накапливается, ограничена, и старое теряется раньше нового."""

from __future__ import annotations

import json
import warnings
from datetime import datetime
from pathlib import Path

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import history


@pytest.fixture()
def путь(tmp_path):
    return tmp_path / "runs.json"


def test_the_first_run_creates_the_history(путь):
    runs = history.append(путь, {"id": 1})
    assert runs == [{"id": 1}]
    assert artifacts.validate(artifacts.read(путь)) == []


def test_runs_accumulate_across_calls(путь):
    """Ради этого артефакт и существует: тренд виден только по нескольким прогонам."""
    history.append(путь, {"id": 1})
    history.append(путь, {"id": 2})
    assert [r["id"] for r in history.append(путь, {"id": 3})] == [1, 2, 3]


def test_the_oldest_is_dropped_first(путь):
    for i in range(5):
        runs = history.append(путь, {"id": i}, limit=3)
    assert [r["id"] for r in runs] == [2, 3, 4]


def test_a_limit_of_zero_keeps_nothing():
    assert history.trim([{"id": 1}], 0) == []


def test_a_negative_limit_is_not_a_slice_from_the_end():
    """`runs[-(-1):]` вернул бы почти всё — тихо и неверно."""
    assert history.trim([{"id": 1}, {"id": 2}], -1) == []


def test_nothing_to_read_is_an_empty_history(tmp_path):
    assert history.read(tmp_path / "нет.json") == []


def test_a_file_of_another_kind_is_not_mistaken_for_history(путь):
    """Без проверки вида чужой артефакт под этим именем был бы молча дописан. В 0.2.0 он
    молча затирался; теперь он не трогается, а запись прогона пропускается."""
    artifacts.write(путь, "db.schema", {"runs": [{"чужое": True}]})
    до = путь.read_bytes()
    assert history.read(путь) == []
    with pytest.warns(history.UnreadableHistory, match="вида 'db.schema'"):
        assert history.append(путь, {"id": 1}) == []
    assert путь.read_bytes() == до


def test_an_interrupted_append_keeps_the_previous_history(путь, monkeypatch):
    """Прерванное снятие схемы стоит повторного снятия. Прерванная дописка истории
    теряет прогоны прошлых дней — их уже не пересобрать."""
    history.append(путь, {"id": 1})
    history.append(путь, {"id": 2})

    def boom(*a, **kw):
        raise RuntimeError("прогон прерван")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(RuntimeError):
        history.append(путь, {"id": 3})

    assert [r["id"] for r in history.read(путь)] == [1, 2]


def test_entries_are_copied_not_referenced(путь):
    """Иначе вызывающий, поправив свой словарь после дописки, менял бы историю."""
    запись = {"id": 1}
    history.append(путь, запись)
    запись["id"] = 99
    assert history.read(путь)[0]["id"] == 1


# --- срез покрытия ---------------------------------------------------------


def test_the_snapshot_takes_the_known_summary_keys():
    doc = {"summary": {"avg": 67.7, "full": 45, "partial": 106, "empty": 9, "endpoints": 160, "лишнее": 1}}
    assert history.coverage_snapshot(doc) == {
        "avg": 67.7,
        "full": 45,
        "partial": 106,
        "empty": 9,
        "endpoints": 160,
        "avgRule": 1,
    }


def test_a_missing_key_is_omitted_not_nulled():
    """Артефакты переживают выпуски. Требовать от прошлогоднего отчёта сегодняшних
    полей значит развалить тренд ровно там, где он и нужен."""
    assert history.coverage_snapshot({"summary": {"avg": 1.0}}) == {"avg": 1.0, "avgRule": 1}


def test_the_snapshot_does_not_change_with_the_optin_selection_key():
    """`meta.selection.optin` из partest 2.5.0 — ключ аддитивный. Срез берёт только
    сводку, и отчёт с этим ключом даёт тот же срез, что и без него."""
    summary = {"avg": 50.0, "full": 1, "partial": 1, "empty": 0, "endpoints": 2}
    old = {"meta": {"selection": {}}, "summary": dict(summary)}
    new = {
        "meta": {
            "selection": {
                "optin": {
                    "slow": {"state": "env", "env": "RUN_SLOW", "deselected": 0},
                    "manual": {"state": "off", "env": None, "deselected": 3},
                }
            }
        },
        "summary": dict(summary),
    }
    assert history.coverage_snapshot(new) == history.coverage_snapshot(old) == {**summary, "avgRule": 1}


def test_no_report_is_an_empty_snapshot():
    assert history.coverage_snapshot(None) == {}
    assert history.coverage_snapshot({}) == {}


def test_the_kind_is_one_the_contract_knows():
    assert history.KIND in artifacts.KINDS
    assert artifacts.filename(history.KIND) == "runs.json"


# --- срез на отчётах двух эпох partest -------------------------------------
#
# Образцы в `tests/data/` — coverage.json, как их пишут partest 2.6.0 (правило среднего 1,
# без `meta.avgRule`) и 2.7.0 (правило 2, `scored`, `summary.depth`, `avg: null`). Это
# файлы, а не вызов partest: формат принадлежит ему, и тест не должен зависеть от того,
# какой partest установлен рядом. Строки одни и те же: две полные-частичные, одна
# пустая, одна `exception`.

DATA = Path(__file__).parent / "data"


def образец(имя):
    return json.loads((DATA / имя).read_text(encoding="utf-8"))


def test_a_2_6_0_report_gives_the_snapshot_it_gave_plus_its_rule():
    """Правило 1: строка `exception` вошла в среднее как 100 % — (100+50+0+100)/4."""
    assert history.coverage_snapshot(образец("coverage-2.6.0.json")) == {
        "avg": 62.5,
        "full": 1,
        "partial": 1,
        "empty": 1,
        "endpoints": 4,
        "exception": 1,
        "avgRule": 1,
    }


def test_a_2_7_0_report_keeps_its_rule_the_scored_rows_and_the_depth():
    """Правило 2: `exception` вне среднего — (100+50+0)/3 по трём `scored`. Глубина —
    объектом, как у владельца формата."""
    assert history.coverage_snapshot(образец("coverage-2.7.0.json")) == {
        "avg": 50.0,
        "full": 1,
        "partial": 1,
        "empty": 1,
        "endpoints": 4,
        "exception": 1,
        "scored": 3,
        "depth": {
            "cells": 6,
            "status": 5,
            "schema": 2,
            "values": None,
            "statusPct": 83.33,
            "schemaPct": 33.33,
        },
        "avgRule": 2,
    }


def test_the_two_epochs_differ_by_added_keys_only():
    """Новый срез — старый плюс ключи; у общих ключей тип значения тот же."""
    old = history.coverage_snapshot(образец("coverage-2.6.0.json"))
    new = history.coverage_snapshot(образец("coverage-2.7.0.json"))
    assert set(old) <= set(new)
    assert all(type(old[k]) is type(new[k]) for k in old)


def test_an_unmeasured_2_7_0_report_is_null_not_zero():
    """partest 2.7.0 пишет `avg: null`, когда считать было не по чему. Срез несёт null
    как есть; глубина `null` — тоже `null`, а не пропуск: ключ в отчёте был."""
    snap = history.coverage_snapshot(образец("coverage-2.7.0-empty.json"))
    assert snap["avg"] is None
    assert snap["endpoints"] == 0 and snap["scored"] == 0
    assert "depth" in snap and snap["depth"] is None
    assert snap["avgRule"] == 2


def test_an_unmeasured_2_6_0_report_is_null_too():
    """До 2.7.0 тот же случай давал `avg: 0.0` при `endpoints: 0`. Это «не измерено», а
    не «ничего не покрыто», — как и читает его partest 2.7.0, — и в срезе это null."""
    old = history.coverage_snapshot(образец("coverage-2.6.0-empty.json"))
    new = history.coverage_snapshot(образец("coverage-2.7.0-empty.json"))
    assert old["avg"] is None
    assert old["avg"] == new["avg"]
    assert "depth" not in old  # глубины 2.6.0 не писал — ключа нет, а не null


def test_zero_with_endpoints_is_a_measured_zero():
    """Ноль при посчитанных эндпоинтах — замер: всё вызванное не покрыто."""
    snap = history.coverage_snapshot({"summary": {"avg": 0.0, "endpoints": 3}})
    assert snap["avg"] == 0.0


def test_zero_without_an_endpoint_count_is_kept():
    """Без `endpoints` не по чему отличить «не измерено» от нуля — значение как есть."""
    assert history.coverage_snapshot({"summary": {"avg": 0.0}})["avg"] == 0.0


def test_false_endpoints_is_not_a_count():
    """В JSON `false` и `0` — разные значения; срез не путает их, как Python."""
    snap = history.coverage_snapshot({"summary": {"avg": 0.0, "endpoints": False}})
    assert snap["avg"] == 0.0 and "endpoints" not in snap


def test_only_exception_rows_under_rule_2_is_null():
    """Правило 2: набор из одних `exception` не оценивается — partest пишет null."""
    doc = {
        "meta": {"avgRule": 2},
        "summary": {"avg": None, "full": 0, "partial": 0, "empty": 0, "exception": 2,
                    "endpoints": 2, "scored": 0, "depth": None},
    }
    snap = history.coverage_snapshot(doc)
    assert snap["avg"] is None and snap["scored"] == 0 and snap["avgRule"] == 2


def test_an_unknown_rule_is_kept_as_written():
    """Правило новее известных — не повод его угадывать: что написано, то и в срезе."""
    doc = {"meta": {"avgRule": 3}, "summary": {"avg": 10.0}}
    assert history.coverage_snapshot(doc)["avgRule"] == 3


@pytest.mark.parametrize("правило", ["2", 2.0, True, None, [2], {"r": 2}, 10**13])
def test_a_rule_that_is_not_an_integer_is_unknown(правило):
    """Не целое — не правило: `null`, «правило неизвестно», а не догадка «1» или «2»."""
    doc = {"meta": {"avgRule": правило}, "summary": {"avg": 10.0}}
    assert history.coverage_snapshot(doc)["avgRule"] is None


@pytest.mark.parametrize("мета", [[], "x", 2, None])
def test_a_meta_that_is_not_a_mapping_is_an_unknown_rule(мета):
    """`meta` есть, но не объект — о правиле отчёт ничего не говорит; «1» было бы догадкой."""
    assert history.coverage_snapshot({"meta": мета, "summary": {"avg": 1.0}})["avgRule"] is None


def test_no_meta_at_all_is_rule_1():
    assert history.coverage_snapshot({"summary": {"avg": 1.0}})["avgRule"] == 1
    assert history.coverage_snapshot({"meta": {}, "summary": {"avg": 1.0}})["avgRule"] == 1


def test_a_report_without_a_summary_gets_no_rule():
    """Правило уточняет числа сводки. Нет чисел — нечего и уточнять."""
    assert history.coverage_snapshot({"meta": {"avgRule": 2}}) == {}


# --- недоверенный отчёт ----------------------------------------------------


@pytest.mark.parametrize("документ", [None, [], "x", 5, {"summary": []}, {"summary": "x"},
                                       {"summary": None}])
def test_a_document_or_summary_that_is_not_a_mapping_is_absent(документ):
    assert history.coverage_snapshot(документ) == {}


@pytest.mark.parametrize("значение", [float("nan"), float("inf"), float("-inf"), "50", True,
                                       [50], {"v": 50}, 10**400])
def test_an_avg_that_is_not_a_finite_number_is_null(значение):
    snap = history.coverage_snapshot({"summary": {"avg": значение, "endpoints": 3}})
    assert snap["avg"] is None


@pytest.mark.parametrize("значение", [-1, 1.5, 2.0, "3", True, None, [3], float("nan"),
                                       10**13])
def test_a_counter_that_is_not_a_count_is_not_written(значение):
    """У счётчика нет «не измерено»: кривое значение не пишется, `null` сменил бы тип."""
    summary = {key: значение for key in history.COUNT_KEYS}
    snap = history.coverage_snapshot({"summary": {"avg": 1.0, **summary}})
    assert snap == {"avg": 1.0, "avgRule": 1}


@pytest.mark.parametrize("глубина", [5, "x", [1], True])
def test_a_depth_that_is_not_a_mapping_is_absent(глубина):
    snap = history.coverage_snapshot({"summary": {"avg": 1.0, "depth": глубина}})
    assert "depth" not in snap


def test_a_partial_depth_is_kept_as_it_is():
    """Ключи глубины переносятся какие есть: недостающие не придумываются."""
    snap = history.coverage_snapshot({"summary": {"avg": 1.0, "depth": {"cells": 2}}})
    assert snap["depth"] == {"cells": 2}


def test_depth_keeps_only_safe_names_and_values():
    depth = {
        "cells": 4,
        "statusPct": float("nan"),
        "schemaPct": float("inf"),
        "big": 10**400,
        "values": None,
        "text": "<script>",
        "flag": True,
        "nested": {"a": 1},
        "list": [1],
        "with space": 1,
        "кириллица": 1,
        "x" * 33: 1,
        "x" * 32: 1,
        7: 1,
        "__proto__": 1,
        "a__b": 1,
        "_ok": 1,
    }
    snap = history.coverage_snapshot({"summary": {"avg": 1.0, "depth": depth}})
    assert snap["depth"] == {
        "cells": 4,
        "statusPct": None,
        "schemaPct": None,
        "big": None,
        "values": None,
        "x" * 32: 1,
        "_ok": 1,
    }


def test_depth_keeps_at_most_sixteen_keys():
    depth = {f"k{i}": i for i in range(40)}
    snap = history.coverage_snapshot({"summary": {"avg": 1.0, "depth": depth}})
    assert list(snap["depth"]) == [f"k{i}" for i in range(history.DEPTH_MAX_KEYS)]


def test_a_snapshot_is_strict_json():
    """Что бы ни было в отчёте, срез записывается без `NaN` и бесконечностей."""
    doc = {
        "meta": {"avgRule": float("nan")},
        "summary": {"avg": float("nan"), "full": float("inf"),
                    "depth": {"statusPct": float("-inf")}},
    }
    json.dumps(history.coverage_snapshot(doc), allow_nan=False)


# --- старые записи и непонятый файл ----------------------------------------


def test_old_history_entries_are_read_and_appended_to_as_written(путь):
    """`runs.json`, записанный 0.2.0, читается как есть: старые срезы без новых ключей
    не дополняются и не переписываются — их `avg: 0.0` при `endpoints: 0` остаётся
    нулём, а отсутствие `avgRule` не превращается в «1»: правило такой записи неизвестно."""
    старое = [
        {"id": 1, "coverage": {"avg": 0.0, "full": 0, "partial": 0, "empty": 0, "endpoints": 0}},
        {"id": 2, "coverage": {"avg": 62.5, "full": 1, "partial": 1, "empty": 1, "endpoints": 4}},
    ]
    artifacts.write(путь, history.KIND, {"runs": старое})
    assert history.read(путь) == старое
    новое = {"id": 3, "coverage": history.coverage_snapshot(образец("coverage-2.7.0.json"))}
    runs = history.append(путь, новое)
    assert runs == [*старое, новое]
    assert "avgRule" not in history.read(путь)[1]["coverage"]
    assert artifacts.validate(artifacts.read(путь)) == []


def test_the_history_is_written_as_strict_json(путь):
    """`NaN` в прежней записи или в новой — `null`: файл читает и не-Python читатель."""
    путь.write_text(
        '{"version": 2, "kind": "runner.history", "scope": {"system": "default", "env": '
        '"default"}, "collected_at": "x", "collector": "", "runs": [{"avg": NaN}]}',
        encoding="utf-8",
    )
    runs = history.append(путь, {"avg": float("inf"), "spread": [float("nan"), 1.0]})
    assert runs == [{"avg": None}, {"avg": None, "spread": [None, 1.0]}]
    текст = путь.read_text(encoding="utf-8")
    assert "NaN" not in текст and "Infinity" not in текст


def test_old_entries_are_opaque_to_the_package(путь):
    """Поля записей задаёт вызывающий (veha кладёт туда свои). Прежние записи не
    проходят через правила среза: ни белого списка ключей, ни проверки типов."""
    старое = [
        {
            "startedAt": 1,
            "suite": "full",
            "coverage": {"avg": "62", "endpoints": -1, "partialRun": True,
                         "optin": {"slow": {"state": "off"}}, "depthCells": 3},
            "своё": [1, {"x": None}, "строка"],
        }
    ]
    artifacts.write(путь, history.KIND, {"runs": старое})
    runs = history.append(путь, {"id": 2})
    assert runs == [*старое, {"id": 2}]
    assert history.read(путь) == runs


# --- непонятый файл не трогается -------------------------------------------
#
# Правило одно: файл, который пакет не понял, не переименовывается и не перезаписывается.
# Запись прогона пропускается, `UnreadableHistory` — до любого действия, возврат `[]`.
# В 0.2.0 такой файл молча заменялся историей из одной записи.


def _нетронут(путь, запись=None, match="запись прогона пропущена, файл не тронут"):
    до = путь.read_bytes()
    with pytest.warns(history.UnreadableHistory, match=match):
        assert history.append(путь, запись or {"id": 9}) == []
    assert путь.read_bytes() == до
    assert sorted(p.name for p in путь.parent.iterdir()) == [путь.name]


@pytest.mark.parametrize(
    "содержимое",
    [
        b"{not json",
        b"\xff\xfe\x00",
        b"[1, 2]",
        b'{"version": 2, "kind": "db.schema", "tables": []}',
        b'{"version": 3, "kind": "runner.history", "runs": [{"id": 1}]}',
        b'{"version": "2", "kind": "runner.history", "runs": [{"id": 1}]}',
        b'{"version": 2, "kind": "runner.history", "runs": {"id": 1}}',
        b'{"version": 2, "kind": "runner.history", "runs": [1, 2]}',
        b'{"version": 2, "kind": "runner.history", "runs": [{"x": "\\ud800"}]}',
        b'{"version": 2, "kind": "runner.history", "runs": [{"x": '
        + b"[" * 200000 + b"]" * 200000 + b"}]}",
    ],
    ids=["битый", "не-utf8", "список", "чужой-вид", "схема-новее",
         "версия-строкой", "runs-объект", "runs-не-записи", "суррогат-в-старой-записи",
         "глубокая-вложенность-в-старой-записи"],
)
def test_a_file_not_understood_is_left_alone(путь, содержимое):
    """Непонятый файл — не пустая история. Переписать его значит потерять прогоны, а
    исключение уронило бы прогон потребителя из-за файла, которого он не писал.
    Схема новее — файл, скорее всего, цел: тем более не трогаем."""
    путь.write_bytes(содержимое)
    _нетронут(путь)


def test_a_directory_under_the_name_is_left_alone(путь):
    путь.mkdir()
    (путь / "внутри").write_text("x", encoding="utf-8")
    with pytest.warns(history.UnreadableHistory, match="не обычный файл"):
        assert history.append(путь, {"id": 1}) == []
    assert (путь / "внутри").read_text(encoding="utf-8") == "x"


def test_a_file_above_the_size_limit_is_not_read_whole(путь, monkeypatch):
    """Читается не больше `MAX_BYTES` + 1 байт: больше — уже ясно, что файл не наш."""
    artifacts.write(путь, history.KIND, {"runs": [{"id": i} for i in range(50)]})
    до = путь.read_bytes()
    monkeypatch.setattr(history, "MAX_BYTES", 10)
    прочитано = []
    настоящий = history.os.read

    def считать(fd, n):
        chunk = настоящий(fd, n)
        прочитано.append(len(chunk))
        return chunk

    monkeypatch.setattr(history.os, "read", считать)
    with pytest.warns(history.UnreadableHistory, match="больше 10 байт"):
        assert history.append(путь, {"id": 2}) == []
    assert sum(прочитано) <= 11
    monkeypatch.undo()
    assert путь.read_bytes() == до


def test_the_size_limit():
    assert history.MAX_BYTES == 16 * 1024 * 1024


def test_an_empty_file_is_an_empty_history(путь):
    """0 байт — известный исход замены без fsync при обрыве питания; терять нечего."""
    путь.write_bytes(b"")
    with warnings.catch_warnings():
        warnings.simplefilter("error", history.UnreadableHistory)
        assert history.read(путь) == []
        assert history.append(путь, {"id": 1}) == [{"id": 1}]


def test_runs_null_is_an_empty_history(путь):
    artifacts.write(путь, history.KIND, {"runs": None})
    assert history.append(путь, {"id": 1}) == [{"id": 1}]


def test_the_package_never_writes_a_file_it_would_refuse(путь, monkeypatch):
    """Иначе пакет сам записал бы файл больше потолка — и потом навсегда отказывался бы
    его дописывать. Отбрасываются старейшие записи."""
    monkeypatch.setattr(history, "MAX_BYTES", 2000)
    with warnings.catch_warnings():
        warnings.simplefilter("error", history.UnreadableHistory)
        for i in range(40):
            runs = history.append(путь, {"id": i, "груз": "x" * 200})
            assert путь.stat().st_size <= history.MAX_BYTES
    assert [r["id"] for r in runs] == list(range(40 - len(runs), 40))
    assert 1 < len(runs) < 40
    assert history.read(путь) == runs


def test_one_entry_above_the_limit_is_the_callers_error(путь, monkeypatch):
    artifacts.write(путь, history.KIND, {"runs": [{"id": 1}]})
    до = путь.read_bytes()
    monkeypatch.setattr(history, "MAX_BYTES", 2000)
    with pytest.raises(ValueError, match="не укладывается"):
        history.append(путь, {"груз": "x" * 5000})
    assert путь.read_bytes() == до


def test_a_memory_error_while_parsing_is_a_skip(путь, monkeypatch):
    artifacts.write(путь, history.KIND, {"runs": [{"id": 1}]})
    до = путь.read_bytes()

    def мало(*a, **kw):
        raise MemoryError

    monkeypatch.setattr(history.json, "loads", мало)
    with pytest.warns(history.UnreadableHistory, match="не помещается в память"):
        assert history.append(путь, {"id": 2}) == []
    monkeypatch.undo()
    assert путь.read_bytes() == до


def test_read_applies_the_same_checks(путь):
    """`read` отдаёт `[]` для того же набора, что пропускает `append`, и `NaN` — `null`."""
    путь.write_bytes(b'{"version": 2, "kind": "runner.history", "runs": [{"x": "\\ud800"}]}')
    assert history.read(путь) == []
    путь.write_bytes(b'{"version": 2, "kind": "runner.history", "runs": [{"x": NaN}]}')
    assert history.read(путь) == [{"x": None}]


def test_the_warning_says_what_to_do(путь):
    путь.write_bytes(b"{broken")
    with pytest.warns(history.UnreadableHistory, match="переместите или удалите файл"):
        history.append(путь, {"id": 1})
    путь.write_bytes(b'{"version": 99, "kind": "runner.history", "runs": []}')
    with pytest.warns(history.UnreadableHistory, match="обновите partest-atlas"):
        history.append(путь, {"id": 1})


def test_a_deep_new_entry_is_the_callers_error(путь):
    глубоко = []
    for _ in range(100000):
        глубоко = [глубоко]
    with pytest.raises(ValueError, match="запись истории не сериализуется"):
        history.append(путь, {"x": глубоко})
    assert not путь.exists()


def test_a_limit_of_zero_erases_the_history(путь):
    """Оговорено, не исправлено в патче: `limit <= 0` пишет историю без записей."""
    artifacts.write(путь, history.KIND, {"runs": [{"id": 1}]})
    assert history.append(путь, {"id": 2}, limit=0) == []
    assert history.read(путь) == []


def test_a_file_that_cannot_be_read_is_left_alone(путь, monkeypatch):
    """Ошибка чтения — не довод, что файл плох: он может быть цел и просто занят."""
    artifacts.write(путь, history.KIND, {"runs": [{"id": 1}]})
    до = путь.read_bytes()

    def занят(fd, n):
        raise PermissionError("занят")

    monkeypatch.setattr(history.os, "read", занят)
    with pytest.warns(history.UnreadableHistory, match="не читается"):
        assert history.append(путь, {"id": 2}) == []
    monkeypatch.undo()
    assert путь.read_bytes() == до


def test_an_old_entry_that_cannot_be_written_back_leaves_the_file(путь, monkeypatch):
    """Сбой очистки или сериализации прежних записей — до записи, а не посреди неё."""
    artifacts.write(путь, history.KIND, {"runs": [{"старая": 1}]})
    настоящий = history._json_safe

    def глубоко(value):
        if isinstance(value, dict) and "старая" in value:
            raise RecursionError
        return настоящий(value)

    monkeypatch.setattr(history, "_json_safe", глубоко)
    _нетронут(путь, match="не записываются строгим JSON")


@pytest.mark.parametrize(
    "запись, ошибка",
    [
        ({"x": datetime(2026, 10, 25)}, TypeError),
        ({"x": {1, 2}}, TypeError),
        ({"x": object()}, TypeError),
        ({"x": "\ud800"}, ValueError),
    ],
    ids=["datetime", "set", "object", "суррогат"],
)
def test_a_new_entry_that_cannot_be_written_is_the_callers_error(путь, запись, ошибка):
    """Новая запись — данные вызывающего, а не файл: не сериализуется — его ошибка, и она
    поднимается, как в 0.2.0, а не прячется в предупреждение. До записи: файл цел."""
    artifacts.write(путь, history.KIND, {"runs": [{"id": 1}]})
    до = путь.read_bytes()
    with warnings.catch_warnings():
        warnings.simplefilter("error", history.UnreadableHistory)
        with pytest.raises(ошибка, match="запись истории не сериализуется"):
            history.append(путь, запись)
    assert путь.read_bytes() == до
    assert sorted(p.name for p in путь.parent.iterdir()) == [путь.name]


def test_the_warning_comes_before_anything_is_done(путь):
    """С `-W error` предупреждение становится исключением — и файл к этому моменту цел."""
    путь.write_bytes(b"{broken")
    with warnings.catch_warnings():
        warnings.simplefilter("error", history.UnreadableHistory)
        with pytest.raises(history.UnreadableHistory):
            history.append(путь, {"id": 1})
    assert путь.read_bytes() == b"{broken"
    assert sorted(p.name for p in путь.parent.iterdir()) == [путь.name]


def test_a_repeated_skip_is_not_swallowed_by_the_warning_filter(путь):
    """Фильтр `default` показывает одинаковое предупреждение с одного места один раз.
    В долгоживущем процессе второй пропуск иначе прошёл бы молча."""
    путь.write_bytes(b"{broken")
    with warnings.catch_warnings(record=True) as пойманные:
        warnings.simplefilter("default")
        for _ in range(3):
            history.append(путь, {"id": 1})
    assert len([w for w in пойманные if issubclass(w.category, history.UnreadableHistory)]) == 3


def test_a_history_without_runs_is_an_empty_history(путь):
    """Свой вид без `runs` — история, в которой ещё нет записей, а не непонятый файл."""
    artifacts.write(путь, history.KIND, {})
    assert history.append(путь, {"id": 1}) == [{"id": 1}]


@pytest.mark.parametrize("где", ["open", "fstat"])
def test_an_open_error_other_than_missing_is_not_a_missing_file(путь, monkeypatch, где):
    """«Нет файла» — только `FileNotFoundError`. Любая другая ошибка открытия или `fstat`
    не повод считать историю пустой и писать поверх."""
    artifacts.write(путь, history.KIND, {"runs": [{"id": 1}]})
    до = путь.read_bytes()

    def нельзя(*a, **kw):
        raise PermissionError("нет прав")

    monkeypatch.setattr(history.os, где, нельзя)
    with pytest.warns(history.UnreadableHistory, match="не читается"):
        assert history.append(путь, {"id": 2}) == []
    monkeypatch.undo()
    assert путь.read_bytes() == до


@pytest.mark.parametrize("версия", ["true", "false"])
def test_a_boolean_version_is_not_a_version(путь, версия):
    """`true` в Python — 1, но в JSON это не число: файл не понят, а не «версия 1»."""
    путь.write_bytes(
        b'{"version": ' + версия.encode() + b', "kind": "runner.history", "runs": [{"id": 1}]}'
    )
    _нетронут(путь, match="версией схемы не целым числом")


def test_a_failed_write_is_a_skip_not_an_exception(путь, monkeypatch):
    """Файл открыт другим процессом без права на удаление (Windows): `os.replace`
    отказывает. Прежний файл цел, временный убран, прогон потребителя не падает."""
    artifacts.write(путь, history.KIND, {"runs": [{"id": 1}]})
    до = путь.read_bytes()

    def занят(*a, **kw):
        raise PermissionError("занят другим процессом")

    monkeypatch.setattr(artifacts.os, "replace", занят)
    with pytest.warns(history.UnreadableHistory, match=r"не записан \(PermissionError\)"):
        assert history.append(путь, {"id": 2}) == []
    assert путь.read_bytes() == до
    assert sorted(p.name for p in путь.parent.iterdir()) == [путь.name]
