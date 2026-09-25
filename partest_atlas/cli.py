"""Команда `partest-atlas`: посмотреть артефакт и проверить его на соответствие контракту.

Точка входа была объявлена в `pyproject.toml` раньше, чем появился этот модуль, и
установка пакета давала неработающую команду. Здесь она появляется, и содержание у неё
выбрано не случайное: единственное, что в пакете уже есть и что уже можно сломать
необратимо, — это **формат артефактов**. Его читает чужая программа, и у неё нет способа
проверить себя, кроме как этой командой.

Ничего не пишет и не изменяет: обе подкоманды только читают.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Sequence

from partest_atlas import artifacts

#: Код возврата, когда расхождения найдены. Отдельный от `1`, потому что `1` достаётся
#: неудаче самой команды (файл не читается, аргументы кривые) — а это разные новости:
#: «проверка нашла» и «проверка не состоялась» нельзя сводить к одному числу.
EXIT_PROBLEMS = 2


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
        print("проверять нечего: ни одного файла артефакта по указанным путям", file=sys.stderr)
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
                print(f"РАСХОЖДЕНИЯ  {item['path']}")
                for problem in item["problems"]:
                    print(f"    · {problem}")
            else:
                print(f"сходится    {item['path']}")
        print(f"\nпрочитано файлов: {len(report)}, с расхождениями: {failed}")

    return EXIT_PROBLEMS if failed else 0


def _cmd_show(paths: Sequence[str], *, as_json: bool) -> int:
    targets = _iter_paths(paths)
    if not targets:
        print("показывать нечего: ни одного файла артефакта по указанным путям", file=sys.stderr)
        return 1

    rows = []
    for path in targets:
        doc = artifacts.read(path)
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
            print(
                f"    вид {row['kind']!r} · схема {row['version']} · "
                f"область {scope.get('system')}/{scope.get('env')} · "
                f"собрано {row['collected_at']} · сборщик {row['collector']!r}"
            )
            for key, size in (row["payload"] or {}).items():
                print(f"    {key}: {size}")
    return 0


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


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="partest-atlas",
        description="Артефакты сбора: посмотреть и проверить на соответствие контракту.",
    )
    parser.add_argument("--json", action="store_true", help="вывод машине, а не человеку")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="проверить артефакты на соответствие контракту")
    p_check.add_argument("paths", nargs="+", help="файлы артефактов или каталоги с ними")

    p_show = sub.add_parser("show", help="показать шапку артефакта и размеры записей")
    p_show.add_argument("paths", nargs="+", help="файлы артефактов или каталоги с ними")

    p_wrap = sub.add_parser(
        "wrap",
        help="выполнить чужую команду и снять наблюдения вокруг неё",
        description="Код возврата отдаётся команды, а не обёртки.",
    )
    p_wrap.add_argument("rest", nargs=argparse.REMAINDER, help="-- и сама команда")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "check":
        return _cmd_check(args.paths, as_json=args.json)
    if args.command == "wrap":
        # Ведущий `--` argparse оставляет в остатке; команде он не нужен.
        return _cmd_wrap([a for a in args.rest if a != "--"])
    return _cmd_show(args.paths, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
