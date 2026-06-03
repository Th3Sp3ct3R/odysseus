# services/skill_registry/__init__.py
"""Read-only Hermes / Claude skills metadata registry (S1/S2).

Public surface — see :mod:`services.skill_registry.registry` for details.
The scanner never writes, deletes, or executes skills, and never stores a
full SKILL.md body.
"""

from .registry import (
    DEFAULT_ALLOWLIST_FILENAME,
    DEFAULT_ROOTS,
    MAX_SUMMARY_CHARS,
    REGISTRY_VERSION,
    S1_DANGEROUS_RISKS,
    SAFE_RISKS,
    SkillMeta,
    build_registry,
    default_allowlist_path,
    default_cache_path,
    derive_summary,
    infer_category,
    is_allowlisted,
    is_exposed,
    list_skills,
    load_allowlist,
    load_registry,
    normalize_allowlist,
    normalize_risk,
    parse_frontmatter,
    parse_skill_file,
    save_registry,
    scan_root,
    search_skills,
)

__all__ = [
    "DEFAULT_ALLOWLIST_FILENAME",
    "DEFAULT_ROOTS",
    "MAX_SUMMARY_CHARS",
    "REGISTRY_VERSION",
    "S1_DANGEROUS_RISKS",
    "SAFE_RISKS",
    "SkillMeta",
    "build_registry",
    "default_allowlist_path",
    "default_cache_path",
    "derive_summary",
    "infer_category",
    "is_allowlisted",
    "is_exposed",
    "list_skills",
    "load_allowlist",
    "load_registry",
    "normalize_allowlist",
    "normalize_risk",
    "parse_frontmatter",
    "parse_skill_file",
    "save_registry",
    "scan_root",
    "search_skills",
]
