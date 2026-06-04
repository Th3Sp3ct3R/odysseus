"""
suno_generate.py — browser-drive Suno music generation for the production pipeline.

Why browser-drive: Suno routes song *generation* through an obfuscated, rotating
service-worker proxy, so it can't be replayed as a clean API call (unlike the
read/social endpoints the engagement bot uses). The robust, non-circumventing
path is to automate a real logged-in browser — click "Create" like a user — then
download the finished MP3 from Suno's public CDN.

Architecture:
    generate (browser)            poll + read           download
    scripts/suno/suno_generate.js  → /api/feed (in-page) → cdn1.suno.ai/{id}.mp3
    (Node + puppeteer-core, CDP)                            (this module, httpx)

Dry-run is the DEFAULT and does nothing external (no browser, no generation, no
download) — it just prints the plan. Use --execute to actually generate (paid:
consumes Suno credits and creates clips on the logged-in account).

No manual login: --execute launches its OWN dedicated, isolated headless Chrome
(separate profile dir) and injects the engagement bot's persisted Suno session
cookies. Because it's a separate browser instance, it does NOT contend with /
lock the posting-flow browser. (Generation must run in a browser at all because
Suno proxies it through an obfuscated service worker — it can't be a plain curl
call like the engagement bot's read/social actions.)

CLI:
    python -m src.suno_generate --prompt "dark cinematic ambient, 528hz"          # dry-run
    python -m src.suno_generate --prompt "..." --execute --out-dir /tmp/suno      # real
    python -m src.suno_generate --prompt "..." --execute --headful               # show the browser

Library:
    from src.suno_generate import generate_song
    result = generate_song("dark cinematic ambient", instrumental=True, dry_run=False)
    result.audio_paths  # -> [Path(...mp3), ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_SCRIPT = REPO_ROOT / "scripts" / "suno" / "suno_generate.js"
DEFAULT_OUT_DIR = Path.home() / ".hermes" / "production" / "clips" / "audio"
DEFAULT_SESSION = (Path.home() / ".hermes" / "skills" / "devops"
                   / "suno-browser-automation" / "references" / "suno-session-persist.json")
DEFAULT_PROFILE = Path.home() / ".hermes" / "production" / ".suno-gen-profile"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_TIMEOUT_S = 300


@dataclass
class SunoClip:
    id: str
    title: str
    status: str
    audio_url: str
    duration: Optional[float] = None
    audio_path: Optional[Path] = None


@dataclass
class SunoResult:
    ok: bool
    dry_run: bool
    prompt: str
    instrumental: bool
    clips: list[SunoClip] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def audio_paths(self) -> list[Path]:
        return [c.audio_path for c in self.clips if c.audio_path]

    def to_dict(self) -> dict:
        """Stable contract for callers (e.g. the production audio_layer bridge)."""
        first = self.clips[0] if self.clips else None
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "prompt": self.prompt,
            "instrumental": self.instrumental,
            "error": self.error,
            # convenience fields (first clip)
            "title": first.title if first else None,
            "status": first.status if first else None,
            "duration": first.duration if first else None,
            "paths": [str(p) for p in self.audio_paths],
            # full per-clip detail
            "clips": [
                {"id": c.id, "title": c.title, "status": c.status,
                 "duration": c.duration, "path": str(c.audio_path) if c.audio_path else None}
                for c in self.clips
            ],
        }


def generate_song(
    prompt: str,
    *,
    instrumental: bool = True,
    dry_run: bool = True,
    out_dir: Path = DEFAULT_OUT_DIR,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    session_file: Path = DEFAULT_SESSION,
    user_data_dir: Path = DEFAULT_PROFILE,
    chrome_path: str = DEFAULT_CHROME,
    headless: bool = True,
    cdp_url: str = "",
    node_bin: str = "node",
) -> SunoResult:
    """Generate a Suno song via an isolated browser and download the MP3(s).

    Launches its own dedicated Chrome (own profile) and injects the persisted
    Suno session — no login, no contention with the posting-flow browser. If
    cdp_url is given, attaches to that running Chrome instead of launching.

    Dry-run (default) performs NO external action — it returns a planned result.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return SunoResult(False, dry_run, prompt, instrumental, error="empty prompt")

    if dry_run:
        logger.info("[suno] DRY RUN — would generate: %r (instrumental=%s)", prompt, instrumental)
        return SunoResult(True, True, prompt, instrumental)

    if not NODE_SCRIPT.exists():
        return SunoResult(False, False, prompt, instrumental,
                          error=f"node script missing: {NODE_SCRIPT}")

    node_args = {
        "prompt": prompt,
        "instrumental": instrumental,
        "dryRun": False,
        "timeoutMs": timeout_s * 1000,
        "sessionFile": str(session_file),
        "userDataDir": str(user_data_dir),
        "chromePath": chrome_path,
        "headless": headless,
    }
    if cdp_url:
        node_args["cdpUrl"] = cdp_url
    try:
        proc = subprocess.run(
            [node_bin, str(NODE_SCRIPT), json.dumps(node_args)],
            capture_output=True, text=True, timeout=timeout_s + 60,
        )
    except subprocess.TimeoutExpired:
        return SunoResult(False, False, prompt, instrumental, error="node process timed out")
    except FileNotFoundError:
        return SunoResult(False, False, prompt, instrumental,
                          error=f"'{node_bin}' not found — is Node installed?")

    payload = _last_json_line(proc.stdout)
    if payload is None:
        return SunoResult(False, False, prompt, instrumental,
                          error=f"no JSON from node. stderr tail: {proc.stderr[-400:]}")

    if not payload.get("ok"):
        return SunoResult(False, False, prompt, instrumental,
                          error=payload.get("error", "generation failed"))

    clips = [
        SunoClip(id=c.get("id", ""), title=c.get("title", ""),
                 status=c.get("status", ""), audio_url=c.get("audioUrl", ""),
                 duration=c.get("duration"))
        for c in payload.get("clips", [])
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        if clip.audio_url:
            clip.audio_path = _download(clip.audio_url, out_dir / f"{clip.id or 'suno'}.mp3")

    return SunoResult(True, False, prompt, instrumental, clips=clips)


def _last_json_line(stdout: str) -> Optional[dict]:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _download(url: str, dest: Path) -> Optional[Path]:
    """Download a CDN MP3 to dest. Suno audio lives on public cdn1.suno.ai."""
    try:
        import httpx
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        logger.info("[suno] downloaded %s", dest)
        return dest
    except Exception as e:  # noqa: BLE001 - download is best-effort; report and continue
        logger.warning("[suno] download failed for %s: %s", url, e)
        return None


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Browser-drive Suno music generation (dry-run by default).")
    p.add_argument("--prompt", required=True, help="Song description / style prompt.")
    p.add_argument("--instrumental", dest="instrumental", action="store_true", default=True,
                   help="Generate instrumental only (default).")
    p.add_argument("--no-instrumental", dest="instrumental", action="store_false",
                   help="Allow vocals.")
    p.add_argument("--execute", action="store_true",
                   help="Actually generate (paid: uses credits, creates clips). Default: dry-run.")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where to save MP3s.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="Generation timeout (s).")
    p.add_argument("--session-file", type=Path, default=DEFAULT_SESSION,
                   help="Persisted Suno session cookies to inject (no manual login).")
    p.add_argument("--user-data-dir", type=Path, default=DEFAULT_PROFILE,
                   help="Dedicated, isolated Chrome profile (kept separate from posting flows).")
    p.add_argument("--chrome-path", default=DEFAULT_CHROME, help="Path to the Chrome binary.")
    p.add_argument("--headful", dest="headless", action="store_false", default=True,
                   help="Show the browser window (default: headless).")
    p.add_argument("--cdp-url", default="",
                   help="Attach to an already-running Chrome instead of launching one.")
    p.add_argument("--json", action="store_true",
                   help="Emit a single JSON result line (stable contract for callers like audio_layer).")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.json else logging.INFO, format="%(message)s")
    dry_run = not args.execute

    if args.json:
        result = generate_song(
            args.prompt, instrumental=args.instrumental, dry_run=dry_run,
            out_dir=args.out_dir, timeout_s=args.timeout,
            session_file=args.session_file, user_data_dir=args.user_data_dir,
            chrome_path=args.chrome_path, headless=args.headless, cdp_url=args.cdp_url,
        )
        print(json.dumps(result.to_dict()))
        return 0 if result.ok else 1

    print("Suno music generation (browser-drive)")
    print(f"  mode ......... {'EXECUTE (paid)' if not dry_run else 'dry-run'}")
    print(f"  prompt ....... {args.prompt!r}")
    print(f"  instrumental . {args.instrumental}")
    if not dry_run:
        print(f"  browser ...... {'attach ' + args.cdp_url if args.cdp_url else ('launch ' + ('headless' if args.headless else 'headful'))}")
        print(f"  session ...... {args.session_file}")
        print(f"  profile ...... {args.user_data_dir}  (isolated from posting flows)")
        print(f"  out dir ...... {args.out_dir}")

    result = generate_song(
        args.prompt, instrumental=args.instrumental, dry_run=dry_run,
        out_dir=args.out_dir, timeout_s=args.timeout,
        session_file=args.session_file, user_data_dir=args.user_data_dir,
        chrome_path=args.chrome_path, headless=args.headless, cdp_url=args.cdp_url,
    )

    print("─" * 56)
    if result.dry_run:
        print("  DRY RUN — no browser, no generation, no download.")
        print("  Re-run with --execute to generate for real (launches its own")
        print("  isolated headless Chrome + injects the persisted Suno session).")
        print("─" * 56)
        return 0

    if not result.ok:
        print(f"  FAILED: {result.error}")
        print("─" * 56)
        return 1

    print(f"  Generated {len(result.clips)} clip(s):")
    for c in result.clips:
        loc = c.audio_path if c.audio_path else "(download failed)"
        print(f"    • {c.title or c.id} [{c.status}] → {loc}")
    print("─" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
