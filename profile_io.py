"""Read-modify-write for config/profile.yaml that preserves comments.

The profile's comments are load-bearing documentation, so the editor
never round-trips through yaml.safe_load/yaml.dump (which drops them).
ruamel.yaml in round-trip mode keeps comments attached to their nodes;
list edits are applied minimally — delete removed items, append new
ones — so comments on untouched items survive. A comment attached to a
deleted item is removed with it.

Only the fields whitelisted in FIELDS are editable: the scraper:
section is out of the editor's reach. Reading for scoring stays on
scrapers.sweep.load_profile; this module is the editor's path.
"""

from pathlib import Path

from ruamel.yaml import YAML

PROFILE_PATH = Path(__file__).parent / "config" / "profile.yaml"

WORK_TYPES = ("remote", "hybrid", "onsite")
SENIORITY_LEVELS = ("intern", "junior", "mid", "senior", "staff", "principal")
ATS_TYPES = ("greenhouse", "lever", "ashby")

# Editable fields: dotted path -> kind ('list' | 'int' | 'float' | 'bool'
# | 'choice' | 'companies'). Form fields use these paths as input names.
FIELDS = {
    "companies": "companies",
    "gates.excluded_title_keywords": "list",
    "gates.work_type.mine": "choice",
    "gates.work_type.reject_conflicts": "bool",
    "gates.allowed_locations": "list",
    "gates.clearance_keywords": "list",
    "gates.max_posting_age_days": "int",
    "gates.max_required_experience_years": "int",
    "gates.delisted_after_days": "int",
    "scoring.target_titles": "list",
    "scoring.title_match_weight": "int",
    "scoring.exact_title_bonus": "int",
    "scoring.required_keywords": "list",
    "scoring.required_keyword_weight": "int",
    "scoring.preferred_keywords": "list",
    "scoring.preferred_keyword_weight": "int",
    "scoring.negative_keywords": "list",
    "scoring.negative_keyword_weight": "int",
    "scoring.must_haves": "list",
    "scoring.missing_must_have_penalty": "int",
    "scoring.seniority.target": "choice",
    "scoring.seniority.years_to_level.junior_max": "int",
    "scoring.seniority.years_to_level.senior_min": "int",
    "scoring.experience.mine_years": "int",
    "scoring.experience.over_penalty_per_year": "int",
    "scoring.sparse_description.min_chars": "int",
    "scoring.sparse_description.multiplier": "float",
} | {f"scoring.seniority.multipliers.{level}": "float" for level in SENIORITY_LEVELS}

CHOICES = {
    "gates.work_type.mine": WORK_TYPES,
    "scoring.seniority.target": SENIORITY_LEVELS,
}

_yaml = YAML()  # round-trip mode by default
_yaml.preserve_quotes = True
_yaml.width = 4096  # never rewrap lines the user laid out
_yaml.indent(mapping=2, sequence=4, offset=2)  # match profile.yaml's layout


def load_round_trip(path: Path = PROFILE_PATH):
    with open(path) as f:
        return _yaml.load(f)


def save_round_trip(doc, path: Path = PROFILE_PATH) -> None:
    with open(path, "w") as f:
        _yaml.dump(doc, f)


def get_value(doc, path: str):
    node = doc
    for key in path.split("."):
        node = node[key]
    return node


def parse_form(form: dict[str, str]) -> dict[str, object]:
    """Raw form strings -> typed edits for every field in FIELDS.

    Iterates FIELDS (not the form) so an unchecked checkbox, absent from
    the POST body, reads as False; unknown form keys are ignored.
    """
    edits: dict[str, object] = {}
    for path, kind in FIELDS.items():
        raw = form.get(path)
        if kind == "bool":
            edits[path] = str(raw).lower() in ("on", "true", "1")
            continue
        if raw is None:
            raise ValueError(f"missing field {path!r}")
        raw = raw.strip()
        if kind == "companies":
            edits[path] = _parse_companies(raw)
        elif kind == "list":
            edits[path] = [line.strip() for line in raw.splitlines() if line.strip()]
        elif kind == "choice":
            if raw not in CHOICES[path]:
                raise ValueError(f"{path}: {raw!r} not one of {CHOICES[path]}")
            edits[path] = raw
        else:
            try:
                edits[path] = int(raw) if kind == "int" else float(raw)
            except ValueError:
                raise ValueError(f"{path}: {raw!r} is not a number") from None
    return edits


def _parse_companies(raw: str) -> list[dict]:
    """Textarea lines 'Name | ats | slug [| weight]' -> watchlist entries.

    weight is an optional score multiplier (e.g. 0.8 to down-weight an
    early-stage company). Duplicate (ats, slug) lines keep the first
    occurrence.
    """
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (3, 4) or not all(parts):
            raise ValueError(f"companies: {line!r} is not 'Name | ats | slug [| weight]'")
        name, ats, slug = parts[:3]
        if ats not in ATS_TYPES:
            raise ValueError(f"companies: ats {ats!r} not one of {ATS_TYPES}")
        if (ats, slug) in seen:
            continue
        seen.add((ats, slug))
        entry = {"name": name, "ats": ats, "slug": slug}
        if len(parts) == 4:
            try:
                weight = float(parts[3])
            except ValueError:
                raise ValueError(f"companies: weight {parts[3]!r} is not a number") from None
            if not 0 < weight <= 2:
                raise ValueError(f"companies: weight {weight:g} must be in (0, 2]")
            if weight != 1:  # 1.0 is the default; don't store noise
                entry["weight"] = weight
        entries.append(entry)
    return entries


def apply_edits(doc, edits: dict[str, object]) -> None:
    """Apply parsed edits onto the ruamel doc in place."""
    for path, value in edits.items():
        if path not in FIELDS:
            raise ValueError(f"field {path!r} is not editable")
        *parents, leaf = path.split(".")
        node = doc
        for key in parents:
            node = node[key]
        if FIELDS[path] == "companies":
            _sync_companies(node[leaf], value)
        elif FIELDS[path] == "list":
            _sync_list(node[leaf], value)
        else:
            node[leaf] = value


def _sync_companies(seq, new_entries: list[dict]) -> None:
    """Sync the watchlist by (ats, slug) with minimal edits.

    Same policy as _sync_list: survivors keep their position (and
    attached comments), removed entries are deleted (a comment attached
    to one goes with it), new entries append. A changed name updates the
    existing entry in place. DB-side effects happen at the next sweep:
    sync_companies upserts additions and deactivates removals.
    """
    wanted = {(e["ats"], e["slug"]): e for e in new_entries}
    for i in range(len(seq) - 1, -1, -1):
        if (seq[i]["ats"], seq[i]["slug"]) not in wanted:
            del seq[i]
    existing = {(e["ats"], e["slug"]): e for e in seq}
    for key, entry in wanted.items():
        if key in existing:
            current = existing[key]
            if current["name"] != entry["name"]:
                current["name"] = entry["name"]
            if "weight" in entry:
                if current.get("weight") != entry["weight"]:
                    current["weight"] = entry["weight"]
            elif "weight" in current:
                del current["weight"]
        else:
            new = {"name": entry["name"], "ats": entry["ats"], "slug": entry["slug"]}
            if "weight" in entry:
                new["weight"] = entry["weight"]
            seq.append(new)


def _sync_list(seq, new_values: list[str]) -> None:
    """Mutate seq to match new_values' membership with minimal edits.

    Surviving items keep their position (and attached comments); new
    items are appended. Reordering existing items is not applied —
    these are membership lists, order has no meaning to the scorer.
    """
    for i in range(len(seq) - 1, -1, -1):
        if seq[i] not in new_values:
            del seq[i]
    existing = set(seq)
    for value in new_values:
        if value not in existing:
            seq.append(value)
