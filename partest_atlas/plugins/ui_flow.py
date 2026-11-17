"""Плагин: снять связи «интерфейс → API». Молчит, пока его не включили.

Включается переменной ``ATLAS_UI_FLOW``. Выключенным не читает конфигурацию и не трогает
страницу — точка входа грузит модуль у каждого, кто поставил пакет.

Подключается фикстурой ``atlas_ui_flow``: проект сам передаёт ей страницу, потому что
имя фикстуры страницы пакету неизвестно и известным быть не должно. Ни одна ошибка
страницы не роняет прогон: сбор карты — не то, ради чего гоняют UI-тесты.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import ui_flow
from partest_atlas.config import Config, load, non_negative

ENABLE_ENV = "ATLAS_UI_FLOW"
MARKER = "ui_flow"

_state: Dict[str, Any] = {}


def _on() -> bool:
    return (os.getenv(ENABLE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", f"{MARKER}: снять связи интерфейса с API")
    _state.clear()
    if not _on():
        return
    try:
        cfg: Config = load()
        section = cfg.section(MARKER)
        window_ms = int(non_negative(section, "window_ms", ui_flow.DEFAULT_WINDOW_MS, int,
                                     where=MARKER) or ui_flow.DEFAULT_WINDOW_MS)
        cfg.scope  # noqa: B018 — негодная область — сейчас, а не в конце прогона
    except Exception as exc:
        print(f"[atlas] ui_flow ВЫКЛЮЧЕН ОШИБКОЙ КОНФИГУРАЦИИ — чинить здесь: {exc}")
        return
    for key in cfg.unknown_keys():
        print(f"[atlas] atlas.yaml: неизвестный ключ {key} — не читается")
    _state.update(
        config=cfg,
        window_ms=window_ms,
        ignore_url=list(section.get("ignore_url") or []),
        ignore_control=list(section.get("ignore_control") or []),
        links=[],
        run=artifacts.run_label(),
    )


@pytest.fixture()
def atlas_ui_flow(request):
    """Фабрика записи: ``recorder = atlas_ui_flow(page)``.

    Выключённым отдаёт пустышку — тест не должен знать, включён сбор или нет, и не
    должен обрастать ветками ради карты. У включённого рекордера provenance — полный
    nodeid именно этого варианта теста и одна метка текущего прогона.
    """
    записано: List[Any] = []

    def подключить(page: Any):
        if not _state:
            return _Пустышка()
        recorder = ui_flow.Recorder(
            window_ms=_state["window_ms"],
            ignore_url=_state["ignore_url"],
            ignore_control=_state["ignore_control"],
            nodeid=request.node.nodeid,
            run=_state["run"],
        ).attach(page)
        записано.append((recorder, page))
        return recorder

    yield подключить

    for recorder, page in записано:
        try:
            _state.setdefault("links", []).extend(recorder.drain(page))
        except Exception as exc:
            _state["drain_failed"] = True
            print(f"[atlas] ui_flow: связи со страницы не сняты ({type(exc).__name__}: {exc})")


class _Пустышка:
    """Ничего не делает и не притворяется, что делает."""

    def drain(self, page: Any) -> List[Dict[str, Any]]:
        return []


def pytest_runtest_call(item) -> None:  # noqa: ARG001
    """Отличает исполненный прогон от `--collect-only` и пустого набора."""
    if _state:
        _state["executed"] = True


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    if not _state:
        return
    cfg: Optional[Config] = _state.get("config")
    links = _state.get("links") or []
    if cfg is None:
        return
    if not _state.get("executed"):
        print(f"[atlas] ui_flow: {ui_flow.KIND} не записан (тесты не исполнялись)")
        return
    if _state.get("drain_failed"):
        print(f"[atlas] ui_flow: {ui_flow.KIND} не записан (не все страницы дочитаны)")
        return
    if exitstatus not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        print(f"[atlas] ui_flow: {ui_flow.KIND} не записан (прогон завершился с ошибкой)")
        return
    if not links:
        # Пустой документ — наблюдение текущего прогона, а не отказ от записи: иначе
        # полный прогон без связей оставлял бы на диске прежнее состояние.
        print("[atlas] ui_flow: связей не зафиксировано — записан текущий пустой артефакт")
    try:
        path = cfg.path_for(ui_flow.KIND)
        doc = ui_flow.build(links)
        artifacts.write(path, ui_flow.KIND, doc, scope=cfg.scope, collector="partest-atlas/ui_flow")
    except Exception as exc:
        # Как у двух других плагинов: файл не записан — строка с причиной, не INTERNALERROR.
        try:
            from partest.redact import redact_text  # noqa: PLC0415

            reason = redact_text(f"{type(exc).__name__}: {exc}")
        except Exception:  # pragma: no cover - partest — зависимость пакета
            reason = type(exc).__name__
        print(f"[atlas] ui_flow: {ui_flow.KIND} не записан ({reason})")
        return
    s = doc["stats"]
    print(
        f"[atlas] ui_flow: связей {s['links']}, экранов {s['screens']}, "
        f"эндпоинтов {s['endpoints']}, прямых {s['direct']} -> {path}"
    )
