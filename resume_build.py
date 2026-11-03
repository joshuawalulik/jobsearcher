"""Render a tailored resume (YAML) to DOCX and PDF, per the workflow's
Stage 5 document defaults (Job_Application_Workflow v4).

Run as: python -m resume_build resume.yaml outdir/ [--basename NAME] [--check] [--force]

Layout replicates the real submitted format: centered 17pt name and contact, section
headers displayed in caps over a bottom-rule border (12.5pt bold,
5pt/2pt spacing), role lines "Title | Company | City" with only the
title bold and dates on a right tab at the text edge, zero-spacing
bullets, regular-weight certifications.

Stage 5 rules implemented here (the ones that caused real bugs):
- Times New Roman; body 11.5pt default, adjustable via `body_pt` in the
  YAML within the 10.5-12pt band; name 17pt; headings 12.5pt bold.
- Explicit docDefaults: pPrDefault spacing before/after 0 with an
  explicit w:line AND w:lineRule, and rPrDefault font+size — so Word,
  Google Docs and LibreOffice don't substitute their own.
- Font and size set explicitly on every run.
- Right tab stop computed as page width minus margins (10800 DXA at
  Letter with 0.5" margins).
- Existing outputs are never overwritten without --force: bump the
  version number instead (per-company counter).
- --check runs the deterministic checklist items (em dashes, en dash
  date ranges, bullet length/periods, banned openers) and prints
  human-readable violations; build() always prints them. Skills with no
  carrier bullet print as NOTE lines only (bank v19: claimability, not
  bullet coverage, is the gate — verified by a human against the bank).

The PDF is printed from a matching HTML template via headless Chrome
(no Word/LibreOffice on this machine); without Chrome it is skipped
with a warning. The PDF is a proxy render: the workflow still requires
opening the DOCX before submitting.

YAML schema:
    name: Joshua R. Walulik        # rendered in caps
    contact: New York, NY  •  (555) 555-5555  •  email  •  linkedin
    body_pt: 11.5                  # optional, 10.5-12 band
    summary: optional, omit the key to omit the section
    experience:
      - title: Software Engineer
        company: Acme Corp
        where: New York, NY
        dates: Jul 2024 – Present
        bullets: [ ... ]
    projects:                      # optional (bank v20); rendered after Experience
      - name: JobSearcher
        tech: Python, FastAPI, SQLite
        link: github.com/user/repo # optional; right-aligned (wins over dates)
        bullets: [ ... ]           # same style rules as experience bullets
    skills:
      - {label: Frontend, items: "React, TypeScript"}
    education:
      - {school: School of Business, State University,
         where: "Tempe, AZ", dates: May 2023,   # dates optional, right-aligned
         lines: [ ... ]}
    certifications: optional, [{name: ...}]     # legacy; v20 builds use additional
    additional:                    # optional (bank v20); labeled lines, rendered last
      - {label: Interests, items: "Card collecting, gaming"}
"""

import argparse
import html
import re
import subprocess
from pathlib import Path

import yaml
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
FONT = "Times New Roman"
NAME_PT = 17
HEADING_PT = 12.5
SIDE_MARGIN_IN = 0.5
TOP_MARGIN_IN = 0.5
CONTENT_WIDTH_IN = 8.5 - 2 * SIDE_MARGIN_IN  # right tab stop (10800 DXA)


def _set_doc_defaults(doc, body_pt: float) -> None:
    """Explicit docDefaults so no renderer substitutes its own spacing
    (the one-page-in-LibreOffice, two-pages-in-Word bug)."""
    styles_el = doc.styles.element
    defaults = styles_el.find(qn("w:docDefaults"))
    if defaults is None:
        defaults = styles_el.makeelement(qn("w:docDefaults"), {})
        styles_el.insert(0, defaults)
    existing = defaults.find(qn("w:pPrDefault"))
    if existing is not None:
        defaults.remove(existing)
    ppr_default = defaults.makeelement(qn("w:pPrDefault"), {})
    ppr = ppr_default.makeelement(qn("w:pPr"), {})
    spacing = ppr.makeelement(qn("w:spacing"), {})
    spacing.set(qn("w:before"), "0")
    spacing.set(qn("w:after"), "0")
    spacing.set(qn("w:line"), "240")
    spacing.set(qn("w:lineRule"), "auto")
    ppr.append(spacing)
    ppr_default.append(ppr)
    defaults.insert(0, ppr_default)
    existing = defaults.find(qn("w:rPrDefault"))
    if existing is not None:
        defaults.remove(existing)
    rpr_default = defaults.makeelement(qn("w:rPrDefault"), {})
    rpr = rpr_default.makeelement(qn("w:rPr"), {})
    fonts = rpr.makeelement(qn("w:rFonts"), {})
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        fonts.set(qn(attr), FONT)
    rpr.append(fonts)
    sz = rpr.makeelement(qn("w:sz"), {})
    sz.set(qn("w:val"), str(int(body_pt * 2)))
    rpr.append(sz)
    rpr_default.append(rpr)
    defaults.insert(0, rpr_default)


def _run(para, text, *, size, bold=False, caps=False):
    r = para.add_run(text)
    r.font.name = FONT
    r.font.size = Pt(size)
    r.bold = bold
    if caps:
        r.font.all_caps = True
    return r


def _para(doc, *, before=0, after=0, center=False):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(before)
    p.paragraph_format.space_after = Pt(after)
    p.paragraph_format.line_spacing = 1.0  # writes w:line + w:lineRule
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return p


def _right_tab(p):
    p.paragraph_format.tab_stops.add_tab_stop(
        Inches(CONTENT_WIDTH_IN), WD_TAB_ALIGNMENT.RIGHT
    )


def _section_header(doc, text):
    """Caps heading over a thin bottom rule, like the reference docx."""
    p = _para(doc, before=5, after=2)
    _run(p, text, size=HEADING_PT, bold=True, caps=True)
    ppr = p._p.get_or_add_pPr()
    pbdr = ppr.makeelement(qn("w:pBdr"), {})
    bottom = pbdr.makeelement(qn("w:bottom"), {})
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:color"), "000000")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    pbdr.append(bottom)
    ppr.append(pbdr)


def _role_line(doc, title, rest, dates, *, size):
    """Bold title, regular ' | Company | City', right-tabbed dates."""
    p = _para(doc, after=1)
    _right_tab(p)
    _run(p, title, size=size, bold=True)
    if rest:
        _run(p, rest, size=size)
    if dates:
        _run(p, "\t" + dates, size=size)
    return p


def _bullet(doc, text, *, size):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.0
    _run(p, text, size=size)


def _labeled_lines(doc, groups, *, size):
    """'Label: items' lines, bold label — the Skills/Additional pattern."""
    for group in groups:
        p = _para(doc)
        _run(p, f"{group['label']}: ", size=size, bold=True)
        _run(p, group["items"], size=size)


def build_docx(data: dict, out_path: Path) -> None:
    body = float(data.get("body_pt", 11.5))
    doc = Document()
    _set_doc_defaults(doc, body)
    for s in doc.sections:
        s.left_margin = s.right_margin = Inches(SIDE_MARGIN_IN)
        s.top_margin = s.bottom_margin = Inches(TOP_MARGIN_IN)

    p = _para(doc, center=True)
    _run(p, data["name"], size=NAME_PT, bold=True, caps=True)
    p = _para(doc, before=1, center=True)
    _run(p, data["contact"], size=body)

    if data.get("summary"):
        _section_header(doc, "Summary")
        _run(_para(doc), data["summary"], size=body)

    _section_header(doc, "Experience")
    for job in data["experience"]:
        _role_line(
            doc, job["title"], f" | {job['company']} | {job['where']}",
            job["dates"], size=body,
        )
        for b in job["bullets"]:
            _bullet(doc, b, size=body)

    if data.get("projects"):
        _section_header(doc, "Projects")
        for proj in data["projects"]:
            _role_line(
                doc, proj["name"],
                f" | {proj['tech']}" if proj.get("tech") else "",
                proj.get("link") or proj.get("dates"), size=body,
            )
            for b in proj["bullets"]:
                _bullet(doc, b, size=body)

    _section_header(doc, "Skills")
    _labeled_lines(doc, data["skills"], size=body)

    _section_header(doc, "Education")
    for school in data["education"]:
        p = _para(doc)
        if school.get("dates"):
            _right_tab(p)
        _run(p, school["school"], size=body, bold=True)
        if school.get("where"):
            _run(p, f", {school['where']}", size=body, bold=True)
        if school.get("dates"):
            _run(p, "\t" + school["dates"], size=body)
        for line in school.get("lines", []):
            _run(_para(doc), line, size=body)

    if data.get("certifications"):
        _section_header(doc, "Certifications")
        for cert in data["certifications"]:
            p = _para(doc)
            _right_tab(p)
            _run(p, cert["name"], size=body)
            if cert.get("issued"):
                _run(p, "\t" + cert["issued"], size=body)

    if data.get("additional"):
        _section_header(doc, "Additional")
        _labeled_lines(doc, data["additional"], size=body)

    doc.save(out_path)


def build_html(data: dict) -> str:
    """The same layout as a print-ready HTML page for Chrome's PDF."""
    e = html.escape
    body = float(data.get("body_pt", 11.5))

    parts = [f'<h1>{e(data["name"])}</h1>',
             f'<p class="contact">{e(data["contact"])}</p>']
    if data.get("summary"):
        parts += ["<h2>Summary</h2>", f"<p>{e(data['summary'])}</p>"]
    parts.append("<h2>Experience</h2>")
    for job in data["experience"]:
        parts.append(
            f'<div class="row role"><span><b>{e(job["title"])}</b>'
            f' | {e(job["company"])} | {e(job["where"])}</span>'
            f"<span>{e(job['dates'])}</span></div>"
        )
        parts.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in job["bullets"]) + "</ul>")
    if data.get("projects"):
        parts.append("<h2>Projects</h2>")
        for proj in data["projects"]:
            tech = f" | {e(proj['tech'])}" if proj.get("tech") else ""
            right = e(proj.get("link") or proj.get("dates") or "")
            parts.append(
                f'<div class="row role"><span><b>{e(proj["name"])}</b>{tech}</span>'
                f"<span>{right}</span></div>"
            )
            parts.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in proj["bullets"]) + "</ul>")
    parts.append("<h2>Skills</h2>")
    for g in data["skills"]:
        parts.append(f'<p><b>{e(g["label"])}:</b> {e(g["items"])}</p>')
    parts.append("<h2>Education</h2>")
    for school in data["education"]:
        where = f", {e(school['where'])}" if school.get("where") else ""
        school_html = f"<b>{e(school['school'])}{where}</b>"
        if school.get("dates"):
            parts.append(f'<div class="row"><span>{school_html}</span>'
                         f"<span>{e(school['dates'])}</span></div>")
        else:
            parts.append(f"<p>{school_html}</p>")
        for line in school.get("lines", []):
            parts.append(f"<p>{e(line)}</p>")
    if data.get("certifications"):
        parts.append("<h2>Certifications</h2>")
        for cert in data["certifications"]:
            issued = e(cert.get("issued", ""))
            parts.append(f'<div class="row"><span>{e(cert["name"])}</span>'
                         f"<span>{issued}</span></div>")
    if data.get("additional"):
        parts.append("<h2>Additional</h2>")
        for g in data["additional"]:
            parts.append(f'<p><b>{e(g["label"])}:</b> {e(g["items"])}</p>')

    css = f"""
    @page {{ size: letter; margin: {TOP_MARGIN_IN}in {SIDE_MARGIN_IN}in; }}
    body {{ font-family: 'Times New Roman', Times, serif; font-size: {body}pt;
            line-height: 1.15; margin: 0; color: #000; }}
    h1 {{ font-size: {NAME_PT}pt; margin: 0; text-align: center;
          text-transform: uppercase; }}
    .contact {{ margin: 1pt 0 0; text-align: center; }}
    h2 {{ font-size: {HEADING_PT}pt; margin: 6pt 0 2pt;
          text-transform: uppercase; border-bottom: 1px solid #000;
          padding-bottom: 1px; }}
    p {{ margin: 0; }}
    .row {{ display: flex; justify-content: space-between; }}
    .role {{ margin: 0 0 1pt; }}
    ul {{ margin: 0; padding-left: 16pt; }}
    li {{ margin: 0; }}
    """
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{css}</style></head><body>{''.join(parts)}</body></html>")


def build_pdf(data: dict, out_path: Path) -> bool:
    """HTML → PDF via headless Chrome. False (with a note) if unavailable."""
    if not CHROME.exists():
        print(f"  ! Chrome not found at {CHROME} — skipped {out_path.name}")
        return False
    html_path = out_path.with_suffix(".pdf.html")
    html_path.write_text(build_html(data))
    try:
        subprocess.run(
            [str(CHROME), "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={out_path}", str(html_path)],
            check=True, capture_output=True, timeout=60,
        )
    finally:
        html_path.unlink(missing_ok=True)
    return out_path.exists()


BANNED_OPENERS = (
    "contributed to", "gained experience with", "took on", "helped", "aided",
    "assisted", "worked on", "exposed to", "participated in", "handles",
    "works across", "utilized", "leveraged",
)


def check(data: dict) -> list[str]:
    """Deterministic Stage 5 checklist items. Returns human-readable
    violations (empty = those checks pass)."""
    problems = []
    all_text = yaml.safe_dump(data, allow_unicode=True)
    if "—" in all_text:
        problems.append("em dash found — the workflow bans em dashes everywhere")
    body = float(data.get("body_pt", 11.5))
    if not 10.5 <= body <= 12:
        problems.append(f"body_pt {body} outside the 10.5-12pt band")
    bullets = [b for job in data["experience"] for b in job["bullets"]]
    bullets += [b for proj in data.get("projects", []) for b in proj["bullets"]]
    for b in bullets:
        if len(b.split()) > 32:
            problems.append(f"bullet over ~30 words: {b[:60]}...")
        if b.rstrip().endswith("."):
            problems.append(f"bullet ends in a period: ...{b[-40:]}")
        lowered = b.lower()
        for opener in BANNED_OPENERS:
            if lowered.startswith(opener):
                problems.append(f'banned opener "{opener}": {b[:50]}...')
    for job in data["experience"]:
        dates = job["dates"]
        if "–" not in dates:
            problems.append(f'date range missing en dash: "{dates}"')
        if "current" in dates.lower():
            problems.append(f'"Present", not "Current": "{dates}"')
    if data.get("summary") and len(re.split(r"(?<=[.!?]) ", data["summary"])) > 2:
        problems.append("summary over two sentences")
    return problems


def advisories(data: dict) -> list[str]:
    """Non-failing notices. Skills without a carrier bullet are allowed
    (bank v19: claimability is the gate, not bullet coverage) but worth a
    look — each listed skill should still trace to the bank's Claimable
    list, which only a human can verify."""
    notes = []
    joined_bullets = " ".join(
        [b for job in data["experience"] for b in job["bullets"]]
        + [b for proj in data.get("projects", []) for b in proj["bullets"]]
    ).lower()
    for group in data["skills"]:
        for skill in (s.strip() for s in group["items"].split(",")):
            if skill and skill.lower() not in joined_bullets:
                notes.append(
                    f'skill "{skill}" has no carrier bullet — fine if bank-claimable'
                )
    return notes


def build(yaml_path: Path, out_dir: Path, basename: str | None = None,
          *, force: bool = False) -> list[Path]:
    data = yaml.safe_load(yaml_path.read_text())
    for problem in check(data):
        print(f"  CHECK: {problem}")
    for note in advisories(data):
        print(f"  NOTE: {note}")
    out_dir.mkdir(parents=True, exist_ok=True)
    basename = basename or yaml_path.stem
    docx_path = out_dir / f"{basename}.docx"
    pdf_path = out_dir / f"{basename}.pdf"
    clashes = [p for p in (docx_path, pdf_path) if p.exists()]
    if clashes and not force:
        raise SystemExit(
            f"  REFUSED: {', '.join(p.name for p in clashes)} already exist — "
            "versions are never overwritten. Bump the _v<N> counter, or pass "
            "--force to deliberately replace."
        )
    written = []
    build_docx(data, docx_path)
    written.append(docx_path)
    if build_pdf(data, pdf_path):
        written.append(pdf_path)
    return written


def mark_ready(yaml_path: Path, conn=None) -> None:
    """Move the application behind resume_<app_id>.yaml to the ready lane.

    Only a considering card moves — a --force re-render of an already
    applied (or further) application never demotes it. Yaml names without
    an app id (ad-hoc renders) are left alone.
    """
    m = re.match(r"resume_(\d+)$", yaml_path.stem)
    if not m:
        return
    import db  # late: keeps the renderer importable without the repo DB
    app_id = int(m.group(1))
    own_conn = conn is None
    if own_conn:
        conn = db.get_connection()
        db.init_db(conn)
    try:
        row = conn.execute(
            "SELECT lane FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        if row and row["lane"] == "considering":
            db.set_application_lane(conn, app_id, "ready")
            print(f"  app {app_id}: considering -> ready")
    finally:
        if own_conn:
            conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("yaml_path", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--basename", help="output filename stem (default: yaml stem)")
    ap.add_argument("--check", action="store_true",
                    help="run checklist only, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="allow overwriting an existing version")
    args = ap.parse_args()
    if args.check:
        data = yaml.safe_load(args.yaml_path.read_text())
        problems = check(data)
        for problem in problems:
            print(f"  CHECK: {problem}")
        for note in advisories(data):
            print(f"  NOTE: {note}")
        raise SystemExit(1 if problems else 0)
    for path in build(args.yaml_path, args.out_dir, args.basename, force=args.force):
        print(f"  wrote {path}")
    mark_ready(args.yaml_path)


if __name__ == "__main__":
    main()
