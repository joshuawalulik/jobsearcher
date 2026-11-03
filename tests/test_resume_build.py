"""Tests for the YAML → DOCX/HTML resume renderer (PDF needs Chrome and
is exercised manually, not here)."""

import docx
import yaml

import resume_build

SAMPLE = {
    "name": "ALEX J. CANDIDATE",
    "contact": "New York, NY  •  alex@example.com",
    "summary": "Full-stack engineer with React & .NET experience.",
    "body_pt": 11.5,
    "experience": [
        {"title": "Software Engineer", "company": "Acme Corp",
         "where": "New York, NY", "dates": "Jul 2024 – Present",
         "bullets": ["Migrated 6+ UI components to the new platform",
                     "Built campaign screens with React.js and Node.js"]},
    ],
    "skills": [{"label": "Frontend", "items": "React.js, Node.js"}],
    "education": [{"school": "State University", "where": "Springfield, NY",
                   "dates": "2019 – 2023",
                   "lines": ["BS, Computer Information Systems"]}],
    "certifications": [{"name": "Azure Fundamentals (AZ-900)", "issued": "Aug 2022"}],
}


def test_build_docx_contains_all_sections(tmp_path):
    out = tmp_path / "resume.docx"
    resume_build.build_docx(SAMPLE, out)
    texts = [p.text for p in docx.Document(out).paragraphs if p.text.strip()]
    joined = "\n".join(texts)
    for expected in ("ALEX J. CANDIDATE", "Summary", "Experience",
                     "Software Engineer | Acme Corp | New York, NY",
                     "Migrated 6+ UI components to the new platform", "Skills",
                     "Education", "Certifications"):
        assert expected in joined
    # Role line carries the right-aligned dates on the same paragraph.
    role = next(t for t in texts if t.startswith("Software Engineer"))
    assert role.endswith("Jul 2024 – Present")


def test_build_html_escapes_and_includes_content():
    data = dict(SAMPLE, summary="C# & <great> engineer")
    html_text = resume_build.build_html(data)
    assert "C# &amp; &lt;great&gt; engineer" in html_text
    assert "Experience" in html_text
    assert "Migrated 6+ UI components to the new platform" in html_text


def test_build_cli_writes_docx_without_chrome(tmp_path, monkeypatch):
    # Pretend Chrome is absent: DOCX must still be produced, PDF skipped.
    monkeypatch.setattr(resume_build, "CHROME", tmp_path / "nope")
    src = tmp_path / "resume.yaml"
    src.write_text(yaml.safe_dump(SAMPLE))
    written = resume_build.build(src, tmp_path / "out", basename="Resume_Acme")
    assert [p.name for p in written] == ["Resume_Acme.docx"]
    assert written[0].exists()


def test_docx_sets_explicit_defaults_and_times_new_roman(tmp_path):
    out = tmp_path / "resume.docx"
    resume_build.build_docx(SAMPLE, out)
    d = docx.Document(out)
    fonts = {r.font.name for p in d.paragraphs for r in p.runs}
    assert fonts == {"Times New Roman"}
    # Stage 5: explicit docDefaults with lineRule, so no renderer
    # substitutes its own spacing.
    from docx.oxml.ns import qn
    spacing = d.styles.element.find(
        f"{qn('w:docDefaults')}/{qn('w:pPrDefault')}/{qn('w:pPr')}/{qn('w:spacing')}"
    )
    assert spacing is not None
    assert spacing.get(qn("w:lineRule")) == "auto"
    assert spacing.get(qn("w:after")) == "0"


def test_check_flags_workflow_violations():
    bad = {**SAMPLE,
           "body_pt": 13,
           "summary": "One. Two. Three sentences here.",
           "experience": [{"title": "SWE", "company": "Acme", "where": "NYC",
                           "dates": "2024 - Current",
                           "bullets": ["Did a thing."]}],
           "skills": [{"label": "Frontend", "items": "React.js, Elm"}]}
    bad["experience"][0]["bullets"].append("Helped build the dashboard")
    problems = "\n".join(resume_build.check(bad))
    assert "en dash" in problems
    assert "Current" in problems
    assert "period" in problems
    assert "Elm" not in problems       # skills coverage is advisory, not a violation
    assert "band" in problems          # body_pt out of range
    assert "summary" in problems
    assert 'banned opener "helped"' in problems
    assert resume_build.check(SAMPLE) == []
    # Bank v19: a skill with no carrier bullet is a note (claimability is
    # the gate, checked by a human against the bank), never a failure.
    notes = "\n".join(resume_build.advisories(bad))
    assert "Elm" in notes
    assert resume_build.advisories(SAMPLE) == []


EXTENDED = dict(
    SAMPLE,
    projects=[{"name": "JobSearcher", "tech": "Python, FastAPI, SQLite",
               "link": "github.com/user/jobsearcher",
               "bullets": ["Built a job-search pipeline with FastAPI"]}],
    additional=[{"label": "Interests", "items": "Card collecting, gaming"},
                {"label": "Currently learning", "items": "Python, RAG"}],
)


def test_projects_and_additional_sections_render(tmp_path):
    out = tmp_path / "resume.docx"
    resume_build.build_docx(EXTENDED, out)
    texts = [p.text for p in docx.Document(out).paragraphs if p.text.strip()]
    joined = "\n".join(texts)
    for expected in ("Projects", "Built a job-search pipeline with FastAPI",
                     "Additional", "Interests: Card collecting, gaming",
                     "Currently learning: Python, RAG"):
        assert expected in joined
    # Project line: bold name, regular stack, link right-tabbed like dates.
    proj = next(t for t in texts if t.startswith("JobSearcher"))
    assert proj == "JobSearcher | Python, FastAPI, SQLite\tgithub.com/user/jobsearcher"
    html_text = resume_build.build_html(EXTENDED)
    for expected in ("Projects", "github.com/user/jobsearcher", "Additional",
                     "Currently learning"):
        assert expected in html_text


def test_project_bullets_are_checked():
    bad = dict(EXTENDED, projects=[{"name": "Thing", "tech": "Elm",
                                    "bullets": ["Helped build the tool."]}])
    problems = "\n".join(resume_build.check(bad))
    assert 'banned opener "helped"' in problems
    assert "period" in problems
    assert resume_build.check(EXTENDED) == []


def test_education_dates_render_right_tabbed(tmp_path):
    out = tmp_path / "resume.docx"
    resume_build.build_docx(SAMPLE, out)
    texts = [p.text for p in docx.Document(out).paragraphs]
    school = next(t for t in texts if t.startswith("State University"))
    assert school.endswith("\t2019 – 2023")
    assert "2019 – 2023" in resume_build.build_html(SAMPLE)


def test_education_without_dates_renders_school_only(tmp_path):
    data = dict(SAMPLE, education=[{"school": "State University, Springfield, NY",
                                    "lines": ["B.S., Computer Information Systems"]}])
    out = tmp_path / "r.docx"
    resume_build.build_docx(data, out)
    texts = [p.text for p in docx.Document(out).paragraphs]
    school_line = next(t for t in texts if t.startswith("State University"))
    assert "\t" not in school_line  # no right-aligned tail
    assert "State University, Springfield, NY" in resume_build.build_html(data)


def test_build_refuses_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr(resume_build, "CHROME", tmp_path / "nope")
    src = tmp_path / "resume.yaml"
    src.write_text(yaml.safe_dump(SAMPLE))
    resume_build.build(src, tmp_path / "out", basename="R_v1")
    import pytest
    with pytest.raises(SystemExit, match="never overwritten"):
        resume_build.build(src, tmp_path / "out", basename="R_v1")
    resume_build.build(src, tmp_path / "out", basename="R_v1", force=True)


def test_docx_reference_layout(tmp_path):
    out = tmp_path / "r.docx"
    resume_build.build_docx(SAMPLE, out)
    d = docx.Document(out)
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    # Centered caps name, centered contact.
    assert d.paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert d.paragraphs[0].runs[0].font.all_caps
    assert d.paragraphs[1].alignment == WD_ALIGN_PARAGRAPH.CENTER
    # Section headers carry caps + a bottom rule.
    header = next(p for p in d.paragraphs if p.text == "Experience")
    assert header.runs[0].font.all_caps
    assert header._p.find(qn("w:pPr")).find(qn("w:pBdr")) is not None
    # Role line: bold title run, regular pipe-separated tail.
    role = next(p for p in d.paragraphs if p.text.startswith("Software Engineer |"))
    assert role.runs[0].bold is True
    assert role.runs[1].bold is False
    assert " | Acme Corp | New York, NY" in role.runs[1].text
