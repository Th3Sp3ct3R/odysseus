"""Read-only Hermes / Claude skills metadata registry (S1/S2).

Scans the on-disk skill libraries — ``~/.claude/skills`` and
``~/.hermes/skills`` — and builds a *metadata-only* index. The scanner is
strictly read-only: it opens ``SKILL.md`` files for reading and never writes,
moves, deletes, or executes anything inside a skill directory.

Design invariants (enforced by tests in ``tests/test_skill_registry.py``):

  * Never store a full ``SKILL.md`` body. Only a bounded, deterministic
    summary derived from frontmatter (or, as a fallback, from the body's
    first heading + first meaningful paragraph). No LLM is involved.
  * Conservative exposure. Only ``risk: safe`` / ``risk: none`` (or an
    explicit allowlist entry) are marked ``exposed=True``. A missing risk
    field is treated as ``unknown`` and indexed as *disabled metadata only*.
    Offensive / unknown / critical skills are never auto-exposed.
  * The generated registry cache (``data/skill_registry.json``) is a local,
    gitignored artifact. This module never writes it under version control.

This module is intentionally dependency-free (stdlib only) so the scanner
stays light and import-cheap, independent of the heavier ``services.memory``
package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

#: Default skill-library roots, labelled by their "root source".
DEFAULT_ROOTS: Dict[str, str] = {
    "claude": os.path.expanduser("~/.claude/skills"),
    "hermes": os.path.expanduser("~/.hermes/skills"),
}

#: Risk values that are safe to auto-expose.
SAFE_RISKS = frozenset({"safe", "none"})

#: S1 "dangerous" tier — never exposed, even via the allowlist. Unlocking these
#: requires an explicitly-approved future dangerous-mode policy (not S1).
#: ``offensive`` is the spec-mandated hard-block; ``critical`` is included as a
#: conservative extension of the same dangerous class (see runbook).
S1_DANGEROUS_RISKS = frozenset({"offensive", "critical"})

#: Canonical risk used when a SKILL.md declares none. Conservative on purpose.
DEFAULT_RISK = "unknown"

#: Hard cap on the stored summary so we never smuggle a body into the cache.
MAX_SUMMARY_CHARS = 280

#: Schema version of the emitted registry document.
REGISTRY_VERSION = 1

#: Optional, gitignored allowlist file. Absent by default; not required.
DEFAULT_ALLOWLIST_FILENAME = "skill_registry_allowlist.json"

_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")
_FM_LIST_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*#*\s*$")


# ---------------------------------------------------------------------------
# Metadata record (NO body field — by design)
# ---------------------------------------------------------------------------

@dataclass
class SkillMeta:
    name: str
    description: str                       # bounded summary, never the full body
    risk: str
    source: Optional[str]
    date_added: Optional[str]
    root: str                              # "claude" | "hermes"
    path: str
    category: str                          # inferred from path
    modified: float                        # mtime (epoch seconds)
    content_hash: str                      # sha256 of the SKILL.md bytes
    exposed: bool                          # computed from risk policy + allowlist
    frontmatter_enabled: Optional[bool] = None  # raw `enabled`/`exposed` flag if declared

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Minimal, dependency-free frontmatter parser
# ---------------------------------------------------------------------------

def _parse_scalar(raw: str) -> Any:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    # inline list: [a, b, c]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p) for p in inner.split(",")]
    return v


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter_dict, body).

    Handles ``key: value``, quoted values, inline ``[a, b]`` lists and simple
    block lists (``- item`` lines under a bare key). Unknown YAML constructs
    degrade gracefully to strings. Never raises on malformed input.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm_text = text[3:end].lstrip("\n")
    body = text[end + 4:].lstrip("\n")

    fm: Dict[str, Any] = {}
    pending_key: Optional[str] = None
    for line in fm_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FM_KEY_RE.match(line)
        if m and not line.startswith(("-", " ", "\t")):
            key, val = m.group(1), m.group(2)
            if val.strip() == "":
                pending_key = key
                fm[key] = []
            else:
                fm[key] = _parse_scalar(val)
                pending_key = None
            continue
        item = _FM_LIST_ITEM_RE.match(line)
        if item and pending_key is not None:
            bucket = fm.get(pending_key)
            if not isinstance(bucket, list):
                fm[pending_key] = bucket = []
            bucket.append(_parse_scalar(item.group(1)))
    return fm, body


# ---------------------------------------------------------------------------
# Deterministic summary fallback (no LLM)
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def derive_summary(frontmatter: Dict[str, Any], body: str) -> str:
    """Return a bounded one-line summary.

    Prefers the frontmatter ``description``. When absent/empty, deterministically
    derives one from the body: the first markdown heading and the first
    meaningful paragraph (skipping headings, list bullets, code fences and HTML
    comments). Purely string ops — no model is ever called.
    """
    desc = frontmatter.get("description")
    if isinstance(desc, list):
        desc = " ".join(str(x) for x in desc)
    if isinstance(desc, str) and desc.strip():
        return _truncate(desc.strip())

    heading = ""
    paragraph = ""
    in_fence = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            if not heading:
                heading = hm.group(1).strip()
            continue
        if line.startswith(("-", "*", ">", "|")) or line.startswith("<!--"):
            continue
        paragraph = line
        break

    parts = [p for p in (heading, paragraph) if p]
    if not parts:
        return ""
    if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
        joined = parts[1]
    else:
        joined = ": ".join(parts)
    return _truncate(joined)


# ---------------------------------------------------------------------------
# Risk + exposure policy
# ---------------------------------------------------------------------------

def normalize_risk(value: Any) -> str:
    if value is None:
        return DEFAULT_RISK
    s = str(value).strip().lower()
    return s or DEFAULT_RISK


def normalize_allowlist(entries: Optional[Iterable[Any]]) -> set:
    """Normalize allowlist entries into a flat set of match tokens.

    Accepts plain strings (a ``skill_id``/name or a filesystem path) or dict
    rows like ``{"skill_id": "..."}`` / ``{"name": "..."}`` / ``{"path": "..."}``.
    Path entries are also stored with ``~`` expanded so user-relative paths match.
    """
    out: set = set()
    for e in entries or ():
        if isinstance(e, dict):
            for k in ("skill_id", "name", "id", "path"):
                v = e.get(k)
                if v:
                    out.add(str(v))
                    out.add(os.path.expanduser(str(v)))
        elif e:
            out.add(str(e))
            out.add(os.path.expanduser(str(e)))
    return out


def is_allowlisted(name: str, path: Optional[str], allow: set) -> bool:
    """True if the skill is allowlisted by ``skill_id``/name or by path."""
    if name and name in allow:
        return True
    if path:
        if path in allow or os.path.expanduser(path) in allow:
            return True
    return False


def is_exposed(
    risk: str,
    name: str,
    allowlist: Iterable[Any] = (),
    *,
    path: Optional[str] = None,
) -> bool:
    """Conservative S1 exposure decision.

    * ``safe`` / ``none`` → exposed (default).
    * S1 dangerous tier (``offensive``, ``critical``) → **never** exposed, even
      if allowlisted. Unlocking requires a future, explicitly-approved
      dangerous-mode policy.
    * Everything else (``unknown``, ``low``, missing → ``unknown``) → disabled
      unless explicitly allowlisted by ``skill_id``/name or path.
    """
    if risk in S1_DANGEROUS_RISKS:
        return False
    if risk in SAFE_RISKS:
        return True
    allow = allowlist if isinstance(allowlist, set) else normalize_allowlist(allowlist)
    return is_allowlisted(name, path, allow)


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "1", "on", "enabled"):
            return True
        if s in ("false", "no", "0", "off", "disabled"):
            return False
    return None


# ---------------------------------------------------------------------------
# Path → category inference
# ---------------------------------------------------------------------------

def infer_category(root: str, skill_dir: str) -> str:
    """Infer a coarse category from the skill's path.

    * Genuinely nested skills (``root/<group>/<name>/SKILL.md``) → ``<group>``.
    * Flat skills (``root/<name>/SKILL.md``) → the leading namespace token of
      the directory name (``gitgod-foo`` → ``gitgod``), else the name itself.
    """
    rel = os.path.relpath(skill_dir, root)
    parts = [p for p in rel.split(os.sep) if p not in (".", "")]
    if len(parts) >= 2:
        return parts[0]
    name = parts[0] if parts else ""
    if "-" in name:
        return name.split("-", 1)[0]
    return name or "_root"


# ---------------------------------------------------------------------------
# Read-only scanning
# ---------------------------------------------------------------------------

def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_skill_file(
    path: str,
    *,
    root: str,
    root_label: str,
    allowlist: Iterable[Any] = (),
) -> SkillMeta:
    """Parse a single SKILL.md into a :class:`SkillMeta` (read-only)."""
    allow = allowlist if isinstance(allowlist, set) else normalize_allowlist(allowlist)
    with open(path, "rb") as fh:                      # read-only, binary for a stable hash
        data = fh.read()
    content_hash = _hash_bytes(data)
    text = data.decode("utf-8", errors="replace")
    fm, body = parse_frontmatter(text)

    skill_dir = os.path.dirname(path)
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        name = os.path.basename(skill_dir)
    name = str(name).strip()

    risk = normalize_risk(fm.get("risk"))
    source = fm.get("source")
    source = str(source) if source not in (None, "") else None
    date_added = fm.get("date_added")
    date_added = str(date_added) if date_added not in (None, "") else None

    enabled_flag = _coerce_bool(fm.get("enabled"))
    if enabled_flag is None:
        enabled_flag = _coerce_bool(fm.get("exposed"))

    return SkillMeta(
        name=name,
        description=derive_summary(fm, body),
        risk=risk,
        source=source,
        date_added=date_added,
        root=root_label,
        path=path,
        category=infer_category(root, skill_dir),
        modified=os.path.getmtime(path),
        content_hash=content_hash,
        exposed=is_exposed(risk, name, allow, path=path),
        frontmatter_enabled=enabled_flag,
    )


def scan_root(
    root_label: str,
    root_path: str,
    *,
    allowlist: Iterable[str] = (),
) -> List[SkillMeta]:
    """Walk one skill root and return metadata for every SKILL.md found.

    Read-only. A missing root yields an empty list rather than raising.
    """
    root_path = os.path.expanduser(root_path)
    if not os.path.isdir(root_path):
        return []
    allow = allowlist if isinstance(allowlist, set) else normalize_allowlist(allowlist)
    out: List[SkillMeta] = []
    for dirpath, _dirnames, filenames in os.walk(root_path):
        if "SKILL.md" in filenames:
            full = os.path.join(dirpath, "SKILL.md")
            try:
                out.append(
                    parse_skill_file(
                        full, root=root_path, root_label=root_label, allowlist=allow
                    )
                )
            except OSError:
                # Unreadable file — skip; never let one bad skill abort the scan.
                continue
    out.sort(key=lambda s: (s.root, s.name, s.path))
    return out


def build_registry(
    roots: Optional[Dict[str, str]] = None,
    *,
    allowlist: Iterable[str] = (),
) -> Dict[str, Any]:
    """Build the in-memory registry document (deterministic; no timestamp).

    ``roots`` maps a *root source* label ("claude"/"hermes") to a directory.
    Defaults to :data:`DEFAULT_ROOTS`. Output ordering is stable so the cache
    diffs cleanly.
    """
    roots = roots if roots is not None else DEFAULT_ROOTS
    allow_set = normalize_allowlist(allowlist)
    skills: List[SkillMeta] = []
    for label, path in roots.items():
        skills.extend(scan_root(label, path, allowlist=allow_set))

    exposed = sum(1 for s in skills if s.exposed)
    return {
        "version": REGISTRY_VERSION,
        "allowlist": sorted(allow_set),
        "counts": {
            "total": len(skills),
            "exposed": exposed,
            "disabled": len(skills) - exposed,
        },
        "skills": [s.to_dict() for s in skills],
    }


# ---------------------------------------------------------------------------
# Cache persistence (local, gitignored artifact)
# ---------------------------------------------------------------------------

def _data_dir() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, "data")


def default_cache_path() -> str:
    return os.path.join(_data_dir(), "skill_registry.json")


def default_allowlist_path() -> str:
    return os.path.join(_data_dir(), DEFAULT_ALLOWLIST_FILENAME)


def load_allowlist(path: Optional[str] = None) -> List[Any]:
    """Load allowlist entries from a JSON file. Optional — never required.

    Returns ``[]`` when the file is absent so default scans work with no
    allowlist present. Accepts either a bare JSON array of entries or an object
    with an ``"allowlist"`` key. Entries may be ``skill_id``/name strings,
    path strings, or ``{"skill_id"|"name"|"path": ...}`` dicts.
    """
    path = path or default_allowlist_path()
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("allowlist", [])
    if not isinstance(data, list):
        return []
    return list(data)


def save_registry(registry: Dict[str, Any], path: Optional[str] = None) -> str:
    """Write the registry cache to ``data/skill_registry.json`` (gitignored).

    Stamps ``generated_at`` at write time (kept out of :func:`build_registry`
    so the build stays deterministic for tests).
    """
    path = path or default_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = dict(registry)
    doc["generated_at"] = time.time()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
    os.replace(tmp, path)
    return path


def load_registry(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or default_cache_path()
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Query surface (metadata only)
# ---------------------------------------------------------------------------

def _skill_list(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(registry.get("skills", []))


def list_skills(
    registry: Dict[str, Any],
    *,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List skill metadata, optionally filtered to one root source."""
    skills = _skill_list(registry)
    if source is not None:
        skills = [s for s in skills if s.get("root") == source]
    return skills


def search_skills(
    registry: Dict[str, Any],
    *,
    query: Optional[str] = None,
    risk: Optional[str] = None,
    exposed: Optional[bool] = None,
    source: Optional[str] = None,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search/filter the registry. Returns metadata records only — never bodies.

    ``query`` matches (case-insensitively) against name, description and
    category. The other parameters are exact-match filters.
    """
    results = _skill_list(registry)
    if source is not None:
        results = [s for s in results if s.get("root") == source]
    if risk is not None:
        rk = risk.strip().lower()
        results = [s for s in results if s.get("risk") == rk]
    if exposed is not None:
        results = [s for s in results if bool(s.get("exposed")) is exposed]
    if category is not None:
        results = [s for s in results if s.get("category") == category]
    if query:
        q = query.strip().lower()
        results = [
            s for s in results
            if q in str(s.get("name", "")).lower()
            or q in str(s.get("description", "")).lower()
            or q in str(s.get("category", "")).lower()
        ]
    return results
