#!/usr/bin/env python3
"""
import_hermes_memory.py — Hermes → Odysseus memory importer (local CLI).

Extracts candidate memory entries from a Hermes source, redacts secrets,
deduplicates against the existing Odysseus memory store, and prints a report.
By design this is a *local, offline* tool — the dry-run path writes nothing and
stops after the report.

Two sources (`--source`):
    • production  — ~/.hermes/production/ "Angel Codex" reel studio. Deterministic
                    structured extraction from storyboards, character bibles, and
                    AGENT.md. **This is the default.**
    • sessions    — ~/.hermes/sessions/session_*.json chat logs. Heuristic
                    extraction of durable self-statements from chat turns.

Run:
    python scripts/import_hermes_memory.py --dry-run                    # production
    python scripts/import_hermes_memory.py --dry-run --source sessions  # chat logs

Hard constraints (enforced by construction — this file imports stdlib only):
    • No MCP server.          • No Vanta / VantaBrain.
    • No network listener.    • No outbound network calls.
    • Dry-run writes nothing. • Stops after the report.

A guarded `--commit` path exists to actually append to memory.json, but it is
OFF by default; dry-run is the default mode and the only one that ever runs
without an explicit, separate flag.

Memory entry shape (matches data/memory.json and src/memory.py):
    {"id", "text", "timestamp", "source", "category", "uses", "owner"}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Paths / defaults
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEMORY_FILE = REPO_ROOT / "data" / "memory.json"
DEFAULT_HERMES_DIR = Path.home() / ".hermes" / "sessions"
DEFAULT_PRODUCTION_DIR = Path.home() / ".hermes" / "production"
DEFAULT_OWNER = "growthgod"

VALID_CATEGORIES = {"fact", "project", "contact", "goal", "preference"}

# --------------------------------------------------------------------------- #
# Text similarity — mirrors src/memory.py:get_text_similarity (Jaccard).
# Re-implemented here so the CLI stays import-light and runnable standalone.
# --------------------------------------------------------------------------- #


def _tokenize(text: str) -> list[str]:
    return [w.strip('.,!?";') for w in text.split()]


def text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta, tb = set(_tokenize(a.lower())), set(_tokenize(b.lower()))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #

# (label, compiled pattern). Order matters: specific before generic.
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("openai_anthropic", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    # key=value / key: value style secrets — redact only the value.
    ("assigned_secret", re.compile(
        r"(?i)\b(api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|token|password|passwd|pwd)\b"
        r"(\s*[:=]\s*)"
        r"['\"]?([A-Za-z0-9._\-/+=]{8,})['\"]?"
    )),
]


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, [labels_found]). Replaces secrets in place."""
    found: list[str] = []
    out = text

    for label, pat in _SECRET_PATTERNS:
        if label == "assigned_secret":
            def _sub(m: re.Match) -> str:
                found.append(label)
                return f"{m.group(1)}{m.group(2)}[REDACTED]"
            out = pat.sub(_sub, out)
        else:
            def _sub(m: re.Match, _label=label) -> str:
                found.append(_label)
                return f"[REDACTED:{_label}]"
            out = pat.sub(_sub, out)

    return out, found


# --------------------------------------------------------------------------- #
# Candidate extraction
# --------------------------------------------------------------------------- #

# Explicit "please remember this" triggers — capture the clause after them.
_TRIGGER_RE = re.compile(
    r"(?i)\b(?:remember(?:\s+that)?|note\s+that|don'?t\s+forget(?:\s+that)?|"
    r"for\s+future\s+reference|keep\s+in\s+mind(?:\s+that)?|make\s+a\s+note(?:\s+that)?|"
    r"save\s+this)\b[:,]?\s*(.+)"
)

# (category, pattern) — first match wins. Patterns target durable self-statements.
_CATEGORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("contact", re.compile(r"(?i)\bmy\s+(?:email|phone|number|address|handle)\b")),
    ("fact", re.compile(r"(?i)\b(?:my\s+name\s+is|call\s+me|i\s+live\s+in|i'?m\s+based\s+in|"
                        r"my\s+timezone|my\s+birthday|i\s+work\s+(?:at|as)|i'?m\s+a\b)")),
    ("goal", re.compile(r"(?i)\b(?:my\s+goal\s+is|i\s+want\s+to|i'?m\s+trying\s+to|"
                        r"i\s+aim\s+to|i\s+plan\s+to|i'?m\s+working\s+towards)\b")),
    ("project", re.compile(r"(?i)\b(?:i'?m\s+working\s+on|we'?re\s+building|"
                           r"the\s+project\s+is|working\s+on\s+a\b)")),
    ("preference", re.compile(r"(?i)\b(?:i\s+prefer|i'?d\s+prefer|i\s+(?:really\s+)?like|"
                              r"i\s+love|i\s+hate|i\s+don'?t\s+like|i\s+always|i\s+never|"
                              r"i\s+usually|please\s+(?:always|never)|from\s+now\s+on)\b")),
]

# Strip Hermes' injected runtime notes, e.g. "[Note: model was just switched ...]".
_INJECTED_NOTE_RE = re.compile(r"\[Note:[^\]]*\]")
_QUESTION_START_RE = re.compile(r"(?i)^\s*(?:who|what|when|where|why|how|can|could|"
                                r"would|should|do|does|did|is|are|will)\b")

MIN_LEN = 12
MAX_LEN = 280


@dataclass
class Candidate:
    text: str
    category: str
    source: str
    session_id: str
    timestamp: int
    redacted_labels: list[str] = field(default_factory=list)

    def to_entry(self, owner: str) -> dict:
        entry = {
            "id": str(uuid.uuid4()),
            "text": self.text,
            "timestamp": self.timestamp,
            "source": self.source,
            "category": self.category,
            "uses": 0,
            "owner": owner,
        }
        return entry


def _split_sentences(text: str) -> Iterable[str]:
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # crude sentence split; good enough for memory candidates
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip()
            if part:
                yield part


def _classify(sentence: str) -> Optional[str]:
    for category, pat in _CATEGORY_PATTERNS:
        if pat.search(sentence):
            return category
    return None


def _clean(text: str) -> str:
    text = _INJECTED_NOTE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _message_text(content) -> str:
    """Flatten a message 'content' (str or list of multimodal parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            elif isinstance(p, str):
                parts.append(p)
        return " ".join(parts)
    return ""


def extract_candidates(session: dict, source: str, include_assistant: bool) -> list[Candidate]:
    session_id = str(session.get("session_id") or "")
    ts = _session_timestamp(session)
    roles = {"user"} | ({"assistant"} if include_assistant else set())

    out: list[Candidate] = []
    for msg in session.get("messages", []) or []:
        if not isinstance(msg, dict) or msg.get("role") not in roles:
            continue
        raw = _clean(_message_text(msg.get("content")))
        if not raw:
            continue

        for sentence in _split_sentences(raw):
            cand = _candidate_from_sentence(sentence, source, session_id, ts)
            if cand:
                out.append(cand)
    return out


def _candidate_from_sentence(sentence: str, source: str, session_id: str, ts: int) -> Optional[Candidate]:
    category: Optional[str] = None
    text = sentence

    trig = _TRIGGER_RE.search(sentence)
    if trig:
        text = trig.group(1).strip()
        category = _classify(text) or "fact"
    else:
        category = _classify(sentence)
        if category is None:
            return None
        text = sentence

    text = text.strip().rstrip(".")
    if _QUESTION_START_RE.match(text) or text.endswith("?"):
        return None
    if "```" in text or text.count("{") + text.count("}") >= 4:
        return None  # code-ish, skip

    return _finalize(text, category, source, session_id, ts)


def _finalize(text: str, category: str, source: str, session_id: str, ts: int) -> Optional[Candidate]:
    """Length-cap, redact, and drop if gutted by redaction. Shared by both sources."""
    text = _clean(text).strip().rstrip(".")
    if len(text) < MIN_LEN:
        return None
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN].rsplit(" ", 1)[0] + "…"

    redacted, labels = redact_secrets(text)
    # If redaction gutted the text to near-nothing, drop it.
    residual = re.sub(r"\[REDACTED(?::[^\]]*)?\]", "", redacted).strip()
    if len(residual) < MIN_LEN:
        return None

    return Candidate(
        text=redacted,
        category=category,
        source=source,
        session_id=session_id,
        timestamp=ts,
        redacted_labels=labels,
    )


# --------------------------------------------------------------------------- #
# Production extraction (~/.hermes/production "Angel Codex" studio).
# Deterministic, structured — no chat-message heuristics.
# --------------------------------------------------------------------------- #

_MD_LABEL_RE = re.compile(r"^\*\*([^*]+?):\*\*\s*(.+?)\s*$", re.MULTILINE)


def extract_production_candidates(prod_dir: Path) -> list[Candidate]:
    out: list[Candidate] = []
    if not prod_dir.exists():
        print(f"  ! production dir not found: {prod_dir}", file=sys.stderr)
        return out

    out.extend(_storyboard_candidates(prod_dir / "storyboards"))
    out.extend(_bible_candidates(prod_dir / "characters"))
    out.extend(_markdown_label_candidates(prod_dir / "AGENT.md"))
    out.extend(_markdown_label_candidates(prod_dir / "README.md"))
    return [c for c in out if c]


def _load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! skip {path.name}: {e}", file=sys.stderr)
        return None


def _storyboard_candidates(sb_dir: Path) -> list[Candidate]:
    out: list[Candidate] = []
    if not sb_dir.exists():
        return out
    for p in sorted(sb_dir.glob("*.json")):
        d = _load_json(p)
        if not isinstance(d, dict):
            continue
        ts = int(p.stat().st_mtime)
        name = d.get("display_name") or d.get("title") or d.get("agent_slug") or p.stem
        slug = d.get("agent_slug", "")
        dur = d.get("duration_target_seconds")
        ar = d.get("aspect_ratio", "")
        style = d.get("style_notes", "")
        spec = " ".join(x for x in [f"{dur}s" if dur else "", ar] if x).strip()
        text = f"Angel Codex reel: {name} ({slug})"
        if spec:
            text += f" — {spec}"
        if style:
            text += f". {style}"
        c = _finalize(text, "project", f"production:storyboards/{p.name}", "", ts)
        if c:
            out.append(c)
    return out


def _bible_candidates(char_dir: Path) -> list[Candidate]:
    out: list[Candidate] = []
    if not char_dir.exists():
        return out
    for p in sorted(char_dir.glob("*/bible.json")):
        d = _load_json(p)
        if not isinstance(d, dict):
            continue
        ts = int(p.stat().st_mtime)
        name = d.get("display_name") or d.get("slug") or p.parent.name
        slug = d.get("slug", p.parent.name)
        src = f"production:characters/{p.parent.name}/bible.json"
        desc = d.get("core_description")
        if isinstance(desc, str) and desc.strip():
            c = _finalize(f"{name} ({slug}): {desc}", "fact", src, "", ts)
            if c:
                out.append(c)
        role = d.get("ops_role")
        if isinstance(role, str) and role.strip():
            c = _finalize(f"{name} ops role: {role}", "fact", src, "", ts)
            if c:
                out.append(c)
    return out


def _markdown_label_candidates(md_path: Path) -> list[Candidate]:
    out: list[Candidate] = []
    if not md_path.exists():
        return out
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  ! skip {md_path.name}: {e}", file=sys.stderr)
        return out
    ts = int(md_path.stat().st_mtime)
    src = f"production:{md_path.name}"
    for m in _MD_LABEL_RE.finditer(text):
        label = m.group(1).strip()
        val = m.group(2).strip().strip("`")
        c = _finalize(f"Angel Codex — {label}: {val}", "fact", src, "", ts)
        if c:
            out.append(c)
    return out


def _session_timestamp(session: dict) -> int:
    for key in ("last_updated", "session_start"):
        val = session.get(key)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
    return int(time.time())


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #


def deduplicate(
    candidates: list[Candidate],
    existing_texts: list[str],
    threshold: float,
) -> tuple[list[Candidate], dict[str, int]]:
    """Drop candidates matching existing memory or earlier candidates."""
    stats = {"dup_existing": 0, "dup_candidate": 0}
    existing_lower = [t.lower() for t in existing_texts]
    accepted: list[Candidate] = []
    accepted_lower: list[str] = []

    for cand in candidates:
        cl = cand.text.lower()

        if any(cl == e for e in existing_lower) or _near(cl, existing_lower, threshold):
            stats["dup_existing"] += 1
            continue
        if any(cl == a for a in accepted_lower) or _near(cl, accepted_lower, threshold):
            stats["dup_candidate"] += 1
            continue

        accepted.append(cand)
        accepted_lower.append(cl)

    return accepted, stats


def _near(text_lower: str, pool: list[str], threshold: float) -> bool:
    return any(text_similarity(text_lower, other) >= threshold for other in pool)


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #


def load_sessions(hermes_dir: Path, limit: Optional[int], max_age_days: Optional[int]) -> list[tuple[Path, dict]]:
    if not hermes_dir.exists():
        print(f"  ! Hermes sessions dir not found: {hermes_dir}", file=sys.stderr)
        return []

    files = sorted(hermes_dir.glob("session_*.json"))
    cutoff = None
    if max_age_days is not None:
        cutoff = time.time() - max_age_days * 86400

    out: list[tuple[Path, dict]] = []
    for path in files:
        if cutoff is not None and path.stat().st_mtime < cutoff:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out.append((path, data))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ! skip unreadable {path.name}: {e}", file=sys.stderr)
        if limit is not None and len(out) >= limit:
            break
    return out


def load_existing_memory(memory_file: Path, owner: Optional[str]) -> list[dict]:
    if not memory_file.exists():
        return []
    try:
        with open(memory_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! could not read {memory_file}: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        return []
    if owner is None:
        return data
    return [e for e in data if e.get("owner") == owner]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def print_report(
    *,
    source: str,
    units_scanned: int,
    messages: int,
    raw: int,
    redacted_count: int,
    dedup_stats: dict[str, int],
    accepted: list[Candidate],
    sample_n: int,
    dry_run: bool,
) -> None:
    by_cat: dict[str, int] = {}
    for c in accepted:
        by_cat[c.category] = by_cat.get(c.category, 0) + 1

    line = "─" * 60
    print(f"\n{line}")
    print(" Hermes → Odysseus memory importer — REPORT")
    print(line)
    if source == "production":
        print(f"  Source files scanned ........ {units_scanned}")
    else:
        print(f"  Sessions scanned ............ {units_scanned}")
        print(f"  Messages scanned ............ {messages}")
    print(f"  Raw candidates extracted .... {raw}")
    print(f"  Candidates with redactions .. {redacted_count}")
    print(f"  Dropped (dup vs existing) ... {dedup_stats.get('dup_existing', 0)}")
    print(f"  Dropped (dup vs candidates) . {dedup_stats.get('dup_candidate', 0)}")
    print(f"  ── Final candidates ......... {len(accepted)}")
    if by_cat:
        print("     by category:")
        for cat in sorted(by_cat):
            print(f"       • {cat:<11} {by_cat[cat]}")

    if accepted:
        print(f"\n  Sample (up to {sample_n}):")
        for c in accepted[:sample_n]:
            flag = "  [REDACTED]" if c.redacted_labels else ""
            print(f"    [{c.category}]{flag} {c.text}")
            print(f"        ↳ {c.source}")

    print(line)
    if dry_run:
        print("  DRY RUN — nothing was written. Stopping after report.")
    print(f"{line}\n")


# --------------------------------------------------------------------------- #
# Commit (guarded; never runs during dry-run)
# --------------------------------------------------------------------------- #


def commit(memory_file: Path, accepted: list[Candidate], owner: str) -> None:
    """Append accepted candidates to memory.json with a timestamped backup."""
    existing_all: list[dict] = []
    if memory_file.exists():
        with open(memory_file, "r", encoding="utf-8") as f:
            existing_all = json.load(f)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = memory_file.with_name(f"{memory_file.name}.bak-{stamp}")
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(existing_all, f, ensure_ascii=False, indent=2)
        print(f"  backup written: {backup}")

    merged = existing_all + [c.to_entry(owner) for c in accepted]
    tmp = memory_file.with_suffix(memory_file.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, memory_file)
    print(f"  committed {len(accepted)} entries → {memory_file}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Import Hermes session memory into Odysseus (local, offline).",
    )
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="Analyze and report only; write nothing (default).")
    p.add_argument("--commit", action="store_true",
                   help="Actually append candidates to memory.json (disables dry-run).")
    p.add_argument("--source", choices=["production", "sessions"], default="production",
                   help="Where to read from (default: production).")
    p.add_argument("--production-dir", type=Path, default=DEFAULT_PRODUCTION_DIR,
                   help=f"Hermes production dir (default: {DEFAULT_PRODUCTION_DIR}).")
    p.add_argument("--hermes-dir", type=Path, default=DEFAULT_HERMES_DIR,
                   help=f"Hermes sessions dir (default: {DEFAULT_HERMES_DIR}).")
    p.add_argument("--memory-file", type=Path, default=DEFAULT_MEMORY_FILE,
                   help=f"Odysseus memory store (default: {DEFAULT_MEMORY_FILE}).")
    p.add_argument("--owner", default=DEFAULT_OWNER, help="Memory owner tag.")
    p.add_argument("--limit", type=int, default=None, help="Max sessions to read.")
    p.add_argument("--max-age-days", type=int, default=None,
                   help="Only read sessions modified within N days.")
    p.add_argument("--dedup-threshold", type=float, default=0.85,
                   help="Jaccard similarity at/above which a candidate is a dup.")
    p.add_argument("--include-assistant", action="store_true",
                   help="Also mine assistant turns (default: user turns only).")
    p.add_argument("--sample", type=int, default=15, help="Sample size in report.")
    args = p.parse_args(argv)

    dry_run = not args.commit  # --commit is the only way out of dry-run

    src_dir = args.production_dir if args.source == "production" else args.hermes_dir
    print("Hermes → Odysseus memory importer")
    print(f"  mode .......... {'COMMIT' if not dry_run else 'dry-run'}")
    print(f"  source ........ {args.source}  ({src_dir})")
    print(f"  memory file ... {args.memory_file}")
    print(f"  owner ......... {args.owner}")

    existing = load_existing_memory(args.memory_file, args.owner)
    existing_texts = [e.get("text", "") for e in existing if e.get("text")]
    print(f"  existing memory entries (owner={args.owner}): {len(existing_texts)}")

    raw_candidates: list[Candidate] = []
    units_scanned = 0  # sessions (sessions mode) or source files (production mode)
    message_count = 0

    if args.source == "production":
        raw_candidates = extract_production_candidates(args.production_dir)
        # count source files scanned for the report
        pd = args.production_dir
        units_scanned = (
            len(list((pd / "storyboards").glob("*.json"))) if (pd / "storyboards").exists() else 0
        ) + (
            len(list((pd / "characters").glob("*/bible.json"))) if (pd / "characters").exists() else 0
        ) + sum(1 for f in ("AGENT.md", "README.md") if (pd / f).exists())
    else:
        sessions = load_sessions(args.hermes_dir, args.limit, args.max_age_days)
        units_scanned = len(sessions)
        for path, session in sessions:
            message_count += len(session.get("messages", []) or [])
            source = f"hermes:{path.stem}"
            raw_candidates.extend(
                extract_candidates(session, source, include_assistant=args.include_assistant)
            )

    redacted_count = sum(1 for c in raw_candidates if c.redacted_labels)
    accepted, dedup_stats = deduplicate(raw_candidates, existing_texts, args.dedup_threshold)

    print_report(
        source=args.source,
        units_scanned=units_scanned,
        messages=message_count,
        raw=len(raw_candidates),
        redacted_count=redacted_count,
        dedup_stats=dedup_stats,
        accepted=accepted,
        sample_n=args.sample,
        dry_run=dry_run,
    )

    if dry_run:
        return 0  # stop after report — no writes

    if not accepted:
        print("  Nothing to commit.")
        return 0
    commit(args.memory_file, accepted, args.owner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
