"""`partest-atlas wrap -- <команда>` — обёртка вокруг чужого прогона.

Зачем. Плагины работают внутри pytest. Но прогон бывает и не pytest: нагрузочный сценарий,
чужой набор на другом языке, ручная проверка. Задумано, что обёртка даёт то же наблюдение —
что прогон сделал с базой и что прошло по шине, — не зная о нём ничего, кроме командной
строки. :func:`run` для этого принимает снимки «до» и «после» от вызывающего, но **команда
`partest-atlas wrap` их пока не передаёт**: сегодня она только выполняет команду и отдаёт
её код возврата. Снимки из CLI — не сделанная возможность, а не скрытая.

**Код возврата отдаётся чужой, а не свой.** Это не мелочь и не вежливость: обёртка,
подменяющая код команды, превращает провал прогона в успех у всех, кто на неё смотрит, —
у человека, у конвейера, у следующего шага сборки. Наблюдатель не имеет права менять
исход того, за чем наблюдает.

Из того же правила: неудача **сбора** не портит код возврата команды. Если прогон прошёл,
а снять наблюдение не удалось, это неудача карты, а не прогона, и сказать об этом надо
словами, не кодом.
"""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

#: Код, которым отвечаем, когда команду не удалось даже запустить. Отдельный от кодов
#: самой команды настолько, насколько возможно: «не запустилось» и «отработало плохо» —
#: разные новости.
EXIT_CANNOT_RUN = 127


#: Что Windows исполняет только через `cmd.exe`. Без оболочки `npm`, `gradlew` и прочие
#: обёртки-скрипты не запускались вовсе: CreateProcess не ищет `.cmd` по `PATHEXT`.
_BATCH_SUFFIXES = (".bat", ".cmd")

#: Что запускается без оболочки. Всё прочее, что нашлось, — не исполняемый файл.
_BINARY_SUFFIXES = (".com", ".exe")

#: Внутри кавычек `cmd.exe` эти символы не трогает — аргумент с ними берётся в кавычки.
_CMD_QUOTE_IF = set(" \t(),;=")

#: А эти скрипту не передать безопасно. `%` раскрывает переменную, `"` закрывает
#: кавычку, перевод строки обрывает команду — это ещё на уровне `cmd /c`. `& | < > ^`
#: кавычки обезвреживают только до тех пор, пока скрипт их не снял: `%~1` в самом
#: скрипте возвращает голый `&`, и он становится второй командой (класс BatBadBut).
#: Аргумент с ними отклоняется, а не «экранируется как-нибудь».
_CMD_REFUSE = set('"%\r\n\x00&|<>^')

#: Для пути к самому скрипту — только то, что ломает кавычки `cmd /c`: путь всегда в
#: кавычках, и скрипт его как аргумент не разбирает.
_CMD_REFUSE_IN_PATH = set('"%\r\n\x00')

_HINT = "вызовите интерпретатор напрямую, без скрипта-обёртки: `node …`, `java …`, `python …`"


class _Refused(ValueError):
    """Команду нельзя безопасно запустить так, как её назвали."""


def _suffix(path: str) -> str:
    """Расширение так, как его увидит Windows: хвостовые точки и пробелы она отбрасывает,
    и `x.cmd.` или `x.cmd ` — это всё тот же скрипт."""
    return ntpath.splitext(path.rstrip(" ."))[1].lower()


def _has_dir(name: str) -> bool:
    return bool(ntpath.dirname(name)) or ":" in name


def _candidates(name: str) -> List[str]:
    if _suffix(name):
        return [name]
    return [name + ext for ext in (*_BINARY_SUFFIXES, *_BATCH_SUFFIXES)]


def _found_nearby(
    name: str,
    *,
    path_env: str,
    cwd: str,
    isfile: Callable[[str], bool],
) -> Optional[str]:
    """Нашёл бы CreateProcess голое имя в текущем каталоге или по относительной записи
    PATH? Возвращает, где именно, или `None`.

    Кандидаты — с `.com .exe .bat .cmd` и без расширения: CreateProcess сам дописывает
    только `.exe`, но скрипт он тоже исполнит, если его так назвать.
    """
    folders = [cwd]
    for raw in path_env.split(";"):
        entry = raw.strip().strip('"')
        if entry and not ntpath.isabs(entry):
            folders.append(ntpath.join(cwd, entry))
    names = [name, *(name + ext for ext in (*_BINARY_SUFFIXES, *_BATCH_SUFFIXES))]
    for folder in folders:
        for candidate in names:
            full = ntpath.join(folder, candidate)
            if isfile(full):
                return full
    return None


def _system_cmd(system_root: Optional[str]) -> str:
    """`cmd.exe` из системного каталога, а не из `COMSPEC`: тот может указывать на другую
    оболочку (TCC/4NT разбирает строку иначе) или быть относительным — и тогда
    CreateProcess нашёл бы `cmd.exe` в текущем каталоге."""
    root = (system_root or "").strip()
    if not root or not ntpath.isabs(root):
        raise _Refused("SystemRoot не задан или не абсолютный — системный cmd.exe не найти")
    return ntpath.join(root, "System32", "cmd.exe")


def _resolve(
    name: str,
    *,
    path_env: str,
    cwd: str,
    isfile: Callable[[str], bool],
) -> Optional[str]:
    """Найти команду так, как её запустит Windows, но **без текущего каталога**.

    `shutil.which` на Windows смотрит в текущий каталог раньше `PATH`, и голое `npm` в
    чужом клоне находило бы `.\\npm.cmd` оттуда. Имя с каталогом — явный выбор
    вызывающего, ищется как названо.
    """
    candidates = _candidates(name)
    if _has_dir(name):
        dirs = [""]
    else:
        dirs = []
        for raw in path_env.split(";"):
            entry = raw.strip().strip('"')
            # Относительная запись PATH — тот же текущий каталог под другим именем.
            # Абсолютная — выбор владельца PATH, даже если совпала с текущим каталогом:
            # `curl` из System32 или `node` из каталога nodejs, запущенные оттуда же,
            # находятся как обычно.
            if not entry or not ntpath.isabs(entry):
                continue
            dirs.append(entry)
    for folder in dirs:
        for candidate in candidates:
            full = ntpath.join(folder, candidate) if folder else candidate
            if isfile(full):
                return full
    return None


def _cmd_quote(arg: str, number: int = 0) -> str:
    bad = sorted(set(arg) & _CMD_REFUSE)
    if bad:
        # Значение не печатается: отказанным аргументом бывает пароль, а сообщение
        # уходит в stderr и лог конвейера. Номер и символы — достаточно, чтобы найти.
        shown = " ".join(repr(c) for c in bad)
        raise _Refused(
            f"аргумент №{number} содержит {shown}: скрипту .cmd/.bat его не передать "
            f"безопасно — кавычки снимает и cmd.exe, и сам скрипт; {_HINT}"
        )
    if arg and not set(arg) & _CMD_QUOTE_IF:
        return arg
    # Хвостовые `\` перед закрывающей кавычкой удваиваются по правилам MSVCRT, как у
    # `subprocess.list2cmdline`: иначе `"…\dir name\"` читается программой за скриптом
    # как экранированная кавычка и склеивает следующие аргументы.
    tail = len(arg) - len(arg.rstrip("\\"))
    return f'"{arg}{chr(92) * tail}"'


def _quote_path(path: str) -> str:
    bad = sorted(set(path) & _CMD_REFUSE_IN_PATH)
    if bad:
        raise _Refused(f"путь {path!r} содержит {' '.join(map(repr, bad))}: через cmd.exe не запустить")
    return f'"{path}"'


def _command_line(
    command: Sequence[str],
    *,
    platform: Optional[str] = None,
    path_env: Optional[str] = None,
    cwd: Optional[str] = None,
    isfile: Callable[[str], bool] = os.path.isfile,
    system_root: Optional[str] = None,
) -> Union[List[str], str]:
    """Во что превратить команду перед запуском. Отказ — :class:`_Refused`.

    Везде, кроме Windows, — список как есть, без оболочки. На Windows команда сначала
    ищется (:func:`_resolve`: имя с каталогом — как названо, голое — по абсолютным
    записям PATH, без текущего каталога), и дальше по расширению:

    ``.exe``/``.com``
        список без оболочки, с найденным путём на месте имени;
    ``.cmd``/``.bat`` (в том числе ``x.cmd.`` и ``x.cmd ``)
        системный ``cmd.exe`` из ``System32`` под ``%SystemRoot%`` с ключами
        ``/d /v:off /s /c`` (не ``COMSPEC``) со строкой,
        собранной здесь, а не ``shell=True``. **Граница гарантии:** аргумент с пробелом,
        табуляцией, ``( ) , ; =`` или пустой берётся в кавычки, хвостовые ``\\`` в нём
        удваиваются (скрипт с ``%~1`` увидит их удвоенными); аргумент с
        ``% " & | < > ^``, переводом строки или NUL отклоняется. Кавычки не спасают от
        скрипта, который снимает их сам (``%~1``), — поэтому метасимволы не экранируются,
        а не пропускаются вовсе. ``!`` пропускается: ``/v:off`` выключает отложенное
        раскрытие для строки ``cmd /c``, но скрипт, включивший его сам
        (``setlocal enabledelayedexpansion``), раскроет ``!x!`` в своих аргументах;
    всё прочее, что нашлось
        отказ: это не исполняемый файл;
    не нашлось
        голое имя, которое CreateProcess нашёл бы в текущем каталоге или по
        относительной записи PATH, — отказ с просьбой назвать путём (``.\\gradlew``);
        голое имя ``.cmd``/``.bat`` — отказ: CreateProcess нашёл бы его в каталоге
        приложения или системном и запустил через неявный ``cmd.exe``. Остальное —
        список как есть: CreateProcess найдёт ``.exe`` в каталоге приложения или
        системном, как раньше, или скажет «не найдена».
    """
    argv = list(command)
    if not (platform or sys.platform).startswith("win") or not argv:
        return argv
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    cwd = os.getcwd() if cwd is None else cwd
    found = _resolve(argv[0], path_env=path_env, cwd=cwd, isfile=isfile)
    if found is None:
        if _has_dir(argv[0]):
            if _suffix(argv[0]) in _BATCH_SUFFIXES:
                raise _Refused(f"скрипт {argv[0]!r} не найден по указанному пути")
            return argv
        nearby = _found_nearby(argv[0], path_env=path_env, cwd=cwd, isfile=isfile)
        if nearby is not None:
            raise _Refused(
                f"{argv[0]!r} нашёлся только в текущем каталоге или по относительной записи "
                f"PATH ({nearby}) — такое не запускается по голому имени; назовите путём: "
                f"`.\\{argv[0]}`"
            )
        if _suffix(argv[0]) in _BATCH_SUFFIXES:
            raise _Refused(f"скрипт {argv[0]!r} не найден в PATH; назовите его путём")
        return argv
    suffix = _suffix(found)
    if suffix in _BINARY_SUFFIXES:
        return [found, *argv[1:]]
    if suffix not in _BATCH_SUFFIXES:
        raise _Refused(f"{found!r} — не исполняемый файл Windows (.exe, .com, .cmd, .bat)")
    inner = " ".join(
        [_quote_path(found.rstrip(" .")), *(_cmd_quote(a, n) for n, a in enumerate(argv[1:], 1))]
    )
    shell = _system_cmd(os.environ.get("SystemRoot") if system_root is None else system_root)
    return f'{_quote_path(shell)} /d /v:off /s /c "{inner}"'


class WrapResult:
    """Что получилось: код команды и что удалось снять."""

    def __init__(self, code: int, records: Dict[str, List[dict]], notes: List[str]) -> None:
        self.code = code
        self.records = records
        self.notes = notes


def run(
    command: Sequence[str],
    *,
    before: Optional[Callable[[], Any]] = None,
    after: Optional[Callable[[Any], Dict[str, List[dict]]]] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> WrapResult:
    """Снять «до», выполнить команду, снять «после».

    `before` и `after` передаются вызывающим — обёртка не решает, что именно снимается.
    """
    notes: List[str] = []
    if not command:
        return WrapResult(EXIT_CANNOT_RUN, {}, ["команда не задана — оборачивать нечего"])

    try:
        prepared = _command_line(command)
    except _Refused as exc:
        return WrapResult(EXIT_CANNOT_RUN, {}, [f"команда не запущена: {exc}"])

    state = None
    if before is not None:
        try:
            state = before()
        except Exception as exc:
            notes.append(f"снимок «до» не снялся ({type(exc).__name__}: {exc})")

    try:
        completed = runner(prepared)
        code = int(getattr(completed, "returncode", 1))
    except FileNotFoundError:
        return WrapResult(EXIT_CANNOT_RUN, {}, [*notes, f"команда не найдена: {command[0]}"])
    except Exception as exc:
        return WrapResult(EXIT_CANNOT_RUN, {}, [*notes, f"команда не запустилась: {exc}"])

    records: Dict[str, List[dict]] = {}
    if after is not None and state is not None:
        try:
            records = after(state) or {}
        except Exception as exc:
            # Сбор провалился — прогон здесь ни при чём, и его код остаётся прежним.
            notes.append(f"снимок «после» не снялся ({type(exc).__name__}: {exc})")

    return WrapResult(code, records, notes)
