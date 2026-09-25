"""Плагин: снять поток шины вокруг прогона. Молчит, пока его не включили.

Включается переменной ``ATLAS_BUS_FLOW``. Пока она не выставлена, плагин загружен и не
делает ничего — ни соединений, ни импорта клиента шины.

**Три исхода, а не два.** Сборщик необязательный и прогон не роняет, но молчать об
этом по-разному нельзя:

``не настроен``
    Проект шину не описал. Законное состояние, обычная строка.

``ОШИБКА КОНФИГУРАЦИИ``
    Описал, но неверно — чинится в `atlas.yaml`. Оплачено полднем поиска: клиент шины
    отверг ключ конфигурации, сборщик до конца прогона сообщал «брокер недоступен»,
    артефакт молча не писался, и по выводу это было неотличимо от лежащего стенда.

``источник недоступен``
    Описан верно, брокера нет — чинится на стенде.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from partest_atlas import artifacts
from partest_atlas.collect import bus_flow
from partest_atlas.collect.bus_flow import ReaderNotConfigured
from partest_atlas.config import Config, load

ENABLE_ENV = "ATLAS_BUS_FLOW"
MARKER = "bus_flow"

_state: Dict[str, Any] = {}


def _on() -> bool:
    return (os.getenv(ENABLE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _note(reason: str, *, misconfigured: bool = False) -> None:
    _state["note"] = reason
    if misconfigured:
        print(f"[atlas] bus_flow ВЫКЛЮЧЕН ОШИБКОЙ КОНФИГУРАЦИИ — чинить здесь: {reason}")
    else:
        print(f"[atlas] bus_flow выключен: {reason}")


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", f"{MARKER}: снять поток шины вокруг теста")
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
        include=list(section.get("include") or []),
        settle=float(section.get("settle_sec") or bus_flow.DEFAULT_SETTLE_SEC),
        records=[],
    )


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    if not _state:
        return
    section = _state["section"]
    try:
        ctx = bus_flow.open_reader(section)
    except ReaderNotConfigured as exc:
        _note(str(exc), misconfigured=True)
        _state.clear()
        return
    if ctx is None:
        _note("читатель шины не настроен")
        _state.clear()
        return

    try:
        reader = ctx.__enter__()
        topics = bus_flow.topics_for(reader.list_topics(), _state["include"])
    except Exception as exc:
        _note(f"источник недоступен ({type(exc).__name__}: {exc})")
        _state.clear()
        return

    if not topics:
        _note("под маски bus_flow.include не подошёл ни один топик")
    _state["ctx"] = ctx
    _state["flow"] = bus_flow.open_flow(reader, topics, scope=bus_flow.SESSION)


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    if not _state:
        return
    flow = _state.pop("flow", None)
    records: List[dict] = list(_state.get("records") or [])
    if flow is not None:
        flow.settle_sec = _state["settle"]
        try:
            records.extend(flow.finish())
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
        print("[atlas] bus_flow: событий не зафиксировано")
        return

    path = cfg.path_for(bus_flow.KIND)
    artifacts.append_records(
        path, bus_flow.KIND, "records", records,
        scope=cfg.scope, collector="partest-atlas/bus_flow",
    )
    print(
        f"[atlas] bus_flow: записей {len(records)}, "
        f"топиков {len({r['topic'] for r in records})} -> {path}"
    )
