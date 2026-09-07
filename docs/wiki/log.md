---
title: Журнал операций над документацией
status: current
verified: 2026-09-07
sources: []
audience: maintainer
allow_version_literals: true
---

# Журнал

Хронология: что сделано с документацией и почему. Новые записи сверху.
Формат строки: дата · операция (ingest / update / lint / restructure) · что затронуто.

## 2026-09-07 · restructure · заведена вики

Репозиторий поставлен под git и получил документацию по схеме, общей для семейства
`partest`.

Содержание страниц взято из уже существовавших `AGENTS.md`, `README.md`, `CHANGELOG.md` и
из кода `partest_atlas/artifacts.py`. Ничего не придумано заново: вики разложила то, что
было написано, и связала с исходниками через `sources:`.

| Страница | Откуда содержание |
|---|---|
| [[components/artifacts]] | `partest_atlas/artifacts.py` |
| [[components/overview]] | `README.md`, `pyproject.toml`, `partest_atlas/__init__.py` |
| [[decisions/agent-and-server]] | `README.md`, раздел «Зачем разделено так» |
| [[concepts/evidence]] | `AGENTS.md`, красная линия 7 — черновик, кода ещё нет |
| [[status]] | `CHANGELOG.md` плюс сверка с рабочим деревом |

Отдельно зафиксировано в [[status]]: `project.scripts` объявляет команду `partest-atlas`,
а `partest_atlas/cli.py` не существует. Это не документационная правка, а дефект — заведён
как ближайшая задача, а не молча описан.

Страница `concepts/evidence` заведена со статусом `draft` и пустым `sources` намеренно:
правило есть в `AGENTS.md`, кода под него нет. Пустой `sources` у `current`-страницы был бы
обманом — такая страница не сможет протухнуть заметно.
