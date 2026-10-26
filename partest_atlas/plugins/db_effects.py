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

**Соединение открывается, только когда есть что мерить.** Не выбран ни один тест под
замером — соединения нет вовсе (и туннеля проекта тоже): такое соединение держало туннель,
на котором оборвалась чужая ручная работа. В режиме ``session`` оно открывается после
отбора тестов, в режиме ``test`` — перед первым тестом под замером и закрывается сразу
после последнего. Дельту всего прогона без маркеров даёт ``ATLAS_DB_EFFECTS_ALL=1``.
Под xdist контроллер маркеров не видит, и там соединение по-прежнему открывается в начале
сессии.

**Метка прогона и фазы.** Каждая запись несёт ``run`` — метку прогона
(:func:`partest_atlas.artifacts.run_label`: ``ATLAS_RUN_ID`` или случайная, плюс время
начала). С ``db_effects.phases: true`` окно теста в режиме ``test`` делится на фазы
``setup``/``call``/``teardown`` — у каждой своя дельта и ключ ``phase``: эффект фикстуры
отделяется от эффекта самого теста. Цена — выдержка ``settle_sec`` на каждой границе
фазы, **внутри** протокола теста (и внутри таймера pytest-timeout, если он стоит на весь
протокол): до двух лишних выдержек на тест. По умолчанию выключено.

**Только чтение держит база:** каждый снимок идёт в транзакции только на чтение
(:func:`partest_atlas.adapters.postgres.read_only`). ``database.schemas: auto`` — все
несистемные схемы; если в выбранных схемах нет ни одной таблицы, сборщик выключается как
ошибка конфигурации, а не пишет пустую дельту.
"""

from __future__ import annotations

import fnmatch
import os
import time
from typing import Any, Dict, List, Optional

import pytest

from partest_atlas import artifacts
from partest_atlas.adapters.postgres import ConnectionNotConfigured, schemas_from
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
        phases = section.get("phases", False)
        if not isinstance(phases, bool):
            raise ValueError(f"{MARKER}.phases={phases!r}: ожидалось true или false")
        limit = _number(section, "per_test_limit", DEFAULT_PER_TEST_LIMIT, int)
        database = cfg.section("database")
        # `None` — `schemas: auto`: какие схемы, узнаём у базы, когда соединение откроется.
        schemas = schemas_from(database)
    except Exception as exc:
        _note(f"{type(exc).__name__}: {exc}", misconfigured=True)
        return

    _state.update(
        config=cfg,
        section=section,
        database=database,
        schemas=schemas,
        include=list(section.get("include") or []),
        settle=settle,
        limit=limit,
        records=[],
        phases=phases,
        run=artifacts.run_label(),
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
    """Режим — по тому, что действительно запустится: после `-k`, `--deselect`, `--lf`.

    Здесь же решается, нужно ли соединение вообще: нет тестов под замером (или только сбор,
    `--collect-only`) — не открывается. Режим ``session`` открывает его сейчас, до первого
    теста; режим ``test`` — перед первым тестом под замером.
    """
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
    if getattr(session.config.option, "collectonly", False):
        _state["idle"] = True
        return
    if not wanted:
        _state["idle"] = True
        print("[atlas] db_effects: ни один тест не выбран под замер — соединение не открывается")
        return
    if _state["mode"] == db_effects.SESSION:
        _open_session_probe()
    else:
        _state["remaining"] = len(wanted)


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    """Под xdist контроллер не собирает тесты и маркеров не видит: соединение — здесь."""
    if _active() and _state.get("distributed"):
        _open_session_probe()


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
    if probe is not None and _state.get("phases"):
        _state["phased"] = (item.nodeid, probe)
    yield
    phased = _state.pop("phased", None)
    if not _active():
        return
    _state["prev_measured"] = probe is not None
    if probe is not None:
        try:
            _state.setdefault("records", []).extend(probe.finish(
                test=item.name, nodeid=item.nodeid,
                phase="teardown" if phased is not None else None,
            ))
        except Exception as exc:  # pragma: no cover - зависит от стенда
            _note(f"снимок «после» теста {item.nodeid} не снялся ({type(exc).__name__}: {exc})")
    if "remaining" in _state and item.nodeid in (_state.get("wanted") or ()):
        # Последний тест под замером позади — соединение больше не нужно, и держать его
        # до конца прогона (вместе с туннелем проекта) незачем.
        _state["remaining"] -= 1
        if _state["remaining"] <= 0:
            _close()


def _phase_boundary(item, phase: str) -> None:
    """Граница фазы окна теста под замером с ``phases: true``. Неудача — без записей фазы."""
    phased = _state.get("phased")
    if not _active() or phased is None or phased[0] != item.nodeid:
        return
    try:
        phased[1].checkpoint(phase, test=item.name, nodeid=item.nodeid)
    except Exception as exc:  # pragma: no cover - зависит от стенда
        _note(f"снимок после фазы {phase} теста {item.nodeid} не снялся "
              f"({type(exc).__name__}: {exc})")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    yield
    _phase_boundary(item, "setup")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    yield
    _phase_boundary(item, "call")


def _open_test_probe(item):
    """Замер одного теста. Не под замером или нечем мерить — `None`.

    Соединение открывается здесь, перед первым тестом под замером. Его первый снимок
    проверяется, как снимок сессии: не снялся или таблиц нет — сборщик выключен.
    """
    if not _active() or _state.get("mode") != db_effects.TEST:
        return None
    if item.nodeid not in (_state.get("wanted") or ()):
        return None
    first = "conn" not in _state
    conn = _connection()
    if conn is None:
        return None
    if not _state.get("prev_measured") and _state["settle"]:
        # Перед нами шёл тест без замера (или никто): его записи ещё могут ехать в
        # статистику и попали бы в наше окно.
        time.sleep(_state["settle"])
    if first:
        return _first_probe(db_effects.TEST, settle_sec=_state["settle"])
    try:
        return db_effects.open_effects(
            conn, _state["schemas"], scope=db_effects.TEST, settle_sec=_state["settle"],
            run=_state.get("run"),
        )
    except Exception as exc:  # pragma: no cover - зависит от стенда
        _note(f"снимок «до» теста {item.nodeid} не снялся ({type(exc).__name__}: {exc})")
        return None


def _close() -> None:
    """Закрыть соединение замера, если оно открыто."""
    _state.pop("conn", None)
    ctx = _state.pop("ctx", None)
    if ctx is not None:
        try:
            ctx.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass


def _off(reason: str, *, misconfigured: bool = False) -> None:
    """Выключить сборщик, но не прогон: закрыть соединение, сказать почему."""
    _close()
    _note(reason, misconfigured=misconfigured)
    _state.clear()


def _connection():
    """Соединение замера — одно, открывается при первой нужде. Неудача выключает сборщик.

    Здесь же `schemas: auto` превращается в список: какие схемы есть, знает только база.
    """
    if "conn" in _state:
        return _state["conn"]
    # Только когда включены и есть что мерить: импорт и соединение не стоят прогону ничего.
    from partest_atlas.adapters.postgres import connect, discover_schemas  # noqa: PLC0415

    try:
        ctx = connect(_state.get("database") or {})
        conn = ctx.__enter__()
    except ConnectionNotConfigured as exc:
        _off(str(exc), misconfigured=True)
        return None
    except Exception as exc:
        _off(f"источник недоступен ({type(exc).__name__}: {exc})")
        return None
    _state["ctx"] = ctx
    if _state.get("schemas") is None:
        try:
            _state["schemas"] = discover_schemas(conn)
        except Exception as exc:
            _off(f"схемы не узнать (schemas: auto) ({type(exc).__name__}: {exc})")
            return None
        if not _state["schemas"]:
            _off("schemas: auto — у базы нет ни одной несистемной схемы с таблицами",
                 misconfigured=True)
            return None
    _state["conn"] = conn
    return conn


def _first_probe(scope: str, *, settle_sec: float = 0.0):
    """Первый снимок на соединении. Не снялся или таблиц нет — сборщик выключен."""
    schemas = list(_state["schemas"])
    try:
        probe = db_effects.open_effects(
            _state["conn"], schemas, scope=scope, settle_sec=settle_sec, run=_state.get("run")
        )
    except Exception as exc:
        # Соединение открылось, а снимок «до» — нет (прав на статистику, схема): тоже
        # выключенный сборщик, а не INTERNALERROR прогона.
        _off(f"снимок «до» не снялся ({type(exc).__name__}: {exc})")
        return None
    if not probe.tables:
        # Пустая статистика — схемы не те (таблицы переехали): молча записанная пустая
        # дельта выглядела бы как «прогон ничего не менял».
        _off(f"в выбранных схемах нет ни одной таблицы: {', '.join(schemas)} — проверьте "
             "database.schemas или задайте schemas: auto", misconfigured=True)
        return None
    return probe


def _open_session_probe() -> None:
    """Замер сессии: соединение и снимок «до». Неудача выключает сборщик, но не прогон."""
    if _connection() is None:
        return
    probe = _first_probe(db_effects.SESSION)
    if probe is not None:
        _state["probe"] = probe


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    if not _active() or _state.get("idle"):
        return
    probe = _state.pop("probe", None)
    records: List[dict] = list(_state.get("records") or [])
    if probe is not None and _state.get("mode") == db_effects.SESSION:
        probe.settle_sec = _state["settle"]
        try:
            records.extend(probe.finish())
        except Exception as exc:  # pragma: no cover - зависит от стенда
            _note(f"снимок «после» не снялся ({type(exc).__name__}: {exc})")

    _close()

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
