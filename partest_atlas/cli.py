"""Команда `partest-atlas`: посмотреть артефакт, проверить его и снять наблюдения без прогона.

Точка входа была объявлена в `pyproject.toml` раньше, чем появился этот модуль, и
установка пакета давала неработающую команду. Здесь она появляется, и содержание у неё
выбрано не случайное: единственное, что в пакете уже есть и что уже можно сломать
необратимо, — это **формат артефактов**. Его читает чужая программа, и у неё нет способа
проверить себя, кроме как этой командой.

`show` и `check` только читают, `wrap` запускает чужую команду и сам ничего не пишет.
`collect` снимает наблюдение, которому прогон тестов не нужен (контракт OpenAPI, схема
базы, связи из журналов контейнеров), и пишет артефакт — только в свой каталог
`<artifacts_dir>/<система>/<контур>/`. Раньше для этого проекту приходилось писать свой код.

Язык вывода — по ключу `--lang ru|en`, без него — по локали (:mod:`partest_atlas.lang`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from partest_atlas import artifacts, lang
from partest_atlas.artifacts import DEFAULT_SCOPE_KEY, Scope
from partest_atlas.lang import t

#: Код возврата, когда расхождения найдены. Отдельный от `1`, потому что `1` достаётся
#: неудаче самой команды (файл не читается, аргументы кривые) — а это разные новости:
#: «проверка нашла» и «проверка не состоялась» нельзя сводить к одному числу.
EXIT_PROBLEMS = 2

#: Код возврата `collect`, когда снять не удалось: одна строка в stderr и `1` — «команда не
#: состоялась», то же правило, что у `check` и `show`. Артефакт в этом случае не пишется и
#: прежний не трогается. `2` у `collect` — только ошибка разбора аргументов (argparse):
#: опечатку в ключе нельзя путать с недоступной базой (решение 2026-10-26, proposals L).
EXIT_COLLECT_FAILED = 1

COMMANDS = ("check", "show", "wrap", "collect")


def _iter_paths(raw: Sequence[str]) -> List[Path]:
    """Развернуть каталоги в файлы артефактов. Порядок — как назвали, без сортировки."""
    out: List[Path] = []
    known = set(artifacts.KINDS.values())
    for item in raw:
        path = Path(item)
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*.json") if p.name in known))
        else:
            out.append(path)
    return out


def _expected_scopes(raw: Sequence[str]) -> Dict[Path, Optional[Scope]]:
    """Область, которую файл обязан нести, — по каталогу, в котором он найден.

    Только для файлов, найденных в названном каталоге по раскладке
    ``<каталог>/<система>/<контур>/<файл>``: про файл, названный напрямую, или про файл
    в корне каталога нельзя сказать, чья это область, и сверять её не с чем. Область —
    через `artifacts.scope_of`; каталог не по правилу имени (`Система/x/`) — ``None``, и
    `check` называет его расхождением, а не падает.
    """
    known = set(artifacts.KINDS.values())
    out: Dict[Path, Optional[Scope]] = {}
    for item in raw:
        base = Path(item)
        if not base.is_dir():
            continue
        for path in base.rglob("*.json"):
            parts = path.relative_to(base).parts
            if path.name in known and len(parts) == 3:
                out[path] = artifacts.scope_of(path)
    return out


def _cmd_check(paths: Sequence[str], *, as_json: bool) -> int:
    targets = _iter_paths(paths)
    scopes = _expected_scopes(paths)
    if not targets:
        # Проверка, которой нечего смотреть, не «прошла» — у неё просто нет предмета,
        # и сообщать об этом надо отдельно от чистого результата.
        print(t("проверять нечего: ни одного файла артефакта по указанным путям",
                "nothing to check: no artifact file under the given paths"), file=sys.stderr)
        return 1

    report = []
    failed = 0
    for path in targets:
        # Строго, как `inspect`: файл с `NaN` терпимый `read` отдал бы целым, и `check`
        # назвал бы «сходится» то, что `inspect` и `JSON.parse` читающей программы не примут.
        doc = artifacts.read_strict(path)
        # Вид — по имени файла, область — по каталогу: файл схемы под именем эффектов и
        # файл чужого контура в этом каталоге — расхождения, а не годные данные.
        laid_out = path in scopes
        expected = scopes.get(path)
        if not doc and artifacts.read(path):
            problems = [t("не строгий JSON: NaN или Infinity, которых не примет JSON.parse",
                          "not strict JSON: NaN or Infinity, which JSON.parse rejects")]
        else:
            problems = artifacts.validate(doc, kind=artifacts.kind_of(path), scope=expected)
        if laid_out and expected is None:
            problems.append(t(f"каталог области не по правилу: {artifacts.SCOPE_RULE_RU}",
                              f"the scope directory breaks the rule: {artifacts.SCOPE_RULE_EN}"))
        if problems:
            failed += 1
        report.append({"path": str(path), "problems": problems})

    if as_json:
        print(json.dumps(_printable_tree(report), ensure_ascii=False, indent=1))
    else:
        for item in report:
            if item["problems"]:
                print(f"{t('РАСХОЖДЕНИЯ', 'PROBLEMS   ')}  {_printable(item['path'])}")
                for problem in item["problems"]:
                    print(f"    · {_printable(problem)}")
            else:
                print(f"{t('сходится   ', 'conforms   ')} {_printable(item['path'])}")
        print(t(f"\nпрочитано файлов: {len(report)}, с расхождениями: {failed}",
                f"\nfiles read: {len(report)}, with problems: {failed}"))

    return EXIT_PROBLEMS if failed else 0


def _cmd_show(paths: Sequence[str], *, as_json: bool) -> int:
    # Несуществующий путь — команда не состоялась, а не «показала пустой артефакт»:
    # `read` снисходителен и отдал бы `{}`, а шапка из одних `None` с кодом 0 выглядела
    # как успех. Существующие пути при этом всё равно показываются.
    missing = [p for p in paths if not Path(p).exists()]
    for path in missing:
        print(_printable(t(f"нет такого пути: {path}", f"no such path: {path}")), file=sys.stderr)
    targets = _iter_paths([p for p in paths if p not in missing])
    if not targets:
        if not missing:
            print(t("показывать нечего: ни одного файла артефакта по указанным путям",
                    "nothing to show: no artifact file under the given paths"), file=sys.stderr)
        return 1

    rows = []
    unreadable = 0
    for path in targets:
        doc = artifacts.read_strict(path)  # строго, как `inspect` и `check`
        if not isinstance(doc, dict) or not doc.get("kind"):
            # Пустой, битый, нестрогий (`NaN`) или чужой JSON: шапка из `None` с кодом 0
            # выдала бы его за показанный артефакт.
            print(_printable(t(
                f"не артефакт (нет поля kind; пуст, битый, с NaN/Infinity или чужой JSON): {path}",
                f"not an artifact (no kind field; empty, broken, with NaN/Infinity or foreign "
                f"JSON): {path}")),
                file=sys.stderr)
            unreadable += 1
            continue
        # Списки записей — всё, что не служебная шапка: их размер и есть содержание.
        head = {"version", "kind", "scope", "collected_at", "collector"}
        payload = {k: v for k, v in doc.items() if k not in head}
        rows.append(
            {
                "path": str(path),
                "version": doc.get("version"),
                "kind": doc.get("kind"),
                "scope": doc.get("scope"),
                "collected_at": doc.get("collected_at"),
                "collector": doc.get("collector"),
                "payload": {
                    k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in payload.items()
                },
            }
        )

    if as_json:
        print(json.dumps(_printable_tree(rows), ensure_ascii=False, indent=1))
    else:
        for row in rows:
            scope = row["scope"] if isinstance(row["scope"], dict) else None
            print(_printable(row["path"]))
            if scope is None:
                # Шапка чужая или битая: `scope` не объект — сказать это, а не упасть.
                print(_printable(t(
                    f"    вид {row['kind']!r} · область в файле не объект ({type(row['scope']).__name__})",
                    f"    kind {row['kind']!r} · the scope in the file is not an object "
                    f"({type(row['scope']).__name__})",
                )))
                scope = {}
            print(_printable(t(
                f"    вид {row['kind']!r} · схема {row['version']} · "
                f"область {scope.get('system')}/{scope.get('env')} · "
                f"собрано {row['collected_at']} · сборщик {row['collector']!r}",
                f"    kind {row['kind']!r} · schema {row['version']} · "
                f"scope {scope.get('system')}/{scope.get('env')} · "
                f"collected {row['collected_at']} · collector {row['collector']!r}",
            )))
            for key, size in (row["payload"] or {}).items():
                print(_printable(f"    {key}: {size}"))
    return 1 if missing or unreadable else 0


def _cmd_wrap(command: Sequence[str]) -> int:
    """Выполнить команду и вернуть **её** код.

    Обёртка, подменяющая код команды, превращает провал прогона в успех у всех, кто на
    неё смотрит: у человека, у конвейера, у следующего шага сборки. Наблюдатель не имеет
    права менять исход того, за чем наблюдает.
    """
    from partest_atlas import wrap

    result = wrap.run(list(command))
    for note in result.notes:
        print(f"[atlas] wrap: {note}", file=sys.stderr)
    return result.code


# --- collect -------------------------------------------------------------------------


class _Refused(ValueError):
    """Отказ самой команды: аргументы или окружение не годятся."""


def _scope_part(value: Optional[str], env_name: str, flag: str) -> str:
    """Часть области — из ключа, иначе из окружения. Правило имени — у владельца формата
    (:func:`partest_atlas.artifacts.valid_scope_part`), то же, что у читающей программы:
    иначе артефакт лёг бы туда, где его никто не покажет. Значение в отказе не
    печатается — оно из чужого окружения."""
    import os  # noqa: PLC0415

    # Пустое значение — «не задано», как у сервера карты и у `ATLAS_SYSTEM=""` (договор
    # `atlas-config`: одинаковые правила во всех командах обоих пакетов).
    given = value if value else None
    raw = given if given is not None else (os.getenv(env_name) or DEFAULT_SCOPE_KEY)
    if not artifacts.valid_scope_part(raw):
        source = flag if given is not None else env_name
        raise _Refused(t(f"{source}: {artifacts.SCOPE_RULE_RU}", f"{source}: {artifacts.SCOPE_RULE_EN}"))
    return raw


def _project_on_path(root: Path) -> None:
    """Каталог проекта — **первым** в `sys.path`: `database.connect` и
    `container_logs.source` называют модули проекта, а у установленной команды текущего
    каталога в пути нет. Если каталог уже стоял в пути не первым, модуль с тем же именем
    из более раннего каталога затенил бы модуль проекта — поэтому он переносится вперёд."""
    text = str(root)
    while text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)


def _one_line(exc: BaseException) -> str:
    """Сообщение исключения одной строкой и через `partest.redact`: в тексте ошибки
    драйвера бывают адрес и имя пользователя, а stderr уходит в журнал конвейера."""
    from partest_atlas.adapters.postgres import (  # noqa: PLC0415
        ConnectionFailed,
        ConnectionNotConfigured,
        NotReadOnly,
    )
    from partest_atlas.collect.container_logs import SourceNotConfigured  # noqa: PLC0415
    from partest_atlas.collect.db_schema import NoTables  # noqa: PLC0415
    from partest_atlas.collect.openapi import SourceError  # noqa: PLC0415
    from partest_atlas.config import ConfigError  # noqa: PLC0415

    own = (_Refused, ConnectionNotConfigured, ConnectionFailed, NotReadOnly, SourceNotConfigured,
           NoTables, SourceError, ConfigError, ValueError)
    text = " ".join(str(exc).split()) or type(exc).__name__
    if not isinstance(exc, own):
        text = f"{type(exc).__name__}: {text}"
    try:
        from partest.redact import redact_text  # noqa: PLC0415
    except Exception:  # pragma: no cover - partest — зависимость пакета
        return text
    return redact_text(text)


def _printable(text: Any) -> str:
    """Строка без управляющих (категория Cc: C0, DEL, C1 — ESC-последовательности) и
    форматирующих символов (Cf: U+202E переворачивает строку в терминале и журнале) — они
    печатаются как `?`. Строки из чужой спецификации и чужих файлов идут только так."""
    import unicodedata  # noqa: PLC0415

    return "".join("?" if unicodedata.category(c) in ("Cc", "Cf") else c for c in str(text))


def _printable_tree(value: Any) -> Any:
    """То же для отчёта `--json`: строки — значения и ключи."""
    if isinstance(value, str):
        return _printable(value)
    if isinstance(value, dict):
        return {(_printable(k) if isinstance(k, str) else k): _printable_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_printable_tree(v) for v in value]
    return value


def _summary(what: str, payload: Dict[str, Any]) -> str:
    return _printable(_summary_text(what, payload))


def _summary_text(what: str, payload: Dict[str, Any]) -> str:
    stats = payload.get("stats") or {}
    if what == "openapi" and len(payload.get("services") or []) > 1:
        names = ", ".join(str(s.get("service")) for s in payload["services"])
        return t(
            f"операций {stats.get('operations')}, путей {stats.get('paths')} · "
            f"служб {len(payload['services'])}: {names}",
            f"operations {stats.get('operations')}, paths {stats.get('paths')} · "
            f"services {len(payload['services'])}: {names}",
        )
    if what == "openapi":
        info = payload.get("info") or {}
        source = payload.get("source") or {}
        title = " ".join(str(v) for v in (info.get("title"), info.get("version")) if v)
        return t(
            f"операций {stats.get('operations')}, путей {stats.get('paths')}"
            f"{' · ' + title if title else ''} · источник {source.get('service')} "
            f"({source.get('type')}: {source.get('location')})",
            f"operations {stats.get('operations')}, paths {stats.get('paths')}"
            f"{' · ' + title if title else ''} · source {source.get('service')} "
            f"({source.get('type')}: {source.get('location')})",
        )
    if what == "db-schema":
        schemas = ", ".join((payload.get("databases") or [{}])[0].get("schemas") or [])
        return t(
            f"таблиц {stats.get('tables')}, колонок {stats.get('columns')}, "
            f"внешних ключей {stats.get('foreignKeys')} · схемы {schemas}",
            f"tables {stats.get('tables')}, columns {stats.get('columns')}, "
            f"foreign keys {stats.get('foreignKeys')} · schemas {schemas}",
        )
    if what == "specs":
        return t(
            f"требований {stats.get('requirements')}, источников {stats.get('sources')}, "
            f"с якорями {stats.get('withAnchors')}",
            f"requirements {stats.get('requirements')}, sources {stats.get('sources')}, "
            f"with anchors {stats.get('withAnchors')}",
        )
    return t(
        f"контейнеров с находками {stats.get('containers')}, таблиц {stats.get('tables')}, "
        f"топиков {stats.get('topics')}",
        f"containers with findings {stats.get('containers')}, tables {stats.get('tables')}, "
        f"topics {stats.get('topics')}",
    )


#: Подкоманды `collect` в порядке, в котором их собирает голый `collect`.
COLLECTORS = ("openapi", "db-schema", "container-logs", "specs")


def _module(what: str) -> Any:
    from partest_atlas.collect import container_logs, db_schema, openapi, specs  # noqa: PLC0415

    return {"openapi": openapi, "db-schema": db_schema, "container-logs": container_logs,
            "specs": specs}[what]


def _configured(what: str, cfg: Any, root: Optional[Path]) -> bool:
    """Настроен ли сборщик. Голый `collect` ненастроенное пропускает, а не роняет."""
    if what == "openapi":
        return ((root or cfg.root) / "confpartest.py").is_file()
    if what == "db-schema":
        return "database" in cfg.raw
    if what == "container-logs":
        return "container_logs" in cfg.raw
    return "specs" in cfg.raw


def _collect_one(what: str, args: argparse.Namespace, cfg: Any, root: Optional[Path],
                 scope: Scope) -> Dict[str, Any]:
    """Снять одно наблюдение. Возвращает записанное (замаскированное) содержание."""
    notes: List[str] = []
    try:
        if what == "openapi":
            payload = _module(what).write(cfg, root=root, service=getattr(args, "service", None),
                                          scope=scope, notes=notes)
        else:
            _project_on_path(cfg.root)
            payload = _module(what).write(cfg, scope=scope)
    finally:
        for note in notes:
            print(f"[atlas] collect {what}: {_printable(note)}", file=sys.stderr)
    return payload


def _report(what: str, scope: Scope, cfg: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    module = _module(what)
    return {
        "kind": module.KIND,
        "path": str(scope.path(cfg.artifacts_dir, module.KIND)),
        "scope": scope.as_dict(),
        "stats": payload.get("stats"),
        **({"info": payload["info"]} if "info" in payload else {}),
        **({"source": payload["source"]} if "source" in payload else {}),
        **({"services": [s.get("service") for s in payload["services"]]}
           if "services" in payload else {}),
    }


def _cmd_collect(args: argparse.Namespace, *, as_json: bool) -> int:
    """`collect <что>` — одно наблюдение; голый `collect` — всё настроенное.

    Печатается то, что записано (после `partest.redact`), а не исходное: заголовок
    спецификации с `password=…` иначе ушёл бы в журнал конвейера, минуя маскировку.
    Неудача — одна строка в stderr и код 1; с `--json` в stdout — `{"error": …}`.
    """
    from partest_atlas import config  # noqa: PLC0415

    single = args.what
    label = f"collect {single}" if single else "collect"
    root_arg = getattr(args, "root", None)
    root = Path(root_arg).resolve() if root_arg else None

    def fail(what_label: str, exc: BaseException) -> Dict[str, Any]:
        reason = _one_line(exc)
        print(_printable(f"partest-atlas {what_label}: {reason}"), file=sys.stderr)
        return {"error": _printable(reason)}

    try:
        if root is not None and not root.is_dir():
            raise _Refused(t(f"--root {root_arg}: нет такого каталога",
                             f"--root {root_arg}: no such directory"))
        cfg = config.load(root)
        scope = Scope(
            system=_scope_part(getattr(args, "system", None), "ATLAS_SYSTEM", "--system"),
            env=_scope_part(getattr(args, "env", None), "ATLAS_ENV", "--env"),
        )
        for key in cfg.unknown_keys():
            # Договор `atlas-config`: неизвестный ключ своего раздела называется. Сбор не
            # останавливается — опечатка оставила бы умолчание, но не испортила бы запись.
            print(t(f"[atlas] atlas.yaml: неизвестный ключ {key} — не читается",
                    f"[atlas] atlas.yaml: unknown key {key} — not read"), file=sys.stderr)
    except Exception as exc:
        error = fail(label, exc)
        if as_json:
            print(json.dumps(error, ensure_ascii=False))
        return EXIT_COLLECT_FAILED

    whats = [single] if single else list(COLLECTORS)
    results: List[Dict[str, Any]] = []
    failed = False
    for what in whats:
        if not single and not _configured(what, cfg, root):
            print(t(f"[atlas] collect: {what} не настроен — пропущено",
                    f"[atlas] collect: {what} is not configured — skipped"), file=sys.stderr)
            continue
        try:
            payload = _collect_one(what, args, cfg, root, scope)
        except Exception as exc:
            failed = True
            results.append({"collector": what, **fail(f"collect {what}", exc)})
            continue
        report = _report(what, scope, cfg, payload)
        results.append({"collector": what, **report})
        if not as_json:
            print(f"{report['kind']}: {_summary(what, payload)} -> {_printable(report['path'])}")

    if as_json:
        body: Any = results[0] if single and results else {"results": results}
        if single and results:
            body = {k: v for k, v in body.items() if k != "collector"}
        print(json.dumps(_printable_tree(body), ensure_ascii=False, indent=1))
    return EXIT_COLLECT_FAILED if failed else 0


# --- разбор аргументов -----------------------------------------------------------------


def _pre_lang(argv: Sequence[str]) -> Optional[str]:
    """`--lang` до имени подкоманды — раньше, чем строится справка: она тоже на этом языке.

    Неизвестное значение здесь не ругается — его отклонит полный разбор, с обычным
    сообщением и кодом 2. После `--` и после подкоманды не смотрим: там чужие аргументы
    (`wrap -- prog --lang x`).
    """
    for i, token in enumerate(argv):
        if token == "--" or token in COMMANDS:
            return None
        value = None
        if token == "--lang" and i + 1 < len(argv):
            value = argv[i + 1]
        elif token.startswith("--lang="):
            value = token.split("=", 1)[1]
        if value is not None:
            return value if value in lang.SUPPORTED else None
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="partest-atlas",
        description=t(
            "Артефакты сбора: снять, посмотреть и проверить на соответствие контракту.",
            "Collected artifacts: collect, show and check them against the contract.",
        ),
    )
    parser.add_argument("--json", action="store_true",
                        help=t("вывод машине, а не человеку", "output for a machine, not a human"))
    parser.add_argument(
        "--lang",
        choices=lang.SUPPORTED,
        help=t("язык вывода; без ключа — по локали (LC_ALL, LC_MESSAGES, LANG)",
               "output language; without it — by locale (LC_ALL, LC_MESSAGES, LANG)"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    paths_help = t("файлы артефактов или каталоги с ними", "artifact files or directories with them")
    p_check = sub.add_parser("check", help=t("проверить артефакты на соответствие контракту",
                                             "check artifacts against the contract"))
    p_check.add_argument("paths", nargs="+", help=paths_help)

    p_show = sub.add_parser("show", help=t("показать шапку артефакта и размеры записей",
                                           "show the artifact header and record counts"))
    p_show.add_argument("paths", nargs="+", help=paths_help)

    p_wrap = sub.add_parser(
        "wrap",
        help=t("выполнить чужую команду и вернуть её код возврата",
               "run another command and return its exit code"),
        description=t(
            "Возвращается код возврата самой команды, а не обёртки. Снимков «до» и «после» "
            "команда пока не делает: она только запускает прогон.",
            "The exit code is the command's own, not the wrapper's. The command takes no "
            "'before' and 'after' snapshots yet: it only runs the suite.",
        ),
    )
    p_wrap.add_argument("rest", nargs=argparse.REMAINDER,
                        help=t("-- и сама команда", "-- and the command itself"))

    def common_options(suppress: bool) -> argparse.ArgumentParser:
        # У подкоманды умолчание — SUPPRESS: иначе её `None` затёр бы значение, заданное
        # у голого `collect` (`collect --root R openapi`).
        kw = {"default": argparse.SUPPRESS} if suppress else {}
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument(
            "--root", **kw,
            help=t("каталог проекта: от него ищется atlas.yaml (и confpartest.py для openapi); "
                   "по умолчанию — текущий",
                   "project directory: atlas.yaml (and confpartest.py for openapi) is looked up "
                   "from it; the current one by default"),
        )
        parser.add_argument("--system", **kw,
                            help=t("система; по умолчанию ATLAS_SYSTEM или default; пусто — не задано",
                                   "system; ATLAS_SYSTEM or default by default; empty — not set"))
        parser.add_argument("--env", **kw,
                            help=t("контур; по умолчанию ATLAS_ENV или default; пусто — не задано",
                                   "environment; ATLAS_ENV or default by default; empty — not set"))
        return parser

    common = common_options(suppress=True)

    p_collect = sub.add_parser(
        "collect",
        parents=[common_options(suppress=False)],
        help=t("снять наблюдение без прогона тестов и записать артефакт",
               "collect an observation that needs no test run and write the artifact"),
        description=t(
            "Пишет только в <artifacts_dir>/<система>/<контур>/. Без подкоманды — всё "
            "настроенное (openapi, если есть confpartest.py; db-schema, если есть раздел "
            "database; container-logs, если есть раздел container_logs; specs, если есть "
            "раздел specs), ненастроенное пропускается одной строкой. Неудача — одна строка "
            "в stderr и код 1; артефакт тогда не пишется.",
            "Writes only to <artifacts_dir>/<system>/<env>/. Without a subcommand — "
            "everything configured (openapi if there is confpartest.py; db-schema if there is "
            "a database section; container-logs if there is a container_logs section; specs if "
            "there is a specs section), the rest is skipped with one line. A failure is one "
            "line on stderr and exit code 1; no artifact is written then.",
        ),
    )
    what = p_collect.add_subparsers(dest="what", required=False)
    p_openapi = what.add_parser(
        "openapi",
        parents=[common],
        help=t("контракт из confpartest.swagger_files -> contract.openapi",
               "the contract from confpartest.swagger_files -> contract.openapi"),
        description=t(
            "Источник — confpartest.swagger_files проекта: локальный путь — от текущего "
            "каталога, иначе от каталога confpartest.py, адрес — partest под его политикой TLS. Без --service — все "
            "службы; с --service — только она, остальные в артефакте сохраняются. "
            "confpartest.py импортируется, но не так, как при прогоне pytest: без conftest.py, "
            "pythonpath из pytest.ini, env из pytest-env и .env.",
            "The source is the project's confpartest.swagger_files: a local path is taken "
            "from the current directory, else from the directory of confpartest.py, a URL is fetched by partest under its TLS "
            "policy. Without --service — every service; with --service — that one only, the "
            "others in the artifact are kept. confpartest.py is imported, but not as in a "
            "pytest run: no conftest.py, pythonpath from pytest.ini, env from pytest-env or .env.",
        ),
    )
    p_openapi.add_argument(
        "--service",
        help=t("ключ в swagger_files: обновить только эту службу. Два вызова с разными "
               "--service одной области одновременно теряют службу — собирайте все службы "
               "одним вызовом без --service или по очереди",
               "key in swagger_files: update this service only. Two concurrent calls with "
               "different --service for one scope lose a service — collect every service "
               "in one call without --service, or one after another"),
    )
    what.add_parser(
        "db-schema",
        parents=[common],
        help=t("схема базы из раздела database -> db.schema (только чтение)",
               "the database schema from the database section -> db.schema (read-only)"),
    )
    what.add_parser(
        "container-logs",
        parents=[common],
        help=t("связи из журналов контейнеров -> containers.logs; нужен container_logs.source",
               "links from container logs -> containers.logs; needs container_logs.source"),
    )
    what.add_parser(
        "specs",
        parents=[common],
        help=t("требования из .docx раздела specs -> specs.requirements",
               "requirements from the .docx files in the specs section -> specs.requirements"),
    )
    return parser


def _tolerant_stdout() -> Optional[Any]:
    """Вывод в перенаправленный поток на Windows идёт в кодовой странице ANSI, и знак,
    которого в ней нет (кириллица под `--lang en` на cp1252), ронял команду **после**
    записи артефакта. Неотображаемое — экранируется, как у stderr по умолчанию.

    Возвращает, как вернуть прежнее (``main`` возвращает после вызова): код, позвавший
    `main` сам, получает свой поток таким, каким отдал."""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    previous = getattr(stream, "errors", None)
    if reconfigure is None or previous is None:
        return None
    try:
        reconfigure(errors="backslashreplace")
    except Exception:  # pragma: no cover - чужой поток (pytest capture и т. п.)
        return None

    def restore() -> None:
        try:
            reconfigure(errors=previous)
        except Exception:  # pragma: no cover
            pass

    return restore


def main(argv: Iterable[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    restore_stdout = _tolerant_stdout()
    previous = lang.current()
    # Язык — на время вызова: код, который зовёт функции пакета сам, получает прежний.
    lang.set_lang(_pre_lang(raw) or lang.detect())
    try:
        return _main(raw)
    finally:
        lang.set_lang(previous)
        if restore_stdout is not None:
            restore_stdout()


def _main(raw: List[str]) -> int:
    args = _parser().parse_args(raw)

    if args.command == "check":
        return _cmd_check(args.paths, as_json=args.json)
    if args.command == "wrap":
        # Ведущий `--` argparse оставляет в остатке; он разделитель обёртки, команде не
        # нужен. Остальные `--` — команды: `npm test -- --grep smoke`.
        rest = list(args.rest)
        return _cmd_wrap(rest[1:] if rest[:1] == ["--"] else rest)
    if args.command == "collect":
        return _cmd_collect(args, as_json=args.json)
    return _cmd_show(args.paths, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
