"""Плагин: снять эффекты прогона в базе. Молчит, пока его не включили.

Включается переменной ``ATLAS_DB_EFFECTS``. Пока она не выставлена, плагин загружен и не
делает **ничего** — ни соединений, ни импорта драйвера. Точка входа `pytest11` означает,
что pytest грузит этот модуль у каждого, кто поставил пакет, и обычный прогон не должен
платить за его присутствие ни временем, ни обращениями к источникам.

Режим выбирается сам, и это не удобство, а необходимость. Снимок вокруг каждого теста
требует выдержки в секунды (см. `collect.db_effects`), и на полном прогоне это часы.
Поэтому: тестов под замером немного — режим ``test``, каждому тесту своя дельта; тестов
много — режим ``session``, один снимок на весь прогон.

Окно теста в режиме ``test`` — весь его протокол: подготовка, вызов и уборка. Данные,
которые тест создаёт фикстурой, — тоже его эффект, и без подготовки окно их теряло бы.
Цена — фикстура шире функции (модуль, сессия) приписывается тесту, на подготовке или
уборке которого она сработала. Тесты не под замером окна не получают вовсе: их записи
в режиме ``test`` не попадают никуда, а не приписываются соседу. Для этого перед
снимком «до» выдерживается ``settle_sec``, если перед тестом под замером шёл тест без
замера или он первый: счётчики публикуются с задержкой, и без выдержки запись соседа
доехала бы до статистики уже внутри чужого окна. Цена режима ``test`` — до двух выдержек
на тест.

Число тестов под замером считается **после** отбора (``-k``, ``--deselect``, ``--lf``):
режим решает то, что действительно запустится.

**Под pytest-xdist** мерит только контроллер и только сессию: воркеры молчат, режим
``test`` выключен с сообщением. Окна параллельных воркеров перекрывались бы, а запись в
один артефакт из нескольких процессов не согласована.

**Недоступный источник не роняет прогон** — карта не то, ради чего проект гоняет тесты.
Но «источника нет» и «я настроен неправильно» сообщаются **по-разному**: первое чинится
на стенде, второе в `atlas.yaml`, и одинаковая строка про них стоила одному проекту
полдня.
"""

from __future__ import annotations

import fnmatch
import os
import time
from typing import Any, Dict, List, Optional

import pytest

from partest_atlas import artifacts
from partest_atlas.adapters.postgres import ConnectionNotConfigured
from partest_atlas.collect import db_effects
from partest_atlas.config import Config, load, non_negative

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


def _active() -> bool:
    """Включён и настроен. Одна строка `note` в состоянии — ещё не включённый плагин."""
    return "config" in _state


def _number(section: Dict[str, Any], key: str, default: float, kind: type) -> Any:
    """Число из раздела `db_effects` — см. :func:`partest_atlas.config.non_negative`."""
    return non_negative(section, key, default, kind, where=MARKER)


def _distributed(config) -> bool:
    """Прогон раздаётся воркерам pytest-xdist, а этот процесс — контроллер.

    Как `_is_distribution_mode` самого xdist: нужен и режим раздачи, и куда раздавать.
    `--dist loadscope` в `addopts` при `-n 0` — прогон в одном процессе, не раздача.
    """
    option = config.option
    return getattr(option, "dist", "no") not in (None, "no") and bool(getattr(option, "tx", None))


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", f"{MARKER}: снять эффекты теста в базе")
    _state.clear()
    if not _on():
        return
    if hasattr(config, "workerinput"):
        # Воркер pytest-xdist: мерит контроллер, второй замер посчитал бы всё дважды.
        return

    try:
        cfg: Config = load()
        section = cfg.section(MARKER)
        settle = _number(section, "settle_sec", db_effects.DEFAULT_SETTLE_SEC, float)
        limit = _number(section, "per_test_limit", DEFAULT_PER_TEST_LIMIT, int)
    except Exception as exc:
        _note(f"{type(exc).__name__}: {exc}", misconfigured=True)
        return

    _state.update(
        config=cfg,
        section=section,
        database=cfg.section("database"),
        include=list(section.get("include") or []),
        settle=settle,
        limit=limit,
        records=[],
        selected=0,
        mode=db_effects.SESSION,
        distributed=_distributed(config),
    )
    if _state["distributed"]:
        print(
            "[atlas] db_effects: под pytest-xdist мерится только дельта сессии на "
            "контроллере, режим test выключен"
        )


def pytest_collection_finish(session) -> None:
    """Режим — по тому, что действительно запустится: после `-k`, `--deselect`, `--lf`."""
    if not _active() or _state.get("distributed"):
        return
    wanted = {
        item.nodeid
        for item in session.items
        if _wanted(item.nodeid, item.get_closest_marker(MARKER) is not None)
    }
    _state["wanted"] = wanted
    _state["selected"] = len(wanted)
    _state["mode"] = choose_mode(len(wanted), _state["limit"])


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    if not _active():
        return
    probe = _open_probe(scope=db_effects.SESSION)
    if probe is not None:
        _state["probe"] = probe


# `tryfirst` — снаружи чужих обёрток протокола: выдержки не должны попадать внутрь
# таймера pytest-timeout (signal — INTERNALERROR, thread — os._exit всего прогона).
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):  # noqa: ARG001
    """Режим ``test``: снимок до и после протокола каждого теста под замером.

    Окно — подготовка, вызов и уборка теста; после него — выдержка ``settle_sec``. Тест
    без замера окна не получает, а следующий за ним тест под замером выдерживает
    ``settle_sec`` ещё и перед снимком «до». В режиме ``session`` хук ничего не делает.
    """
    probe = _open_test_probe(item)
    yield
    if not _active():
        return
    _state["prev_measured"] = probe is not None
    if probe is None:
        return
    try:
        _state.setdefault("records", []).extend(
            probe.finish(test=item.name, nodeid=item.nodeid)
        )
    except Exception as exc:  # pragma: no cover - зависит от стенда
        _note(f"снимок «после» теста {item.nodeid} не снялся ({type(exc).__name__}: {exc})")


def _open_test_probe(item):
    """Замер одного теста на соединении сессии. Не под замером или нечем мерить — `None`."""
    if not _active() or _state.get("mode") != db_effects.TEST:
        return None
    if item.nodeid not in (_state.get("wanted") or ()):
        return None
    session_probe = _state.get("probe")
    if session_probe is None:
        return None
    if not _state.get("prev_measured") and _state["settle"]:
        # Перед нами шёл тест без замера (или никто): его записи ещё могут ехать в
        # статистику и попали бы в наше окно.
        time.sleep(_state["settle"])
    try:
        return db_effects.open_effects(
            session_probe.conn,
            session_probe.schemas,
            scope=db_effects.TEST,
            settle_sec=_state["settle"],
        )
    except Exception as exc:  # pragma: no cover - зависит от стенда
        _note(f"снимок «до» теста {item.nodeid} не снялся ({type(exc).__name__}: {exc})")
        return None


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

    schemas = list((_state.get("database") or {}).get("schemas") or ["public"])
    try:
        probe = db_effects.open_effects(conn, schemas, scope=scope, settle_sec=settle_sec)
    except Exception as exc:
        # Соединение открылось, а снимок «до» — нет (прав на статистику, схема): тоже
        # выключенный сборщик, а не INTERNALERROR прогона.
        try:
            ctx.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass
        _note(f"снимок «до» не снялся ({type(exc).__name__}: {exc})")
        _state.clear()
        return None
    _state["ctx"] = ctx
    return probe


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    if not _active():
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
