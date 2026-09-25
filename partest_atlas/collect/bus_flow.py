"""Что прошло по шине → артефакт `bus.flow`. Строго на чтение.

Читатель **не входит в группу потребителей и не коммитит смещения**: наблюдатель не имеет
права влиять на то, за чем наблюдает. Иначе сбор отнимал бы события у настоящего
потребителя — и карта описывала бы систему, которую сама же и сломала.

Наблюдение — разность конечных смещений топиков до и после куска работы. Это говорит
«сколько событий прошло», а не «какие именно»: читать тела означало бы вычитывать топик
целиком, что дорого и на общем стенде заметно.

Как и у соседей, **вычисление отделено от опроса**: :func:`diff_offsets` — чистая
функция над двумя снимками смещений.

Чего здесь намеренно нет: вывода «событие пришло после последней записи теста». Он
опирается на часы HTTP-запросов — отдельный сборщик, который ещё не перенесён. Пока он
не приехал, признак принимается параметром, а не вычисляется тут: перенос по частям
должен оставаться проверяемым на каждом шаге.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
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
        out.append(
            {
                "test": test,
                "nodeid": nodeid,
                "topic": topic,
                "events": events,
                "scope": scope,
            }
        )
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
        if self.settle_sec:
            time.sleep(self.settle_sec)
        after = {t: dict(self.reader.end_offsets(t)) for t in self._before}
        return diff_offsets(self._before, after, test=test, nodeid=nodeid, scope=self.scope)


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
