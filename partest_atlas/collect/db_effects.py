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
from typing import Any, Dict, List, Mapping, Optional, Sequence

KIND = "db.effects"

STATS_SQL = """
SELECT relname AS table_name,
       n_tup_ins AS ins, n_tup_upd AS upd, n_tup_del AS del
  FROM pg_stat_user_tables
 WHERE schemaname = ANY(%(schemas)s)
"""

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
) -> List[Dict[str, Any]]:
    """Дельта двух снимков счётчиков. Таблицы без изменений не попадают в вывод.

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
        out.append(
            {
                "test": test,
                "nodeid": nodeid,
                "table": table,
                "insert": inserted,
                "update": updated,
                "delete": deleted,
                "shared": shared,
                "scope": scope,
            }
        )
    return out


@dataclass
class DbEffects:
    """Снимает дельту статистики таблиц вокруг куска работы."""

    conn: Any
    schemas: List[str] = field(default_factory=lambda: ["public"])
    settle_sec: float = 0.0
    scope: str = SESSION
    _before: Dict[str, dict] = field(default_factory=dict)
    _sessions_before: int = 0

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
        """
        self._rollback()
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute(STATS_SQL, {"schemas": self.schemas})
            rows = {r["table_name"]: r for r in cur.fetchall()}
        self._rollback()
        return rows

    def active_sessions(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(ACTIVE_SESSIONS_SQL)
            row = cur.fetchone()
        self._rollback()
        return int((row or {}).get("n") or 0)

    def start(self) -> "DbEffects":
        self._before = self.snapshot()
        self._sessions_before = self.active_sessions()
        return self

    def finish(self, *, test: str = "", nodeid: str = "") -> List[Dict[str, Any]]:
        if self.settle_sec:
            # Выдержка безусловная: проверка «изменилось ли хоть что-нибудь» бесполезна —
            # на общем стенде посторонняя активность меняет какую-нибудь таблицу почти
            # всегда, и снимок брался бы раньше, чем долетал счётчик нужной.
            time.sleep(self.settle_sec)
        after = self.snapshot()
        shared = max(self._sessions_before, self.active_sessions()) > 0
        return diff(
            self._before, after, test=test, nodeid=nodeid, shared=shared, scope=self.scope
        )


def open_effects(
    conn: Any,
    schemas: Optional[Sequence[str]] = None,
    *,
    scope: str = SESSION,
    settle_sec: float = 0.0,
) -> DbEffects:
    """Открыть замер: снимок «до» берётся сразу."""
    return DbEffects(
        conn=conn,
        schemas=list(schemas or ["public"]),
        scope=scope,
        settle_sec=settle_sec,
    ).start()
