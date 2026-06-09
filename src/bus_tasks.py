#!/usr/bin/env python3
"""
bus_tasks.py — Odysseus orchestrator side of the task-handoff loop.

Odysseus (orchestrator) maps & plans, then hands work to Hermes (executor)
through the VANTA-Brain bus:

    emit_task()      Odysseus -> writes tasks/inbox/<ts>-<slug>.md   (a job spec)
    [Hermes claims]  inbox/<f> -> active/<f>  (atomic move = lock; claim_task.sh)
    [Hermes works]   ...
    [Hermes done]    -> writes tasks/done/<id>.result.md            (complete_task.sh)
    ingest_results() Odysseus <- reads done/, feeds memory_candidates back into
                     memory.json (which re-mirrors to the bus), clears the active
                     task, and marks the result processed.

Idempotent: processed result filenames are tracked in .bus_state.json so the
scheduler can call ingest_results() on a loop without double-ingesting.

CLI:
    python -m src.bus_tasks emit --goal "Build X" [--priority high] \
        [--acceptance "crit 1" --acceptance "crit 2"] [--context-ref <id>] [--body "..."]
    python -m src.bus_tasks ingest
    python -m src.bus_tasks list
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Reuse paths + redaction from the mirror module (single source of truth).
from src.bus_export import _bus_dir, _read_state, _write_json_atomic, redact

VALID_STATUS = {"done", "failed", "partial"}


# --------------------------------------------------------------------------- #
# Frontmatter helpers (minimal, dependency-light)
# --------------------------------------------------------------------------- #
def _parse_frontmatter(text: str) -> Tuple[Dict, str]:
    """Parse a leading ``---`` YAML-ish frontmatter block. Returns (dict, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw, body = parts[1], parts[2]
    fm: Dict = {}
    cur_list_key: Optional[str] = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        m_item = re.match(r"\s*-\s+(.*)$", line)
        if m_item and cur_list_key:
            fm.setdefault(cur_list_key, []).append(m_item.group(1).strip())
            continue
        m_kv = re.match(r"([A-Za-z0-9_]+):\s*(.*)$", line)
        if m_kv:
            key, val = m_kv.group(1), m_kv.group(2).strip()
            if val == "":
                cur_list_key = key
                fm.setdefault(key, [])
            else:
                cur_list_key = None
                fm[key] = val
    return fm, body.strip()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "task"


# --------------------------------------------------------------------------- #
# Emit  (orchestrator -> executor)
# --------------------------------------------------------------------------- #
def emit_task(
    goal: str,
    acceptance: Optional[List[str]] = None,
    context_refs: Optional[List[str]] = None,
    priority: str = "normal",
    body: str = "",
    task_id: Optional[str] = None,
) -> Dict[str, str]:
    """Write a task spec to tasks/inbox/. Returns {id, path}."""
    if not goal or not goal.strip():
        raise ValueError("emit_task: goal is required")
    bus = _bus_dir()
    inbox = bus / "tasks" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    tid = task_id or str(uuid.uuid4())
    ts = int(time.time())
    fname = f"{ts}-{_slug(goal)}.md"
    path = inbox / fname

    lines = ["---", f"id: {tid}", f"goal: {goal.strip()}", f"priority: {priority}",
             f"created: {ts}", "status: pending"]
    if acceptance:
        lines.append("acceptance:")
        lines += [f"  - {a}" for a in acceptance]
    if context_refs:
        lines.append("context_refs:")
        lines += [f"  - {c}" for c in context_refs]
    lines += ["---", "", body.strip() or goal.strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"id": tid, "path": str(path), "file": fname}


# --------------------------------------------------------------------------- #
# Ingest  (executor results -> orchestrator memory)
# --------------------------------------------------------------------------- #
def _add_memory(text: str, source: str) -> bool:
    """Append a memory via MemoryManager (which re-mirrors to the bus). Returns added?"""
    try:
        from src.constants import DATA_DIR
        from src.memory import MemoryManager
        mm = MemoryManager(DATA_DIR)
        entries = mm.load_all()
        if any((e.get("text") or "").strip().lower() == text.strip().lower() for e in entries):
            return False  # dedup against existing memory
        entries.append(mm.add_entry(text.strip(), source=source, category="project", owner="growthgod"))
        mm.save(entries)  # triggers bus_export.mirror_async
        return True
    except Exception:
        return False


def ingest_results() -> Dict[str, int]:
    """Process new tasks/done/*.result.md. Returns {ingested, memories_added, cleared}."""
    bus = _bus_dir()
    done = bus / "tasks" / "done"
    active = bus / "tasks" / "active"
    done.mkdir(parents=True, exist_ok=True)
    active.mkdir(parents=True, exist_ok=True)

    state = _read_state(bus)
    processed = set(state.get("processed_results", []))

    stats = {"ingested": 0, "memories_added": 0, "cleared": 0}
    for f in sorted(done.glob("*.result.md")):
        if f.name in processed:
            continue
        try:
            fm, _body = _parse_frontmatter(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        tid = fm.get("id")

        # Feed durable facts from the executor back into orchestrator memory.
        for cand in fm.get("memory_candidates", []) or []:
            red, hard = redact(str(cand))
            if hard or not red.strip():
                continue
            if _add_memory(red, source=f"hermes-task:{tid or 'unknown'}"):
                stats["memories_added"] += 1

        # Clear the matching claimed task from active/.
        if tid:
            for af in active.glob("*.md"):
                try:
                    afm, _ = _parse_frontmatter(af.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if afm.get("id") == tid:
                    af.unlink(missing_ok=True)
                    stats["cleared"] += 1

        processed.add(f.name)
        stats["ingested"] += 1

    state["processed_results"] = sorted(processed)
    state["last_ingest"] = int(time.time())
    _write_json_atomic(bus / ".bus_state.json", state)
    return stats


def _counts() -> Dict[str, int]:
    bus = _bus_dir()
    def n(sub):
        d = bus / "tasks" / sub
        return len(list(d.glob("*.md"))) if d.is_dir() else 0
    return {"inbox": n("inbox"), "active": n("active"), "done": n("done")}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Odysseus bus task tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="emit a task to the bus inbox")
    e.add_argument("--goal", required=True)
    e.add_argument("--priority", default="normal", choices=["low", "normal", "high"])
    e.add_argument("--acceptance", action="append", default=[])
    e.add_argument("--context-ref", action="append", default=[], dest="context_refs")
    e.add_argument("--body", default="")

    sub.add_parser("ingest", help="ingest executor results back into memory")
    sub.add_parser("list", help="show inbox/active/done counts")

    args = ap.parse_args()
    if args.cmd == "emit":
        r = emit_task(args.goal, acceptance=args.acceptance, context_refs=args.context_refs,
                      priority=args.priority, body=args.body)
        print(f"✅ Emitted task {r['id']}\n   {r['path']}")
    elif args.cmd == "ingest":
        print(f"✅ ingest: {ingest_results()}")
    elif args.cmd == "list":
        print(f"📊 bus tasks: {_counts()}")


if __name__ == "__main__":
    main()
