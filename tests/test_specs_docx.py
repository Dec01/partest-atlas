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
