"""Требования из спецификаций → артефакт `specs.requirements`.

Зачем. Сбор знает, что реализовано и чем проверено, но не знает, **чему это должно
соответствовать**. В документе аналитики написано, как должно работать. Сборщик режет
документ на требования и вешает на каждое якоря — пути API, таблицы, экраны, — по
которым карта связывает требование с узлами и с тестами.

Ядро — нарезка по заголовкам и якоря; чтение файла отделено от него намеренно. Форматов
документов много, и разбор одного из них не должен решать, как устроено требование:
:func:`read_docx` отдаёт блоки, а что с ними делать, знает :func:`slice_requirements`.

Ничего проектного: стили заголовков, префикс API и словарь синонимов приходят из
`atlas.yaml`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

KIND = "specs.requirements"

#: Номер раздела в начале заголовка: «6.5 Расчёт сплита».
_NUM_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)*)[.)]?\s+(?P<title>\S.*)$")

#: Путь вида `/корень/дальше`.
_REL_PATH_RE = re.compile(r"(?P<root>/[\w\-]+)(?P<rest>(?:/[\w\-{}]+)*)")

#: Идентификатор внутри пути: `/items/1` и `/items/{id}` — одна операция.
_ID_IN_PATH_RE = re.compile(r"/(?:\d+|\{[^}]*\})(?=/|$)")

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Block:
    """Кусок документа: абзац, таблица или код."""

    text: str
    style: str = ""
    kind: str = "para"


@dataclass
class Requirement:
    id: str
    title: str
    section: str
    level: int
    text: str
    lead: str
    source: str
    path: List[str] = field(default_factory=list)
    children: int = 0
    anchors: Dict[str, List[str]] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "section": self.section,
            "level": self.level,
            "text": self.text,
            "lead": self.lead,
            "source": self.source,
            "path": list(self.path),
            "children": self.children,
            "anchors": dict(self.anchors),
        }


#: Пространство имён Word в `word/document.xml`.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(node: Any) -> str:
    """Текст абзаца с учётом разрывов строк и табуляций внутри него."""
    parts: List[str] = []
    for el in node.iter():
        if el.tag == f"{_W}t":
            parts.append(el.text or "")
        elif el.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
        elif el.tag == f"{_W}tab":
            parts.append(" ")
    return "".join(parts).strip()


def _para_style(node: Any) -> str:
    props = node.find(f"{_W}pPr")
    if props is None:
        return ""
    style = props.find(f"{_W}pStyle")
    return (style.get(f"{_W}val") or "") if style is not None else ""


def _table_text(table: Any) -> str:
    """Таблица разворачивается в строки «ячейка | ячейка».

    В спецификациях таблицы несут атрибуты сущностей и описания полей — самое ценное для
    якорей. Разметка теряется сознательно: нужен текст для поиска, а не вёрстка.
    """
    rows: List[str] = []
    for tr in table.findall(f"{_W}tr"):
        cells = [_WS_RE.sub(" ", _para_text(tc)).strip() for tc in tr.findall(f"{_W}tc")]
        line = " | ".join(c for c in cells if c)
        if line:
            rows.append(line)
    return "\n".join(rows)


def read_docx(path: Any) -> List[Block]:
    """Документ → плоский список блоков в порядке чтения.

    Идём по **прямым детям** `body`, а не по всему дереву: абзацы внутри таблиц иначе
    попали бы в поток дважды и разорвали бы разделы — заголовок оказался бы отделён от
    своего текста куском, который уже прочитан.

    Читается штатной библиотекой: `.docx` — это zip с `word/document.xml`, и ради одного
    формата тянуть зависимость в публичный пакет незачем.
    """
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    body = ET.fromstring(xml).find(f"{_W}body")
    if body is None:
        return []

    out: List[Block] = []
    for node in list(body):
        if node.tag == f"{_W}p":
            text = _para_text(node)
            if text:
                style = _para_style(node)
                kind = "code" if style.lower().startswith("sourcecode") else "para"
                out.append(Block(text=text, style=style, kind=kind))
        elif node.tag == f"{_W}tbl":
            text = _table_text(node)
            if text:
                out.append(Block(text=text, kind="table"))
    return out


def _slug(title: str) -> str:
    return _WS_RE.sub("-", (title or "").strip().lower())[:40] or "без-имени"


def heading_level(block: Block, styles: Mapping[str, int]) -> Optional[int]:
    """Уровень заголовка или ``None``.

    **Стиль важнее нумерации.** В документах попадаются списки вида «1. База», которые
    выглядят как заголовки и заголовками не являются; нарезка по виду строки разрезала
    бы требование посреди перечисления.
    """
    return styles.get((block.style or "").lower())


def breadcrumbs(heads: Sequence[tuple], pos: int) -> List[str]:
    """Путь до требования: ближайшие заголовки старших уровней слева."""
    _, level, _, _ = heads[pos]
    out: List[str] = []
    current = level
    for idx in range(pos - 1, -1, -1):
        _, lvl, _, title = heads[idx]
        if lvl < current:
            out.append(title)
            current = lvl
        if current == 1:
            break
    return list(reversed(out))


def slice_requirements(
    blocks: Sequence[Block],
    *,
    source: str,
    styles: Mapping[str, int],
    prefix: str = "REQ",
    min_chars: int = 0,
) -> List[Requirement]:
    """Заголовок и всё до следующего заголовка того же или старшего уровня.

    Текст раздела **включает вложенные подразделы**: у раздела «6» должно быть описание
    целиком, а у «6.5» — своё, более узкое. Одно предложение попадает в два требования,
    и это верно: они разной крупности, и карта показывает их на разных уровнях.
    Рядом лежит `lead` — собственный текст раздела до первого подзаголовка.
    """
    heads: List[tuple] = []
    for i, block in enumerate(blocks):
        level = heading_level(block, styles)
        if level is None:
            continue
        match = _NUM_RE.match(block.text)
        num = match.group("num") if match else ""
        title = (match.group("title") if match else block.text).strip()
        heads.append((i, level, num, title))

    out: List[Requirement] = []
    seen: Dict[str, int] = {}
    for pos, (idx, level, num, title) in enumerate(heads):
        end = len(blocks)
        children = 0
        for next_idx, next_level, _, _ in heads[pos + 1:]:
            if next_level <= level:
                end = next_idx
                break
            children += 1
        own_end = heads[pos + 1][0] if pos + 1 < len(heads) else end

        body = [b.text for b in blocks[idx + 1:end] if heading_level(b, styles) is None]
        text = "\n\n".join(body).strip()
        if len(text) < min_chars:
            continue
        lead = "\n\n".join(b.text for b in blocks[idx + 1:min(own_end, end)]).strip()

        section = num or _slug(title)
        rid = f"{prefix}-{section}"
        # Два раздела с одним номером — не редкость в живых документах. Молча слить их
        # значило бы потерять одно требование целиком.
        if rid in seen:
            seen[rid] += 1
            rid = f"{rid}~{seen[rid]}"
        else:
            seen[rid] = 1

        out.append(
            Requirement(
                id=rid,
                title=title,
                section=section,
                level=level,
                text=text,
                lead=lead,
                source=source,
                path=breadcrumbs(heads, pos),
                children=children,
            )
        )
    return out


def norm_api(path: str) -> str:
    return _ID_IN_PATH_RE.sub("/{id}", path or "")


def api_roots(texts: Iterable[str], api_prefix: str) -> Set[str]:
    """Корни ресурсов, встреченные с полным префиксом.

    Нужны, чтобы принимать относительные пути: спецификация пишет `/items/{id}`, опуская
    префикс. Без такого списка пришлось бы либо терять эти пути, либо превращать в
    эндпоинты куски вроде `/withdraw` из колонки «статусные действия».
    """
    if not api_prefix:
        return set()
    pattern = re.compile(re.escape(api_prefix) + r"/([\w\-]+)")
    return {m for text in texts for m in pattern.findall(text)}


def extract_anchors(
    text: str,
    *,
    api_prefix: str = "",
    tables: Iterable[str] = (),
    synonyms: Mapping[str, str] = {},
    roots: Iterable[str] = (),
) -> Dict[str, List[str]]:
    """Что из текста **надёжно** сопоставляется с узлами карты.

    Только проверяемое: путь API, имя настоящей таблицы. Свободные формулировки
    переводятся в таблицы словарём синонимов из конфигурации — угадывать по морфологии
    не беремся, потому что угаданная связь в карте читается как факт.
    """
    low = (text or "").lower()
    out: Dict[str, List[str]] = {}

    if api_prefix:
        known_roots = set(roots)
        api: Set[str] = set()
        for m in _REL_PATH_RE.finditer(text or ""):
            full = f"{m.group('root')}{m.group('rest')}"
            if full.startswith(api_prefix + "/"):
                api.add(norm_api(full))
            elif m.group("root").lstrip("/") in known_roots:
                api.add(norm_api(f"{api_prefix}{full}"))
        if api:
            out["api"] = sorted(api)

    known = set(tables)
    hits = {t for t in known if re.search(rf"\b{re.escape(t)}\b", low)}
    # Синоним ведёт на таблицу, только если такая таблица правда есть: иначе словарь
    # тихо разъедется со схемой и требование повиснет в пустоту.
    for word, table in synonyms.items():
        if table in known and word.lower() in low:
            hits.add(table)
    if hits:
        out["table"] = sorted(hits)

    return out


def build(requirements: Sequence[Requirement]) -> Dict[str, Any]:
    rows = [r.as_dict() for r in requirements]
    return {
        "requirements": rows,
        "stats": {
            "requirements": len(rows),
            "sources": len({r["source"] for r in rows}),
            "withAnchors": sum(1 for r in rows if r["anchors"]),
        },
    }
