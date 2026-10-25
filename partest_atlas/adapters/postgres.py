"""Соединение с PostgreSQL так, как назвал его проект.

Пакет не знает, как у проекта устроен доступ к базе, и знать не должен: там бывают
бастионы, туннели, чужие менеджеры секретов. Поэтому соединение **называется в
`atlas.yaml`**, а не выбирается кодом.

В прототипе путь к функции соединения конкретного проекта был вписан в сборщик
константой. Это делало перенос на второй проект правкой пакета — ровно то, что пакет
запрещает.

**Только чтение — механизмом, а не соглашением.** Запросы сборщиков написаны как
`SELECT`, но обещание «мы пишем только `SELECT`» держится на внимательности того, кто
правит код. :func:`read_only` переводит транзакцию в режим только чтения средствами самой
базы и проверяет, что режим включился: запись в такой транзакции PostgreSQL отклонит сам,
что бы ни оказалось в запросе.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any, Iterator, List, Mapping, Optional

from partest_atlas.config import env
from partest_atlas.lang import t

#: Значение `database.schemas`, при котором берутся все несистемные схемы.
AUTO_SCHEMAS = "auto"

#: Схемы, в которых есть хоть одна таблица или представление, кроме системных. Системные —
#: `pg_catalog`, `information_schema` и всё на `pg_` (`pg_toast`, временные схемы).
#: `left(…)`, а не `LIKE 'pg\_%'`: ни экранирования, ни знака `%`, который драйвер с
#: параметрами принял бы за подстановку.
SCHEMAS_SQL = """
SELECT DISTINCT table_schema AS schema_name
  FROM information_schema.tables
 WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
   AND left(table_schema, 3) <> 'pg_'
 ORDER BY table_schema
"""

#: Открыть блок транзакции только на чтение. Если драйвер работает в autocommit, это и
#: есть блок, в котором пойдут запросы. Если драйвер уже открыл транзакцию сам (обычный
#: режим psycopg), PostgreSQL отвечает предупреждением и ничего не меняет — тогда режим
#: выставляет следующая строка.
BEGIN_READ_ONLY_SQL = "BEGIN TRANSACTION READ ONLY"

#: Перевести текущую транзакцию в режим только чтения — кто бы её ни открыл. Только
#: уровень транзакции, не сессии: настройка сессии пережила бы нас на соединении, которое
#: проект отдал из своего пула, и его следующая запись упала бы на чужом запрете.
SET_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"

#: Проверка, что режим включился, — у базы, а не по нашему ожиданию.
CHECK_READ_ONLY_SQL = "SHOW transaction_read_only"

#: Закрыть блок. Вне транзакции PostgreSQL отвечает предупреждением, не ошибкой.
END_SQL = "ROLLBACK"


class ConnectionNotConfigured(RuntimeError):
    """Соединение не описано либо описано неверно.

    Отдельный класс, потому что вызывающему нужно отличать **это** от «база недоступна».
    Первое чинится в `atlas.yaml`, второе — на стенде, и одинаковое сообщение про них
    стоило одному проекту полдня поисков.
    """


class NotReadOnly(RuntimeError):
    """База не подтвердила режим только чтения — опрашивать её сборщик не станет."""


@contextmanager
def connect(section: Mapping[str, Any]) -> Iterator[Any]:
    """Соединение по разделу `database` конфигурации.

    Два пути, в порядке предпочтения:

    ``connect: "модуль:функция"``
        У проекта своя функция-контекстменеджер. Она знает про туннели, про права и про
        всё прочее, чего пакету знать не положено.

    ``dsn_env: ИМЯ``
        Строка подключения в переменной окружения; psycopg поднимаем сами. Имя, а не
        значение: `atlas.yaml` лежит в репозитории.
    """
    target = section.get("connect")
    if target:
        module_name, _, attr = str(target).partition(":")
        if not attr:
            raise ConnectionNotConfigured(t(
                f"database.connect={target!r}: ожидалось 'модуль:функция'",
                f"database.connect={target!r}: expected 'module:function'",
            ))
        try:
            factory = getattr(importlib.import_module(module_name), attr)
        except Exception as exc:
            raise ConnectionNotConfigured(t(
                f"database.connect={target!r} не импортируется: {exc}",
                f"database.connect={target!r} cannot be imported: {exc}",
            )) from exc
        with factory() as conn:
            yield conn
        return

    dsn_env = str(section.get("dsn_env") or "DATABASE_URL")
    dsn = env(dsn_env)
    if not dsn:
        raise ConnectionNotConfigured(t(
            f"database.connect не задан и переменная {dsn_env} пуста — опрашивать нечего",
            f"database.connect is not set and {dsn_env} is empty — nothing to query",
        ))
    try:
        import psycopg  # noqa: PLC0415 — драйвер в extras, не у всех установлен
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ConnectionNotConfigured(t(
            "задан dsn_env, но psycopg не установлен — поставьте partest-atlas[postgres]",
            "dsn_env is set, but psycopg is not installed — install partest-atlas[postgres]",
        )) from exc

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def _first_value(row: Any) -> Any:
    """Первое значение строки: `dict_row` отдаёт словарь, другие драйверы — кортеж."""
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    try:
        return row[0]
    except (IndexError, KeyError, TypeError):
        return None


def _end(conn: Any) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(END_SQL)
    except Exception:  # pragma: no cover - соединение уже потеряно
        pass


@contextmanager
def read_only(conn: Any) -> Iterator[Any]:
    """Блок транзакции только на чтение; на выходе — откат.

    Работает и в autocommit, и в обычном режиме драйвера, и на соединении, которое отдал
    проект (`database.connect`): режим выставляется SQL, а не свойством конкретного
    драйвера. Если база не подтвердила режим — :class:`NotReadOnly`, и ни одного запроса
    сборщика не выполнено.
    """
    with conn.cursor() as cur:
        cur.execute(BEGIN_READ_ONLY_SQL)
        cur.execute(SET_READ_ONLY_SQL)
        cur.execute(CHECK_READ_ONLY_SQL)
        state = _first_value(cur.fetchone())
    if str(state).strip().lower() != "on":
        _end(conn)
        raise NotReadOnly(t(
            f"база не подтвердила режим только чтения (transaction_read_only={state!r}) — "
            "опрос отменён",
            f"the database did not confirm read-only mode (transaction_read_only={state!r}) — "
            "nothing was queried",
        ))
    try:
        yield conn
    finally:
        _end(conn)


def schemas_from(section: Mapping[str, Any]) -> Optional[List[str]]:
    """Схемы из `database.schemas`. ``None`` — `auto`: все несистемные, узнать у базы.

    Нет ключа или пустой список — `public`, как было. Строка — одна схема, а не её буквы:
    `schemas: public` раньше превращался в `['p', 'u', 'b', …]` и молча давал пусто.
    """
    raw = section.get("schemas")
    if raw is None or raw == [] or raw == "":
        return ["public"]
    if isinstance(raw, str):
        return None if raw.strip().lower() == AUTO_SCHEMAS else [raw]
    if isinstance(raw, (list, tuple)) and all(isinstance(x, str) and x for x in raw):
        if [x.strip().lower() for x in raw] == [AUTO_SCHEMAS]:
            return None
        return list(raw)
    raise ValueError(t(
        f"database.schemas={raw!r}: ожидался список схем или auto",
        f"database.schemas={raw!r}: expected a list of schemas or auto",
    ))


def discover_schemas(conn: Any) -> List[str]:
    """Несистемные схемы, в которых есть таблицы или представления. Только чтение."""
    with read_only(conn):
        with conn.cursor() as cur:
            cur.execute(SCHEMAS_SQL)
            return [str(_first_value(row)) for row in cur.fetchall()]
