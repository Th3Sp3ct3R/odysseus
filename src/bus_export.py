#!/usr/bin/env python3
"""
bus_export.py — Odysseus memory -> VANTA-Brain Obsidian vault mirror.

The missing reverse direction. tools/obsidian_sync/sync_memory.py pulls the
vault INTO Odysseus; this module mirrors Odysseus `data/memory.json` OUT to the
shared bus so Hermes can pull it (via its SessionStart hook).

Two trigger paths:
  - Real-time:   MemoryManager.save() calls mirror_async(entries) fire-and-forget.
  - Safety sweep: `python -m src.bus_export` (or `python src/bus_export.py`) — full
    re-mirror, run on a cron to catch any drift.

Guarantees:
  - Redaction gate: every entry's text passes redact(); entries containing hard
    secrets (connection strings, private keys, API keys, creds) are SKIPPED —
    never written to the vault.
  - Idempotent: per-entry content hash in .bus_state.json. Unchanged -> no write.
    The volatile `uses` counter is excluded from the hash, so use-bumps (which
    call save() constantly) never trigger a re-mirror.
  - Fail-open & non-blocking: mirror_async never raises into the caller and runs
    on a daemon thread, so it can never slow or break Odysseus's memory writes.

Outputs (under $ODYSSEUS_VAULT_DIR/odysseus/, default ~/Documents/VANTA-Brain/odysseus/):
  memory/MEMORY.md          human-readable index
  memory/entries/<id>.md    one note per memory
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _vault_root() -> Path:
    return Path(os.environ.get("ODYSSEUS_VAULT_DIR", str(Path.home() / "Documents" / "VANTA-Brain")))


def _bus_dir() -> Path:
    return _vault_root() / "odysseus"


def _memory_json() -> Path:
    # Resolve against the Odysseus repo data dir, independent of cwd.
    here = Path(__file__).resolve().parent.parent  # repo root
    return here / "data" / "memory.json"


# --------------------------------------------------------------------------- #
# Redaction gate — vendored from ~/.hermes/tools/odysseus-memory-bridge/scan.py
# Kept in-repo so Odysseus stays self-contained (no cross-repo import of ~/.hermes).
# --------------------------------------------------------------------------- #
SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b([a-z]+)://[^\s:/@]+:[^\s:/@]+@[^\s]+", re.I), "<REDACTED_CONNSTR>"),
    # bare email:password credential pairs (the Suno/IG ingest format) — no scheme prefix.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}:[^\s:@]{4,}"), "<REDACTED_CRED>"),
    (re.compile(r"(?i)\bssh\s+(-\S+\s+)*[a-z0-9_.-]+@[a-z0-9_.:-]+"), "<REDACTED_SSH>"),
    (re.compile(r"\b[a-z0-9_]+@(?:\d{1,3}\.){3}\d{1,3}\b", re.I), "<REDACTED_SSH>"),
    (re.compile(r"\bDB:\s*\S+/\S+/\S+", re.I), "<REDACTED_DBCRED>"),
    (re.compile(r"(?i)\b(sshpass\s+)?(pw|pass(word)?)\s*[=:]\s*\S+"), "<REDACTED_PASSWORD>"),
    (re.compile(r"(?i)\b(bearer|authorization|auth)\s*[:=]?\s*[A-Za-z0-9._\-:+/]{16,}"), "<REDACTED_TOKEN>"),
    (re.compile(
        r"\b(sk-[A-Za-z0-9]{16,}|npg_[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
        r"|IGT:[0-9]:[A-Za-z0-9+/=_-]{10,})\b"), "<REDACTED_KEY>"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|client[_-]?secret)\s*[=:]\s*\S+"), "<REDACTED_SECRET>"),
    (re.compile(r"(?i)\b(sessionid|ds_user_id|__session|csrftoken|x-mid|set-cookie)\s*[=:]\s*\S+"), "<REDACTED_COOKIE>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<REDACTED_PRIVATE_KEY>"),
]
# If a mirror candidate trips one of these after redaction, reject it outright.
HARD_REJECT = re.compile(r"<REDACTED_(CONNSTR|CRED|PRIVATE_KEY|DBCRED|SSH|KEY|PASSWORD|TOKEN|SECRET|COOKIE)>")


def redact(text: str) -> Tuple[str, bool]:
    """Return (redacted_text, is_hard_secret). is_hard_secret=True => do not mirror."""
    out = text
    for pat, repl in SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out, bool(HARD_REJECT.search(out))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _entry_hash(e: Dict) -> str:
    """Stable per-entry hash. Excludes volatile `uses` so use-bumps don't re-mirror."""
    basis = "|".join(str(e.get(k, "")) for k in ("text", "category", "source", "owner"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _load_entries() -> List[Dict]:
    p = _memory_json()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:  # corrupt/locked mid-write — skip this pass
        logger.debug("bus_export: could not read memory.json (%s)", e)
        return []


def _read_state(bus: Path) -> Dict:
    sp = bus / ".bus_state.json"
    if sp.exists():
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _entry_note(e: Dict, red_text: str) -> str:
    fm = {
        "id": e.get("id", ""),
        "source": e.get("source", ""),
        "category": e.get("category", "fact"),
        "owner": e.get("owner", "growthgod"),
        "timestamp": e.get("timestamp", 0),
    }
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(red_text.strip())
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def mirror_all(entries: Optional[List[Dict]] = None) -> Dict[str, int]:
    """Mirror memory.json -> vault. Returns {mirrored, skipped_secret, unchanged, pruned}.

    Idempotent: only writes notes whose content hash changed; only rewrites
    MEMORY.md when the index changed. Safe to call repeatedly.
    """
    if entries is None:
        entries = _load_entries()

    bus = _bus_dir()
    mem_dir = bus / "memory"
    entries_dir = mem_dir / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)
    (bus / "tasks" / "inbox").mkdir(parents=True, exist_ok=True)
    (bus / "tasks" / "active").mkdir(parents=True, exist_ok=True)
    (bus / "tasks" / "done").mkdir(parents=True, exist_ok=True)

    state = _read_state(bus)
    note_hashes: Dict[str, str] = dict(state.get("memory_notes", {}))

    stats = {"mirrored": 0, "skipped_secret": 0, "unchanged": 0, "pruned": 0}
    index_rows: List[Tuple[int, str, str, str]] = []  # (ts, category, source, text)
    live_ids = set()

    for e in entries:
        eid = e.get("id")
        text = (e.get("text") or "").strip()
        if not eid or not text:
            continue
        red_text, is_secret = redact(text)
        if is_secret:
            stats["skipped_secret"] += 1
            continue
        live_ids.add(eid)
        index_rows.append((int(e.get("timestamp", 0) or 0), e.get("category", "fact"),
                           e.get("source", ""), red_text))
        h = _entry_hash(e)
        if note_hashes.get(eid) == h and (entries_dir / f"{eid}.md").exists():
            stats["unchanged"] += 1
            continue
        (entries_dir / f"{eid}.md").write_text(_entry_note(e, red_text), encoding="utf-8")
        note_hashes[eid] = h
        stats["mirrored"] += 1

    # Prune notes whose memory was deleted upstream.
    for f in entries_dir.glob("*.md"):
        if f.stem not in live_ids:
            f.unlink(missing_ok=True)
            note_hashes.pop(f.stem, None)
            stats["pruned"] += 1

    # Rebuild MEMORY.md index only if the index signature changed.
    index_rows.sort(key=lambda r: r[0], reverse=True)
    index_sig = hashlib.sha256(
        "\n".join(f"{c}|{s}|{t}" for _, c, s, t in index_rows).encode("utf-8")
    ).hexdigest()[:16]

    if state.get("index_sig") != index_sig:
        md = ["---", "type: memory-mirror", "owner: growthgod",
              f"count: {len(index_rows)}", f"generated: {int(time.time())}", "---", "",
              "# Odysseus Memory (mirror)", "",
              "_Auto-generated from `~/Desktop/VAN/odysseus/data/memory.json`. "
              "Read-only mirror — edit memory in Odysseus, not here._", ""]
        last_cat = None
        for _, cat, src, text in index_rows:
            if cat != last_cat:
                md.append(f"\n## {cat}\n")
                last_cat = cat
            tag = f" _({src})_" if src else ""
            md.append(f"- {text}{tag}")
        md.append("")
        (mem_dir / "MEMORY.md").write_text("\n".join(md), encoding="utf-8")

    state["memory_notes"] = note_hashes
    state["index_sig"] = index_sig
    state["last_mirror"] = int(time.time())
    _write_json_atomic(bus / ".bus_state.json", state)
    return stats


def mirror_async(entries: Optional[List[Dict]] = None) -> None:
    """Fire-and-forget real-time mirror. Never raises into the caller."""
    snapshot = list(entries) if entries is not None else None

    def _run():
        try:
            mirror_all(snapshot)
        except Exception as e:  # never let the bus break a memory write
            logger.debug("bus_export.mirror_async failed: %s", e)

    try:
        threading.Thread(target=_run, name="bus-mirror", daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = mirror_all()
    print(f"✅ bus_export: {result}")
    print(f"   vault: {_bus_dir()}")
