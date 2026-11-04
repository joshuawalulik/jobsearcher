---
name: apply
description: Run the application workflow - tailor a resume per job (DOCX+PDF into the per-company folder), give the apply links, then move jobs to applied once the user says they submitted. Use when the user asks to run the (application) workflow or to apply to matches.
---

# Application workflow

Flow: pick job(s) → run the workflow per job → hand the user
the apply links → when the user says they've applied, mark applied.

## 0. Load the project brain

`workflow.home` in config/profile.yaml points at the living job-search
folder (your own folder, outside this repo). Read both project docs
from `<home>/claude/`:
- `Application_Workflow.md` — THE process. Its stages 1-7 govern
  everything below; where this file and that one disagree, that one wins.
- `Resume_Master_Bank.md` — the only source of resume content and style
  rules. Never invent, upgrade, or imply experience that is not in the
  bank. Name the bank version (from its header) in every run's note.
- `Interview_Prep_Guide.md` — source for Stage 6's gap answers and STAR
  story references.

Project docs keep stable filenames and are updated in place — never
create a second copy or a versioned sibling. (`Applications/project
docs/` holds historical versioned exports; do not read or write those.)

## 1. Pick the job(s)

**One job at a time is the default — batch only when the user
explicitly asks** ("top 5", "do all of them", or named jobs).

- User pasted a JD → use that (track it via the feed/manual entry if
  it isn't in the DB yet).
- User named jobs or a count → stage exactly those.
- Otherwise → `.venv/bin/python -m apply_run list 5`, show the short
  numbered list (company, title, score) and wait for the pick.

```bash
.venv/bin/python -m apply_run start <job_ids>
```

`start` tracks each job (considering lane; already-tracked jobs are
reused, so considering-lane jobs can be staged too) and writes
`<home>/Applications/<company>/<role>/job_<app_id>.md` (apply link,
score breakdown, full JD) — existing company folders are reused even
under variant names (e.g. "globex" for Globex Corp), and each role gets its own
subfolder. `start` also prints the `basename` to render with: it
carries the next free version from a scan of the WHOLE company tree,
so historical versions keep the per-company counter honest.

## 2. Per job: run the workflow stages

Each `job_<app_id>.md` is the "pasted JD". Follow Application_Workflow.md's
stages in order: parse (1), honest hiring screen (2 — Skip is a valid
verdict; the score breakdown is evidence, not a verdict), Stage 3
questions via AskUserQuestion — do not write the resume until they are
answered; never skip the location-line question for out-of-metro hybrid
roles. Then build (4) per the bank's standing rules into
`resume_<app_id>.yaml` in the company folder (schema in resume_build.py).

**Stage 4½ — adversarial review (after the YAML draft, before any
render).** Drafting-time only; reviewers never touch the scoring/ranking
path. Fan out three parallel subagents (Agent tool, all three in ONE
message, each with `model: sonnet` — reviewer work is checklist
execution against explicit sources, and Sonnet keeps it off the
top-tier model's usage limits; read-only — they report, never edit).
Each prompt is a block in
[reviewers.md](reviewers.md) (sibling file in this skill folder) with
the three input paths filled in: the job's `job_<app_id>.md`, the draft
`resume_<app_id>.yaml`, and `<home>/claude/Resume_Master_Bank.md`.

1. **Truth auditor** — every claim must trace to the bank; untraceable
   or inflated claims become questions for the user, never silent
   rewrites.
2. **Hiring-manager simulator** — cold 60-second screen against the JD;
   the stored score breakdown is evidence, not a verdict (same framing
   as Stage 2).
3. **ATS / language reviewer** — JD keyword gaps and templated/
   AI-sounding phrasing only; it must NOT re-check what resume_build's
   deterministic checklist already covers.

Merge the three reports into one deduplicated punch list (keep each
item's reviewer tag, severity, and evidence; drop anything check()
covers). Put it to the user with AskUserQuestion — multiSelect, MAJOR
items first, batched if more than 4; truth-auditor QUESTIONs are asked
as questions ("can you defend X?"), and the answer decides keep vs cut.
Apply ONLY the accepted edits to the YAML — every applied edit must
still trace to the bank — then render.

**Skills-line guard.** Skills need only be bank-claimable,
not bullet-carried (resume_build prints no-carrier skills as NOTE
lines, not violations). But when an accepted edit cuts the only bullet
carrying a listed skill, the keep-or-drop call is the user's: put it in
the punch-list questions ("cutting X also drops Y from skills — keep Y
anyway (claimable), or drop it?"), never resolve it silently.

```bash
.venv/bin/python -m resume_build "<folder>/resume_<app_id>.yaml" "<folder>" --basename "<basename from apply_run start>"
```

resume_build refuses to overwrite an existing version (versions are
append-only; `--force` exists but needs an explicit user ask). A rerun
of the same job gets a fresh basename from `apply_run start`.

**Stage 5 verification here:** resume_build sets the explicit document
defaults and prints deterministic checklist violations — fix any before
delivering. Read the generated PDF to confirm one page and 85-94% fill.
The DOCX cannot be rendered on this machine (no Word/LibreOffice), so
report that check as NOT run and tell the user to open the DOCX once
before submitting. Report every check honestly (run / not run).

**Every run — including a Skip at Stage 2 — saves a short record** to
the tracker card: verdict, bank version, resume version used, key
tailoring decisions, the Stage 4½ review outcome (findings accepted /
rejected, and any claim the user chose to keep over a truth-auditor
flag), plus the Stage 6 handoff notes:
```bash
.venv/bin/python -c "import db; c=db.get_connection(); db.append_application_note(c, <app_id>, '''<note>'''); c.close()"
```

On a Skip verdict, after saving the note, move the card to the skipped
lane: `.venv/bin/python -m apply_run skip <app_id>`.

## 3. Hand off

End the turn with a compact per-job block: company — title, verdict,
resume files (clickable paths), Stage 6 notes, and the **apply link**.
Remind per Stage 7: upload the PDF, renamed to
`Joshua_Walulik_Resume.pdf` (keep the versioned files for the record).
An unversioned `Joshua_Walulik_Resume.pdf` kept in a role folder is the
user's marker that the application was actually submitted — never
rename, version, or delete one during folder cleanups.
The user applies manually. Do not mark anything applied yet — a
successful render already moved the card to the **ready** lane (resume
built, submission pending; resume_build does it automatically, or
`.venv/bin/python -m apply_run ready <app_id>` by hand).

## 4. After the user says they applied

```bash
.venv/bin/python -m apply_run applied <app_ids>
```

Only the ones they actually submitted ("all" = every Go in the batch;
Skips are already in the skipped lane). First move into applied stamps
applied_at automatically.
