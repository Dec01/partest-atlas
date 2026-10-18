"""Плагин эффектов: молчит, пока не включён, и по-разному сообщает о двух бедах."""

from __future__ import annotations

import os

import pytest

from partest_atlas.collect import db_effects
from partest_atlas.plugins import db_effects as plugin


@pytest.fixture(autouse=True)
def чисто():
    plugin._state.clear()
    yield
    plugin._state.clear()


class ФейковыйConfig:
    def __init__(self):
        self.строки = []

    def addinivalue_line(self, *a):
        self.строки.append(a)


def test_it_does_nothing_until_switched_on(monkeypatch):
    """Точка входа грузит модуль у каждого, кто поставил пакет. Обычный прогон не должен
    платить за это ни временем, ни соединениями."""
    monkeypatch.delenv(plugin.ENABLE_ENV, raising=False)
    monkeypatch.setattr(
        plugin, "load", lambda *a, **kw: pytest.fail("конфигурация прочитана на выключенном плагине")
    )
    plugin.pytest_configure(ФейковыйConfig())
    assert plugin._state == {}


def test_it_registers_its_marker_even_when_off(monkeypatch):
    """Иначе `--strict-markers` у потребителя ругался бы на маркер, который мы же и
    предлагаем ставить."""
    monkeypatch.delenv(plugin.ENABLE_ENV, raising=False)
    cfg = ФейковыйConfig()
    plugin.pytest_configure(cfg)
    assert any(plugin.MARKER in " ".join(map(str, a)) for a in cfg.строки)


@pytest.mark.parametrize(
    ("отобрано", "предел", "режим"),
    [
        (0, 12, db_effects.SESSION),
        (1, 12, db_effects.TEST),
        (12, 12, db_effects.TEST),
        (13, 12, db_effects.SESSION),
        (2000, 12, db_effects.SESSION),
    ],
)
def test_mode_is_chosen_by_how_many_tests_are_measured(отобрано, предел, режим):
    """Поштучный замер требует выдержки в секунды — на полном прогоне это часы."""
    assert plugin.choose_mode(отобрано, предел) == режим


def test_zero_selected_still_measures_the_session():
    """Мерить поштучно нечего, но общая дельта прогона осмысленна."""
    assert plugin.choose_mode(0, 12) == db_effects.SESSION


def test_a_misconfiguration_says_it_is_ours(monkeypatch, capsys):
    """Чинится в `atlas.yaml`, а не на стенде — и сообщение обязано это сказать."""
    plugin._note("database.connect=кривое", misconfigured=True)
    out = capsys.readouterr().out
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" in out
    assert "чинить здесь" in out


def test_an_unavailable_source_is_worded_differently(capsys):
    plugin._note("источник недоступен (OSError: нет маршрута)")
    out = capsys.readouterr().out
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" not in out
    assert "выключен" in out


def test_wanted_by_marker(monkeypatch):
    monkeypatch.delenv(plugin.ALL_ENV, raising=False)
    plugin._state["include"] = []
    assert plugin._wanted("файл.py::тест", True) is True
    assert plugin._wanted("файл.py::тест", False) is False


def test_wanted_by_mask(monkeypatch):
    monkeypatch.delenv(plugin.ALL_ENV, raising=False)
    plugin._state["include"] = ["*крупные*"]
    assert plugin._wanted("тесты/крупные/файл.py::т", False) is True
    assert plugin._wanted("тесты/мелкие/файл.py::т", False) is False


def test_the_all_switch_measures_everything(monkeypatch):
    monkeypatch.setenv(plugin.ALL_ENV, "1")
    plugin._state["include"] = []
    assert plugin._wanted("любой", False) is True


# --- режим `test` сквозь настоящий pytest ---------------------------------
#
# Здесь дефект и жил: режим `test` выбирался, а хука на тест не было, и при 1–12 тестах
# под замером артефакт не писался вовсе. Модульной проверкой `choose_mode` этого не
# поймать — нужен прогон, в котором pytest сам зовёт хуки плагина.

pytest_plugins = ["pytester"]

ЗАПИСИ = "запись-*.txt"
ЧАСЫ = "часы.txt"
ПОРЯДОК = "порядок.txt"

#: Через сколько «секунд» запись видна в статистике. Как у настоящего PostgreSQL:
#: счётчики пишущего бэкенда доезжают до наблюдателя с задержкой, и выдержка по
#: умолчанию (13 с) её перекрывает.
ЗАДЕРЖКА = 10.0


class ФейковаяБаза:
    """Счётчики `pg_stat_user_tables` по строкам файла: тест пишет строку — таблица растёт.

    Файлы, а не объекты в памяти, потому что писать в «базу» должен вложенный прогон
    (и воркеры xdist), которому до объектов внешнего теста не дотянуться. Время —
    логические часы в файле: их двигает только выдержка, и запись видна в снимке не
    раньше, чем через :data:`ЗАДЕРЖКА` после того, как сделана.
    """

    ТАБЛИЦЫ = ("orders", "users")

    def __init__(self, каталог):
        self.каталог = каталог

    def rollback(self):
        pass

    def _снимок(self):
        сейчас = _время(self.каталог)
        # Файл на запись: воркеры xdist пишут одновременно, а дозапись в общий файл на
        # Windows не атомарна — одна строка затирала другую.
        строки = [f.read_text(encoding="utf-8").strip() for f in self.каталог.glob(ЗАПИСИ)]
        видно = [т for т, когда in (с.split() for с in строки) if float(когда) + ЗАДЕРЖКА <= сейчас]
        return [{"table_name": т, "ins": видно.count(т), "upd": 0, "del": 0} for т in self.ТАБЛИЦЫ]

    def cursor(self):
        база = self

        class Курсор:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def execute(self_, sql, params=None):
                if "pg_stat_clear_snapshot" in sql:
                    self_.строки = []
                elif "pg_stat_activity" in sql:
                    self_.строки = [{"n": 0}]
                else:
                    self_.строки = база._снимок()

            def fetchall(self_):
                return self_.строки

            def fetchone(self_):
                return self_.строки[0] if self_.строки else None

        return Курсор()


def _время(каталог):
    путь = каталог / ЧАСЫ
    return float(путь.read_text()) if путь.exists() else 0.0


@pytest.fixture
def прогон(pytester, monkeypatch):
    """Вложенный прогон с включённым плагином и фейковой базой вместо PostgreSQL."""
    import contextlib
    from pathlib import Path

    from partest_atlas.adapters import postgres
    from partest_atlas.config import load

    monkeypatch.setenv(plugin.ENABLE_ENV, "1")
    for имя in (plugin.ALL_ENV, "ATLAS_SYSTEM", "ATLAS_ENV"):
        monkeypatch.delenv(имя, raising=False)
    # Воркерам xdist пакет не установлен — находят его по пути.
    корень = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [корень, os.getenv("PYTHONPATH")])))

    выдержки = []

    def выдержать(сек):
        выдержки.append(сек)
        (pytester.path / ЧАСЫ).write_text(str(_время(pytester.path) + сек))
        with (pytester.path / ПОРЯДОК).open("a", encoding="utf-8") as f:
            f.write("сон\n")

    monkeypatch.setattr(db_effects.time, "sleep", выдержать)

    @contextlib.contextmanager
    def соединить(section):
        yield ФейковаяБаза(pytester.path)

    monkeypatch.setattr(postgres, "connect", соединить)
    pytester.makepyfile(
        база=f"""
import uuid
from pathlib import Path

def записать(таблица):
    часы = Path({ЧАСЫ!r})
    сейчас = часы.read_text() if часы.exists() else "0"
    Path(f"запись-{{uuid.uuid4().hex}}.txt").write_text(f"{{таблица}} {{сейчас}}", encoding="utf-8")
"""
    )
    pytester.syspathinsert()

    def запустить(atlas_yaml, *аргументы, кэш=False, **тесты):
        pytester.makefile(".yaml", atlas=atlas_yaml)
        if тесты:
            pytester.makepyfile(**тесты)
        # Установленный пакет грузит плагин сам; здесь — явно, без двойной регистрации.
        без_кэша = () if кэш else ("-p", "no:cacheprovider")
        result = pytester.runpytest_inprocess(
            "-p", "no:partest_atlas_db_effects", "-p", "partest_atlas.plugins.db_effects",
            "-p", "no:asyncio", "-p", "no:benchmark", *без_кэша, *аргументы,
        )
        cfg = load(pytester.path)
        from partest_atlas import artifacts

        return result, artifacts.read(cfg.path_for(db_effects.KIND))

    запустить.выдержки = выдержки
    return запустить


def test_a_single_measured_test_gets_its_own_records(прогон):
    result, doc = прогон(
        "artifacts_dir: atlas\n",
        test_один="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_пишет():
    записать("orders")
""",
    )
    result.assert_outcomes(passed=1)
    assert doc["records"] == [
        {
            "test": "test_пишет",
            "nodeid": "test_один.py::test_пишет",
            "table": "orders",
            "insert": 1,
            "update": 0,
            "delete": 0,
            "shared": False,
            "scope": db_effects.TEST,
        }
    ]


def test_several_measured_tests_are_attributed_one_by_one(прогон):
    """Эффект — тому тесту, который его сделал; тест вне замера не приписывается никому.

    `test_вне_замера` пишет в `users` прямо перед `test_b`. Счётчик доезжает с
    задержкой, и без выдержки перед снимком «до» его запись попала бы в окно `test_b`.
    """
    result, doc = прогон(
        "artifacts_dir: atlas\n",
        test_несколько="""
import pytest
from база import записать

@pytest.fixture
def заказ():
    записать("orders")

@pytest.mark.db_effects
def test_a(заказ):
    записать("orders")

def test_вне_замера():
    записать("users")

@pytest.mark.db_effects
def test_b():
    записать("users")

@pytest.mark.db_effects
def test_тихий():
    pass
""",
    )
    result.assert_outcomes(passed=4)
    сводка = sorted((r["test"], r["table"], r["insert"], r["scope"]) for r in doc["records"])
    assert сводка == [
        ("test_a", "orders", 2, db_effects.TEST),  # с фикстурой: подготовка — тоже тест
        ("test_b", "users", 1, db_effects.TEST),
    ]


def test_back_to_back_measured_tests_wait_once_each(прогон):
    """Выдержка перед «до» нужна только после теста без замера или в начале: после теста
    под замером его собственная выдержка уже дала счётчикам доехать."""
    result, _ = прогон(
        "artifacts_dir: atlas\n",
        test_подряд="""
import pytest

@pytest.mark.db_effects
def test_a():
    pass

@pytest.mark.db_effects
def test_b():
    pass
""",
    )
    result.assert_outcomes(passed=2)
    assert прогон.выдержки == [db_effects.DEFAULT_SETTLE_SEC] * 3  # перед a, после a, после b


def test_over_the_limit_it_is_one_session_delta(прогон):
    """Выше предела режим сессии остаётся прежним: одна дельта на весь прогон."""
    result, doc = прогон(
        "artifacts_dir: atlas\ndb_effects:\n  per_test_limit: 1\n",
        test_много="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")

@pytest.mark.db_effects
def test_b():
    записать("orders")
""",
    )
    result.assert_outcomes(passed=2)
    assert [(r["test"], r["table"], r["insert"], r["scope"]) for r in doc["records"]] == [
        ("", "orders", 2, db_effects.SESSION)
    ]


def test_the_mode_counts_what_is_left_after_deselection(прогон):
    """Два теста под замером при пределе 1 — сессия; но `-k` оставил один, и мерить его
    надо поштучно."""
    result, doc = прогон(
        "artifacts_dir: atlas\ndb_effects:\n  per_test_limit: 1\n",
        "-k", "test_a",
        test_отбор="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")

@pytest.mark.db_effects
def test_b():
    записать("orders")
""",
    )
    result.assert_outcomes(passed=1, deselected=1)
    assert [(r["test"], r["scope"]) for r in doc["records"]] == [("test_a", db_effects.TEST)]


@pytest.mark.parametrize("ключ", ["settle_sec", "per_test_limit"])
@pytest.mark.parametrize("значение", ["abc", "-1"])
def test_a_bad_number_is_a_configuration_error_not_a_crash(прогон, ключ, значение):
    result, doc = прогон(
        f"artifacts_dir: atlas\ndb_effects:\n  {ключ}: {значение}\n",
        test_кривой="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")
""",
    )
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    result.stdout.fnmatch_lines([f"*ОШИБКОЙ КОНФИГУРАЦИИ*{ключ}*"])
    assert "INTERNALERROR" not in result.stdout.str()
    assert doc == {}


def test_under_xdist_only_the_controller_measures_the_session(прогон):
    """Воркеры молчат, режим `test` выключен: окна воркеров перекрывались бы, а один
    артефакт писали бы несколько процессов."""
    pytest.importorskip("xdist")
    result, doc = прогон(
        "artifacts_dir: atlas\n",
        "-n", "2",
        test_параллельно="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")

@pytest.mark.db_effects
def test_b():
    записать("orders")
""",
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*под pytest-xdist*режим test выключен*"])
    assert [(r["test"], r["table"], r["insert"], r["scope"]) for r in doc["records"]] == [
        ("", "orders", 2, db_effects.SESSION)
    ]


# --- разбор чисел и определение роли ---------------------------------------


class ФейковыйConfigXdist(ФейковыйConfig):
    def __init__(self, dist="no", worker=False, tx=None):
        super().__init__()

        class Опции:
            pass

        self.option = Опции()
        self.option.dist = dist
        self.option.tx = tx if tx is not None else (["popen"] * 2 if dist != "no" else [])
        if worker:
            self.workerinput = {}


def _включить(monkeypatch, tmp_path, yaml_text):
    from partest_atlas.config import load

    (tmp_path / "atlas.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv(plugin.ENABLE_ENV, "1")
    monkeypatch.setattr(plugin, "load", lambda *a, **kw: load(tmp_path))


def test_zero_is_zero_not_the_default(monkeypatch, tmp_path):
    """`or` подменял `0` умолчанием: `settle_sec: 0` молча становилось 13 секундами."""
    _включить(monkeypatch, tmp_path, "db_effects:\n  settle_sec: 0\n  per_test_limit: 0\n")
    plugin.pytest_configure(ФейковыйConfigXdist())
    assert plugin._state["settle"] == 0.0
    assert plugin._state["limit"] == 0


def test_missing_numbers_take_the_defaults(monkeypatch, tmp_path):
    _включить(monkeypatch, tmp_path, "db_effects: {}\n")
    plugin.pytest_configure(ФейковыйConfigXdist())
    assert plugin._state["settle"] == db_effects.DEFAULT_SETTLE_SEC
    assert plugin._state["limit"] == plugin.DEFAULT_PER_TEST_LIMIT


@pytest.mark.parametrize("значение", ["abc", "-1", "true", "1.5"])
def test_a_bad_limit_leaves_the_plugin_off(monkeypatch, tmp_path, capsys, значение):
    _включить(monkeypatch, tmp_path, f"db_effects:\n  per_test_limit: {значение}\n")
    plugin.pytest_configure(ФейковыйConfigXdist())
    assert not plugin._active()
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" in capsys.readouterr().out


def test_an_xdist_worker_stays_silent(monkeypatch, tmp_path):
    _включить(monkeypatch, tmp_path, "")
    plugin.pytest_configure(ФейковыйConfigXdist(dist="load", worker=True))
    assert not plugin._active()


def test_an_xdist_controller_measures_only_the_session(monkeypatch, tmp_path, capsys):
    _включить(monkeypatch, tmp_path, "")
    plugin.pytest_configure(ФейковыйConfigXdist(dist="load"))
    assert plugin._active() and plugin._state["distributed"] is True
    assert "режим test выключен" in capsys.readouterr().out


# --- порядок обёрток, бесконечности, отказ первого снимка -----------------


def test_the_settle_wraps_other_protocol_wrappers(прогон, pytester):
    """Выдержки — снаружи чужих обёрток протокола (pytest-timeout и ему подобных): иначе
    13 секунд сна попадают внутрь чужого таймера."""
    pytester.makeconftest(f"""
import pytest

def _метка(текст):
    with open({ПОРЯДОК!r}, "a", encoding="utf-8") as f:
        f.write(текст + "\\n")

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _метка("вход")
    yield
    _метка("выход")
""")
    result, _ = прогон(
        "artifacts_dir: atlas\n",
        test_порядок="""
import pytest

@pytest.mark.db_effects
def test_a():
    pass
""",
    )
    result.assert_outcomes(passed=1)
    порядок = (pytester.path / ПОРЯДОК).read_text(encoding="utf-8").split()
    assert порядок == ["сон", "вход", "выход", "сон"]


@pytest.mark.parametrize("ключ", ["settle_sec", "per_test_limit"])
@pytest.mark.parametrize("значение", [".nan", ".inf", "-.inf", "1e400"])
def test_infinities_are_a_configuration_error(monkeypatch, tmp_path, capsys, ключ, значение):
    """`time.sleep(nan)` — INTERNALERROR, `time.sleep(inf)` — вечный сон, `int(inf)` —
    сырой OverflowError."""
    _включить(monkeypatch, tmp_path, f"db_effects:\n  {ключ}: {значение}\n")
    plugin.pytest_configure(ФейковыйConfigXdist())
    assert not plugin._active()
    assert "ОШИБКОЙ КОНФИГУРАЦИИ" in capsys.readouterr().out


def test_a_nan_settle_does_not_crash_the_run(прогон):
    result, doc = прогон(
        "artifacts_dir: atlas\ndb_effects:\n  settle_sec: .nan\n",
        test_нан="""
import pytest

@pytest.mark.db_effects
def test_a():
    pass
""",
    )
    result.assert_outcomes(passed=1)
    assert "INTERNALERROR" not in result.stdout.str()
    assert doc == {}


class БазаБезПрав(ФейковаяБаза):
    """Соединение есть, а статистика не читается — первый же снимок падает."""

    закрыта = False

    def cursor(self):
        class Курсор:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def execute(self_, sql, params=None):
                raise PermissionError("нет прав на pg_stat_user_tables")

        return Курсор()


def test_a_failing_first_snapshot_turns_the_collector_off_not_the_run(прогон, pytester, monkeypatch):
    import contextlib

    from partest_atlas.adapters import postgres

    закрыто = []

    @contextlib.contextmanager
    def соединить(section):
        try:
            yield БазаБезПрав(pytester.path)
        finally:
            закрыто.append(True)

    monkeypatch.setattr(postgres, "connect", соединить)
    result, doc = прогон(
        "artifacts_dir: atlas\n",
        test_без_прав="""
import pytest

@pytest.mark.db_effects
def test_a():
    pass
""",
    )
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*db_effects выключен*снимок «до» не снялся*PermissionError*"])
    assert "INTERNALERROR" not in result.stdout.str()
    assert закрыто == [True]
    assert doc == {}


def test_an_unreachable_database_in_test_mode_leaves_the_run_alone(прогон, monkeypatch):
    """Сессионного пробника нет (база упала) — поштучно мерить не на чем, прогон идёт."""
    from partest_atlas.adapters import postgres

    def упала(section):
        raise OSError("нет маршрута")

    monkeypatch.setattr(postgres, "connect", упала)
    result, doc = прогон(
        "artifacts_dir: atlas\n",
        test_без_базы="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")
""",
    )
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*db_effects выключен: источник недоступен*"])
    assert doc == {}


def test_only_tests_under_measurement_count_towards_the_limit(прогон):
    """Предел 1, под замером один тест, рядом ещё два без замера: режим `test`."""
    result, doc = прогон(
        "artifacts_dir: atlas\ndb_effects:\n  per_test_limit: 1\n",
        test_счёт="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")

def test_b():
    pass

def test_c():
    pass
""",
    )
    result.assert_outcomes(passed=3)
    assert [(r["test"], r["scope"]) for r in doc["records"]] == [("test_a", db_effects.TEST)]


def test_last_failed_picks_the_mode_after_selection(прогон):
    """`--lf` отбирает упавший тест — режим считается по нему одному."""
    тесты = """
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")
    assert False

@pytest.mark.db_effects
def test_b():
    pass
"""
    yaml = "artifacts_dir: atlas\ndb_effects:\n  per_test_limit: 1\n"
    первый, doc = прогон(yaml, кэш=True, test_lf=тесты)
    первый.assert_outcomes(passed=1, failed=1)
    assert [r["scope"] for r in doc["records"]] == [db_effects.SESSION]

    второй, doc = прогон(yaml, "--lf", кэш=True)
    второй.assert_outcomes(failed=1)
    второй.stdout.fnmatch_lines(["*db_effects: режим test*"])
    assert [(r["test"], r["scope"]) for r in doc["records"] if r["scope"] == db_effects.TEST] == [
        ("test_a", db_effects.TEST)
    ]


def test_dist_without_workers_is_not_xdist(monkeypatch, tmp_path):
    """`--dist loadscope` в addopts при `-n 0`: прогон в одном процессе, режим `test`
    остаётся доступен."""
    _включить(monkeypatch, tmp_path, "")
    plugin.pytest_configure(ФейковыйConfigXdist(dist="loadscope", tx=[]))
    assert plugin._active() and plugin._state["distributed"] is False


@pytest.mark.parametrize(
    "аргументы", [("--dist", "loadscope"), ("-n", "0", "--dist", "loadscope")],
    ids=["dist-без-n", "n0"],
)
def test_n0_with_dist_loadscope_measures_per_test(прогон, аргументы):
    """Режим раздачи без воркеров (`--dist` в addopts) — не xdist."""
    pytest.importorskip("xdist")
    result, doc = прогон(
        "artifacts_dir: atlas\n",
        *аргументы,
        test_n0="""
import pytest
from база import записать

@pytest.mark.db_effects
def test_a():
    записать("orders")
""",
    )
    result.assert_outcomes(passed=1)
    assert [(r["test"], r["scope"]) for r in doc["records"]] == [("test_a", db_effects.TEST)]
