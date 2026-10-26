---
title: Статус partest-atlas
status: current
verified: 2026-10-26
sources: [partest_atlas/__init__.py, CHANGELOG.md]
audience: agent
allow_version_literals: true
---

# Статус

**Единственный источник** версии, состояния и того, что дальше. В других страницах этих
сведений нет — там пишут «см. [[status]]».

## Где сейчас

| | |
|---|---|
| Версия | 0.2.2, выпуск 2026-10-25 (совместимо с 0.2.1) |
| Не выпущено | `CHANGELOG.md`, Unreleased: `partest-atlas collect openapi\|db-schema\|container-logs` — [[components/collect]]; `contract.openapi` с `info` и `source`; `database.schemas: auto`, `database.key`, «нет ни одной таблицы» — ошибка; транзакция только на чтение механизмом (`adapters.postgres.read_only`); ленивое соединение `db_effects`; `--lang ru\|en` и язык по локали; пол partest 2.6.0. Набор формата: метка прогона `run` в `db.effects`/`bus.flow`, фазы `setup`/`call`/`teardown` (`db_effects.phases`), `write(..., collected_at=)`, `validate(kind=, scope=)` и `check` сверяют вид с именем и область с каталогом, `PAYLOAD_KEYS`, `inspect`/`discover_scopes`, `__all__` у `artifacts`, `config`, `collect.history`, записи `runs` непрозрачны, `containers.logs.logs` (прочитан/не прочитан, строки), нормализация `ui.flow.screen`, схема 1 — `invalid`. Схема — 2, аддитивно |
| 0.2.2 | `partest_atlas.__version__` в 0.2.1 сообщал 0.2.0 (метаданные колеса — 0.2.1); номер теперь в одном месте — `partest_atlas/__init__.py`, `pyproject.toml` берёт его динамически (`CHANGELOG.md`, 0.2.2). |
| PyPI | `partest-atlas` 0.2.0, опубликована 2026-10-18; 0.1.0 — 2026-10-01; 0.2.1 — 2026-10-25 |
| 0.2.1 | срез покрытия под `coverage.json` partest 2.7.0 и сохранная история (`CHANGELOG.md`, 0.2.1, совместимо с 0.2.0): `avgRule`, `exception`, `scored`, объект `depth`, `avg: null` как «не измерено» (и для `0.0` при `endpoints: 0` из отчёта постарше), проверка типов недоверенного отчёта; непонятый `runs.json` не трогается: запись прогона пропускается с предупреждением `UnreadableHistory` (в 0.2.0 он молча затирался); `runs.json` — строгий JSON — [[components/history]]. |
| Выпущено последним | 0.2.1, опубликована 2026-10-25 (совместимо с 0.2.0) — см. строку 0.2.1 |
| 0.2.0 | несовместимо с 0.1.0 (см. `CHANGELOG.md`): режим `test` у `db_effects` (поштучный замер снаружи чужих обёрток, выдержка перед «до», режим после отбора, только сессия под xdist, нули, бесконечности и кривые числа в `atlas.yaml`, отказ первого снимка без INTERNALERROR); `bus_flow` под xdist только на контроллере; `wrap` и `.cmd`/`.bat` на Windows через системный `cmd.exe` с отказом от опасных аргументов (без печати значения), без текущего каталога и относительного `PATH`, только ведущий `--`; код 1 у `show` на несуществующем пути и не-артефакте; отказ `.docx` с DTD, с `document.xml` больше 16 МиБ и понятная ошибка без `document.xml`; `LICENSE` в дистрибутиве; классификатор `Operating System` |
| Схема артефактов | `SCHEMA_VERSION = 2` |
| Python | ≥ 3.10 |
| Лицензия | MIT, файл `LICENSE` в репозитории и в колесе (в колесе 0.1.0 его нет) |
| Под git | да, с 2026-09-07; `master` опубликован в удалённом репозитории |

## Что есть в коде

- `partest_atlas/artifacts.py` — контракт артефактов версии 2 целиком:
  `version`, `kind`, `scope`, `collected_at`, `collector`; атомарная запись, дописывание
  записей со схлопыванием повторов, снисходительное чтение. См. [[components/artifacts]].
- `Scope` с умолчанием `default/default` — однопроектный случай неотличим от прежнего.
  Артефакты версии 1 контракт не проходят — их снимают заново ([[components/artifacts]]).
- `validate(doc, kind=, scope=)` — проверка документа на соответствие контракту, вида — с
  именем файла, области — с каталогом. Отдельно от `read`, который снисходителен нарочно:
  снисходительность чтения держит карту на ногах, строгость проверки ловит того, кто пишет
  артефакты мимо контракта. `inspect` — решение об артефакте машинным кодом
  (`ok`/`missing`/`broken`/`newer`/`invalid`), `discover_scopes` — области на диске (не
  выпущено).
- `partest_atlas/cli.py` — команда `partest-atlas`: `show`, `check`, `wrap` и `collect`
  (не выпущено). Коды и поведение — [[components/overview]], `collect` —
  [[components/collect]]; язык вывода — `partest_atlas/lang.py`.
- `wrap.py` — запуск чужого прогона, который не наш pytest; возвращается код самой
  команды, а не обёртки. На Windows скрипты `.cmd`/`.bat` идут через `cmd.exe` с отказом
  от аргументов с `% " & | < > ^` и управляющими символами — граница в
  [[components/overview]].
- `tests/` — 685 проверок: контракт, атомарность, упаковка, девять сборщиков, три
  плагина (у `db_effects` — сквозь настоящий pytest, в том числе под xdist), обёртка
  (на Windows — с настоящим `cmd.exe`), команда; срез покрытия — на образцах
  `coverage.json` двух эпох partest в `tests/data/`.
- `config.py` — чтение `atlas.yaml`; `adapters/postgres.py` — соединение, названное
  проектом, а не выбранное пакетом.
- `collect/db_schema.py`, `collect/db_effects.py`, `collect/bus_flow.py`,
  `collect/http_clock.py`, `collect/openapi.py`, `collect/container_logs.py`,
  `collect/history.py`, `collect/specs.py`, `collect/ui_flow.py` — **девять из девяти**.
  У всех вычисление отделено от опроса: форма артефакта, дельта счётчиков и дельта
  смещений проверяются без базы и без брокера.
- `plugins/db_effects.py`, `plugins/bus_flow.py`, `plugins/ui_flow.py` — **все три**;
  каждая строка `pytest11` появилась вместе со своим модулем.

## Чего в коде нет

Ничего из перечисленного не реализовано, и обещать это потребителю нельзя:

- **снимки в команде `wrap`**: `wrap.run` принимает `before`/`after`, но
  `partest-atlas wrap` их не передаёт и только запускает команду;
- **связь с `partest` вне команды `collect`** (не выпущено; пол `partest>=2.6.0`): `collect`
  импортирует `load_confpartest`, `httpx_client`, `partest.tls`, `partest.redact` и
  `partest.sideeffects.assert_read_only_sql` (запросы `db_schema`); `partest.path_match` не
  подключён, а записи плагинов (`nodeid`, `test` в `db.effects`) мимо `partest.redact`;
- маркер требований `req` — ни здесь, ни в `partest`;
- `afterWrite` в `bus.flow` в задуманном смысле: сегодня он значит «часы HTTP видели хоть
  одну пишущую запись» — [[components/overview]];
- зонд по объявленным эндпоинтам с меткой `evidence: probe`;
- адаптеры, кроме `adapters/postgres.py`. Читателя шины и источник журналов называет
  проект в `atlas.yaml` (`bus_flow.reader`, `container_logs.source`).

## Что дальше

Порядок задан планом закрытой части, не этой страницей.

1. ~~`cli.py`~~ — сделано 2026-09-25.
2. ~~Каталог `tests/` и проверки контракта артефактов~~ — сделано 2026-09-25.
3. ~~Перенос сборщиков~~ — **9 из 9**, сделано 2026-09-25: `db.schema`, `db.effects`,
   `bus.flow`, часы HTTP, `contract.openapi`, `containers.logs`, `runner.history`,
   `specs.requirements`, `ui.flow`. Три плагина и обёртка `wrap` — там же.
4. ~~Чтение `.docx`~~ — сделано 2026-09-25. Перенос закончен полностью.
5. ~~Первый выпуск~~ — push и PyPI, 0.1.0 опубликована 2026-10-01.
6. ~~Выпуск исправлений после сверки документации с выпущенной 0.1.0~~ — 0.2.0,
   2026-10-18.
7. ~~Срез покрытия под `coverage.json` partest 2.7.0~~ — 0.2.1, 2026-10-25.
8. Сборщики из командной строки, ленивое соединение `db_effects`, английский вывод — в коде,
   не выпущено (этап 2 дорожной карты, шаг 1а).
9. Набор формата от Вехи (запись 2026-10-06, пп. 2–7, 9–16; схема 2, аддитивно) — в коде,
   не выпущено (шаг 1б). П. 9 закрыт ещё в 0.2.1. Отложено до схемы 3: п. 1 (текст и роль
   элемента в ключе `merge`), `afterWrite` → `sawWrite` ([[proposals]], B), п. 8 (вид
   состояния контейнеров), п. 17 (публичность `collect.*.build` для демо).

**Правило, которое держится тестом.** Строка `pytest11` в `pyproject.toml` появляется
вместе со своим модулем — не раньше и не позже; соответствие в обе стороны проверяет
`tests/test_packaging.py`.

## Открытые вопросы

Спорное с доводами — [[proposals]] (A–E от 2026-10-16).

- Красные линии 2 и 6 в `AGENTS.md` (не выпущено): линия 2 держится транзакцией только на
  чтение у `db_schema` и `db_effects` и проверкой `assert_read_only_sql` у `db_schema`; у
  `db_effects` запросы — константы, проверенные тестом. Линия 6: через `partest.redact` идут
  адрес спецификации в `contract.openapi` и ошибки `collect`, но не `nodeid`/`test` в
  `db.effects` и не текст исключений в выводе плагинов ([[proposals]], D).
