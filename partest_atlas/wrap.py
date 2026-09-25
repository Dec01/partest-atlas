"""`partest-atlas wrap -- <команда>` — снимки до и после чужого прогона.

Зачем. Плагины работают внутри pytest. Но прогон бывает и не pytest: нагрузочный сценарий,
чужой набор на другом языке, ручная проверка. Обёртка даёт то же наблюдение — что прогон
сделал с базой и что прошло по шине — не зная о нём ничего, кроме командной строки.

**Код возврата отдаётся чужой, а не свой.** Это не мелочь и не вежливость: обёртка,
подменяющая код команды, превращает провал прогона в успех у всех, кто на неё смотрит, —
у человека, у конвейера, у следующего шага сборки. Наблюдатель не имеет права менять
исход того, за чем наблюдает.

Из того же правила: неудача **сбора** не портит код возврата команды. Если прогон прошёл,
а снять наблюдение не удалось, это неудача карты, а не прогона, и сказать об этом надо
словами, не кодом.
"""

from __future__ import annotations

import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence

#: Код, которым отвечаем, когда команду не удалось даже запустить. Отдельный от кодов
#: самой команды настолько, насколько возможно: «не запустилось» и «отработало плохо» —
#: разные новости.
EXIT_CANNOT_RUN = 127


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

    state = None
    if before is not None:
        try:
            state = before()
        except Exception as exc:
            notes.append(f"снимок «до» не снялся ({type(exc).__name__}: {exc})")

    try:
        completed = runner(list(command))
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
