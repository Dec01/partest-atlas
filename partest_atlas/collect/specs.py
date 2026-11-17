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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from partest_atlas import artifacts
from partest_atlas.artifacts import Scope
from partest_atlas.config import Config, load, non_negative
from partest_atlas.lang import t

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


#: Потолок распакованного `word/document.xml`. Текст спецификации в сотни страниц —
#: единицы мегабайт. Ограничены байты, а память на дерево ElementTree в худшем случае —
#: около 45 байт на байт XML: на потолке это около 0,6–0,75 ГБ. Больше — не
#: спецификация, а zip-бомба.
MAX_DOCUMENT_XML_BYTES = 16 * 1024 * 1024


class UnsafeDocument(ValueError):
    """`.docx` не разбираем: в `word/document.xml` есть DTD (Word его не пишет, а через
    него идут атаки на XML) или он больше :data:`MAX_DOCUMENT_XML_BYTES`."""


def _refuse_dtd(xml: bytes) -> None:
    """Отказать документу с DTD до того, как его разберёт ElementTree.

    Обе классические атаки на XML живут в DTD: внешние сущности (чтение файлов, запросы
    наружу) и «миллиард смешков» (экспоненциальное раскрытие внутренних сущностей).
    Штатный expat внешние сущности не загружает, а от раскрытия защищён только с версии
    2.4 — какая expat под интерпретатором потребителя, пакет не выбирает. Word в
    `document.xml` DTD не пишет никогда, поэтому проще и надёжнее не разбирать DTD вовсе:
    предварительный проход голым expat останавливается на первом же объявлении, раньше
    любой подстановки. Так же устроен `defusedxml` (`forbid_dtd`), но без новой
    зависимости в открытом пакете.
    """
    from xml.parsers import expat  # noqa: PLC0415

    def refuse(*_: Any) -> None:
        raise UnsafeDocument("word/document.xml содержит DTD — такой .docx не разбираем")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    try:
        parser.Parse(xml, True)
    except expat.ExpatError:
        # Синтаксис проверяет ElementTree: битый XML падает `ET.ParseError`. DTD до места
        # ошибки к этому моменту уже отклонён.
        pass


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

    name = "word/document.xml"
    with zipfile.ZipFile(path) as archive:
        # Объявленный размер проверяется до распаковки, а читается не больше потолка:
        # заголовок архива может и соврать.
        try:
            declared = archive.getinfo(name).file_size
        except KeyError:
            raise ValueError(f"{path}: в архиве нет {name} — это не документ Word") from None
        if declared > MAX_DOCUMENT_XML_BYTES:
            raise UnsafeDocument(
                f"{name}: {declared} байт после распаковки, потолок {MAX_DOCUMENT_XML_BYTES}"
            )
        with archive.open(name) as member:
            xml = member.read(MAX_DOCUMENT_XML_BYTES + 1)
        if len(xml) > MAX_DOCUMENT_XML_BYTES:
            raise UnsafeDocument(f"{name}: больше потолка {MAX_DOCUMENT_XML_BYTES} байт")
    _refuse_dtd(xml)
    # Подавление B314 обосновано: документ без DTD (проверено выше) не может объявить ни
    # внешних, ни внутренних сущностей — разбирать штатным ElementTree безопасно.
    body = ET.fromstring(xml).find(f"{_W}body")  # nosec B314
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
    seen: Optional[Dict[str, int]] = None,
) -> List[Requirement]:
    """Заголовок и всё до следующего заголовка того же или старшего уровня.

    Текст раздела **включает вложенные подразделы**: у раздела «6» должно быть описание
    целиком, а у «6.5» — своё, более узкое. Одно предложение попадает в два требования,
    и это верно: они разной крупности, и карта показывает их на разных уровнях.
    Рядом лежит `lead` — собственный текст раздела до первого подзаголовка.
    *seen* — общий счётчик идентификаторов при нарезке нескольких документов: повтор
    получает следующий суффикс `~2`, `~3`… в порядке файлов и заголовков. Без аргумента
    поведение одного документа прежнее.
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
    seen = {} if seen is None else seen
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
        base = f"{prefix}-{section}"
        rid = base
        # Два раздела с одним номером — не редкость в живых документах. Молча слить их
        # значило бы потерять одно требование целиком. Проверяем и исходный ID, и каждый
        # сгенерированный суффикс: такой ID мог уже встретиться как естественный slug.
        suffix = seen.get(base, 1)
        while rid in seen:
            suffix += 1
            rid = f"{base}~{suffix}"
        seen[base] = suffix
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


DEFAULT_HEADING_STYLES = {
    "heading1": 1,
    "heading2": 2,
    "heading3": 3,
    "heading 1": 1,
    "heading 2": 2,
    "heading 3": 3,
}


@dataclass(frozen=True)
class _SpecFile:
    file: str
    source: str
    prefix: Optional[str]


def _files(section: Mapping[str, Any]) -> List[_SpecFile]:
    raw = section.get("files")
    if not isinstance(raw, list) or not raw:
        raise ValueError(t(
            "specs.files: ожидался непустой список путей к .docx или объектов "
            "{file, label, prefix}",
            "specs.files: expected a non-empty list of .docx paths or "
            "{file, label, prefix} objects",
        ))

    out: List[_SpecFile] = []
    for index, item in enumerate(raw):
        where = f"specs.files[{index}]"
        if isinstance(item, str):
            if not item.strip():
                raise ValueError(t(f"{where}: ожидался непустой путь строкой",
                                   f"{where}: expected a non-empty path string"))
            out.append(_SpecFile(file=item, source=item, prefix=None))
            continue
        if not isinstance(item, Mapping):
            raise ValueError(t(
                f"{where}: ожидался путь строкой или объект {{file, label, prefix}}",
                f"{where}: expected a path string or a {{file, label, prefix}} object",
            ))
        unknown = set(item) - {"file", "label", "prefix"}
        if unknown:
            names = ", ".join(sorted(str(key) for key in unknown))
            raise ValueError(t(f"{where}: неизвестные ключи: {names}",
                               f"{where}: unknown keys: {names}"))
        values = {}
        for key in ("file", "label", "prefix"):
            value = item.get(key)
            if key in item and (not isinstance(value, str) or not value.strip()):
                raise ValueError(t(f"{where}.{key}: ожидалась непустая строка",
                                   f"{where}.{key}: expected a non-empty string"))
            values[key] = value
        if not isinstance(values["file"], str):
            raise ValueError(t(f"{where}.file: ожидался непустой путь строкой",
                               f"{where}.file: expected a non-empty path string"))
        out.append(_SpecFile(
            file=values["file"],
            source=values["label"] if values["label"] is not None else values["file"],
            prefix=values["prefix"],
        ))
    return out


def _styles(section: Mapping[str, Any]) -> Dict[str, int]:
    raw = section.get("heading_styles")
    if raw is None:
        return dict(DEFAULT_HEADING_STYLES)
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(t(
            "specs.heading_styles: ожидался непустой словарь «стиль: уровень»",
            "specs.heading_styles: expected a non-empty mapping of style to level",
        ))
    if any(not isinstance(name, str) or not name.strip()
           or not isinstance(level, int) or isinstance(level, bool) or level < 1
           for name, level in raw.items()):
        raise ValueError(t(
            "specs.heading_styles: стили — непустые строки, уровни — положительные целые",
            "specs.heading_styles: styles must be non-empty strings and levels positive integers",
        ))
    return {name.lower(): level for name, level in raw.items()}


def _synonyms(section: Mapping[str, Any]) -> Mapping[str, str]:
    raw = section.get("synonyms")
    if raw is None:
        raw = {}
    if (not isinstance(raw, Mapping)
            or any(not isinstance(word, str) or not isinstance(table, str)
                   for word, table in raw.items())):
        raise ValueError(t(
            "specs.synonyms: ожидался словарь строк «слово: таблица»",
            "specs.synonyms: expected a string mapping of word to table",
        ))
    return raw


def _tables(cfg: Config, scope: Scope) -> Set[str]:
    schema = artifacts.read(cfg.path_for("db.schema", scope=scope))
    return {
        str(table["name"])
        for database in schema.get("databases") or []
        if isinstance(database, Mapping)
        for table in database.get("tables") or []
        if isinstance(table, Mapping) and table.get("name")
    }


def collect(config: Optional[Config] = None, *, scope: Optional[Scope] = None) -> Dict[str, Any]:
    """Прочитать все настроенные `.docx` и слепить содержание артефакта.

    Все файлы читаются до записи: ошибка в любом из них не оставляет частичный артефакт.
    Относительные пути разрешаются от каталога `atlas.yaml`. В `source` сохраняется `label`
    записи, а если его нет — путь как объявлен. Список таблиц берётся из уже снятого
    `db.schema` той же области; без него ставятся только якоря API.
    """
    cfg = config or load()
    where = scope or cfg.scope
    section = cfg.section("specs")
    files = _files(section)
    styles = _styles(section)
    prefix = section.get("prefix", "REQ")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError(t("specs.prefix: ожидалась непустая строка",
                           "specs.prefix: expected a non-empty string"))
    min_chars = non_negative(section, "min_chars", 0, int, where="specs")
    synonyms = _synonyms(section)
    api_prefix = cfg.project.get("api_prefix") or ""
    if not isinstance(api_prefix, str):
        raise ValueError(t("project.api_prefix: ожидалась строка",
                           "project.api_prefix: expected a string"))

    requirements: List[Requirement] = []
    texts: List[str] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(files):
        # Общий ресурс отклоняется до `Path.is_file()` и `ZipFile`: обе операции на UNC
        # могут уйти в SMB. Проверка и текст отказа принадлежат владельцу формата.
        artifacts._refuse_unc(entry.file, f"specs.files[{index}]")
        path = Path(entry.file)
        if not path.is_absolute():
            path = cfg.root / path
        if not path.is_file():
            raise ValueError(t(f"specs.files: нет файла {entry.file}",
                               f"specs.files: no such file: {entry.file}"))
        try:
            blocks = read_docx(path)
        except Exception as exc:
            raise ValueError(t(f"specs.files: {entry.file} не прочитан — {exc}",
                               f"specs.files: cannot read {entry.file} — {exc}")) from exc
        texts.extend(block.text for block in blocks)
        requirements.extend(slice_requirements(
            blocks,
            source=entry.source,
            styles=styles,
            prefix=entry.prefix if entry.prefix is not None else prefix,
            min_chars=min_chars,
            seen=seen,
        ))

    roots = api_roots(texts, api_prefix)
    tables = _tables(cfg, where)
    for requirement in requirements:
        requirement.anchors = extract_anchors(
            requirement.text, api_prefix=api_prefix, tables=tables, synonyms=synonyms, roots=roots,
        )
    return build(requirements)


def write(config: Optional[Config] = None, *, scope: Optional[Scope] = None) -> Dict[str, Any]:
    """Снять требования и положить артефакт. Возвращает записанное содержание."""
    cfg = config or load()
    where = scope or cfg.scope
    payload = collect(cfg, scope=where)
    return artifacts.write(
        cfg.path_for(KIND, scope=where), KIND, payload, scope=where,
        collector="partest-atlas/specs",
    )
