/* ═══════════════════════════════════════════════════════════════
   KINOKORE — Gen AI tab (genai.js)

   Veo (text/image → video) + Nano Banana (generate/edit/compose
   images) via the Gemini API. Thin client over /api/genai/{veo,nano}.
   Each POST makes ONE artifact; a batch loops here so no single
   request outlives a client timeout. Veo's >1-clip cost gate is an
   explicit estimate + confirm before the loop, per the CLAUDE.md rule.

   Loaded AFTER app.js — reuses loadVideo() + the shared file picker
   (.fpick-btn with data-fpick-files, bound by app.js).
═══════════════════════════════════════════════════════════════ */

(() => {
  'use strict';

  const $id = (s) => document.getElementById(s);
  const panel = document.querySelector('.tab-panel[data-panel="genai"]');
  if (!panel) return;

  // mirror server's _VEO_USD_PER_SEC for the cost estimate
  const VEO_USD_PER_SEC = {
    'veo-3.1-fast-generate-preview': 0.15,
    'veo-3.1-generate-preview': 0.40,
    'veo-3.1-lite-generate-preview': 0.10,
  };

  const statusEl = $id('genai-status');
  const progEl = $id('genai-progress');
  const progFill = $id('genai-prog-fill');
  const progLabel = $id('genai-prog-label');
  const resultsEl = $id('genai-results');
  const gridEl = $id('genai-grid');
  const runLabel = $id('genai-run-label');
  const veoForm = $id('genai-veo');
  const nanoForm = $id('genai-nano');
  const veoCost = $id('veo-cost');

  let mode = 'veo';

  function setStatus(msg, isErr) {
    statusEl.hidden = !msg;
    statusEl.textContent = msg || '';
    statusEl.style.color = isErr ? 'var(--err)' : '';
  }

  // ── mode chips ────────────────────────────────────────────────
  document.querySelectorAll('#genai-mode-row .chip').forEach((c) =>
    c.addEventListener('click', () => {
      mode = c.dataset.genaiMode;
      document.querySelectorAll('#genai-mode-row .chip').forEach((x) =>
        x.classList.toggle('selected', x === c));
      veoForm.hidden = mode !== 'veo';
      nanoForm.hidden = mode !== 'nano';
    }));

  // ── list models ───────────────────────────────────────────────
  const modelsBtn = $id('genai-models-btn');
  if (modelsBtn) modelsBtn.addEventListener('click', async () => {
    setStatus('listing models…');
    try {
      const res = await fetch('/api/genai/models');
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      const fill = (sel, ids) => {
        if (!sel || !ids || !ids.length) return;
        const prev = sel.value;
        sel.innerHTML = '';
        ids.forEach((id) => {
          const o = document.createElement('option');
          o.value = id; o.textContent = id;
          sel.appendChild(o);
        });
        if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
      };
      fill($id('veo-model'), data.veo);
      fill($id('nano-model'), data.image);
      setStatus(`models refreshed — ${(data.veo || []).length} video · ${(data.image || []).length} image`
        + (data.error ? ` (partial: ${data.error})` : ''));
    } catch (e) {
      setStatus('list models failed: ' + e.message, true);
    }
  });

  // ── results grid ──────────────────────────────────────────────
  function clearGrid() { gridEl.innerHTML = ''; resultsEl.hidden = false; }

  function addVideo(path, gen) {
    const card = document.createElement('div');
    card.className = 'genai-item';
    const name = path.split('/').pop();
    card.innerHTML = `
      <video src="/api/file/${encodeURI(path)}" controls preload="metadata"></video>
      <div class="gi-name" title="${path}">${name}</div>
      <div class="gi-actions">
        <button type="button" class="ghost-mini gi-load">▶ monitor</button>
        <button type="button" class="ghost-mini gi-copy">copy path</button>
      </div>`;
    card.querySelector('.gi-load').addEventListener('click', () => {
      try { if (typeof loadVideo === 'function') loadVideo(path); } catch { /* */ }
    });
    card.querySelector('.gi-copy').addEventListener('click', () =>
      navigator.clipboard && navigator.clipboard.writeText(path));
    gridEl.appendChild(card);
  }

  function addImage(path) {
    const card = document.createElement('div');
    card.className = 'genai-item';
    const name = path.split('/').pop();
    card.innerHTML = `
      <img src="/api/file/${encodeURI(path)}" alt="${name}" />
      <div class="gi-name" title="${path}">${name}</div>
      <div class="gi-actions">
        <button type="button" class="ghost-mini gi-veo">→ Veo first-frame</button>
        <button type="button" class="ghost-mini gi-copy">copy path</button>
      </div>`;
    // chain: use this render as Veo's first frame (NB → Veo consistency)
    card.querySelector('.gi-veo').addEventListener('click', () => {
      $id('veo-image').value = path;
      document.querySelector('#genai-mode-row .chip[data-genai-mode="veo"]').click();
      setStatus('set as Veo first-frame — switch to a Veo prompt and generate');
    });
    card.querySelector('.gi-copy').addEventListener('click', () =>
      navigator.clipboard && navigator.clipboard.writeText(path));
    gridEl.appendChild(card);
  }

  const splitList = (s) => (s || '').split(',').map((x) => x.trim()).filter(Boolean);

  // ── Veo generate ──────────────────────────────────────────────
  const veoBtn = $id('veo-generate');
  veoBtn.addEventListener('click', async () => {
    const prompt = $id('veo-prompt').value.trim();
    if (!prompt) { setStatus('prompt is required', true); return; }
    const count = Math.max(1, Math.min(8, parseInt($id('veo-count').value, 10) || 1));
    const model = $id('veo-model').value;
    const dur = parseInt($id('veo-duration').value, 10) || 8;

    // cost gate — >1 clip needs an explicit confirm with the estimate
    if (count > 1) {
      const rate = VEO_USD_PER_SEC[model] ?? 0.40;
      const est = (rate * dur * count).toFixed(2);
      veoCost.hidden = false;
      veoCost.textContent = `${count} clips × ${dur}s × $${rate}/s ≈ $${est} (approx). `;
      if (!window.confirm(
        `Veo will generate ${count} clips (${dur}s each) on ${model}.\n`
        + `Estimated cost ≈ $${est} (Google bills per second; this is approximate).\n\nProceed?`)) {
        setStatus('cancelled — no clips generated');
        return;
      }
    } else {
      veoCost.hidden = true;
    }

    const run = 'veo_' + Date.now();
    const payloadBase = {
      prompt,
      negative: $id('veo-negative').value.trim(),
      aspect: $id('veo-aspect').value,
      resolution: $id('veo-resolution').value,
      duration: $id('veo-duration').value ? dur : null,
      model,
      image: $id('veo-image').value.trim() || null,
      reference_images: splitList($id('veo-refs').value),
      run,
    };

    veoBtn.disabled = true;
    clearGrid();
    runLabel.textContent = `· ${run}`;
    try {
      for (let i = 0; i < count; i++) {
        setStatus(`Veo rendering clip ${i + 1}/${count} — ~70s each, please wait…`);
        const jobId = window.kfNewJobId('veo');
        const stop = window.kfTrackProgress(jobId,
          { container: progEl, fill: progFill, label: progLabel });
        try {
          const res = await fetch('/api/genai/veo', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...payloadBase, index: i, job_id: jobId }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || res.statusText);
          addVideo(data.output, data.gen);
        } finally { stop(); }
      }
      setStatus(`✔ done — ${count} clip(s) in videos/edit/genai/${run}/`);
    } catch (e) {
      setStatus('Veo failed: ' + e.message, true);
    } finally {
      veoBtn.disabled = false;
    }
  });

  // ── Nano Banana generate ──────────────────────────────────────
  const nanoBtn = $id('nano-generate');
  nanoBtn.addEventListener('click', async () => {
    const prompt = $id('nano-prompt').value.trim();
    if (!prompt) { setStatus('prompt is required', true); return; }
    const count = Math.max(1, Math.min(10, parseInt($id('nano-count').value, 10) || 1));
    const run = 'nano_' + Date.now();
    const payloadBase = {
      prompt,
      images: splitList($id('nano-images').value),
      aspect: $id('nano-aspect').value,
      size: $id('nano-size').value,
      model: $id('nano-model').value,
      run,
    };

    nanoBtn.disabled = true;
    clearGrid();
    runLabel.textContent = `· ${run}`;
    try {
      for (let i = 0; i < count; i++) {
        setStatus(`Nano Banana rendering ${i + 1}/${count}…`);
        const jobId = window.kfNewJobId('nano');
        const stop = window.kfTrackProgress(jobId,
          { container: progEl, fill: progFill, label: progLabel });
        try {
          const res = await fetch('/api/genai/nano', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...payloadBase, index: i, job_id: jobId }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || res.statusText);
          (data.outputs || []).forEach(addImage);
        } finally { stop(); }
      }
      setStatus(`✔ done — image(s) in videos/edit/genai/${run}/`);
    } catch (e) {
      setStatus('Nano Banana failed: ' + e.message, true);
    } finally {
      nanoBtn.disabled = false;
    }
  });
})();
