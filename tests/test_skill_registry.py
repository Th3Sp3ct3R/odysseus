"""Tests for the read-only Hermes/Claude skills metadata registry (S1/S2).

Proves the safety invariants the design depends on:

  * SKILL.md frontmatter parsing (Claude- and Hermes-shaped files).
  * Deterministic summary fallback when `description` is absent (no LLM).
  * Conservative risk filtering: only safe/none (or allowlisted) are exposed.
  * Unknown / offensive / missing-risk skills are disabled by default.
  * No full skill body is ever stored in the registry.
  * The scanner does not modify skill files (bytes + mtimes unchanged).
  * The registry cache path (`data/skill_registry.json`) is gitignored.

All filesystem fixtures are synthetic (tmp_path); the live ~/.claude and
~/.hermes libraries are never touched.
"""

import os
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.skill_registry import registry as reg


# ---------------------------------------------------------------------------
# Fixtures — synthetic skill libraries
# ---------------------------------------------------------------------------

CLAUDE_SAFE = """\
---
name: steve-jobs
description: A persona skill that simulates Steve Jobs for product critique.
risk: safe
source: community
date_added: "2026-03-06"
tags: [persona, design]
---

## When to Use
When you want a product-design critique.
"""

CLAUDE_UNKNOWN = """\
---
name: typescript-scaffold
description: "Scaffold a production TypeScript project."
risk: unknown
source: community
date_added: "2026-02-27"
---

## Procedure
1. Run the generator.
"""

CLAUDE_OFFENSIVE = """\
---
name: red-team-tool
description: "Offensive tooling."
risk: offensive
source: community
---

## Procedure
Do offensive things.
"""

CLAUDE_NONE_RISK = """\
---
name: readme-writer
description: Writes a README.
risk: none
---

Body.
"""

# Hermes-shaped: NO risk field, NO description either -> exercises both the
# missing-risk default (disabled) and the deterministic summary fallback.
HERMES_NO_RISK_NO_DESC = """\
---
name: bland
category: voice
tags: [bland, twilio, voice]
---

# Bland Voice Agent

Bland.ai and Twilio voice agents for phone call automation. Use this when you
need outbound calling.

## Procedure
- step one
"""

# A skill whose body leads with bullets/code before the first paragraph,
# to confirm the fallback skips non-paragraph lines deterministically.
HERMES_FENCED_BODY = """\
---
name: jq-helper
---

## Usage

```bash
jq '.foo' file.json
```

Process JSON on the command line with jq filters.
"""


def _write_skill(base: pathlib.Path, name: str, content: str) -> pathlib.Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(content, encoding="utf-8")
    return f


@pytest.fixture
def libraries(tmp_path):
    """Build synthetic ~/.claude/skills and ~/.hermes/skills trees."""
    claude = tmp_path / "claude" / "skills"
    hermes = tmp_path / "hermes" / "skills"
    _write_skill(claude, "steve-jobs", CLAUDE_SAFE)
    _write_skill(claude, "typescript-scaffold", CLAUDE_UNKNOWN)
    _write_skill(claude, "red-team-tool", CLAUDE_OFFENSIVE)
    _write_skill(claude, "readme-writer", CLAUDE_NONE_RISK)
    _write_skill(hermes, "bland", HERMES_NO_RISK_NO_DESC)
    _write_skill(hermes, "jq-helper", HERMES_FENCED_BODY)
    return {"claude": str(claude), "hermes": str(hermes)}


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def test_frontmatter_parsing_claude_shape():
    fm, body = reg.parse_frontmatter(CLAUDE_SAFE)
    assert fm["name"] == "steve-jobs"
    assert fm["risk"] == "safe"
    assert fm["source"] == "community"
    assert fm["date_added"] == "2026-03-06"
    assert fm["tags"] == ["persona", "design"]
    assert "When to Use" in body
    assert body.lstrip().startswith("##")  # body excludes frontmatter


def test_frontmatter_parsing_hermes_shape():
    fm, body = reg.parse_frontmatter(HERMES_NO_RISK_NO_DESC)
    assert fm["name"] == "bland"
    assert fm["category"] == "voice"
    assert "risk" not in fm
    assert "description" not in fm
    assert body.startswith("# Bland Voice Agent")


def test_frontmatter_missing_is_empty():
    fm, body = reg.parse_frontmatter("no frontmatter here\njust text")
    assert fm == {}
    assert body == "no frontmatter here\njust text"


# ---------------------------------------------------------------------------
# Deterministic summary fallback (no LLM)
# ---------------------------------------------------------------------------

def test_summary_prefers_frontmatter_description():
    fm, body = reg.parse_frontmatter(CLAUDE_SAFE)
    assert reg.derive_summary(fm, body) == (
        "A persona skill that simulates Steve Jobs for product critique."
    )


def test_summary_fallback_uses_heading_and_paragraph():
    fm, body = reg.parse_frontmatter(HERMES_NO_RISK_NO_DESC)
    summary = reg.derive_summary(fm, body)
    assert summary  # non-empty
    assert "Bland Voice Agent" in summary
    assert "Bland.ai and Twilio" in summary
    assert "## Procedure" not in summary  # later sections are not pulled in


def test_summary_fallback_skips_code_fences():
    fm, body = reg.parse_frontmatter(HERMES_FENCED_BODY)
    summary = reg.derive_summary(fm, body)
    assert "jq '.foo'" not in summary           # fenced code is skipped
    assert "Process JSON on the command line" in summary


def test_summary_is_deterministic():
    fm, body = reg.parse_frontmatter(HERMES_NO_RISK_NO_DESC)
    a = reg.derive_summary(fm, body)
    b = reg.derive_summary(fm, body)
    assert a == b


def test_summary_is_bounded():
    long_para = "word " * 500
    fm, body = {}, "# Title\n\n" + long_para
    summary = reg.derive_summary(fm, body)
    assert len(summary) <= reg.MAX_SUMMARY_CHARS


# ---------------------------------------------------------------------------
# Risk + exposure policy
# ---------------------------------------------------------------------------

def test_missing_risk_defaults_to_unknown():
    assert reg.normalize_risk(None) == "unknown"
    assert reg.normalize_risk("") == "unknown"
    assert reg.normalize_risk("  SAFE ") == "safe"


@pytest.mark.parametrize("risk,expected", [
    ("safe", True),
    ("none", True),
    ("unknown", False),
    ("offensive", False),
    ("critical", False),
    ("low", False),
])
def test_exposure_policy(risk, expected):
    assert reg.is_exposed(risk, "x", allowlist=()) is expected


def test_unknown_and_offensive_disabled_by_default(libraries):
    registry = reg.build_registry(libraries)
    by_name = {s["name"]: s for s in registry["skills"]}
    assert by_name["typescript-scaffold"]["risk"] == "unknown"
    assert by_name["typescript-scaffold"]["exposed"] is False
    assert by_name["red-team-tool"]["risk"] == "offensive"
    assert by_name["red-team-tool"]["exposed"] is False
    # Hermes skills carry no risk field -> unknown -> disabled
    assert by_name["bland"]["risk"] == "unknown"
    assert by_name["bland"]["exposed"] is False


def test_safe_and_none_are_exposed(libraries):
    registry = reg.build_registry(libraries)
    by_name = {s["name"]: s for s in registry["skills"]}
    assert by_name["steve-jobs"]["exposed"] is True
    assert by_name["readme-writer"]["exposed"] is True


def test_allowlist_is_explicit_override(libraries):
    # Without allowlist: unknown stays disabled.
    base = reg.build_registry(libraries)
    assert {s["name"]: s for s in base["skills"]}["typescript-scaffold"]["exposed"] is False
    # With explicit allowlist: that one skill becomes exposed; others unchanged.
    allowed = reg.build_registry(libraries, allowlist=["typescript-scaffold"])
    by_name = {s["name"]: s for s in allowed["skills"]}
    assert by_name["typescript-scaffold"]["exposed"] is True
    assert by_name["red-team-tool"]["exposed"] is False  # not allowlisted


# ---------------------------------------------------------------------------
# Allowlist support (S1 policy)
# ---------------------------------------------------------------------------

def test_unknown_skill_disabled_by_default(libraries):
    registry = reg.build_registry(libraries)  # no allowlist
    by_name = {s["name"]: s for s in registry["skills"]}
    assert by_name["typescript-scaffold"]["risk"] == "unknown"
    assert by_name["typescript-scaffold"]["exposed"] is False
    assert by_name["bland"]["exposed"] is False  # hermes, missing risk -> unknown


def test_unknown_skill_exposed_when_allowlisted_by_name(libraries):
    registry = reg.build_registry(libraries, allowlist=["typescript-scaffold"])
    by_name = {s["name"]: s for s in registry["skills"]}
    assert by_name["typescript-scaffold"]["exposed"] is True


def test_unknown_skill_exposed_when_allowlisted_by_path(libraries):
    target = os.path.join(libraries["claude"], "typescript-scaffold", "SKILL.md")
    registry = reg.build_registry(libraries, allowlist=[{"path": target}])
    by_name = {s["name"]: s for s in registry["skills"]}
    assert by_name["typescript-scaffold"]["exposed"] is True
    # A different unknown skill not on the allowlist stays disabled.
    assert by_name["bland"]["exposed"] is False


def test_allowlist_accepts_skill_id_dicts(libraries):
    registry = reg.build_registry(libraries, allowlist=[{"skill_id": "bland"}])
    by_name = {s["name"]: s for s in registry["skills"]}
    assert by_name["bland"]["exposed"] is True


def test_offensive_remains_disabled_in_s1_even_if_allowlisted(libraries):
    # Allowlisting MUST NOT expose an S1 dangerous-tier skill.
    for entry in ("red-team-tool",
                  {"skill_id": "red-team-tool"},
                  {"path": os.path.join(libraries["claude"], "red-team-tool", "SKILL.md")}):
        registry = reg.build_registry(libraries, allowlist=[entry])
        by_name = {s["name"]: s for s in registry["skills"]}
        assert by_name["red-team-tool"]["risk"] == "offensive"
        assert by_name["red-team-tool"]["exposed"] is False, f"exposed via {entry!r}"
    # Direct policy check too.
    assert reg.is_exposed("offensive", "red-team-tool", ["red-team-tool"]) is False
    assert reg.is_exposed("critical", "x", ["x"]) is False  # critical also hard-blocked


def test_allowlist_file_is_optional_missing_returns_empty(tmp_path):
    missing = tmp_path / "nope" / "skill_registry_allowlist.json"
    assert reg.load_allowlist(str(missing)) == []


def test_allowlist_file_not_required_for_default_scan(libraries, tmp_path, monkeypatch):
    # Point the default allowlist path at a non-existent file; a default build
    # must still succeed and expose only safe/none skills.
    monkeypatch.setattr(reg, "default_allowlist_path",
                        lambda: str(tmp_path / "absent_allowlist.json"))
    assert reg.load_allowlist() == []
    registry = reg.build_registry(libraries)
    by_name = {s["name"]: s for s in registry["skills"]}
    assert by_name["steve-jobs"]["exposed"] is True       # safe
    assert by_name["typescript-scaffold"]["exposed"] is False  # unknown, no allowlist


def test_allowlist_file_loads_array_and_object_forms(tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text(json.dumps(["a", {"path": "/x/SKILL.md"}]), encoding="utf-8")
    obj = tmp_path / "obj.json"
    obj.write_text(json.dumps({"allowlist": ["b"]}), encoding="utf-8")
    assert reg.load_allowlist(str(arr)) == ["a", {"path": "/x/SKILL.md"}]
    assert reg.load_allowlist(str(obj)) == ["b"]


def test_allowlisted_registry_still_metadata_only(libraries):
    registry = reg.build_registry(libraries, allowlist=["bland", "typescript-scaffold"])
    blob = json.dumps(registry)
    assert "step one" not in blob and "jq '.foo' file.json" not in blob
    for s in registry["skills"]:
        assert "body" not in s and "content" not in s
        assert len(s["description"]) <= reg.MAX_SUMMARY_CHARS


def test_default_allowlist_path_is_under_data():
    p = reg.default_allowlist_path()
    assert p.replace(os.sep, "/").endswith("data/skill_registry_allowlist.json")


def test_allowlist_file_path_is_gitignored():
    p = reg.default_allowlist_path()
    result = subprocess.run(
        ["git", "check-ignore", p], cwd=str(ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, "data/skill_registry_allowlist.json is NOT gitignored"


# ---------------------------------------------------------------------------
# No full body stored
# ---------------------------------------------------------------------------

def test_no_full_body_stored(libraries):
    registry = reg.build_registry(libraries)
    # The unique body markers must never appear anywhere in the serialized cache.
    blob = json.dumps(registry)
    assert "jq '.foo' file.json" not in blob          # fenced code body
    assert "step one" not in blob                      # body bullet
    assert "Do offensive things." not in blob          # offensive body text
    # SkillMeta has no 'body'/'content' field at all.
    for s in registry["skills"]:
        assert "body" not in s
        assert "content" not in s
        assert len(s["description"]) <= reg.MAX_SUMMARY_CHARS


def test_skillmeta_fields_are_metadata_only():
    fields = set(reg.SkillMeta.__dataclass_fields__.keys())
    assert "body" not in fields and "content" not in fields
    assert {"name", "description", "risk", "source", "date_added",
            "root", "path", "category", "modified", "content_hash",
            "exposed"} <= fields


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------

def test_scanner_does_not_modify_skill_files(libraries):
    # Snapshot bytes + mtimes of every SKILL.md before scanning.
    def snapshot():
        snap = {}
        for label, rootdir in libraries.items():
            for dp, _dn, fn in os.walk(rootdir):
                if "SKILL.md" in fn:
                    p = os.path.join(dp, "SKILL.md")
                    snap[p] = (os.path.getmtime(p), pathlib.Path(p).read_bytes())
        return snap

    before = snapshot()
    reg.build_registry(libraries)
    reg.build_registry(libraries, allowlist=["steve-jobs"])
    after = snapshot()

    assert set(before) == set(after)
    for p in before:
        assert before[p][0] == after[p][0], f"mtime changed: {p}"
        assert before[p][1] == after[p][1], f"bytes changed: {p}"


def test_missing_root_is_tolerated(tmp_path):
    roots = {"claude": str(tmp_path / "nope"), "hermes": str(tmp_path / "also-nope")}
    registry = reg.build_registry(roots)
    assert registry["skills"] == []
    assert registry["counts"]["total"] == 0


# ---------------------------------------------------------------------------
# Search / list surface
# ---------------------------------------------------------------------------

def test_list_by_source(libraries):
    registry = reg.build_registry(libraries)
    claude = reg.list_skills(registry, source="claude")
    hermes = reg.list_skills(registry, source="hermes")
    assert {s["name"] for s in claude} == {
        "steve-jobs", "typescript-scaffold", "red-team-tool", "readme-writer"}
    assert {s["name"] for s in hermes} == {"bland", "jq-helper"}


def test_search_by_query_and_filters(libraries):
    registry = reg.build_registry(libraries)
    # text query against name/description/category
    hits = reg.search_skills(registry, query="jobs")
    assert [s["name"] for s in hits] == ["steve-jobs"]
    # risk filter
    safe = reg.search_skills(registry, risk="safe")
    assert {s["name"] for s in safe} == {"steve-jobs"}
    # exposed filter returns only safe/none here
    exposed = reg.search_skills(registry, exposed=True)
    assert {s["name"] for s in exposed} == {"steve-jobs", "readme-writer"}
    # disabled filter
    disabled = reg.search_skills(registry, exposed=False)
    assert "red-team-tool" in {s["name"] for s in disabled}
    assert "bland" in {s["name"] for s in disabled}


def test_search_returns_metadata_only(libraries):
    registry = reg.build_registry(libraries)
    for s in reg.search_skills(registry, query="voice"):
        assert "body" not in s and "content" not in s
        assert set(s).issubset(set(reg.SkillMeta.__dataclass_fields__.keys()))


# ---------------------------------------------------------------------------
# Cache persistence + gitignore
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(libraries, tmp_path):
    registry = reg.build_registry(libraries)
    cache = tmp_path / "data" / "skill_registry.json"
    reg.save_registry(registry, str(cache))
    assert cache.exists()
    loaded = reg.load_registry(str(cache))
    assert loaded["counts"] == registry["counts"]
    assert "generated_at" in loaded  # stamped at save time
    assert len(loaded["skills"]) == len(registry["skills"])


def test_default_cache_path_is_under_data():
    p = reg.default_cache_path()
    assert p.replace(os.sep, "/").endswith("data/skill_registry.json")


def test_registry_cache_path_is_gitignored():
    p = reg.default_cache_path()
    # Repo root = two levels up from this test file's parent.
    repo = str(ROOT)
    result = subprocess.run(
        ["git", "check-ignore", p],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"data/skill_registry.json is NOT gitignored (git check-ignore rc="
        f"{result.returncode}, out={result.stdout!r})"
    )
