# services/skill_registry/__main__.py
"""CLI for the read-only skills registry.

Examples::

    # Build the local cache from ~/.claude/skills + ~/.hermes/skills
    python -m services.skill_registry build

    # List exposed skills from the Hermes root
    python -m services.skill_registry list --source hermes --exposed

    # Search by text, filter by risk
    python -m services.skill_registry search --query voice --risk safe

The scanner is strictly read-only; ``build`` only writes the gitignored cache
at ``data/skill_registry.json``.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .registry import (
    DEFAULT_ROOTS,
    build_registry,
    default_allowlist_path,
    default_cache_path,
    list_skills,
    load_allowlist,
    load_registry,
    save_registry,
    search_skills,
)


def _print_rows(rows: List[dict]) -> None:
    for s in rows:
        flag = "EXPOSED " if s.get("exposed") else "disabled"
        print(f"[{flag}] {s.get('root'):6} {s.get('risk'):10} {s.get('name')}")
    print(f"\n{len(rows)} skill(s)")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="services.skill_registry")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="scan roots and write the local cache")
    pb.add_argument("--out", default=None, help="cache path (default data/skill_registry.json)")
    pb.add_argument("--allowlist", default=None, help="JSON file of explicitly allowed skill names")

    pl = sub.add_parser("list", help="list skills from the cache")
    pl.add_argument("--source", default=None, choices=sorted(DEFAULT_ROOTS))
    pl.add_argument("--exposed", action="store_true", help="only exposed skills")
    pl.add_argument("--cache", default=None)

    ps = sub.add_parser("search", help="search/filter the cache")
    ps.add_argument("--query", default=None)
    ps.add_argument("--risk", default=None)
    ps.add_argument("--source", default=None, choices=sorted(DEFAULT_ROOTS))
    ps.add_argument("--category", default=None)
    ps.add_argument("--exposed", dest="exposed", action="store_true")
    ps.add_argument("--disabled", dest="disabled", action="store_true")
    ps.add_argument("--cache", default=None)

    args = p.parse_args(argv)

    if args.cmd == "build":
        # Explicit --allowlist wins; otherwise auto-load the optional default
        # file if it exists. Absent file → empty allowlist (default scan).
        allow_path = args.allowlist or default_allowlist_path()
        allow = load_allowlist(allow_path)
        reg = build_registry(allowlist=allow)
        out = save_registry(reg, args.out)
        c = reg["counts"]
        src = allow_path if (args.allowlist or allow) else "(none)"
        print(f"Scanned {c['total']} skills → {c['exposed']} exposed, {c['disabled']} disabled")
        print(f"Allowlist: {src} ({len(allow)} entr{'y' if len(allow)==1 else 'ies'})")
        print(f"Cache written: {out}")
        return 0

    cache = getattr(args, "cache", None) or default_cache_path()
    try:
        reg = load_registry(cache)
    except FileNotFoundError:
        print(f"No registry cache at {cache!r}. Run `build` first.", file=sys.stderr)
        return 1

    if args.cmd == "list":
        rows = list_skills(reg, source=args.source)
        if args.exposed:
            rows = [s for s in rows if s.get("exposed")]
        _print_rows(rows)
        return 0

    if args.cmd == "search":
        exposed_filter: Optional[bool] = None
        if args.exposed and not args.disabled:
            exposed_filter = True
        elif args.disabled and not args.exposed:
            exposed_filter = False
        rows = search_skills(
            reg,
            query=args.query,
            risk=args.risk,
            source=args.source,
            category=args.category,
            exposed=exposed_filter,
        )
        _print_rows(rows)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
