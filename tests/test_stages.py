"""Everything here runs without a network or a key: a scripted client stands in for the models."""
import json
import re

import pytest

from nifty import pages as page_files, project as project_module, stages
from nifty.llm import LLMError
from nifty.project import Project, ProjectError, Translation

MODELS = {"glossary": "g", "translate": "t", "translate_fallback": "tf", "proofread": ["p"], "proofread_advisory": ["adv"],
          "align": "a", "fallback": "f"}
RU = dict(language="Russian", script="cyrillic", models=MODELS)
RUN = {"concurrency": 1, "chunk_target_words": 900, "reference_chars": 1000, "style_chars": 100}


def page(text, printed="none", pid="p0001", condition="good"):
    return {"pid": pid, "printed": printed, "condition": condition, "text": text}


def words(n, word="ord"):
    return " ".join([word] * n)


class Scripted:
    """Answers by the kind of request; records what was asked."""
    spent = 0.0

    def __init__(self, translate=None, issues=None, rules=None):
        self.translate, self.issues, self.rules, self.calls = translate, issues or [], rules or [], []

    def chat(self, model, system, content, *, label, **kw):
        self.calls.append((model, label, system, content))
        if ":glossary" in label:
            return {"text": "## People\n- Olaf: Олаф"}
        if ":align" in label:
            return {"text": json.dumps({"rules": self.rules})}
        if ":proofread" in label:
            return {"text": json.dumps({"issues": self.issues if model == "p" else []})}
        section = content.rsplit("Translate this section:\n\n", 1)[1]
        return {"text": self.translate(section, model)}

    def chat_with_fallback(self, models, system, content, **kw):
        return self.chat(models[0], system, content, **kw)


def to_russian(section, model):
    return re.sub(r"[^\W\d_]+", lambda m: "слово" if m.group(0)[0].isalpha() and m.group(0)[0] != "p" else m.group(0), section)


@pytest.fixture
def book(tmp_path, monkeypatch):
    def make(translation=None, references=()):
        t = translation or Translation(**RU)
        monkeypatch.setattr(Project, "work", tmp_path / "work")
        monkeypatch.setattr(Project, "output", tmp_path / "output")
        return Project(name="b", dir=tmp_path, sources=[], source_language="Norwegian", translations=[t],
                       models=MODELS, run=RUN, about="A diary.", references=list(references)), t
    return make


def test_readers_differing_only_in_marks_keep_every_mark():
    assert stages.merge_uncertainty("en [?god] dag", "en god [?dag]") == "en [?god] [?dag]"
    assert stages.merge_uncertainty("en god dag", "en ond dag") is None


def test_typography_is_not_a_difference():
    assert stages.word_diffs("2ᵈᵉⁿ – „ja”", '2den - "ja"') == []


def test_late_scanned_page_finds_its_place_and_unnumbered_pages_follow_theirs():
    order = stages.book_order([page("a", "5", "p1"), page("b", "none", "p2"), page("c", "4", "p3")])
    assert [p["pid"] for p in order] == ["p3", "p1", "p2"]


def test_a_jump_in_page_numbers_divides_sections_and_is_recorded():
    chunks = stages.build_chunks([page(words(50), "1"), page(words(50), "4", "p0002")], 900)
    assert len(chunks) == 2
    assert chunks[1]["gap_before"] == "2-3"


def test_a_sentence_crossing_a_page_stays_in_one_section():
    chunks = stages.build_chunks([page("## Mandag\nHan gik", "1"), page("hjem igjen.", "2", "p0002")], 900)
    assert len(chunks) == 1
    assert chunks[0]["markers"] == ["⟦p1⟧", "⟦p2⟧"]
    assert chunks[0]["source"].startswith("⟦p1⟧\n## Mandag")


def test_text_without_headings_is_cut_at_paragraphs():
    text = "\n\n".join(words(400) for _ in range(20))
    chunks = stages.build_chunks([page(text)], 900)
    assert len(chunks) > 3
    assert max(len(c["source"].split()) for c in chunks) < 2700
    assert sum(c["source"].count("ord") for c in chunks) == 8000


def source_and_good_text():
    return "⟦p1⟧\n## Dag\n" + words(100, "hund") + " [?katt] [illegible]", "⟦p1⟧\n## День\n" + words(100, "собака") + " [?кот] [illegible]"


def test_a_sound_translation_passes():
    source, good = source_and_good_text()
    assert stages.check_translation(source, good, Translation(**RU)) == []


@pytest.mark.parametrize("change, expected", [
    (lambda t: t.replace("⟦p1⟧", ""), "page markers"),
    (lambda t: t.replace("[?кот]", "кот"), "[?] marks"),
    (lambda t: t.replace("[illegible]", "текст"), "[illegible]"),
    (lambda t: t.replace("## ", ""), "heading count"),
    (lambda t: "⟦p1⟧\n## День [?кот] [illegible]", "length ratio"),
    (lambda t: "I am unable to translate this. " + t, "refusal"),
    (lambda t: t + " " + words(9, "dog"), "outside cyrillic"),
])
def test_the_checks_catch(change, expected):
    source, good = source_and_good_text()
    assert any(expected in p for p in stages.check_translation(source, change(good), Translation(**RU)))


def test_a_copyright_line_late_in_the_book_is_not_a_refusal():
    source = words(100, "hund")
    assert stages.check_translation(source, words(100, "dogs") + " copyrighted", Translation("English", MODELS)) == []


def test_foreign_words_the_source_quotes_are_allowed():
    source = "⟦p1⟧ " + words(100, "hund")
    text = "⟦p1⟧ " + words(100, "собака") + " " + words(9, "Bier")
    assert stages.check_translation(source, text, Translation(**RU, allow_foreign=["Bier"])) == []


def test_text_left_in_the_source_language_is_caught():
    source = " ".join(f"ordet{i}" for i in range(100))
    assert any("left untranslated" in p for p in stages.check_translation(source, source, Translation("English", MODELS)))


def test_failed_output_is_retried_with_the_problems_then_sent_to_the_fallback(book):
    project, t = book()
    answers = iter(["Sorry, I cannot translate this.", "still a refusal: cannot translate", "⟦p1⟧ перевод"])
    client = Scripted(translate=lambda section, model: next(answers))
    result = stages.translate_section(client, t, "sys", {"id": "c1", "source": "⟦p1⟧ tekst"}, "Translate this section:\n\n⟦p1⟧ tekst")
    assert result["model"] == "tf"
    assert "failed these checks" in client.calls[1][3]


def test_a_section_that_cannot_be_translated_says_so(book):
    project, t = book()
    client = Scripted(translate=lambda section, model: "cannot translate")
    result = stages.translate_section(client, t, "sys", {"id": "c1", "source": "⟦p1⟧ tekst"}, "Translate this section:\n\n⟦p1⟧ tekst")
    assert result["text"] == "" and result["problems"][0] == "NOT TRANSLATED"


def test_a_breaking_edit_is_reverted_alone_and_the_good_one_stays(book):
    project, t = book()
    (project.work / "russian" / "proofread").mkdir(parents=True)
    source, good = source_and_good_text()
    issues = [{"type": "language", "find": "⟦p1⟧\n## День", "replace": "День"},
              {"type": "language", "find": "[?кот] [illegible]", "replace": "кот"},
              {"type": "language", "find": "собака [?кот]", "replace": "пёс [?кот]"}]
    chunk = {"id": "c1", "source": source, "text": good, "problems": [], "gap_before": None}
    result = stages.proofread_section(Scripted(issues=issues), project, t, chunk, "sys", "gl")
    assert "пёс [?кот]" in result["text"] and "⟦p1⟧" in result["text"]
    assert [i["status"].split(":")[0] for i in result["issues"]] == ["not_applied", "reverted", "applied"]


def test_an_edit_that_alters_a_marker_or_invents_a_placeholder_is_refused():
    issues = [{"find": "⟦p1⟧ a", "replace": "a"}, {"find": "b", "replace": "[rest of text]"}, {"find": "c", "replace": "d"}]
    assert stages.apply_edits("⟦p1⟧ a b c", issues) == "⟦p1⟧ a b d"
    assert [i["status"] for i in issues][2] == "applied"


def test_a_failed_proofreader_is_tried_again_next_run(book):
    project, t = book()
    class Broken(Scripted):
        def chat_with_fallback(self, *a, **k):
            raise LLMError("no answer")
    chunk = {"id": "c1", "source": "⟦p1⟧ tekst", "text": "⟦p1⟧ перевод", "problems": [], "gap_before": None}
    (project.work / "russian" / "proofread").mkdir(parents=True)
    result = stages.proofread_section(Broken(), project, t, chunk, "sys", "gl")
    assert result["text"] == "⟦p1⟧ перевод" and "failed" in result["problems"][0]
    assert not list((project.work / "russian" / "proofread").iterdir())


def test_align_replaces_whole_words_only_and_refuses_words_not_in_the_text(book):
    project, t = book()
    chunks = [{"id": "c1", "source": "Olav " + words(3), "text": "Олав и Олава, Олавская улица", "problems": []}]
    rules = [{"find": "Олав", "replace": "Олаф"}, {"find": "Олава", "replace": "Олафа"}, {"find": "Петров", "replace": "Пётр"}]
    final, kept = stages.align_sections(Scripted(rules=rules), project, t, chunks, "gl")
    assert final[0]["text"] == "Олаф и Олафа, Олавская улица"
    assert kept[2]["status"].startswith("not_applied")


def test_align_keeps_a_section_it_would_break(book):
    project, t = book()
    chunks = [{"id": "c1", "source": "⟦p1⟧ Olav", "text": "⟦p1⟧ Олав", "problems": []}]
    # A word of the text that a broken rule would turn into a second [?] mark.
    final, _ = stages.align_sections(Scripted(rules=[{"find": "Олав", "replace": "[?Олаф]"}]), project, t, chunks, "gl")
    assert final[0]["text"] == "⟦p1⟧ Олав" and "alignment skipped" in final[0]["problems"][0]


def test_an_existing_glossary_is_never_overwritten(book, tmp_path):
    project, t = book()
    (tmp_path / "glossary.russian.md").write_text("mine")
    client = Scripted()
    assert stages.write_glossary(client, project, t, [{"source": "x"}], trial=False) == "mine"
    assert not client.calls


def test_a_trial_glossary_stays_out_of_the_project_folder(book, tmp_path):
    project, t = book()
    stages.write_glossary(Scripted(), project, t, [{"source": "x"}], trial=True)
    assert not (tmp_path / "glossary.russian.md").exists()
    stages.write_glossary(Scripted(), project, t, [{"source": "x"}], trial=False)
    assert (tmp_path / "glossary.russian.md").exists()


def test_references_and_book_reach_the_glossary_and_a_missing_reference_stops_the_run(book, tmp_path):
    (tmp_path / "wiki.md").write_text("Олаф Бьолан был лыжником")
    project, t = book(references=["wiki.md"])
    client = Scripted()
    stages.write_glossary(client, project, t, [{"source": "⟦p1⟧ Bjaaland"}], trial=False)
    assert "Олаф Бьолан" in client.calls[0][3] and "Bjaaland" in client.calls[0][3] and "⟦p1⟧" not in client.calls[0][3]
    (tmp_path / "glossary.russian.md").unlink()
    project.references = ["gone.md"]
    with pytest.raises(ProjectError):
        stages.write_glossary(Scripted(), project, t, [{"source": "x"}], trial=False)


def test_a_later_language_sees_the_earlier_one_as_a_cross_check(book, tmp_path):
    project, t = book(Translation(**RU, cross_check="English"))
    project.translations.insert(0, Translation("English", MODELS))
    chunk = {"id": "c1", "source": "⟦p1⟧ tekst", "problems": [], "gap_before": None}
    english = project.work / "english" / "final"
    english.mkdir(parents=True)
    (english / f"{stages.text_key(chunk['source'])}.json").write_text(json.dumps({"text": "⟦p1⟧ text"}))
    client = Scripted(translate=lambda s, m: "⟦p1⟧ перевод")
    stages.translate_sections(client, project, t, [chunk], "gl")
    assert "English translation, for reference only:\n text" in client.calls[0][3]


def test_saved_sections_cost_nothing_on_a_rerun(book):
    project, t = book()
    chunk = {"id": "c1", "source": "⟦p1⟧ tekst", "problems": [], "gap_before": None}
    client = Scripted(translate=lambda s, m: "⟦p1⟧ перевод")
    stages.translate_sections(client, project, t, [chunk], "gl")
    stages.translate_sections(client, project, t, [chunk], "gl")
    assert len(client.calls) == 1


def test_text_sources_need_no_reading_and_end_up_in_the_report(book, tmp_path):
    project, t = book()
    source = tmp_path / "book.md"
    source.write_text("## Kapittel\nDet var en gang.")
    ids = page_files.extract_book([source], project.work / "pages")
    client = Scripted(translate=lambda s, m: "⟦p0001⟧\n## Глава\nЖил-был.")
    pages = stages.transcribe_all(client, project, ids)
    chunks = stages.build_chunks(stages.book_order(pages), 900)
    drafts = stages.translate_sections(client, project, t, chunks, "gl")
    checked = stages.proofread_sections(client, project, t, drafts, "gl")
    final, rules = stages.align_sections(client, project, t, checked, "gl")
    path = stages.assemble(project, t, pages, final, rules, trial=False)
    assert "Жил-был" in path.read_text()
    assert (path.parent / "translation.qa_report.md").exists()
    assert not any(":reader" in c[1] for c in client.calls)
    assert (project.work / "russian" / "final").exists()


def write_project(tmp_path, extra=""):
    (tmp_path / "project.toml").write_text(f'name = "b"\nsources = ["x.md"]\nsource_language = "Norwegian"\n{extra}\n'
                                           '[[translation]]\nlanguage = "English"\n[[translation]]\nlanguage = "Russian"\n'
                                           'cross_check = "English"\n[translation.models]\ntranslate = "x/y"\n')
    return tmp_path


def test_settings_merge_with_the_defaults(tmp_path):
    p = project_module.load(write_project(tmp_path, '[models]\nglossary = "claude-haiku-5-5"\n'))
    assert p.translations[0].models["glossary"] == "claude-haiku-5-5"
    assert p.translations[1].models["translate"] == "x/y" and p.translations[0].models["translate"] == "claude-opus-5-5"
    assert p.run["chunk_target_words"] == 900


@pytest.mark.parametrize("extra, message", [
    ('[models]\nocr_a = "claude-opus-5-5"\n', "OpenRouter"),
    ('langauge = "typo"\n', "langauge"),
])
def test_bad_settings_stop_early(tmp_path, extra, message):
    with pytest.raises(ProjectError, match=message):
        project_module.load(write_project(tmp_path, extra))


def test_a_cross_check_must_come_first(tmp_path):
    (tmp_path / "project.toml").write_text('name = "b"\nsources = ["x.md"]\nsource_language = "N"\n'
                                           '[[translation]]\nlanguage = "Russian"\ncross_check = "English"\n'
                                           '[[translation]]\nlanguage = "English"\n')
    with pytest.raises(ProjectError, match="listed before"):
        project_module.load(tmp_path)


def test_progress_reports_work_and_summarises_what_was_saved(book, capsys, monkeypatch):
    from nifty import progress
    project, t = book()
    chunks = [{"id": f"c{i}", "source": f"⟦p{i}⟧ tekst", "problems": [], "gap_before": None} for i in range(1, 4)]
    client = Scripted(translate=lambda s, m: s.replace("tekst", "текст"))
    monkeypatch.setattr(progress, "SAVED_SECONDS", -1)  # the scripted client is as quick as a saved result
    stages.translate_sections(client, project, t, chunks, "gl")
    first = capsys.readouterr().out
    assert "[russian translate] 3 to do" in first and "1/3 (33%) c1" in first and "left" in first and "3/3 (100%) c3" in first
    monkeypatch.undo()
    stages.translate_sections(client, project, t, chunks, "gl")
    second = capsys.readouterr().out
    assert "1/3" not in second and "3/3 (100%) c3" in second and "3 already saved" in second


def test_template_is_a_valid_project(tmp_path):
    from nifty.llm import ROOT
    (tmp_path / "project.toml").write_text((ROOT / "project.template.toml").read_text().replace("NAME", "x"))
    p = project_module.load(tmp_path)
    assert p.name == "x" and p.translations[0].language == "English"


def test_the_estimate_multiplies_page_work_but_not_one_off_calls(tmp_path, monkeypatch):
    from nifty import __main__ as cli
    ledger = tmp_path / "ledger.jsonl"
    rows = [("b", "p0001:reader_a", 1.0), ("b", "c0001:russian", 1.0), ("b", "russian:glossary", 5.0),
            ("b", "russian:align", 2.0), ("other", "c0001:russian", 100.0)]
    ledger.write_text("".join(json.dumps({"project": p, "label": l, "cost": c, "model": "m"}) + "\n" for p, l, c in rows))
    monkeypatch.setattr(cli, "LEDGER", ledger)
    assert cli.whole_book_estimate("b", 10, 100) == 27.0
