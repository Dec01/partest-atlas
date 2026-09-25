"""Плагин: снять эффекты прогона в базе. Молчит, пока его не включили.

Включается переменной ``ATLAS_DB_EFFECTS``. Пока она не выставлена, плагин загружен и не
делает **ничего** — ни соединений, ни импорта драйвера. Точка входа `pytest11` означает,
что pytest грузит этот модуль у каждого, кто поставил пакет, и обычный прогон не должен
платить за его присутствие ни временем, ни обращениями к источникам.

Режим выбирается сам, и это не удобство, а необходимость. Снимок вокруг каждого теста
требует выдержки в секунды (см. `collect.db_effects`), и на полном прогоне это часы.
Поэтому: тестов под замером немного — режим ``test``, каждому тесту своя дельта; тестов
много — режим ``session``, один снимок на весь прогон.

**Недоступный источник не роняет прогон** — карта не то, ради чего проект гоняет тесты.
Но «источника нет» и «я настроен неправильно» сообщаются **по-разному**: первое чинится
на стенде, второе в `atlas.yaml`, и одинаковая строка про них стоила одному проекту
полдня.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Dict, List, Optional

from partest_atlas import artifacts
from partest_atlas.adapters.postgres import ConnectionNotConfigured
from partest_atlas.collect import db_effects
from partest_atlas.config import Config, load

ENABLE_ENV = "ATLAS_DB_EFFECTS"
ALL_ENV = "ATLAS_DB_EFFECTS_ALL"
MARKER = "db_effects"

#: Сколько тестов под замером ещё имеет смысл мерить поштучно. Выше — режим сессии.
DEFAULT_PER_TEST_LIMIT = 12

_state: Dict[str, Any] = {}


def _on() -> bool:
    return (os.getenv(ENABLE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _wanted(nodeid: str, marked: bool) -> bool:
    """Тест под замером: помечен маркером, подошёл под маску, или мерим всех."""
    if (os.getenv(ALL_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if marked:
        return True
    return any(fnmatch.fnmatch(nodeid, pat) for pat in _state.get("include") or [])


def choose_mode(selected: int, limit: int) -> str:
    """Режим по числу тестов под замером. Ноль — тоже `session`: мерить нечего поштучно,
    но общая дельта прогона всё равно осмысленна."""
    return db_effects.TEST if 0 < selected <= limit else db_effects.SESSION


def _note(reason: str, *, misconfigured: bool = False) -> None:
    _state["note"] = reason
    if misconfigured:
        print(f"[atlas] db_effects ВЫКЛЮЧЕН ОШИБКОЙ КОНФИГУРАЦИИ — чинить здесь: {reason}")
    else:
        print(f"[atlas] db_effects выключен: {reason}")


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", f"{MARKER}: снять эффекты теста в базе")
    _state.clear()
    if not _on():
        return

    try:
        cfg: Config = load()
    except Exception as exc:
        _note(f"{type(exc).__name__}: {exc}", misconfigured=True)
        return

    section = cfg.section(MARKER)
    _state.update(
        config=cfg,
        section=section,
        database=cfg.section("database"),
        include=list(section.get("include") or []),
        settle=float(section.get("settle_sec") or db_effects.DEFAULT_SETTLE_SEC),
        limit=int(section.get("per_test_limit") or DEFAULT_PER_TEST_LIMIT),
        records=[],
        selected=0,
        mode=db_effects.SESSION,
    )


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ARG001
    if not _state:
        return
    selected = sum(
        1 for item in items if _wanted(item.nodeid, item.get_closest_marker(MARKER) is not None)
    )
    _state["selected"] = selected
    _state["mode"] = choose_mode(selected, _state["limit"])


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    if not _state:
        return
    probe = _open_probe(scope=db_effects.SESSION)
    if probe is not None:
        _state["probe"] = probe


def _open_probe(*, scope: str, settle_sec: float = 0.0):
    """Открыть замер. Любая неудача выключает сборщик, но не прогон."""
    from partest_atlas.adapters.postgres import connect  # noqa: PLC0415 — только когда включены

    section = _state.get("database") or {}
    try:
        ctx = connect(section)
        conn = ctx.__enter__()
    except ConnectionNotConfigured as exc:
        _note(str(exc), misconfigured=True)
        _state.clear()
        return None
    except Exception as exc:
        _note(f"источник недоступен ({type(exc).__name__}: {exc})")
        _state.clear()
        return None

    _state["ctx"] = ctx
    schemas = list((_state.get("database") or {}).get("schemas") or ["public"])
    return db_effects.open_effects(conn, schemas, scope=scope, settle_sec=settle_sec)


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    if not _state:
        return
    probe = _state.pop("probe", None)
    records: List[dict] = list(_state.get("records") or [])
    if probe is not None and _state.get("mode") == db_effects.SESSION:
        probe.settle_sec = _state["settle"]
        try:
            records.extend(probe.finish())
        except Exception as exc:  # pragma: no cover - зависит от стенда
            _note(f"снимок «после» не снялся ({type(exc).__name__}: {exc})")

    ctx = _state.pop("ctx", None)
    if ctx is not None:
        try:
            ctx.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass

    cfg: Optional[Config] = _state.get("config")
    if cfg is None:
        return
    if not records:
        print(f"[atlas] db_effects: режим {_state.get('mode')}, изменений не зафиксировано")
        return

    path = cfg.path_for(db_effects.KIND)
    artifacts.append_records(
        path,
        db_effects.KIND,
        "records",
        records,
        scope=cfg.scope,
        collector="partest-atlas/db_effects",
    )
    tables = {r["table"] for r in records}
    print(
        f"[atlas] db_effects: режим {_state.get('mode')}, записей {len(records)}, "
        f"таблиц {len(tables)} -> {path}"
    )
