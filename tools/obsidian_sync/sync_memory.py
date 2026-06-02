#!/usr/bin/env python3
"""
One-way Obsidian -> Odysseus Core Memory sync.

DESIGN GOALS (16GB Mac, low disk, no local inference server):
  - Deterministic. NEVER calls an LLM. Extraction is pure rules + parsing.
  - Curated only. Imports durable facts from a small allow-list of dirs,
    NOT the whole vault. Full-vault semantic search is a separate concern
    (Documents/RAG), handled by RAG_SOURCE_DIRS -> manifest only.
  - Idempotent. Dedups by normalized-text hash across runs (.sync_state.json)
    and relies on server-side exact-match dedup as a second guard.
  - Safe by default. DRY_RUN=true unless explicitly disabled.

PERSISTENCE:
  Writes each new entry via POST /api/memory/add (form-encoded). That path
  dedups, syncs the ChromaDB vector index, and attributes ownership to the
  authenticated user (cookie session). It does NOT invoke an LLM.

  The generated JSON artifact (out/memory_import_*.json) is shaped
  [{text, category, source, owner}, ...] which is exactly the shape the
  in-app memory importer (/api/memory/import) round-trips with NO LLM call,
  so you can also drag-drop it into the UI if you prefer.

USAGE:
  python3 sync_memory.py                 # dry run (default), prints report
  python3 sync_memory.py --commit        # actually import (DRY_RUN=false)
  python3 sync_memory.py --config x.yaml

Config precedence (low -> high): defaults < config.yaml < env vars < CLI flags.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    yaml = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

HERE = Path(__file__).resolve().parent
VALID_CATEGORIES = {"identity", "preference", "fact", "contact", "project", "goal"}

# Files that are inherently time-sequenced / ephemeral -> never durable facts.
EPHEMERAL_FILENAME_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{12,})")
# Headings under which content is a log/feed, not a durable fact.
EPHEMERAL_HEADINGS = {
    "activity log", "log", "logs", "timeline", "changelog", "history",
    "todo", "todos", "tasks", "appointments", "known appointments",
    "daily", "journal", "agenda", "schedule", "session log", "chat log",
}
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+•]\s+|\d{1,3}[.):]\s+)")
_BOLD_KV_RE = re.compile(r"^\s*\*\*(?P<k>[^*]+)\*\*\s*[:\-]\s*(?P<v>.+)$")
_WS_RE = re.compile(r"\s+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL_RE = re.compile(r"https?://\S+")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DEFAULTS: Dict[str, Any] = {
    "odysseus_url": "http://localhost:7001",
    "username": "growthgod",
    "password": "",                 # prefer env ODYSSEUS_PASSWORD
    "owner": "growthgod",
    "memory_source_dirs": [],       # curated durable-fact dirs (ALLOW-LIST)
    "rag_source_dirs": [],          # NOT imported to memory; manifest only
    "max_memory_entries_per_run": 200,
    "dry_run": True,
    "include_dated": False,         # include YYYY-MM-DD.md daily notes
    "source_prefix": "obsidian",
    "min_len": 8,
    "max_len": 400,
    "internal_token": "",           # loopback impersonation; prefer env over yaml
}

ENV_MAP = {
    "ODYSSEUS_URL": ("odysseus_url", str),
    "ODYSSEUS_USERNAME": ("username", str),
    "ODYSSEUS_PASSWORD": ("password", str),
    "ODYSSEUS_OWNER": ("owner", str),
    "MEMORY_SOURCE_DIRS": ("memory_source_dirs", "pathlist"),
    "RAG_SOURCE_DIRS": ("rag_source_dirs", "pathlist"),
    "MAX_MEMORY_ENTRIES_PER_RUN": ("max_memory_entries_per_run", int),
    "DRY_RUN": ("dry_run", "bool"),
    "INCLUDE_DATED": ("include_dated", "bool"),
    # Loopback-only impersonation token. When set, the script authenticates
    # via the X-Odysseus-Internal-Token + X-Odysseus-Owner headers instead of
    # a password login. Only honored by the server for 127.0.0.1/::1 callers.
    "ODYSSEUS_INTERNAL_TOKEN": ("internal_token", str),
}


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _as_pathlist(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    # comma- or os.pathsep-separated
    parts = re.split(r"[,\n" + re.escape(os.pathsep) + r"]", str(v))
    return [p.strip() for p in parts if p.strip()]


def load_config(path: Optional[str], cli: argparse.Namespace) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg_path = Path(path) if path else (HERE / "config.yaml")
    if cfg_path.exists():
        if yaml is None:
            sys.exit("PyYAML required to read config.yaml (pip install pyyaml) — or use env vars.")
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        for k, v in loaded.items():
            cfg[k] = v
    # env overrides
    for env_key, (cfg_key, typ) in ENV_MAP.items():
        if env_key in os.environ:
            raw = os.environ[env_key]
            if typ == "bool":
                cfg[cfg_key] = _as_bool(raw)
            elif typ == "pathlist":
                cfg[cfg_key] = _as_pathlist(raw)
            elif typ is int:
                cfg[cfg_key] = int(raw)
            else:
                cfg[cfg_key] = raw
    # normalize list-ish fields that may have come from yaml as str
    cfg["memory_source_dirs"] = _as_pathlist(cfg["memory_source_dirs"])
    cfg["rag_source_dirs"] = _as_pathlist(cfg["rag_source_dirs"])
    # CLI overrides
    if cli.commit:
        cfg["dry_run"] = False
    if cli.dry_run:
        cfg["dry_run"] = True
    if cli.max is not None:
        cfg["max_memory_entries_per_run"] = cli.max
    return cfg


# --------------------------------------------------------------------------- #
# Normalization + dedup
# --------------------------------------------------------------------------- #
def normalize_text(text: str) -> str:
    """Canonical form for dedup hashing: strip md, lowercase, collapse ws."""
    t = _MD_LINK_RE.sub(r"\1", text)          # [label](url) -> label
    t = t.replace("`", "").replace("*", "").replace("_", "")
    t = _WS_RE.sub(" ", t).strip().lower()
    t = t.strip(" .,:;-—–\t")
    return t


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Quality + category
# --------------------------------------------------------------------------- #
def quality_ok(text: str, min_len: int, max_len: int) -> bool:
    n = normalize_text(text)
    if not (min_len <= len(n) <= max_len):
        return False
    # must contain at least two word-ish tokens with letters
    words = [w for w in n.split() if any(c.isalpha() for c in w)]
    if len(words) < 2:
        return False
    # reject pure-URL / pure-link noise
    stripped = _URL_RE.sub("", text).strip()
    if len(normalize_text(stripped)) < min_len:
        return False
    return True


def detect_category(text: str, meta: Dict[str, Any], rel_path: str) -> str:
    fm = str(meta.get("category", "")).strip().lower()
    if fm in VALID_CATEGORIES:
        return fm
    parts = {p.lower() for p in Path(rel_path).parts}
    if "people" in parts or "contacts" in parts:
        return "contact"
    t = text.lower()
    if "@" in t or re.search(r"\b(phone|email|lives|works at|call me)\b", t):
        return "contact"
    if re.search(r"\b(goal|want to|aim to|objective|plan to|aspire)\b", t):
        return "goal"
    if re.search(r"\b(project|building|repo|platform|github\.com|tool|app)\b", t):
        return "project"
    if re.search(r"\b(prefer|favou?rite|i like|i love|i hate|dislike|enjoy)\b", t):
        return "preference"
    if re.search(r"\b(my name is|i am a|i'm a|name is|call me)\b", t):
        return "identity"
    return "fact"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_frontmatter(raw: str) -> Tuple[Dict[str, Any], str]:
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            block = raw[3:end].strip()
            body = raw[end + 4:]
            meta: Dict[str, Any] = {}
            if yaml is not None:
                try:
                    meta = yaml.safe_load(block) or {}
                except Exception:
                    meta = {}
            return (meta if isinstance(meta, dict) else {}), body
    return {}, raw


def _candidate_lines(body: str) -> List[str]:
    """Yield durable-fact candidate strings from markdown body, skipping
    content under ephemeral headings, code fences, and headings themselves."""
    out: List[str] = []
    in_code = False
    skip_section = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s:
            continue
        if s.startswith("#"):  # heading -> set section skip flag
            heading = s.lstrip("#").strip().lower()
            skip_section = heading in EPHEMERAL_HEADINGS
            continue
        if skip_section:
            continue
        # A candidate is a bullet item OR a top-level "**key**: value" line.
        # Strip the list marker first so the key/value placeholder filter also
        # applies to bulleted definitions (e.g. "- **x**: x").
        if _LIST_MARKER_RE.match(line):
            content = _LIST_MARKER_RE.sub("", line, count=1).strip()
        elif _BOLD_KV_RE.match(line):
            content = s
        else:
            continue  # plain prose / headings are not durable-fact candidates
        m = _BOLD_KV_RE.match(content)
        if m:
            k, v = m.group("k").strip(), m.group("v").strip()
            if normalize_text(k) == normalize_text(v):  # "x: x" placeholder noise
                continue
            content = f"{k}: {v}"
        if not content:
            continue
        out.append(content)
    return out


def extract_md(path: Path, root: Path, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(raw)
    if meta.get("memory") is False or _as_bool(meta.get("no_memory", False)):
        return []
    rel = str(path.relative_to(root)) if _is_relative(path, root) else path.name
    entries: List[Dict[str, str]] = []
    for cand in _candidate_lines(body):
        if not quality_ok(cand, cfg["min_len"], cfg["max_len"]):
            continue
        entries.append({
            "text": cand,
            "category": detect_category(cand, meta, rel),
            "source": f"{cfg['source_prefix']}:{rel}",
        })
    return entries


def extract_jsonl(path: Path, root: Path, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    rel = str(path.relative_to(root)) if _is_relative(path, root) else path.name
    out: List[Dict[str, str]] = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        texts: List[str] = []
        if isinstance(obj, dict) and obj.get("type") == "entity":
            name = str(obj.get("name", "")).strip()
            for ob in obj.get("observations", []) or []:
                ob = str(ob).strip()
                if not ob:
                    continue
                texts.append(ob if name.lower() in ob.lower() else f"{name}: {ob}")
        elif isinstance(obj, dict) and obj.get("type") == "relation":
            a, r, b = obj.get("from"), obj.get("relationType"), obj.get("to")
            if a and r and b:
                texts.append(f"{a} {r} {b}")
        elif isinstance(obj, dict) and obj.get("text"):
            texts.append(str(obj["text"]))
        for t in texts:
            if quality_ok(t, cfg["min_len"], cfg["max_len"]):
                out.append({
                    "text": t,
                    "category": detect_category(t, {}, rel),
                    "source": f"{cfg['source_prefix']}:{rel}",
                })
    return out


def _is_relative(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def iter_source_files(dirs: List[str], include_dated: bool):
    for d in dirs:
        root = Path(d).expanduser()
        if not root.exists():
            yield ("missing", root, None)
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part.startswith(".") for part in p.relative_to(root).parts):
                continue  # skip .obsidian, .git, dotfiles
            if p.suffix.lower() not in {".md", ".jsonl"}:
                continue
            if not include_dated and EPHEMERAL_FILENAME_RE.match(p.stem):
                yield ("skipped_dated", root, p)
                continue
            yield ("ok", root, p)


# --------------------------------------------------------------------------- #
# State (cross-run dedup)
# --------------------------------------------------------------------------- #
def load_state() -> Dict[str, Any]:
    sp = HERE / ".sync_state.json"
    if sp.exists():
        try:
            return json.loads(sp.read_text())
        except Exception:
            return {"hashes": {}}
    return {"hashes": {}}


def save_state(state: Dict[str, Any]) -> None:
    (HERE / ".sync_state.json").write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session(cfg: Dict[str, Any]):
    """Return an authenticated requests.Session.

    Two modes:
      - internal-token (loopback only): no password; sends
        X-Odysseus-Internal-Token + X-Odysseus-Owner on every request so the
        server attributes writes to `owner`. The server only honors this for
        127.0.0.1/::1 callers.
      - password login: POST /api/auth/login -> cookie session.
    """
    if requests is None:
        sys.exit("The 'requests' library is required to commit (pip install requests).")
    token = cfg.get("internal_token") or os.environ.get("ODYSSEUS_INTERNAL_TOKEN", "")
    if token:
        s = requests.Session()
        s.headers.update({
            "X-Odysseus-Internal-Token": token,
            "X-Odysseus-Owner": cfg["owner"],
            # Suppress the server-side "memory_added" event so a bulk import
            # never trips the Memory Tidy / consolidate_memory auto-task.
            "X-Odysseus-Bulk-Import": "1",
        })
        print(f"  auth: internal-token (loopback impersonation as '{cfg['owner']}')")
        return s
    pw = cfg["password"] or os.environ.get("ODYSSEUS_PASSWORD", "")
    if not pw:
        sys.exit("No credentials. Set env ODYSSEUS_INTERNAL_TOKEN (loopback) "
                 "or put a password in config.yaml / env ODYSSEUS_PASSWORD.")
    s = requests.Session()
    r = s.post(f"{cfg['odysseus_url']}/api/auth/login",
               json={"username": cfg["username"], "password": pw, "remember": True},
               timeout=15)
    if r.status_code == 429:
        sys.exit("Login rate-limited (429). Wait a minute and retry.")
    if r.status_code != 200 or not r.json().get("ok"):
        sys.exit(f"Login failed ({r.status_code}): {r.text[:200]}")
    s.headers["X-Odysseus-Bulk-Import"] = "1"  # see internal-token branch
    print(f"  auth: password login as '{cfg['username']}'")
    return s


def post_memory(s, cfg: Dict[str, Any], entry: Dict[str, str]) -> Tuple[bool, str]:
    # /api/memory/add types its body as a Pydantic model -> send JSON, not form.
    r = s.post(f"{cfg['odysseus_url']}/api/memory/add",
               json={"text": entry["text"], "category": entry["category"],
                     "source": entry["source"]},
               timeout=20)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    body = r.json()
    if body.get("message") == "Memory already exists":
        return True, "server-dup"
    return bool(body.get("ok")), "ok"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="One-way Obsidian -> Odysseus memory sync")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--commit", action="store_true", help="actually import (DRY_RUN=false)")
    ap.add_argument("--dry-run", action="store_true", help="force dry run")
    ap.add_argument("--max", type=int, help="override MAX_MEMORY_ENTRIES_PER_RUN")
    ap.add_argument("--from-artifact", help="import entries directly from a "
                    "previously generated out/memory_import_*.json (skips vault scan)")
    args = ap.parse_args()

    cfg = load_config(args.config, args)
    if not args.from_artifact and not cfg["memory_source_dirs"]:
        sys.exit("No MEMORY_SOURCE_DIRS configured. Edit config.yaml (copy config.example.yaml).")

    state = load_state()
    seen_hashes = set(state.get("hashes", {}).keys())

    files_scanned = 0
    files_skipped_dated = 0
    files_missing: List[str] = []
    raw_candidates = 0
    run_seen: set = set()
    new_entries: List[Dict[str, str]] = []
    dup_cross_run = 0
    dup_in_run = 0

    def _ingest(it: Dict[str, str]) -> None:
        nonlocal raw_candidates, dup_cross_run, dup_in_run
        raw_candidates += 1
        h = text_hash(it["text"])
        if h in seen_hashes:
            dup_cross_run += 1
            return
        if h in run_seen:
            dup_in_run += 1
            return
        run_seen.add(h)
        e = dict(it)
        e["owner"] = cfg["owner"]
        e["_hash"] = h
        new_entries.append(e)

    if args.from_artifact:
        ap_path = Path(args.from_artifact)
        if not ap_path.exists():
            sys.exit(f"Artifact not found: {ap_path}")
        loaded = json.loads(ap_path.read_text())
        if not isinstance(loaded, list):
            sys.exit("Artifact must be a JSON list of {text,category,source,owner}.")
        files_scanned = 1
        for it in loaded:
            if isinstance(it, dict) and str(it.get("text", "")).strip():
                _ingest({"text": it["text"],
                         "category": it.get("category", "fact"),
                         "source": it.get("source", f"{cfg['source_prefix']}:artifact")})
        print(f"  source: artifact {ap_path.name} ({len(loaded)} entries)")
    else:
        for status, root, p in iter_source_files(cfg["memory_source_dirs"], cfg["include_dated"]):
            if status == "missing":
                files_missing.append(str(root))
                continue
            if status == "skipped_dated":
                files_skipped_dated += 1
                continue
            files_scanned += 1
            items = (extract_jsonl(p, root, cfg) if p.suffix.lower() == ".jsonl"
                     else extract_md(p, root, cfg))
            for it in items:
                _ingest(it)

    # Apply per-run cap
    cap = int(cfg["max_memory_entries_per_run"])
    capped = max(0, len(new_entries) - cap)
    to_import = new_entries[:cap]

    # category histogram
    cats: Dict[str, int] = {}
    for e in to_import:
        cats[e["category"]] = cats.get(e["category"], 0) + 1

    # artifact (import-compatible shape, no internal fields)
    artifact = [{"text": e["text"], "category": e["category"],
                 "source": e["source"], "owner": e["owner"]} for e in to_import]
    est_bytes = len(json.dumps(artifact, ensure_ascii=False).encode("utf-8"))

    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_path = out_dir / f"memory_import_{stamp}.json"
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))

    # RAG manifest (NEVER imported to memory — separate Documents/RAG concern)
    rag_files = 0
    rag_bytes = 0
    rag_manifest: List[Dict[str, Any]] = []
    for d in cfg["rag_source_dirs"]:
        root = Path(d).expanduser()
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and not any(x.startswith(".") for x in p.relative_to(root).parts):
                rag_files += 1
                sz = p.stat().st_size
                rag_bytes += sz
                rag_manifest.append({"path": str(p), "bytes": sz})
    if rag_manifest:
        (out_dir / "rag_manifest.json").write_text(json.dumps(rag_manifest, indent=2))

    # ---- Report ----
    mode = "DRY RUN" if cfg["dry_run"] else "COMMIT"
    print("=" * 64)
    print(f"  Obsidian -> Odysseus memory sync   [{mode}]")
    print("=" * 64)
    if files_missing:
        print(f"  ! missing source dirs: {files_missing}")
    print(f"  files scanned ............. {files_scanned}")
    print(f"  files skipped (dated) ..... {files_skipped_dated}")
    print(f"  raw candidates ............ {raw_candidates}")
    print(f"  duplicates skipped ........ {dup_cross_run + dup_in_run} "
          f"(cross-run {dup_cross_run}, in-run {dup_in_run})")
    print(f"  entries created ........... {len(to_import)}")
    if capped:
        print(f"  ! capped (MAX={cap}) ...... {capped} entries deferred to next run")
    print(f"  categories detected ....... {dict(sorted(cats.items()))}")
    print(f"  estimated import size ..... {est_bytes:,} bytes")
    print(f"  artifact .................. {artifact_path}")
    if rag_files:
        print(f"  RAG (manifest only) ....... {rag_files} files, {rag_bytes:,} bytes "
              f"-> out/rag_manifest.json (NOT imported to memory)")
    print("-" * 64)
    if to_import[:5]:
        print("  sample entries:")
        for e in to_import[:5]:
            print(f"   - [{e['category']}] {e['text'][:80]}")
    print("=" * 64)

    if cfg["dry_run"]:
        print("DRY RUN — nothing sent. Re-run with --commit to import.")
        return 0

    if not to_import:
        print("Nothing new to import.")
        return 0

    # ---- Commit ----
    s = make_session(cfg)
    ok = 0
    failed = 0
    for e in to_import:
        success, note = post_memory(s, cfg, e)
        if success:
            ok += 1
            state.setdefault("hashes", {})[e["_hash"]] = {
                "source": e["source"], "ts": int(time.time()), "note": note}
        else:
            failed += 1
            print(f"  FAIL: {note} :: {e['text'][:60]}")
    save_state(state)
    print(f"committed: {ok} ok, {failed} failed. state updated.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
