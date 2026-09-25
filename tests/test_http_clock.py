"""Часы HTTP: что считается записью, и почему патч обязан сниматься."""

from __future__ import annotations

import pytest

from partest_atlas.collect import http_clock


@pytest.fixture(autouse=True)
def чисто():
    http_clock.uninstall()
    http_clock.reset()
    yield
    http_clock.uninstall()
    http_clock.reset()


class Запрос:
    def __init__(self, method):
        self.method = method


def test_no_requests_means_zero_not_now():
    """`0.0` — «запросов не было». Текущее время здесь читалось бы как «только что
    писали», и вывод про тишину переворачивался бы на противоположный."""
    assert http_clock.last() == 0.0
    assert http_clock.last_write() == 0.0


@pytest.mark.parametrize("метод", ["POST", "PUT", "PATCH", "DELETE", "post", "Patch"])
def test_writing_methods_move_the_write_clock(метод):
    http_clock._tick(метод)
    assert http_clock.last_write() > 0


@pytest.mark.parametrize("метод", ["GET", "HEAD", "OPTIONS", ""])
def test_reading_methods_move_only_the_general_clock(метод):
    """Ради чего всё и написано: тест публикует задачу и дальше опрашивает статус.
    Опрос — `GET`, и если считать его, тишина не наступит никогда."""
    http_clock._tick(метод)
    assert http_clock.last() > 0
    assert http_clock.last_write() == 0.0


def test_polling_after_a_write_does_not_move_the_write_clock():
    """Тот самый живой сценарий целиком."""
    http_clock._tick("POST")
    отметка = http_clock.last_write()
    for _ in range(5):
        http_clock._tick("GET")
    assert http_clock.last_write() == отметка
    assert http_clock.counters() == {"requests": 6, "writes": 1}
    # `last() > отметка` здесь не утверждается намеренно: `time.time()` идёт шагами
    # (на Windows около 15 мс), и шесть запросов подряд попадают в один шаг. Выводу это
    # не мешает — он сравнивает отметку события с последней записью через секунды
    # выдержки, — но утверждать строгое неравенство на соседних вызовах нельзя.


def test_counters_say_how_much_to_trust_the_inference():
    """Признак не доказательство: стенд бывает общий. Счётчики лежат рядом, чтобы это
    было видно, а не подразумевалось."""
    http_clock._tick("GET")
    http_clock._tick("POST")
    http_clock._tick("GET")
    assert http_clock.counters() == {"requests": 3, "writes": 1}


def test_reset_clears_everything():
    http_clock._tick("POST")
    http_clock.reset()
    assert http_clock.last() == 0.0
    assert http_clock.counters() == {"requests": 0, "writes": 0}


def test_an_unrecognisable_request_is_treated_as_safe():
    """Часы не имеют права ронять прогон, если сигнатура транспорта окажется иной."""
    http_clock._tick(http_clock._method_of())
    assert http_clock.last() > 0
    assert http_clock.last_write() == 0.0


def test_method_is_read_from_a_keyword_argument_too():
    assert http_clock._method_of(request=Запрос("POST")) == "POST"


# --- патч ставится и снимается --------------------------------------------


def test_install_patches_httpx_and_counts_a_real_send(monkeypatch):
    import httpx

    установлено = http_clock.install()
    assert "httpx" in установлено

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as client:
        client.post("https://проба.invalid/x")
        client.get("https://проба.invalid/x")

    assert http_clock.counters() == {"requests": 2, "writes": 1}


def test_uninstall_puts_the_transport_back():
    """Без этого сбор нельзя выключить в том же процессе: включивший однажды платит
    за него до конца жизни процесса."""
    import httpx

    было = httpx.Client.send
    http_clock.install()
    assert httpx.Client.send is not было
    http_clock.uninstall()
    assert httpx.Client.send is было
    assert http_clock.installed() == []


def test_installing_twice_does_not_count_a_request_twice():
    """Две обёртки вокруг одной точки удвоили бы каждый запрос — и вывод про тишину
    остался бы верным, а счётчики доверия соврали бы."""
    import httpx

    http_clock.install()
    http_clock.install()

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as client:
        client.post("https://проба.invalid/x")

    assert http_clock.counters() == {"requests": 1, "writes": 1}


def test_the_wrapper_keeps_the_original_name():
    """Чужой код и отладчик читают `__name__`; безымянная обёртка усложняет разбор
    чужого прогона ради нашего удобства."""
    import httpx

    http_clock.install()
    assert httpx.Client.send.__name__ == "send"
