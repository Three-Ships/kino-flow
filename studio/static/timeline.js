/* ═══════════════════════════════════════════════════════════════
   KINOKORE — editor timeline dock (timeline.js)

   Renders the multi-track assembly of a finished run (A-cam cut,
   b-roll cutaways, dialog, VO, caption cues) from
   GET /api/timeline/manifest, synced to the #player program monitor.

   Editing model:
   - v1 (A-CAM) clips: edge-trim + delete, ripple — downstream clips
     re-pack cumulatively because the cut defines program length.
   - v2 (B-ROLL) clips: move, edge-trim, delete — overlay windows on
     output time, no ripple.
   - cc (CAPTIONS): move, trim, delete, text edit via the inspector.
   Edits stay client-side until ⟳ re-render POSTs the full desired
   state to /api/timeline/render (deterministic ffmpeg pipeline), or
   🎬 ask Kino drops a human-readable change list into the composer.

   Loaded AFTER app.js — reuses its globals when present (loadVideo,
   TEAMS, resolveTeamFolder) but degrades gracefully without them.
═══════════════════════════════════════════════════════════════ */

(() => {
  'use strict';

  const $id = (s) => document.getElementById(s);
  const dock = $id('timeline-dock');
  if (!dock) return;

  const runSelect = $id('kt-run-select');
  const videoSelect = $id('kt-video-select');
  const reloadBtn = $id('kt-reload');
  const dirtyBadge = $id('kt-dirty');
  const undoBtn = $id('kt-undo');
  const askBtn = $id('kt-ask-kino');
  const renderBtn = $id('kt-rerender');
  const zoomInBtn = $id('kt-zoom-in');
  const zoomOutBtn = $id('kt-zoom-out');
  const zoomFitBtn = $id('kt-zoom-fit');
  const tcEl = $id('kt-tc');
  const headersEl = $id('kt-headers');
  const lanesScroll = $id('kt-lanes-scroll');
  const lanesEl = $id('kt-lanes');
  const rulerEl = $id('kt-ruler');
  const playheadEl = $id('kt-playhead');
  const inspectorEl = $id('kt-inspector');
  const statusEl = $id('kt-status');
  const player = $id('player');

  const FPS = 30;
  const MIN_CLIP = 0.15; // seconds — can't trim a clip shorter than this

  // ── state ─────────────────────────────────────────────────────
  let manifest = null;        // live (edited) manifest
  let pristine = null;        // deep copy as loaded, for undo/diffing
  let pxPerSec = 20;
  let selected = null;        // {trackId, clipId}
  let dirty = { cut: false, broll: false, captions: false, titles: false, music: false, hook: false };
  let rendering = false;
  let tool = 'select';        // 'select' (V) | 'razor' (C) | 'text' (T)
  let uid = 0;                // unique suffix for clips created client-side

  const deep = (o) => JSON.parse(JSON.stringify(o));
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const newId = (prefix) => `${prefix}_n${++uid}`;

  // ── tools (V/C/T) ─────────────────────────────────────────────
  const TOOL_CURSOR = { select: '', razor: 'crosshair', text: 'text' };
  function setTool(t) {
    tool = t;
    lanesEl.style.cursor = TOOL_CURSOR[t] || '';
    document.querySelectorAll('.kt-tool').forEach((b) =>
      b.classList.toggle('active', b.dataset.tool === t));
    const overlay = $id('kt-monitor-overlay');
    if (overlay) overlay.hidden = t !== 'text';
    setStatus(t === 'razor' ? 'razor — click a clip to split it (V to exit)'
      : t === 'text' ? 'text — click the monitor or timeline to place a title (V to exit)'
      : '');
  }
  document.querySelectorAll('.kt-tool').forEach((b) =>
    b.addEventListener('click', () => setTool(b.dataset.tool)));
  window.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (document.activeElement && document.activeElement.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const k = e.key.toLowerCase();
    if (k === 'v') setTool('select');
    else if (k === 'c') setTool('razor');
    else if (k === 't') setTool('text');
  });

  function setStatus(msg, isErr) {
    if (!statusEl) return;
    statusEl.textContent = msg || '';
    statusEl.classList.toggle('err', !!isErr);
  }

  function tc(t) {
    t = Math.max(0, t || 0);
    const f = Math.floor((t % 1) * FPS);
    const s = Math.floor(t) % 60;
    const m = Math.floor(t / 60) % 60;
    const h = Math.floor(t / 3600);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(h)}:${p(m)}:${p(s)}:${p(f)}`;
  }

  function anyDirty() { return dirty.cut || dirty.broll || dirty.captions || dirty.titles || dirty.music || dirty.hook; }

  function refreshDirtyUI() {
    const d = anyDirty();
    if (dirtyBadge) dirtyBadge.hidden = !d;
    if (undoBtn) undoBtn.disabled = !d;
    if (askBtn) askBtn.disabled = !d;
    if (renderBtn) renderBtn.disabled = !d || rendering;
  }

  // ── run list ──────────────────────────────────────────────────
  async function refreshRuns(keepSelection) {
    try {
      const res = await fetch('/api/timeline/runs');
      const data = await res.json();
      const prev = keepSelection ? runSelect.value : '';
      runSelect.innerHTML = '<option value="">— pick an edit run —</option>';
      for (const r of data.runs || []) {
        const opt = document.createElement('option');
        opt.value = r.run;
        opt.textContent = (r.has_timeline ? '' : '▫ ') + r.run;
        opt.dataset.videos = JSON.stringify(r.videos || []);
        opt.dataset.variant = r.variant_run ? '1' : '';
        runSelect.appendChild(opt);
      }
      if (prev && [...runSelect.options].some((o) => o.value === prev)) {
        runSelect.value = prev;
      } else if (!keepSelection) {
        const first = (data.runs || []).find((r) => r.has_timeline);
        if (first) { runSelect.value = first.run; await loadRun(first.run); }
      }
    } catch (e) {
      setStatus('could not list runs: ' + e.message, true);
    }
  }

  // ── manifest load ─────────────────────────────────────────────
  async function loadRun(run, video) {
    if (!run) { manifest = pristine = null; renderAll(); return; }
    setStatus('loading timeline…');
    try {
      const q = new URLSearchParams({ run });
      if (video) q.set('video', video);
      const res = await fetch('/api/timeline/manifest?' + q.toString());
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      manifest = await res.json();
      pristine = deep(manifest);
      dirty = { cut: false, broll: false, captions: false, titles: false, music: false, hook: false };
      selected = null;

      // variant runs: expose per-variant picker
      const sv = manifest.sidecar_videos || [];
      videoSelect.hidden = sv.length === 0;
      if (sv.length) {
        videoSelect.innerHTML = '';
        for (const v of sv) {
          const o = document.createElement('option');
          o.value = v; o.textContent = v;
          videoSelect.appendChild(o);
        }
        if (video) videoSelect.value = video;
      }

      if (manifest.program) loadIntoPlayer(manifest.program);
      fitZoom();
      renderAll();
      // re-fit once layout settles — at boot the lanes-scroll can still be
      // 0-wide, which clamps pxPerSec to minimum and renders slivers.
      // setTimeout (not rAF) so it also fires in a hidden/background tab.
      setTimeout(() => { if (manifest && !anyDirty()) { fitZoom(); renderAll(); } }, 200);
      const e = manifest.editable || {};
      const readOnly = !e.cut && !e.broll && !e.captions && !e.titles;
      setStatus(manifest.duration
        ? `${(manifest.tracks || []).length} tracks · ${manifest.duration.toFixed(1)}s`
          + (readOnly ? ' · read-only (run predates edit support or is missing VO/EDL data)' : '')
        : 'no timeline data in this run');
    } catch (e) {
      manifest = pristine = null;
      renderAll();
      setStatus('timeline load failed: ' + e.message, true);
    }
    refreshDirtyUI();
  }

  function loadIntoPlayer(path) {
    try {
      if (typeof loadVideo === 'function') { loadVideo(path); return; }
    } catch (e) { /* fall through */ }
    if (player) player.src = '/api/file/' + path;
  }

  // ── zoom / geometry ───────────────────────────────────────────
  function duration() {
    if (!manifest) return 0;
    if (manifest.duration) return manifest.duration;
    let end = 0;
    for (const t of manifest.tracks || [])
      for (const c of t.clips) end = Math.max(end, c.end);
    return end || 60;
  }

  function fitZoom() {
    const w = lanesScroll ? lanesScroll.clientWidth - 24 : 800;
    pxPerSec = clamp(w / Math.max(1, duration()), 2, 400);
  }

  function laneWidth() { return Math.ceil(duration() * pxPerSec) + 60; }

  // ── rendering ─────────────────────────────────────────────────
  const TRACK_META = {
    t1: { cls: 'kt-t-titles' }, hk: { cls: 'kt-t-hook' },
    v2: { cls: 'kt-t-broll' }, v1: { cls: 'kt-t-acam' },
    a1: { cls: 'kt-t-audio' }, a2: { cls: 'kt-t-music' },
    cc: { cls: 'kt-t-caps' },
  };

  function renderAll() {
    headersEl.innerHTML = '';
    // wipe lanes except ruler + playhead
    [...lanesEl.querySelectorAll('.kt-lane')].forEach((n) => n.remove());
    renderRuler();
    if (!manifest) {
      lanesEl.style.width = '100%';
      renderInspector();
      return;
    }
    lanesEl.style.width = laneWidth() + 'px';

    // ruler header stub keeps the two columns aligned
    const stub = document.createElement('div');
    stub.className = 'kt-header kt-header-ruler';
    headersEl.appendChild(stub);

    for (const track of manifest.tracks || []) {
      const meta = TRACK_META[track.id] || { cls: '' };

      const head = document.createElement('div');
      head.className = 'kt-header ' + meta.cls;
      const badge = track.kind === 'hook' ? 'H'
        : track.kind === 'audio' ? 'A'
        : track.kind === 'captions' ? 'CC'
        : track.kind === 'titles' ? 'T' : 'V';
      head.innerHTML = `<span class="kt-h-badge">${badge}</span><span class="kt-h-label">${track.label}</span>`;
      headersEl.appendChild(head);

      const lane = document.createElement('div');
      lane.className = 'kt-lane ' + meta.cls;
      lane.dataset.trackId = track.id;
      for (const clip of track.clips) lane.appendChild(clipEl(track, clip));
      lanesEl.insertBefore(lane, playheadEl);
    }
    positionPlayhead();
    renderInspector();
  }

  function clipEl(track, clip) {
    const el = document.createElement('div');
    el.className = 'kt-clip';
    if (selected && selected.trackId === track.id && selected.clipId === clip.id)
      el.classList.add('selected');
    el.dataset.clipId = clip.id;
    el.style.left = clip.start * pxPerSec + 'px';
    el.style.width = Math.max(3, (clip.end - clip.start) * pxPerSec) + 'px';
    const body = document.createElement('span');
    body.className = 'kt-clip-label';
    body.textContent = clip.label || clip.id;
    el.title = (clip.label || '') + (clip.note ? '\n' + clip.note : '');
    el.appendChild(body);
    if (isEditable(track)) {
      const hl = document.createElement('span'); hl.className = 'kt-handle kt-handle-l';
      const hr = document.createElement('span'); hr.className = 'kt-handle kt-handle-r';
      el.appendChild(hl); el.appendChild(hr);
    }
    el.addEventListener('mousedown', (e) => onClipMouseDown(e, track, clip, el));
    el.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      // captions + titles: double-click = edit the text in place
      if ((track.id === 'cc' || track.id === 't1') && isEditable(track)) {
        select({ trackId: track.id, clipId: clip.id });
        const ta = $id(track.id === 'cc' ? 'kt-cap-text' : 'kt-title-text');
        if (ta) { ta.focus(); ta.select(); }
        return;
      }
      if (player) player.currentTime = clip.start + 0.01;
    });
    return el;
  }

  function isEditable(track) {
    const e = (manifest && manifest.editable) || {};
    if (track.id === 'v1' || track.id === 'a1') return !!e.cut && track.id === 'v1';
    if (track.id === 'v2') return !!e.broll;
    if (track.id === 'cc') return !!e.captions;
    if (track.id === 't1') return !!e.titles;
    if (track.id === 'a2') return !!e.music;
    if (track.id === 'hk') return !!e.hook;
    return false;
  }

  function timeAtEvent(e) {
    const rect = lanesEl.getBoundingClientRect();
    return clamp((e.clientX - rect.left) / pxPerSec, 0, duration());
  }

  function renderRuler() {
    rulerEl.innerHTML = '';
    const dur = duration();
    if (!dur) return;
    const steps = [0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120];
    const step = steps.find((s) => s * pxPerSec >= 56) || 300;
    for (let t = 0; t <= dur + step; t += step) {
      const tick = document.createElement('span');
      tick.className = 'kt-tick';
      tick.style.left = t * pxPerSec + 'px';
      tick.textContent = t >= 60
        ? `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, '0')}`
        : `${+t.toFixed(2)}s`;
      rulerEl.appendChild(tick);
    }
  }

  // ── playhead + seek ───────────────────────────────────────────
  function positionPlayhead() {
    if (!player || !playheadEl) return;
    playheadEl.style.left = (player.currentTime || 0) * pxPerSec + 'px';
    if (tcEl) tcEl.textContent = tc(player.currentTime);
  }
  if (player) {
    player.addEventListener('timeupdate', positionPlayhead);
    player.addEventListener('seeked', positionPlayhead);
  }

  let scrubbing = false;
  function seekFromEvent(e) {
    if (!player || !manifest) return;
    const rect = lanesEl.getBoundingClientRect();
    const t = clamp((e.clientX - rect.left) / pxPerSec, 0, duration());
    player.currentTime = t;
    positionPlayhead();
  }
  rulerEl.addEventListener('mousedown', (e) => { scrubbing = true; seekFromEvent(e); });
  lanesEl.addEventListener('mousedown', (e) => {
    if (e.target === lanesEl || e.target.classList.contains('kt-lane')) {
      if (tool === 'text') { createTitle(timeAtEvent(e), 0.5, 0.42); return; }
      scrubbing = true; seekFromEvent(e);
      select(null);
    }
  });

  // ── text tool: create a title ─────────────────────────────────
  function createTitle(t, x, y) {
    if (!manifest) return;
    const e = manifest.editable || {};
    if (!e.titles) { setStatus('titles are not editable on this run'); return; }
    let track = (manifest.tracks || []).find((tr) => tr.id === 't1');
    if (!track) {
      track = { id: 't1', kind: 'titles', label: 'TITLES', clips: [] };
      manifest.tracks.unshift(track);
    }
    const start = clamp(t, 0, Math.max(0, duration() - 0.5));
    const clip = {
      id: newId('t'),
      start: +start.toFixed(3),
      end: +Math.min(duration() || start + 3, start + 3).toFixed(3),
      label: 'Title', text: 'Title',
      x: +x.toFixed(3), y: +y.toFixed(3), size: 96, color: '#FFFFFF',
      note: '',
    };
    track.clips.push(clip);
    track.clips.sort((a, b) => a.start - b.start);
    markDirty(track);
    renderAll();
    select({ trackId: 't1', clipId: clip.id });
    const ta = $id('kt-title-text');
    if (ta) { ta.focus(); ta.select(); }
    setTool('select');
  }

  // monitor overlay — click the program monitor to place a title where
  // you clicked (letterbox-aware: maps the click into frame coordinates)
  const monitorOverlay = $id('kt-monitor-overlay');
  if (monitorOverlay && player) {
    monitorOverlay.addEventListener('mousedown', (e) => {
      if (tool !== 'text') return;
      const rect = monitorOverlay.getBoundingClientRect();
      const nw = player.videoWidth || 1080, nh = player.videoHeight || 1920;
      const scale = Math.min(rect.width / nw, rect.height / nh);
      const dw = nw * scale, dh = nh * scale;
      const ox = (rect.width - dw) / 2, oy = (rect.height - dh) / 2;
      const fx = clamp((e.clientX - rect.left - ox) / dw, 0, 1);
      const fy = clamp((e.clientY - rect.top - oy) / dh, 0, 1);
      createTitle(player.currentTime || 0, fx, fy);
    });
  }
  window.addEventListener('mousemove', (e) => { if (scrubbing) seekFromEvent(e); });
  window.addEventListener('mouseup', () => { scrubbing = false; });

  // ── selection + inspector ─────────────────────────────────────
  function select(sel) {
    selected = sel;
    lanesEl.querySelectorAll('.kt-clip.selected').forEach((n) => n.classList.remove('selected'));
    if (sel) {
      const lane = lanesEl.querySelector(`.kt-lane[data-track-id="${sel.trackId}"]`);
      const el = lane && lane.querySelector(`.kt-clip[data-clip-id="${sel.clipId}"]`);
      if (el) el.classList.add('selected');
    }
    renderInspector();
  }

  function findClip(sel) {
    if (!sel || !manifest) return {};
    const track = (manifest.tracks || []).find((t) => t.id === sel.trackId);
    const clip = track && track.clips.find((c) => c.id === sel.clipId);
    return { track, clip };
  }

  function renderInspector() {
    if (!inspectorEl) return;
    const { track, clip } = findClip(selected);
    if (!track || !clip) { inspectorEl.hidden = true; inspectorEl.innerHTML = ''; return; }
    inspectorEl.hidden = false;
    const editable = isEditable(track);
    const isCap = track.id === 'cc';
    const isTitle = track.id === 't1';
    const isHook = track.id === 'hk';
    const isMusic = track.id === 'a2';
    const src = clip.source ? `<span class="kt-i-src" title="${clip.source}">${clip.source}</span>` : '';
    const srcRange = clip.source_in != null && clip.source_out != null
      ? `<span class="kt-i-dim">src ${(+clip.source_in).toFixed(2)}–${(+clip.source_out).toFixed(2)}s</span>` : '';
    inspectorEl.innerHTML = `
      <span class="kt-i-track">${track.label}</span>
      <span class="kt-i-time">${clip.start.toFixed(2)}s → ${clip.end.toFixed(2)}s · ${(clip.end - clip.start).toFixed(2)}s</span>
      ${src} ${srcRange}
      ${clip.note ? `<span class="kt-i-note" title="${clip.note}">${clip.note}</span>` : ''}
      ${isCap && editable ? `<textarea id="kt-cap-text" rows="1">${clip.text || clip.label || ''}</textarea>
        <button id="kt-cap-apply" type="button">apply text</button>` : ''}
      ${isTitle && editable ? `<textarea id="kt-title-text" rows="1">${clip.text || ''}</textarea>
        <input type="number" id="kt-title-size" min="24" max="240" step="4" value="${clip.size || 96}" title="font size (pt)" />
        <input type="color" id="kt-title-color" value="${clip.color || '#FFFFFF'}" title="text color" />
        <button id="kt-title-apply" type="button">apply</button>` : ''}
      ${isHook && editable ? `<textarea id="kt-hook-text" rows="1">${clip.text || clip.label || ''}</textarea>
        <button id="kt-hook-apply" type="button">apply hook</button>` : ''}
      ${isMusic && editable ? `<button id="kt-music-swap" type="button">swap music…</button>` : ''}
      ${editable && !isHook ? `<button id="kt-clip-delete" type="button" class="danger">${isMusic ? '✕ remove music' : '✕ delete'}</button>` : ''}
    `;
    const del = $id('kt-clip-delete');
    if (del) del.addEventListener('click', () => deleteClip(track, clip));
    const capApply = $id('kt-cap-apply');
    if (capApply) capApply.addEventListener('click', () => {
      const ta = $id('kt-cap-text');
      if (!ta) return;
      clip.text = ta.value;
      clip.label = ta.value;
      markDirty(track);
      renderAll();
    });
    const titleApply = $id('kt-title-apply');
    if (titleApply) titleApply.addEventListener('click', () => {
      const ta = $id('kt-title-text');
      if (ta) { clip.text = ta.value; clip.label = ta.value; }
      const sz = $id('kt-title-size');
      if (sz) clip.size = clamp(parseInt(sz.value, 10) || 96, 24, 240);
      const col = $id('kt-title-color');
      if (col) clip.color = col.value;
      markDirty(track);
      renderAll();
    });
    const hookApply = $id('kt-hook-apply');
    if (hookApply) hookApply.addEventListener('click', () => {
      const ta = $id('kt-hook-text');
      if (ta) { clip.text = ta.value; clip.label = ta.value; }
      markDirty(track);
      renderAll();
    });
    const musicSwap = $id('kt-music-swap');
    if (musicSwap) musicSwap.addEventListener('click', () => {
      if (typeof window.openFolderPicker !== 'function') return;
      const startDir = (clip.source || '').replace(/[\\/][^\\/]*$/, '') || null;
      window.openFolderPicker(null, startDir, (p) => {
        clip.source = p; clip.source_key = p;
        clip.label = p.split(/[\\/]/).pop();
        markDirty(track);
        renderAll();
        select({ trackId: track.id, clipId: clip.id });
        setStatus('music swapped — re-render to apply');
      }, { files: true });
    });
  }

  // ── editing ───────────────────────────────────────────────────
  function markDirty(track) {
    if (track.id === 'v1' || track.id === 'a1') dirty.cut = true;
    else if (track.id === 'v2') dirty.broll = true;
    else if (track.id === 'cc') dirty.captions = true;
    else if (track.id === 't1') dirty.titles = true;
    else if (track.id === 'a2') dirty.music = true;
    else if (track.id === 'hk') dirty.hook = true;
    refreshDirtyUI();
  }

  // ── razor: split a clip at time t ─────────────────────────────
  function splitClip(track, clip, t) {
    if (t < clip.start + MIN_CLIP || t > clip.end - MIN_CLIP) {
      setStatus('too close to the clip edge to split there');
      return;
    }
    const offset = t - clip.start;
    const b = deep(clip);
    b.id = newId(clip.id);
    if (clip.source_in != null) {
      const cutSrc = (+clip.source_in) + offset;
      clip.source_out = cutSrc;
      b.source_in = cutSrc;
    }
    clip.end = +t.toFixed(3);
    b.start = +t.toFixed(3);
    const idx = track.clips.indexOf(clip);
    track.clips.splice(idx + 1, 0, b);
    if (track.id === 'v1') ripple();
    markDirty(track);
    renderAll();
    select({ trackId: track.id, clipId: b.id });
    setStatus(`split at ${t.toFixed(2)}s`);
  }

  // Re-pack v1 cumulatively after a trim/delete (the cut IS the program
  // timeline) and mirror onto a1.
  function ripple() {
    const v1 = (manifest.tracks || []).find((t) => t.id === 'v1');
    if (!v1) return;
    let cursor = 0;
    for (const c of v1.clips) {
      const d = (c.source_out - c.source_in);
      c.start = +cursor.toFixed(3);
      c.end = +(cursor + d).toFixed(3);
      cursor += d;
    }
    const a1 = (manifest.tracks || []).find((t) => t.id === 'a1');
    if (a1) a1.clips = v1.clips.map((c) => ({ ...c, id: c.id.replace('c', 'd') }));
    manifest.duration = cursor || manifest.duration;
  }

  function deleteClip(track, clip) {
    track.clips = track.clips.filter((c) => c.id !== clip.id);
    if (track.id === 'v1') ripple();
    markDirty(track);
    select(null);
    renderAll();
  }

  function onClipMouseDown(e, track, clip, el) {
    e.stopPropagation();
    select({ trackId: track.id, clipId: clip.id });
    if (!isEditable(track)) return;

    if (tool === 'razor') {
      splitClip(track, clip, timeAtEvent(e));
      return;
    }
    if (tool === 'text') return; // text tool creates on empty lane / monitor

    const mode = e.target.classList.contains('kt-handle-l') ? 'l'
      : e.target.classList.contains('kt-handle-r') ? 'r'
      : (track.id === 'v2' || track.id === 'cc' || track.id === 't1') ? 'move'
      : track.id === 'v1' ? 'reorder' : null;
    if (!mode) return;

    // v1 is a sequential concat — dragging a clip re-orders it, then the
    // ripple repacks everything back-to-back.
    if (mode === 'reorder') {
      const startX = e.clientX;
      let moved = false;
      const onMove = (ev) => {
        if (Math.abs(ev.clientX - startX) > 4) moved = true;
        if (moved) el.style.transform = `translateX(${ev.clientX - startX}px)`;
      };
      const onUp = (ev) => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        el.style.transform = '';
        if (!moved) return;
        const t = timeAtEvent(ev);
        const others = track.clips.filter((c) => c.id !== clip.id);
        let idx = others.length;
        let cursor = 0;
        for (let i = 0; i < others.length; i++) {
          const dur = (others[i].source_out - others[i].source_in);
          if (t < cursor + dur / 2) { idx = i; break; }
          cursor += dur;
        }
        others.splice(idx, 0, clip);
        track.clips = others;
        ripple();
        markDirty(track);
        renderAll();
        select({ trackId: track.id, clipId: clip.id });
        setStatus(`moved "${clip.label}" to position ${idx + 1}`);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      return;
    }

    const startX = e.clientX;
    const o = { start: clip.start, end: clip.end, sin: clip.source_in, sout: clip.source_out };
    let moved = false;

    function onMove(ev) {
      const dt = (ev.clientX - startX) / pxPerSec;
      if (Math.abs(ev.clientX - startX) > 2) moved = true;
      if (!moved) return;
      if (mode === 'move') {
        const d = o.end - o.start;
        clip.start = clamp(o.start + dt, 0, duration() - d);
        clip.end = clip.start + d;
      } else if (mode === 'l') {
        if (track.id === 'v1') {
          clip.source_in = clamp(o.sin + dt, 0, o.sout - MIN_CLIP);
        } else {
          clip.start = clamp(o.start + dt, 0, o.end - MIN_CLIP);
          if (clip.source_in != null) clip.source_in = o.sin + (clip.start - o.start);
        }
      } else if (mode === 'r') {
        if (track.id === 'v1') {
          clip.source_out = Math.max(o.sout + dt, clip.source_in + MIN_CLIP);
        } else {
          clip.end = Math.max(o.end + dt, o.start + MIN_CLIP);
          if (clip.source_out != null) clip.source_out = o.sout + (clip.end - o.end);
        }
      }
      if (track.id === 'v1') ripple();
      renderAll();
      select({ trackId: track.id, clipId: clip.id });
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      if (moved) { markDirty(track); renderAll(); select({ trackId: track.id, clipId: clip.id }); }
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Delete' && e.key !== 'Backspace') return;
    const tag = (document.activeElement && document.activeElement.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const { track, clip } = findClip(selected);
    if (track && clip && isEditable(track)) { e.preventDefault(); deleteClip(track, clip); }
  });

  // ── undo all ──────────────────────────────────────────────────
  if (undoBtn) undoBtn.addEventListener('click', () => {
    if (!pristine) return;
    manifest = deep(pristine);
    dirty = { cut: false, broll: false, captions: false, titles: false, music: false, hook: false };
    select(null);
    renderAll();
    refreshDirtyUI();
    setStatus('timeline restored to last render');
  });

  // ── change summary (shared by ask-Kino) ───────────────────────
  function changeSummary() {
    const lines = [];
    const byId = (m, tid) => ((m.tracks || []).find((t) => t.id === tid) || { clips: [] }).clips;
    for (const tid of ['v1', 'v2', 'cc', 't1']) {
      const before = byId(pristine, tid), after = byId(manifest, tid);
      const label = tid === 'v1' ? 'A-CAM cut' : tid === 'v2' ? 'B-roll'
        : tid === 't1' ? 'title' : 'captions';
      for (const a of after) {
        if (!before.some((b) => b.id === a.id))
          lines.push(`- ${label}: ADDED "${a.label || a.text}" at ${a.start.toFixed(2)}–${a.end.toFixed(2)}s`
            + (a.source ? ` (from ${a.source})` : ''));
      }
      for (const b of before) {
        const a = after.find((c) => c.id === b.id);
        if (!a) { lines.push(`- ${label}: DELETED "${b.label}" (was ${b.start.toFixed(2)}–${b.end.toFixed(2)}s)`); continue; }
        const timeChanged = Math.abs(a.start - b.start) > 0.01 || Math.abs(a.end - b.end) > 0.01
          || Math.abs((a.source_in ?? 0) - (b.source_in ?? 0)) > 0.01
          || Math.abs((a.source_out ?? 0) - (b.source_out ?? 0)) > 0.01;
        if (timeChanged)
          lines.push(`- ${label}: "${b.label}" ${b.start.toFixed(2)}–${b.end.toFixed(2)}s → ${a.start.toFixed(2)}–${a.end.toFixed(2)}s`
            + (a.source_in != null ? ` (source ${(+a.source_in).toFixed(2)}–${(+a.source_out).toFixed(2)}s)` : ''));
        if (tid === 'cc' && (a.text || a.label) !== (b.text || b.label))
          lines.push(`- caption at ${b.start.toFixed(2)}s: "${b.text || b.label}" → "${a.text || a.label}"`);
      }
    }
    return lines;
  }

  if (askBtn) askBtn.addEventListener('click', () => {
    if (!manifest || !anyDirty()) return;
    const lines = changeSummary();
    const promptEl = $id('prompt');
    if (!promptEl) return;
    promptEl.value =
      `I adjusted the timeline for the edit run "videos/edit/${manifest.run}" in the studio timeline. ` +
      `Please apply these changes by updating the EDLs/SRT in that folder and re-rendering (preview gate applies):\n\n` +
      lines.join('\n') +
      `\n\nEDLs live in that run folder (cut_edl.json / broll_edl.json / master.srt). Keep everything else identical.`;
    promptEl.focus();
    const tabBtn = document.querySelector('.tab[data-tab="chat"]');
    if (tabBtn) tabBtn.click();
    setStatus('change list dropped into the composer — review + send');
  });

  // ── deterministic re-render ───────────────────────────────────
  async function assetRootForTeam() {
    let root = '';
    try { root = localStorage.getItem('veditor.diceRoot.v1') || ''; } catch { /* */ }
    if (!root) return '';
    let team = '';
    try { team = localStorage.getItem('veditor.team.v1') || ''; } catch { /* */ }
    try {
      if (team && typeof resolveTeamFolder === 'function')
        return (await resolveTeamFolder(root, team)) || root;
    } catch { /* */ }
    return root;
  }

  if (renderBtn) renderBtn.addEventListener('click', async () => {
    if (!manifest || !anyDirty() || rendering) return;
    rendering = true;
    refreshDirtyUI();
    renderBtn.textContent = '⟳ rendering…';
    setStatus('re-rendering from timeline — this re-runs the ffmpeg pipeline…');
    try {
      const byId = (tid) => ((manifest.tracks || []).find((t) => t.id === tid) || { clips: [] }).clips;
      const payload = { run: manifest.run };
      if (manifest.sidecar_video) payload.video = manifest.sidecar_video;
      if (dirty.cut) payload.cut_ranges = byId('v1').map((c) => ({
        source: c.source_key || 'src',
        start: +(+c.source_in).toFixed(3),
        end: +(+c.source_out).toFixed(3),
        note: c.note || '',
      }));
      // a re-cut regenerates the base video, so the b-roll overlay must be
      // re-applied even when untouched — send it whenever the cut changes.
      if (dirty.broll || (dirty.cut && byId('v2').length)) payload.broll = byId('v2').map((c) => ({
        start: +c.start.toFixed(3), end: +c.end.toFixed(3),
        source: c.source, source_in: +(+c.source_in || 0).toFixed(3),
        note: c.note || '',
      }));
      if (dirty.captions) payload.captions = byId('cc').map((c) => ({
        start: +c.start.toFixed(3), end: +c.end.toFixed(3),
        text: c.text || c.label || '',
      }));
      if (dirty.titles) payload.titles = byId('t1').map((c) => ({
        start: +c.start.toFixed(3), end: +c.end.toFixed(3),
        text: c.text || c.label || '',
        x: +(c.x ?? 0.5), y: +(c.y ?? 0.42),
        size: c.size || 96, color: c.color || '#FFFFFF',
      }));
      // music: the a2 clip's source, or null when the bed was removed
      if (dirty.music) {
        const m = byId('a2');
        payload.music = m.length ? (m[0].source || null) : null;
      }
      // hook: the edited hook-band text
      if (dirty.hook) {
        const hk = byId('hk');
        payload.hook = hk.length ? (hk[0].text || hk[0].label || '') : '';
      }
      const root = await assetRootForTeam();
      if (root) payload.broll_root = root;

      const res = await fetch('/api/timeline/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      setStatus(`✔ rendered ${data.output.split('/').pop()} — loaded in the monitor`);
      loadIntoPlayer(data.output);
      dirty = { cut: false, broll: false, captions: false, titles: false, music: false, hook: false };
      pristine = deep(manifest);
    } catch (e) {
      setStatus('re-render failed: ' + e.message, true);
    } finally {
      rendering = false;
      renderBtn.textContent = '⟳ re-render';
      refreshDirtyUI();
    }
  });

  // ── toolbar wiring ────────────────────────────────────────────
  runSelect.addEventListener('change', () => loadRun(runSelect.value));
  videoSelect.addEventListener('change', () => loadRun(runSelect.value, videoSelect.value));
  if (reloadBtn) reloadBtn.addEventListener('click', () => refreshRuns(true).then(() => loadRun(runSelect.value, videoSelect.hidden ? undefined : videoSelect.value)));
  if (zoomInBtn) zoomInBtn.addEventListener('click', () => { pxPerSec = clamp(pxPerSec * 1.5, 2, 400); renderAll(); });
  if (zoomOutBtn) zoomOutBtn.addEventListener('click', () => { pxPerSec = clamp(pxPerSec / 1.5, 2, 400); renderAll(); });
  if (zoomFitBtn) zoomFitBtn.addEventListener('click', () => { fitZoom(); renderAll(); });

  // ── drag & drop assets onto the timeline ──────────────────────
  // Accepts in-app drags from the file panels (application/x-veditor-fspath,
  // set by app.js on every file row). Dropping a video:
  //  - on a sidecar run's v1 (concat) → inserts the full clip at that spot
  //  - on an EDL run's v2 (b-roll)    → adds a 3s cutaway window there
  const VIDEO_RX = /\.(mp4|mov|mkv|webm|m4v)$/i;

  function probeDropDuration(path) {
    return new Promise((res) => {
      const v = document.createElement('video');
      v.preload = 'metadata';
      const done = (d) => { v.removeAttribute('src'); res(d); };
      v.onloadedmetadata = () => done(v.duration && isFinite(v.duration) ? v.duration : 3);
      v.onerror = () => done(3);
      v.src = '/api/file/' + path;
    });
  }

  lanesEl.addEventListener('dragover', (e) => {
    if ([...(e.dataTransfer?.types || [])].includes('application/x-veditor-fspath')) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    }
  });

  lanesEl.addEventListener('drop', async (e) => {
    const path = e.dataTransfer?.getData('application/x-veditor-fspath');
    if (!path) return;
    e.preventDefault();
    if (!manifest) return;
    if (!VIDEO_RX.test(path)) { setStatus('only video files can be dropped on the timeline'); return; }
    const ed = manifest.editable || {};
    const t = timeAtEvent(e);
    const laneId = e.target.closest?.('.kt-lane')?.dataset?.trackId;

    // sidecar concat runs → insert into the v1 sequence
    if (manifest.variant && ed.cut) {
      const track = (manifest.tracks || []).find((tr) => tr.id === 'v1');
      if (!track) return;
      setStatus('probing dropped clip…');
      const dur = await probeDropDuration(path);
      const clip = {
        id: newId('drop'), start: 0, end: 0,
        label: path.split('/').pop(),
        source: path, source_key: path,
        source_in: 0, source_out: +dur.toFixed(3),
        note: 'dropped from files',
      };
      let idx = track.clips.length, cursor = 0;
      for (let i = 0; i < track.clips.length; i++) {
        const cdur = track.clips[i].source_out - track.clips[i].source_in;
        if (t < cursor + cdur / 2) { idx = i; break; }
        cursor += cdur;
      }
      track.clips.splice(idx, 0, clip);
      ripple();
      markDirty(track);
      renderAll();
      select({ trackId: 'v1', clipId: clip.id });
      setStatus(`inserted "${clip.label}" (${dur.toFixed(1)}s) at position ${idx + 1} — remember the VO length is fixed; trim to fit`);
      return;
    }

    // EDL runs → add a b-roll overlay window where dropped
    if (ed.broll && (laneId === 'v2' || !manifest.variant)) {
      const track = (manifest.tracks || []).find((tr) => tr.id === 'v2');
      if (!track) { setStatus('this run has no b-roll track'); return; }
      setStatus('probing dropped clip…');
      const dur = await probeDropDuration(path);
      const len = Math.min(3, dur);
      const clip = {
        id: newId('drop'),
        start: +clamp(t, 0, Math.max(0, duration() - len)).toFixed(3),
        end: 0, label: path.split('/').pop(),
        source: path, source_in: 0,
        note: 'dropped from files',
      };
      clip.end = +(clip.start + len).toFixed(3);
      track.clips.push(clip);
      track.clips.sort((a, b) => a.start - b.start);
      markDirty(track);
      renderAll();
      select({ trackId: 'v2', clipId: clip.id });
      setStatus(`added "${clip.label}" as a ${len.toFixed(1)}s cutaway at ${clip.start.toFixed(2)}s`);
      return;
    }
    setStatus('this run does not accept dropped clips (read-only or unsupported track)');
  });

  // Re-fit when the window (or the split panes) resize the dock.
  let resizeT = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => { if (manifest && !anyDirty()) { fitZoom(); renderAll(); } }, 150);
  });

  // Warn before leaving with unsaved timeline edits.
  window.addEventListener('beforeunload', (e) => {
    if (anyDirty()) { e.preventDefault(); e.returnValue = ''; }
  });

  // ── boot ──────────────────────────────────────────────────────
  refreshRuns(false);
})();
