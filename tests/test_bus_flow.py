"""Поток шины: дельта смещений проверяется без брокера."""

from __future__ import annotations

import contextlib
import sys
import types

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import bus_flow
from partest_atlas.collect.bus_flow import ReaderNotConfigured, diff_offsets, topics_for


def test_only_topics_with_events_appear():
    before = {"а": {0: 10}, "б": {0: 5}}
    after = {"а": {0: 12}, "б": {0: 5}}
    assert [r["topic"] for r in diff_offsets(before, after)] == ["а"]


def test_events_are_summed_across_partitions():
    before = {"т": {0: 10, 1: 20, 2: 0}}
    after = {"т": {0: 12, 1: 25, 2: 0}}
    assert diff_offsets(before, after)[0]["events"] == 7


def test_a_shrinking_offset_counts_as_zero_not_negative():
    """Топик пересоздали или сработало хранение. Удаление старых сообщений — не
    отрицательный трафик."""
    assert diff_offsets({"т": {0: 100}}, {"т": {0: 10}}) == []


def test_a_shrinking_partition_does_not_hide_a_growing_one():
    """Считать сумму разностей со знаком значило бы вычесть удалённое из пришедшего."""
    before = {"т": {0: 100, 1: 0}}
    after = {"т": {0: 10, 1: 7}}
    assert diff_offsets(before, after)[0]["events"] == 7


def test_a_new_partition_is_counted_whole():
    """Раздела не было в первом снимке — значит всё, что в нём лежит, прошло за наше время."""
    assert diff_offsets({"т": {0: 5}}, {"т": {0: 5, 1: 9}})[0]["events"] == 9


def test_a_topic_absent_before_is_skipped():
    """У него нет «до»: записать его целиком значило бы приписать прогону весь топик."""
    assert diff_offsets({}, {"новый": {0: 10_000}}) == []


def test_topics_are_ordered():
    before = {"в": {0: 0}, "а": {0: 0}, "б": {0: 0}}
    after = {"в": {0: 1}, "а": {0: 1}, "б": {0: 1}}
    assert [r["topic"] for r in diff_offsets(before, after)] == ["а", "б", "в"]


def test_identity_and_scope_travel_with_the_record():
    row = diff_offsets({"т": {0: 0}}, {"т": {0: 1}}, test="проверка", nodeid="f::t", scope=bus_flow.TEST)[0]
    assert (row["test"], row["nodeid"], row["scope"]) == ("проверка", "f::t", bus_flow.TEST)


# --- отбор топиков ---------------------------------------------------------


def test_without_masks_everything_available_is_taken():
    assert topics_for(["б", "а"]) == ["а", "б"]


def test_masks_keep_other_systems_out():
    """На общем брокере лежат чужие топики, и снять их значит записать чужой трафик
    под своим именем."""
    # Порядок отсортированный, как у остальных наблюдений: два одинаковых прогона
    # обязаны давать одинаковый артефакт.
    assert topics_for(["наш.один", "наш.два", "чужой.три"], ["наш.*"]) == ["наш.два", "наш.один"]


def test_masks_that_match_nothing_give_nothing():
    assert topics_for(["а", "б"], ["нет.*"]) == []


# --- читатель приходит из конфигурации -------------------------------------


def test_no_reader_configured_is_not_an_error():
    """Проект, который шину не настраивал, — законное состояние, а не поломка."""
    assert bus_flow.open_reader({}) is None


def test_a_malformed_reader_is_refused_by_name():
    with pytest.raises(ReaderNotConfigured, match="модуль:функция"):
        bus_flow.open_reader({"reader": "простоимя"})


def test_an_unimportable_reader_says_so():
    with pytest.raises(ReaderNotConfigured, match="не импортируется"):
        bus_flow.open_reader({"reader": "нет_модуля:сессия"})


def _модуль(monkeypatch, *, настроен=True):
    m = types.ModuleType("шина_проекта")

    @contextlib.contextmanager
    def сессия():
        yield "читатель"

    m.сессия = сессия
    m.настроена = lambda: настроен
    monkeypatch.setitem(sys.modules, "шина_проекта", m)
    return m


def test_the_project_reader_named_in_config_is_used(monkeypatch):
    _модуль(monkeypatch)
    with bus_flow.open_reader({"reader": "шина_проекта:сессия"}) as r:
        assert r == "читатель"


def test_the_guard_tells_not_configured_from_configured_wrong(monkeypatch):
    """«Проект не настраивал шину» — не ошибка. «Настроил неверно» — ошибка."""
    _модуль(monkeypatch, настроен=False)
    assert bus_flow.open_reader({"reader": "шина_проекта:сессия", "configured": "настроена"}) is None


def test_a_missing_guard_function_is_an_error(monkeypatch):
    _модуль(monkeypatch)
    with pytest.raises(ReaderNotConfigured, match="такой функции нет"):
        bus_flow.open_reader({"reader": "шина_проекта:сессия", "configured": "нет_такой"})


def test_misconfiguration_is_its_own_class():
    assert issubclass(ReaderNotConfigured, RuntimeError)


# --- обвязка ---------------------------------------------------------------


class ПоддельныйЧитатель:
    def __init__(self, снимки, топики=("т",)):
        self.снимки = list(снимки)
        self.топики = list(топики)

    def list_topics(self):
        return list(self.топики)

    def end_offsets(self, topic):
        return (self.снимки[0] if len(self.снимки) == 1 else self.снимки.pop(0)).get(topic, {})


def test_topics_are_discovered_when_not_given():
    r = ПоддельныйЧитатель([{"т": {0: 0}}, {"т": {0: 3}}], топики=["т"])
    flow = bus_flow.open_flow(r)
    assert flow.topics == ["т"]
    assert flow.finish()[0]["events"] == 3


def test_no_settle_no_sleep(monkeypatch):
    спал = []
    monkeypatch.setattr(bus_flow.time, "sleep", lambda s: спал.append(s))
    r = ПоддельныйЧитатель([{"т": {0: 0}}, {"т": {0: 1}}])
    bus_flow.open_flow(r, ["т"]).finish()
    assert спал == []


def test_settle_is_applied_when_asked(monkeypatch):
    спал = []
    monkeypatch.setattr(bus_flow.time, "sleep", lambda s: спал.append(s))
    r = ПоддельныйЧитатель([{"т": {0: 0}}, {"т": {0: 1}}])
    bus_flow.open_flow(r, ["т"], settle_sec=2.0).finish()
    assert спал == [2.0]


def test_the_artifact_it_writes_validates_clean(tmp_path):
    rows = diff_offsets({"т": {0: 0}}, {"т": {0: 2}})
    path = tmp_path / "bus_flow.json"
    artifacts.write(path, bus_flow.KIND, {"records": rows}, collector="partest-atlas/bus_flow")
    assert artifacts.validate(artifacts.read(path)) == []


def test_the_kind_is_one_the_contract_knows():
    assert bus_flow.KIND in artifacts.KINDS
    assert artifacts.filename(bus_flow.KIND) == "bus_flow.json"


# --- вывод про потребителя -------------------------------------------------


def test_without_the_clock_the_record_stays_as_it_was():
    """Не было записей — нет и вывода. Пустой `afterWrite` читался бы как «нет», то
    есть как утверждение, которого мы не делали."""
    row = diff_offsets({"т": {0: 0}}, {"т": {0: 1}})
    assert "afterWrite" not in row[0]
    assert "http" not in row[0]


def test_after_write_is_true_once_the_clock_has_seen_any_write():
    """Фактический смысл поля: часы видели хоть одну пишущую запись с `install()` или
    последнего `reset()`. Времени события здесь нет, и о потребителе поле не говорит."""
    row = diff_offsets({"т": {0: 0}}, {"т": {0: 1}}, last_write=1.0, counters={"requests": 3, "writes": 1})[0]
    assert row["afterWrite"] is True
    assert row["http"] == {"requests": 3, "writes": 1}


def test_counters_travel_with_the_inference():
    """Признак не доказательство: на общем стенде писать могла чужая сессия, и по
    числу запросов видно, насколько выводу можно верить."""
    row = diff_offsets({"т": {0: 0}}, {"т": {0: 1}}, counters={"requests": 0, "writes": 0})[0]
    assert row["afterWrite"] is False
    assert row["http"] == {"requests": 0, "writes": 0}


def test_the_clock_is_read_before_the_settle(monkeypatch):
    """Запросы, пришедшие во время выдержки, к окну уже не относятся."""
    порядок = []
    monkeypatch.setattr(bus_flow.http_clock, "last_write", lambda: порядок.append("часы") or 1.0)
    monkeypatch.setattr(bus_flow.time, "sleep", lambda s: порядок.append("сон"))
    r = ПоддельныйЧитатель([{"т": {0: 0}}, {"т": {0: 1}}])
    bus_flow.open_flow(r, ["т"], settle_sec=1.0).finish()
    assert порядок == ["часы", "сон"]
