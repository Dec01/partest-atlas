"""Что прогон сделал с базой → артефакт `db.effects`. Строго на чтение.

Точный аудит записей потребовал бы триггеров или логической репликации — на общий стенд
такое не ставят. Берём то, что PostgreSQL считает сам: `pg_stat_user_tables` копит
вставки, обновления и удаления по таблицам.

**Ограничение, из-за которого сборщик устроен именно так.** Счётчики пишет не
наблюдатель, а тот бэкенд, который выполнил запрос, и публикует он их в общую память не
сразу, а пачками. Приложение держит пул долгоживущих соединений, поэтому запись теста
доезжает до статистики с задержкой: замер на живом стенде дал **около десяти секунд**.
Ждать столько после каждого теста дороже самого прогона.

Отсюда два режима:

``session``
    Один снимок в начале прогона, один в конце. Точно и почти бесплатно, но отвечает на
    вопрос «какие таблицы затронул прогон», а не «какой тест что записал».

``test``
    Снимок вокруг каждого теста с выдержкой. Имеет смысл только при точечном запуске
    нескольких тестов: на полном прогоне выдержка превращается в часы.

Стенд бывает общий, и посторонняя активность попадёт в дельту тоже. Такие записи
помечаются ``shared: true`` — не выбрасываются: наблюдение с оговоркой полезнее
отсутствующего, а молчаливое — опаснее обоих.

Как и у схемы, **вычисление отделено от опроса**: :func:`diff` — чистая функция над двумя
снимками, и вся тонкость дельты проверяется обычным тестом без базы.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from partest_atlas.adapters.postgres import read_only

KIND = "db.effects"

#: Схема — в выборке и в ключе снимка: одноимённые таблицы двух схем иначе затирали бы
#: друг друга, и в снимках «до» и «после» могла победить разная — дельта из чужих счётчиков.
STATS_SQL = """
SELECT schemaname AS table_schema, relname AS table_name,
       n_tup_ins AS ins, n_tup_upd AS upd, n_tup_del AS del
  FROM pg_stat_user_tables
 WHERE schemaname = ANY(%(schemas)s)
 ORDER BY schemaname, relname
"""


def snapshot_key(row: Mapping[str, Any]) -> str:
    """Ключ таблицы в снимке: `схема.имя`; строка без схемы (снятая по-старому) — имя."""
    schema = row.get("table_schema")
    return f"{schema}.{row['table_name']}" if schema else str(row["table_name"])

ACTIVE_SESSIONS_SQL = """
SELECT count(*) AS n FROM pg_stat_activity
 WHERE state = 'active' AND pid <> pg_backend_pid()
"""

#: Замер на живом стенде: публикация статистики пишущего бэкенда доходит до наблюдателя
#: примерно за десять секунд. Значение по умолчанию — с запасом.
DEFAULT_SETTLE_SEC = 13.0

SESSION = "session"
TEST = "test"


def diff(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    *,
    test: str = "",
    nodeid: str = "",
    shared: bool = False,
    scope: str = SESSION,
    run: Optional[Mapping[str, Any]] = None,
    phase: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Дельта двух снимков счётчиков. Таблицы без изменений не попадают в вывод.

    *run* — метка прогона (:func:`partest_atlas.artifacts.run_label`), пишется ключом
    ``run``; *phase* — фаза окна теста (``setup``/``call``/``teardown``), ключом
    ``phase``. Не переданы — ключей нет, запись как раньше.

    Таблица, которой не было в первом снимке, **пропускается**: у неё нет «до», и
    разность считать не от чего. Записать её целиком было бы неверно — счётчики там
    накоплены за всё время жизни таблицы, а не за наш прогон, и такая строка выглядела
    бы как гигантский эффект теста.
    """
    out: List[Dict[str, Any]] = []
    for table in sorted(after):
        was = before.get(table)
        if not was:
            continue
        now = after[table]
        inserted = int(now["ins"]) - int(was["ins"])
        updated = int(now["upd"]) - int(was["upd"])
        deleted = int(now["del"]) - int(was["del"])
        if not (inserted or updated or deleted):
            continue
        record: Dict[str, Any] = {
            "test": test,
            "nodeid": nodeid,
            "table": str(now.get("table_name") or table),
            "insert": inserted,
            "update": updated,
            "delete": deleted,
            "shared": shared,
            "scope": scope,
        }
        if now.get("table_schema"):
            record["schema"] = now["table_schema"]
        if run is not None:
            record["run"] = dict(run)
        if phase is not None:
            record["phase"] = phase
        out.append(record)
    return out


@dataclass
class DbEffects:
    """Снимает дельту статистики таблиц вокруг куска работы."""

    conn: Any
    schemas: List[str] = field(default_factory=lambda: ["public"])
    settle_sec: float = 0.0
    scope: str = SESSION
    run: Optional[Dict[str, Any]] = None
    _before: Dict[str, dict] = field(default_factory=dict)
    _sessions_before: int = 0
    _last: Dict[str, dict] = field(default_factory=dict)
    _phased: List[Dict[str, Any]] = field(default_factory=list)

    def _rollback(self) -> None:
        try:
            self.conn.rollback()
        except Exception:
            pass

    def snapshot(self) -> Dict[str, dict]:
        """Свежий снимок счётчиков.

        PostgreSQL кэширует статистику на время транзакции: два запроса подряд в одной
        транзакции вернут одно и то же, и дельта выходила нулевой. Поэтому закрываем
        открытую транзакцию и явно сбрасываем кэш снимка.

        Каждый снимок — в своей транзакции только на чтение
        (:func:`~partest_atlas.adapters.postgres.read_only`): запрет записи держит база.
        """
        self._rollback()
        with read_only(self.conn), self.conn.cursor() as cur:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute(STATS_SQL, {"schemas": self.schemas})
            rows = {snapshot_key(r): r for r in cur.fetchall()}
        self._rollback()
        return rows

    def active_sessions(self) -> int:
        with read_only(self.conn), self.conn.cursor() as cur:
            cur.execute(ACTIVE_SESSIONS_SQL)
            row = cur.fetchone()
        self._rollback()
        return int((row or {}).get("n") or 0)

    def start(self) -> "DbEffects":
        self._before = self.snapshot()
        self._last = self._before
        self._sessions_before = self.active_sessions()
        return self

    @property
    def tables(self) -> List[str]:
        """Таблицы, которые видел снимок «до». Пусто — схемы выбраны не те."""
        return sorted(self._before)

    def _settled_snapshot(self) -> Tuple[Dict[str, dict], bool]:
        if self.settle_sec:
            # Выдержка безусловная: проверка «изменилось ли хоть что-нибудь» бесполезна —
            # на общем стенде посторонняя активность меняет какую-нибудь таблицу почти
            # всегда, и снимок брался бы раньше, чем долетал счётчик нужной.
            time.sleep(self.settle_sec)
        after = self.snapshot()
        return after, max(self._sessions_before, self.active_sessions()) > 0

    def drop_phases(self) -> None:
        """Забыть дельты фаз: окно закончится одной дельтой от снимка «до»."""
        self._phased = []

    def checkpoint(self, phase: str, *, test: str = "", nodeid: str = "") -> None:
        """Граница фазы окна: выдержка, снимок и дельта фазы с прошлой границы.

        Записи копятся и отдаются :meth:`finish` вместе с последней фазой.
        """
        after, shared = self._settled_snapshot()
        self._phased.extend(diff(
            self._last, after, test=test, nodeid=nodeid, shared=shared, scope=self.scope,
            run=self.run, phase=phase,
        ))
        self._last = after

    def finish(
        self, *, test: str = "", nodeid: str = "", phase: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Последний снимок и дельта. С *phase* — дельта последней фазы и все накопленные
        :meth:`checkpoint`; без — одна дельта на всё окно, как раньше."""
        if phase is not None:
            self.checkpoint(phase, test=test, nodeid=nodeid)
            out, self._phased = self._phased, []
            return out
        after, shared = self._settled_snapshot()
        return diff(
            self._before, after, test=test, nodeid=nodeid, shared=shared, scope=self.scope,
            run=self.run,
        )


def open_effects(
    conn: Any,
    schemas: Optional[Sequence[str]] = None,
    *,
    scope: str = SESSION,
    settle_sec: float = 0.0,
    run: Optional[Mapping[str, Any]] = None,
) -> DbEffects:
    """Открыть замер: снимок «до» берётся сразу. *run* — метка прогона в каждой записи."""
    return DbEffects(
        conn=conn,
        schemas=list(schemas or ["public"]),
        scope=scope,
        settle_sec=settle_sec,
        run=dict(run) if run is not None else None,
    ).start()
