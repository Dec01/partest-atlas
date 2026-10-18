"""Команда `partest-atlas`: «показать нечего» — это не успех."""

from __future__ import annotations

import pytest

from partest_atlas import artifacts, cli
from partest_atlas.collect import db_effects


def _артефакт(tmp_path):
    path = tmp_path / "db_effects.json"
    artifacts.write(path, db_effects.KIND, {"records": []}, collector="тест")
    return path


def test_show_of_a_missing_path_fails_and_says_so(tmp_path, capsys):
    """Снисходительный `read` отдал бы `{}`, и шапка из одних `None` с кодом 0 выглядела
    бы как показанный артефакт."""
    нет = tmp_path / "нет" / "db_effects.json"
    код = cli.main(["show", str(нет)])
    out, err = capsys.readouterr()
    assert код == 1
    assert str(нет) in err
    assert "None" not in out


def test_show_of_a_real_artifact_is_still_zero(tmp_path, capsys):
    код = cli.main(["show", str(_артефакт(tmp_path))])
    out, err = capsys.readouterr()
    assert код == 0
    assert "db.effects" in out
    assert err == ""


def test_show_shows_what_exists_and_still_fails_for_what_does_not(tmp_path, capsys):
    есть = _артефакт(tmp_path)
    нет = tmp_path / "нет.json"
    код = cli.main(["show", str(есть), str(нет)])
    out, err = capsys.readouterr()
    assert код == 1
    assert str(есть) in out
    assert str(нет) in err


def test_wrap_help_does_not_promise_snapshots(capsys):
    """Справка обещала «снять наблюдения», а команда их не снимает."""
    try:
        cli.main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    строка = next(s for s in out.splitlines() if s.strip().startswith("wrap"))
    assert "наблюден" not in строка and "сним" not in строка


@pytest.mark.parametrize(
    "содержимое", ["", "{битый", '{"records": []}', "[]"], ids=["пустой", "битый", "без-kind", "список"]
)
def test_show_of_something_that_is_not_an_artifact_fails(tmp_path, capsys, содержимое):
    path = tmp_path / "db_effects.json"
    path.write_text(содержимое, encoding="utf-8")
    код = cli.main(["show", str(path)])
    out, err = capsys.readouterr()
    assert код == 1
    assert str(path) in err
    assert "None" not in out


def test_wrap_own_help_does_not_promise_snapshots(capsys):
    with pytest.raises(SystemExit):
        cli.main(["wrap", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Снимков «до» и «после» команда пока не делает" in out
    assert "отдаётся команды" not in out
