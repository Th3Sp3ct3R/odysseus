# Reel Pipeline + GeeLark Posting — Plan

**Status:** Planning only (no implementation). Branch `feat/reel-pipeline`.
**Date:** 2026-06-04
**Goal:** End-to-end pipeline — research → creative briefs → Suno music + TTS + Kling video → stitched reel + frequency bed → **post to TikTok & Instagram via GeeLark cloud phones**. The "Taxonomy of Love" is the first new content vertical poured through this pipe (the existing Angel/Demon Codex is proof the pipe works).

---

## 1. Current state (grounded)

### Works today (the Codex pipeline, mostly in `~/.hermes/production/`)
- **30 characters** shipped (12 archangels + 18 fallen), `out/{archangels|fallen}/{slug}/{portrait.png,loop.mp4}`, tracked in `~/.hermes/out/manifest.csv` (all `ready`).
- **38 storyboard JSONs** in `~/.hermes/production/storyboards/`; current schema is richer than the 4-scene template — uses `narrative_mode` (e.g. `solomonic_interview`), `display_name`, per-scene `prompt_for_kling`, `text_overlay{text,style,position,animation,timing}`, `narration`, `demon_dialogue`, `camera_notes`.
- **~1.1 GB finished reels** in `~/.hermes/production/edits/` — Kling→stitch→TTS has run many times.
- **Real renderers** in `~/.hermes/production/tools/`: `reel_render_v2.py` (ffmpeg, camera motion, text PNGs), `auto_stitcher.py` (frequency-aware storyboard gen), `parler_tts_reel.py` (Parler TTS + pitch shift), `audio_layer.py` (Suno bridge), Kling clients (`vmos_kling_client.py`, `fal_kling.py`, `kling-mcp-server.py`).
- **Frequency registry** `~/.hermes/production/frequency_registry/stacks.json` — 4 brainwave stacks (theta/beta/alpha/gamma), carrier map includes 396/417/432/528/639/741/852/963; binaural + isochronic post-prod at −18 dB via `binaural_post_prod.py`.
- **Suno** = browser-driven CDP (`src/suno_generate.py` + `scripts/suno/suno_generate.js`), dry-run by default, downloads mp3 from in-page feed.

### Works today (Odysseus repo)
- **`DeepResearcherV2`** (`src/deep_research_v2.py`) — claims-centric research loop (plan → classify → think/search/extract/synthesize/decide → report), deduped claims w/ confidence + contradiction detection. Fully wired over HTTP (`routes/research_routes.py`), passing integration test. **Domain-generic** — no love/song-character specialization.
- **`GeeLark` client + RPA skill** at `~/.hermes/skills/devops/awesome-geelark-skill/` — client, phone_manager (auto-close context mgr), boot helper, doctor; `assets/config.json` has `appId` set, `apiKey` present, `token` empty. Live `geelark` MCP server in-session (`geelark_phone_list/start/stop/status`, `app_list`, `screenshot`, `wallet`).

### Gaps (what "testing the pipeline" will hit)
| Gap | Detail |
|---|---|
| **`tools/orchestrate_reel.py` is all dry-run** | Research stage is a stub; Kling stage writes a manifest but makes no call; stitch writes an ffmpeg plan but doesn't run; `"posting": False` hardcoded. Real renderers in `~/.hermes/` are never invoked. |
| **Research → storyboard handoff** | `DeepResearcherV2` emits markdown; nothing maps it into storyboard scene fields. |
| **No posting code in Odysseus** | GeeLark integration lives only as a `~/.hermes` skill. Not callable from the app. |
| **TikTok has no publish RPA** | GeeLark RPA covers IG (`instagramPubReels`) but **not** TikTok publish — only login/follow/like/comment/edit/del/hide. TikTok upload must be custom uiautomator2 UI automation. |
| **File delivery to cloud phone** | Unverified how the reel mp4 reaches the phone for `instagramPubReels` `video:[path]` (GeeLark upload endpoint? URL fetch? ADB push to on-device path?). Must confirm before posting works. |
| **Love vertical** | Zero assets. 528/639 Hz defined in registry but assigned to no stack. |

---

## 2. Target architecture (the full pipe)

```
                    ┌─────────────────────────────────────────────────────────┐
                    │  CONTENT SPEC (per song-character / love-type)           │
                    │  - figure, love thesis, archetype, frequency, sub-theme  │
                    └─────────────────────────────────────────────────────────┘
                                          │
         (Phase 1 research)               ▼
   DeepResearcherV2 ───────────►  research dossier (claims + report)
                                          │
         (NEW: enrichment)                ▼
   research → creative briefs ──►  song brief, cover-art prompt, storyboard scenes,
   (structured extractor v2)        TTS voice desc, symbol, frequency profile
                                          │
            ┌─────────────────────────────┼─────────────────────────────┐
            ▼                             ▼                              ▼
      Suno (CDP)                  Kling video (per scene)         Parler/Minimax TTS
      song mp3                    scene clips                     narration + dialogue
            └─────────────────────────────┼─────────────────────────────┘
                                          ▼
                          reel_render_v2.py (ffmpeg stitch + overlays)
                                          ▼
                          binaural_post_prod.py (frequency bed, −18 dB)
                                          ▼
                                  finished reel mp4  ──► out/love/{slug}/reel.mp4
                                          │
         (NEW: posting last-mile)         ▼
   GeeLark: pick phone → ensure app → deliver file → publish/schedule
            ├── Instagram: instagramPubReels (built-in RPA)
            └── TikTok:    custom uiautomator2 upload flow (no RPA)
                                          ▼
                          posting record + status poll + manifest update
```

**Where new code lives (per CLAUDE.md):**
- `services/posting/` — self-contained GeeLark posting subsystem (client, RPA wrappers, TikTok uiautomator2 flow, file delivery). Port the proven auth + RPA patterns from the `~/.hermes` skill; do not depend on the skill at runtime.
- `src/reel_orchestrator.py` (or evolve `tools/orchestrate_reel.py`) — promote stubs to real stage calls into `~/.hermes/production/tools/*` renderers.
- `src/creative_briefs.py` — research → briefs/storyboard enrichment (Phase 2 logic).
- `routes/posting_routes.py` — `setup_posting_routes(app, ...)`; expose post/schedule/status. Wire in `app.py`.
- `tests/test_posting.py`, `tests/test_creative_briefs.py`.

---

## 3. Posting last-mile via GeeLark (detailed)

**API basics:** `https://openapi.geelark.com`, all `POST`. Auth via Bearer token (recommended) or `appId`+SHA256 `sign`. Limits: 200/min, 24000/hr; `40014` = **2-hour lock** on overage — must rate-limit and back off. Response envelope `{code, msg, data}`, `code:0` = success.

**Phone lifecycle (from skill `boot_and_connect`):**
1. `wallet()` balance check first.
2. List/create phone (`/open/v1/phone/addNew`, `mobileType`).
3. Boot + enable ADB; `u2.connect(ip:port)` then **immediately `glogin pwd`** (or all ops hang).
4. Install app by `appVersionId` (from `/open/v1/app/installable/list`) — **never by name**.
5. Run RPA or drive uiautomator2.
6. Auto-close via `PhoneManager` context manager.

**Instagram (supported path):**
```
instagramLogin  {id, account, password, scheduleAt, name}   # session warmup
instagramPubReels {id, description, video:[<path>], scheduleAt, name}
```
`scheduleAt` (unix seconds) is required → gives native scheduling for free.

**TikTok (must build):** No publish RPA. Options, in order of preference:
- **A. uiautomator2 UI automation** — push mp4 to phone, open TikTok, drive upload → caption → post. Most control, most brittle (UI changes). Mirror GeeLark's RandomComment/Edit task patterns.
- **B. Confirm with GeeLark** whether a newer `tiktokPublish`/generic upload task exists (API may be ahead of the vendored docs).
- **C. Cross-post** — publish to IG Reels via RPA, accept TikTok as fast-follow once A is built.

**Open integration question — file delivery:** Determine how `video:[path]` is resolved for `instagramPubReels` (GeeLark upload endpoint vs URL fetch vs on-device path after ADB push). This gates all posting. Verify in a sandbox phone before wiring orchestrator.

**Account model:** which IG/TikTok accounts map to which content vertical (one love-brand account? per-archetype?), proxy per phone, and warmup cadence (`instagramWarmup`) — needs your input (see §6).

---

## 4. The "Taxonomy of Love" vertical mapping

The 7-type × Solfeggio × Hermetic mapping from research drops straight into existing formats:

| # | Type | Freq | Sub-theme |
|---|---|---|---|
| 1 | Eros | 417 Hz | Dionysian / Zohar |
| 2 | Philia | 639 Hz | Pythagorean / Hermetic Mentalism |
| 3 | Storge | 396 Hz | Stoicism / Chesed |
| 4 | Agape | 528 Hz | Eckhart / Tiferet |
| 5 | Ludus | 741 Hz | Ovid / Trickster Hermes |
| 6 | Pragma | 852 Hz | Aquinas / Gevurah |
| 7 | Philautia | 963 Hz | Jung / Magnum Opus |

**Asset slots to create (mirroring the Codex):**
- `frequency_registry/stacks.json` → add `love_*` stacks (carrier + binaural target per type).
- `production/storyboards/{love_slug}_reel_01.json` → **current rich schema** (not the old 4-field one): `narrative_mode`, `display_name`, scenes with `prompt_for_kling`, `text_overlay`, `narration`, `dialogue`, `camera_notes`.
- `out/love/{slug}/{portrait.png,reel.mp4}` + `manifest.csv` rows.
- Song brief + cover-art prompt + symbol per type (dark hermetic series cohesion).
- TTS voice descriptions per type (extend `VOICE_DESCRIPTIONS`).

**Note:** The deeper "Love Counselor" model (Five Faces, archetypes, tensions, stages) is a *conversational* spec — it belongs in the Odysseus chat/research layer, not the reel renderer. Keep it as a separate workstream; the reel pipeline only needs the per-type creative briefs.

---

## 5. Phased plan

**Phase 0 — De-risk the unknowns (no content yet)**
- 0.1 Verify GeeLark auth from Odysseus (token vs key+sign), `wallet()`, `phone_list` round-trip.
- 0.2 Sandbox: boot one phone, install IG by `appVersionId`, resolve the **file-delivery** question, do one `instagramPubReels` of a throwaway mp4 end to end.
- 0.3 Spike TikTok upload via uiautomator2 on the same phone (or confirm no RPA exists).
- **Exit:** one real IG post + a documented TikTok approach.

**Phase 1 — Posting subsystem in Odysseus**
- `services/posting/geelark.py` (ported client: auth, rate-limit/backoff, phone lifecycle, RPA wrappers).
- `services/posting/tiktok_ui.py` (uiautomator2 upload flow).
- `services/posting/service.py` (post(reel, caption, platforms, schedule) → records + status).
- `routes/posting_routes.py` + `app.py` wiring + `tests/test_posting.py` (mock GeeLark HTTP).
- **Exit:** `POST /api/posting/publish` posts a given mp4 to IG (+TikTok) via GeeLark.

**Phase 2 — Make `orchestrate_reel.py` real (one existing character)**
- Replace stubs: call `DeepResearcherV2`, real Kling client, `reel_render_v2.py`, `binaural_post_prod.py`. Keep an `execute` flag (dry-run stays default).
- Add a posting stage that calls the Phase 1 service.
- **Exit:** one existing Codex character goes prompt → posted reel in one command.

**Phase 3 — Research → creative briefs**
- `src/creative_briefs.py`: dossier → song brief + storyboard scenes + cover prompt + TTS desc + frequency profile (structured-output extractor, like `structured_extractor.py`).
- **Exit:** given a figure, auto-emit a valid storyboard JSON + Suno brief.

**Phase 4 — Love vertical content**
- Add `love_*` frequency stacks; generate 7 storyboards + briefs + portraits; populate `out/love/` + manifest.
- Run 1–2 love types through the full Phase 2 pipe → posted.
- **Exit:** first love-type reel live on IG/TikTok.

**Phase 5 — Scale & schedule**
- Batch orchestration across phones (respect 200/min), warmup cadence, scheduling via `scheduleAt`, posting analytics back into Odysseus.

---

## 6. Decisions

### Locked
- **Integration shape (2026-06-04):** Odysseus talks to GeeLark via a **plain async REST client + one internal route** — `services/posting/geelark.py` + `routes/posting_routes.py` (`POST /api/posting/publish`). **No MCP in the runtime posting path.** GeeLark's own REST API is consumed directly; the in-session `geelark` MCP is for interactive dev/debug only. An MCP wrapper around the posting service stays an optional *later* add-on if agent-driven posting is wanted.
- **Vendor vs reuse client (2026-06-04):** Port the proven auth + RPA patterns from `~/.hermes/skills/devops/awesome-geelark-skill/scripts/geelark_client.py` into `services/posting/geelark.py` so the app has **no runtime dependency** on the skill.

### Still open (need input before/while building)
1. **Accounts & phones:** one love-brand account, or per-archetype accounts? How many GeeLark phones, proxies, and warmup policy?
2. **TikTok approach:** invest in uiautomator2 upload now (Phase 0.3), or ship IG-first and fast-follow TikTok?
3. **Where the orchestrator lives:** evolve `tools/orchestrate_reel.py` in place, or promote to `src/reel_orchestrator.py` with a thin CLI wrapper?
4. **Captions/hashtags:** generated per reel by the briefs layer, or hand-authored per post?

---

## 7. Risks
- **GeeLark UI brittleness** (TikTok especially) — UI automation breaks on app updates; needs screenshot-based smoke checks (`geelark_screenshot`).
- **Rate-limit lockout** (`40014` = 2 hr) — central rate limiter mandatory.
- **Account bans** — warmup + proxy hygiene + human-like cadence before volume.
- **Suno CDP fragility** — session cookies expire; generation is obfuscated/non-API.
- **Cost** — Kling + Suno + GeeLark phone-time per reel; keep dry-run default and a cost estimate per run.
