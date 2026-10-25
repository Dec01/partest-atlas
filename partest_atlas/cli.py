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

#: Код возврата `collect`, когда снять не удалось: одна строка в stderr и `2`, как у
#: разбора аргументов. Артефакт в этом случае не пишется и прежний не трогается.
EXIT_COLLECT_FAILED = 2

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


def _cmd_check(paths: Sequence[str], *, as_json: bool) -> int:
    targets = _iter_paths(paths)
    if not targets:
        # Проверка, которой нечего смотреть, не «прошла» — у неё просто нет предмета,
        # и сообщать об этом надо отдельно от чистого результата.
        print(t("проверять нечего: ни одного файла артефакта по указанным путям",
                "nothing to check: no artifact file under the given paths"), file=sys.stderr)
        return 1

    report = []
    failed = 0
    for path in targets:
        doc = artifacts.read(path)
        problems = artifacts.validate(doc)
        if problems:
            failed += 1
        report.append({"path": str(path), "problems": problems})

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for item in report:
            if item["problems"]:
                print(f"{t('РАСХОЖДЕНИЯ', 'PROBLEMS   ')}  {item['path']}")
                for problem in item["problems"]:
                    print(f"    · {problem}")
            else:
                print(f"{t('сходится   ', 'conforms   ')} {item['path']}")
        print(t(f"\nпрочитано файлов: {len(report)}, с расхождениями: {failed}",
                f"\nfiles read: {len(report)}, with problems: {failed}"))

    return EXIT_PROBLEMS if failed else 0


def _cmd_show(paths: Sequence[str], *, as_json: bool) -> int:
    # Несуществующий путь — команда не состоялась, а не «показала пустой артефакт»:
    # `read` снисходителен и отдал бы `{}`, а шапка из одних `None` с кодом 0 выглядела
    # как успех. Существующие пути при этом всё равно показываются.
    missing = [p for p in paths if not Path(p).exists()]
    for path in missing:
        print(t(f"нет такого пути: {path}", f"no such path: {path}"), file=sys.stderr)
    targets = _iter_paths([p for p in paths if p not in missing])
    if not targets:
        if not missing:
            print(t("показывать нечего: ни одного файла артефакта по указанным путям",
                    "nothing to show: no artifact file under the given paths"), file=sys.stderr)
        return 1

    rows = []
    unreadable = 0
    for path in targets:
        doc = artifacts.read(path)
        if not isinstance(doc, dict) or not doc.get("kind"):
            # Пустой, битый или чужой JSON: `read` снисходителен и отдал бы его как есть,
            # а шапка из `None` с кодом 0 выдала бы его за показанный артефакт.
            print(t(f"не артефакт (нет поля kind; пуст, битый или чужой JSON): {path}",
                    f"not an artifact (no kind field; empty, broken or foreign JSON): {path}"),
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
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for row in rows:
            scope = row["scope"] or {}
            print(f"{row['path']}")
            print(t(
                f"    вид {row['kind']!r} · схема {row['version']} · "
                f"область {scope.get('system')}/{scope.get('env')} · "
                f"собрано {row['collected_at']} · сборщик {row['collector']!r}",
                f"    kind {row['kind']!r} · schema {row['version']} · "
                f"scope {scope.get('system')}/{scope.get('env')} · "
                f"collected {row['collected_at']} · collector {row['collector']!r}",
            ))
            for key, size in (row["payload"] or {}).items():
                print(f"    {key}: {size}")
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
    """Часть области — из ключа, иначе из окружения. Это имя каталога, а не путь: `..` или
    разделитель вывели бы запись за пределы каталога артефактов."""
    import os  # noqa: PLC0415

    raw = value if value is not None else (os.getenv(env_name) or DEFAULT_SCOPE_KEY)
    if not raw or raw in {".", ".."} or any(c in raw for c in "/\\:") or raw != raw.strip():
        raise _Refused(t(
            f"{flag} / {env_name}={raw!r}: ожидалось имя каталога без разделителей и `..`",
            f"{flag} / {env_name}={raw!r}: expected a directory name without separators or `..`",
        ))
    return raw


def _project_on_path(root: Path) -> None:
    """Каталог проекта — в начало `sys.path`: `database.connect` и `container_logs.source`
    называют модули проекта, а у установленной команды текущего каталога в пути нет."""
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)


def _one_line(exc: BaseException) -> str:
    """Сообщение исключения одной строкой и через `partest.redact`: в тексте ошибки
    драйвера бывают адрес и имя пользователя, а stderr уходит в журнал конвейера."""
    from partest_atlas.adapters.postgres import ConnectionNotConfigured, NotReadOnly  # noqa: PLC0415
    from partest_atlas.collect.container_logs import SourceNotConfigured  # noqa: PLC0415
    from partest_atlas.collect.db_schema import NoTables  # noqa: PLC0415
    from partest_atlas.collect.openapi import SourceError  # noqa: PLC0415
    from partest_atlas.config import ConfigError  # noqa: PLC0415

    own = (_Refused, ConnectionNotConfigured, NotReadOnly, SourceNotConfigured, NoTables,
           SourceError, ConfigError, ValueError)
    text = " ".join(str(exc).split()) or type(exc).__name__
    if not isinstance(exc, own):
        text = f"{type(exc).__name__}: {text}"
    try:
        from partest.redact import redact_text  # noqa: PLC0415
    except Exception:  # pragma: no cover - partest — зависимость пакета
        return text
    return redact_text(text)


def _summary(what: str, payload: Dict[str, Any]) -> str:
    stats = payload.get("stats") or {}
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
    return t(
        f"контейнеров с находками {stats.get('containers')}, таблиц {stats.get('tables')}, "
        f"топиков {stats.get('topics')}",
        f"containers with findings {stats.get('containers')}, tables {stats.get('tables')}, "
        f"topics {stats.get('topics')}",
    )


def _cmd_collect(args: argparse.Namespace, *, as_json: bool) -> int:
    from partest_atlas import config  # noqa: PLC0415
    from partest_atlas.collect import container_logs, db_schema, openapi  # noqa: PLC0415

    collectors = {
        "openapi": openapi,
        "db-schema": db_schema,
        "container-logs": container_logs,
    }
    module = collectors[args.what]
    root = Path(args.root).resolve() if args.root else None
    try:
        if root is not None and not root.is_dir():
            raise _Refused(t(f"--root {args.root}: нет такого каталога",
                             f"--root {args.root}: no such directory"))
        cfg = config.load(root)
        scope = Scope(
            system=_scope_part(args.system, "ATLAS_SYSTEM", "--system"),
            env=_scope_part(args.env, "ATLAS_ENV", "--env"),
        )
        if args.what == "openapi":
            payload = openapi.write(cfg, root=root, service=args.service, scope=scope)
        else:
            _project_on_path(cfg.root)
            payload = module.write(cfg, scope=scope)
    except Exception as exc:
        print(f"partest-atlas collect {args.what}: {_one_line(exc)}", file=sys.stderr)
        return EXIT_COLLECT_FAILED

    path = scope.path(cfg.artifacts_dir, module.KIND)
    if as_json:
        print(json.dumps(
            {
                "kind": module.KIND,
                "path": str(path),
                "scope": scope.as_dict(),
                "stats": payload.get("stats"),
                **({"info": payload["info"]} if "info" in payload else {}),
                **({"source": payload["source"]} if "source" in payload else {}),
            },
            ensure_ascii=False,
            indent=1,
        ))
    else:
        print(f"{module.KIND}: {_summary(args.what, payload)} -> {path}")
    return 0


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

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        help=t("каталог проекта: от него ищется atlas.yaml (и confpartest.py для openapi); "
               "по умолчанию — текущий",
               "project directory: atlas.yaml (and confpartest.py for openapi) is looked up "
               "from it; the current one by default"),
    )
    common.add_argument("--system", help=t("система; по умолчанию ATLAS_SYSTEM или default",
                                           "system; ATLAS_SYSTEM or default by default"))
    common.add_argument("--env", help=t("контур; по умолчанию ATLAS_ENV или default",
                                        "environment; ATLAS_ENV or default by default"))

    p_collect = sub.add_parser(
        "collect",
        help=t("снять наблюдение без прогона тестов и записать артефакт",
               "collect an observation that needs no test run and write the artifact"),
        description=t(
            "Пишет только в <artifacts_dir>/<система>/<контур>/. Неудача — одна строка в "
            "stderr и код 2; артефакт тогда не пишется.",
            "Writes only to <artifacts_dir>/<system>/<env>/. A failure is one line on "
            "stderr and exit code 2; no artifact is written then.",
        ),
    )
    what = p_collect.add_subparsers(dest="what", required=True)
    p_openapi = what.add_parser(
        "openapi",
        parents=[common],
        help=t("контракт из confpartest.swagger_files → contract.openapi",
               "the contract from confpartest.swagger_files -> contract.openapi"),
        description=t(
            "Источник — confpartest.swagger_files проекта: локальный путь — от каталога "
            "confpartest.py, адрес — клиентом partest под его политикой TLS. confpartest.py "
            "импортируется, как при прогоне pytest.",
            "The source is the project's confpartest.swagger_files: a local path is taken "
            "from the directory of confpartest.py, a URL is fetched with partest's client "
            "under its TLS policy. confpartest.py is imported, as in a pytest run.",
        ),
    )
    p_openapi.add_argument(
        "--service",
        help=t("ключ в swagger_files; обязателен, если спецификаций несколько",
               "key in swagger_files; required when there are several specifications"),
    )
    what.add_parser(
        "db-schema",
        parents=[common],
        help=t("схема базы из раздела database → db.schema (только чтение)",
               "the database schema from the database section -> db.schema (read-only)"),
    )
    what.add_parser(
        "container-logs",
        parents=[common],
        help=t("связи из журналов контейнеров → containers.logs; нужен container_logs.source",
               "links from container logs -> containers.logs; needs container_logs.source"),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    previous = lang.current()
    # Язык — на время вызова: код, который зовёт функции пакета сам, получает прежний.
    lang.set_lang(_pre_lang(raw) or lang.detect())
    try:
        return _main(raw)
    finally:
        lang.set_lang(previous)


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
