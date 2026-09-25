"""Дельта счётчиков: вся тонкость проверяется без базы.

Тонкости здесь не декоративные — каждая соответствует тому, как ведут себя счётчики
PostgreSQL на общем стенде, и каждую легко потерять при правке.
"""

from __future__ import annotations

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import db_effects
from partest_atlas.collect.db_effects import diff


def snap(**tables):
    """`snap(users=(10, 0, 0))` — вставок, обновлений, удалений."""
    return {
        name: {"table_name": name, "ins": i, "upd": u, "del": d}
        for name, (i, u, d) in tables.items()
    }


def test_only_changed_tables_appear():
    before = snap(users=(10, 0, 0), orders=(5, 0, 0))
    after = snap(users=(12, 0, 0), orders=(5, 0, 0))
    assert [r["table"] for r in diff(before, after)] == ["users"]


def test_the_delta_is_a_difference_not_the_new_value():
    before = snap(users=(10, 3, 1))
    after = snap(users=(12, 5, 4))
    row = diff(before, after)[0]
    assert (row["insert"], row["update"], row["delete"]) == (2, 2, 3)


def test_a_table_absent_before_is_skipped():
    """У неё нет «до». Счётчики там накоплены за всю жизнь таблицы, и записать их
    целиком значило бы показать гигантский эффект теста, которого не было."""
    assert diff(snap(), snap(созданная=(1000, 0, 0))) == []


def test_a_table_that_disappeared_is_simply_absent():
    """Таблицу удалили во время прогона — дельты по ней нет и быть не может."""
    assert diff(snap(users=(10, 0, 0)), snap()) == []


def test_nothing_changed_gives_an_empty_list_not_zero_rows():
    """Пустой список и строки с нулями — разные утверждения. Вторые засоряют артефакт
    и делают «прогон ничего не трогал» неотличимым от «прогон трогал всё на ноль»."""
    assert diff(snap(users=(10, 2, 1)), snap(users=(10, 2, 1))) == []


def test_rows_are_ordered_by_table_name():
    """Порядок задан, иначе два одинаковых прогона дают разные артефакты и сравнение
    между прогонами превращается в шум."""
    before = snap(б=(0, 0, 0), а=(0, 0, 0), в=(0, 0, 0))
    after = snap(б=(1, 0, 0), а=(1, 0, 0), в=(1, 0, 0))
    assert [r["table"] for r in diff(before, after)] == ["а", "б", "в"]


def test_the_shared_flag_travels_with_the_record():
    """Наблюдение с оговоркой полезнее отсутствующего, а молчаливое — опаснее обоих."""
    rows = diff(snap(t=(0, 0, 0)), snap(t=(1, 0, 0)), shared=True)
    assert rows[0]["shared"] is True


def test_test_identity_travels_with_the_record():
    rows = diff(
        snap(t=(0, 0, 0)), snap(t=(1, 0, 0)), test="проверка", nodeid="файл.py::проверка"
    )
    assert rows[0]["test"] == "проверка"
    assert rows[0]["nodeid"] == "файл.py::проверка"


def test_scope_says_what_the_delta_is_attributed_to():
    """`session` отвечает «какие таблицы затронул прогон», `test` — «какой тест что
    записал». Не отличать их значит приписать тесту чужую работу."""
    assert diff(snap(t=(0, 0, 0)), snap(t=(1, 0, 0)))[0]["scope"] == db_effects.SESSION
    assert (
        diff(snap(t=(0, 0, 0)), snap(t=(1, 0, 0)), scope=db_effects.TEST)[0]["scope"]
        == db_effects.TEST
    )


def test_a_negative_delta_is_reported_not_hidden():
    """Счётчики сбрасывает `pg_stat_reset()` и рестарт базы. Отрицательная дельта —
    признак того, что между снимками произошло такое, и прятать её нельзя: иначе
    прогон выглядел бы как не трогавший ничего."""
    rows = diff(snap(t=(100, 0, 0)), snap(t=(5, 0, 0)))
    assert rows[0]["insert"] == -95


# --- обвязка вокруг чистой функции ----------------------------------------


class ПоддельноеСоединение:
    """Ровно столько, сколько трогает сборщик: курсор, выборки, откат."""

    def __init__(self, снимки, сессий=0):
        self.снимки = list(снимки)
        self.сессий = сессий
        self.откатов = 0
        self.сброшено = 0

    def rollback(self):
        self.откатов += 1

    def cursor(self):
        conn = self

        class Курсор:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def execute(self_, sql, params=None):
                if "pg_stat_clear_snapshot" in sql:
                    conn.сброшено += 1
                    self_.строки = []
                elif "pg_stat_activity" in sql:
                    self_.строки = [{"n": conn.сессий}]
                else:
                    self_.строки = list((conn.снимки.pop(0) if conn.снимки else {}).values())

            def fetchall(self_):
                return self_.строки

            def fetchone(self_):
                return self_.строки[0] if self_.строки else None

        return Курсор()


def test_the_snapshot_cache_is_cleared_before_every_read():
    """Без сброса два запроса в одной транзакции вернут одно и то же, и дельта выйдет
    нулевой — то есть сборщик будет уверенно сообщать, что прогон ничего не делал."""
    conn = ПоддельноеСоединение([snap(t=(0, 0, 0)), snap(t=(3, 0, 0))])
    effects = db_effects.open_effects(conn, ["public"])
    effects.finish()
    assert conn.сброшено == 2


def test_a_run_against_a_quiet_stand_is_not_marked_shared():
    conn = ПоддельноеСоединение([snap(t=(0, 0, 0)), snap(t=(1, 0, 0))], сессий=0)
    rows = db_effects.open_effects(conn, ["public"]).finish()
    assert rows[0]["shared"] is False


def test_a_busy_stand_marks_every_record_shared():
    conn = ПоддельноеСоединение([snap(t=(0, 0, 0)), snap(t=(1, 0, 0))], сессий=2)
    rows = db_effects.open_effects(conn, ["public"]).finish()
    assert rows[0]["shared"] is True


def test_no_settle_no_sleep(monkeypatch):
    """Выдержка по умолчанию нулевая: режим `session` не должен стоить прогону секунд."""
    спал = []
    monkeypatch.setattr(db_effects.time, "sleep", lambda s: спал.append(s))
    conn = ПоддельноеСоединение([snap(t=(0, 0, 0)), snap(t=(1, 0, 0))])
    db_effects.open_effects(conn, ["public"]).finish()
    assert спал == []


def test_settle_is_unconditional(monkeypatch):
    """Именно безусловная: проверять «изменилось ли что-нибудь» бесполезно — на общем
    стенде меняется почти всегда, а счётчик нужной таблицы ещё не долетел."""
    спал = []
    monkeypatch.setattr(db_effects.time, "sleep", lambda s: спал.append(s))
    conn = ПоддельноеСоединение([snap(t=(0, 0, 0)), snap(t=(0, 0, 0))])
    db_effects.open_effects(conn, ["public"], settle_sec=1.5).finish()
    assert спал == [1.5]


def test_the_artifact_it_writes_validates_clean(tmp_path):
    rows = diff(snap(t=(0, 0, 0)), snap(t=(2, 1, 0)))
    path = tmp_path / "db_effects.json"
    artifacts.write(path, db_effects.KIND, {"records": rows}, collector="partest-atlas/db_effects")
    assert artifacts.validate(artifacts.read(path)) == []


def test_the_kind_is_one_the_contract_knows():
    assert db_effects.KIND in artifacts.KINDS
    assert artifacts.filename(db_effects.KIND) == "db_effects.json"
