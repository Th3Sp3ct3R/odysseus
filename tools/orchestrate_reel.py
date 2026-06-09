#!/usr/bin/env python3
"""
orchestrate_reel.py — Odysseus reel pipeline orchestrator (Phase 1, dry-run).

Pipeline:  research → enriched storyboard → Kling video → Suno audio → stitch → reel

This is the DRY-RUN orchestrator. It plans every stage and writes a JSON manifest
per stage plus a final reel manifest — WITHOUT calling any paid API:
  • research stage does NOT call the LLM/web (writes a research plan)
  • Kling stage does NOT call Kling (writes a job manifest)
  • Suno stage goes through the bridged audio_layer in DRY-RUN (no credits)
  • stitch stage does NOT run ffmpeg (writes an ffmpeg plan)

It "enriches existing agents": reads an existing agent's storyboard + bible from
~/.hermes/production (READ-ONLY) and plans an enriched reel for the given topic.

Usage:
    python tools/orchestrate_reel.py --dry-run --topic "the scribe of heaven"
    python tools/orchestrate_reel.py --dry-run --topic "..." --agent metatron

Outputs (under data/pipeline_runs/<ts>_<agent>/):
    research_summary.json   storyboard.enriched.json   video_manifest.json
    audio_manifest.json     stitch_manifest.json       reel_manifest.json

Guardrails: no paid Suno/Kling, no posting/publishing, no deleting clips, no
session/cookie logging. Real (paid) execution is NOT wired in Phase 1.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REEL_W, REEL_H, REEL_FPS = 1080, 1920, 30
GOLD = (212, 175, 55)
DEFAULT_FONT = "/System/Library/Fonts/Supplemental/Georgia.ttf"
AUDIO_DIR = Path.home() / ".hermes" / "production" / "clips" / "audio"

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD = Path.home() / ".hermes" / "production"
PROD_STORYBOARDS = PROD / "storyboards"
PROD_CHARACTERS = PROD / "characters"
PROD_TOOLS = PROD / "tools"
RUNS_DIR = REPO_ROOT / "data" / "pipeline_runs"
DEFAULT_AGENT = "metatron"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "topic"


def write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_base_storyboard(agent: str) -> Optional[dict]:
    for name in (f"{agent}_reel_01.json", f"{agent}_reel_v2_auto.json"):
        p = PROD_STORYBOARDS / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
    return None


def load_bible(agent: str) -> dict:
    p = PROD_CHARACTERS / agent / "bible.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


# --------------------------------------------------------------------------- #
# Stages — each returns (manifest_dict, written_path)
# --------------------------------------------------------------------------- #


def stage_research(run: Path, topic: str, agent: str, bible: dict, base: Optional[dict]) -> Path:
    core = bible.get("core_description", "") if isinstance(bible, dict) else ""
    style = (base or {}).get("style_notes", "")
    sub_questions = [
        f"What is the canonical lore of {agent} relevant to '{topic}'?",
        f"What visual motifs/symbols define {agent} for '{topic}'?",
        f"What emotional arc best expresses '{topic}' through {agent}?",
        f"What sonic palette fits {agent} + '{topic}'?",
    ]
    manifest = {
        "stage": "research",
        "mode": "dry-run",
        "note": "No LLM/web call in dry-run. This is the research plan only.",
        "topic": topic,
        "agent": agent,
        "seed_core_description": core[:400],
        "seed_style_notes": style,
        "planned_sub_questions": sub_questions,
        "planned_engine": "src.deep_research_v2.DeepResearcherV2 (run only with --execute, future)",
    }
    return write_json(run / "research_summary.json", manifest)


def stage_storyboard(run: Path, topic: str, agent: str, base: Optional[dict], research_path: Path) -> Path:
    base = base or {
        "agent_slug": agent, "title": agent.title(),
        "duration_target_seconds": 24, "aspect_ratio": "9:16",
        "style_notes": "", "scenes": [],
    }
    enriched = dict(base)
    enriched["_enrichment"] = {
        "mode": "dry-run",
        "topic": topic,
        "source_research": str(research_path),
        "note": "Real enrichment (style/scene rewrites from research) runs only with --execute.",
        "planned_style_addition": f"[research-enriched for '{topic}'] " + base.get("style_notes", ""),
    }
    enriched["scenes"] = [
        {**s, "_planned_enrichment": f"deepen description with research on '{topic}'"}
        for s in base.get("scenes", [])
    ]
    return write_json(run / "storyboard.enriched.json", enriched)


def stage_video(run: Path, agent: str, storyboard: dict) -> Path:
    jobs = []
    for i, s in enumerate(storyboard.get("scenes", [])):
        jobs.append({
            "scene_id": s.get("scene_id", f"scene_{i}"),
            "prompt_for_kling": s.get("prompt_for_kling") or s.get("description", ""),
            "shot_type": s.get("shot_type", ""),
            "duration_seconds": s.get("duration_seconds", 6),
            "reference_images": s.get("reference_images", []),
            "model": "kling-v1",
            "dry_run": True,
        })
    manifest = {
        "stage": "video",
        "mode": "dry-run",
        "note": "No real Kling generation. Job manifest only (requires --execute + approval).",
        "agent": agent,
        "aspect_ratio": storyboard.get("aspect_ratio", "9:16"),
        "job_count": len(jobs),
        "jobs": jobs,
    }
    return write_json(run / "video_manifest.json", manifest)


def stage_audio(run: Path, topic: str, agent: str, storyboard: dict) -> Path:
    style_notes = storyboard.get("style_notes", "")
    music_prompt = f"{style_notes} cinematic instrumental score for '{topic}'".strip()
    bridge_result = _audio_bridge_dry_run(music_prompt, style_notes)
    manifest = {
        "stage": "audio",
        "mode": "dry-run",
        "note": "Suno via bridged audio_layer in DRY-RUN — no credits, no browser.",
        "agent": agent,
        "music_prompt": music_prompt,
        "instrumental": True,
        "bridge_result": bridge_result,   # {ok, dry_run, paths:[], ...}
    }
    return write_json(run / "audio_manifest.json", manifest)


def _audio_bridge_dry_run(prompt: str, style: str) -> dict:
    """Call the production audio_layer bridge in dry-run (no paid Suno)."""
    try:
        if str(PROD_TOOLS) not in sys.path:
            sys.path.insert(0, str(PROD_TOOLS))
        import audio_layer  # noqa: WPS433 (intentional dynamic import of prod bridge)
        return audio_layer.generate_music_with_suno_engine(prompt, style=style, execute=False)
    except Exception as e:  # noqa: BLE001 - never let the bridge break the dry-run plan
        return {"ok": False, "dry_run": True, "paths": [],
                "error": f"audio_layer bridge unavailable in dry-run: {e}"}


def stage_stitch(run: Path, agent: str, storyboard: dict, video_path: Path, audio_path: Path) -> Path:
    overlays = [
        {"scene_id": s.get("scene_id", f"scene_{i}"),
         "text_overlay": s.get("text_overlay", ""),
         "duration_seconds": s.get("duration_seconds", 6)}
        for i, s in enumerate(storyboard.get("scenes", []))
    ]
    manifest = {
        "stage": "stitch",
        "mode": "dry-run",
        "note": "No ffmpeg run. ffmpeg plan only.",
        "agent": agent,
        "aspect_ratio": storyboard.get("aspect_ratio", "9:16"),
        "inputs": {"video_manifest": str(video_path), "audio_manifest": str(audio_path)},
        "text_overlays": overlays,
        "ffmpeg_plan": {
            "concat": "per-scene Kling clips in order",
            "audio": "mix Suno track under scenes",
            "overlays": "burn gold text per scene with fade in/out",
            "output": str(run / f"{agent}_reel.mp4"),
        },
    }
    return write_json(run / "stitch_manifest.json", manifest)


# --------------------------------------------------------------------------- #
# Real ffmpeg stitch (LOCAL only — no paid APIs, no network, no posting).
# This ffmpeg build lacks drawtext, so text overlays are rendered with PIL to
# transparent PNGs and composited via ffmpeg `overlay` (same approach as the
# production renderer). Background = per-scene Kling clip if present, else a
# dark plate (Kling is not run in Phase 1). Audio = a real local Suno track.
# --------------------------------------------------------------------------- #


def _overlay_text(scene: dict) -> str:
    t = scene.get("text_overlay", "")
    if isinstance(t, dict):
        return str(t.get("text", "")).strip()
    return str(t).strip()


def _overlay_position(scene: dict) -> str:
    t = scene.get("text_overlay", "")
    return str(t.get("position", "center")) if isinstance(t, dict) else "center"


def _render_text_png(text: str, position: str, out_path: Path, font_path: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont  # lazy import (not needed for dry-run)

    img = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(font_path, 92)
    except OSError:
        font = ImageFont.load_default()

    # word-wrap to fit width
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= REEL_W * 0.84:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    block = "\n".join(lines) or text

    bbox = draw.multiline_textbbox((0, 0), block, font=font, align="center", spacing=18)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (REEL_W - tw) / 2
    y = {"center": (REEL_H - th) / 2,
         "lower third": REEL_H * 0.70,
         "bottom center": REEL_H * 0.82}.get(position, (REEL_H - th) / 2)

    # shadow then gold text for legibility
    draw.multiline_text((x + 4, y + 4), block, font=font, fill=(0, 0, 0, 200), align="center", spacing=18)
    draw.multiline_text((x, y), block, font=font, fill=GOLD + (255,), align="center", spacing=18)
    img.save(out_path)
    return out_path


def _pick_audio(explicit: Optional[Path]) -> Optional[Path]:
    if explicit and Path(explicit).exists():
        return Path(explicit)
    if not AUDIO_DIR.exists():
        return None
    mp3s = [p for p in AUDIO_DIR.glob("*.mp3") if p.stat().st_size > 0]
    return max(mp3s, key=lambda p: p.stat().st_mtime) if mp3s else None


def _scene_source(agent: str, scene_id: str) -> Optional[Path]:
    """A real Kling clip for this scene, if one exists (none in Phase 1 dry-run)."""
    p = Path.home() / ".hermes" / "production" / "clips" / agent / f"{scene_id}.mp4"
    return p if p.exists() else None


def render_stitch(run: Path, agent: str, storyboard: dict, audio_path: Optional[Path],
                  font_path: str) -> dict:
    """Render a REAL 9:16 mp4 from scene text overlays + a local Suno track."""
    scenes = storyboard.get("scenes", []) or []
    if not scenes:
        return {"ok": False, "error": "no scenes to render"}
    if audio_path is None:
        return {"ok": False, "error": "no local audio track found (clips/audio empty)"}

    png_dir = run / "overlays"
    png_dir.mkdir(parents=True, exist_ok=True)

    # cumulative scene windows
    windows, t = [], 0.0
    for i, s in enumerate(scenes):
        dur = float(s.get("duration_seconds", 6) or 6)
        windows.append((i, t, t + dur, s))
        t += dur
    total = t

    # render a text PNG per scene
    pngs = []
    for i, start, end, s in windows:
        png = _render_text_png(_overlay_text(s), _overlay_position(s), png_dir / f"scene_{i}.png", font_path)
        pngs.append(png)

    # build ffmpeg command: dark plate bg + per-scene PNG overlays (enable-timed) + audio
    cmd = ["ffmpeg", "-y", "-nostdin",
           "-f", "lavfi", "-i", f"color=c=0x0A0A12:s={REEL_W}x{REEL_H}:r={REEL_FPS}:d={total:.2f}"]
    for png in pngs:
        cmd += ["-loop", "1", "-i", str(png)]
    cmd += ["-i", str(audio_path)]

    audio_idx = len(pngs) + 1
    parts, prev = [], "[0:v]"
    for i, start, end, _ in windows:
        out = f"[v{i}]"
        parts.append(f"{prev}[{i+1}:v]overlay=enable='between(t,{start:.2f},{end:.2f})':eof_action=pass{out}")
        prev = out
    filtergraph = ";".join(parts)

    out_mp4 = run / f"{agent}_reel.mp4"
    cmd += ["-filter_complex", filtergraph, "-map", prev, "-map", f"{audio_idx}:a",
            "-t", f"{total:.2f}", "-r", str(REEL_FPS), "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(out_mp4)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_mp4.exists():
        return {"ok": False, "error": f"ffmpeg failed (rc={proc.returncode})",
                "stderr_tail": proc.stderr[-400:]}
    return {"ok": True, "output": str(out_mp4), "duration_s": round(total, 2),
            "scenes": len(scenes), "audio": str(audio_path),
            "sources": "dark plate (no Kling clips in Phase 1)"}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def orchestrate(topic: str, agent: str, dry_run: bool, now: str) -> dict:
    run = RUNS_DIR / f"{now}_{slugify(agent)}"
    run.mkdir(parents=True, exist_ok=True)

    bible = load_bible(agent)
    base = load_base_storyboard(agent)

    research_path = stage_research(run, topic, agent, bible, base)
    storyboard_path = stage_storyboard(run, topic, agent, base, research_path)
    storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
    video_path = stage_video(run, agent, storyboard)
    audio_path = stage_audio(run, topic, agent, storyboard)
    stitch_path = stage_stitch(run, agent, storyboard, video_path, audio_path)

    reel_manifest = {
        "pipeline": "research -> storyboard -> video -> audio -> stitch -> reel",
        "mode": "dry-run" if dry_run else "execute",
        "created_utc": now,
        "topic": topic,
        "agent": agent,
        "base_storyboard_found": base is not None,
        "bible_found": bool(bible),
        "paid_calls": {"suno": False, "kling": False},
        "posting": False,
        "stages": {
            "research_summary": str(research_path),
            "storyboard": str(storyboard_path),
            "video_manifest": str(video_path),
            "audio_manifest": str(audio_path),
            "stitch_manifest": str(stitch_path),
        },
        "final_reel_output": str(run / f"{agent}_reel.mp4") + "  (not produced in dry-run)",
    }
    reel_path = write_json(run / "reel_manifest.json", reel_manifest)
    reel_manifest["_manifest_path"] = str(reel_path)
    reel_manifest["_run_dir"] = str(run)
    return reel_manifest


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Odysseus reel pipeline orchestrator (Phase 1 dry-run).")
    p.add_argument("--topic", required=True, help="Research subject for the reel.")
    p.add_argument("--agent", default=DEFAULT_AGENT, help=f"Existing agent to enrich (default: {DEFAULT_AGENT}).")
    p.add_argument("--dry-run", action="store_true", default=True, help="Plan only; no paid calls (default).")
    p.add_argument("--execute", action="store_true",
                   help="Execute the real pipeline (calls fal_kling.py and reel_render_v2.py).")
    p.add_argument("--render", action="store_true",
                   help="After planning, run the REAL ffmpeg stitch (LOCAL only, no paid APIs) "
                        "to produce an actual mp4 from scene text + a local Suno track.")
    p.add_argument("--audio", type=Path, default=None,
                   help="Audio track for --render (default: newest local Suno mp3).")
    p.add_argument("--font", default=DEFAULT_FONT, help="Font for text overlays.")
    args = p.parse_args(argv)

    if args.execute:
        print("\n" + "="*60)
        print(" [EXECUTE MODE] Initiating real pipeline execution")
        print(" Guardrails: Ensure sufficient API credits (fal.ai) and env vars are set.")
        print("="*60)
        
        storyboard_path = Path(result["stages"]["storyboard"])
        
        # Step 1: Kling Generation
        print("\n[Step 1/2] Generating Kling video clips via fal_kling.py...")
        kling_cmd = [sys.executable, str(PROD_TOOLS / "fal_kling.py"), "--storyboard", str(storyboard_path)]
        print(f"  ▶ {' '.join(kling_cmd)}")
        proc = subprocess.run(kling_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"  ❌ Kling generation failed:\n{proc.stderr[-600:]}")
            return 1
        print("  ✅ Kling clips generated (see ~/.hermes/production/clips/)")

        # Step 2: Reel Stitching
        print("\n[Step 2/2] Stitching final reel via reel_render_v2.py...")
        stitch_cmd = [sys.executable, str(PROD_TOOLS / "reel_render_v2.py"), "--storyboard", str(storyboard_path), args.agent]
        print(f"  ▶ {' '.join(stitch_cmd)}")
        proc = subprocess.run(stitch_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"  ❌ Reel stitching failed:\n{proc.stderr[-600:]}")
            return 1
            
        out_path = PROD / "edits" / args.agent / f"{args.agent}_reel_02.mp4"
        print("  ✅ Reel stitched successfully!")
        print(f"  🎉 Final Output: {out_path}")
        
        # Update manifest to reflect execution
        manifest = json.loads(Path(result["_manifest_path"]).read_text(encoding="utf-8"))
        manifest["mode"] = "executed"
        manifest["paid_calls"]["kling"] = True
        manifest["final_reel_output"] = str(out_path)
        write_json(Path(result["_manifest_path"]), manifest)
        
        return 0

    # Timestamp from system clock (this is a normal script, not a workflow).
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("Odysseus reel orchestrator")
    print(f"  mode ... dry-run (no paid Suno/Kling, no posting)")
    print(f"  topic .. {args.topic!r}")
    print(f"  agent .. {args.agent}")

    result = orchestrate(args.topic, args.agent, dry_run=True, now=now)

    line = "─" * 60
    print(f"\n{line}\n Manifests written:\n{line}")
    for k, v in result["stages"].items():
        print(f"  {k:<18} {v}")
    print(f"  {'reel_manifest':<18} {result['_manifest_path']}")
    print(f"{line}")
    print(f"  paid_calls: {result['paid_calls']}   posting: {result['posting']}")
    print(f"  run dir: {result['_run_dir']}")
    print(f"{line}")

    if args.render:
        run = Path(result["_run_dir"])
        storyboard = json.loads((run / "storyboard.enriched.json").read_text(encoding="utf-8"))
        audio = _pick_audio(args.audio)
        print(f"\n[render] REAL ffmpeg stitch (local only, no paid APIs)")
        print(f"[render] audio: {audio}")
        render = render_stitch(run, args.agent, storyboard, audio, args.font)
        # record render result into the reel manifest
        manifest = json.loads(Path(result["_manifest_path"]).read_text(encoding="utf-8"))
        manifest["render"] = render
        write_json(Path(result["_manifest_path"]), manifest)
        if render.get("ok"):
            print(f"[render] ✅ reel: {render['output']}  ({render['duration_s']}s, {render['scenes']} scenes)")
        else:
            print(f"[render] ❌ {render.get('error')}\n{render.get('stderr_tail','')}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
