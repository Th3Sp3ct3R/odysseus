#!/usr/bin/env node
/**
 * suno_generate.js — browser-drive Suno song generation (CDP).
 *
 * Suno proxies song *generation* through an obfuscated, rotating service-worker
 * path, so the generate request cannot be replayed as a clean API call. The
 * reliable, non-circumventing approach is to automate a real logged-in browser:
 * click "Create" like a user, then read results via the page's own session.
 *
 * This script connects to an ALREADY-RUNNING Chrome over CDP (one that is logged
 * into suno.com), drives the create page, polls the page's own /api/feed for the
 * new clips, and prints their metadata (incl. CDN audio URLs) as JSON.
 *
 * It does NOT download audio (the Python wrapper does that) and it does NOT touch
 * the obfuscated generate endpoint directly — generation happens via a UI click.
 *
 * Two ways to get a browser (no manual login required either way):
 *   1. LAUNCH (default): this script launches its OWN dedicated, isolated Chrome
 *      (own userDataDir, headless by default) and injects the engagement bot's
 *      persisted Suno session cookies. No login UI, and — crucially — it shares
 *      nothing with the posting-flow browser, so it can't cause the lock-ups /
 *      race conditions that come from contending over one browser.
 *   2. CONNECT: if cdpUrl is given, attach to an already-running Chrome instead.
 *
 * Usage:
 *   node suno_generate.js '<jsonArgs>'
 *   jsonArgs = { prompt, instrumental?, dryRun?, timeoutMs?, expectClips?,
 *                cdpUrl?, sessionFile?, userDataDir?, chromePath?, headless? }
 *
 * Output: a single JSON object on the LAST stdout line:
 *   { ok, dryRun?, clips:[{id,title,status,audioUrl,imageUrl}], error?, log:[...] }
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const puppeteer = require('puppeteer-core');

const log = [];
const note = (m) => { log.push(m); console.error('[suno_generate] ' + m); };

const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const DEFAULT_SESSION = path.join(os.homedir(),
  '.hermes/skills/devops/suno-browser-automation/references/suno-session-persist.json');
// Dedicated, isolated profile — NOT the posting-flow browser. This is what keeps
// Suno generation from contending/locking with posting automation.
const DEFAULT_PROFILE = path.join(os.homedir(), '.hermes/production/.suno-gen-profile');

function parseArgs() {
  let raw = process.argv[2] || '{}';
  let a;
  try { a = JSON.parse(raw); } catch (e) { a = {}; }
  return {
    prompt: a.prompt || '',
    instrumental: a.instrumental !== false,        // default true
    dryRun: !!a.dryRun,
    timeoutMs: a.timeoutMs || 240000,              // 4 min for generation
    expectClips: a.expectClips || 2,               // Suno makes 2 per request
    // Browser sourcing:
    cdpUrl: a.cdpUrl || process.env.SUNO_CDP_URL || '',  // empty => launch our own
    sessionFile: a.sessionFile || process.env.SUNO_SESSION_FILE || DEFAULT_SESSION,
    userDataDir: a.userDataDir || DEFAULT_PROFILE,
    chromePath: a.chromePath || process.env.SUNO_CHROME_PATH || DEFAULT_CHROME,
    headless: a.headless !== false,                // default headless (isolated, no UI)
  };
}

// Load the engagement bot's persisted Suno session (a flat name->value cookie
// map) and inject it so the dedicated browser is logged in — no manual login.
async function injectSession(page, sessionFile) {
  if (!sessionFile || !fs.existsSync(sessionFile)) {
    note(`session file not found: ${sessionFile} (continuing; may not be logged in)`);
    return 0;
  }
  let data;
  try { data = JSON.parse(fs.readFileSync(sessionFile, 'utf8')); }
  catch (e) { note('session file unreadable: ' + e.message); return 0; }
  const META = new Set(['captured_at', '_refreshed_note']);
  const cookies = [];
  for (const [name, value] of Object.entries(data)) {
    if (META.has(name) || typeof value !== 'string') continue;
    // Set on the apex so it covers suno.com + studio-api-prod.suno.com.
    cookies.push({ name, value, domain: '.suno.com', path: '/' });
  }
  try { await page.setCookie(...cookies); note(`injected ${cookies.length} session cookies`); }
  catch (e) { note('setCookie failed: ' + e.message); }
  return cookies.length;
}

// List this account's recent clips via the clean API. Confirmed shape:
//   GET https://studio-api-prod.suno.com/api/project/default
//   -> { project_clips: [ { clip: { id, title, status, audio_url, ... } } ] }
// Auth = Clerk Bearer token (window.Clerk.session.getToken()), NOT just cookies,
// and the base is studio-api-prod.suno.com (relative /api/* hits the wrong origin).
async function recentClips(page) {
  return page.evaluate(async () => {
    const API = 'https://studio-api-prod.suno.com';
    let tok = null;
    try { tok = (window.Clerk && window.Clerk.session) ? await window.Clerk.session.getToken() : null; } catch (e) {}
    try {
      const r = await fetch(API + '/api/project/default', {
        headers: tok ? { Authorization: 'Bearer ' + tok } : {}, credentials: 'include',
      });
      if (!r.ok) return { __status: r.status };
      const j = await r.json();
      const clips = (j.project_clips || []).map((pc) => pc.clip).filter(Boolean).map((c) => ({
        id: c.id, title: c.title || '', status: c.status || '',
        audioUrl: c.audio_url || '', imageUrl: c.image_url || '',
        duration: (c.metadata && (c.metadata.duration || c.metadata.duration_seconds)) || null,
      }));
      return { clips };
    } catch (e) { return { __error: String(e).slice(0, 120) }; }
  });
}

// React controlled inputs ignore JS-injected values — use REAL keystrokes so the
// app's state updates and the Create button enables.
async function setDescription(page, text) {
  // ALL the create-panel placeholders are RANDOMIZED examples, so placeholder
  // matching is unreliable. Instead, find the textarea inside the box headed
  // "Song Description" (the stable label). Verified via screenshot.
  await page.keyboard.press('Escape').catch(() => {});  // dismiss any stray modal
  const handle = await page.evaluateHandle(() => {
    const heads = Array.from(document.querySelectorAll('*'))
      .filter((e) => /^song description$/i.test((e.textContent || '').trim()) && e.children.length <= 1);
    for (const hd of heads) {
      let box = hd;
      for (let i = 0; i < 6 && box; i++) {
        const ta = box.querySelector && box.querySelector('textarea');
        if (ta) return ta;
        box = box.parentElement;
      }
    }
    return Array.from(document.querySelectorAll('textarea')).find((t) => t.offsetParent !== null) || null;
  });
  const el = handle.asElement();
  if (!el) return false;
  await el.click().catch(() => {});
  await page.keyboard.down('Meta').catch(() => {});
  await page.keyboard.press('a').catch(() => {});
  await page.keyboard.up('Meta').catch(() => {});
  await page.keyboard.press('Backspace').catch(() => {});    // clear any existing text
  await el.type(text, { delay: 12 });                         // real keystrokes
  return true;
}

// Locate the generate button (the prominent "Create", not the sidebar nav link).
async function createButton(page) {
  const h = await page.evaluateHandle(() => {
    const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
    // exact "Create" / "Create song" wins; nav link is an <a>, so excluded.
    return btns.find((b) => /^create( song)?$/i.test((b.textContent || '').trim()))
        || btns.find((b) => /create/i.test((b.getAttribute('aria-label') || ''))) || null;
  });
  return h.asElement();
}

async function createButtonState(page) {
  const btn = await createButton(page);
  if (!btn) return { found: false };
  // Only trust real disabled signals — NOT className (Tailwind keeps `disabled:`
  // variant classes in the string at all times, which would false-positive).
  const disabled = await btn.evaluate((b) => b.disabled === true
    || b.getAttribute('aria-disabled') === 'true');
  return { found: true, disabled };
}

async function ensureInstrumental(page, want) {
  return page.evaluate((on) => {
    const els = Array.from(document.querySelectorAll('button, [role="checkbox"], [role="switch"]'));
    const el = els.find((b) => /instrumental/i.test(
      (b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '')));
    if (!el) return 'not-found';
    const checked = el.getAttribute('aria-checked') === 'true'
      || /(^|\s)(checked|active|selected)(\s|$)/i.test(el.className);
    if (checked !== on) { el.click(); return on ? 'enabled' : 'disabled'; }
    return 'already-' + (on ? 'on' : 'off');
  }, want);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const args = parseArgs();
  if (!args.prompt) {
    console.log(JSON.stringify({ ok: false, error: 'prompt required', log }));
    return;
  }

  let browser;
  let launched = false;
  try {
    if (args.cdpUrl) {
      note(`connecting to existing Chrome at ${args.cdpUrl}`);
      browser = await puppeteer.connect({ browserURL: args.cdpUrl, defaultViewport: null });
    } else {
      note(`launching dedicated ${args.headless ? 'headless ' : ''}Chrome (isolated profile: ${args.userDataDir})`);
      browser = await puppeteer.launch({
        executablePath: args.chromePath,
        headless: args.headless ? 'new' : false,
        userDataDir: args.userDataDir,
        args: ['--no-first-run', '--no-default-browser-check', '--disable-blink-features=AutomationControlled'],
      });
      launched = true;
    }
  } catch (e) {
    console.log(JSON.stringify({
      ok: false,
      error: (args.cdpUrl
        ? `cannot connect to Chrome at ${args.cdpUrl}: ${e.message}`
        : `cannot launch Chrome at ${args.chromePath}: ${e.message}. Set chromePath/SUNO_CHROME_PATH.`),
      log,
    }));
    return;
  }

  const finish = async (obj) => {
    try { launched ? await browser.close() : await browser.disconnect(); } catch (_) {}
    console.log(JSON.stringify(obj));
  };

  try {
    const pages = await browser.pages();
    let page = pages.find((p) => /suno\.com/.test(p.url())) || (await browser.newPage());
    await page.bringToFront().catch(() => {});
    // Inject persisted session BEFORE loading suno.com so we land logged in.
    await injectSession(page, args.sessionFile);
    if (!/suno\.com\/create/.test(page.url())) {
      note('navigating to suno.com/create');
      await page.goto('https://suno.com/create', { waitUntil: 'domcontentloaded', timeout: 60000 });
    } else {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    }
    await sleep(2500);

    // Login check: the create textarea must exist.
    const ok = await setDescription(page, args.prompt);
    if (!ok) {
      await finish({
        ok: false,
        error: 'create UI not found — session likely not valid/logged in. ' +
               'Refresh the persisted Suno session (sessionFile).',
        log,
      });
      return;
    }
    note('description set');
    const instr = await ensureInstrumental(page, args.instrumental);
    note(`instrumental: ${instr}`);

    const beforeResp = await recentClips(page);
    const before = (beforeResp.clips || []).map((c) => c.id);
    note(`existing clips before: ${before.length}` + (beforeResp.__status ? ` (api ${beforeResp.__status})` : ''));

    // React enables Create a beat after the prompt registers — poll for it.
    let btnState = await createButtonState(page);
    for (let i = 0; i < 8 && btnState.found && btnState.disabled; i++) {
      await sleep(1000);
      btnState = await createButtonState(page);
    }
    note(`create button: ${JSON.stringify(btnState)}`);

    if (args.dryRun) {
      await finish({
        ok: true, dryRun: true,
        plan: { prompt: args.prompt, instrumental: args.instrumental, wouldClick: 'Create' },
        createButton: btnState, existingClips: before.length, log,
      });
      return;
    }

    if (!btnState.found) {
      await finish({ ok: false, error: 'Create button not found', log });
      return;
    }
    if (btnState.disabled) {
      await finish({ ok: false, error: 'Create button disabled (prompt not registered?)', log });
      return;
    }
    const btn = await createButton(page);
    await btn.click();                                  // real mouse click
    note('clicked Create');

    // Poll the account's recent clips for the new ones reaching `complete` with audio.
    const beforeSet = new Set(before);
    const deadline = Date.now() + args.timeoutMs;
    let ready = [];
    while (Date.now() < deadline) {
      await sleep(6000);
      const resp = await recentClips(page);
      const fresh = (resp.clips || []).filter((c) => !beforeSet.has(c.id));
      const done = fresh.filter((c) => c.audioUrl && (c.status === 'complete' || c.status === 'streaming'));
      note(`fresh=${fresh.length} ready=${done.length} statuses=[${fresh.map((c) => c.status).join(',')}]`);
      if (done.length >= Math.min(args.expectClips, 1)) { ready = done.slice(0, args.expectClips); break; }
    }

    await finish({
      ok: ready.length > 0,
      clips: ready,
      error: ready.length ? undefined : 'timed out waiting for audio URLs',
      log,
    });
  } catch (e) {
    await finish({ ok: false, error: e.message, log });
  }
}

main();
