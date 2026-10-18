"""Чтение `.docx`: настоящий файл, собранный тестом.

Подделывать XML Word руками — значит проверять свои представления о формате. Документ
здесь собирается как настоящий архив, и читается тем же кодом, что и файл от аналитика.
"""

from __future__ import annotations

import zipfile

import pytest

from partest_atlas.collect import specs
from partest_atlas.collect.specs import read_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def абзац(text, style=None, *, разрывы=False):
    pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    if разрывы:
        return f"<w:p>{pr}<w:r><w:t>{text}</w:t><w:br/><w:t>вторая строка</w:t></w:r></w:p>"
    return f"<w:p>{pr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def таблица(*строки):
    tr = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
        for row in строки
    )
    return f"<w:tbl>{tr}</w:tbl>"


@pytest.fixture()
def документ(tmp_path):
    def собрать(*куски):
        path = tmp_path / "спека.docx"
        xml = f'<w:document xmlns:w="{W}"><w:body>{"".join(куски)}</w:body></w:document>'
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("word/document.xml", xml)
        return path

    return собрать


def test_paragraphs_come_out_in_reading_order(документ):
    blocks = read_docx(документ(абзац("первый"), абзац("второй")))
    assert [b.text for b in blocks] == ["первый", "второй"]


def test_the_style_is_kept_because_slicing_needs_it(документ):
    """Нарезка опирается на стиль, а не на вид строки: списки «1. База» выглядят как
    заголовки и заголовками не являются."""
    blocks = read_docx(документ(абзац("6 Сплит", "Heading1")))
    assert blocks[0].style == "Heading1"


def test_empty_paragraphs_are_dropped(документ):
    blocks = read_docx(документ(абзац("текст"), "<w:p/>", абзац("ещё")))
    assert [b.text for b in blocks] == ["текст", "ещё"]


def test_line_breaks_inside_a_paragraph_survive(документ):
    """Разрыв внутри абзаца — часть смысла: перечисление склеилось бы в одну строку."""
    blocks = read_docx(документ(абзац("первая", разрывы=True)))
    assert blocks[0].text == "первая\nвторая строка"


def test_a_table_becomes_searchable_lines(документ):
    """В спецификациях таблицы несут атрибуты сущностей — самое ценное для якорей.
    Вёрстка теряется сознательно: нужен текст для поиска."""
    blocks = read_docx(документ(таблица(["поле", "тип"], ["name", "varchar"])))
    assert blocks[0].kind == "table"
    assert blocks[0].text == "поле | тип\nname | varchar"


def test_empty_cells_do_not_leave_stray_separators(документ):
    blocks = read_docx(документ(таблица(["а", "", "б"])))
    assert blocks[0].text == "а | б"


def test_paragraphs_inside_a_table_are_not_counted_twice(документ):
    """Идём по прямым детям `body`. По всему дереву абзацы таблицы попали бы в поток
    дважды и разорвали бы раздел: заголовок отделился бы от своего текста."""
    blocks = read_docx(документ(абзац("до"), таблица(["в таблице"]), абзац("после")))
    assert [b.kind for b in blocks] == ["para", "table", "para"]
    assert [b.text for b in blocks] == ["до", "в таблице", "после"]


def test_a_code_style_is_marked_as_code(документ):
    blocks = read_docx(документ(абзац("SELECT 1", "SourceCode")))
    assert blocks[0].kind == "code"


def test_a_document_without_a_body_reads_as_nothing(tmp_path):
    path = tmp_path / "пустая.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"/>')
    assert read_docx(path) == []


def test_reading_and_slicing_work_together(документ):
    """Сквозная проверка: файл → блоки → требования. Разделены они внутри, а снаружи
    обязаны сходиться."""
    path = документ(
        абзац("6 Сплит", "Heading1"),
        абзац("меняет таблицу task"),
        абзац("6.5 Расчёт", "Heading2"),
        абзац("вызывает /api/v1/splits/1/calculate"),
    )
    blocks = read_docx(path)
    req = specs.slice_requirements(
        blocks, source="спека.docx", styles={"heading1": 1, "heading2": 2}
    )
    assert [r.id for r in req] == ["REQ-6", "REQ-6.5"]

    якоря = specs.extract_anchors(req[1].text, api_prefix="/api/v1", tables={"task"})
    assert якоря["api"] == ["/api/v1/splits/{id}/calculate"]


# --- враждебный XML --------------------------------------------------------
#
# Документ от аналитика — чужой файл. Обе классические атаки на XML живут в DTD, а Word
# его в `document.xml` не пишет: документ с DTD отклоняется до разбора, а не «разбирается
# осторожно» в надежде на версию expat у потребителя.


def _архив(tmp_path, xml):
    path = tmp_path / "враждебная.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)
    return path


МИЛЛИАРД_СМЕШКОВ = (
    '<?xml version="1.0"?>\n<!DOCTYPE lolz [\n<!ENTITY lol "lol">\n'
    + "".join(
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">\n'
        for i in range(1, 10)
    )
    + f']>\n<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&lol9;</w:t></w:r></w:p>'
    "</w:body></w:document>"
)

ВНЕШНЯЯ_СУЩНОСТЬ = (
    '<?xml version="1.0"?>\n<!DOCTYPE d [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
    f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p>'
    "</w:body></w:document>"
)


@pytest.mark.parametrize("xml", [МИЛЛИАРД_СМЕШКОВ, ВНЕШНЯЯ_СУЩНОСТЬ], ids=["laughs", "xxe"])
def test_a_document_with_a_dtd_is_refused(tmp_path, xml):
    with pytest.raises(specs.UnsafeDocument):
        read_docx(_архив(tmp_path, xml))


def test_a_bare_doctype_is_refused_too(tmp_path):
    """Отказ по самому объявлению, а не по тому, что в нём: разбирать DTD не нужно вовсе."""
    xml = f'<!DOCTYPE d><w:document xmlns:w="{W}"><w:body/></w:document>'
    with pytest.raises(specs.UnsafeDocument):
        read_docx(_архив(tmp_path, xml))


def test_a_broken_xml_still_fails_as_elementtree_says(tmp_path):
    """Предварительный проход не подменяет тип ошибки: о синтаксисе говорит ElementTree."""
    import xml.etree.ElementTree as ET

    with pytest.raises(ET.ParseError):
        read_docx(_архив(tmp_path, f'<w:document xmlns:w="{W}"><w:body>'))


def test_a_document_over_the_ceiling_is_refused_before_reading(tmp_path, monkeypatch):
    """Zip-бомба: сжатое — килобайты, распакованное — гигабайты."""
    monkeypatch.setattr(specs, "MAX_DOCUMENT_XML_BYTES", 1000)
    xml = f'<w:document xmlns:w="{W}"><w:body>' + "<w:p/>" * 1000 + "</w:body></w:document>"
    with pytest.raises(specs.UnsafeDocument, match="потолок"):
        read_docx(_архив(tmp_path, xml))


def test_a_document_under_the_ceiling_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(specs, "MAX_DOCUMENT_XML_BYTES", 10_000)
    xml = f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>есть</w:t></w:r></w:p></w:body></w:document>'
    assert [b.text for b in read_docx(_архив(tmp_path, xml))] == ["есть"]


def test_the_ceiling_itself_is_allowed(tmp_path, monkeypatch):
    """Граница — «больше потолка», а не «не меньше»."""
    xml = f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>к</w:t></w:r></w:p></w:body></w:document>'
    monkeypatch.setattr(specs, "MAX_DOCUMENT_XML_BYTES", len(xml.encode("utf-8")))
    assert [b.text for b in read_docx(_архив(tmp_path, xml))] == ["к"]
    monkeypatch.setattr(specs, "MAX_DOCUMENT_XML_BYTES", len(xml.encode("utf-8")) - 1)
    with pytest.raises(specs.UnsafeDocument):
        read_docx(_архив(tmp_path, xml))


def test_an_archive_without_the_document_says_so(tmp_path):
    path = tmp_path / "не-ворд.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.txt", "привет")
    with pytest.raises(ValueError, match="нет word/document.xml"):
        read_docx(path)
