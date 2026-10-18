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

## 2026-10-18 · release · 0.2.0

`Unreleased` свёрнут в 0.2.0 (несовместимо с 0.1.0); `status.md` — 0.2.0 на PyPI.

## 2026-10-16 · update · сверка с выпущенной 0.1.0

Вики сверена с колесом PyPI и нашли устаревшее и неправду; исправлено
вместе с кодом.

| Страница | Что |
|---|---|
| [[status]] | выпуск на PyPI, честный список «чего нет» (снимки `wrap`, связь с partest, `req`, `afterWrite`) |
| [[howto/prepare-a-project]] | ушло «сборщики не перенесены»; коды `show` |
| [[components/overview]] | коды команды, `wrap` на Windows и без снимков, окно `db_effects`, что на деле значит `afterWrite` |

2026-10-17: [[components/overview]] — граница гарантии `wrap` для
`.cmd`/`.bat`, выдержка перед «до» и xdist у `db_effects`, одна формулировка `afterWrite`;
[[proposals]] — спорное A–E (отказ против экранирования, `afterWrite`, зависимость от
partest, красные линии против кода, эффект широкой фикстуры).

2026-10-17: [[components/overview]] — поиск команды и системный `cmd.exe` у
`wrap`, `.\gradlew`, раздел про чтение `.docx` (`specs.py` в `sources:`), xdist у
`bus_flow` и `ui_flow`; [[components/artifacts]] — дописывание без блокировки;
[[proposals]] — С1 к A, С2 принято, С3–С5.

2026-10-17: [[components/overview]] — абсолютная запись PATH, совпавшая с
текущим каталогом, просматривается; признак раздачи xdist; `afterWrite` и `http` под
xdist; память на потолке `.docx`; разделены склеенные абзацы. [[proposals]] — С-1 к С4,
С-2 (`wrap -- python` в неактивированном venv).

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
| [[concepts/evidence]] | `AGENTS.md`, красная линия 7 — черновик, кода ещё нет |
| [[status]] | `CHANGELOG.md` плюс сверка с рабочим деревом |

Отдельно зафиксировано в [[status]]: `project.scripts` объявляет команду `partest-atlas`,
а `partest_atlas/cli.py` не существует. Это не документационная правка, а дефект — заведён
как ближайшая задача, а не молча описан.

Страница `concepts/evidence` заведена со статусом `draft` и пустым `sources` намеренно:
правило есть в `AGENTS.md`, кода под него нет. Пустой `sources` у `current`-страницы был бы
обманом — такая страница не сможет протухнуть заметно.
