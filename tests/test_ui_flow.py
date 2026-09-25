"""Связи «интерфейс → API»: привязка и нормализация, без браузера."""

from __future__ import annotations

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import ui_flow
from partest_atlas.collect.ui_flow import attribute, build, merge, norm_path, norm_testid


def действие(ts, control="кнопка", screen="экран", type_="click"):
    return {"ts": ts, "control": control, "screen": screen, "type": type_}


def запрос(ts, url="/api/v1/items", method="GET", status=200):
    return {"ts": ts, "url": url, "method": method, "status": status}


# --- нормализация ----------------------------------------------------------


@pytest.mark.parametrize(
    ("сырой", "ожидалось"),
    [
        ("planning-task-row-32142", "planning-task-row-{id}"),
        ("row-42-select", "row-{id}-select"),
        ("date-range-day-2026-09-04", "date-range-day-{date}"),
        ("простой-testid", "простой-testid"),
        (None, None),
        ("", ""),
    ],
)
def test_dynamic_tails_are_collapsed(сырой, ожидалось):
    """Без схлопывания каждая строка таблицы каждого прогона — отдельный узел карты."""
    assert norm_testid(сырой) == ожидалось


def test_the_date_is_collapsed_before_the_identifier():
    """Иначе `day-2026-09-04` стало бы `day-2026-09-{id}` — дата развалилась бы."""
    assert norm_testid("day-2026-09-04") == "day-{date}"


@pytest.mark.parametrize(
    ("url", "ожидалось"),
    [
        ("/api/v1/clients/42?x=1", "/api/v1/clients/{id}"),
        ("https://х.invalid/api/v1/a/7/b", "/api/v1/a/{id}/b"),
        ("/api/v1/x/0f8fad5b-d9cb-469f-a165-70867728950e", "/api/v1/x/{id}"),
        ("/api/v1/items/", "/api/v1/items"),
    ],
)
def test_paths_are_normalised(url, ожидалось):
    assert norm_path(url) == ожидалось


# --- привязка --------------------------------------------------------------


def test_a_call_goes_to_the_last_action_before_it():
    links = attribute([действие(0, "первая"), действие(100, "вторая")], [запрос(150)])
    assert links[0]["control"] == "вторая"


def test_a_call_before_any_action_is_dropped():
    """Приписать его первому попавшемуся действию значило бы выдумать связь."""
    assert attribute([действие(100)], [запрос(50)]) == []


def test_a_call_outside_the_window_is_dropped():
    assert attribute([действие(0)], [запрос(9999)], window_ms=2500) == []


def test_a_prompt_call_is_direct_and_a_late_one_is_window():
    """Два значения, а не флаг: «сразу» и «в окне» — разные утверждения."""
    сразу = attribute([действие(0)], [запрос(100)])[0]
    поздно = attribute([действие(0)], [запрос(2000)])[0]
    assert сразу["confidence"] == "direct"
    assert поздно["confidence"] == "window"


def test_the_gap_is_reported_so_the_reader_can_judge():
    assert attribute([действие(0)], [запрос(1234)])[0]["gapMs"] == 1234


def test_ignored_urls_never_become_links():
    links = attribute([действие(0)], [запрос(10, "/static/main.js"), запрос(20)], ignore_url=["/static/"])
    assert [l["path"] for l in links] == ["/api/v1/items"]


def test_ignored_controls_do_not_attract_calls():
    """Клик по строке таблицы тащит в карту подписи с засеянными данными. Что считать
    шумом, знает проект — список приходит из конфигурации."""
    links = attribute(
        [действие(0, "AQA UI Graph 9510895e"), действие(10, "кнопка")],
        [запрос(20)],
        ignore_control=["AQA "],
    )
    assert links[0]["control"] == "кнопка"


def test_the_control_is_normalised_in_the_link():
    assert attribute([действие(0, "row-42")], [запрос(10)])[0]["control"] == "row-{id}"


def test_actions_out_of_order_are_sorted_first():
    """Источник событий не обязан быть упорядоченным."""
    links = attribute([действие(100, "поздняя"), действие(0, "ранняя")], [запрос(110)])
    assert links[0]["control"] == "поздняя"


# --- схлопывание -----------------------------------------------------------


def test_repeats_are_collapsed_with_a_counter():
    links = attribute([действие(0)], [запрос(10), запрос(20)])
    сведено = merge(links)
    assert len(сведено) == 1
    assert сведено[0]["count"] == 2


def test_the_worst_confidence_wins():
    """Иначе одно удачное наблюдение повысило бы доверие к связи, которая обычно
    угадывается по окну."""
    links = attribute([действие(0)], [запрос(100), запрос(2000)])
    assert merge(links)[0]["confidence"] == "window"


def test_different_endpoints_stay_apart():
    links = attribute([действие(0)], [запрос(10, "/api/v1/a"), запрос(20, "/api/v1/b")])
    assert len(merge(links)) == 2


# --- содержание артефакта --------------------------------------------------


def test_stats_agree_with_the_links_under_them():
    links = attribute([действие(0)], [запрос(10, "/api/v1/a"), запрос(2000, "/api/v1/b")])
    doc = build(links)
    assert doc["stats"]["links"] == len(doc["links"]) == 2
    assert doc["stats"]["endpoints"] == 2
    assert doc["stats"]["screens"] == 1
    assert doc["stats"]["direct"] == 1


def test_the_artifact_it_writes_validates_clean(tmp_path):
    doc = build(attribute([действие(0)], [запрос(10)]))
    path = tmp_path / "ui_flow.json"
    artifacts.write(path, ui_flow.KIND, doc, collector="partest-atlas/ui_flow")
    assert artifacts.validate(artifacts.read(path)) == []


def test_the_kind_is_one_the_contract_knows():
    assert ui_flow.KIND in artifacts.KINDS
    assert artifacts.filename(ui_flow.KIND) == "ui_flow.json"
