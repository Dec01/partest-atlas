"""Нарезка требований и якоря: без документов, на блоках."""

from __future__ import annotations

import pytest

from partest_atlas import artifacts
from partest_atlas.collect import specs
from partest_atlas.collect.specs import Block, build, extract_anchors, norm_api, slice_requirements

СТИЛИ = {"heading 1": 1, "heading 2": 2, "heading 3": 3}


def h(text, level=1):
    return Block(text=text, style=f"Heading {level}")


def p(text):
    return Block(text=text, style="Normal")


def нарезать(blocks, **kw):
    return slice_requirements(blocks, source="док.docx", styles=СТИЛИ, **kw)


def test_a_section_takes_everything_until_the_next_heading_of_its_level():
    req = нарезать([h("6 Сплит"), p("описание"), h("7 Другое"), p("чужое")])
    assert req[0].text == "описание"


def test_a_section_includes_its_subsections():
    """У раздела «6» должно быть описание целиком, у «6.5» — своё, более узкое."""
    blocks = [h("6 Сплит"), p("общее"), h("6.5 Расчёт", 2), p("узкое")]
    шесть, пять = нарезать(blocks)
    assert "общее" in шесть.text and "узкое" in шесть.text
    assert пять.text == "узкое"


def test_lead_is_the_section_own_text_before_the_first_subsection():
    blocks = [h("6 Сплит"), p("общее"), h("6.5 Расчёт", 2), p("узкое")]
    шесть = нарезать(blocks)[0]
    assert шесть.lead == "общее"


def test_style_beats_numbering():
    """В документах есть списки вида «1. База», которые выглядят как заголовки.
    Нарезка по виду строки разрезала бы требование посреди перечисления."""
    blocks = [h("6 Сплит"), p("1. База"), p("2. Шина")]
    req = нарезать(blocks)
    assert len(req) == 1
    assert "1. База" in req[0].text


def test_the_section_number_becomes_the_identifier():
    assert нарезать([h("6.5 Расчёт"), p("х")])[0].id == "REQ-6.5"


def test_a_heading_without_a_number_gets_a_slug():
    req = нарезать([h("Общие положения"), p("х")])[0]
    assert req.id.startswith("REQ-")
    assert "общие" in req.id


def test_duplicate_numbers_do_not_collapse_into_one():
    """Два раздела с одним номером — не редкость в живых документах. Слить их молча
    значит потерять требование целиком."""
    req = нарезать([h("6 Первый"), p("а"), h("6 Второй"), p("б")])
    assert [r.id for r in req] == ["REQ-6", "REQ-6~2"]
    assert [r.text for r in req] == ["а", "б"]


def test_natural_slug_does_not_reuse_a_generated_suffix():
    req = нарезать([
        h("1 Первый"), p("а"),
        h("1 Второй"), p("б"),
        h("1~2"), p("в"),
    ])

    assert [r.id for r in req] == ["REQ-1", "REQ-1~2", "REQ-1~2~2"]
    assert [r.text for r in req] == ["а", "б", "в"]


def test_generated_suffixes_skip_natural_slugs_across_documents():
    def read_documents():
        seen = {}
        first = нарезать([h("1~2"), p("занято")], seen=seen)
        second = нарезать([
            h("1 Первый"), p("а"),
            h("1 Второй"), p("б"),
            h("1 Третий"), p("в"),
        ], seen=seen)
        return [r.id for r in first + second]

    expected = ["REQ-1~2", "REQ-1", "REQ-1~3", "REQ-1~4"]
    assert read_documents() == expected
    assert read_documents() == expected


def test_short_sections_are_dropped_when_asked():
    """Заголовок без содержания — не требование."""
    assert нарезать([h("6 Пусто"), p("три"), h("7 Есть"), p("х" * 50)], min_chars=20)[0].id == "REQ-7"


def test_breadcrumbs_lead_up_the_levels():
    blocks = [h("6 Сплит"), p("а"), h("6.5 Расчёт", 2), p("б"), h("6.5.1 Шаг", 3), p("в")]
    шаг = нарезать(blocks)[-1]
    assert шаг.path == ["Сплит", "Расчёт"]


def test_the_top_level_has_no_breadcrumbs():
    assert нарезать([h("6 Сплит"), p("а")])[0].path == []


def test_children_are_counted():
    blocks = [h("6 Сплит"), p("а"), h("6.1 Раз", 2), p("б"), h("6.2 Два", 2), p("в")]
    assert нарезать(blocks)[0].children == 2


def test_a_document_without_headings_yields_nothing():
    assert нарезать([p("просто текст"), p("ещё")]) == []


# --- настройка документов ----------------------------------------------------


def test_spec_files_accept_strings_objects_and_a_mixed_list():
    entries = specs._files({"files": [
        "docs/a.docx",
        {"file": "docs/b.docx", "label": "Документ Б", "prefix": "API"},
        {"file": "docs/c.docx"},
    ]})

    assert [(entry.file, entry.source, entry.prefix) for entry in entries] == [
        ("docs/a.docx", "docs/a.docx", None),
        ("docs/b.docx", "Документ Б", "API"),
        ("docs/c.docx", "docs/c.docx", None),
    ]


@pytest.mark.parametrize("raw", [None, [], "docs/a.docx"])
def test_spec_files_require_a_non_empty_list(raw):
    with pytest.raises(ValueError, match="specs.files"):
        specs._files({"files": raw})


@pytest.mark.parametrize("entry", [
    "",
    " ",
    None,
    7,
    {},
    {"file": ""},
    {"file": " "},
    {"label": "Без файла"},
    {"file": "a.docx", "label": " "},
    {"file": "a.docx", "label": None},
    {"file": "a.docx", "prefix": ""},
    {"file": "a.docx", "prefix": None},
    {"file": "a.docx", "typo": "x"},
])
def test_invalid_spec_file_entries_name_their_index(entry):
    with pytest.raises(ValueError, match=r"specs\.files\[0\]"):
        specs._files({"files": [entry]})


def test_unknown_spec_file_keys_are_named():
    with pytest.raises(ValueError, match="typo"):
        specs._files({"files": [{"file": "a.docx", "typo": "x"}]})


@pytest.mark.parametrize("raw", [[], False, 0, ""])
def test_falsy_non_mapping_synonyms_are_rejected(raw):
    with pytest.raises(ValueError, match="specs.synonyms"):
        specs._synonyms({"synonyms": raw})


# publish-ok: фиктивные UNC-образцы негативного теста; Path.is_file и ZipFile заблокированы, сетевое обращение исключено
@pytest.mark.parametrize("path", [r"\\host\share\req.docx", "//host/share/req.docx",
                                  r"/\host\share\req.docx", r"\/host/share/req.docx"])
def test_specs_unc_is_refused_before_any_file_or_zip_access(tmp_path, monkeypatch, path):
    """Даже проверка существования UNC может уйти в SMB, поэтому сетевых операций ноль."""
    import zipfile
    from pathlib import Path

    from partest_atlas.config import Config

    cfg = Config(root=tmp_path, raw={"specs": {"files": [path]}})

    def forbidden(*args, **kwargs):
        pytest.fail("UNC дошёл до файловой или zip-операции")

    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(ValueError, match=r"specs\.files\[0\].*UNC") as отказ:
        specs.collect(cfg)
    assert "host" not in str(отказ.value)


# --- якоря -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("путь", "ожидалось"),
    [("/api/v1/items/1", "/api/v1/items/{id}"), ("/a/{x}/b", "/a/{id}/b"), ("/a", "/a")],
)
def test_identifiers_in_paths_are_normalised(путь, ожидалось):
    assert norm_api(путь) == ожидалось


def test_a_full_path_is_taken():
    out = extract_anchors("см. /api/v1/items/{id}", api_prefix="/api/v1")
    assert out["api"] == ["/api/v1/items/{id}"]


def test_a_relative_path_is_completed_only_for_known_roots():
    """Спецификация пишет пути без префикса. Без списка корней пришлось бы либо терять
    их, либо превращать в эндпоинты куски вроде `/withdraw` из колонки действий."""
    out = extract_anchors("см. /items/{id} и /withdraw", api_prefix="/api/v1", roots={"items"})
    assert out["api"] == ["/api/v1/items/{id}"]


def test_without_a_prefix_no_api_anchors_are_invented():
    assert "api" not in extract_anchors("/items/1", api_prefix="")


def test_a_real_table_name_is_anchored():
    assert extract_anchors("меняет таблицу task", tables={"task", "client"})["table"] == ["task"]


def test_a_synonym_leads_to_a_table_only_if_that_table_exists():
    """Иначе словарь тихо разъедется со схемой и требование повиснет в пустоту."""
    есть = extract_anchors("пакет сплитов", tables={"split_package"}, synonyms={"пакет сплитов": "split_package"})
    нет = extract_anchors("пакет сплитов", tables={"task"}, synonyms={"пакет сплитов": "split_package"})
    assert есть["table"] == ["split_package"]
    assert "table" not in нет


def test_nothing_recognisable_gives_no_anchors():
    """Пустой словарь якорей честнее выдуманной связи: карту читают как факт."""
    assert extract_anchors("свободная формулировка", tables={"task"}) == {}


def test_api_roots_are_learned_from_full_paths():
    assert specs.api_roots(["см. /api/v1/items/1 и /api/v1/tasks"], "/api/v1") == {"items", "tasks"}


# --- содержание артефакта --------------------------------------------------


def test_stats_agree_with_the_requirements_under_them():
    req = нарезать([h("6 Сплит"), p("а"), h("7 Другое"), p("б")])
    req[0].anchors = {"table": ["task"]}
    doc = build(req)
    assert doc["stats"] == {"requirements": 2, "sources": 1, "withAnchors": 1}


def test_the_artifact_it_writes_validates_clean(tmp_path):
    doc = build(нарезать([h("6 Сплит"), p("а")]))
    path = tmp_path / "specs.json"
    artifacts.write(path, specs.KIND, doc, collector="partest-atlas/specs")
    assert artifacts.validate(artifacts.read(path)) == []


def test_the_kind_is_one_the_contract_knows():
    assert specs.KIND in artifacts.KINDS
    assert artifacts.filename(specs.KIND) == "specs.json"
