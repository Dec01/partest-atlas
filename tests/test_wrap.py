"""Обёртка вокруг чужого прогона: чужой код возврата остаётся чужим."""

from __future__ import annotations

import ntpath
import os
import sys
from unittest import mock

import pytest

from partest_atlas import wrap


class Результат:
    def __init__(self, code):
        self.returncode = code


@pytest.mark.parametrize("код", [0, 1, 2, 5, 127])
def test_the_wrapped_command_keeps_its_exit_code(код):
    """Обёртка, подменяющая код, превращает провал в успех у всех, кто на неё смотрит."""
    r = wrap.run(["что-то"], runner=lambda cmd: Результат(код))
    assert r.code == код


def test_a_failed_collection_does_not_spoil_a_successful_run():
    """Прогон прошёл, снять наблюдение не удалось — это неудача карты, а не прогона."""

    def падает(state):
        raise RuntimeError("источник исчез")

    r = wrap.run(
        ["что-то"],
        before=lambda: "состояние",
        after=падает,
        runner=lambda cmd: Результат(0),
    )
    assert r.code == 0
    assert any("не снялся" in n for n in r.notes)


def test_a_failed_before_snapshot_still_runs_the_command():
    """Иначе недоступная база отменяла бы прогон, ради которого всё и затевалось."""
    запущено = []

    def падает():
        raise RuntimeError("базы нет")

    r = wrap.run(["что-то"], before=падает, runner=lambda cmd: запущено.append(cmd) or Результат(0))
    assert запущено
    assert r.code == 0
    assert any("«до»" in n for n in r.notes)


def test_a_missing_command_is_its_own_code():
    """«Не запустилось» и «отработало плохо» — разные новости."""

    def нет_такой(cmd):
        raise FileNotFoundError(cmd[0])

    r = wrap.run(["нет-такой-команды"], runner=нет_такой)
    assert r.code == wrap.EXIT_CANNOT_RUN
    assert any("не найдена" in n for n in r.notes)


def test_an_empty_command_is_refused():
    r = wrap.run([])
    assert r.code == wrap.EXIT_CANNOT_RUN
    assert r.records == {}


def test_records_come_from_the_after_snapshot():
    r = wrap.run(
        ["что-то"],
        before=lambda: "с",
        after=lambda state: {"db.effects": [{"table": "t"}]},
        runner=lambda cmd: Результат(0),
    )
    assert r.records == {"db.effects": [{"table": "t"}]}


def test_without_a_before_there_is_no_after():
    """Дельта без «до» — не наблюдение, а содержимое счётчиков за всю жизнь системы."""
    звали = []
    r = wrap.run(["что-то"], after=lambda s: звали.append(s) or {}, runner=lambda cmd: Результат(0))
    assert звали == []
    assert r.records == {}


# --- сквозь команду --------------------------------------------------------


@pytest.mark.parametrize("код", [0, 1, 3, 7])
def test_the_exit_code_survives_the_whole_command_line(код, tmp_path):
    """Модульных проверок мало: код теряется не в `run`, а по дороге через разбор
    аргументов. Здесь это и поймалось — остаток командной строки затирал имя
    подкоманды, и обёртка молча уходила в другую ветку, возвращая единицу на всё.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "partest_atlas.cli", "wrap", "--",
         sys.executable, "-c", f"raise SystemExit({код})"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == код, result.stderr[-500:]


def test_a_command_that_cannot_start_gets_its_own_code_end_to_end():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "partest_atlas.cli", "wrap", "--", "нет-такой-команды"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == wrap.EXIT_CANNOT_RUN


def test_only_the_leading_double_dash_is_dropped():
    """`npm test -- --grep smoke`: второй `--` принадлежит команде, а не обёртке."""
    import subprocess

    проверка = "import sys; sys.exit(0 if sys.argv[1:] == ['--', 'x'] else 3)"
    result = subprocess.run(
        [sys.executable, "-m", "partest_atlas.cli", "wrap", "--",
         sys.executable, "-c", проверка, "--", "x"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-500:]


# --- скрипты Windows: .cmd и .bat ------------------------------------------
#
# Без оболочки CreateProcess не находит `npm` (это `npm.cmd`), а скрипт, названный путём,
# запускает через неявный `cmd.exe` с аргументами, которые тот разбирает по-своему.
# Строка для `cmd.exe` собирается из списка; то, что кавычки не спасают, отклоняется.

# Буква диска вынесена в одну константу: пути ниже — примерные пути Windows для фейковой
# файловой системы, а не пути чьей-то машины.
ДИСК = "C:"
КОРЕНЬ = rf"{ДИСК}\Windows"
КОМАНДНЫЙ = rf"{ДИСК}\Windows\System32\cmd.exe"


def _фс(*файлы):
    """Файловая система Windows в миниатюре: регистр не важен, хвостовые `.` и пробел
    отбрасываются — как у настоящей."""
    есть = {ntpath.normcase(f) for f in файлы}
    return lambda p: ntpath.normcase(p.rstrip(" .")) in есть


def _как_на_windows(command, *файлы, path_env=rf"{ДИСК}\node;{ДИСК}\py", cwd=rf"{ДИСК}\clone"):
    return wrap._command_line(
        command, platform="win32", path_env=path_env, cwd=cwd, isfile=_фс(*файлы),
        system_root=КОРЕНЬ,
    )


def test_off_windows_the_command_is_left_alone():
    assert wrap._command_line(["x", "a b"], platform="linux", isfile=lambda p: True) == ["x", "a b"]


def test_on_windows_an_exe_runs_by_the_path_it_was_found_at():
    готово = _как_на_windows(["pytest", "-q"], rf"{ДИСК}\py\pytest.exe")
    assert готово == [rf"{ДИСК}\py\pytest.exe", "-q"]


def test_on_windows_an_unknown_name_is_left_to_createprocess():
    assert _как_на_windows(["нет-такой", "-q"]) == ["нет-такой", "-q"]


@pytest.mark.parametrize("суффикс", [".cmd", ".bat", ".CMD"])
def test_on_windows_a_script_goes_through_cmd(суффикс):
    готово = _как_на_windows(
        ["npm", "test", "a b", "x(1)", ""], rf"{ДИСК}\Program Files\nodejs\npm{суффикс}",
        path_env=rf"{ДИСК}\Program Files\nodejs",
    )
    assert готово == (
        rf'"{КОМАНДНЫЙ}" /d /v:off /s /c '
        # Путь — как его нашли по имени: регистр расширения у Windows не важен.
        rf'""{ДИСК}\Program Files\nodejs\npm{суффикс.lower()}" test "a b" "x(1)" """'
    )


@pytest.mark.parametrize("хвост", [".", " ", ". .", ".."])
def test_a_trailing_dot_or_space_does_not_hide_a_script(хвост):
    """Windows отбрасывает хвостовые `.` и пробелы: `x.cmd.` — тот же скрипт, и без этой
    нормализации он уходил бы мимо проверки в неявный `cmd.exe`."""
    имя = rf"{ДИСК}\s\x.cmd" + хвост
    готово = _как_на_windows([имя, "ok"], rf"{ДИСК}\s\x.cmd")
    assert isinstance(готово, str) and готово.startswith(f'"{КОМАНДНЫЙ}"')
    assert rf'"{ДИСК}\s\x.cmd" ok' in готово
    with pytest.raises(wrap._Refused):
        _как_на_windows([имя, "a&b"], rf"{ДИСК}\s\x.cmd")


def test_the_cmd_line_keeps_autorun_and_delayed_expansion_off():
    """`/d` — без AutoRun из реестра, `/v:off` — без раскрытия `!x!` в строке `/c`."""
    готово = _как_на_windows(["npm"], rf"{ДИСК}\node\npm.cmd")
    assert готово.startswith(f'"{КОМАНДНЫЙ}" /d /v:off /s /c "')


@pytest.mark.parametrize("аргумент", ["a\tb", "a,b", "a;b", "a=b", "a b", ""])
def test_what_cmd_splits_on_is_quoted(аргумент):
    готово = _как_на_windows(["npm", аргумент], rf"{ДИСК}\node\npm.cmd")
    assert готово.endswith(f' "{аргумент}""')


@pytest.mark.parametrize("путь", [rf"{ДИСК}\100%\npm.cmd", f'{ДИСК}\\a"b\\npm.cmd'])
def test_a_script_path_cmd_cannot_quote_is_refused(путь):
    with pytest.raises(wrap._Refused):
        _как_на_windows([путь], путь)


@pytest.mark.parametrize("корень", ["", "Windows", None])
def test_without_an_absolute_system_root_there_is_no_cmd(корень):
    """`COMSPEC` не берётся: он бывает другой оболочкой или относительным путём."""
    with pytest.raises(wrap._Refused, match="SystemRoot"):
        wrap._command_line(
            ["npm"], platform="win32", path_env=rf"{ДИСК}\node", cwd=rf"{ДИСК}\clone",
            isfile=_фс(rf"{ДИСК}\node\npm.cmd"), system_root=корень or "",
        )


def test_a_refused_argument_is_named_by_number_not_by_value():
    """Отказанный аргумент бывает паролем, а сообщение уходит в лог конвейера."""
    with pytest.raises(wrap._Refused) as отказ:
        _как_на_windows(["npm", "login", "Pa55&w0rd%1"], rf"{ДИСК}\node\npm.cmd")
    текст = str(отказ.value)
    assert "Pa55" not in текст and "w0rd" not in текст
    assert "№2" in текст and "'&'" in текст and "'%'" in текст


@pytest.mark.parametrize(
    "аргумент", ['a"b', "%PATH%", "a\nb", "a\rb", "a\x00b", "a&b", "a|b", "a<b", "a>b", "a^b"]
)
def test_what_a_script_could_unquote_is_refused(аргумент):
    """`& | < > ^` в кавычках безопасны только до `%~1` в самом скрипте."""
    with pytest.raises(wrap._Refused) as отказ:
        _как_на_windows(["npm", аргумент], rf"{ДИСК}\node\npm.cmd")
    assert "node" in str(отказ.value)  # подсказка, как обойтись без скрипта


def test_a_trailing_backslash_is_doubled_inside_quotes():
    """Иначе `\\"` у программы за скриптом — экранированная кавычка, и следующий
    аргумент склеивается с этим."""
    готово = _как_на_windows(["npm", f"{ДИСК}\\dir name\\", "next"], rf"{ДИСК}\node\npm.cmd")
    assert готово.endswith(f' "{ДИСК}\\dir name\\\\" next"')


def test_a_script_in_the_current_directory_is_not_picked_up_by_a_bare_name():
    """Голое `npm` в чужом клоне не должно находить `.\\npm.cmd`: при кандидате только в
    текущем каталоге — отказ с просьбой назвать путём; есть в PATH — берётся оттуда."""
    with pytest.raises(wrap._Refused, match=r"назовите путём"):
        _как_на_windows(["npm", "test"], rf"{ДИСК}\clone\npm.cmd")
    готово = _как_на_windows(["npm", "test"], rf"{ДИСК}\clone\npm.cmd", rf"{ДИСК}\node\npm.cmd")
    assert rf'"{ДИСК}\node\npm.cmd" test' in готово


@pytest.mark.parametrize(
    "файл", [rf"{ДИСК}\clone\gradlew.bat", rf"{ДИСК}\clone\fake.exe", rf"{ДИСК}\clone\gradlew"],
    ids=["gradlew.bat", "exe", "без-расширения"],
)
def test_a_bare_name_found_only_nearby_asks_for_a_path(файл):
    """CreateProcess смотрит в текущий каталог раньше PATH: голое имя оттуда — подмена
    в чужом клоне. Отказ подсказывает `.\\имя`."""
    имя = ntpath.splitext(ntpath.basename(файл))[0]
    with pytest.raises(wrap._Refused) as отказ:
        _как_на_windows([имя], файл)
    assert f".\\{имя}" in str(отказ.value)


def test_a_relative_path_entry_is_treated_as_the_current_directory():
    with pytest.raises(wrap._Refused, match="относительной записи"):
        _как_на_windows(["tool"], rf"{ДИСК}\clone\bin\tool.exe", path_env=rf"bin;{ДИСК}\node")


def test_a_named_relative_path_runs_the_script():
    """`.\\gradlew` — явный выбор: ищется как названо, с расширениями."""
    готово = _как_на_windows([r".\gradlew", "build"], r".\gradlew.bat")
    assert r'".\gradlew.bat" build' in готово


@pytest.mark.parametrize("path_env", [rf".;{ДИСК}\node", rf"sub;{ДИСК}\node"])
def test_relative_path_entries_are_skipped(path_env):
    готово = _как_на_windows(
        # И абсолютные, и относительные написания каталогов клона: пропуск записи должен
        # сработать раньше, чем её файл найдётся.
        ["npm"], rf"{ДИСК}\clone\npm.cmd", rf"{ДИСК}\clone\sub\npm.cmd", r".\npm.cmd", r"sub\npm.cmd",
        rf"{ДИСК}\node\npm.cmd",
        path_env=path_env,
    )
    assert rf'"{ДИСК}\node\npm.cmd"' in готово


def test_a_bare_script_name_found_only_in_the_current_directory_is_refused():
    """CreateProcess сам нашёл бы `npm.cmd` в текущем каталоге и запустил через cmd.exe."""
    with pytest.raises(wrap._Refused):
        _как_на_windows(["npm.cmd"], rf"{ДИСК}\clone\npm.cmd")


def test_a_named_path_is_taken_as_named():
    готово = _как_на_windows([r".\run.cmd"], r".\run.cmd")
    assert r'"' + "\\".join([".", "run.cmd"]) + '"' in готово


def test_a_found_file_that_is_not_executable_is_refused():
    with pytest.raises(wrap._Refused):
        _как_на_windows([rf"{ДИСК}\s\script.py"], rf"{ДИСК}\s\script.py")


def test_a_refused_command_does_not_run_anything():
    """Отказ — до снимка «до» и до запуска, со своим кодом и словами, почему."""
    запущено = []
    вызвано = []

    def отказ(command):
        raise wrap._Refused("нельзя")

    with mock.patch.object(wrap, "_command_line", отказ):
        r = wrap.run(
            ["npm", "x"],
            before=lambda: вызвано.append("до"),
            runner=lambda cmd: запущено.append(cmd),
        )
    assert r.code == wrap.EXIT_CANNOT_RUN
    assert запущено == [] and вызвано == []
    assert any("не запущена" in n and "нельзя" in n for n in r.notes)


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="настоящий cmd.exe есть только на Windows")


@windows_only
@pytest.mark.parametrize("суффикс", [".cmd", ".bat"])
def test_a_script_found_on_path_runs_and_keeps_its_code(суффикс, tmp_path, monkeypatch):
    (tmp_path / f"atlas_probe{суффикс}").write_text("@exit /b 7\r\n", encoding="ascii")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    r = wrap.run(["atlas_probe"])
    assert r.code == 7, r.notes


@windows_only
@pytest.mark.parametrize("хвост", [".", " "])
def test_a_trailing_dot_or_space_script_on_real_windows(хвост, tmp_path, monkeypatch):
    скрипт = tmp_path / "atlas_probe.cmd"
    скрипт.write_text("@exit /b 7\r\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    assert wrap.run([str(скрипт) + хвост]).code == 7
    r = wrap.run([str(скрипт) + хвост, "a&echo.>pwned.txt"])
    assert r.code == wrap.EXIT_CANNOT_RUN
    assert not (tmp_path / "pwned.txt").exists()


@windows_only
def test_a_script_that_unquotes_its_argument_cannot_be_injected(tmp_path, monkeypatch):
    """`%~1` снимает кавычки — поэтому `&` до скрипта не доходит вовсе."""
    скрипт = tmp_path / "atlas_echo.cmd"
    скрипт.write_text('@echo %~1>"%~dp0got.txt"\r\n', encoding="ascii")
    monkeypatch.chdir(tmp_path)
    r = wrap.run([str(скрипт), "a&echo.>pwned.txt"])
    assert r.code == wrap.EXIT_CANNOT_RUN
    assert not (tmp_path / "pwned.txt").exists()
    assert wrap.run([str(скрипт), "a b"]).code == 0
    assert (tmp_path / "got.txt").read_text().strip() == "a b"


@windows_only
def test_comspec_is_not_used(tmp_path, monkeypatch):
    """Подменённый `COMSPEC` не перехватывает запуск скрипта."""
    скрипт = tmp_path / "atlas_probe.cmd"
    скрипт.write_text("@exit /b 7\r\n", encoding="ascii")
    monkeypatch.setenv("COMSPEC", str(tmp_path / "cmd.exe"))
    assert wrap.run([str(скрипт)]).code == 7


@windows_only
@pytest.mark.parametrize("имя", ["fake.exe", "gradlew.bat"])
def test_a_bare_name_found_in_the_current_directory_is_refused_on_real_windows(
    имя, tmp_path, monkeypatch
):
    (tmp_path / имя).write_bytes(b"@echo.>ran.txt\r\n")
    monkeypatch.chdir(tmp_path)
    r = wrap.run([имя.split(".")[0]])
    assert r.code == wrap.EXIT_CANNOT_RUN
    assert any("назовите путём" in n for n in r.notes)
    assert not (tmp_path / "ran.txt").exists()


@windows_only
def test_dot_backslash_gradlew_runs(tmp_path, monkeypatch):
    (tmp_path / "gradlew.bat").write_text("@exit /b 4\r\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    assert wrap.run([".\\gradlew"]).code == 4


@windows_only
def test_a_relative_path_entry_is_refused_on_real_windows(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "atlas_tool.bat").write_text("@exit /b 0\r\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"bin{os.pathsep}{os.environ.get('PATH', '')}")
    assert wrap.run(["atlas_tool"]).code == wrap.EXIT_CANNOT_RUN


@windows_only
def test_a_bare_name_does_not_run_a_script_from_the_current_directory(tmp_path, monkeypatch):
    (tmp_path / "atlas_probe.cmd").write_text('@echo.>"%~dp0ran.txt"\r\n', encoding="ascii")
    monkeypatch.chdir(tmp_path)
    for имя in ("atlas_probe", "atlas_probe.cmd"):
        assert wrap.run([имя]).code == wrap.EXIT_CANNOT_RUN
    assert not (tmp_path / "ran.txt").exists()


@windows_only
def test_arguments_arrive_split_as_given(tmp_path):
    """Хвостовой `\\` не склеивает аргументы у программы, которую зовёт скрипт."""
    import json

    выход = tmp_path / "argv.json"
    код = f"import sys,json;open(r'{выход}','w').write(json.dumps(sys.argv[1:]))"
    скрипт = tmp_path / "atlas_argv.cmd"
    скрипт.write_text(f'@"{sys.executable}" -c "{код}" %*\r\n', encoding="utf-8")
    аргументы = [f"{ДИСК}\\dir name\\", "next", "a b", "x(1)"]
    r = wrap.run([str(скрипт), *аргументы])
    assert r.code == 0, r.notes
    assert json.loads(выход.read_text()) == аргументы


@windows_only
def test_a_script_runs_end_to_end_through_the_cli(tmp_path):
    import subprocess

    скрипт = tmp_path / "atlas_probe.cmd"
    скрипт.write_text("@exit /b 5\r\n", encoding="ascii")
    result = subprocess.run(
        [sys.executable, "-m", "partest_atlas.cli", "wrap", "--", str(скрипт)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 5, result.stderr[-500:]


def test_an_absolute_path_entry_equal_to_the_current_directory_is_honoured():
    """`curl` из System32, запущенный из System32, — не подмена: абсолютную запись
    PATH выбрал её владелец."""
    готово = _как_на_windows(
        ["curl", "-V"], rf"{ДИСК}\Windows\System32\curl.exe",
        path_env=rf"{ДИСК}\Windows\System32", cwd=rf"{ДИСК}\Windows\System32",
    )
    assert готово == [rf"{ДИСК}\Windows\System32\curl.exe", "-V"]


@windows_only
def test_a_script_in_an_absolute_path_entry_runs_from_that_directory(tmp_path, monkeypatch):
    (tmp_path / "atlas_tool.bat").write_text("@exit /b 3\r\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    assert wrap.run(["atlas_tool"]).code == 3
