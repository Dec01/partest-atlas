"""Пакет не должен объявлять того, чего в нём нет.

Этот файл заведён после поломки, которая стоила бы потребителю всего прогона.
Объявлялись три точки входа `pytest11` на модули, которых в пакете не существовало.
pytest грузит `pytest11` на старте, **до сбора тестов**, поэтому установка
`partest-atlas` роняла у потребителя любой прогон целиком: вместо единственного
проходящего теста — `ModuleNotFoundError: partest_atlas.plugins.bus_flow`.

Объявленная и отсутствующая **команда** мешает тому, кто её позвал. Объявленный и
отсутствующий **плагин** мешает всем и всегда, включая прогоны, которые про карту ничего
не знают. Разница в цене — причина, по которой проверка написана первой.

**Читаются метаданные установленного дистрибутива, а не `pyproject.toml`.** Так делает
и сам pytest, когда грузит плагины; значит проверяется то, что потребитель получит, а не
то, что мы написали. Попутно отпадает разбор TOML, которого в стандартной библиотеке
3.10 нет вовсе.

**Граница, которую надо знать.** Против самого тяжёлого случая — объявлен плагин,
модуля нет — этот файл бессилен по устройству: такой дефект валит pytest на старте,
включая прогон этого набора. Проверено возвратом дефекта 2026-09-25: не «тест упал», а
«тесты не собрались». Дефект **прячет сам себя**, отключая то, что должно его поймать.
Ловится он не здесь, а тем, что прогон в репозитории вообще не стартует, — сигнал
громкий, но приходит не отсюда. Здесь ловится всё остальное: сломанная команда, и —
что тише и оттого опаснее — модуль плагина, который есть, а строки для него нет.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = "partest-atlas"


def _entry_points(group: str) -> dict[str, str]:
    try:
        eps = metadata.distribution(DIST).entry_points
    except metadata.PackageNotFoundError:  # pragma: no cover
        pytest.skip(f"{DIST} не установлен — проверять нечего, поставьте `pip install -e .`")
    return {ep.name: ep.value for ep in eps if ep.group == group}


def test_every_declared_console_script_is_importable():
    """`project.scripts` обещает работающую команду — значит модуль обязан быть."""
    scripts = _entry_points("console_scripts")
    assert scripts, "команда исчезла из метаданных — проверка перестала иметь предмет"
    for name, target in scripts.items():
        module_name, _, attr = target.partition(":")
        module = importlib.import_module(module_name)
        assert hasattr(module, attr), f"{name}: в {module_name} нет {attr!r}"


def test_every_declared_pytest_plugin_is_importable():
    """Ради чего файл и заведён.

    Пустой набор — законный ответ: пока модулей нет, строк быть не должно. Поэтому
    проверка не требует непустоты, а требует **соответствия**.
    """
    for name, target in _entry_points("pytest11").items():
        importlib.import_module(target), name


def test_declared_pytest_plugins_match_what_the_package_has():
    """Обратная сторона: модуль плагина есть, а строки нет — потребитель его не получит.

    Без этой половины предыдущая проверка проходила бы и на пакете, который тихо
    перестал подключать то, что умеет.
    """
    declared = set(_entry_points("pytest11").values())
    plugins_dir = ROOT / "partest_atlas" / "plugins"
    present = {
        f"partest_atlas.plugins.{p.stem}"
        for p in plugins_dir.glob("*.py")
        if p.stem != "__init__"
    }
    assert declared == present, (
        f"объявлено {sorted(declared)}, лежит в пакете {sorted(present)}. "
        "Строка добавляется вместе с модулем — не раньше и не позже"
    )


def test_installing_the_package_does_not_break_a_foreign_pytest_run(tmp_path):
    """Тот самый симптом целиком: чужой прогон обязан дойти до теста.

    Проверяется не импорт, а **поведение pytest** — ломалось именно оно, и ломалось до
    того, как хоть один тест был собран.
    """
    project = tmp_path / "foreign"
    project.mkdir()
    (project / "test_sanity.py").write_text(
        "def test_sanity():\n    assert True\n", encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_sanity.py"],
        capture_output=True,
        text=True,
        cwd=str(project),
    )
    assert result.returncode == 0, (
        "чужой прогон не дошёл до теста при установленном partest-atlas:\n"
        f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    )
