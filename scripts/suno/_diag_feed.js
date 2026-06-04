#!/usr/bin/env node
/* Read-only diagnostic: launch isolated headless Chrome, inject session, read the
 * newest workspace clips and dump the RAW shape of the feed/clip API responses so
 * we can fix field names. Does NOT generate anything (no credits). */
'use strict';
const fs = require('fs'), os = require('os'), path = require('path');
const puppeteer = require('puppeteer-core');
const SESSION = path.join(os.homedir(), '.hermes/skills/devops/suno-browser-automation/references/suno-session-persist.json');
const PROFILE = path.join(os.homedir(), '.hermes/production/.suno-gen-profile');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const redact = (s) => String(s).replace(/eyJ[\w-]+\.[\w-]+\.[\w-]+/g, '[JWT]').replace(/\b[\w-]{40,}\b/g, '[TOK]');

(async () => {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', userDataDir: PROFILE,
    args: ['--no-first-run', '--no-default-browser-check', '--disable-blink-features=AutomationControlled'] });
  const out = {};
  try {
    const page = await browser.newPage();
    const data = JSON.parse(fs.readFileSync(SESSION, 'utf8'));
    const cookies = Object.entries(data).filter(([k, v]) => typeof v === 'string' && k !== 'captured_at' && k !== '_refreshed_note')
      .map(([name, value]) => ({ name, value, domain: '.suno.com', path: '/' }));
    await page.setCookie(...cookies);
    await page.goto('https://suno.com/create', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(3000);

    out.pageState = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      hasTextarea: !!document.querySelector('textarea'),
      bodyStart: (document.body ? document.body.innerText : '').slice(0, 300),
      signin: /sign in|log in|verify you are human|cloudflare|just a moment/i.test(document.body ? document.body.innerText : ''),
    }));

    const ids = await page.evaluate(() => {
      const s = new Set();
      document.querySelectorAll('a[href^="/song/"]').forEach(a => { const m = a.getAttribute('href').match(/\/song\/([0-9a-f-]{36})/i); if (m) s.add(m[1]); });
      return Array.from(s);
    });
    out.domIds = { count: ids.length, newest: ids.slice(0, 4) };

    // Probe the REAL API base (studio-api-prod) with a Clerk Bearer token.
    out.probes = await page.evaluate(async () => {
      const API = 'https://studio-api-prod.suno.com';
      let tok = null;
      try { tok = (window.Clerk && window.Clerk.session) ? await window.Clerk.session.getToken() : null; } catch (e) {}
      const H = tok ? { Authorization: 'Bearer ' + tok } : {};
      const summarize = (text) => {
        try { const j = JSON.parse(text); const arr = Array.isArray(j) ? j : (j.clips || j.feed || j.songs || j.clips_v2 || []);
          const it = arr && arr[0];
          return { count: arr && arr.length, firstItemKeys: it ? Object.keys(it) : null,
            audioFields: it ? Object.keys(it).filter(k => /audio|video|image|url|status|title/i.test(k)).map(k => k + '=' + String(it[k]).slice(0, 70)) : null };
        } catch (e) { return { parseError: true }; }
      };
      const results = { hasToken: !!tok };
      // 1) list recent clips for this account — clips are nested as project_clips[].clip
      try {
        const r = await fetch(API + '/api/project/default', { headers: H, credentials: 'include' });
        const j = await r.json();
        const clips = (j.project_clips || []).map(pc => pc.clip).filter(Boolean);
        const c0 = clips[0] || {};
        results.projectDefault = {
          status: r.status,
          clipCount: clips.length,
          firstClipKeys: Object.keys(c0),
          firstClipAudioFields: Object.keys(c0).filter(k => /audio|video|image|url|status|title/i.test(k))
            .map(k => k + '=' + String(c0[k]).slice(0, 75)),
        };
        results.recentIds = clips.slice(0, 3).map(c => c.id).filter(Boolean);
      } catch (e) { results.projectDefault = { error: String(e).slice(0, 120) }; }
      // 2) feed/v2 (GET) and feed/v3 (POST) using any ids we found
      const ids = results.recentIds || [];
      if (ids.length) {
        try { const r = await fetch(API + '/api/feed/v2/?ids=' + ids.join(','), { headers: H, credentials: 'include' });
          const t = await r.text(); results.feedV2 = { status: r.status, summary: summarize(t) }; } catch (e) { results.feedV2 = { error: String(e).slice(0,120) }; }
        try { const r = await fetch(API + '/api/feed/v3', { method: 'POST', credentials: 'include',
            headers: { ...H, 'Content-Type': 'application/json' }, body: JSON.stringify({ clip_ids: ids }) });
          const t = await r.text(); results.feedV3 = { status: r.status, summary: summarize(t) }; } catch (e) { results.feedV3 = { error: String(e).slice(0,120) }; }
      }
      return results;
    });

    console.log(JSON.stringify(out, null, 2).split('\n').map(redact).join('\n'));
  } catch (e) { console.log('DIAG ERROR: ' + e.message); }
  finally { await browser.close(); }
})();
