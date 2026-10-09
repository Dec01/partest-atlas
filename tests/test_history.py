"""История прогонов: накапливается, ограничена, и старое теряется раньше нового."""

from __future__ import annotations

import json

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
    """Без проверки вида чужой артефакт под этим именем был бы молча дописан."""
    artifacts.write(путь, "db.schema", {"runs": [{"чужое": True}]})
    assert history.read(путь) == []
    runs = history.append(путь, {"id": 1})
    assert runs == [{"id": 1}]
    assert artifacts.read(путь)["kind"] == history.KIND


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
    }


def test_a_missing_key_is_omitted_not_nulled():
    """Артефакты переживают выпуски. Требовать от прошлогоднего отчёта сегодняшних
    полей значит развалить тренд ровно там, где он и нужен."""
    assert history.coverage_snapshot({"summary": {"avg": 1.0}}) == {"avg": 1.0}


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
    assert history.coverage_snapshot(new) == history.coverage_snapshot(old) == summary


def test_no_report_is_an_empty_snapshot():
    assert history.coverage_snapshot(None) == {}
    assert history.coverage_snapshot({}) == {}


def test_the_kind_is_one_the_contract_knows():
    assert history.KIND in artifacts.KINDS
    assert artifacts.filename(history.KIND) == "runs.json"
