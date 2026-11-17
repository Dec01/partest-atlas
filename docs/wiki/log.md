---
title: Журнал операций над документацией
status: current
verified: 2026-11-17
sources: []
audience: maintainer
allow_version_literals: true
---

# Журнал

Хронология: что сделано с документацией и почему. Новые записи сверху.
Формат строки: дата · операция (ingest / update / lint / restructure) · что затронуто.

## 2026-11-17 · update · подготовка кандидата 0.4.0

`Невыпущено` свёрнуто в 0.4.0: `collect specs`, provenance и корректные пустые
артефакты `ui.flow`, глобально уникальные ID и отказ UNC в `specs`. Номер версии остаётся
в одном месте — `partest_atlas/__init__.py`. 0.3.0 — последняя опубликованная версия;
0.4.0 подготовлена, но ещё не выпущена. [[status]] синхронизирована с CHANGELOG.

## 2026-11-16 · update · подготовка выпуска 0.3.0

`Unreleased` свёрнут в 0.3.0: CLI-сборщики, аддитивный набор формата со схемой 2,
pass-through `lifecycle_plugins` и обновлённые правила чтения/записи. Номер версии остаётся
в одном месте — `partest_atlas/__init__.py`; `setup.py` только вызывает setuptools и не
дублирует метаданные. [[status]] синхронизирована с CHANGELOG.

## 2026-10-26 · update · находки qa: `attempts` и строгое чтение (не выпущено)

[[components/artifacts]], [[components/overview]] — `attempt` → `attempts: N` (окно
охватывает все попытки повтора, дельта — их сумма); `check` и `show` читают строго, как
`inspect`. [[status]]; [[proposals]] — Y (составные ключи в тексте — у partest).

## 2026-10-26 · update · узкий раунд security: исключение для идентификаторов (не выпущено)

[[components/artifacts]] — как есть только «чистая» строка-идентификатор, секретные ключи по
окончаниям, число под секретным ключом остаётся. [[components/collect]] — локальное место
через `redact_url`, секретные колонки по тому же правилу. [[proposals]] — W (ссылки и
junction), X (потолок узлов).

## 2026-10-26 · update · сводный раунд контролёров по кандидату 0.3.0 (не выпущено)

[[components/artifacts]] — `fullmatch`, UNC в любом написании, правила маскировки (секретные
ключи, идентификаторы, совпавшие ключи, бюджет), `keep_runs`: прогон — `run.id`, плюс
последняя запись связи, `attempt`, строгий `inspect`. [[components/collect]] — голый
`collect`, печать записанного без Cc/Cf, `--json` с `error`, службы (`collectedAt`, порядок,
убранные службы, параллельные вызовы), `resolvedFrom`, потолки разбора, пустая область.
[[components/overview]] — `KNOWN_KEYS`, `keep_runs`, `attempt`, сбой границы фазы, фазы в
`session`, неизвестные ключи у всех плагинов; `sources` + `collect/db_effects.py`.
[[howto/prepare-a-project]] — шаги сведены с таблицей, все переменные, первый запуск с
`ATLAS_DB_EFFECTS_ALL=1`, разделы `database`/`bus_flow`, `veha init`, ключи `project` не
читаются. [[status]], [[proposals]] — T, U, V.

## 2026-10-26 · update · код выхода `collect` и поправки к partest 2.8 (не выпущено)

[[components/collect]], [[components/overview]], [[howto/prepare-a-project]] — `collect` на «не снято» отдаёт 1, 
2 — только разбор аргументов ([[proposals]], L — решено). [[components/collect]] — грамматика из `partest.conf`, 
локальный путь по правилу partest (сначала текущий каталог), честное описание 180 с: проверка между шагами, не общий предел.

## 2026-10-26 · update · находки контролёров шага 1а (не выпущено)

[[components/collect]] — загрузка у partest (`resolve_swagger`, потолки), импорт
`confpartest.py` «не как в pytest» и сверка до импорта, несколько служб (`services`),
правило имени области, неизвестные ключи и UNC, коды и вопрос о `1`, кодовая страница
вывода, `exclude` по схеме, `connect_timeout`, `ConnectionFailed`, цена отката, язык —
внутренний модуль. [[components/artifacts]] — правило имени области, маскировка при
записи, `keep_runs`, поля `schema`, `services`. [[components/overview]] — коды `collect`,
`ATLAS_DB_EFFECTS_ALL` всегда сессия, `keep_runs`, почему фазы выключены (таймер
pytest-timeout), `schema` в `db.effects`. [[status]] — строка «Не выпущено», 737 проверок,
связь с partest, линии 2 и 6. [[proposals]] — D (линии закрыты в коде), L–S.

## 2026-10-26 · update · набор формата от Вехи (не выпущено)

[[components/artifacts]] — публичные имена `artifacts`, `collected_at`, ключи полезной
нагрузки, аддитивные поля (`run`, `phase`, `logs`, `screen`), сверка области с каталогом,
`inspect`/`discover_scopes`, «версия 1 не годится как данные» вместо «читается без
миграции». [[components/overview]] — публичный экспорт трёх модулей, метка прогона и фазы
`db_effects`, нормализация экрана. [[components/history]] — записи `runs` непрозрачны как
часть контракта, `__all__`. [[components/collect]] — `logs` у `containers.logs`.
[[howto/prepare-a-project]] — что ещё сверяет `check`. [[status]] — строка «Не выпущено»,
685 проверок, п. 9 закрыт, отложенное до схемы 3. [[proposals]] — E принято; H — команда
`python -m partest.reports compare`.

## 2026-10-25 · ingest · сборщики из командной строки (не выпущено)

Заведена [[components/collect]]: `collect openapi|db-schema|container-logs`, источник
контракта из `confpartest.swagger_files`, `info`/`source` в `contract.openapi`,
`schemas: auto`, `database.key`, транзакция только на чтение, язык вывода. [[components/overview]] —
строка `collect`, ключи `--json`/`--lang`, ленивое соединение `db_effects`;
[[components/artifacts]] — язык текстов `validate`; [[howto/prepare-a-project]] — команды
шага 5; [[status]] — строка «Не выпущено», 620 проверок, связь с partest, линии 2 и 6;
[[proposals]] — дополнения к C и D.

## 2026-10-25 · update · выпуск 0.2.2

Исправление 0.2.1: `__version__` отставал от метаданных колеса. Версия — один литерал в
`partest_atlas/__init__.py`, `pyproject.toml` — `dynamic = ["version"]`. [[status]] — 0.2.2, 516 проверок, «Выпущено последним» — 0.2.1;
`CHANGELOG.md` — 0.2.2. [[components/artifacts]], [[components/overview]] и [[howto/prepare-a-project]]
сверены: о номере версии пакета они не пишут.

## 2026-10-25 · update · выпуск 0.2.1

`Unreleased` свёрнут в 0.2.1 (совместимо с 0.2.0); `pyproject.toml` — 0.2.1; [[status]] — 0.2.1. [[components/overview]] и [[howto/prepare-a-project]] сверены: в
`pyproject.toml` изменилась только версия. `MANIFEST.in` — образцы `tests/data` в sdist.

## 2026-10-25 · ingest · срез покрытия под partest 2.7.0

Заведена [[components/history]]: записи `runner.history`, ключи среза покрытия, `avg: null`
как «не измерено», правило среднего `avgRule` и несравнимость средних разных правил.
Раньше срез не описывала ни одна страница. [[status]] — Unreleased, число проверок;
[[components/artifacts]] — ссылка на страницу истории из таблицы видов; `index.md` — строка
каталога.

Чтение недоверенного отчёта: глубина — объектом `depth`, как у владельца формата; типы
проверяются; запись без `avgRule` — правило неизвестно, а не «1». История: непонятый
`runs.json` не переименовывается и не перезаписывается — запись пропускается с
предупреждением до любого действия; записи непрозрачны; `meta` не объект — правило
неизвестно; потолок 16 МиБ и обрезка по размеру, чтение по одному дескриптору, пустой файл —
пустая история, `read` с той же проверкой, советы в предупреждении, `limit <= 0` оговорён.
[[proposals]] — F (пересчёт среднего при снятии), G (частичный прогон в срезе), H (чтение
кривых отчётов у `partest` и атласа), I (машиночитаемый исход `append` и `read`), J (чужой
вид под именем `runs.json`), K (`limit <= 0`).

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
