"""Обёртка вокруг чужого прогона: чужой код возврата остаётся чужим."""

from __future__ import annotations

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
