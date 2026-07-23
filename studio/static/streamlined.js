/* ═══════════════════════════════════════════════════════════════
   KINOKORE — Streamlined Ads (streamlined.js)

   The ⚡ sidebar button → modal → POST /api/streamlined_ad. Two
   formats (bullets / text-vo), both fully deterministic renders via
   helpers/streamlined_ad.py. The "let Kino write the copy" toggle
   instead sends an autonomous prompt: Kino reads the team's Brand
   Guidelines, writes hook/bullets/CTA, and runs the same helper.

   Loaded AFTER app.js — reuses its globals when present (TEAMS,
   resolveTeamFolder, loadVideo) and the shared folder-picker modal
   (.fpick-btn auto-binding via app.js's MutationObserver).
═══════════════════════════════════════════════════════════════ */

(() => {
  'use strict';

  const $id = (s) => document.getElementById(s);
  const btn = $id('streamlined-btn');
  const modal = $id('streamlined-modal');
  if (!btn || !modal) return;

  const hookEl = $id('sa-hook');
  const bulletsField = $id('sa-bullets-field');
  const bulletsEl = $id('sa-bullets');
  const ctaEl = $id('sa-cta');
  const bgEl = $id('sa-background');
  const musicEl = $id('sa-music');
  const voiceRow = $id('sa-voice-row');
  const voiceSel = $id('sa-voice');
  const durEl = $id('sa-duration');
  const aspectEl = $id('sa-aspect');
  const countEl = $id('sa-count');
  const kinoHook = $id('sa-kino-hook');
  const kinoBullets = $id('sa-kino-bullets');
  const kinoCta = $id('sa-kino-cta');
  const discOn = $id('sa-disclaimer-on');
  const discEl = $id('sa-disclaimer');
  const statusEl = $id('sa-status');
  const progEl = $id('sa-progress');
  const progFill = $id('sa-prog-fill');
  const progLabel = $id('sa-prog-label');
  const genBtn = $id('sa-generate');
  const logoSel = $id('sa-logo');
  const brandOn = $id('sa-brand-on');
  const numBrollEl = $id('sa-num-broll');
  const ctaOn = $id('sa-cta-on');
  const ctaBg = $id('sa-cta-bg');
  const ctaFg = $id('sa-cta-fg');
  const brandPrimary = $id('sa-brand-primary');
  const brandAccent = $id('sa-brand-accent');
  const brandSave = $id('sa-brand-save');

  let format = 'bullets';
  let brandFolder = '';   // resolved team folder the loaded preset belongs to

  function setStatus(msg, isErr) {
    statusEl.hidden = !msg;
    statusEl.textContent = msg || '';
    statusEl.style.color = isErr ? 'var(--err)' : '';
  }

  // ── format chips ──────────────────────────────────────────────
  document.querySelectorAll('#sa-format-row .chip').forEach((c) =>
    c.addEventListener('click', () => {
      format = c.dataset.saFormat;
      document.querySelectorAll('#sa-format-row .chip').forEach((x) =>
        x.classList.toggle('selected', x === c));
      bulletsField.style.display = format === 'bullets' ? '' : 'none';
      voiceRow.hidden = format !== 'text-vo';
    }));

  // ── per-piece "let Kino write this" checkboxes ────────────────
  // Ticking a piece dims its input (Kino will author it) and routes the
  // whole render through Kino so it can fill the blanks.
  function syncKinoPiece(cb, inputEl, ph) {
    if (!cb || !inputEl) return;
    inputEl.disabled = cb.checked;
    inputEl.classList.toggle('kino-owned', cb.checked);
    if (cb.checked) {
      inputEl.dataset.ph = inputEl.placeholder || '';
      inputEl.placeholder = ph;
    } else if (inputEl.dataset.ph != null) {
      inputEl.placeholder = inputEl.dataset.ph;
    }
  }
  if (kinoHook) kinoHook.addEventListener('change', () =>
    syncKinoPiece(kinoHook, hookEl, '🎲 Kino will write the hook'));
  if (kinoBullets) kinoBullets.addEventListener('change', () =>
    syncKinoPiece(kinoBullets, bulletsEl, '🎲 Kino will write the bullets'));
  if (kinoCta) kinoCta.addEventListener('change', () =>
    syncKinoPiece(kinoCta, ctaEl, '🎲 Kino will write the CTA'));

  // ── disclaimer toggle ─────────────────────────────────────────
  if (discOn) discOn.addEventListener('change', () => {
    discEl.disabled = !discOn.checked;
    if (discOn.checked) discEl.focus();
  });

  const anyKino = () => !!(
    (kinoHook && kinoHook.checked) ||
    (kinoBullets && kinoBullets.checked && format === 'bullets') ||
    (kinoCta && kinoCta.checked));

  // ── voices (same static cache the variants modal uses) ────────
  async function loadVoices() {
    try {
      const res = await fetch('/voices.json');
      const data = await res.json();
      const voices = data.voices || data || [];
      const prev = voiceSel.value;
      voiceSel.innerHTML = '<option value="">— pick a voice —</option>';
      for (const v of voices) {
        const o = document.createElement('option');
        o.value = v.voice_id || v.id || '';
        o.textContent = (v.name || o.value) + (v.category === 'cloned' ? ' · cloned' : '');
        voiceSel.appendChild(o);
      }
      if (prev) voiceSel.value = prev;
      // default to the studio's usual VO voice when present
      if (!voiceSel.value && [...voiceSel.options].some((o) => o.value === 'xsLQCPQf2lJnUdFzvAJ2'))
        voiceSel.value = 'xsLQCPQf2lJnUdFzvAJ2';
    } catch (e) { /* voices stay manual */ }
  }
  const vr = $id('sa-voice-refresh');
  if (vr) vr.addEventListener('click', loadVoices);

  // ── team-aware defaults ───────────────────────────────────────
  // /api/folder/scan both verifies a folder exists AND heals stale drive
  // letters (the saved asset root can still say D: after the D:→E: move).
  async function probeFolder(path) {
    try {
      const res = await fetch('/api/folder/scan?path=' + encodeURIComponent(path));
      if (!res.ok) return null;
      const data = await res.json();
      return data.absolute_folder || path;
    } catch { return null; }
  }

  async function fillDefaults() {
    let root = '';
    try { root = localStorage.getItem('veditor.diceRoot.v1') || ''; } catch { /* */ }
    if (!root) return;
    root = root.replace(/[\\/]+$/, '');
    if (!musicEl.value) {
      musicEl.value = (await probeFolder(root + '/Music')) || (root + '/Music');
    }
    if (!bgEl.value) {
      let team = '';
      try { team = localStorage.getItem('veditor.team.v1') || ''; } catch { /* */ }
      try {
        if (team && typeof resolveTeamFolder === 'function') {
          const tf = (await resolveTeamFolder(root, team) || '').replace(/[\\/]+$/, '');
          if (tf) {
            // finished shots only — prefer B-Roll/Final when the team has one
            bgEl.value = (await probeFolder(tf + '/B-Roll/Final'))
              || (await probeFolder(tf + '/B-Roll'))
              || (tf + '/B-Roll');
          }
        }
      } catch { /* */ }
    }
  }

  // ── brand presets (logo + colors) ─────────────────────────────
  // Reads <team>/Guidelines/brand.json + discovers logo images via
  // GET /api/brand. "save as team default" writes the json back.
  async function currentTeamFolder() {
    let root = '', team = '';
    try {
      root = (localStorage.getItem('veditor.diceRoot.v1') || '').replace(/[\\/]+$/, '');
      team = localStorage.getItem('veditor.team.v1') || '';
    } catch { /* */ }
    if (!root || !team) return '';
    try {
      if (typeof resolveTeamFolder === 'function') {
        const tf = (await resolveTeamFolder(root, team) || '').replace(/[\\/]+$/, '');
        if (tf && tf !== root) return tf;
      }
    } catch { /* */ }
    return '';
  }

  function setBrandEnabled(on) {
    brandPrimary.disabled = !on;
    brandAccent.disabled = !on;
  }
  brandOn.addEventListener('change', () => setBrandEnabled(brandOn.checked));
  if (ctaOn) ctaOn.addEventListener('change', () => {
    ctaBg.disabled = ctaFg.disabled = !ctaOn.checked;
  });

  async function loadBrand() {
    brandFolder = await currentTeamFolder();
    if (!brandFolder) return;
    let data;
    try {
      const res = await fetch('/api/brand?folder=' + encodeURIComponent(brandFolder));
      if (!res.ok) return;
      data = await res.json();
    } catch { return; }

    const prev = logoSel.value;
    logoSel.innerHTML = '<option value="">— none —</option>';
    for (const p of data.logos || []) {
      const o = document.createElement('option');
      o.value = p;
      o.textContent = p.split(/[\\/]/).pop();
      logoSel.appendChild(o);
    }
    if (prev && [...logoSel.options].some((o) => o.value === prev)) logoSel.value = prev;

    const b = data.brand;
    if (b) {
      if (b.primary) brandPrimary.value = b.primary;
      if (b.accent) brandAccent.value = b.accent;
      if (b.logo && [...logoSel.options].some((o) => o.value === b.logo) && !prev)
        logoSel.value = b.logo;
      if (b.primary || b.accent) { brandOn.checked = true; setBrandEnabled(true); }
    }
  }

  brandSave.addEventListener('click', async () => {
    if (!brandFolder) { setStatus('pick a team first (no team folder resolved)', true); return; }
    try {
      const res = await fetch('/api/brand', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          folder: brandFolder,
          primary: brandOn.checked ? brandPrimary.value : '',
          accent: brandOn.checked ? brandAccent.value : '',
          logo: logoSel.value || '',
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      setStatus('✔ saved team brand preset → ' + data.saved);
    } catch (e) {
      setStatus('brand save failed: ' + e.message, true);
    }
  });

  // ── open / close ──────────────────────────────────────────────
  function open() {
    modal.hidden = false;
    setStatus('');
    fillDefaults();
    loadBrand();
    if (!voiceSel.options.length || voiceSel.options.length <= 1) loadVoices();
  }
  function close() { modal.hidden = true; }
  btn.addEventListener('click', open);
  $id('sa-close').addEventListener('click', close);
  $id('sa-cancel').addEventListener('click', close);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) close(); });

  // ── generate ──────────────────────────────────────────────────
  function bulletsList() {
    return bulletsEl.value.split('\n').map((s) => s.trim()).filter(Boolean);
  }

  function validate() {
    // pieces the user is authoring themselves must be filled in
    if (!(kinoHook && kinoHook.checked) && !hookEl.value.trim())
      return 'hook is required (or tick 🎲 Kino to have it written)';
    if (format === 'bullets' && !(kinoBullets && kinoBullets.checked) && !bulletsList().length)
      return 'add at least one bullet (or tick 🎲 Kino)';
    if (format === 'text-vo' && !voiceSel.value) return 'pick a voice for the VO';
    if (!bgEl.value.trim()) return 'background file/folder is required';
    if (discOn && discOn.checked && !discEl.value.trim())
      return 'disclaimer is on but empty — type it or untick';
    return null;
  }

  const disclaimerText = () => (discOn && discOn.checked) ? discEl.value.trim() : '';

  async function generateDirect() {
    const count = Math.max(1, Math.min(6, parseInt(countEl.value, 10) || 1));
    const disclaimer = disclaimerText();
    genBtn.disabled = true;
    const outputs = [];
    try {
      for (let i = 0; i < count; i++) {
        setStatus(`rendering ${i + 1}/${count}…`);
        const jobId = window.kfNewJobId('sa');
        const stop = window.kfTrackProgress(jobId,
          { container: progEl, fill: progFill, label: progLabel });
        const payload = {
          format,
          hook: hookEl.value.trim(),
          bullets: format === 'bullets' ? bulletsList() : [],
          cta: ctaEl.value.trim(),
          disclaimer,
          background: bgEl.value.trim(),
          music: musicEl.value.trim() || null,
          voice_id: format === 'text-vo' ? voiceSel.value : null,
          duration: parseFloat(durEl.value) || 15,
          aspect: aspectEl.value,
          seed: count > 1 ? Date.now() + i * 7919 : null,
          logo: logoSel.value || null,
          brand_primary: brandOn.checked ? brandPrimary.value : null,
          brand_accent: brandOn.checked ? brandAccent.value : null,
          cta_bg: (ctaOn && ctaOn.checked) ? ctaBg.value : null,
          cta_fg: (ctaOn && ctaOn.checked) ? ctaFg.value : null,
          num_broll: Math.max(1, parseInt((numBrollEl && numBrollEl.value) || '1', 10) || 1),
          job_id: jobId,
        };
        try {
          const res = await fetch('/api/streamlined_ad', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || res.statusText);
          outputs.push(data.output);
        } finally { stop(); }
      }
      setStatus(`✔ done — ${outputs.length} ad(s) in Final Output. Loaded the first in the monitor; edit runs are in the timeline picker.`);
      try { if (typeof loadVideo === 'function') loadVideo(outputs[0]); } catch { /* */ }
    } catch (e) {
      setStatus('failed: ' + e.message, true);
    } finally {
      genBtn.disabled = false;
    }
  }

  function generateViaKino() {
    const promptEl = $id('prompt');
    const form = $id('prompt-form');
    if (!promptEl || !form) { setStatus('composer not found', true); return; }
    let team = '';
    try { team = localStorage.getItem('veditor.team.v1') || ''; } catch { /* */ }
    const teamName = (typeof TEAMS !== 'undefined' && TEAMS[team] && TEAMS[team].name) || team || 'the selected team';
    let root = '';
    try { root = localStorage.getItem('veditor.diceRoot.v1') || ''; } catch { /* */ }
    const count = Math.max(1, Math.min(6, parseInt(countEl.value, 10) || 1));
    const voiceArg = format === 'text-vo' ? voiceSel.value : '';
    const brandArgs = (brandOn.checked
      ? ` --brand-primary "${brandPrimary.value}" --brand-accent "${brandAccent.value}"` : '')
      + (logoSel.value ? ` --logo "${logoSel.value}"` : '');
    const ctaArgs = (ctaOn && ctaOn.checked)
      ? ` --cta-bg "${ctaBg.value}" --cta-fg "${ctaFg.value}"` : '';
    const nBroll = Math.max(1, parseInt((numBrollEl && numBrollEl.value) || '1', 10) || 1);
    const shotArgs = nBroll > 1 ? ` --num-broll ${nBroll}` : '';

    // which pieces Kino authors vs. which the user locked in
    const wHook = !!(kinoHook && kinoHook.checked);
    const wBullets = !!(kinoBullets && kinoBullets.checked) && format === 'bullets';
    const wCta = !!(kinoCta && kinoCta.checked);
    const userHook = hookEl.value.trim();
    const userBullets = bulletsList();
    const userCta = ctaEl.value.trim();
    const disclaimer = disclaimerText();

    const toWrite = [];
    if (wHook) toWrite.push(format === 'bullets'
      ? 'HOOK — one benefit-framed header line (like "An Endurance protection plan includes:")'
      : 'HOOK — one scroll-stopping question/statement, ≤10 words (renders huge + read aloud)');
    if (wBullets) toWrite.push('BULLETS — 3 to 5 punchy selling points, ≤~42 chars each');
    if (wCta) toWrite.push('CTA — one call-to-action line' + (format === 'text-vo' ? ' (read aloud)' : ''));

    const locked = [];
    if (!wHook) locked.push(`HOOK = "${userHook}"`);
    if (format === 'bullets' && !wBullets) locked.push('BULLETS = ' + JSON.stringify(userBullets));
    if (!wCta) locked.push(`CTA = "${userCta}"`);
    if (disclaimer) locked.push(`DISCLAIMER (verbatim, do not rewrite) = "${disclaimer}"`);

    // build the command with locked pieces inlined, placeholders for Kino's
    const hookArg = wHook ? '<hook>' : userHook;
    const ctaArg = wCta ? '<cta>' : userCta;
    const bulletArg = format !== 'bullets' ? ''
      : wBullets ? ' --bullet "<b1>" --bullet "<b2>" --bullet "<b3>"'
        : userBullets.map((b) => ` --bullet "${b}"`).join('');
    const discArg = disclaimer ? ` --disclaimer "${disclaimer}"` : '';

    promptEl.value =
`STREAMLINED AD — autonomous, no approval gates. The user pre-authorized by clicking Generate.

Create ${count} "${format}" streamlined ad(s) for team ${teamName}.

${toWrite.length ? `STEP 1 — generate the copy with the LOCAL copy model (free, $0 tokens) — do NOT write it yourself. Run copy_gen.py, which reads the brand guidelines under "${root}/${teamName}/Brand Guidelines/" and returns JSON. Write ONLY these piece(s)${count > 1 ? ', a DIFFERENT angle per ad' : ''}:
${toWrite.map((t) => '- ' + t).join('\n')}
Run (once per ad; pick a distinct --angle per ad, e.g. problem-first / social-proof / benefit-first):
PYTHONUTF8=1 $VU_PY video-use/helpers/copy_gen.py --format ${format === 'bullets' ? 'bullets' : 'vo'} --brand "${root}/${teamName}/Brand Guidelines/<the guidelines file>" --angle <angle>${wHook ? '' : ` --hook "${userHook}"`}${wCta ? '' : ` --cta "${userCta}"`} --json
Parse its JSON for the piece(s) you need${wHook ? ' (hook)' : ''}${wBullets ? ' (bullets)' : ''}${wCta ? ' (cta)' : ''}. If Ollama is unreachable, fall back to writing them yourself, brand-compliant. Print the final copy.`
  : 'STEP 1 — all copy is user-provided below; no writing needed.'}

${locked.length ? `Use these EXACTLY as given (do not change them):\n${locked.map((l) => '- ' + l).join('\n')}\n` : ''}
STEP ${toWrite.length ? '3' : '2'} — render each ad by running (fill only the <placeholders> you wrote; everything else is already inlined). Background rule: FINISHED footage only — prefer "<team>/B-Roll/Final/"; never Install/. Heal drive letters if a path 404s (assets moved D:→E:).
PYTHONUTF8=1 $VU_PY video-use/helpers/streamlined_ad.py --format ${format} --background "${bgEl.value.trim() || '<team B-Roll/Final folder>'}" --hook "${hookArg}"${bulletArg} --cta "${ctaArg}"${discArg}${voiceArg ? ` --voice-id ${voiceArg}` : ''}${musicEl.value.trim() ? ` --music "${musicEl.value.trim()}"` : ''}${brandArgs}${ctaArgs}${shotArgs} --duration ${parseFloat(durEl.value) || 15} --output "videos/edit/streamlined_<timestamp>/ad_<n>.mp4" --json

STEP ${toWrite.length ? '4' : '3'} — deliver: copy each finished mp4 to "Final Output/<the ad's hook text, sanitized>/ad_<n>.mp4" (create the folder). The edit run stays in videos/edit/ for the timeline.

Renders are seconds each and deterministic. When done, list every Final Output path.`;
    close();
    const tabBtn = document.querySelector('.tab[data-tab="chat"]');
    if (tabBtn) tabBtn.click();
    // Autonomous batch → let the server route the model (Sonnet) + fresh session.
    if (typeof window.kfRouteNextSubmit === 'function') window.kfRouteNextSubmit();
    form.requestSubmit();
  }

  genBtn.addEventListener('click', () => {
    const err = validate();
    if (err) { setStatus(err, true); return; }
    if (anyKino()) generateViaKino();
    else generateDirect();
  });
})();
