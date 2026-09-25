"""Что прошло по шине → артефакт `bus.flow`. Строго на чтение.

Читатель **не входит в группу потребителей и не коммитит смещения**: наблюдатель не имеет
права влиять на то, за чем наблюдает. Иначе сбор отнимал бы события у настоящего
потребителя — и карта описывала бы систему, которую сама же и сломала.

Наблюдение — разность конечных смещений топиков до и после куска работы. Это говорит
«сколько событий прошло», а не «какие именно»: читать тела означало бы вычитывать топик
целиком, что дорого и на общем стенде заметно.

Как и у соседей, **вычисление отделено от опроса**: :func:`diff_offsets` — чистая
функция над двумя снимками смещений.

Вывод «писал не API, а потребитель» строится рядом, по часам HTTP-запросов
(`collect.http_clock`): если событие прошло **позже** последнего пишущего запроса, сам
API попросить о записи уже не мог. Признак не доказательство — стенд бывает общий, —
поэтому рядом с ним всегда лежат счётчики запросов, по которым видно, чему верить.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field

from partest_atlas.collect import http_clock
from typing import Any, Dict, List, Mapping, Optional, Sequence

KIND = "bus.flow"

#: Сколько ждать, прежде чем снимать смещения. Потребитель разбирает событие не
#: мгновенно, а запись в базу видна ещё позже — выдержка здесь та же по смыслу, что и
#: у эффектов в базе, но короче: шина быстрее базы.
DEFAULT_SETTLE_SEC = 5.0

SESSION = "session"
TEST = "test"


class ReaderNotConfigured(RuntimeError):
    """Читатель шины не описан либо описан неверно.

    Отдельно от «брокер недоступен» — и это не педантизм. Ровно на смешении этих двух
    один проект потерял полдня: клиент шины отверг ключ конфигурации, сборщик до конца
    прогона сообщал «брокер недоступен», артефакт молча не писался, и по выводу это было
    неотличимо от лежащего стенда.
    """


def diff_offsets(
    before: Mapping[str, Mapping[int, int]],
    after: Mapping[str, Mapping[int, int]],
    *,
    test: str = "",
    nodeid: str = "",
    scope: str = SESSION,
    last_write: float = 0.0,
    counters: Optional[Mapping[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Сколько событий прошло по каждому топику. Топики без событий не попадают в вывод.

    Две тонкости, каждая оплачена поведением настоящей шины:

    Смещение может **уменьшиться** — топик пересоздали, сработала политика хранения.
    Отрицательная разность не событие, поэтому берётся ноль: иначе удаление старых
    сообщений читалось бы как отрицательный трафик.

    Раздел, которого не было в первом снимке, считается **целиком**: он появился между
    снимками, и всё, что в нём лежит, прошло за наше время.
    """
    out: List[Dict[str, Any]] = []
    for topic in sorted(after):
        was = before.get(topic)
        if was is None:
            continue
        now = after[topic]
        events = sum(max(0, int(now.get(part, 0)) - int(offset)) for part, offset in was.items())
        events += sum(int(v) for part, v in now.items() if part not in was)
        if not events:
            continue
        record: Dict[str, Any] = {
            "test": test,
            "nodeid": nodeid,
            "topic": topic,
            "events": events,
            "scope": scope,
        }
        if last_write or counters:
            # `afterWrite` — признак работы потребителя: тест больше не просил ничего
            # записать. Счётчики рядом, потому что признак не доказательство: на общем
            # стенде писать могла чужая сессия, и по числу запросов видно, насколько
            # выводу можно верить.
            record["afterWrite"] = bool(last_write) and time.time() > last_write
            record["http"] = dict(counters or {})
        out.append(record)
    return out


def topics_for(available: Sequence[str], include: Sequence[str] = ()) -> List[str]:
    """Топики под замером: по маскам из конфигурации, иначе все доступные.

    Маски нужны не ради экономии: на общем брокере лежат топики чужих систем, и снимать
    их — значит писать в артефакт чужой трафик под своим именем.
    """
    import fnmatch

    if not include:
        return sorted(available)
    return sorted(t for t in available if any(fnmatch.fnmatch(t, p) for p in include))


def open_reader(section: Mapping[str, Any]):
    """Читатель шины так, как назвал его проект. ``None``, если проект его не назвал.

    ``reader: "модуль:функция"`` — контекст-менеджер, отдающий читателя с методами
    ``list_topics()`` и ``end_offsets(topic)``. Необязательный ``configured: "имя"`` —
    функция того же модуля, отвечающая, настроен ли доступ; она отличает «проект не
    настраивал шину» от «настроил неверно».
    """
    target = section.get("reader")
    if not target:
        return None
    module_name, _, attr = str(target).partition(":")
    if not attr:
        raise ReaderNotConfigured(f"bus_flow.reader={target!r}: ожидалось 'модуль:функция'")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ReaderNotConfigured(f"bus_flow.reader={target!r} не импортируется: {exc}") from exc

    guard = section.get("configured")
    if guard:
        check = getattr(module, str(guard), None)
        if check is None:
            raise ReaderNotConfigured(
                f"bus_flow.configured={guard!r}: в {module_name} такой функции нет"
            )
        if not check():
            return None

    factory = getattr(module, attr, None)
    if factory is None:
        raise ReaderNotConfigured(f"bus_flow.reader={target!r}: в {module_name} нет {attr!r}")
    return factory()


@dataclass
class BusFlow:
    """Смещения топиков до и после куска работы."""

    reader: Any
    topics: List[str] = field(default_factory=list)
    settle_sec: float = 0.0
    scope: str = SESSION
    _before: Dict[str, Dict[int, int]] = field(default_factory=dict, init=False)

    def discover(self) -> List[str]:
        return sorted(self.reader.list_topics())

    def start(self) -> "BusFlow":
        self._before = {t: dict(self.reader.end_offsets(t)) for t in self.topics}
        return self

    def finish(self, *, test: str = "", nodeid: str = "") -> List[Dict[str, Any]]:
        # Момент берётся **до** выдержки: после сна «последняя запись» окажется в
        # прошлом на величину сна у любого прогона, и признак выродится в «всегда да».
        last_write = http_clock.last_write()
        counters = http_clock.counters()
        if self.settle_sec:
            time.sleep(self.settle_sec)
        after = {t: dict(self.reader.end_offsets(t)) for t in self._before}
        return diff_offsets(
            self._before, after, test=test, nodeid=nodeid, scope=self.scope,
            last_write=last_write, counters=counters,
        )


def open_flow(
    reader: Any,
    topics: Optional[Sequence[str]] = None,
    *,
    scope: str = SESSION,
    settle_sec: float = 0.0,
) -> BusFlow:
    flow = BusFlow(
        reader=reader,
        topics=list(topics or []),
        scope=scope,
        settle_sec=settle_sec,
    )
    if not flow.topics:
        flow.topics = flow.discover()
    return flow.start()
