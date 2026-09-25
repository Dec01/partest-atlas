"""Соединение с PostgreSQL так, как назвал его проект.

Пакет не знает, как у проекта устроен доступ к базе, и знать не должен: там бывают
бастионы, туннели, чужие менеджеры секретов. Поэтому соединение **называется в
`atlas.yaml`**, а не выбирается кодом.

В прототипе путь к функции соединения конкретного проекта был вписан в сборщик
константой. Это делало перенос на второй проект правкой пакета — ровно то, что пакет
запрещает.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from partest_atlas.config import env


class ConnectionNotConfigured(RuntimeError):
    """Соединение не описано либо описано неверно.

    Отдельный класс, потому что вызывающему нужно отличать **это** от «база недоступна».
    Первое чинится в `atlas.yaml`, второе — на стенде, и одинаковое сообщение про них
    стоило одному проекту полдня поисков.
    """


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
            raise ConnectionNotConfigured(
                f"database.connect={target!r}: ожидалось 'модуль:функция'"
            )
        try:
            factory = getattr(importlib.import_module(module_name), attr)
        except Exception as exc:
            raise ConnectionNotConfigured(
                f"database.connect={target!r} не импортируется: {exc}"
            ) from exc
        with factory() as conn:
            yield conn
        return

    dsn_env = str(section.get("dsn_env") or "DATABASE_URL")
    dsn = env(dsn_env)
    if not dsn:
        raise ConnectionNotConfigured(
            f"database.connect не задан и переменная {dsn_env} пуста — опрашивать нечего"
        )
    try:
        import psycopg  # noqa: PLC0415 — драйвер в extras, не у всех установлен
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ConnectionNotConfigured(
            "задан dsn_env, но psycopg не установлен — поставьте partest-atlas[postgres]"
        ) from exc

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()
