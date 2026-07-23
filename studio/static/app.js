// KINOKORE / Veditor Studio — client. Streams claude CLI stream-json over SSE,
// drives the chat / presets / sessions tabs, handles uploads + file browser,
// tracks per-prompt / per-tab / project usage, and shows live thinking status.

// Global error trap — surfaces uncaught exceptions in a fixed banner so a
// silent script failure never leaves the user with dead buttons. Without
// this, a single throw during init breaks everything below it and gives
// the user no signal as to why.
(function installErrorTrap() {
  const show = (label, msg, src, line) => {
    let banner = document.getElementById('__kk_err_banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = '__kk_err_banner';
      banner.style.cssText = [
        'position:fixed', 'left:0', 'right:0', 'top:0', 'z-index:99999',
        'background:#3a0010', 'color:#ffd0d6', 'border-bottom:2px solid #ff3060',
        'font-family:monospace', 'font-size:12px', 'padding:6px 12px',
        'white-space:pre-wrap', 'box-shadow:0 4px 12px rgba(0,0,0,0.6)',
      ].join(';');
      document.body && document.body.appendChild(banner);
    }
    const at = src ? ` @ ${src.split('/').pop()}:${line || '?'}` : '';
    banner.textContent = `[${label}] ${msg}${at}\n(open DevTools → Console for full stack)`;
  };
  window.addEventListener('error', (e) => {
    show('JS error', e.message || 'unknown', e.filename, e.lineno);
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason || {};
    show('Promise', r.message || String(r));
  });
})();

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---- Resizable splits (Split.js) ----
function makeSplit(selectors, opts) {
  if (typeof Split !== 'function') return;
  const els = selectors.map((s) => document.querySelector(s));
  if (els.some((e) => !e)) return;
  const KEY = opts.storageKey;
  let sizes = opts.defaultSizes;
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '');
    if (Array.isArray(saved) && saved.length === selectors.length) sizes = saved;
  } catch {}
  Split(selectors, {
    sizes,
    minSize:    opts.minSize    || 100,
    direction:  opts.direction  || 'horizontal',
    gutterSize: 6,
    snapOffset: 0,
    onDragEnd: (s) => {
      try { localStorage.setItem(KEY, JSON.stringify(s)); } catch {}
    },
  });
}

(function initSplits() {
  // 0. Content panel (left) + right panel — two columns split horizontally.
  makeSplit(['.content-panel', 'aside.right'], {
    storageKey: 'veditor.mainSplit2.v1',
    defaultSizes: [62, 38],
    minSize: [320, 280],
  });
  // 2. Inside chat panel: history (top) ↔ input region (bottom)
  makeSplit(
    ['.tab-panel[data-panel="chat"] > .chat-history-pane',
     '.tab-panel[data-panel="chat"] > .chat-input-pane'],
    {
      storageKey: 'veditor.chatSplit.v1',
      defaultSizes: [60, 40],
      minSize: [120, 180],
      direction: 'vertical',
    }
  );
  // 3. Inside right column: sources+sync (top) ↔ player+files (bottom)
  makeSplit(
    ['aside.right > .right-top-pane', 'aside.right > .right-bottom-pane'],
    {
      storageKey: 'veditor.rightSplit.v1',
      defaultSizes: [45, 55],
      minSize: [180, 200],
      direction: 'vertical',
    }
  );
})();

// ============================================================
// FS BROWSER — left-side filesystem pane
// ============================================================
const fsRoots       = document.getElementById('fs-roots');
const fsBreadcrumb  = document.getElementById('fs-breadcrumb');
const fsList        = document.getElementById('fs-list');
const fsRefreshBtn  = document.getElementById('fs-refresh-btn');

const FS_LAST_PATH_KEY = 'veditor.fsLastPath.v1';
const FS_DRAG_MIME = 'application/x-veditor-fspath';
let fsCurrentPath = null;

function fmtSize(bytes) {
  if (!bytes) return '';
  const u = ['B', 'K', 'M', 'G', 'T'];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v < 10 && i > 0 ? `${v.toFixed(1)}${u[i]}` : `${Math.round(v)}${u[i]}`;
}

async function loadFsRoots() {
  fsRoots.innerHTML = '';
  try {
    const r = await fetch('/api/fs/roots');
    const data = await r.json();
    const make = (label, path, cls) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = `fs-root-btn ${cls || ''}`;
      b.textContent = label;
      b.dataset.path = path;
      b.addEventListener('click', () => loadFsList(path));
      fsRoots.appendChild(b);
    };
    for (const p of (data.pinned || [])) make(p.label, p.path, 'pinned');
    for (const d of (data.drives || [])) make(d.label, d.path);
  } catch (e) {
    fsRoots.innerHTML = '<span class="fs-error">failed to list roots</span>';
  }
}

function renderBreadcrumb(path) {
  fsBreadcrumb.innerHTML = '';
  if (!path) return;
  const parts = path.replace(/\/+$/, '').split('/').filter(Boolean);
  // Reconstruct cumulative paths so each segment is clickable.
  let acc = path.startsWith('/') ? '' : '';
  // On Windows, the first part is like "D:" — keep as-is for the path.
  const isWin = /^[A-Za-z]:$/.test(parts[0] || '');
  parts.forEach((seg, i) => {
    if (i === 0) {
      acc = isWin ? `${seg}/` : `/${seg}`;
    } else {
      acc = acc.endsWith('/') ? `${acc}${seg}` : `${acc}/${seg}`;
    }
    const span = document.createElement('span');
    span.className = 'crumb';
    span.textContent = seg;
    span.dataset.path = acc;
    span.addEventListener('click', () => loadFsList(acc));
    if (i > 0) {
      const sep = document.createElement('span');
      sep.className = 'sep';
      sep.textContent = '/';
      fsBreadcrumb.appendChild(sep);
    }
    fsBreadcrumb.appendChild(span);
  });
}

function renderFsRow(entry) {
  const li = document.createElement('li');
  li.className = `fs-row ${entry.is_dir ? 'is-dir' : 'fs-file'}`;
  const icon = document.createElement('span');
  icon.className = 'fs-icon';
  icon.textContent = entry.is_dir ? '📁' : (
    /^(mp4|mov|mkv|webm|m4v)$/.test(entry.ext) ? '🎬' :
    /^(wav|mp3|m4a|aac|flac|ogg|opus)$/.test(entry.ext) ? '🎵' :
    /^(txt|md|srt|vtt|json)$/.test(entry.ext) ? '📄' : '·'
  );
  const name = document.createElement('span');
  name.className = 'fs-name';
  name.textContent = entry.name;
  const size = document.createElement('span');
  size.className = 'fs-size';
  size.textContent = entry.is_dir ? '' : fmtSize(entry.size);
  li.append(icon, name, size);

  if (entry.is_dir) {
    li.addEventListener('click', () => loadFsList(entry.path));
  } else {
    li.draggable = true;
    li.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData(FS_DRAG_MIME, entry.path);
      e.dataTransfer.setData('text/plain', entry.path);
      e.dataTransfer.effectAllowed = 'copy';
      // Highlight the dropzones so the user knows where they can drop.
      document.querySelectorAll('.dropzone').forEach((dz) =>
        dz.classList.add('in-app-drop-target')
      );
    });
    li.addEventListener('dragend', () => {
      document.querySelectorAll('.dropzone').forEach((dz) =>
        dz.classList.remove('in-app-drop-target')
      );
    });
    // Double-click also imports (no drag needed).
    li.addEventListener('dblclick', () => importFsFile(entry.path));
  }
  return li;
}

async function loadFsList(path) {
  fsCurrentPath = path;
  try { localStorage.setItem(FS_LAST_PATH_KEY, path); } catch {}
  // Highlight active root.
  document.querySelectorAll('.fs-root-btn').forEach((b) => {
    b.classList.toggle('active', path.startsWith(b.dataset.path));
  });
  fsList.innerHTML = '<li class="fs-empty">loading…</li>';
  try {
    const r = await fetch(`/api/fs/list?path=${encodeURIComponent(path)}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      fsList.innerHTML = `<li class="fs-error">${err.detail || 'error'}</li>`;
      return;
    }
    const data = await r.json();
    renderBreadcrumb(data.path);
    fsList.innerHTML = '';
    if (data.parent && data.parent !== data.path) {
      const up = document.createElement('li');
      up.className = 'fs-row fs-up';
      up.innerHTML = '<span class="fs-icon">↑</span><span class="fs-name">.. (up)</span><span class="fs-size"></span>';
      up.addEventListener('click', () => loadFsList(data.parent));
      fsList.appendChild(up);
    }
    if (!data.entries.length) {
      const e = document.createElement('li');
      e.className = 'fs-empty';
      e.textContent = 'empty folder';
      fsList.appendChild(e);
      return;
    }
    for (const entry of data.entries) {
      fsList.appendChild(renderFsRow(entry));
    }
  } catch (e) {
    fsList.innerHTML = `<li class="fs-error">failed: ${e.message}</li>`;
  }
}

async function importFsFile(absPath) {
  try {
    const r = await fetch('/api/fs/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: absPath }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(`import failed: ${err.detail || r.status}`);
      return null;
    }
    const data = await r.json();
    // Refresh the file list so dropdowns pick it up.
    if (typeof refreshFiles === 'function') refreshFiles();
    return data;
  } catch (e) {
    alert(`import failed: ${e.message}`);
    return null;
  }
}

// fs-refresh-btn was removed when the Files tab was redesigned (2026-05-07).
// The new tab uses #files-refresh-btn handled inside initFilesTab(). Guard
// for older HTML versions just in case.
if (fsRefreshBtn) {
  fsRefreshBtn.addEventListener('click', () => {
    if (fsCurrentPath) loadFsList(fsCurrentPath);
    else loadFsRoots();
  });
}

// Boot the FS pane.
loadFsRoots().then(() => {
  let last = null;
  try { last = localStorage.getItem(FS_LAST_PATH_KEY); } catch {}
  if (last) loadFsList(last);
});

// ---- Completion chime ----------------------------------------------------
// Plays a short two-note "ding" via Web Audio when a run finishes. No asset
// file (CSP-safe, works offline). Muteable; preference persists.
const DING_KEY = 'veditor.ding.v1';
let dingEnabled = (() => { try { return localStorage.getItem(DING_KEY) !== '0'; } catch { return true; } })();
let _audioCtx = null;
function playDing() {
  if (!dingEnabled) return;
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const ctx = _audioCtx;
    if (ctx.state === 'suspended') ctx.resume();
    const now = ctx.currentTime;
    // Two ascending notes (C6 → E6) — a pleasant, unmistakable "done" chime.
    [[1046.5, 0], [1318.5, 0.14]].forEach(([freq, t]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      const start = now + t;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.25, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.45);
      osc.connect(gain).connect(ctx.destination);
      osc.start(start);
      osc.stop(start + 0.5);
    });
  } catch (e) { /* audio not available — silent no-op */ }
}
(function initDingToggle() {
  const btn = document.getElementById('ding-toggle');
  if (!btn) return;
  const render = () => {
    btn.textContent = dingEnabled ? '🔔' : '🔕';
    btn.classList.toggle('muted', !dingEnabled);
    btn.title = dingEnabled ? 'Chime on render finish — ON (click to mute)'
                            : 'Chime on render finish — OFF (click to enable)';
  };
  btn.addEventListener('click', () => {
    dingEnabled = !dingEnabled;
    try { localStorage.setItem(DING_KEY, dingEnabled ? '1' : '0'); } catch {}
    render();
    if (dingEnabled) playDing();  // preview + unlock the AudioContext on user gesture
  });
  render();
})();

const conversation = $('#conversation');
const promptForm = $('#prompt-form');
const promptInput = $('#prompt');
const sendBtn = $('#send-btn');
const stopBtn = $('#stop-btn');
const continueToggle = $('#continue-session');
const modelSelect = $('#model-select');

// Restore the last-used model from localStorage; default to Opus.
const MODEL_KEY = 'veditor.model.v1';
modelSelect.value = (() => {
  try { return localStorage.getItem(MODEL_KEY) || 'opus'; } catch (e) { return 'opus'; }
})();
modelSelect.addEventListener('change', () => {
  try { localStorage.setItem(MODEL_KEY, modelSelect.value); } catch (e) { /* */ }
  // Keep sidebar select in sync
  const sbSel = $('#sb-model-select');
  if (sbSel) sbSel.value = modelSelect.value;
});

// Sidebar model select — two-way sync with the main #model-select
(function initSidebarModel() {
  const sbSel = $('#sb-model-select');
  if (!sbSel) return;
  sbSel.value = modelSelect.value;
  sbSel.addEventListener('change', () => {
    modelSelect.value = sbSel.value;
    try { localStorage.setItem(MODEL_KEY, sbSel.value); } catch (e) { /* */ }
  });
})();

const dzVideo = $('#dz-video');
const dzAudio = $('#dz-audio');
const dzScript = $('#dz-script');
const fileInputVideo = $('#file-input-video');
const fileInputAudio = $('#file-input-audio');
const fileInputScript = $('#file-input-script');
const uploadProgressVideo = $('#upload-progress-video');
const uploadProgressAudio = $('#upload-progress-audio');
const uploadProgressScript = $('#upload-progress-script');
const pairStatus = $('#pair-status');
const syncDetectBtn = $('#sync-detect-btn');
const syncApplyBtn = $('#sync-apply-btn');
const syncVideoSelect = $('#sync-video-select');
const syncAudioSelect = $('#sync-audio-select');
const syncModelSelect = $('#sync-model-select');
const composerVideoSelect = $('#composer-video-select');
const composerAudioSelect = $('#composer-audio-select');
const composerScriptSelect = $('#composer-script-select');
const composerMusicSelect = $('#composer-music-select');
const composerVoSelect = $('#composer-vo-select');
const composerModelSelect = $('#composer-model-select');
// B-roll mode + folder live in the B-roll tab now (not the composer).
const composerQualitySelect = $('#composer-quality-select');
const composerFreeform = $('#composer-freeform');
const composerBuildBtn = $('#composer-build-btn');
const composerClearBtn = $('#composer-clear-btn');
const composerStatus = $('#composer-status');

// Sync-specific model preference. Defaults to Sonnet — sync is mechanical
// work that doesn't benefit from Opus's reasoning, and the savings are ~5×.
const SYNC_MODEL_KEY = 'veditor.sync_model.v1';
const _initialSyncModel = (() => {
  try { return localStorage.getItem(SYNC_MODEL_KEY) || 'sonnet'; }
  catch (e) { return 'sonnet'; }
})();
syncModelSelect.value = _initialSyncModel;

// All sync-style mechanical workflows (sync tool, auto-pair batch form, take
// selector) share the same model preference. Changing one mirrors to all.
function setSyncModel(value) {
  if (!value) return;
  syncModelSelect.value = value;
  const batchModelSelect = document.getElementById('batch-model-select');
  if (batchModelSelect) batchModelSelect.value = value;
  if (composerModelSelect) composerModelSelect.value = value;
  try { localStorage.setItem(SYNC_MODEL_KEY, value); } catch (e) { /* */ }
}
syncModelSelect.addEventListener('change', () => setSyncModel(syncModelSelect.value));
composerModelSelect.addEventListener('change', () => setSyncModel(composerModelSelect.value));
composerModelSelect.value = _initialSyncModel;
// The batch dropdown is in the DOM at parse time but bind defensively after
// the rest of the script wires up its handlers.
queueMicrotask(() => {
  const batchModelSelect = document.getElementById('batch-model-select');
  if (batchModelSelect) {
    batchModelSelect.value = _initialSyncModel;
    batchModelSelect.addEventListener('change', () => setSyncModel(batchModelSelect.value));
  }
});

// One-shot model override for the next prompt submission. Sync actions set
// this so they use the sync model (Sonnet by default) without permanently
// changing the global MODEL dropdown in the prompt bar.
let nextSubmitModelOverride = null;
// When true, the next /api/chat submit is a self-contained autonomous batch
// (variants / streamlined / dice): let the SERVER route the model (→ Sonnet)
// and skip --continue, instead of forcing the picker's model. Set by the batch
// launchers here + window.kfRouteNextSubmit() for streamlined.js.
let nextSubmitRouted = false;
window.kfRouteNextSubmit = () => { nextSubmitRouted = true; };
const player = $('#player');
const playerMeta = $('#player-meta');
const fileList = $('#file-list');
const refreshBtn = $('#refresh-files');
const healthEl = $('#health');
const statusBar = $('#status-bar');
const statusText = $('#status-text');
const statusElapsed = $('#status-elapsed');

let activeAbort = null;
let currentLoaded = null;

// Sync tool state. The selected video/audio are read directly from the dropdowns
// (which are populated from /api/files). `pair` only persists sync RESULTS so
// the UI can show "synced · offset +1.234s" after a successful detect+apply.
const PAIR_KEY = 'veditor.sync_results.v2';
const pair = loadPair();

function loadPair() {
  try {
    const raw = localStorage.getItem(PAIR_KEY);
    if (raw) return Object.assign(blankPair(), JSON.parse(raw));
  } catch (e) { /* */ }
  return blankPair();
}
function blankPair() {
  return {
    lastSyncedVideo: null,
    lastSyncedAudio: null,
    offsetSeconds: null,
    syncedPath: null,
    confidence: null,
  };
}
function savePair() {
  try { localStorage.setItem(PAIR_KEY, JSON.stringify(pair)); } catch (e) { /* */ }
}

// -------------- helpers --------------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function fmtNum(n) {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k';
  return (n / 1_000_000).toFixed(2) + 'M';
}
function fmtSize(n) {
  if (!n) return '';
  if (n < 1024) return `${n}B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)}K`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)}M`;
  return `${(n / 1073741824).toFixed(2)}G`;
}
function fmtCost(n) {
  return '$' + (n || 0).toFixed(n >= 1 ? 2 : 4);
}
function fmtAgo(tsMs) {
  const d = (Date.now() - tsMs) / 1000;
  if (d < 60) return `${d.toFixed(0)}s ago`;
  if (d < 3600) return `${(d / 60).toFixed(0)}m ago`;
  if (d < 86400) return `${(d / 3600).toFixed(1)}h ago`;
  return new Date(tsMs).toLocaleString();
}

// -------------- tabs --------------
$$('.tab').forEach((btn) => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});
function activateTab(name) {
  $$('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === name));
  if (name === 'sessions') renderSessionList();
  if (name === 'jobs') refreshJobs();
  if (name === 'logs') {
    logUnreadCount = 0;
    document.getElementById('logs-badge').hidden = true;
  }
}

// -------------- usage meters --------------
const USAGE_KEY = 'veditor.usage.v1';
const usage = loadUsage();
let lastPrompt = '';
let promptUsage = { tokens: 0, cost: 0 };

function loadUsage() {
  try {
    const raw = localStorage.getItem(USAGE_KEY);
    if (raw) return Object.assign(blankUsage(), JSON.parse(raw));
  } catch (e) { /* ignore */ }
  return blankUsage();
}
function blankUsage() {
  return { inTokens: 0, outTokens: 0, cacheRead: 0, cacheCreate: 0, costUsd: 0, turns: 0 };
}
function saveUsage() {
  try { localStorage.setItem(USAGE_KEY, JSON.stringify(usage)); } catch (e) { /* quota */ }
}
function renderUsage() {
  $('#u-in').textContent = fmtNum(usage.inTokens);
  $('#u-out').textContent = fmtNum(usage.outTokens);
  $('#u-cache').textContent = fmtNum(usage.cacheRead + usage.cacheCreate);
  $('#u-cost').textContent = fmtCost(usage.costUsd);
}
function renderPromptUsage() {
  $('#p-tok').textContent = fmtNum(promptUsage.tokens);
  $('#p-cost').textContent = fmtCost(promptUsage.cost);
}
function flashUsage(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 600);
}
function resetUsage() {
  Object.assign(usage, blankUsage());
  renderUsage();
  saveUsage();
}
$('#u-reset').addEventListener('click', resetUsage);

async function refreshLifetime() {
  try {
    const r = await fetch('/api/usage/summary');
    const j = await r.json();
    $('#l-turns').textContent = fmtNum(j.turns || 0);
    $('#l-tokens').textContent = fmtNum((j.in || 0) + (j.out || 0) + (j.cache_read || 0) + (j.cache_create || 0));
    $('#l-cost').textContent = fmtCost(j.cost_usd || 0);
  } catch (e) { /* server may be restarting */ }
}
async function logTurn(turnData) {
  const fd = new FormData();
  Object.entries(turnData).forEach(([k, v]) => fd.append(k, String(v)));
  try {
    await fetch('/api/usage', { method: 'POST', body: fd });
    refreshLifetime();
  } catch (e) { /* don't block UI */ }
}

// -------------- status bar (live thinking) --------------
let statusStartedAt = 0;
let lastEventAt = 0;
let statusTimer = null;
let lastActivity = '';
const STALE_THRESHOLD_MS = 30 * 1000;  // 30s with no events = "still running…"

function startStatus() {
  statusBar.hidden = false;
  statusBar.classList.remove('stale');
  statusStartedAt = Date.now();
  lastEventAt = Date.now();
  lastActivity = 'thinking…';
  statusText.textContent = lastActivity;
  $('#usage-prompt').classList.add('live');
  if (statusTimer) clearInterval(statusTimer);
  statusTimer = setInterval(() => {
    const now = Date.now();
    const elapsed = (now - statusStartedAt) / 1000;
    const sinceEvent = (now - lastEventAt) / 1000;
    const fmt = (s) => s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${(s % 60).toFixed(0)}s`;
    statusElapsed.textContent = fmt(elapsed);
    if (sinceEvent * 1000 >= STALE_THRESHOLD_MS) {
      statusBar.classList.add('stale');
      statusText.textContent = `${lastActivity} — no events for ${fmt(sinceEvent)} (still running)`;
    } else {
      statusBar.classList.remove('stale');
      statusText.textContent = lastActivity;
    }
  }, 250);
}
function stopStatus() {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  statusBar.hidden = true;
  statusBar.classList.remove('stale');
  $('#usage-prompt').classList.remove('live');
}
function setActivity(text) {
  lastActivity = text;
  lastEventAt = Date.now();
  statusText.textContent = text;
}
function noteEvent() {
  lastEventAt = Date.now();
}

// ============================================================
// JOBS / EFFICIENCY TRACKER
// ============================================================
//
// Each user prompt → 1 job. Operations are auto-detected from the agent's
// Bash tool calls. Server stores at videos/edit/jobs.jsonl, and aggregates
// efficiency stats per operation type.

let activeJob = null;

function newJobId() {
  return 'j_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

/** Inspect a tool_use event and return any operation tags it implies. */
function detectOperations(toolName, toolInput) {
  if (toolName !== 'Bash' && toolName !== 'PowerShell') return [];
  const cmd = (toolInput && (toolInput.command || '')).toString();
  const ops = new Set();
  if (cmd.includes('match_pairs.py'))   ops.add('auto_pair_sync');
  if (cmd.includes('sync_audio.py'))    ops.add('audio_sync');
  if (cmd.includes('transcribe.py'))    ops.add('transcribe');
  if (cmd.includes('pack_transcripts.py')) ops.add('pack_transcripts');
  if (cmd.includes('tts_voice.py'))     ops.add('tts_voice');
  if (cmd.includes('tts_music.py'))     ops.add('tts_music');
  if (cmd.includes('level_audio.py'))   ops.add('level_audio');
  if (cmd.includes('best_take.py'))     ops.add('best_take');
  if (cmd.includes('broll_overlay.py')) ops.add('broll_overlay');
  if (cmd.includes('graphics_overlay.py')) ops.add('graphics_overlay');
  if (cmd.includes('heygen_video.py'))  ops.add('heygen_video');
  if (cmd.includes('grade.py'))         ops.add('color_grade');
  if (cmd.includes('split_hooks.py'))   ops.add('split_hooks');
  if (cmd.includes('render.py')) {
    ops.add('render');
    if (cmd.includes('--build-subtitles')) ops.add('captions');
    if (cmd.includes('--draft')) ops.add('preview_render');
    if (cmd.includes('--preview')) ops.add('preview_render');
  }
  if (/\bffmpeg\b/.test(cmd)) {
    if (/transpose=/.test(cmd)) ops.add('rotate');
    if (/subtitles=/.test(cmd)) ops.add('captions');
    if (/crop=/.test(cmd)) ops.add('crop');
    if (/scale=/.test(cmd) && !cmd.includes('render.py')) ops.add('resize');
    if (/loudnorm/.test(cmd)) ops.add('audio_normalize');
    if (/silenceremove/.test(cmd)) ops.add('silence_cut');
    if (/afftdn|arnndn/.test(cmd)) ops.add('audio_denoise');
    if (/h264_nvenc|libx264/.test(cmd) && /-c:v/.test(cmd)) ops.add('encode');
    // Rough: a bare ffmpeg with no other tag is some kind of render
    if (ops.size === 0) ops.add('ffmpeg_other');
  }
  if (cmd.includes('hyperframes')) ops.add('motion_graphics');
  return Array.from(ops);
}

/** Detect output files in a tool_result content blob — heuristic. */
function detectOutputFiles(content) {
  if (!content) return [];
  const text = typeof content === 'string' ? content : JSON.stringify(content);
  const re = /videos\/edit\/[\w\-./]+\.(mp4|mov|webm|mkv)/gi;
  return Array.from(new Set(text.match(re) || []));
}

async function startJob(prompt) {
  const id = newJobId();
  activeJob = {
    id,
    prompt,
    started_at: new Date().toISOString(),
    operations: [],
    turns: 0,
    cost_usd: 0,
    tokens_in: 0,
    tokens_out: 0,
    tokens_cache: 0,
    wall_clock_start: Date.now(),
    output_files: new Set(),
    // Record the model that will actually be used for THIS submission. The
    // sync override (if set) takes priority over the global MODEL dropdown,
    // matching the precedence in submitChat. This is read here BEFORE the
    // override is consumed, so jobs.jsonl tags reflect what really ran.
    model: nextSubmitModelOverride || modelSelect.value || '',
  };
  try {
    await fetch('/api/jobs/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id, prompt, started_at: activeJob.started_at, model: activeJob.model,
        // Mirror submitChat's routing decision so jobs.jsonl logs the model
        // that's actually spawned (server re-routes when force_model is false).
        force_model: !nextSubmitRouted,
      }),
    });
  } catch (e) { /* server may not be restarted yet */ }
}

function recordOpInJob(op, tool, durMs) {
  if (!activeJob) return;
  activeJob.operations.push({
    op, tool, duration_ms: durMs || 0,
    ts: new Date().toISOString(),
  });
}

function recordOutputs(files) {
  if (!activeJob) return;
  files.forEach((f) => activeJob.output_files.add(f));
}

async function finalizeJob() {
  if (!activeJob) return;
  const wall = Date.now() - activeJob.wall_clock_start;
  const payload = {
    id: activeJob.id,
    completed_at: new Date().toISOString(),
    operations: activeJob.operations,
    output_files: Array.from(activeJob.output_files),
    turns: activeJob.turns,
    cost_usd: activeJob.cost_usd,
    tokens_in: activeJob.tokens_in,
    tokens_out: activeJob.tokens_out,
    tokens_cache: activeJob.tokens_cache,
    wall_clock_ms: wall,
  };
  try {
    await fetch('/api/jobs/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) { /* ignore */ }
  activeJob = null;
  refreshJobs();
}

function fmtMs(ms) {
  if (!ms) return '—';
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

async function refreshJobs() {
  try {
    const r = await fetch('/api/jobs');
    if (!r.ok) return;
    const j = await r.json();
    renderOpsTable(j.by_operation || {});
    renderJobsList(j.jobs || []);
    // Workflow-category summary (added 2026-05-07). Defined later in the
    // file as window.renderWorkflowSummary by the initWorkflowSummary IIFE.
    if (typeof window.renderWorkflowSummary === 'function') {
      window.renderWorkflowSummary(j.jobs || []);
    }
  } catch (e) { /* server may not be restarted */ }
}

function renderOpsTable(byOp) {
  const tbody = $('#ops-table tbody');
  tbody.innerHTML = '';
  const entries = Object.entries(byOp).sort((a, b) => b[1].total_cost - a[1].total_cost);
  if (!entries.length) {
    const tr = el('tr', 'empty-row');
    const td = el('td');
    td.colSpan = 6;
    td.textContent = 'no completed jobs yet';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  entries.forEach(([op, s]) => {
    const tr = el('tr');
    tr.appendChild(el('td', null, op));
    tr.appendChild(el('td', null, String(s.n)));
    tr.appendChild(el('td', null, '$' + (s.avg_cost || 0).toFixed(4)));
    tr.appendChild(el('td', null, fmtMs(s.avg_wall_ms)));
    tr.appendChild(el('td', null, String(s.avg_turns)));
    tr.appendChild(el('td', null, '$' + (s.total_cost || 0).toFixed(2)));
    tbody.appendChild(tr);
  });
}

function renderJobsList(jobs) {
  const list = $('#jobs-list');
  list.innerHTML = '';
  jobs
    .slice()
    .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''))
    .slice(0, 50)
    .forEach((j) => list.appendChild(renderJobCard(j)));
  if (!jobs.length) {
    list.appendChild(el('li', null, '— no jobs yet —'));
  }
}

function renderJobCard(job) {
  const li = el('li', 'job-card ' + (job.completed_at ? 'done' : 'open'));
  const promptText = (job.prompt || '').slice(0, 280) + ((job.prompt || '').length > 280 ? '…' : '');
  li.appendChild(el('div', 'jc-prompt', promptText || '(no prompt recorded)'));

  const meta = el('div', 'jc-meta');
  if (job.completed_at) meta.appendChild(el('span', null, fmtAgo(new Date(job.completed_at).getTime())));
  else meta.appendChild(el('span', null, 'IN PROGRESS'));
  meta.appendChild(el('span', 'jcm-cost', '$' + (job.cost_usd || 0).toFixed(4)));
  meta.appendChild(el('span', 'jcm-wall', fmtMs(job.wall_clock_ms)));
  meta.appendChild(el('span', null, `${job.turns || 0} turns`));
  if (job.model) meta.appendChild(el('span', null, job.model));
  li.appendChild(meta);

  // Operation chips — unique per job
  const ops = Array.from(new Set((job.operations || []).map((o) => o.op))).filter(Boolean);
  if (ops.length) {
    const opsRow = el('div', 'jc-ops');
    ops.forEach((op) => opsRow.appendChild(el('span', `op-tag ${op}`, op)));
    li.appendChild(opsRow);
  }

  // Output file links
  if (job.output_files && job.output_files.length) {
    const files = el('div', 'jc-files');
    files.appendChild(el('span', null, '→ '));
    job.output_files.forEach((f) => {
      const a = document.createElement('a');
      a.href = '/api/file/' + f;
      a.target = '_blank';
      a.textContent = f.replace(/^videos\//, '');
      files.appendChild(a);
    });
    li.appendChild(files);
  }

  return li;
}

$('#jobs-refresh-btn').addEventListener('click', refreshJobs);

// -------------- logs (raw stream-json viewer) --------------
const MAX_LOG_ENTRIES = 2000;
const logsList = $('#logs-list');
const logsBadge = $('#logs-badge');
const logsOnlyTools = $('#logs-only-tools');
const logsAutoscroll = $('#logs-autoscroll');
let logEntries = [];
let logUnreadCount = 0;

function pushLog(evt) {
  const ts = new Date();
  const stamp = ts.toLocaleTimeString('en-US', { hour12: false }) + '.' +
    String(ts.getMilliseconds()).padStart(3, '0');
  let primaryType = evt.type || '?';
  let summary = '';

  if (evt.type === 'assistant' && evt.message) {
    const blocks = evt.message.content || [];
    const text = blocks.filter((b) => b.type === 'text').map((b) => b.text).join(' ');
    const tools = blocks.filter((b) => b.type === 'tool_use').map((b) => `${b.name}(...)`);
    summary = (tools.length ? `[tool_use] ${tools.join(', ')} ` : '') + (text || '').slice(0, 400);
    if (tools.length) primaryType = 'tool_use';
  } else if (evt.type === 'user' && evt.message) {
    const blocks = evt.message.content || [];
    const tr = blocks.find((b) => b.type === 'tool_result');
    if (tr) {
      const content = typeof tr.content === 'string' ? tr.content : JSON.stringify(tr.content);
      summary = content.slice(0, 400);
      primaryType = 'tool_result';
    } else {
      summary = JSON.stringify(evt.message).slice(0, 400);
    }
  } else if (evt.type === 'result') {
    summary = `${evt.subtype || 'success'} · $${(evt.total_cost_usd ?? 0).toFixed(4)} · ${(evt.duration_ms || 0)}ms · ${evt.usage?.input_tokens || 0}+${evt.usage?.output_tokens || 0} tok`;
  } else if (evt.type === 'system') {
    summary = `[${evt.subtype || 'system'}]` + (evt.session_id ? ` session=${evt.session_id.slice(0, 8)}` : '');
  } else if (evt.type === '_done') {
    summary = `exit ${evt.exit_code}` + (evt.stderr ? ` · stderr: ${evt.stderr.slice(0, 200)}` : '');
    primaryType = evt.exit_code === 0 ? 'done' : 'error';
  } else {
    summary = JSON.stringify(evt).slice(0, 400);
  }

  const entry = { stamp, type: primaryType, summary, raw: evt };
  logEntries.push(entry);
  if (logEntries.length > MAX_LOG_ENTRIES) logEntries.shift();

  // Render this one if filter allows.
  appendLogRow(entry);

  // Badge if user is on a different tab.
  const onLogs = document.querySelector('.tab[data-tab="logs"]').classList.contains('active');
  if (!onLogs) {
    logUnreadCount += 1;
    logsBadge.hidden = false;
    logsBadge.textContent = String(logUnreadCount);
  }
}

function appendLogRow(entry) {
  const onlyTools = logsOnlyTools.checked;
  const isToolish = ['tool_use', 'tool_result'].includes(entry.type);
  if (onlyTools && !isToolish) return;

  const row = el('div', 'log-entry');
  row.appendChild(el('span', 'lt-time', entry.stamp));
  row.appendChild(el('span', `lt-type t-${entry.type.replace(/[^a-z_]/g, '')}`, entry.type));
  row.appendChild(el('span', 'lt-summary', entry.summary || ''));
  row.addEventListener('click', () => {
    if (row.classList.contains('expanded')) {
      row.classList.remove('expanded');
      row.querySelector('.lt-summary').textContent = entry.summary;
    } else {
      row.classList.add('expanded');
      row.querySelector('.lt-summary').textContent = JSON.stringify(entry.raw, null, 2);
    }
  });
  logsList.appendChild(row);
  if (logsAutoscroll.checked) logsList.scrollTop = logsList.scrollHeight;
}

function rerenderLogs() {
  logsList.innerHTML = '';
  for (const e of logEntries) appendLogRow(e);
}

function clearLogs() {
  logEntries = [];
  logsList.innerHTML = '';
  logUnreadCount = 0;
  logsBadge.hidden = true;
}

logsOnlyTools.addEventListener('change', rerenderLogs);
$('#logs-clear-btn').addEventListener('click', clearLogs);
$('#logs-copy-btn').addEventListener('click', async () => {
  const text = logEntries.map((e) =>
    `[${e.stamp}] ${e.type}  ${JSON.stringify(e.raw)}`
  ).join('\n');
  try { await navigator.clipboard.writeText(text); } catch (e) { /* clipboard blocked */ }
});

// -------------- conversation rendering --------------
function clearEmpty() {
  const empty = conversation.querySelector('.empty');
  if (empty) empty.remove();
}
function addMsg(role, body) {
  clearEmpty();
  const wrap = el('div', `msg ${role}`);
  wrap.appendChild(el('div', 'role', role));
  wrap.appendChild(el('div', 'body', body));
  conversation.appendChild(wrap);
  conversation.scrollTop = conversation.scrollHeight;
  return wrap.querySelector('.body');
}

// -------------- health --------------
async function checkHealth() {
  try {
    const r = await fetch('/api/health');
    const j = await r.json();
    if (j.claude_found) {
      healthEl.textContent = `claude ✓  ${j.project_root}`;
      healthEl.classList.add('ok'); healthEl.classList.remove('err');
    } else {
      healthEl.textContent = `claude CLI not found`;
      healthEl.classList.add('err'); healthEl.classList.remove('ok');
    }
  } catch (e) {
    healthEl.textContent = 'server unreachable';
    healthEl.classList.add('err');
  }
}

// -------------- file browser --------------
const VIDEO_RX = /\.(mp4|mov|webm|m4v|mkv)$/i;
const AUDIO_RX = /\.(mp3|wav|m4a|aac|flac|ogg)$/i;
const SCRIPT_RX = /\.(txt|md)$/i;

async function refreshFiles() {
  try {
    const r = await fetch('/api/files');
    const j = await r.json();
    fileList.innerHTML = '';

    if (j.sources.length) {
      fileList.appendChild(el('li', 'group', 'sources'));
      j.sources.forEach((f) => fileList.appendChild(fileItem(f, 'source')));
    }
    if (j.artifacts.length) {
      fileList.appendChild(el('li', 'group', 'edit/'));
      j.artifacts.forEach((f) => fileList.appendChild(fileItem(f, f.kind || 'artifact')));
    }
    if (!j.sources.length && !j.artifacts.length) {
      fileList.appendChild(el('li', 'group', '— empty —'));
    }
    if (!currentLoaded) {
      const firstVideo = [...j.artifacts, ...j.sources].find((f) => VIDEO_RX.test(f.path));
      if (firstVideo) loadVideo(firstVideo.path);
    }
    // Populate sync dropdowns from the same file inventory.
    populateSyncSelects([...j.sources, ...j.artifacts]);
    refreshSyncUI();
    refreshComposerUI();
  } catch (e) { console.error(e); }
}
function fileItem(f, kind) {
  const li = el('li');
  const name = el('span', 'fname', f.name || f.path);
  const meta = el('span', 'fkind', `${kind} · ${fmtSize(f.size)}`);
  li.appendChild(name);
  li.appendChild(meta);
  li.dataset.path = f.path;
  if (currentLoaded === f.path) li.classList.add('active');
  if (VIDEO_RX.test(f.path)) {
    li.addEventListener('click', () => loadVideo(f.path));
    li.title = 'click to load in player';
  } else {
    li.title = f.path;
    li.addEventListener('click', () => window.open(`/api/file/${f.path}`, '_blank'));
  }
  return li;
}
function loadVideo(path) {
  currentLoaded = path;
  player.src = `/api/file/${path}`;
  playerMeta.textContent = path;
  $$('.file-list li').forEach((el) =>
    el.classList.toggle('active', el.dataset.path === path)
  );
}

// -------------- transient upload (drop zones reset after each file) --------------
const DZ_BY_KIND = { video: () => dzVideo, audio: () => dzAudio, script: () => dzScript };
const PROGRESS_BY_KIND = {
  video: () => uploadProgressVideo,
  audio: () => uploadProgressAudio,
  script: () => uploadProgressScript,
};
async function uploadOne(file, kind) {
  const dz = DZ_BY_KIND[kind]();
  const progressEl = PROGRESS_BY_KIND[kind]();
  const fd = new FormData();
  fd.append('file', file);
  progressEl.hidden = false;
  progressEl.textContent = `uploading ${file.name}…`;
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    if (!r.ok) throw new Error(await r.text());
    const j = await r.json();
    progressEl.textContent = `✓ ${j.name} (${fmtSize(j.size)})`;
    addMsg('system', `Uploaded ${kind}: ${j.path}`);
    dz.classList.add('flash');

    await refreshFiles();
    // Auto-select the just-uploaded file in the matching tool so the
    // user's intent flows naturally into whichever workflow needs it.
    if (kind === 'video') {
      syncVideoSelect.value = j.path;
      composerVideoSelect.value = j.path;
      loadVideo(j.path);
    } else if (kind === 'audio') {
      syncAudioSelect.value = j.path;
      // Audio uploads are also valid music/VO candidates — leave those
      // dropdowns alone (they're explicit choices), but they'll appear
      // in the dropdown list after refreshFiles populates them.
    } else if (kind === 'script') {
      composerScriptSelect.value = j.path;
    }
    refreshSyncUI();
    refreshComposerUI();

    // Reset the dropzone back to "drop here" — the file lives in the file list now.
    setTimeout(() => {
      progressEl.hidden = true;
      progressEl.textContent = '';
      dz.classList.remove('flash');
    }, 1800);
  } catch (e) {
    progressEl.textContent = `upload failed: ${e.message}`;
  }
}

/** Populate the sync + composer dropdowns from the project's file list. */
function populateSyncSelects(files) {
  const videos = files.filter((f) => VIDEO_RX.test(f.path));
  const audios = files.filter((f) => AUDIO_RX.test(f.path));
  const scripts = files.filter((f) => SCRIPT_RX.test(f.path));
  fillSelect(syncVideoSelect, videos, '— pick a video —');
  fillSelect(syncAudioSelect, audios, '— pick an audio —');
  fillSelect(composerVideoSelect, videos, '— pick a video —');
  fillSelect(composerAudioSelect, audios, '— use video audio —');
  fillSelect(composerScriptSelect, scripts, '— none —');
  // Music + VO both sit in the audio file pool. Eventually each will gain a
  // "(generate via ElevenLabs)" sentinel — until then they share inventory.
  fillSelect(composerMusicSelect, audios, '— none —');
  fillSelect(composerVoSelect, audios, '— none —');
}

function refreshComposerUI() {
  const v = composerVideoSelect.value;
  if (v) {
    composerStatus.textContent = 'pipeline armed — your prompt will be wrapped on send';
    composerStatus.className = 'st-status ready';
  } else {
    composerStatus.textContent = 'optional · pick a video to wrap your prompt as a pipeline';
    composerStatus.className = 'st-status';
  }
  if (composerBuildBtn) composerBuildBtn.disabled = !v;
}
function fillSelect(sel, items, placeholder) {
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  const opt0 = document.createElement('option');
  opt0.value = '';
  opt0.textContent = placeholder;
  sel.appendChild(opt0);
  items.forEach((f) => {
    const opt = document.createElement('option');
    opt.value = f.path;
    opt.textContent = f.path.replace(/^videos\//, '');
    sel.appendChild(opt);
  });
  // Preserve prior selection if still valid.
  if (prev && [...sel.options].some((o) => o.value === prev)) {
    sel.value = prev;
  }
}

function refreshSyncUI() {
  const v = syncVideoSelect.value;
  const a = syncAudioSelect.value;
  const both = !!(v && a);
  syncDetectBtn.disabled = !both;
  syncApplyBtn.disabled = !both;

  // Status: "ready", "synced", or hint about what's missing.
  const sameAsLastSync =
    v === pair.lastSyncedVideo && a === pair.lastSyncedAudio &&
    pair.syncedPath && pair.offsetSeconds != null;

  if (sameAsLastSync) {
    pairStatus.textContent =
      `synced · offset ${pair.offsetSeconds >= 0 ? '+' : ''}${pair.offsetSeconds.toFixed(3)}s` +
      (pair.confidence != null ? ` · conf ${pair.confidence.toFixed(2)}` : '');
    pairStatus.className = 'st-status synced';
  } else if (both) {
    pairStatus.textContent = 'ready to sync';
    pairStatus.className = 'st-status ready';
  } else if (v && !a) {
    pairStatus.textContent = 'pick an audio file →';
    pairStatus.className = 'st-status';
  } else if (!v && a) {
    pairStatus.textContent = '← pick a video file';
    pairStatus.className = 'st-status';
  } else {
    pairStatus.textContent = 'pick a video + an audio file';
    pairStatus.className = 'st-status';
  }
}

syncVideoSelect.addEventListener('change', refreshSyncUI);
syncAudioSelect.addEventListener('change', refreshSyncUI);

function buildSyncPrompt({ apply }) {
  const v = syncVideoSelect.value;
  const a = syncAudioSelect.value;
  const lines = [];
  lines.push(`Run the audio-sync helper on this dual-source pair:`);
  lines.push('');
  lines.push(`- Primary video (with on-board audio): \`${v}\``);
  lines.push(`- Secondary audio (better mic):        \`${a}\``);
  lines.push('');
  if (apply) {
    const baseName = v.split('/').pop().replace(/\.[^.]+$/, '');
    const outPath = `videos/${baseName}_synced.mp4`;
    lines.push(`Detect the offset AND build the synced MP4:`);
    lines.push('```');
    lines.push(`PYTHONUTF8=1 PATH="$FFMPEG_DIR:$PATH" \\`);
    lines.push(`  $VU_PY \\`);
    lines.push(`  video-use/helpers/sync_audio.py \\`);
    lines.push(`  "${v}" "${a}" \\`);
    lines.push(`  --apply --out "${outPath}" --json`);
    lines.push('```');
    lines.push('');
    lines.push(`After it succeeds, parse the JSON and:`);
    lines.push(`1. Read back the \`offset_seconds\` and \`confidence\`.`);
    lines.push(`2. Confirm \`confidence >= 1.3\` — if not, warn me before proceeding.`);
    lines.push(`3. The synced file at \`${outPath}\` is now the primary source for any further edits — its on-board audio is muted and the secondary mic is the only audio track.`);
    lines.push(`4. Update the EDL's \`sources\` to point at the synced file (or build a fresh EDL if there isn't one yet).`);
    lines.push(`5. Report the offset, confidence, and synced file path back to me.`);
  } else {
    lines.push(`Detect-only (don't bake an MP4 yet):`);
    lines.push('```');
    lines.push(`PYTHONUTF8=1 PATH="$FFMPEG_DIR:$PATH" \\`);
    lines.push(`  $VU_PY \\`);
    lines.push(`  video-use/helpers/sync_audio.py \\`);
    lines.push(`  "${v}" "${a}" --json`);
    lines.push('```');
    lines.push('');
    lines.push(`Report the offset and confidence. If confidence is high (≥ 2.0) I'll likely want you to apply it; if it's borderline (1.3–2.0) suggest I check; if it's low (<1.3) warn that the two recordings probably don't overlap.`);
  }
  lines.push('');
  lines.push(`Don't touch transcripts or subtitles in this turn.`);
  return lines.join('\n');
}

function sendSyncPrompt({ apply }) {
  try {
    if (!syncVideoSelect.value || !syncAudioSelect.value) {
      alert('Pick a video and an audio file in the sync tool first.');
      return;
    }
    const prompt = buildSyncPrompt({ apply });
    if (!prompt || prompt.split('\n').length < 5) {
      throw new Error('sync prompt assembly produced unexpectedly short output');
    }
    // Remember which pair we're operating on so the status line can show
    // "synced · offset …" once it's done.
    pair.lastSyncedVideo = syncVideoSelect.value;
    pair.lastSyncedAudio = syncAudioSelect.value;
    pair.offsetSeconds = null;
    pair.syncedPath = null;
    pair.confidence = null;
    savePair();
    pairStatus.textContent = 'syncing…';
    pairStatus.className = 'st-status busy';
    promptInput.value = prompt;
    nextSubmitModelOverride = syncModelSelect.value || 'sonnet';
    skipPipelineWrap = true;
    activateTab('chat');
    promptForm.requestSubmit();
  } catch (e) {
    console.error('audio sync failed:', e);
    alert(`audio sync failed: ${e.message}`);
  }
}

syncDetectBtn.addEventListener('click', () => sendSyncPrompt({ apply: false }));
syncApplyBtn.addEventListener('click', () => sendSyncPrompt({ apply: true }));

// -------------- batch sync (auto-pair folder) --------------
const batchRow = $('#sync-batch-row');
const batchForm = $('#sync-batch-form');
const batchFolderSelect = $('#batch-folder-select');
const batchFolderInput = $('#batch-folder-input');
const batchThresholdInput = $('#batch-threshold-input');
const batchScanResult = $('#batch-scan-result');
const batchRunBtn = $('#batch-run-btn');
const batchScanBtn = $('#batch-scan-btn');
let lastScannedFolder = null;

function chosenBatchFolder() {
  // Manual path takes priority if filled; otherwise use the dropdown.
  const typed = batchFolderInput.value.trim();
  if (typed) return typed.replace(/^\/+|\/+$/g, '');
  return batchFolderSelect.value || 'videos';
}

async function refreshBatchFolders() {
  try {
    const r = await fetch('/api/folders');
    if (!r.ok) return;
    const j = await r.json();
    batchFolderSelect.innerHTML = '';
    (j.folders || []).forEach((f) => {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = f;
      batchFolderSelect.appendChild(opt);
    });
    if (j.default) batchFolderSelect.value = j.default;
  } catch (e) { /* server may not be restarted yet */ }
}

async function scanBatchFolder() {
  const folder = chosenBatchFolder();
  batchScanResult.classList.add('shown');
  batchScanResult.innerHTML = `<span>scanning <code>${folder}</code>…</span>`;
  batchRunBtn.disabled = true;
  try {
    const r = await fetch('/api/folder/scan?path=' + encodeURIComponent(folder));
    if (!r.ok) {
      const txt = await r.text();
      batchScanResult.innerHTML = `<span class="bsr-err">${r.status}: ${txt}</span>`;
      return;
    }
    const j = await r.json();
    const nv = j.videos.length;
    const na = j.audios.length;
    let html = '';
    const displayPath = j.absolute_folder || j.folder;
    if (nv && na) {
      html += `<span class="bsr-ok">found ${nv} video${nv === 1 ? '' : 's'} + ${na} audio file${na === 1 ? '' : 's'} in <code>${displayPath}</code></span>`;
      batchRunBtn.disabled = false;
      // Use the absolute path for the agent prompt (works whether inside or outside project).
      lastScannedFolder = j.absolute_folder || j.folder;
    } else if (!nv && !na) {
      html += `<span class="bsr-warn">no video or audio files in <code>${displayPath}</code></span>`;
    } else if (!nv) {
      html += `<span class="bsr-warn">no videos in <code>${displayPath}</code> (only ${na} audio file${na === 1 ? '' : 's'})</span>`;
    } else {
      html += `<span class="bsr-warn">no audio files in <code>${displayPath}</code> (only ${nv} video${nv === 1 ? '' : 's'})</span>`;
    }
    if (nv) {
      html += '<ul>' + j.videos.map((f) => `<li>🎬 ${f.name} <span style="color:var(--fg-mute)">(${fmtSize(f.size)})</span></li>`).join('') + '</ul>';
    }
    if (na) {
      html += '<ul>' + j.audios.map((f) => `<li>🎤 ${f.name} <span style="color:var(--fg-mute)">(${fmtSize(f.size)})</span></li>`).join('') + '</ul>';
    }
    if (nv && na && j.in_project === false) {
      html += `<div style="margin-top:6px;color:var(--fg-mute);font-size:10px;">note: this folder is outside the project. Synced output files will land next to each source — not in the studio's file browser. Open them from your OS file explorer when done.</div>`;
    }
    batchScanResult.innerHTML = html;
  } catch (e) {
    batchScanResult.innerHTML = `<span class="bsr-err">${e.message}</span>`;
  }
}

function buildBatchSyncPrompt(folder, threshold) {
  // Normalize separators. Detect absolute paths (Windows drive-letter or Unix
  // root) and pass them through as-is; otherwise resolve under the project.
  const norm = folder.replace(/\\/g, '/');
  const isAbsolute = /^[A-Za-z]:[\\/]/.test(folder) || /^\//.test(norm);
  const winFolder = isAbsolute ? norm : ('' + norm);
  const lines = [];
  lines.push(`Auto-pair every video in \`${folder}\` with its matching audio file.`);
  lines.push('');
  lines.push(`Use \`match_pairs.py\` with \`--audio-continuous\` — Sean's DJI lav rolls continuously across multiple takes (one long audio file covers a whole batch), so each video should map to its single best-match audio above threshold and audio files can be shared across videos. Without this flag, sync_audio takes the wrong ffmpeg branch for offset > video_duration and silently produces 19 KB empty MP4s (failure mode caught 2026-04-30, see memory).`);
  lines.push('');
  lines.push(`**Important pre-step:** if the folder already contains \`*_synced.mp4\` files from a previous run, delete them first or move them aside. The matcher's folder scan doesn't filter them out, so they'll be treated as new "videos" to sync and produce \`*_synced_synced.mp4\` garbage.`);
  lines.push('');
  lines.push(`**Match and sync in one pass — no approval gate, no dry run.** Run match_pairs.py with \`--apply\` directly:`);
  lines.push('```');
  lines.push(`PYTHONUTF8=1 PATH="$FFMPEG_DIR:$PATH" \\`);
  lines.push(`  $VU_PY \\`);
  lines.push(`  video-use/helpers/match_pairs.py \\`);
  lines.push(`  --folder "${winFolder}" \\`);
  lines.push(`  --threshold ${threshold} --audio-continuous --level-dialogue --apply --json`);
  lines.push('```');
  lines.push('');
  lines.push(`Each accepted pair encodes to \`<video_stem>_synced.mp4\` next to its source using the current sync_audio defaults (NVENC CFR re-encode, auto-trim-head ≥0.5s) AND **peak-levels the dialogue to land between -6 and -3 dBFS** (linear gain — the DJI lav records around -12 dBFS, this brings it up to broadcast dialogue spec without ever dropping levels below the target window). Do not pause to ask — just run it.`);
  lines.push('');
  lines.push(`**Skip rule (hard):** if a video has no audio match above the threshold, leave it alone — do NOT fall back to its scratch audio, do NOT pick the next-best below-threshold pair, do NOT prompt me to choose. Just report it under "unpaired videos" and move on. Unpaired = untouched.`);
  lines.push('');
  lines.push(`After encoding, \`ffprobe\` each output's duration — if it's <5% of the source size or <1 second, the empty-MP4 bug bit; investigate before declaring success. The matcher also records each output's measured peak dBFS in \`videos/edit/sync_log.jsonl\`; any \`output_in_target_window: false\` entry should be flagged in the summary.`);
  lines.push('');
  lines.push(`Then give me a summary:`);
  lines.push(`- Each synced pair: video ↔ audio with offset, confidence, and output path`);
  lines.push(`- Unpaired videos (skipped, untouched)`);
  lines.push(`- Unpaired audios (likely unused takes)`);
  lines.push(`- Any suspicious pairs (confidence between ${threshold} and 2.0) — flag these so I can spot-check the result, but they ARE synced`);
  lines.push('');
  lines.push(`Note: the DJI mic typically rolls continuously across multiple video takes, so offsets in the tens-of-seconds-to-minutes range are normal and not a sign of a bad match. Trust the confidence score, not gut intuition about how big the offset "should" be.`);
  return lines.join('\n');
}

function showBatchForm() {
  batchRow.hidden = true;
  batchForm.hidden = false;
  batchScanResult.classList.remove('shown');
  batchScanResult.innerHTML = '';
  batchRunBtn.disabled = true;
  refreshBatchFolders();
}
function hideBatchForm() {
  batchForm.hidden = true;
  batchRow.hidden = false;
  batchScanResult.classList.remove('shown');
}

$('#sync-batch-btn').addEventListener('click', showBatchForm);
$('#batch-cancel-btn').addEventListener('click', hideBatchForm);
$('#batch-scan-btn').addEventListener('click', scanBatchFolder);
batchFolderSelect.addEventListener('change', () => {
  batchFolderInput.value = '';  // clear the manual override
  scanBatchFolder();
});
batchFolderInput.addEventListener('change', scanBatchFolder);
batchRunBtn.addEventListener('click', () => {
  const folder = lastScannedFolder || chosenBatchFolder();
  const threshold = parseFloat(batchThresholdInput.value) || 1.5;
  promptInput.value = buildBatchSyncPrompt(folder, threshold);
  hideBatchForm();
  // Auto-pair is also a sync workflow — use the sync model.
  nextSubmitModelOverride = syncModelSelect.value || 'sonnet';
  skipPipelineWrap = true;
  activateTab('chat');
  promptForm.requestSubmit();
});

// ============================================================
// PIPELINE COMPOSER
// ============================================================
function buildPipelinePrompt({ video, audio, script, music, vo, freeform, brollMode, brollFolder, pendingVo, quality }) {
  const lines = [];
  const isMedium = quality === 'medium';
  const teamId   = (typeof loadTeam === 'function') ? loadTeam() : '';
  const team     = teamId && (typeof TEAMS !== 'undefined') ? TEAMS[teamId] : null;

  lines.push('## Pipeline build — autonomous execution, NO approval gates, NO preview gates, NO permission asks');
  lines.push('');
  lines.push('**HARD RULE — RUN STRAIGHT THROUGH.** Once every input listed under Assets is verified to exist, execute every step below back-to-back without pausing. Do NOT ask "should I proceed?", do NOT render a 10-second preview and wait, do NOT ask which mode to use, do NOT confirm encoder choices. The user has pre-authorized the entire pipeline by clicking Send. The only legitimate stop condition is a hard failure (file missing, ffmpeg exits non-zero, low-confidence sync below 1.3) — and even then, report and stop, do not ask for permission to retry.');
  lines.push('');

  // --- Team / brand context ---
  // The Team picker in the sidebar applies to EVERY pipeline run, not just
  // Roll the Dice. If a team is selected, every creative decision must be
  // appropriate for that product line.
  if (team) {
    lines.push(`### Team / brand: ${team.name} · ${team.sub}`);
    lines.push(`This run is scoped to the **${team.name}** product line. ALL creative decisions — script copy, b-roll selections, music tone, caption phrasing, brand voice, terminology — must be appropriate for ${team.sub}. Do NOT mix references from other product lines.`);
    lines.push('');
  }

  // --- Per-run output folder ---
  // Every pipeline run gets its own subfolder under videos/edit/ so 46-deep
  // result lists don't pile up at the top level.
  {
    const stem = (video || 'run').split('/').pop().replace(/\.[^.]+$/, '');
    lines.push('### Output folders (HARD RULE)');
    lines.push(`Two per-run folders. Pick \`<RUN_TS>\` once (e.g. \`20260507-153022\`) and reuse it:`);
    lines.push(`\`\`\``);
    lines.push(`videos/edit/${stem}_<RUN_TS>/       ← working files: proxy MP4, EDL JSONs, SRT, intermediate cuts`);
    lines.push(`Final Output/${stem}_<RUN_TS>/       ← the finished deliverable MP4 ONLY`);
    lines.push(`\`\`\``);
    lines.push(`The finished video goes in \`Final Output/${stem}_<RUN_TS>/\` so completed deliverables are easy to find in the project root. Everything else (intermediates) stays under \`videos/edit/${stem}_<RUN_TS>/\`. The \`Final Output\` name contains a space — ALWAYS quote it in shell commands (\`"Final Output/..."\`) and \`mkdir -p\` it first. Do NOT scatter files at the top level of \`videos/edit/\`.`);
    lines.push('');
  }
  if (isMedium) {
    lines.push('**Mode: medium quality** — cost-optimized run, 1080p delivery. Follow the inlined rules below; do NOT open `docs/TRIMMING_PHILOSOPHY.md`, `docs/RENDER_EXPORT_PHILOSOPHY.md`, or `docs/MOTION_PHILOSOPHY.md` unless an unrecoverable edge case forces it (and say so explicitly if you do). Emit tool calls only — no prose narration between steps. End with a single 5-line summary: deliverable path, total wall time, ops count, anything notable, anything skipped.');
  } else {
    lines.push('**Mode: high quality (1080p)** — final delivery 1920×1080 (Meta-friendly).');
  }
  lines.push('');

  // --- Proxy-first downscale rule ---
  // Without this rule, the agent re-applies `scale=1920:1080` on every encode
  // pass (sync, best_take, EDL cut, graphics composite, caption burn) — a 6-min
  // 4K source can spend 15+ min wall time scaling the same frames 5 times. The
  // fix is the standard NLE workflow: downsize ONCE upfront to a 1080p proxy,
  // then run the entire pipeline at 1080p (downstream steps see 1080p input
  // and need no scale filter at all).
  lines.push('### Performance — proxy-first downscale (HARD RULE)');
  lines.push('');
  lines.push('Source footage is 4K. Final delivery is 1080p. **Downsize EXACTLY ONCE — at the top of the pipeline — and treat the 1080p result as the working source for every downstream step.** Do NOT re-apply `scale=1920:1080` in any later encode. Captions, graphics overlays, EDL cuts, and the final composite all run on 1080p data, so they need no scale filter.');
  lines.push('');
  lines.push('**Probe first.** If the source is already ≤1920×1080, skip the proxy step entirely and use the source as-is.');
  lines.push('');
  lines.push('**Proxy command** — single NVENC pass, audio stream-copied:');
  lines.push('```');
  lines.push('PATH="$FFMPEG_DIR:$PATH" ffmpeg -y -hwaccel cuda -i "<SOURCE>" \\');
  lines.push('  -vf "scale=1920:1080:flags=lanczos" \\');
  lines.push('  -c:v h264_nvenc -preset p4 -rc vbr -cq 19 -b:v 12M -maxrate 18M -bufsize 24M \\');
  lines.push('  -c:a copy -movflags +faststart \\');
  lines.push('  "videos/edit/<stem>_1080p.mp4"');
  lines.push('```');
  lines.push('Wall time on a 6-min clip: ~2-3 min on RTX-class NVENC. After this completes, **all downstream commands reference the `_1080p.mp4` file**, never the original 4K source.');
  lines.push('');
  lines.push('**Stream-copy when possible.** If a downstream operation only changes container or trims at keyframes, use `-c copy` (no re-encode). Re-encode only when filters require it (caption burn, overlay composite, dialogue level adjust on the same MP4 as video).');
  lines.push('');
  lines.push('**One ffmpeg invocation per logical step.** Don\'t chain three ffmpegs where one with `-filter_complex` would do. Each extra encode is another decode→re-encode round trip on every frame.');
  lines.push('');
  lines.push('**Background long jobs.** For any ffmpeg step you estimate >2 min, run it with `&` in bash, then `wait` on the PID. Don\'t poll status, don\'t stream `-progress`, don\'t narrate intermediate output. The user pays Claude tokens per second the agent is "watching" — your job is to fire-and-wait, then parse the result code.');
  lines.push('');

  // --- Assets ---
  lines.push('### Assets');
  lines.push(`- **Video:** \`${video}\``);
  if (audio)  lines.push(`- **External audio:** \`${audio}\`  ← sync this to the video before any other step`);
  if (script) lines.push(`- **Script:** \`${script}\``);
  if (music)  lines.push(`- **Music bed:** \`${music}\``);
  if (vo)     lines.push(`- **VO source:** \`${vo}\``);
  if (pendingVo) lines.push(`- **VO source (TO BE GENERATED — see step 0 below):** \`${pendingVo.output}\``);
  if (brollMode && brollMode !== 'none' && brollFolder) {
    lines.push(`- **B-roll root:** \`${brollFolder}\``);
  }
  lines.push('');

  // --- Step 0a: External audio sync (runs before everything else, including VO gen) ---
  if (audio) {
    const videoBase = video.split('/').pop().replace(/\.[^.]+$/, '');
    const syncOut   = `videos/edit/${videoBase}_synced.mp4`;
    lines.push('### Step 0a — Sync external audio (run this FIRST, before any other step)');
    lines.push('');
    lines.push('An external audio source was provided (lav mic / DJI recorder). Sync it to the video using the exact same process as the Audio Sync tool:');
    lines.push('');
    lines.push('```');
    lines.push('PYTHONUTF8=1 PATH="$FFMPEG_DIR:$PATH" \\');
    lines.push('  $VU_PY \\');
    lines.push('  video-use/helpers/sync_audio.py \\');
    lines.push(`  "${video}" \\`);
    lines.push(`  "${audio}" \\`);
    lines.push(`  --apply --out "${syncOut}" \\`);
    lines.push('  --audio-continuous --auto-trim-head --level-dialogue --json');
    lines.push('```');
    lines.push('');
    lines.push('After the sync completes:');
    lines.push(`1. Parse the JSON. Confirm \`confidence >= 1.3\` — if it's lower, report the value and stop; do not proceed with a low-confidence sync.`);
    lines.push(`2. The synced file \`${syncOut}\` is now **the primary source** for every remaining step. Treat it exactly as if it were the original video — the on-board camera audio is replaced by the levelled external mic.`);
    lines.push(`3. All downstream EDL paths, b-roll references, and caption burns must reference \`${syncOut}\`, not the original.`);
    lines.push('');
  }

  // --- Step 0: VO generation (only when staged) ---
  if (pendingVo) {
    const cmdLines = [
      'PYTHONUTF8=1 $VU_PY \\',
      '  video-use/helpers/tts_voice.py \\',
      `  --voice "${pendingVo.voiceId}" \\`,
      `  --output "${pendingVo.output}" \\`,
      `  --stability ${pendingVo.stability} --similarity ${pendingVo.similarity} --style ${pendingVo.style} \\`,
      '  --json \\',
      '  --text "$(cat <<\'__VO_TEXT__\'',
      pendingVo.text,
      '__VO_TEXT__',
      '  )"',
    ];
    lines.push('### Step 0 — Generate VO (run this FIRST, before anything else)');
    lines.push('');
    lines.push(`Synthesize a VO clip using ElevenLabs voice **${pendingVo.voiceName}** (\`${pendingVo.voiceId}\`).`);
    lines.push(`Label: \`${pendingVo.label || '(none)'}\`. Output: \`${pendingVo.output}\`.`);
    lines.push('');
    lines.push('VO text (verbatim, do not edit):');
    lines.push('');
    lines.push('```');
    lines.push(pendingVo.text);
    lines.push('```');
    lines.push('');
    lines.push('Run this command directly — no approval gate, no clarifying questions:');
    lines.push('');
    lines.push('```bash');
    lines.push(cmdLines.join('\n'));
    lines.push('```');
    lines.push('');
    lines.push(`After it succeeds, treat \`${pendingVo.output}\` as the **VO source** for every downstream step (splicing, captions, etc.). Continue with the rest of the pipeline immediately — do NOT stop and ask for permission.`);
    lines.push('');
  }

  // --- Standing rules always injected ---
  lines.push('### Standing rules (always apply)');
  lines.push('- Level dialogue to peak **[-6, -3] dBFS** (lav records ~-12 dBFS — boost upward, never cut below floor). Use `--level-dialogue` flag on sync/match_pairs, or `level_audio.py --dialogue` on already-synced MP4. Never use loudnorm.');
  if (music) lines.push('- Level music bed to peak **[-24, -20] dBFS** using `--level-music`.');
  if (script) {
    lines.push('- When selecting a best take: run `best_take.py` directly. Closest-match wins. No approval gate. Main-speaker detection via per-speaker RMS — discard side speakers.');
  }
  lines.push('- Subtitles burn **last** — after every other processing step is locked.');
  lines.push('- Working files go to `videos/edit/<run>/`; the finished deliverable goes to `Final Output/<run>/` (see Output folders rule above).');
  if (isMedium) {
    // Trim constants inlined so the agent doesn't need to re-read TRIMMING_PHILOSOPHY.md.
    lines.push('- **Trim caps for talking-head cuts (inlined from TRIMMING_PHILOSOPHY.md):** word-boundary cuts only, 30 ms cross-fade between cuts. End-of-clip tail cap: NORMAL_MAX=0.25 s. If the last word ends in a stop consonant (p / t / k / b / d / g / hard-c), use LAST_CHAR_CAP=0.28 s. TAIL_PAD=0.08 s, HEAD_PAD=0.05 s. Do NOT open the philosophy doc — these constants are authoritative for this run.');
    // Render mode constants inlined from RENDER_EXPORT_PHILOSOPHY.md.
    lines.push('- **Render mode (inlined from RENDER_EXPORT_PHILOSOPHY.md):** for outputs ≤ 2 minutes do a single full composite encode (Mode A). For > 2 minutes, default to layered handoff (Mode B). Skip the operator-confirmation gate for Mode A — just render.');
  }
  // Preview gate is OFF for ALL pipeline runs initiated through the composer.
  // The user pre-authorizes the entire pipeline at Send time. Re-render only
  // if the final has a defect they call out.
  lines.push('- **NO preview gate. NO mode-A/mode-B confirmation. NO "should I proceed?" question.** Render the final encode directly once cuts/EDL/composite are locked. The user has pre-authorized this entire pipeline by clicking Send — pausing to ask wastes their time and is explicitly forbidden.');
  lines.push('');

  // --- B-roll, Captions, Graphics blocks (driven by their tabs) ---
  // These come from the Captions / Graphics / B-roll tab states. Each block
  // returns '' if its tab is disabled, in which case we skip the section.
  const brollBlock    = (typeof buildBrollBlock    === 'function') ? buildBrollBlock()    : '';
  const captionsBlock = (typeof buildCaptionsBlock === 'function') ? buildCaptionsBlock() : '';
  const graphicsBlock = (typeof buildGraphicsBlock === 'function') ? buildGraphicsBlock() : '';
  const musicBlock    = (typeof buildMusicBlock    === 'function') ? buildMusicBlock()    : '';
  if (musicBlock)    { lines.push(musicBlock);    lines.push(''); }
  if (brollBlock)    { lines.push(brollBlock);    lines.push(''); }
  if (graphicsBlock) { lines.push(graphicsBlock); lines.push(''); }
  if (captionsBlock) { lines.push(captionsBlock); lines.push(''); }

  // --- Order of operations note (matters when multiple overlays are enabled) ---
  if (brollBlock || graphicsBlock || captionsBlock || musicBlock) {
    lines.push('### Order of overlay operations');
    lines.push('When more than one of {music, b-roll, motion graphics, captions} is enabled, run them in this order:');
    lines.push('1. Lock the cut first.');
    lines.push('2. **Music & SFX** (if enabled) — mix audio, produce `<cut>_audio.mp4`.');
    lines.push('3. **B-roll cutaways** (if enabled) — produces `<cut>_broll.mp4`.');
    lines.push('4. **Motion graphics** (if enabled) — produces `<cut>_gfx.mp4`.');
    lines.push('5. **Captions burn LAST** onto whichever was the last produced output.');
    lines.push('');
  }

  // --- User instructions ---
  lines.push('### Pipeline instructions');
  lines.push(freeform.trim());

  return lines.join('\n');
}

// Composer event wiring
// (b-roll mode/folder state moved to the B-roll tab — see brollState below)

const QUALITY_KEY = 'veditor.quality.v1';
try {
  const savedQuality = localStorage.getItem(QUALITY_KEY);
  if (savedQuality) composerQualitySelect.value = savedQuality;
} catch {}
composerQualitySelect.addEventListener('change', () => {
  try { localStorage.setItem(QUALITY_KEY, composerQualitySelect.value); } catch {}
});

// ---- ElevenLabs generation shortcuts ----
const VOICE_ID_KEY  = 'veditor.elevenVoiceId.v1';
const MUSIC_LAST_KEY = 'veditor.musicLastPrompt.v1';

// ---- VO generation modal ----
const voModal       = $('#vo-modal');
const voModalClose  = $('#vo-modal-close');
const voModalCancel = $('#vo-modal-cancel');
const voModalGen    = $('#vo-modal-generate');
const voVoiceSelect = $('#vo-voice-select');
const voRefreshBtn  = $('#vo-refresh-voices');
const voLabelInput  = $('#vo-label-input');
const voTextInput   = $('#vo-text-input');
const voStability   = $('#vo-stability');
const voSimilarity  = $('#vo-similarity');
const voStyle       = $('#vo-style');
const voHint        = $('#vo-hint');

let voicesCache = [];

async function loadVoicesFromCache() {
  try {
    const r = await fetch('/voices.json?_=' + Date.now());
    if (!r.ok) return [];
    const data = await r.json();
    return Array.isArray(data) ? data : [];
  } catch {
    return [];
  }
}

function populateVoiceDropdown(voices) {
  voicesCache = voices;
  voVoiceSelect.innerHTML = '';
  if (!voices.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = '— no voices cached · click ↻ refresh —';
    voVoiceSelect.appendChild(o);
    voHint.textContent = 'No voice list cached yet. Click ↻ refresh, send the chat prompt, then reopen this dialog.';
    return;
  }
  // "— pick a voice —" placeholder + voices, cloned first
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '— pick a voice —';
  voVoiceSelect.appendChild(placeholder);

  const cloned = voices.filter((v) => v.category === 'cloned');
  const other  = voices.filter((v) => v.category !== 'cloned');
  const addGroup = (label, list) => {
    if (!list.length) return;
    const og = document.createElement('optgroup');
    og.label = label;
    for (const v of list) {
      const o = document.createElement('option');
      o.value = v.voice_id;
      o.textContent = `${v.name}  ·  ${v.voice_id.slice(0, 8)}…`;
      og.appendChild(o);
    }
    voVoiceSelect.appendChild(og);
  };
  addGroup('cloned', cloned);
  addGroup('preset', other);

  // restore last-used voice
  try {
    const last = localStorage.getItem(VOICE_ID_KEY) || '';
    if (last && voices.some((v) => v.voice_id === last)) voVoiceSelect.value = last;
  } catch {}
  voHint.textContent = `${voices.length} voices loaded (${cloned.length} cloned).`;
}

function refreshGenButtonState() {
  const haveVoice = !!voVoiceSelect.value;
  const haveText  = voTextInput.value.trim().length > 0;
  voModalGen.disabled = !(haveVoice && haveText);
}

function openVoModal() {
  voModal.hidden = false;
  refreshGenButtonState();
  loadVoicesFromCache().then(populateVoiceDropdown).then(refreshGenButtonState);
}

function closeVoModal() { voModal.hidden = true; }

$('#gen-vo-btn').addEventListener('click', openVoModal);

// ---- Mode: split hooks ----
// When ticked, hitting send transforms the freeform text into a
// split-hooks task instead of a pipeline build. Persisted across reloads
// because the mode is sticky per session.
const modeSplitHooks = $('#mode-split-hooks');
const SPLIT_HOOKS_KEY = 'veditor.modeSplitHooks.v1';
try {
  modeSplitHooks.checked = localStorage.getItem(SPLIT_HOOKS_KEY) === '1';
} catch {}
modeSplitHooks.addEventListener('change', () => {
  try { localStorage.setItem(SPLIT_HOOKS_KEY, modeSplitHooks.checked ? '1' : '0'); } catch {}
});

function buildSplitHooksPrompt(videoPath, freeform) {
  const stem = videoPath.split('/').pop().replace(/\.[^.]+$/, '');
  const transcribeCmd =
    'PYTHONUTF8=1 $VU_PY \\\n' +
    '  video-use/helpers/transcribe.py \\\n' +
    `  "${videoPath}"`;
  const splitCmd =
    'PYTHONUTF8=1 $VU_PY \\\n' +
    '  video-use/helpers/split_hooks.py \\\n' +
    `  "${videoPath}" \\\n` +
    '  --edl videos/edit/hooks_edl.json \\\n' +
    '  --json';

  const lines = [];
  lines.push('## Mode: split hooks — autonomous execution, no approval gates');
  lines.push('');
  lines.push('Split the selected video into per-hook MP4 deliverables.');
  lines.push('');
  lines.push(`**Source:** \`${videoPath}\``);
  if (freeform) {
    lines.push('');
    lines.push('**Operator notes (apply to this run):**');
    lines.push(freeform);
  }
  lines.push('');
  lines.push('### Step 1 — make sure a transcript exists');
  lines.push('Cached if already transcribed:');
  lines.push('');
  lines.push('```');
  lines.push(transcribeCmd);
  lines.push('```');
  lines.push('');
  lines.push('### Step 2 — read the transcript and design the hook EDL');
  lines.push(`Open the transcript JSON (\`videos/edit/transcripts/${stem}.json\` or \`videos/edit/edit/transcripts/${stem}.json\`).`);
  lines.push('');
  lines.push('**Use the WORD-LEVEL stream** in `transcript.words[]` (entries with `type: "word"` and `start`/`end` timestamps) to design boundaries. **Do NOT** estimate from phrase ranges — phrase boundaries are coarse and leave the hook starting mid-word.');
  lines.push('');
  lines.push('For each distinct hook:');
  lines.push('- `start` = the **`start` timestamp of the first word** of the hook **minus 0.10 s** (small head pad, captures lead-in breath).');
  lines.push('- `end` = the **`end` timestamp of the last word** of the hook **plus 0.30 s** (small tail pad, captures trailing breath / consonant decay).');
  lines.push('- `preview` = the first ~10 words copied verbatim from the transcript so the cut text and the EDL preview always agree.');
  lines.push('');
  lines.push('When the talent re-records the same hook 2–3 times back-to-back, mark each as a take of the same hook via the `take_of` field (use a short slug like `renewal_offer` for all takes of one hook), and set `"best": true` on the strongest take of that group.');
  lines.push('');
  lines.push('Drop fragments shorter than ~4 seconds and obvious slate / mic-test chatter. Use the talent\'s first words to derive a name slug.');
  lines.push('');
  lines.push('**Self-check before writing the EDL:** for each hook, confirm that the word at `transcript.words[]` whose `start` is closest to (but ≥) your `start + 0.10` is the **first content word** of `preview`. If it isn\'t, your EDL is wrong — re-pick the word index. Same check on the tail.');
  lines.push('');
  lines.push('Write the hook EDL to `videos/edit/hooks_edl.json` in this exact shape:');
  lines.push('');
  lines.push('```json');
  lines.push('[');
  lines.push('  {');
  lines.push('    "name": "renewal_offer",');
  lines.push('    "start": 0.92, "end": 32.40,');
  lines.push('    "take_of": "renewal_offer", "best": false,');
  lines.push('    "preview": "So renew by Andersen right now is offering…"');
  lines.push('  },');
  lines.push('  {');
  lines.push('    "name": "renewal_offer",');
  lines.push('    "start": 35.10, "end": 65.85,');
  lines.push('    "take_of": "renewal_offer", "best": true,');
  lines.push('    "preview": "So Renewal by Andersen has a crazy deal…"');
  lines.push('  }');
  lines.push(']');
  lines.push('```');
  lines.push('');
  lines.push('### Step 3 — encode');
  lines.push('```');
  lines.push(splitCmd);
  lines.push('```');
  lines.push('');
  lines.push(`Files land in \`videos/edit/hooks/${stem}/NN_<name>.mp4\` plus a \`hooks_manifest.json\` audit log. When done, report a table of \`# | name | duration | best | preview\` so the operator can pick which ones to develop into full pipelines. Refresh the file list so the hook MP4s appear in the dropdowns.`);
  lines.push('');
  lines.push('**Do NOT proceed to a full pipeline build** — this run is split-hooks only.');
  return lines.join('\n');
}
voModalClose.addEventListener('click', closeVoModal);
voModalCancel.addEventListener('click', closeVoModal);
voModal.addEventListener('click', (e) => { if (e.target === voModal) closeVoModal(); });
voVoiceSelect.addEventListener('change', refreshGenButtonState);
voTextInput.addEventListener('input', refreshGenButtonState);

voRefreshBtn.addEventListener('click', () => {
  // Pre-fill the chat with a one-liner that dumps voices.json into static/.
  const cmd =
    'PYTHONUTF8=1 $VU_PY ' +
    'video-use/helpers/tts_voice.py ' +
    '--list-voices --json > studio/static/voices.json';
  promptInput.value =
    'Refresh the cached ElevenLabs voice list. Run this directly — no approval gate:\n\n' +
    '```\n' + cmd + '\n```\n\n' +
    'After it succeeds, just confirm the file size of `studio/static/voices.json`. ' +
    "Don't trigger any pipeline build — this is only the voice-list refresh.";
  skipPipelineWrap = true;
  closeVoModal();
  promptInput.focus();
});

function slugify(s, maxLen = 32) {
  return (s || '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_|_$/g, '')
    .slice(0, maxLen) || 'vo';
}

// ---- Pending VO state — stages a VO to generate during the next pipeline run ----
const PENDING_VO_KEY = 'veditor.pendingVo.v1';
const PENDING_VO_OPTION = '__pending_vo__';
let pendingVoConfig = null;
try {
  const raw = localStorage.getItem(PENDING_VO_KEY);
  if (raw) pendingVoConfig = JSON.parse(raw);
} catch {}

function refreshPendingVoOption() {
  // Remove any existing pending option first.
  const existing = composerVoSelect.querySelector(`option[value="${PENDING_VO_OPTION}"]`);
  if (existing) existing.remove();
  if (!pendingVoConfig) return;
  const opt = document.createElement('option');
  opt.value = PENDING_VO_OPTION;
  const preview = pendingVoConfig.text.length > 40
    ? pendingVoConfig.text.slice(0, 40) + '…'
    : pendingVoConfig.text;
  opt.textContent = `🎙 generate: ${pendingVoConfig.label || 'vo'} — "${preview}"`;
  // Insert right after the "— none —" option.
  composerVoSelect.insertBefore(opt, composerVoSelect.children[1] || null);
  composerVoSelect.value = PENDING_VO_OPTION;
}
refreshPendingVoOption();

function clearPendingVo() {
  pendingVoConfig = null;
  try { localStorage.removeItem(PENDING_VO_KEY); } catch {}
  refreshPendingVoOption();
}

voModalGen.addEventListener('click', () => {
  const voiceId = voVoiceSelect.value;
  const text    = voTextInput.value.trim();
  if (!voiceId || !text) return;
  try { localStorage.setItem(VOICE_ID_KEY, voiceId); } catch {}

  const voiceMeta = voicesCache.find((v) => v.voice_id === voiceId);
  const voiceName = voiceMeta ? voiceMeta.name : voiceId;

  // Filename: <label>_vo_<ts>.mp3, falling back to first few words of text.
  const labelRaw = voLabelInput.value.trim();
  const slug = slugify(labelRaw || text.split(/\s+/).slice(0, 5).join(' '));
  const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(2, 12); // YYMMDDhhmm
  // .mp3 by default — works on every ElevenLabs tier. .wav (PCM) needs Pro+.
  const outRel = `videos/${slug}_vo_${ts}.mp3`;

  pendingVoConfig = {
    voiceId,
    voiceName,
    text,
    label: labelRaw,
    output: outRel,
    stability:  voStability.value,
    similarity: voSimilarity.value,
    style:      voStyle.value,
  };
  try { localStorage.setItem(PENDING_VO_KEY, JSON.stringify(pendingVoConfig)); } catch {}

  refreshPendingVoOption();
  closeVoModal();
  composerStatus.textContent = `VO armed (${voiceName} → ${outRel}) — will generate on next pipeline send`;
  composerStatus.className = 'st-status ready';
});

// Pre-warm the voice cache on load so the modal opens fast.
loadVoicesFromCache().then((vs) => { voicesCache = vs; });

$('#gen-music-btn').addEventListener('click', () => {
  let last = '';
  try { last = localStorage.getItem(MUSIC_LAST_KEY) || ''; } catch {}
  const brief = (prompt(
    'Describe the music bed (style, mood, instrumentation):\n\n' +
    'Examples:\n' +
    '  "uplifting acoustic instrumental, warm guitar, 90 BPM"\n' +
    '  "soft cinematic piano, contemplative, sparse"\n' +
    '  "energetic electronic, driving beat, 120 BPM"',
    last || 'uplifting acoustic instrumental, warm, 90 BPM'
  ) || '').trim();
  if (!brief) return;
  try { localStorage.setItem(MUSIC_LAST_KEY, brief); } catch {}

  const durStr = (prompt('Track length in seconds (max 600):', '60') || '').trim();
  const dur = parseFloat(durStr);
  if (!isFinite(dur) || dur <= 0 || dur > 600) {
    alert('Invalid duration.');
    return;
  }

  const slug = brief.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 32);
  const outRel = `videos/music_${slug}_${Math.round(dur)}s.mp3`;
  const cmd =
    'PYTHONUTF8=1 $VU_PY ' +
    'video-use/helpers/tts_music.py ' +
    `--prompt ${JSON.stringify(brief)} ` +
    `--duration ${dur} ` +
    `--output "${outRel}" ` +
    '--json';

  promptInput.value =
    `Compose a music bed via ElevenLabs Music.\n\n` +
    `Brief: ${brief}\nDuration: ${dur}s\n\n` +
    'Run this directly — no approval gate, no clarifying questions:\n\n' +
    '```\n' + cmd + '\n```\n\n' +
    `When it succeeds, the file will land at \`${outRel}\`. After it finishes, ` +
    `report the wall time and refresh the file list so the music dropdown picks ` +
    `it up. Do NOT proceed to a full pipeline build — this is just the music ` +
    `generation step.`;
  skipPipelineWrap = true;
  promptInput.focus();
});

composerVideoSelect.addEventListener('change', () => {
  if (composerVideoSelect.value) loadVideo(composerVideoSelect.value);
  refreshComposerUI();
});
composerScriptSelect.addEventListener('change', refreshComposerUI);
composerFreeform.addEventListener('input', refreshComposerUI);

composerClearBtn.addEventListener('click', () => {
  composerVideoSelect.value  = '';
  if (composerAudioSelect) composerAudioSelect.value  = '';
  composerScriptSelect.value = '';
  composerMusicSelect.value  = '';
  composerVoSelect.value     = '';
  if (composerFreeform) composerFreeform.value = '';
  clearPendingVo();
  refreshComposerUI();
  loadVideo('');
});

// ─── STITCH-SCRIPT MODE ───
// Toggle in composer-header swaps the single video <select> into a multi-select
// and reveals a "▶ run stitch" button. Clicking it POSTs the picked videos +
// script to /api/stitch_script, which spawns video-use/helpers/stitch_script.py.
const modeStitchScript = $('#mode-stitch-script');
const composerStitchRunBtn = $('#composer-stitch-run');

function applyStitchMode() {
  const on = !!(modeStitchScript && modeStitchScript.checked);
  composerVideoSelect.multiple = on;
  composerVideoSelect.size = on ? 6 : 1;
  composerStitchRunBtn.hidden = !on;
  if (on) {
    composerStatus.textContent = 'stitch mode · Ctrl-click 2+ videos, pick a script, hit ▶ run stitch';
    // Drop the placeholder "— none —" from selection — irrelevant in multi-mode.
    Array.from(composerVideoSelect.options).forEach(o => { if (!o.value) o.selected = false; });
  } else {
    refreshComposerUI();
  }
}

if (modeStitchScript) {
  modeStitchScript.addEventListener('change', applyStitchMode);
}

async function runStitchScript() {
  const videos = Array.from(composerVideoSelect.selectedOptions)
    .map(o => o.value).filter(Boolean);
  const script = composerScriptSelect.value;
  if (videos.length < 2) {
    composerStatus.textContent = 'stitch needs 2+ videos · Ctrl-click to pick more';
    return;
  }
  if (!script) {
    composerStatus.textContent = 'stitch needs a script · pick one in the script dropdown';
    return;
  }
  composerStitchRunBtn.disabled = true;
  composerStatus.textContent = `stitching ${videos.length} video(s) … transcripts cache hot ≈ fast, cold ≈ 20s/video`;
  try {
    const r = await fetch('/api/stitch_script', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ videos, script }),
    });
    const body = await r.json().catch(() => null);
    if (!r.ok) {
      const detail = (body && body.detail) || `HTTP ${r.status}`;
      composerStatus.textContent = `stitch failed · ${String(detail).slice(0, 200)}`;
      return;
    }
    const out = body && body.output;
    const segs = body && body.segment_count;
    const dur = body && body.total_duration;
    composerStatus.textContent =
      `stitched ${segs} segment(s) · ${dur ? dur.toFixed(1) + 's' : ''} · ${out ? out.split(/[\\/]/).pop() : 'output'}`;
    if (out) {
      // Reload file list and try to load the new video in the player.
      try { await refreshFiles && refreshFiles(); } catch (e) { /* */ }
      try { loadVideo(out); } catch (e) { /* */ }
    }
  } catch (e) {
    composerStatus.textContent = `stitch error · ${(e && e.message) || e}`;
  } finally {
    composerStitchRunBtn.disabled = false;
  }
}

if (composerStitchRunBtn) {
  composerStitchRunBtn.addEventListener('click', runStitchScript);
}

// ─── ON-SCREEN HOOK ───
// Lives under the Captions tab. Three modes:
//   - Manual single: type hook text → POST /api/hook_overlay
//   - Manual batch:  type 3 hook texts → POST /api/hook_overlay with all 3
//   - AI single/batch: blank text + "Kino writes" toggle → wrap a chat prompt
//     so Kino reads the transcript and runs hook_overlay.py with its picks.
const HOOK_KEY = 'veditor.hook.v1';
const hookState = (() => {
  const def = {
    enabled: false,
    text: '',
    text2: '',
    text3: '',
    ai: false,
    batch: false,
    position: 'upper-third',
    durationMode: 'timed',  // 'timed' | 'full'
    duration: 3,
    fontFamily: 'Impact',
    fontSize: 96,
    color: '#FFFFFF',
    bgColor: '#000000',
    bgAlpha: 160,
    rounded: true,
    shadow: false,
  };
  try {
    const saved = JSON.parse(localStorage.getItem(HOOK_KEY) || 'null');
    return Object.assign({}, def, saved || {});
  } catch (e) { return def; }
})();
function saveHookState() {
  try { localStorage.setItem(HOOK_KEY, JSON.stringify(hookState)); } catch (e) { /* */ }
}

function applyHookUI() {
  const sub = $('#hook-sub');
  const batchFields = $('#hook-batch-fields');
  const slider = $('#hook-duration-slider-row');
  if (!sub) return;  // tab not in DOM (defensive — should always be present)

  sub.hidden = !hookState.enabled;
  $('#hook-enable').checked = hookState.enabled;
  $('#hook-text').value = hookState.text || '';
  $('#hook-text-2').value = hookState.text2 || '';
  $('#hook-text-3').value = hookState.text3 || '';
  $('#hook-ai').checked = hookState.ai;
  $('#hook-batch').checked = hookState.batch || hookState.ai;  // AI implies batch

  // Manual-batch needs the 2 extra text fields; AI mode hides them (Kino fills in).
  batchFields.hidden = !(hookState.batch && !hookState.ai);

  // Position chip selection.
  document.querySelectorAll('#hook-position-row .chip').forEach((c) => {
    c.classList.toggle('selected', c.dataset.hookPos === hookState.position);
  });

  // Duration-mode chip selection + slider visibility.
  document.querySelectorAll('#hook-duration-mode-row .chip').forEach((c) => {
    c.classList.toggle('selected', c.dataset.hookDurMode === hookState.durationMode);
  });
  if (slider) slider.style.display = (hookState.durationMode === 'full') ? 'none' : '';

  $('#hook-duration-slider').value = hookState.duration;
  $('#hook-duration').value = hookState.duration;
  $('#hook-font-size-slider').value = hookState.fontSize;
  $('#hook-font-size').value = hookState.fontSize;
  $('#hook-color').value = hookState.color;
  $('#hook-bg-color').value = hookState.bgColor;
  $('#hook-bg-alpha').value = hookState.bgAlpha;
  $('#hook-bg-alpha-out').textContent = String(hookState.bgAlpha);
  $('#hook-rounded').checked = !!hookState.rounded;
  $('#hook-shadow').checked = !!hookState.shadow;
}

function renderHookFontRow() {
  const row = $('#hook-font-family-row');
  if (!row || typeof FONT_FAMILIES === 'undefined') return;
  row.innerHTML = '';
  for (const f of FONT_FAMILIES) {
    const chip = el('button', 'chip');
    chip.type = 'button';
    chip.dataset.hookFont = f.id;
    if (f.id === hookState.fontFamily) chip.classList.add('selected');
    chip.style.fontFamily = f.stack;
    chip.innerHTML = `<span>${f.name}</span><span class="chip-meta">${f.note}</span>`;
    chip.addEventListener('click', () => {
      hookState.fontFamily = f.id;
      saveHookState();
      row.querySelectorAll('.chip').forEach((c) => c.classList.remove('selected'));
      chip.classList.add('selected');
    });
    row.appendChild(chip);
  }
}

function bindHookInputs() {
  if (!$('#hook-enable')) return;
  $('#hook-enable').addEventListener('change', (e) => {
    hookState.enabled = e.target.checked;
    saveHookState(); applyHookUI();
  });
  $('#hook-text').addEventListener('input', (e) => {
    hookState.text = e.target.value; saveHookState();
  });
  $('#hook-text-2').addEventListener('input', (e) => {
    hookState.text2 = e.target.value; saveHookState();
  });
  $('#hook-text-3').addEventListener('input', (e) => {
    hookState.text3 = e.target.value; saveHookState();
  });
  $('#hook-ai').addEventListener('change', (e) => {
    hookState.ai = e.target.checked;
    // AI mode forces batch on — Kino generates 3 hooks.
    if (hookState.ai) hookState.batch = true;
    saveHookState(); applyHookUI();
  });
  $('#hook-batch').addEventListener('change', (e) => {
    hookState.batch = e.target.checked;
    if (!hookState.batch) hookState.ai = false;  // can't be AI without batch (3-variant default)
    saveHookState(); applyHookUI();
  });

  document.querySelectorAll('#hook-position-row .chip').forEach((c) => {
    c.addEventListener('click', () => {
      hookState.position = c.dataset.hookPos;
      saveHookState(); applyHookUI();
    });
  });
  document.querySelectorAll('#hook-duration-mode-row .chip').forEach((c) => {
    c.addEventListener('click', () => {
      hookState.durationMode = c.dataset.hookDurMode;
      saveHookState(); applyHookUI();
    });
  });

  // Bidirectional slider <-> number sync for duration + font size.
  const wirePair = (sliderId, numId, key, parser) => {
    const a = $('#' + sliderId), b = $('#' + numId);
    if (!a || !b) return;
    const on = (v) => { hookState[key] = parser(v); saveHookState(); a.value = b.value = hookState[key]; };
    a.addEventListener('input', () => on(a.value));
    b.addEventListener('input', () => on(b.value));
  };
  wirePair('hook-duration-slider', 'hook-duration', 'duration', (v) => Math.max(0.5, Math.min(20, parseFloat(v) || 3)));
  wirePair('hook-font-size-slider', 'hook-font-size', 'fontSize', (v) => Math.max(20, Math.min(300, parseInt(v, 10) || 96)));

  $('#hook-color').addEventListener('input', (e) => { hookState.color = e.target.value; saveHookState(); });
  $('#hook-bg-color').addEventListener('input', (e) => { hookState.bgColor = e.target.value; saveHookState(); });
  $('#hook-bg-alpha').addEventListener('input', (e) => {
    hookState.bgAlpha = parseInt(e.target.value, 10) || 0;
    $('#hook-bg-alpha-out').textContent = String(hookState.bgAlpha);
    saveHookState();
  });
  $('#hook-rounded').addEventListener('change', (e) => { hookState.rounded = e.target.checked; saveHookState(); });
  $('#hook-shadow').addEventListener('change', (e) => { hookState.shadow = e.target.checked; saveHookState(); });

  $('#hook-run-btn').addEventListener('click', runHook);
}

function _currentVideoForHook() {
  // Prefer the composer-picked video; fall back to the player's currently
  // loaded source so the user can drop a video into the right panel and
  // hit Burn Hook without configuring the composer.
  const composerV = composerVideoSelect && composerVideoSelect.value;
  if (composerV) return composerV;
  if (player && player.currentSrc) {
    try {
      const u = new URL(player.currentSrc);
      const path = decodeURIComponent(u.pathname.replace(/^\/(api\/)?file\//, '').replace(/^\//, ''));
      return path;
    } catch (e) { /* */ }
  }
  return '';
}

async function runHook() {
  const status = $('#hook-status');
  const btn = $('#hook-run-btn');
  if (!hookState.enabled) { status.textContent = 'enable the hook toggle first'; return; }
  const video = _currentVideoForHook();
  if (!video) { status.textContent = 'no video selected — pick one in the composer or load one in the player'; return; }

  // AI mode → wrap a chat prompt so Kino reads context and orchestrates.
  if (hookState.ai) {
    const script = composerScriptSelect && composerScriptSelect.value;
    const styleSummary = JSON.stringify({
      position: hookState.position,
      duration: hookState.durationMode === 'full' ? 'full' : hookState.duration,
      font_family: hookState.fontFamily,
      font_size: hookState.fontSize,
      text_color: hookState.color,
      bg_color: hookState.bgColor,
      bg_alpha: hookState.bgAlpha,
      rounded: hookState.rounded,
      shadow: hookState.shadow,
    }, null, 2);
    const prompt =
`Generate 3 short on-screen hook lines for the video at ${video}.

Read its transcript (run video-use/helpers/transcribe.py if not cached) and/or the script at ${script || '(no script provided — derive from transcript)'} to understand the subject matter. Write 3 hooks that:
- Are < 8 words each
- Stop the scroll in the first 3 seconds
- Each takes a different angle (problem · curiosity · benefit)
- Read clearly at 96pt on a 9:16 frame

Then run video-use/helpers/hook_overlay.py once with all three --text values + --output-prefix (use videos/edit/hook_ai_<timestamp> as the prefix). Style:

${styleSummary}

Drop the 3 output paths in your final message.`;
    promptInput.value = prompt;
    skipPipelineWrap = true;
    activateTab('chat');
    status.textContent = 'wrapped a prompt for Kino — hit send in the Chat tab to generate + burn 3 variants';
    return;
  }

  // Manual mode — direct POST to /api/hook_overlay.
  const texts = [hookState.text.trim()];
  if (hookState.batch) {
    const t2 = (hookState.text2 || '').trim();
    const t3 = (hookState.text3 || '').trim();
    if (!t2 || !t3) { status.textContent = 'batch mode needs all 3 hook texts filled in'; return; }
    texts.push(t2, t3);
  }
  if (!texts[0]) { status.textContent = 'hook text is empty'; return; }

  const body = {
    video,
    texts,
    position: hookState.position,
    duration: hookState.durationMode === 'full' ? 'full' : hookState.duration,
    font_family: hookState.fontFamily,
    font_size: hookState.fontSize,
    text_color: hookState.color,
    bg_color: hookState.bgColor,
    bg_alpha: hookState.bgAlpha,
    rounded: hookState.rounded,
    shadow: hookState.shadow,
  };
  btn.disabled = true;
  status.textContent = `burning ${texts.length} variant(s) …`;
  try {
    const r = await fetch('/api/hook_overlay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => null);
    if (!r.ok) {
      const detail = (j && j.detail) || `HTTP ${r.status}`;
      status.textContent = `hook failed · ${String(detail).slice(0, 200)}`;
      return;
    }
    const variants = (j && j.variants) || [];
    status.textContent = `burned ${variants.length} variant(s) → ${variants.map(v => (v.output || '').split(/[\\/]/).pop()).join(', ')}`;
    try { await (typeof refreshFiles === 'function' && refreshFiles()); } catch (e) { /* */ }
    if (variants[0] && variants[0].output) {
      try { loadVideo(variants[0].output); } catch (e) { /* */ }
    }
  } catch (e) {
    status.textContent = `hook error · ${(e && e.message) || e}`;
  } finally {
    btn.disabled = false;
  }
}

// Init hook UI on load. Defer so FONT_FAMILIES is defined.
queueMicrotask(() => {
  try {
    renderHookFontRow();
    bindHookInputs();
    applyHookUI();
  } catch (e) {
    console.error('hook UI init failed:', e);
  }
});

composerBuildBtn.addEventListener('click', () => {
  const v = composerVideoSelect.value;
  const f = composerFreeform.value.trim();
  if (!v || !f) return;
  promptInput.value = buildPipelinePrompt({
    video:    v,
    audio:    composerAudioSelect.value   || null,
    script:   composerScriptSelect.value  || null,
    music:    composerMusicSelect.value   || null,
    vo:       composerVoSelect.value      || null,
    freeform: f,
  });
  nextSubmitModelOverride = composerModelSelect.value || syncModelSelect.value || 'sonnet';
  skipPipelineWrap = true;
  activateTab('chat');
  promptForm.requestSubmit();
});

// Wire each dropzone independently.
function bindDropzone(dz, fileInput, kind) {
  dz.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', (e) => {
    const f = e.target.files[0];
    if (f) uploadOne(f, kind);
    fileInput.value = '';
  });
  ['dragenter', 'dragover'].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('drag'); })
  );
  ['dragleave', 'drop'].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('drag'); })
  );
  dz.addEventListener('drop', async (e) => {
    // In-app drag from the FS pane: a path string in our custom MIME.
    const fsPath = e.dataTransfer.getData(FS_DRAG_MIME);
    if (fsPath) {
      const name = fsPath.split('/').pop() || '';
      const isAudio = AUDIO_RX.test(name);
      const isScript = SCRIPT_RX.test(name);
      const detected = isAudio ? 'audio' : (isScript ? 'script' : 'video');
      if (detected !== kind) {
        alert(`That looks like ${detected} — drop it in the ${detected} zone instead.`);
        return;
      }
      await importFsFile(fsPath);
      return;
    }
    // OS drag (file from desktop / explorer): upload via the existing path.
    const f = e.dataTransfer.files[0];
    if (!f) return;
    const isAudio = AUDIO_RX.test(f.name) || (f.type || '').startsWith('audio/');
    const isScript = SCRIPT_RX.test(f.name) || (f.type || '').startsWith('text/');
    const detected = isAudio ? 'audio' : (isScript ? 'script' : 'video');
    if (detected !== kind) {
      alert(`That looks like ${detected} — drop it in the ${detected} zone instead.`);
      return;
    }
    uploadOne(f, kind);
  });
}
bindDropzone(dzVideo, fileInputVideo, 'video');
bindDropzone(dzAudio, fileInputAudio, 'audio');
bindDropzone(dzScript, fileInputScript, 'script');

// ============================================================
// PRESETS
// ============================================================
const CAPTION_PRESETS = [
  { id: 'voltchu',  name: 'Voltchu',  bg: '#FFE600', fg: '#000000', sample: 'This stops the scroll',          tag: 'Electric yellow + black · max contrast' },
  { id: 'cindrax',  name: 'Cindrax',  bg: '#FF2D20', fg: '#FFFFFF', sample: 'Watch this right now',            tag: 'Alert red + white · urgency' },
  { id: 'tideon',   name: 'Tideon',   bg: '#00F5D4', fg: '#0D0D1A', sample: 'You need to hear this',           tag: 'Neon teal + dark · TikTok native' },
  { id: 'psyglow',  name: 'Psyglow',  bg: '#FFDE59', fg: '#1A0F40', sample: 'Wait for it',                     tag: 'Yellow + deep purple · luxe bold' },
  { id: 'embrix',   name: 'Embrix',   bg: '#FFFFFF', fg: '#1A1A1A', sample: 'Nobody talks about this',         tag: 'White + dark · thumb-stopping' },
  { id: 'umbrak',   name: 'Umbrak',   bg: '#FF4136', fg: '#FFFFFF', sample: 'Red flag spotted',                tag: 'Red + white · cinematic urgency' },
  { id: 'mellowf',  name: 'Mellowf',  bg: '#1A1A1A', fg: '#F5F0E8', sample: 'POV: you finally figured it out', tag: 'Charcoal + cream · editorial' },
  { id: 'verdling', name: 'Verdling', bg: '#059669', fg: '#ECFDF5', sample: 'Save this for later',             tag: 'Emerald + white · wellness/finance' },
  { id: 'phantorb', name: 'Phantorb', bg: '#A855F7', fg: '#FFFFFF', sample: 'Nobody expected this',            tag: 'Violet + white · gaming/tech' },
];
const PLACEMENTS = [
  { id: 'top',         name: 'Top',          y: '8%' },
  { id: 'upper-third', name: 'Upper third',  y: '25%' },
  { id: 'center',      name: 'Center',       y: '50%' },
  { id: 'lower-third', name: 'Lower third',  y: '72%' },
  { id: 'bottom',      name: 'Bottom',       y: '90%' },
];
const ASPECTS = [
  { id: '9x16', name: '9:16', w: 1080, h: 1920, def: true,  note: 'vertical · IG/TikTok' },
  { id: '16x9', name: '16:9', w: 1920, h: 1080,             note: 'landscape · YouTube' },
  { id: '5x4',  name: '5:4',  w: 1080, h: 1350,             note: 'portrait card' },
  { id: '1x1',  name: '1:1',  w: 1080, h: 1080,             note: 'square · IG feed' },
];

const PRESET_KEY = 'veditor.preset.v1';
const presetState = loadPreset();

function loadPreset() {
  let merged = defaultPreset();
  try {
    const raw = localStorage.getItem(PRESET_KEY);
    if (raw) merged = Object.assign(defaultPreset(), JSON.parse(raw));
  } catch (e) { /* corrupt JSON → fall back to defaults */ }
  return normalizePreset(merged);
}

// Repair fields that are the wrong type / out of range. Survives stale-shape
// localStorage AND user-edited values. Type-only checks (no list lookups) so
// it can run before LAYOUTS / CASES are declared. The build*Prompt functions
// handle id-not-in-list separately via .find(...) || fallback.
function normalizePreset(p) {
  const d = defaultPreset();
  const num = (v, fb) => (typeof v === 'number' && Number.isFinite(v)) ? v : fb;
  const str = (v, fb) => (typeof v === 'string') ? v : fb;
  return {
    captionId:   str(p.captionId,   d.captionId),
    placementId: str(p.placementId, d.placementId),
    aspectId:    str(p.aspectId,    d.aspectId),
    layout:      str(p.layout,      d.layout),
    caseStyle:   str(p.caseStyle,   d.caseStyle),
    hook:        str(p.hook,        d.hook),
    extra:       str(p.extra,       d.extra),
    rounded:     !!(p.rounded ?? d.rounded),
    shadow:      !!(p.shadow  ?? d.shadow),
    maxChars:    Math.max(10, Math.min(100, num(p.maxChars,    d.maxChars))),
    minDuration: Math.max(0.3, Math.min(5.0, num(p.minDuration, d.minDuration))),
    gapFrames:   Math.max(0,  Math.min(12,  num(p.gapFrames,   d.gapFrames))),
    fontFamily:  str(p.fontFamily,  d.fontFamily),
    fontSize:    Math.max(40, Math.min(160, num(p.fontSize,    d.fontSize))),
  };
}
function defaultPreset() {
  return {
    captionId: 'voltchu',
    placementId: 'lower-third',
    aspectId: '9x16',
    hook: '',
    rounded: true,
    shadow: false,
    extra: '',
    layout: 'single',
    maxChars: 30,
    minDuration: 1.5,
    gapFrames: 0,
    caseStyle: 'natural',
    fontFamily: 'Arial',
    fontSize: 80,
  };
}

const LAYOUTS = [
  { id: 'single', name: 'Single line' },
  { id: 'double', name: 'Double line' },
];
const CASES = [
  { id: 'natural',    name: 'Natural' },
  { id: 'upper',      name: 'UPPERCASE' },
  { id: 'sentence',   name: 'Sentence case' },
];
// Caption font families — id is the exact name passed to libass FontName
const FONT_FAMILIES = [
  { id: 'Arial',        name: 'Arial',        stack: 'Arial, sans-serif',                note: 'System · universal' },
  { id: 'Arial Black',  name: 'Arial Black',  stack: '"Arial Black", sans-serif',        note: 'System · heavy' },
  { id: 'Impact',       name: 'Impact',        stack: 'Impact, sans-serif',               note: 'System · classic' },
  { id: 'Montserrat',   name: 'Montserrat',    stack: 'Montserrat, sans-serif',           note: 'Social · bold' },
  { id: 'Bebas Neue',   name: 'Bebas Neue',    stack: '"Bebas Neue", sans-serif',         note: 'Social · condensed' },
  { id: 'Oswald',       name: 'Oswald',        stack: 'Oswald, sans-serif',               note: 'Social · tall' },
  { id: 'DM Sans',      name: 'DM Sans',       stack: '"DM Sans", sans-serif',            note: 'Clean · modern' },
];
function savePreset() {
  try { localStorage.setItem(PRESET_KEY, JSON.stringify(presetState)); } catch (e) { /* */ }
}

function renderCaptionGrid() {
  const grid = $('#caption-grid');
  grid.innerHTML = '';
  CAPTION_PRESETS.forEach((p) => {
    const card = el('button', 'caption-card');
    card.type = 'button';
    if (presetState.captionId === p.id) card.classList.add('selected');
    const preview = el('div', 'caption-preview');
    preview.style.background = p.bg === '#FFFFFF' ? '#fafafa' : p.bg;
    const span = el('span');
    span.style.background = p.bg;
    span.style.color = p.fg;
    span.style.borderRadius = presetState.rounded ? '10px' : '0';
    span.style.boxShadow = presetState.shadow ? '0 4px 14px rgba(0,0,0,0.45)' : 'none';
    span.textContent = p.sample;
    preview.appendChild(span);
    const meta = el('div', 'caption-meta');
    meta.appendChild(el('div', 'cm-name', p.name));
    meta.appendChild(el('div', 'cm-hex', `bg ${p.bg} · text ${p.fg}`));
    const tag = el('div', null, p.tag);
    tag.style.fontSize = '10px';
    tag.style.marginTop = '2px';
    tag.style.color = 'var(--fg-mute)';
    meta.appendChild(tag);
    card.appendChild(preview);
    card.appendChild(meta);
    card.addEventListener('click', () => {
      presetState.captionId = p.id;
      savePreset();
      renderCaptionGrid();
      renderPresetPreview(false);
    });
    grid.appendChild(card);
  });
}

function renderChips(rowId, items, selectedId, onSelect) {
  const row = $('#' + rowId);
  row.innerHTML = '';
  items.forEach((it) => {
    const chip = el('button', 'chip');
    chip.type = 'button';
    if (it.id === selectedId) chip.classList.add('selected');
    chip.textContent = it.name;
    if (it.note) {
      const meta = el('span', 'chip-meta', it.note);
      chip.appendChild(meta);
    }
    if (it.w && it.h) {
      const meta = el('span', 'chip-meta', `${it.w}×${it.h}${it.def ? ' · default' : ''}`);
      chip.appendChild(meta);
    }
    chip.addEventListener('click', () => {
      onSelect(it.id);
      renderPresetPreview(false);
    });
    row.appendChild(chip);
  });
}

function renderFontRow() {
  const row = $('#font-family-row');
  if (!row) return;
  row.innerHTML = '';
  for (const f of FONT_FAMILIES) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip font-chip' + (presetState.fontFamily === f.id ? ' selected' : '');
    // Show the chip label in its own font so the user can see what they're picking
    chip.style.fontFamily = f.stack;
    const nm = document.createElement('span');
    nm.textContent = f.name;
    const meta = document.createElement('span');
    meta.className = 'chip-meta';
    meta.style.fontFamily = 'var(--mono)'; // meta always monospace, not the preview font
    meta.textContent = f.note;
    chip.append(nm, meta);
    chip.addEventListener('click', () => {
      presetState.fontFamily = f.id;
      savePreset();
      renderFontRow();
    });
    row.appendChild(chip);
  }
}

function bindPresetInputs() {
  const hook = $('#caption-hook');
  hook.value = presetState.hook;
  hook.addEventListener('input', () => { presetState.hook = hook.value; savePreset(); });
  const rounded = $('#caption-rounded');
  rounded.checked = presetState.rounded;
  rounded.addEventListener('change', () => { presetState.rounded = rounded.checked; savePreset(); renderCaptionGrid(); });
  const shadow = $('#caption-shadow');
  shadow.checked = presetState.shadow;
  shadow.addEventListener('change', () => { presetState.shadow = shadow.checked; savePreset(); renderCaptionGrid(); });
  const extra = $('#preset-extra');
  extra.value = presetState.extra;
  extra.addEventListener('input', () => { presetState.extra = extra.value; savePreset(); });
  // Font size slider + number input
  bindRangePair('font-size-slider', 'font-size', 'fontSize', (v) => parseInt(v, 10));
}

function buildPresetPrompt() {
  const cap = CAPTION_PRESETS.find((p) => p.id === presetState.captionId) || CAPTION_PRESETS[0];
  const place = PLACEMENTS.find((p) => p.id === presetState.placementId) || PLACEMENTS[3];
  const aspect = ASPECTS.find((a) => a.id === presetState.aspectId) || ASPECTS[0];
  const hook = (presetState.hook || '').trim();
  const layout = LAYOUTS.find((l) => l.id === presetState.layout) || LAYOUTS[0];
  const caseStyle = CASES.find((c) => c.id === presetState.caseStyle) || CASES[0];
  const maxLines = presetState.layout === 'double' ? 2 : 1;

  const lines = [];
  lines.push(`Apply these settings to the current edit:`);
  lines.push('');
  lines.push(`**Caption style — ${cap.name}**`);
  lines.push(`- Background: ${cap.bg}`);
  lines.push(`- Text:       ${cap.fg}`);
  const fontDef = FONT_FAMILIES.find((f) => f.id === presetState.fontFamily) || FONT_FAMILIES[0];
  lines.push(`- Font family: ${fontDef.id} — set FontName="${fontDef.id}" in the ASS subtitle style. If the font is not installed on the system, fall back to Arial Bold.`);
  lines.push(`- Font size: ${presetState.fontSize}pt (set Fontsize=${presetState.fontSize} in the ASS style block). Tight tracking, comfortable padding (10–14px H, 6–8px V).`);
  lines.push(`- Corners: ${presetState.rounded ? 'rounded (~12px radius)' : 'square'}`);
  lines.push(`- Drop shadow: ${presetState.shadow ? 'subtle (0 4px 14px rgba(0,0,0,0.45))' : 'none'}`);
  lines.push(`- Case: ${caseStyle.name}`);
  if (hook) lines.push(`- Hook text override (apply to the first kept phrase or as a static intro card): "${hook}"`);
  lines.push('');
  lines.push(`**Caption format (Premiere-style chunking)**`);
  lines.push(`- Layout: ${layout.name} — wrap to a maximum of ${maxLines} line${maxLines > 1 ? 's' : ''} per caption block.`);
  lines.push(`- Max ${presetState.maxChars} characters per line (counting spaces). When chunking the SRT, break at the nearest word boundary BEFORE this limit; never split a word.`);
  lines.push(`- Minimum on-screen duration: ${presetState.minDuration.toFixed(1)} seconds. If the natural word timing for a chunk is shorter, EXTEND the caption end time to the minimum (or merge with the next chunk if that fits within ${presetState.maxChars}×${maxLines} chars).`);
  lines.push(`- Gap between caption blocks: ${presetState.gapFrames} frame${presetState.gapFrames === 1 ? '' : 's'} (at 24fps that's ${(presetState.gapFrames / 24 * 1000).toFixed(0)}ms). 0 = back-to-back.`);
  lines.push(`- Tail-pad each caption by +250ms (or whatever it takes to outlast the spoken final consonant) so the last line never reads as "cut off". Subtitles burn LAST per the philosophy doc, so this is a render.py / SRT-build setting, not an EDL change.`);
  lines.push('');
  // EXPLICIT ASS alignment + MarginV values. Prose like "≈ 25% from frame top"
  // was being interpreted as Alignment=8/MarginV=480 (top-anchor upper-third)
  // even when the user picked Lower third. ASS uses a numeric-keypad alignment
  // and MarginV is measured from whichever edge that anchor points to — so
  // we compute both here so Claude doesn't have to translate the prose.
  // Reference (anchor edge → MarginV is distance FROM that edge):
  //   top         → Alignment=8 (top-center), MarginV=80   (~4% from top)
  //   upper-third → Alignment=8 (top-center), MarginV=480  (~25% from top)
  //   center      → Alignment=5 (true center), MarginV=0
  //   lower-third → Alignment=2 (bottom-center), MarginV=540 (~28% up from bottom = 72% from top)
  //   bottom      → Alignment=2 (bottom-center), MarginV=80  (~4% from bottom)
  const ASS_PLACEMENT = {
    'top':         { alignment: 8, marginV: 80,  y: '~4% from top'    },
    'upper-third': { alignment: 8, marginV: 480, y: '~25% from top'   },
    'center':      { alignment: 5, marginV: 0,   y: 'true center'     },
    'lower-third': { alignment: 2, marginV: 540, y: '~28% from bottom (= 72% from top)' },
    'bottom':      { alignment: 2, marginV: 80,  y: '~4% from bottom' },
  };
  const ap = ASS_PLACEMENT[presetState.placementId] || ASS_PLACEMENT['lower-third'];
  lines.push(`**Placement: ${place.name}** — ${ap.y}, horizontally centered`);
  lines.push(`- ASS subtitle style MUST use: \`Alignment=${ap.alignment}, MarginV=${ap.marginV}\` (with PlayResY=1920 for vertical aspects).`);
  lines.push(`- Do NOT pick a different alignment/margin combo. The placement chip is authoritative.`);
  lines.push('');
  lines.push(`**Aspect ratio: ${aspect.name} at ${aspect.w}×${aspect.h}**`);
  lines.push(`- Resize the source proportionally to fit. Do **not** crop or pillarbox unless I ask.`);
  lines.push(`- For vertical targets from a horizontal source: scale to fit width, blur-extend (background blur) for the top/bottom bars.`);
  lines.push(`- For horizontal targets from a vertical source: scale to fit height, blur-extend the side bars.`);
  lines.push('');
  lines.push(`**Order of operations** (per the trimming + render philosophies):`);
  lines.push(`1. Lock the cut first (silence trim → word-boundary EDL → 30ms fades).`);
  lines.push(`2. Resize to target aspect.`);
  lines.push(`3. Build the master SRT with the chunking rules above (max-chars, min-duration, gap, tail-pad).`);
  lines.push(`4. Render captions LAST — after every overlay, after the cut is approved.`);
  lines.push('');
  lines.push(`**Render command — use these exact flags** (they're already wired in render.py):`);
  lines.push('```');
  lines.push(`python video-use/helpers/render.py videos/edit/edl.json -o videos/edit/final.mp4 \\`);
  lines.push(`    --build-subtitles \\`);
  lines.push(`    --caption-max-chars ${presetState.maxChars} \\`);
  lines.push(`    --caption-max-lines ${maxLines} \\`);
  lines.push(`    --caption-min-duration ${presetState.minDuration.toFixed(1)} \\`);
  lines.push(`    --caption-gap-frames ${presetState.gapFrames} \\`);
  lines.push(`    --caption-tail-pad 250 \\`);
  lines.push(`    --caption-case ${presetState.caseStyle} \\`);
  lines.push(`    --encoder auto`);
  lines.push('```');
  if (presetState.extra && presetState.extra.trim()) {
    lines.push('');
    lines.push(`**Extra instructions:**`);
    lines.push(presetState.extra.trim());
  }
  lines.push('');
  lines.push(`Show me the cut plan as a table before rendering. Wait for "approve".`);
  return lines.join('\n');
}

function renderPresetPreview(show) {
  const prev = $('#preset-preview');
  prev.textContent = buildPresetPrompt();
  if (show) prev.hidden = false;
}

$('#preset-preview-btn').addEventListener('click', () => {
  try {
    const prev = $('#preset-preview');
    prev.hidden = !prev.hidden;
    if (!prev.hidden) renderPresetPreview(true);
  } catch (e) {
    console.error('preview prompt failed:', e);
    alert(`preview prompt failed: ${e.message}`);
  }
});
// (apply & send removed — captions auto-apply to the next Chat tab send)

function pickPlacement(id) { presetState.placementId = id; savePreset(); renderPlacementRow(); }
function pickAspect(id)    { presetState.aspectId    = id; savePreset(); renderAspectRow(); }
function pickLayout(id)    { presetState.layout      = id; savePreset(); renderLayoutRow(); }
function pickCase(id)      { presetState.caseStyle   = id; savePreset(); renderCaseRow(); renderCaptionGrid(); }
function renderPlacementRow() {
  renderChips('placement-row', PLACEMENTS, presetState.placementId, pickPlacement);
}
function renderAspectRow() {
  renderChips('aspect-row', ASPECTS, presetState.aspectId, pickAspect);
}
function renderLayoutRow() {
  renderChips('layout-row', LAYOUTS, presetState.layout, pickLayout);
}
function renderCaseRow() {
  renderChips('case-row', CASES, presetState.caseStyle, pickCase);
}

// Wire a slider + number input pair so they stay in sync and clamp to range.
function bindRangePair(sliderId, numberId, key, parseFn = Number) {
  const slider = $('#' + sliderId);
  const number = $('#' + numberId);
  const initial = presetState[key];
  slider.value = initial;
  number.value = initial;
  const update = (v) => {
    const min = parseFn(slider.min), max = parseFn(slider.max);
    let val = parseFn(v);
    if (Number.isNaN(val)) val = parseFn(presetState[key]);
    val = Math.max(min, Math.min(max, val));
    presetState[key] = val;
    slider.value = val;
    number.value = val;
    savePreset();
  };
  slider.addEventListener('input', () => update(slider.value));
  number.addEventListener('change', () => update(number.value));
  number.addEventListener('blur', () => update(number.value));
}

function initPresets() {
  renderCaptionGrid();
  renderLayoutRow();
  renderCaseRow();
  renderFontRow();
  renderPlacementRow();
  renderAspectRow();
  bindRangePair('max-chars-slider', 'max-chars', 'maxChars', (v) => parseInt(v, 10));
  bindRangePair('min-duration-slider', 'min-duration', 'minDuration', (v) => parseFloat(v));
  bindRangePair('gap-frames-slider', 'gap-frames', 'gapFrames', (v) => parseInt(v, 10));
  bindPresetInputs();
}

// ============================================================
// SESSIONS
// ============================================================
const SESSIONS_KEY = 'veditor.sessions.v1';
let sessions = loadSessions();
let currentSessionId = null;
let currentSessionName = null;
let currentSessionTurns = 0;
let currentSessionCost = 0;

function loadSessions() {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) { /* */ }
  return [];
}
function saveSessions() {
  try { localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions)); } catch (e) { /* */ }
}

function snapshotConversationHTML() {
  return conversation.innerHTML;
}

function upsertSession(opts = {}) {
  if (!currentSessionId) return;
  const idx = sessions.findIndex((s) => s.id === currentSessionId);
  const record = {
    id: currentSessionId,
    name: currentSessionName || `Session ${new Date().toLocaleString()}`,
    started: idx >= 0 ? sessions[idx].started : Date.now(),
    lastActive: Date.now(),
    video: currentLoaded || (idx >= 0 ? sessions[idx].video : null),
    turns: currentSessionTurns,
    cost: currentSessionCost,
    html: snapshotConversationHTML(),
    ...opts,
  };
  if (idx >= 0) sessions[idx] = record;
  else sessions.unshift(record);
  saveSessions();
  // Best-effort server mirror so the on-disk store knows about it.
  fetch('/api/sessions/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: record.id, name: record.name, video: record.video,
      lastActive: record.lastActive, turns: record.turns, cost: record.cost,
    }),
  }).catch(() => { /* server endpoint may not exist yet — fine */ });
  $('#session-active-info').textContent =
    `active: ${record.name} · ${record.turns} turns · ${fmtCost(record.cost)}`;
}

function renderSessionList() {
  const list = $('#session-list');
  list.innerHTML = '';
  if (!sessions.length) {
    list.appendChild(el('li', 'group', '— none yet —'));
    return;
  }
  sessions.forEach((s) => {
    const li = el('li', 'session-item');
    if (s.id === currentSessionId) li.classList.add('active');
    const top = el('div');
    top.appendChild(el('div', 'si-name', s.name));
    top.appendChild(el('div', 'si-meta',
      `${s.turns || 0} turns · ${fmtCost(s.cost || 0)} · ${fmtAgo(s.lastActive)}` +
      (s.video ? ` · ${s.video}` : '')));
    const actions = el('div', 'si-actions');
    const open = el('button', null, 'open');
    open.addEventListener('click', (e) => { e.stopPropagation(); openSession(s); });
    const rename = el('button', 'ghost', 'rename');
    rename.addEventListener('click', (e) => {
      e.stopPropagation();
      const n = prompt('rename session:', s.name);
      if (n != null && n.trim()) { s.name = n.trim(); saveSessions(); renderSessionList(); }
    });
    const del = el('button', 'ghost', '×');
    del.title = 'delete';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!confirm(`delete "${s.name}"?`)) return;
      sessions = sessions.filter((x) => x.id !== s.id);
      saveSessions();
      renderSessionList();
    });
    actions.appendChild(open);
    actions.appendChild(rename);
    actions.appendChild(del);
    li.appendChild(top);
    li.appendChild(actions);
    li.appendChild(el('div', 'si-id', `id ${s.id.slice(0, 8)}…`));
    li.addEventListener('click', () => openSession(s));
    list.appendChild(li);
  });
}

function openSession(s) {
  currentSessionId = s.id;
  currentSessionName = s.name;
  currentSessionTurns = s.turns || 0;
  currentSessionCost = s.cost || 0;
  conversation.innerHTML = s.html || '';
  if (s.video) loadVideo(s.video);
  $('#session-active-info').textContent =
    `active: ${s.name} · ${s.turns || 0} turns · ${fmtCost(s.cost || 0)} · view-only until restart`;
  activateTab('chat');
  addMsg('system',
    `Opened "${s.name}" — viewing snapshot. New prompts here start a fresh thread (true conversation resume needs a server restart).`);
}

function newSession() {
  if (currentSessionId && currentSessionTurns > 0) {
    upsertSession();
  }
  currentSessionId = null;
  currentSessionName = null;
  currentSessionTurns = 0;
  currentSessionCost = 0;
  conversation.innerHTML = '';
  conversation.appendChild(el('div', 'empty',
    'Fresh session. Drop a video and describe what you want, or use a preset.'));
  $('#session-active-info').textContent = 'no active session yet';
  // Force the next /api/chat call to NOT pass --continue.
  continueToggle.checked = false;
  renderSessionList();
}

$('#session-new-btn').addEventListener('click', newSession);
$('#session-save-btn').addEventListener('click', () => {
  if (!currentSessionId) {
    alert('No session is active yet — send at least one prompt first.');
    return;
  }
  const n = prompt('save session as:', currentSessionName || '');
  if (n != null && n.trim()) {
    currentSessionName = n.trim();
    upsertSession();
    renderSessionList();
  }
});

// ============================================================
// CHAT / STREAMING
// ============================================================
function renderStreamEvent(evt, currentAssistant) {
  if (!evt || !evt.type) return currentAssistant;
  noteEvent();
  pushLog(evt);

  if (evt.type === '_done') {
    if (evt.exit_code !== 0) {
      addMsg('error', `claude exited ${evt.exit_code}\n${evt.stderr || ''}`);
    }
    return currentAssistant;
  }

  if (evt.type === 'system') {
    if (evt.subtype === 'init' && evt.session_id && !currentSessionId) {
      currentSessionId = evt.session_id;
      currentSessionName = currentSessionName || `Session ${new Date().toLocaleTimeString()}`;
    }
    if (evt.subtype === 'skipped_oversize_event') {
      addMsg('system', `[skipped oversize event · ${evt.consumed} bytes]`);
    } else if (evt.subtype && evt.subtype !== 'init') {
      addMsg('system', `[${evt.subtype}]`);
    }
    return currentAssistant;
  }

  if (evt.type === 'assistant' && evt.message) {
    setActivity('writing response…');
    const blocks = evt.message.content || [];
    for (const b of blocks) {
      if (b.type === 'text') {
        if (!currentAssistant) currentAssistant = addMsg('assistant', '');
        currentAssistant.textContent += b.text;
        conversation.scrollTop = conversation.scrollHeight;
      } else if (b.type === 'tool_use') {
        const summary = `${b.name}(${JSON.stringify(b.input).slice(0, 200)})`;
        addMsg('tool', summary);
        setActivity(`running: ${b.name}`);
        // Track tool start time so we can record duration when result comes in.
        b._veditor_started = Date.now();
        b._veditor_ops = detectOperations(b.name, b.input);
        if (!window.__pendingToolUses) window.__pendingToolUses = {};
        window.__pendingToolUses[b.id] = b;
        currentAssistant = null;
      }
    }
    return currentAssistant;
  }

  if (evt.type === 'user' && evt.message) {
    const blocks = evt.message.content || [];
    for (const b of blocks) {
      if (b.type === 'tool_result') {
        const content = typeof b.content === 'string'
          ? b.content
          : JSON.stringify(b.content).slice(0, 1200);
        addMsg('tool', `↳ ${content.slice(0, 1200)}`);
        setActivity('thinking…');
        // Pair with its tool_use, record op + duration in the active job.
        const pending = (window.__pendingToolUses || {})[b.tool_use_id];
        if (pending && activeJob) {
          const dur = Date.now() - (pending._veditor_started || Date.now());
          (pending._veditor_ops || []).forEach((op) => {
            recordOpInJob(op, pending.name, dur);
          });
          recordOutputs(detectOutputFiles(b.content));
          delete window.__pendingToolUses[b.tool_use_id];
        }
      }
    }
    return currentAssistant;
  }

  if (evt.type === 'result') {
    const u = evt.usage || {};
    const turnIn = u.input_tokens || 0;
    const turnOut = u.output_tokens || 0;
    const turnCacheRead = u.cache_read_input_tokens || 0;
    const turnCacheCreate = u.cache_creation_input_tokens || 0;
    const turnCost = evt.total_cost_usd ?? evt.cost_usd ?? 0;
    const turnDurMs = evt.duration_ms || 0;

    usage.turns += 1;
    usage.inTokens += turnIn;
    usage.outTokens += turnOut;
    usage.cacheRead += turnCacheRead;
    usage.cacheCreate += turnCacheCreate;
    usage.costUsd += turnCost;
    renderUsage(); saveUsage(); flashUsage('usage');

    // Accumulate into the active job too.
    if (activeJob) {
      activeJob.turns += 1;
      activeJob.cost_usd += turnCost;
      activeJob.tokens_in += turnIn;
      activeJob.tokens_out += turnOut;
      activeJob.tokens_cache += (turnCacheRead + turnCacheCreate);
    }

    promptUsage.tokens += turnIn + turnOut + turnCacheRead + turnCacheCreate;
    promptUsage.cost += turnCost;
    renderPromptUsage(); flashUsage('usage-prompt');

    currentSessionTurns += 1;
    currentSessionCost += turnCost;

    logTurn({
      prompt: lastPrompt,
      turn: usage.turns,
      input_tokens: turnIn,
      output_tokens: turnOut,
      cache_read: turnCacheRead,
      cache_create: turnCacheCreate,
      cost_usd: turnCost,
      duration_ms: turnDurMs,
      is_error: !!(evt.is_error || evt.subtype === 'error_during_execution'),
    });

    const stats = el('div', 'turn-stats');
    const total = turnIn + turnOut + turnCacheRead + turnCacheCreate;
    const modelTag = modelSelect.value || 'default';
    stats.innerHTML =
      `turn ${usage.turns} · <span style="color:var(--accent)">${modelTag}</span> ` +
      `· ${fmtNum(total)} tok ` +
      `(${fmtNum(turnIn)} in, ${fmtNum(turnOut)} out, ` +
      `${fmtNum(turnCacheRead)} cache↻, ${fmtNum(turnCacheCreate)} cache+) ` +
      `· <span class="ts-cost">$${turnCost.toFixed(4)}</span> ` +
      `· ${(turnDurMs / 1000).toFixed(1)}s`;
    if (evt.is_error || evt.subtype === 'error_during_execution') {
      stats.innerHTML += ` · <span class="ts-err">${evt.subtype || 'error'}</span>`;
      addMsg('error', evt.result || 'execution error');
    }
    conversation.appendChild(stats);
    conversation.scrollTop = conversation.scrollHeight;

    upsertSession();
    refreshFiles();
    return null;
  }

  return currentAssistant;
}

async function sendPrompt(prompt) {
  // Finalize any previous open job before starting a new one.
  if (activeJob) await finalizeJob();
  await startJob(prompt);

  lastPrompt = prompt;
  promptUsage = { tokens: 0, cost: 0 };
  renderPromptUsage();
  addMsg('user', prompt);
  sendBtn.disabled = true;
  sendBtn.hidden = true;
  stopBtn.hidden = false;
  startStatus();

  const fd = new FormData();
  fd.append('prompt', prompt);
  fd.append('continue_session', continueToggle.checked ? 'true' : 'false');
  if (currentSessionId) fd.append('resume_session_id', currentSessionId);
  // One-shot override (set by sync actions) takes priority; fall back to the
  // global MODEL dropdown in the prompt bar.
  const effectiveModel = nextSubmitModelOverride || modelSelect.value;
  // Autonomous batch runs let the server route the model; everything else is an
  // explicit pick that should win (force_model).
  const routed = nextSubmitRouted;
  nextSubmitRouted = false;
  nextSubmitModelOverride = null;
  if (effectiveModel) fd.append('model', effectiveModel);
  fd.append('force_model', routed ? 'false' : 'true');

  activeAbort = new AbortController();
  let currentAssistant = null;
  let runAborted = false;

  try {
    const r = await fetch('/api/chat', { method: 'POST', body: fd, signal: activeAbort.signal });
    if (!r.ok) {
      addMsg('error', `${r.status}: ${await r.text()}`);
      return;
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!block.startsWith('data: ')) continue;
        const payload = block.slice(6);
        try {
          const evt = JSON.parse(payload);
          currentAssistant = renderStreamEvent(evt, currentAssistant);
        } catch (e) {
          addMsg('system', payload.slice(0, 300));
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') runAborted = true;
    else addMsg('error', e.message);
  } finally {
    sendBtn.disabled = false;
    sendBtn.hidden = false;
    stopBtn.hidden = true;
    activeAbort = null;
    stopStatus();
    refreshFiles();
    // Finalize the job for this prompt now that the stream closed.
    finalizeJob();
    // Chime when a run actually finishes (not when the user hits Stop).
    if (!runAborted) playDing();
    // Re-enable continue toggle after a fresh-start prompt.
    if (!continueToggle.checked) continueToggle.checked = true;
  }
}

// When true, the next submit skips pipeline-wrapping (used by preset / sync /
// auto-pair flows that already build their own complete prompt).
let skipPipelineWrap = false;

promptForm.addEventListener('submit', (e) => {
  e.preventDefault();
  let v = promptInput.value.trim();
  try {

  // ---- Mode: split hooks ----
  // Highest-priority composer mode. Requires a video; freeform is optional.
  if (modeSplitHooks && modeSplitHooks.checked && !skipPipelineWrap) {
    const videoPath = composerVideoSelect && composerVideoSelect.value;
    if (!videoPath) {
      alert('Split hooks mode is on but no video is picked. Pick a video in the composer first.');
      return;
    }
    console.log('[veditor] mode: split-hooks. video=', videoPath, 'notes=', v || '(none)');
    v = buildSplitHooksPrompt(videoPath, v);
    nextSubmitModelOverride = composerModelSelect.value || nextSubmitModelOverride;
    promptInput.value = '';
    sendPrompt(v);
    return;
  }

  if (!v) return;

  // If the pipeline composer has a video selected, wrap the prompt
  // as a full pipeline build instead of sending it raw.
  const pipelineVideo = composerVideoSelect && composerVideoSelect.value;
  if (pipelineVideo && !skipPipelineWrap) {
    const brollMode = brollState.mode || 'none';
    const brollFolder = (brollState.folder || '').trim();
    const voSelected = composerVoSelect.value;
    // If the VO dropdown is set to the pending-generate sentinel, don't pass
    // it as an existing VO file — instead, route through the pendingVo block.
    const voIsPending = voSelected === PENDING_VO_OPTION;
    const pendingVo = (voIsPending && pendingVoConfig) ? pendingVoConfig : null;
    const quality = composerQualitySelect.value || 'high';
    console.log('[veditor] wrapping prompt as pipeline. video=', pipelineVideo,
                'script=', composerScriptSelect.value,
                'music=',  composerMusicSelect.value,
                'vo=',     voIsPending ? `(pending: ${pendingVo?.output})` : voSelected,
                'broll=',  brollMode, brollFolder,
                'quality=', quality);
    v = buildPipelinePrompt({
      video:    pipelineVideo,
      audio:    composerAudioSelect ? (composerAudioSelect.value || null) : null,
      script:   composerScriptSelect.value || null,
      music:    composerMusicSelect.value  || null,
      vo:       voIsPending ? null : (voSelected || null),
      brollMode,
      brollFolder,
      pendingVo,
      quality,
      freeform: v,
    });
    nextSubmitModelOverride = composerModelSelect.value || nextSubmitModelOverride;
    // Pending VO has been baked into the prompt — clear it so the next send
    // doesn't re-generate a duplicate.
    if (pendingVo) clearPendingVo();
  }
  skipPipelineWrap = false;

  promptInput.value = '';
  sendPrompt(v);
  } catch (err) {
    console.error('[veditor] submit error:', err);
    addMsg('error', `Send failed: ${err.message}\n\nOpen DevTools (F12) → Console for the full stack trace.`);
    sendBtn.disabled = false;
    sendBtn.hidden = false;
    stopBtn.hidden = true;
  }
});

promptInput.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault();
    promptForm.requestSubmit();
  }
});

stopBtn.addEventListener('click', () => {
  if (activeAbort) activeAbort.abort();
});

refreshBtn.addEventListener('click', refreshFiles);

// -------------- hotkeys --------------
document.addEventListener('keydown', (e) => {
  // Don't hijack typing.
  const t = e.target;
  const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA');
  if (e.altKey && !typing) {
    if (e.key === '1') { activateTab('chat'); e.preventDefault(); }
    else if (e.key === '2') { activateTab('captions'); e.preventDefault(); }
    else if (e.key === '3') { activateTab('graphics'); e.preventDefault(); }
    else if (e.key === '4') { activateTab('broll'); e.preventDefault(); }
    else if (e.key === '5') { activateTab('sessions'); e.preventDefault(); }
    else if (e.key === '6') { activateTab('jobs'); e.preventDefault(); }
    else if (e.key === '7') { activateTab('logs'); e.preventDefault(); }
    else if (e.key.toLowerCase() === 'a') { /* apply&send was removed */ }
    else if (e.key.toLowerCase() === 'n') { newSession(); e.preventDefault(); }
  }
});

// ============================================================
// GRAPHICS TAB — auto-motion-graphics keyed to transcript
// ============================================================
const GRAPHICS_PRESETS = [
  { id: 'crisp_callout', name: 'Crisp Callout',  bg: '#FFFFFF', fg: '#0F172A', sample: 'KEY METRIC',          tag: 'White card + dark text · clean SaaS' },
  { id: 'neon_pop',      name: 'Neon Pop',       bg: '#0F172A', fg: '#22D3EE', sample: '↑ 47% conversion',    tag: 'Dark + cyan glow · tech demo' },
  { id: 'pastel_soft',   name: 'Pastel Soft',    bg: '#FEF3C7', fg: '#7C2D12', sample: 'Try it free',         tag: 'Cream + warm brown · friendly' },
  { id: 'mono_stark',    name: 'Mono Stark',     bg: '#000000', fg: '#FFFFFF', sample: 'NEW',                  tag: 'Black + white · editorial' },
  { id: 'corp_navy',     name: 'Corp Navy',      bg: '#1E40AF', fg: '#F8FAFC', sample: 'Enterprise-ready',     tag: 'Navy + cream · B2B trust' },
  { id: 'highlight_yellow', name: 'Highlight',   bg: '#FACC15', fg: '#1A1A1A', sample: 'WATCH THIS',          tag: 'Yellow + black · attention grab' },
];
const GRAPHICS_DENSITIES = [
  { id: 'sparse',  name: 'Sparse',  desc: 'hero beats only · ~1 every 15s' },
  { id: 'medium',  name: 'Medium',  desc: 'key claims · ~1 every 7s' },
  { id: 'dense',   name: 'Dense',   desc: 'every distinct point · ~1 every 4s' },
];
const GRAPHICS_KINDS = [
  { id: 'callout',     name: 'Callout',     desc: 'small text card pinned to a corner' },
  { id: 'lower_third', name: 'Lower third', desc: 'wide bar across the lower portion of frame' },
  { id: 'big_stat',    name: 'Big stat',    desc: 'oversized number or metric flash' },
  { id: 'bullet_list', name: 'Bullet list', desc: 'animated 2–3 item list reveal' },
  { id: 'end_card',    name: 'End card',    desc: 'final-frame logo/CTA card' },
];

const GFX_KEY = 'veditor.graphicsState.v1';
const graphicsState = {
  enabled:   false,
  presetId:  'crisp_callout',
  density:   'medium',
  kinds:     ['callout', 'big_stat', 'lower_third'],
  extra:     '',
};
try {
  const saved = JSON.parse(localStorage.getItem(GFX_KEY) || 'null');
  if (saved && typeof saved === 'object') Object.assign(graphicsState, saved);
} catch {}
function saveGraphics() {
  try { localStorage.setItem(GFX_KEY, JSON.stringify(graphicsState)); } catch {}
}

function renderGraphicsGrid() {
  const grid = $('#graphics-grid');
  if (!grid) return;
  grid.innerHTML = '';
  for (const p of GRAPHICS_PRESETS) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'caption-card';
    if (graphicsState.presetId === p.id) card.classList.add('selected');

    const preview = document.createElement('div');
    preview.className = 'caption-preview';
    preview.style.background = p.bg === '#FFFFFF' ? '#fafafa' : p.bg;
    const span = document.createElement('span');
    span.style.background = p.bg;
    span.style.color = p.fg;
    span.style.borderRadius = '8px';
    span.textContent = p.sample;
    preview.appendChild(span);

    const meta = document.createElement('div');
    meta.className = 'caption-meta';
    const nm = document.createElement('div');
    nm.className = 'cm-name';
    nm.textContent = p.name;
    const hex = document.createElement('div');
    hex.className = 'cm-hex';
    hex.textContent = `bg ${p.bg} · text ${p.fg}`;
    const tag = document.createElement('div');
    tag.style.fontSize = '10px';
    tag.style.marginTop = '2px';
    tag.style.color = 'var(--fg-mute)';
    tag.textContent = p.tag;
    meta.append(nm, hex, tag);

    card.append(preview, meta);
    card.addEventListener('click', () => {
      graphicsState.presetId = p.id;
      saveGraphics();
      renderGraphicsGrid();
    });
    grid.appendChild(card);
  }
}

function renderChipsGeneric(rowId, items, currentId, onClick) {
  const row = document.getElementById(rowId);
  if (!row) return;
  row.innerHTML = '';
  for (const it of items) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    if (it.id === currentId) chip.classList.add('selected');
    chip.appendChild(Object.assign(document.createElement('span'), { textContent: it.name }));
    if (it.desc) {
      const meta = document.createElement('span');
      meta.className = 'chip-meta';
      meta.textContent = it.desc;
      chip.appendChild(meta);
    }
    chip.addEventListener('click', () => onClick(it.id));
    row.appendChild(chip);
  }
}
function renderMultiChips(rowId, items, currentSet, onToggle) {
  const row = document.getElementById(rowId);
  if (!row) return;
  row.innerHTML = '';
  for (const it of items) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    if (currentSet.includes(it.id)) chip.classList.add('selected');
    chip.appendChild(Object.assign(document.createElement('span'), { textContent: it.name }));
    if (it.desc) {
      const meta = document.createElement('span');
      meta.className = 'chip-meta';
      meta.textContent = it.desc;
      chip.appendChild(meta);
    }
    chip.addEventListener('click', () => onToggle(it.id));
    row.appendChild(chip);
  }
}

function renderGraphicsTab() {
  const enable = $('#graphics-enable');
  if (enable) enable.checked = !!graphicsState.enabled;
  renderGraphicsGrid();
  renderChipsGeneric('graphics-density-row', GRAPHICS_DENSITIES, graphicsState.density, (id) => {
    graphicsState.density = id; saveGraphics(); renderGraphicsTab();
  });
  renderMultiChips('graphics-kinds-row', GRAPHICS_KINDS, graphicsState.kinds, (id) => {
    const i = graphicsState.kinds.indexOf(id);
    if (i >= 0) graphicsState.kinds.splice(i, 1);
    else graphicsState.kinds.push(id);
    saveGraphics(); renderGraphicsTab();
  });
  const extra = $('#graphics-extra');
  if (extra) extra.value = graphicsState.extra || '';
}

if ($('#graphics-enable')) {
  $('#graphics-enable').addEventListener('change', (e) => {
    graphicsState.enabled = e.target.checked;
    saveGraphics();
  });
  $('#graphics-extra').addEventListener('input', (e) => {
    graphicsState.extra = e.target.value;
    saveGraphics();
  });
  $('#graphics-preview-btn').addEventListener('click', () => {
    const pre = $('#graphics-preview');
    pre.hidden = !pre.hidden;
    if (!pre.hidden) pre.textContent = buildGraphicsBlock() || '(graphics disabled)';
  });
}

function buildGraphicsBlock() {
  if (!graphicsState.enabled) return '';
  const preset = GRAPHICS_PRESETS.find((p) => p.id === graphicsState.presetId) || GRAPHICS_PRESETS[0];
  const density = GRAPHICS_DENSITIES.find((d) => d.id === graphicsState.density) || GRAPHICS_DENSITIES[1];
  const kindNames = graphicsState.kinds
    .map((id) => GRAPHICS_KINDS.find((k) => k.id === id)?.name)
    .filter(Boolean);
  const lines = [];
  lines.push('### Motion graphics');
  lines.push('Inject auto-generated motion-graphic overlays keyed to the transcript timeline. Use the helper `video-use/helpers/graphics_overlay.py` to render and composite.');
  lines.push('');
  lines.push(`**Style preset:** \`${preset.id}\` — bg \`${preset.bg}\`, fg \`${preset.fg}\` (${preset.tag}).`);
  lines.push(`**Density:** ${density.name} — ${density.desc}.`);
  lines.push(`**Allowed kinds:** ${kindNames.join(', ') || 'callout'}.`);
  if (graphicsState.extra && graphicsState.extra.trim()) {
    lines.push(`**Operator extra:** ${graphicsState.extra.trim()}`);
  }
  lines.push('');
  lines.push('**Workflow:**');
  lines.push('1. Read the cut video\'s word-level transcript (run `transcribe.py` if not cached).');
  lines.push('2. Walk the transcript and identify graphic moments at the chosen density. For each moment, pick a kind from the allowed list and write 3–8 words of display text drawn from or summarizing what the speaker just said. Use `start` = first word of the spoken claim, `end` = `start + 2.0` to `start + 3.5` depending on text length.');
  lines.push('3. Build a graphics EDL and save it to `videos/edit/graphics_edl.json` in this shape:');
  lines.push('');
  lines.push('```json');
  lines.push('[');
  lines.push(`  { "at": 4.20, "duration": 2.5, "kind": "big_stat",    "text": "47% faster", "preset": "${preset.id}" },`);
  lines.push(`  { "at": 12.80, "duration": 3.0, "kind": "callout",    "text": "Real-time sync", "anchor": "top-right", "preset": "${preset.id}" },`);
  lines.push(`  { "at": 22.40, "duration": 3.5, "kind": "lower_third","text": "Trusted by 5,000+ teams", "preset": "${preset.id}" }`);
  lines.push(']');
  lines.push('```');
  lines.push('');
  lines.push('4. Run the helper:');
  lines.push('```');
  lines.push('PYTHONUTF8=1 $VU_PY \\');
  lines.push('  video-use/helpers/graphics_overlay.py <input.mp4> \\');
  lines.push('  --edl videos/edit/graphics_edl.json \\');
  lines.push('  --output videos/edit/<input>_gfx.mp4 --json');
  lines.push('```');
  lines.push('');
  lines.push('5. Captions burn AFTER graphics if both are enabled — graphics first, then captions overlay on the graphics-composited output.');
  return lines.join('\n');
}

// ============================================================
// B-ROLL TAB — moved out of the composer
// ============================================================
const BROLL_MODES_LIST = [
  { id: 'none',    name: 'Off',           desc: 'no b-roll on this run' },
  { id: 'keyword', name: 'Keyword map',   desc: 'follow a fixed keyword→category map' },
  { id: 'script',  name: 'Script-driven', desc: 'agent reasons per script beat' },
];
const BROLL_DEFAULT_CATEGORIES = [
  { id: 'Install',   name: 'Install' },
  { id: 'Final',     name: 'Final' },
  { id: 'Before',    name: 'Before' },
  { id: 'evergreen', name: 'evergreen' },
  { id: 'Timelapse', name: 'Timelapse' },
];
const BROLL_DENSITIES = [
  { id: 'light',  name: 'Light',  desc: '~25% of talking head replaced' },
  { id: 'medium', name: 'Medium', desc: '~50% replaced (default)' },
  { id: 'heavy',  name: 'Heavy',  desc: '~75% replaced' },
];

const BROLL_KEY = 'veditor.brollState.v1';
const brollState = {
  mode:       'none',
  folder:     '',
  categories: ['Install', 'Final', 'Before', 'evergreen'],  // Timelapse off by default
  density:    'medium',
  extra:      '',
};
try {
  const saved = JSON.parse(localStorage.getItem(BROLL_KEY) || 'null');
  if (saved && typeof saved === 'object') Object.assign(brollState, saved);
  // Migrate from old per-key storage if present
  const legacyMode = localStorage.getItem('veditor.brollMode.v1');
  if (legacyMode && !saved) brollState.mode = legacyMode;
  const legacyFolder = localStorage.getItem('veditor.brollFolder.v1');
  if (legacyFolder && !saved) brollState.folder = legacyFolder;
} catch {}
function saveBroll() {
  try { localStorage.setItem(BROLL_KEY, JSON.stringify(brollState)); } catch {}
}

function renderBrollTab() {
  const folder = $('#broll-folder-input');
  if (folder) folder.value = brollState.folder || '';
  renderChipsGeneric('broll-mode-row', BROLL_MODES_LIST, brollState.mode, (id) => {
    brollState.mode = id; saveBroll(); renderBrollTab();
  });
  renderMultiChips('broll-categories-row', BROLL_DEFAULT_CATEGORIES, brollState.categories, (id) => {
    const i = brollState.categories.indexOf(id);
    if (i >= 0) brollState.categories.splice(i, 1);
    else brollState.categories.push(id);
    saveBroll(); renderBrollTab();
  });
  renderChipsGeneric('broll-density-row', BROLL_DENSITIES, brollState.density, (id) => {
    brollState.density = id; saveBroll(); renderBrollTab();
  });
  const extra = $('#broll-extra');
  if (extra) extra.value = brollState.extra || '';
}

if ($('#broll-folder-input')) {
  $('#broll-folder-input').addEventListener('input', (e) => {
    brollState.folder = e.target.value.trim(); saveBroll();
  });
  $('#broll-extra').addEventListener('input', (e) => {
    brollState.extra = e.target.value; saveBroll();
  });
  $('#broll-preview-btn').addEventListener('click', () => {
    const pre = $('#broll-preview');
    pre.hidden = !pre.hidden;
    if (!pre.hidden) pre.textContent = buildBrollBlock() || '(b-roll mode is off)';
  });
}

function buildBrollBlock() {
  if (brollState.mode === 'none' || !brollState.folder) return '';
  const allowed = brollState.categories.length
    ? brollState.categories.map((c) => `\`${c}/\``).join(', ')
    : '(none — empty list)';
  const density = BROLL_DENSITIES.find((d) => d.id === brollState.density) || BROLL_DENSITIES[1];
  const lines = [];
  lines.push('### B-roll cutaways');
  lines.push(`Splice b-roll cutaways into the master video using \`video-use/helpers/broll_overlay.py\`. The b-roll root is \`${brollState.folder}\`. Allowed category subfolders for this run: ${allowed}.`);
  lines.push('');
  lines.push('**Hard rules:**');
  lines.push('- B-roll audio is always muted. Talking-head audio (or VO) plays continuously.');
  lines.push('- If a generated VO is present, b-roll covers most of the visual; with a talking head, cut to b-roll for 2–4 s beats and return to the face.');
  lines.push(`- Density: **${density.name}** — ${density.desc}.`);
  lines.push('- Never repeat the same in/out range from the same source clip in a single video.');
  lines.push('- Track which `(source_path, source_in, source_in+duration)` ranges have been consumed.');
  lines.push('- **`Timelapse/` rule:** do NOT pick from `Timelapse/` unless the operator explicitly mentions "timelapse" / "time-lapse" / "time lapse" in the freeform notes below. When timelapse IS requested, use the entire clip (`source_in: 0`, `end - start = full clip duration`); one timelapse = one cutaway.');
  lines.push('');
  if (brollState.mode === 'keyword') {
    lines.push('**Mode: keyword map.** Walk the script (or talking-head transcript) sentence by sentence. For each sentence, pick the category whose keywords match best. If no category matches, default to `evergreen/` if it\'s allowed; otherwise skip.');
    lines.push('');
    lines.push('Default keyword map (case-insensitive):');
    lines.push('- `Install/`: install, installing, installation, installer, setup, mount, mounting, fitting, replace, replacement, swap, swapping, putting in, taking out');
    lines.push('- `Before/`: old, broken, drafty, draft, leak, leaky, problem, damage, damaged, worn, worn out, original, existing, outdated, decrepit, rotted, cracked');
    lines.push('- `Final/`: new, finished, done, complete, completed, beautiful, modern, fresh, transformed, upgrade, upgraded, improved, gorgeous, stunning, clean');
    lines.push('- `evergreen/`: catch-all (house, home, window, exterior, family, neighborhood, generic establishing shots)');
  } else if (brollState.mode === 'script') {
    lines.push('**Mode: script-driven.** Read the full script in context, segment into beats, and pick a category per beat by meaning. Map intent → folder using the allowed-categories list above.');
  }
  lines.push('');
  if (brollState.extra && brollState.extra.trim()) {
    lines.push(`**Operator extra:** ${brollState.extra.trim()}`);
    lines.push('');
  }
  lines.push('**Workflow:**');
  lines.push('1. Lock the cut + audio levels first (no captions yet).');
  lines.push('2. Build a b-roll EDL JSON `[{"start": s, "end": s, "source": "<rel_or_abs_path>", "source_in": s}, ...]` and save it to `videos/edit/broll_edl.json`.');
  lines.push('3. Run `broll_overlay.py <cut.mp4> --edl videos/edit/broll_edl.json --broll-root "<broll root>" --output videos/edit/<cut>_broll.mp4 --json`. If a generated VO exists, also pass `--audio-source <vo.wav>`.');
  lines.push('4. Burn captions LAST onto the b-roll-composited output.');
  return lines.join('\n');
}

// ============================================================
// CAPTIONS BLOCK — emits from existing presetState (with on/off toggle)
// ============================================================
const CAPTIONS_ENABLED_KEY = 'veditor.captionsEnabled.v1';
let captionsEnabled = true;
try {
  const saved = localStorage.getItem(CAPTIONS_ENABLED_KEY);
  if (saved !== null) captionsEnabled = saved === '1';
} catch {}
const captionsEnableToggle = $('#captions-enable');
if (captionsEnableToggle) {
  captionsEnableToggle.checked = captionsEnabled;
  captionsEnableToggle.addEventListener('change', (e) => {
    captionsEnabled = e.target.checked;
    try { localStorage.setItem(CAPTIONS_ENABLED_KEY, captionsEnabled ? '1' : '0'); } catch {}
  });
}

function buildCaptionsBlock() {
  if (!captionsEnabled) return '';
  if (typeof buildPresetPrompt !== 'function') return '';
  let raw;
  try { raw = buildPresetPrompt(); } catch { return ''; }
  if (!raw) return '';
  return '### Caption style\n' + raw.trim();
}

// -------------- init --------------
renderUsage();
renderPromptUsage();
refreshLifetime();
checkHealth();
refreshFiles();
initPresets();
renderSessionList();
refreshSyncUI();
renderGraphicsTab();
renderBrollTab();
// initMusicTab() is called below, AFTER musicSfxState is declared (it lives in
// the Music/SFX block further down to keep state and tab init colocated). If
// you call it here, you'll hit a TDZ error: "Cannot access 'musicSfxState'
// before initialization."
setInterval(refreshFiles, 6000);

// ============================================================
// SHARED FOLDER PICKER MODAL
// ============================================================
(function initFolderPicker() {
  const modal    = $('#folder-picker-modal');
  const closeBtn = $('#fpm-close');
  const cancelBtn= $('#fpm-cancel');
  const list     = $('#fpm-list');
  const cwd      = $('#fpm-cwd');
  const upBtn    = $('#fpm-up');
  const selBtn   = $('#fpm-select');

  if (!modal) return;

  let _targetEl  = null;   // the <input> to fill on selection
  let _browseCwd = null;
  let _browseParent = null;
  let _onSelect  = null;   // optional callback(path)
  let _fileMode  = false;  // when true, files are pickable (not just folders)
  let _multiple  = false;  // when true, picking a file appends comma-separated

  // Public API — called by any Browse button.
  // opts: { files?: bool (allow picking a file), multiple?: bool (append files) }
  window.openFolderPicker = function(targetInputEl, initialPath, onSelectCb, opts) {
    _targetEl  = targetInputEl;
    _onSelect  = onSelectCb || null;
    _fileMode  = !!(opts && opts.files);
    _multiple  = !!(opts && opts.multiple);
    modal.hidden = false;
    const start = initialPath || (targetInputEl && targetInputEl.value.trim()) || '';
    // a comma-list starting point isn't a real dir — fall back to roots
    start && start.indexOf(',') === -1 ? fpmNavigateTo(start) : fpmNavigateRoots();
  };

  // Notify any input/change listeners bound to the field (e.g. the Variants
  // modal copies the value into its saved state on 'input'). Setting .value
  // programmatically does NOT fire these events, so dispatch them explicitly.
  function _notifyTarget() {
    if (!_targetEl) return;
    _targetEl.dispatchEvent(new Event('input',  { bubbles: true }));
    _targetEl.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function pickFile(path) {
    if (_targetEl) {
      if (_multiple) {
        const cur = _targetEl.value.split(',').map((s) => s.trim()).filter(Boolean);
        if (!cur.includes(path)) cur.push(path);
        _targetEl.value = cur.join(', ');
      } else {
        _targetEl.value = path;
      }
      _notifyTarget();
    }
    if (_onSelect) _onSelect(path);
    if (!_multiple) close();
  }

  function close() {
    modal.hidden = true;
    _targetEl = null; _onSelect = null; _fileMode = false; _multiple = false;
  }
  closeBtn  && closeBtn.addEventListener('click', close);
  cancelBtn && cancelBtn.addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });

  upBtn && upBtn.addEventListener('click', () => {
    _browseParent ? fpmNavigateTo(_browseParent) : fpmNavigateRoots();
  });

  selBtn && selBtn.addEventListener('click', () => {
    if (!_browseCwd) return;
    if (_targetEl) { _targetEl.value = _browseCwd; _notifyTarget(); }
    if (_onSelect) _onSelect(_browseCwd);
    close();
  });

  async function fpmNavigateRoots() {
    if (cwd) cwd.textContent = 'Drives & shortcuts';
    if (upBtn) upBtn.disabled = true;
    _browseCwd = null; _browseParent = null;
    if (!list) return;
    list.innerHTML = '<li style="color:var(--fg-mute);font-size:10px;pointer-events:none">loading…</li>';
    try {
      const data = await (await fetch('/api/fs/roots')).json();
      list.innerHTML = '';
      (data.pinned || []).forEach((p) => fpmItem(p.label + '  (' + p.path + ')', 'dfb-dir', () => fpmNavigateTo(p.path)));
      (data.drives || []).forEach((d) => fpmItem(d.label, 'dfb-drive', () => fpmNavigateTo(d.path)));
    } catch { list.innerHTML = '<li style="color:var(--err)">Failed to load drives</li>'; }
  }

  async function fpmNavigateTo(path) {
    if (!list) return;
    list.innerHTML = '<li style="color:var(--fg-mute);font-size:10px;pointer-events:none">loading…</li>';
    try {
      const res  = await fetch('/api/fs/list?path=' + encodeURIComponent(path));
      if (!res.ok) throw new Error();
      const data = await res.json();
      _browseCwd = data.path; _browseParent = data.parent;
      if (cwd)   cwd.textContent  = data.path;
      if (upBtn) upBtn.disabled   = !data.parent;
      list.innerHTML = '';
      (data.entries || []).filter((e) =>  e.is_dir).forEach((d) => fpmItem(d.name, 'dfb-dir',  () => fpmNavigateTo(d.path)));
      (data.entries || []).filter((e) => !e.is_dir).forEach((f) =>
        fpmItem(f.name, 'dfb-file', _fileMode ? () => pickFile(f.path) : null));
      if (!(data.entries || []).length) fpmItem('(empty)', '', null);
    } catch { list.innerHTML = '<li style="color:var(--err)">Cannot read: ' + path + '</li>'; }
  }

  function fpmItem(text, cls, onClick) {
    const li = document.createElement('li');
    if (cls) li.className = cls;
    li.textContent = text;
    if (!onClick) { li.style.color = 'var(--fg-mute)'; li.style.cursor = 'default'; }
    else li.addEventListener('click', onClick);
    list.appendChild(li);
  }

  // Wire any .fpick-btn that exists in the DOM now, and observe future ones
  function bindBrowseBtn(btn) {
    if (btn._fpickBound) return;
    btn._fpickBound = true;
    btn.addEventListener('click', () => {
      const targetId = btn.dataset.target;
      const inp = targetId ? document.getElementById(targetId) : null;
      const opts = { files: btn.dataset.fpickFiles === '1',
                     multiple: btn.dataset.fpickMultiple === '1' };
      window.openFolderPicker(inp, null, null, opts);
    });
  }
  document.querySelectorAll('.fpick-btn').forEach(bindBrowseBtn);
  // Observe future Browse buttons (tabs render lazily)
  new MutationObserver((muts) => {
    muts.forEach((m) => m.addedNodes.forEach((n) => {
      if (n.nodeType !== 1) return;
      if (n.classList && n.classList.contains('fpick-btn')) bindBrowseBtn(n);
      n.querySelectorAll && n.querySelectorAll('.fpick-btn').forEach(bindBrowseBtn);
    }));
  }).observe(document.body, { childList: true, subtree: true });
})();

// ── Output folder chooser (sidebar) ─────────────────────────────────
// Lets the user pick where finished deliverables are saved. Seeds OUTPUT_ROOT
// (used by the Variants prompt) and persists via /api/output-dir (read by the
// server-side Streamlined finalizer).
(function () {
  const input   = document.getElementById('output-dir-input');
  const saveBtn = document.getElementById('output-dir-save');
  const resetBtn = document.getElementById('output-dir-reset');
  const statusEl = document.getElementById('output-dir-status');
  if (!input && !saveBtn) return;               // control not present
  const setStatus = (m) => { if (statusEl) statusEl.textContent = m || ''; };

  async function load() {
    try {
      const r = await fetch('/api/output-dir');
      if (!r.ok) return;
      const d = await r.json();
      OUTPUT_ROOT = d.dir || 'Final Output';
      if (input) input.value = d.dir || '';
      setStatus(d.is_custom ? 'custom' : 'default');
    } catch (e) { /* keep default */ }
  }
  async function save(dir) {
    setStatus('saving…');
    try {
      const r = await fetch('/api/output-dir', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ dir: dir || '' }),
      });
      const d = await r.json();
      if (!r.ok) { setStatus(d.detail || 'error'); return; }
      OUTPUT_ROOT = d.dir || 'Final Output';
      if (input) input.value = d.dir || '';
      setStatus(d.is_custom ? 'saved ✓' : 'reset ✓');
    } catch (e) { setStatus('error'); }
  }
  if (saveBtn)  saveBtn.addEventListener('click', () => save(input ? input.value.trim() : ''));
  if (resetBtn) resetBtn.addEventListener('click', () => save(''));
  load();
})();

// ============================================================
// MUSIC / SFX TAB
// ============================================================
const MUSIC_CFG_KEY = 'veditor.musicSfx.v1';
const MUSIC_MODES = [
  { id: 'bed',   name: 'Full bed',    desc: 'loops under entire video' },
  { id: 'intro', name: 'Intro only',  desc: 'first ~10 s then fades' },
  { id: 'outro', name: 'Outro only',  desc: 'fades in last ~10 s' },
];
const MUSIC_LEVELS = [
  { id: '-20', name: '-20 dBFS', note: 'louder — prominent' },
  { id: '-22', name: '-22 dBFS', note: 'balanced (default)' },
  { id: '-24', name: '-24 dBFS', note: 'subtle — background' },
];
const SFX_PLACEMENTS = [
  { id: 'transitions', name: 'Transitions',    desc: 'cuts between sections' },
  { id: 'impact',      name: 'Impact moments', desc: 'stats, reveals, CTA' },
  { id: 'hook',        name: 'Hook cut',        desc: 'first frame of video' },
];

const musicSfxState = (() => {
  const defaults = {
    musicEnabled: true,
    musicFolder:  '',
    musicMode:    'bed',
    musicLevel:   '-22',
    sfxEnabled:   true,
    sfxFolder:    '',
    sfxPlacements: ['transitions', 'impact'],
    extra:        '',
  };
  try {
    const saved = JSON.parse(localStorage.getItem(MUSIC_CFG_KEY) || 'null');
    if (saved && typeof saved === 'object') return Object.assign({}, defaults, saved);
  } catch {}
  return { ...defaults };
})();

function saveMusicState() {
  try { localStorage.setItem(MUSIC_CFG_KEY, JSON.stringify(musicSfxState)); } catch {}
}

// Derive a folder path from the dice asset root for a given slot
// Team / brand definitions. Keys are stable IDs; aliases are case-insensitive
// folder-name matches the asset root may contain. Used both by the dice flow
// (to scope into the team subfolder) and by the Music/SFX tab "↺ root" sync.
const TEAMS = {
  endurance: {
    id: 'endurance',
    name: 'Endurance',
    sub: 'Auto Warranty',
    aliases: ['endurance', 'auto warranty', 'auto-warranty', 'auto_warranty', 'auto', 'endurance auto warranty'],
  },
  windows: {
    id: 'windows',
    name: 'Windows',
    sub: 'RbA Windows',
    aliases: ['windows', 'rba windows', 'rba-windows', 'rba_windows', 'rba'],
  },
  bath: {
    id: 'bath',
    name: 'Bath',
    sub: 'Walk-in Showers',
    aliases: ['bath', 'baths', 'walk-in showers', 'walk-in-showers', 'walk_in_showers',
              'walkin showers', 'walkin', 'showers', 'walk-in', 'walk_in'],
  },
};
const TEAM_KEY = 'veditor.team.v1';
// Where finished deliverables are saved. Default is the relative "Final Output"
// (resolved under the project root, unchanged behavior). The sidebar chooser
// (below) overrides it with a user-picked folder, seeded from /api/output-dir.
let OUTPUT_ROOT = 'Final Output';
function loadTeam() {
  try { return localStorage.getItem(TEAM_KEY) || ''; } catch { return ''; }
}
function saveTeam(id) {
  try { id ? localStorage.setItem(TEAM_KEY, id) : localStorage.removeItem(TEAM_KEY); } catch {}
}

// Given an asset root + active team id, return the team subfolder path inside
// the root. Falls back to the raw root if no team is selected or no matching
// subfolder exists. The match uses team.aliases (case-insensitive).
async function resolveTeamFolder(rootPath, teamId) {
  if (!rootPath || !teamId) return rootPath || '';
  const team = TEAMS[teamId];
  if (!team) return rootPath;
  try {
    const res = await fetch('/api/fs/list?path=' + encodeURIComponent(rootPath));
    if (!res.ok) return rootPath;
    const data = await res.json();
    const dirs = (data.entries || []).filter((e) => e.is_dir);
    const match = dirs.find((d) => team.aliases.includes(d.name.toLowerCase()));
    return match ? match.path : rootPath;
  } catch { return rootPath; }
}

async function diceRootSlot(slotName) {
  const DICE_SLOTS = {
    music: ['music', 'audio', 'soundtrack'],
    sfx:   ['sfx', 'sound effects', 'sounds'],
  };
  const root = (() => { try { return localStorage.getItem('veditor.diceRoot.v1') || ''; } catch { return ''; } })();
  if (!root) return '';
  // Scope into the selected team's subfolder if one is set; otherwise scan
  // the root directly (legacy behavior for setups without team folders).
  const scoped = await resolveTeamFolder(root, loadTeam());
  try {
    const res  = await fetch('/api/fs/list?path=' + encodeURIComponent(scoped));
    if (!res.ok) return '';
    const data = await res.json();
    const aliases = DICE_SLOTS[slotName] || [];
    const match = (data.entries || []).find((e) => e.is_dir && aliases.includes(e.name.toLowerCase()));
    return match ? match.path : '';
  } catch { return ''; }
}

function renderMusicTab() {
  // mode chips
  renderChipsGeneric('music-mode-row', MUSIC_MODES, musicSfxState.musicMode, (id) => {
    musicSfxState.musicMode = id; saveMusicState();
  });
  // level chips
  renderChipsGeneric('music-level-row', MUSIC_LEVELS, musicSfxState.musicLevel, (id) => {
    musicSfxState.musicLevel = id; saveMusicState();
  });
  // sfx placements (multi-select)
  renderMultiChips('sfx-placement-row', SFX_PLACEMENTS, musicSfxState.sfxPlacements, (id) => {
    const i = musicSfxState.sfxPlacements.indexOf(id);
    if (i >= 0) musicSfxState.sfxPlacements.splice(i, 1);
    else musicSfxState.sfxPlacements.push(id);
    saveMusicState(); renderMusicTab();
  });
}

function initMusicTab() {
  // Restore inputs
  const musicInp = $('#music-folder-input');
  const sfxInp   = $('#sfx-folder-input');
  const musicEn  = $('#music-enable');
  const sfxEn    = $('#sfx-enable');
  const extra    = $('#music-extra');

  if (musicInp) musicInp.value = musicSfxState.musicFolder;
  if (sfxInp)   sfxInp.value   = musicSfxState.sfxFolder;
  if (musicEn)  musicEn.checked = musicSfxState.musicEnabled;
  if (sfxEn)    sfxEn.checked   = musicSfxState.sfxEnabled;
  if (extra)    extra.value     = musicSfxState.extra;

  // Toggle sub-sections visibility
  function syncVis() {
    const ms = $('#music-sub');
    const ss = $('#sfx-sub');
    document.querySelectorAll('.music-sub').forEach((el) => el.style.opacity = musicSfxState.musicEnabled ? '1' : '0.4');
    document.querySelectorAll('.sfx-sub').forEach((el)   => el.style.opacity = musicSfxState.sfxEnabled   ? '1' : '0.4');
  }
  syncVis();

  musicEn && musicEn.addEventListener('change', () => {
    musicSfxState.musicEnabled = musicEn.checked; saveMusicState(); syncVis();
  });
  sfxEn && sfxEn.addEventListener('change', () => {
    musicSfxState.sfxEnabled = sfxEn.checked; saveMusicState(); syncVis();
  });

  // Save on input change
  musicInp && musicInp.addEventListener('input', () => { musicSfxState.musicFolder = musicInp.value.trim(); saveMusicState(); });
  sfxInp   && sfxInp.addEventListener('input',   () => { musicSfxState.sfxFolder   = sfxInp.value.trim();   saveMusicState(); });
  extra    && extra.addEventListener('input',     () => { musicSfxState.extra       = extra.value;           saveMusicState(); });

  // ↺ sync buttons — derive paths from dice asset root
  const musicSync = $('#music-folder-sync');
  const sfxSync   = $('#sfx-folder-sync');
  musicSync && musicSync.addEventListener('click', async () => {
    musicSync.textContent = '…';
    const p = await diceRootSlot('music');
    musicSync.textContent = '↺ root';
    if (p) { if (musicInp) musicInp.value = p; musicSfxState.musicFolder = p; saveMusicState(); }
    else alert('No Music subfolder found in asset root. Set the asset root via the ⚙ on the Roll the Dice button.');
  });
  sfxSync && sfxSync.addEventListener('click', async () => {
    sfxSync.textContent = '…';
    const p = await diceRootSlot('sfx');
    sfxSync.textContent = '↺ root';
    if (p) { if (sfxInp) sfxInp.value = p; musicSfxState.sfxFolder = p; saveMusicState(); }
    else alert('No SFX subfolder found in asset root. Set the asset root via the ⚙ on the Roll the Dice button.');
  });

  // Preview button
  const previewBtn = $('#music-preview-btn');
  const previewEl  = $('#music-preview');
  previewBtn && previewBtn.addEventListener('click', () => {
    previewEl.hidden = !previewEl.hidden;
    if (!previewEl.hidden) previewEl.textContent = buildMusicBlock() || '(music and SFX both disabled)';
  });

  renderMusicTab();
}

// Now that musicSfxState is declared and initMusicTab is defined, run it.
initMusicTab();

function buildMusicBlock() {
  // CRITICAL: emit when *enabled* regardless of whether the user remembered
  // to click the `↺ root` button to fill in a folder path. Previous behavior
  // (`enabled && folder`) silently dropped the entire Music & SFX block from
  // the wrapped prompt when the folder was empty — Claude never saw the
  // user's intent to add music, so music step was skipped and the output
  // had no music bed at all. This was the 2026-05-13 regression complaint.
  // When the folder is empty, we tell Claude how to derive it from the team
  // asset root, so the music step runs even without explicit configuration.
  const musicOn = !!musicSfxState.musicEnabled;
  const sfxOn   = !!musicSfxState.sfxEnabled;
  if (!musicOn && !sfxOn) return '';

  // Best-effort discovery hint: where to look when a folder wasn't set.
  let assetRoot = '';
  try { assetRoot = localStorage.getItem('veditor.diceRoot.v1') || ''; } catch (e) { /* */ }
  const teamName = (typeof loadTeam === 'function') ? (loadTeam() || '') : '';
  const discoveryHint = (assetRoot && teamName)
    ? `If the source folder is empty below, look under \`${assetRoot}/${teamName}/Music\` (or \`/SFX\`) and pick the most appropriate track.`
    : (assetRoot
        ? `If the source folder is empty below, look under \`${assetRoot}/<team>/Music\` (or \`/SFX\`) and pick the most appropriate track.`
        : `If the source folder is empty below, ask the user where to find music/SFX before skipping. Do NOT silently omit this step — it was explicitly enabled.`);

  const lines = [];
  lines.push('### Music & SFX');
  lines.push('');
  lines.push(discoveryHint);

  if (musicOn) {
    const mode  = MUSIC_MODES.find((m) => m.id === musicSfxState.musicMode)  || MUSIC_MODES[0];
    const level = MUSIC_LEVELS.find((l) => l.id === musicSfxState.musicLevel) || MUSIC_LEVELS[1];
    const folder = musicSfxState.musicFolder
      || (assetRoot && teamName ? `${assetRoot}/${teamName}/Music` : '(unset — derive from asset root)');
    lines.push('');
    lines.push('**Music bed (ENABLED — do not skip)**');
    lines.push(`- Source folder: \`${folder}\``);
    lines.push(`- Mode: ${mode.name} — ${mode.desc}.`);
    lines.push(`- Level: ${level.id} dBFS (${level.note}). Hard rule: music must NEVER exceed -20 dBFS under active VO. Dialogue must always be clearly audible.`);
    lines.push('- Pick the track that best fits the video tone. Mix it in using ffmpeg amix or apad+adelay; apply afade in/out at start/end.');
  }

  if (sfxOn) {
    const placements = SFX_PLACEMENTS
      .filter((p) => musicSfxState.sfxPlacements.includes(p.id))
      .map((p) => `${p.name} (${p.desc})`)
      .join(', ') || 'any appropriate moment';
    const folder = musicSfxState.sfxFolder
      || (assetRoot && teamName ? `${assetRoot}/${teamName}/SFX` : '(unset — derive from asset root)');
    lines.push('');
    lines.push('**Sound effects (ENABLED — do not skip)**');
    lines.push(`- Source folder: \`${folder}\``);
    lines.push(`- Place SFX at: ${placements}.`);
    lines.push('- Keep SFX brief and purposeful — 1–2 s max per hit. Don\'t stack SFX with music peaks.');
  }

  if (musicSfxState.extra && musicSfxState.extra.trim()) {
    lines.push('');
    lines.push('**Additional direction:**');
    lines.push(musicSfxState.extra.trim());
  }

  return lines.join('\n');
}

// Wire into pipeline builder — called from initMusicTab but also needs to happen at module level
// so buildPipelinePrompt can call buildMusicBlock() like it calls buildBrollBlock()

// ============================================================
// ROLL THE DICE — autonomous full-video composer
// ============================================================
(function initRollDice() {
  const btn        = $('#roll-dice-btn');
  const cfgBtn     = $('#dice-cfg-btn');
  const modal      = $('#dice-modal');
  const closeBtn   = $('#dice-modal-close');
  const cancelBtn  = $('#dice-modal-cancel');
  const saveBtn    = $('#dice-modal-save');
  const rootInput  = $('#dfr-root');
  const browseBtn  = $('#dfr-browse-root');
  const discovered = $('#dfr-discovered');
  const chips      = $('#dfrd-chips');
  const fsBrowser  = $('#dice-fsbrowser');
  const dfbList    = $('#dfb-list');
  const dfbCwd     = $('#dfb-cwd');
  const dfbUp      = $('#dfb-up');
  const dfbSelect  = $('#dfb-select');

  if (!btn) return;

  // ── Persisted config — just one path ────────────────────
  const DICE_CFG_KEY = 'veditor.diceRoot.v1';
  const loadRoot = () => { try { return localStorage.getItem(DICE_CFG_KEY) || ''; } catch { return ''; } };
  const saveRoot = (p) => { try { localStorage.setItem(DICE_CFG_KEY, p); } catch {} };

  // Known subfolder slot names and their aliases (all lowercased for matching).
  // Brand discovery removed per user request 2026-05-13 — the dice flow
  // no longer reads brand guidelines.
  const SLOTS = {
    hooks: ['hooks', 'hook'],
    broll: ['b-roll', 'broll', 'b_roll', 'b roll'],
    ctas:  ['ctas', 'cta'],
    music: ['music', 'audio', 'soundtrack'],
    sfx:   ['sfx', 'sound effects', 'sounds'],
  };

  // Scan the root and return { hooks, broll, ctas, music, sfx } → abs path or null
  async function discoverSlots(rootPath) {
    if (!rootPath) return {};
    try {
      const res = await fetch('/api/fs/list?path=' + encodeURIComponent(rootPath));
      if (!res.ok) return {};
      const data = await res.json();
      const dirs = (data.entries || []).filter((e) => e.is_dir);
      const result = {};
      for (const [slot, aliases] of Object.entries(SLOTS)) {
        const match = dirs.find((d) => aliases.includes(d.name.toLowerCase()));
        if (match) result[slot] = match.path;
      }
      return result;
    } catch { return {}; }
  }

  // Refresh modal status: active team, what team folders exist at the root,
  // and what slot subfolders exist inside the scoped team folder. This is
  // the operator's main feedback loop — they see at a glance what the
  // dice will actually use when they click Roll.
  async function refreshDiscovered() {
    const tsBox   = document.getElementById('dfr-team-status');
    const tsValue = document.getElementById('dts-value');
    const teamsBox = document.getElementById('dfr-teams-detected');
    const teamChips = document.getElementById('dfrd-team-chips');

    const teamId = (typeof loadTeam === 'function') ? loadTeam() : '';
    const team   = teamId && (typeof TEAMS !== 'undefined') ? TEAMS[teamId] : null;
    const root   = rootInput ? rootInput.value.trim() : '';

    // Team-status banner at top of modal
    if (tsBox && tsValue) {
      if (team) {
        tsBox.classList.remove('no-team');
        tsValue.textContent = team.name + ' · ' + team.sub;
      } else {
        tsBox.classList.add('no-team');
        tsValue.textContent = '— pick a team in the sidebar first —';
      }
    }

    // Empty root → hide both chip strips
    if (!root) {
      if (discovered) discovered.hidden = true;
      if (teamsBox)   teamsBox.hidden = true;
      return;
    }

    // 1. Scan the bare root for team folders (informational — confirms the
    //    new layout is set up correctly).
    let rootEntries = [];
    try {
      const r = await fetch('/api/fs/list?path=' + encodeURIComponent(root));
      if (r.ok) {
        const data = await r.json();
        rootEntries = (data.entries || []).filter((e) => e.is_dir);
      }
    } catch {}

    const teamsFound = [];
    for (const tId of Object.keys(TEAMS)) {
      const aliases = TEAMS[tId].aliases;
      const match = rootEntries.find((d) => aliases.includes(d.name.toLowerCase()));
      if (match) teamsFound.push({ id: tId, name: TEAMS[tId].name, dir: match.name });
    }

    if (teamsBox && teamChips) {
      teamChips.innerHTML = '';
      if (teamsFound.length) {
        for (const tf of teamsFound) {
          const ch = document.createElement('span');
          ch.className = 'dfrd-team-chip' + (tf.id === teamId ? ' active' : '');
          ch.textContent = tf.dir;
          teamChips.appendChild(ch);
        }
        teamsBox.hidden = false;
      } else {
        // No team folders detected — surface this as a warning chip
        const ch = document.createElement('span');
        ch.className = 'dfrd-chip';
        ch.style.borderColor = 'var(--warn)';
        ch.style.color = 'var(--warn)';
        ch.textContent = 'no team folders found — expected Endurance/, Windows/, or Bath/';
        teamChips.appendChild(ch);
        teamsBox.hidden = false;
      }
    }

    // 2. Scope into the active team's folder (or fall back to bare root)
    //    and show what slot subfolders exist inside.
    const scoped = (typeof resolveTeamFolder === 'function')
      ? await resolveTeamFolder(root, teamId)
      : root;
    const slots = await discoverSlots(scoped);
    const found = Object.keys(slots);

    if (!discovered || !chips) return;
    chips.innerHTML = '';
    if (!found.length) {
      if (team) {
        // Team selected but no slot folders inside → setup issue
        const ch = document.createElement('span');
        ch.className = 'dfrd-chip';
        ch.style.borderColor = 'var(--err)';
        ch.style.color = 'var(--err)';
        ch.textContent = `no slot folders inside ${team.name}/ — expected HOOKS, B-Roll, etc.`;
        chips.appendChild(ch);
        discovered.hidden = false;
      } else {
        discovered.hidden = true;
      }
      return;
    }
    for (const k of found) {
      const ch = document.createElement('span');
      ch.className = 'dfrd-chip';
      ch.textContent = k.toUpperCase();
      chips.appendChild(ch);
    }
    discovered.hidden = false;
  }

  // Re-render the modal whenever the team picker changes — even if the modal
  // is already open. The team pill click handler fires this custom event;
  // we also listen for cross-tab 'storage' changes as a backup.
  window.addEventListener('kk:team-changed', () => {
    if (modal && !modal.hidden) refreshDiscovered();
  });
  window.addEventListener('storage', (e) => {
    if (e.key === 'veditor.team.v1' && modal && !modal.hidden) refreshDiscovered();
  });

  // ── Modal open / close ───────────────────────────────────
  function openModal() {
    if (rootInput) rootInput.value = loadRoot();
    closeFsBrowser();
    refreshDiscovered();
    modal.hidden = false;
  }
  function closeModal() {
    modal.hidden = true;
    closeFsBrowser();
  }

  if (cfgBtn)    cfgBtn.addEventListener('click', openModal);
  if (closeBtn)  closeBtn.addEventListener('click', closeModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal && modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

  if (rootInput) rootInput.addEventListener('input', () => {
    clearTimeout(rootInput._dt);
    rootInput._dt = setTimeout(refreshDiscovered, 600);
  });

  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      const v = rootInput ? rootInput.value.trim() : '';
      saveRoot(v);
      closeModal();
    });
  }

  // ── Mini filesystem browser ──────────────────────────────
  let _browseCwd    = null;
  let _browseParent = null;

  function closeFsBrowser() {
    if (fsBrowser) fsBrowser.hidden = true;
    if (browseBtn) browseBtn.classList.remove('active');
  }

  async function navigateRoots() {
    if (dfbCwd) dfbCwd.textContent = 'Drives & shortcuts';
    if (dfbUp)  dfbUp.disabled = true;
    _browseCwd = null; _browseParent = null;
    if (!dfbList) return;
    dfbList.innerHTML = '<li style="color:var(--fg-mute);font-size:10px;pointer-events:none">loading…</li>';
    try {
      const res  = await fetch('/api/fs/roots');
      const data = await res.json();
      dfbList.innerHTML = '';
      for (const p of (data.pinned || [])) {
        addFsItem(p.label + '  (' + p.path + ')', 'dfb-dir', () => navigateTo(p.path));
      }
      for (const d of (data.drives || [])) {
        addFsItem(d.label, 'dfb-drive', () => navigateTo(d.path));
      }
    } catch {
      dfbList.innerHTML = '<li style="color:var(--err)">Failed to load drives</li>';
    }
  }

  async function navigateTo(path) {
    if (!path) { await navigateRoots(); return; }
    if (!dfbList) return;
    dfbList.innerHTML = '<li style="color:var(--fg-mute);font-size:10px;pointer-events:none">loading…</li>';
    try {
      const res  = await fetch('/api/fs/list?path=' + encodeURIComponent(path));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      _browseCwd = data.path; _browseParent = data.parent;
      if (dfbCwd) dfbCwd.textContent = data.path;
      if (dfbUp)  dfbUp.disabled = !data.parent;
      dfbList.innerHTML = '';
      const entries = data.entries || [];
      entries.filter((e) =>  e.is_dir).forEach((d) => addFsItem(d.name, 'dfb-dir',  () => navigateTo(d.path)));
      entries.filter((e) => !e.is_dir).forEach((f) => addFsItem(f.name, 'dfb-file', null));
      if (!entries.length) addFsItem('(empty)', '', null);
    } catch {
      dfbList.innerHTML = '<li style="color:var(--err)">Cannot read: ' + path + '</li>';
    }
  }

  function addFsItem(text, cls, onClick) {
    const li = document.createElement('li');
    if (cls) li.className = cls;
    li.textContent = text;
    if (!onClick) { li.style.color = 'var(--fg-mute)'; li.style.cursor = 'default'; }
    else li.addEventListener('click', onClick);
    dfbList.appendChild(li);
  }

  if (dfbUp)     dfbUp.addEventListener('click', () => _browseParent ? navigateTo(_browseParent) : navigateRoots());
  if (dfbSelect) dfbSelect.addEventListener('click', () => {
    if (!_browseCwd) return;
    if (rootInput) rootInput.value = _browseCwd;
    closeFsBrowser();
    refreshDiscovered();
  });

  if (browseBtn) {
    browseBtn.addEventListener('click', () => {
      if (!fsBrowser.hidden) { closeFsBrowser(); return; }
      browseBtn.classList.add('active');
      fsBrowser.hidden = false;
      const current = rootInput ? rootInput.value.trim() : '';
      current ? navigateTo(current) : navigateRoots();
    });
  }

  // Show a transient error toast at the top of the dice modal. Falls back
  // to alert() if the modal DOM is missing.
  function flashDiceError(msg) {
    if (!modal) { alert(msg); return; }
    let toast = document.getElementById('dice-error-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'dice-error-toast';
      toast.style.cssText = [
        'margin:8px 0', 'padding:8px 12px',
        'background:rgba(255,48,96,0.15)', 'border:1px solid var(--err)',
        'border-radius:6px', 'color:#ffd0d6',
        'font-family:var(--mono)', 'font-size:11px', 'line-height:1.4',
      ].join(';');
      const body = modal.querySelector('.dice-modal-body');
      if (body) body.insertBefore(toast, body.firstChild);
    }
    toast.textContent = msg;
    toast.hidden = false;
    setTimeout(() => { if (toast) toast.hidden = true; }, 8000);
  }

  // ── Roll! ────────────────────────────────────────────────
  btn.addEventListener('click', async () => {
    if (btn.classList.contains('rolling')) return;

    const root = loadRoot();
    if (!root) {
      openModal();
      flashDiceError('Set the asset root folder first. It should contain Endurance/, Windows/, and Bath/ subfolders.');
      if (rootInput) rootInput.focus();
      return;
    }

    const teamId = loadTeam();
    const team   = teamId ? TEAMS[teamId] : null;

    // Hard gate: require a team selection. Without one we'd pick assets
    // from the bare root (which under the new layout has nothing useful).
    if (!team) {
      openModal();
      flashDiceError('Pick a team in the sidebar first (Endurance / Windows / Bath). Roll the Dice needs to know which product line to scope assets to.');
      return;
    }

    btn.classList.add('rolling');
    btn.disabled = true;

    const scopedRoot = await resolveTeamFolder(root, teamId);

    // resolveTeamFolder returns the bare root if no matching team folder
    // exists under it. Detect that and surface a clear setup error.
    if (scopedRoot === root) {
      btn.classList.remove('rolling');
      btn.disabled = false;
      openModal();
      const aliases = team.aliases.slice(0, 3).join(', ');
      flashDiceError(`No "${team.name}" folder found under the asset root. Create a subfolder named one of: ${aliases} (matching is case-insensitive).`);
      return;
    }

    const slots = await discoverSlots(scopedRoot);

    // Scan hooks for file listing
    let hookFiles = [];
    if (slots.hooks) {
      try {
        const res  = await fetch('/api/fs/list?path=' + encodeURIComponent(slots.hooks));
        const data = await res.json();
        hookFiles  = (data.entries || []).filter((e) => !e.is_dir);
      } catch {}
    }

    btn.classList.remove('rolling');
    btn.disabled = false;

    if (!slots.hooks) {
      openModal();
      flashDiceError(`No HOOKS folder found inside ${team.name}/. Create ${scopedRoot.split(/[/\\]/).pop()}/HOOKS/ and drop hook videos in it.`);
      return;
    }

    const ts       = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const runDir   = `videos/edit/dice_${ts}`;                 // intermediates
    const outFile  = `Final Output/dice_${ts}/final.mp4`;      // final deliverable

    const hookList = hookFiles.length
      ? hookFiles.map((f) => '  - ' + f.path).join('\n')
      : '  (scan the HOOKS folder directly — it may not be empty)';

    // Resolve caption preset for the prompt
    const dicePreset = (typeof CAPTION_PRESETS !== 'undefined' && typeof presetState !== 'undefined')
      ? (CAPTION_PRESETS.find((p) => p.id === presetState.captionId) || CAPTION_PRESETS[0])
      : { id: 'voltchu', name: 'Voltchu', bg: '#FFE600', fg: '#000000' };
    const diceFontDef = (typeof FONT_FAMILIES !== 'undefined' && typeof presetState !== 'undefined')
      ? (FONT_FAMILIES.find((f) => f.id === presetState.fontFamily) || FONT_FAMILIES[0])
      : { id: 'Arial', name: 'Arial' };
    const dicePs = (typeof presetState !== 'undefined') ? presetState : {
      maxChars: 22, minDuration: 1.5, gapFrames: 2, caseStyle: 'upper',
      fontFamily: 'Arial', fontSize: 80,
    };

    // HeyGen avatar toggle — read saved state from the Avatar tab
    const useHeygenAvatar = !!(document.getElementById('dice-heygen-toggle')?.checked);
    let hgDiceState = null;
    let hgDiceDim = { w: 1080, h: 1920 };  // dice delivers vertical Meta reels
    if (useHeygenAvatar) {
      try {
        hgDiceState = JSON.parse(localStorage.getItem('veditor.heygen.v1') || 'null') || {};
      } catch {}
      // heygen_video.py takes --width/--height, not quality/aspect. Resolve the
      // picker's quality (720|1080) + aspect (vertical|square|landscape) to px.
      const base = (hgDiceState && hgDiceState.quality === '720') ? 720 : 1080;
      const asp = (hgDiceState && hgDiceState.aspect) || 'vertical';
      hgDiceDim = asp === 'square'    ? { w: base, h: base }
                : asp === 'landscape' ? { w: Math.round(base * 16 / 9), h: base }
                : /* vertical */        { w: base, h: Math.round(base * 16 / 9) };
    }

    // Build the found-slots block for the prompt
    const slotLines = Object.entries({
      'HOOKS':  slots.hooks,
      'B-ROLL': slots.broll,
      'CTAs':   slots.ctas,
      'MUSIC':  slots.music,
      'SFX':    slots.sfx,
    })
      .filter(([, v]) => v)
      .map(([k, v]) => `  ${k.padEnd(7)} ${v}`)
      .join('\n');

    const prompt =
`ROLL THE DICE — AUTONOMOUS VIDEO COMPOSITION
${team ? `
━━━ TEAM / BRAND ━━━
  ${team.name} · ${team.sub}
  This run is scoped to the ${team.name} product line. ALL assets, script \
copy, brand voice, terminology, and creative choices must be appropriate \
for ${team.sub}. Do NOT mix in references, examples, or visuals from other \
product lines (e.g. if team is Bath, do not mention windows or auto warranty).
` : ''}
━━━ ASSET ROOT ━━━
  ${root}${team ? `
  ↳ scoped to: ${scopedRoot}` : ''}

━━━ DISCOVERED SUBFOLDERS ━━━
${slotLines || '  (none found — check asset root)'}

━━━ HOOK FILES ━━━
${hookList}

━━━ HARD RULES (non-negotiable) ━━━
• Music levels: music bed must be mixed at -20 to -24 dBFS so dialogue is \
always intelligible. Never exceed -20 dBFS for music under active VO.
• Hook cutting: the chosen hook video is NOT used as-is. It must go through \
the full trimming pipeline — silence removal (30 ms crossfades, \
NORMAL_MAX=0.25 s tail cap, LAST_CHAR_CAP=0.28 s, TAIL_PAD=0.08 s, \
HEAD_PAD=0.05 s) and bad-take detection. A bad take is any pattern where the \
speaker starts a phrase, stops mid-sentence, and then restarts — that entire \
false-start segment must be cut out. Use word-level transcript data to locate \
these precisely; never use padding heuristics to fix what should be a cut.
• Caption style: use the preset currently selected in the studio UI — \
id="${dicePreset.id}" (${dicePreset.name}), bg=${dicePreset.bg}, \
fg=${dicePreset.fg}, max ${dicePs.maxChars} chars/line, \
min duration ${dicePs.minDuration}s, gap ${dicePs.gapFrames} frames, \
case: ${dicePs.caseStyle}. Do not deviate from these settings.

━━━ AUTONOMY (HARD RULE — RUN STRAIGHT THROUGH) ━━━
Once every input listed under Assets is verified to exist, execute every step \
below back-to-back without pausing. Do NOT ask "should I proceed?", do NOT \
render a 10-second preview and wait for go-ahead, do NOT confirm encoder \
choices, do NOT ask which mode to use. The user has pre-authorized the \
entire pipeline by clicking Roll the Dice. The only legitimate stop \
condition is a hard failure (file missing, ffmpeg exits non-zero) — and \
even then, report and stop, do not ask for permission to retry.

━━━ PERFORMANCE — proxy-first downscale (HARD RULE) ━━━
Source footage is likely 4K. Final delivery is 1080p. **Downsize EXACTLY \
ONCE — at the top of each clip's working chain — and treat the 1080p result \
as the working source for every downstream step on that clip.** Do NOT \
re-apply \`scale=1920:1080\` in any later encode. Captions, graphics \
overlays, EDL cuts, and the final composite all run on 1080p data, so they \
need no scale filter.

Probe each clip first (ffprobe). If it's already ≤1920×1080, skip the \
proxy step for that clip and use it as-is. Otherwise:
\`\`\`
PATH="$FFMPEG_DIR:$PATH" ffmpeg -y -hwaccel cuda -i "<SOURCE>" \\
  -vf "scale=1920:1080:flags=lanczos" \\
  -c:v h264_nvenc -preset p4 -rc vbr -cq 19 -b:v 12M -maxrate 18M -bufsize 24M \\
  -c:a copy -movflags +faststart \\
  "videos/edit/<stem>_1080p.mp4"
\`\`\`
Wall time on a 6-min clip: ~2-3 min on RTX-class NVENC.

• **Stream-copy when possible.** If a downstream operation only changes \
container or trims at keyframes, use \`-c copy\` (no re-encode). Re-encode \
only when filters require it (caption burn, overlay composite).
• **One ffmpeg invocation per logical step.** Don't chain three ffmpegs \
where one with \`-filter_complex\` would do.
• **Background long jobs.** For any ffmpeg step you estimate >2 min, run it \
with \`&\` in bash, then \`wait\` on the PID. Don't poll status, don't stream \
\`-progress\`, don't narrate intermediate output. Fire-and-wait, parse \
result code.

━━━ PIPELINE ━━━

STEP 1 — HOOK SELECTION & TRIMMING
List all video files in the HOOKS folder. Pick one at random (Python \`random.choice\`). Then:
a) **Proxy first.** Probe the chosen hook with ffprobe. If width > 1920 or height > 1080, downsize once to a 1080p proxy at \`${runDir}/hook_proxy.mp4\` using the NVENC command from the PERFORMANCE block. If it's already ≤1080p, skip and use the source directly. Every step below operates on the proxy (or original-if-already-1080p), never on the raw 4K source.
b) Transcribe the proxy with video-use/helpers/transcribe.py to get word-level data.
c) Build a silence-removal EDL following the TRIMMING_PHILOSOPHY.md rules (docs/TRIMMING_PHILOSOPHY.md).
d) Scan the transcript for false starts: any run of words that breaks off mid-thought and restarts within ~3 s. Cut each false-start segment word-boundary to word-boundary with 30 ms fades.
e) Render the trimmed hook to \`${runDir}/hook_trimmed.mp4\` — input is the 1080p proxy, so NO scale filter on this encode.

STEP 2 — SCRIPT
Write a 45–90 second narration script continuing from where the trimmed hook \
ends. Structure: hook handoff (5–8 s) → core insight (20–30 s) → solution/payoff \
(15–25 s) → CTA (5–8 s). Short punchy VO sentences — not prose.
CTA RULE (HARD): the CTA must drive the viewer to CLICK/TAP THE AD ITSELF — \
"Tap the link below", "Click below", "I've left the link right below". NEVER \
say a website URL, "go to our website", or a phone number; the Meta ad carries \
its own click-through link.
Also write a VISUAL HOOK LINE: a single punchy phrase of 5–8 words that captures \
the video's core hook. Save it to \`${runDir}/hook_text.txt\` (one line, plain text, \
no quotes). This will be burned as a text overlay at the top of the final video.

${useHeygenAvatar && hgDiceState && hgDiceState.avatarId ? `STEP 3 — AVATAR VIDEO (HeyGen — replaces ElevenLabs VO)
Generate a HeyGen talking-head avatar speaking the script. Use the exact \
credentials saved in the Avatar tab. No approval gate, no preview — submit, poll, download.

\`\`\`bash
PYTHONUTF8=1 $VU_PY \\
  video-use/helpers/heygen_video.py \\
  --avatar-id "${hgDiceState.avatarId || ''}" \\
  --voice-id "${hgDiceState.voiceId || ''}" \\
  --width ${hgDiceDim.w} --height ${hgDiceDim.h} \\
  --avatar-style ${hgDiceState.avatarStyle || 'normal'} \\
  --voice-speed ${hgDiceState.voiceSpeed || 1.0} \\
  --output "${runDir}/avatar.mp4" \\
  --json \\
  --text "$(cat <<'__HG_SCRIPT__'
$(SCRIPT_TEXT_PLACEHOLDER)
__HG_SCRIPT__
  )"
\`\`\`
**Substitute the script from STEP 2 for the placeholder above.** \
After the avatar render completes, treat \`${runDir}/avatar.mp4\` as the VO \
source for all downstream steps (levelling, b-roll sync, captions). \
Level the avatar audio track to peak [-6, -3] dBFS via \`level_audio.py --dialogue\`.` : `STEP 3 — VOICE-OVER
Generate the script via video-use/helpers/tts_voice.py:
  --output "${runDir}/vo.wav"
Pick the best narrator-fit voice from studio/static/voices.json if it exists; \
otherwise use the ElevenLabs default. Level the VO to peak [-6, -3] dBFS.`}

STEP 4 — B-ROLL${slots.broll ? `
Scan: ${slots.broll}
Map each script section to 1–3 clips that visually match the topic. **Proxy each chosen b-roll clip to 1080p before adding it to the EDL** — ffprobe each clip; if >1080p, downsize once via the NVENC command from the PERFORMANCE block to \`${runDir}/broll_<stem>_1080p.mp4\` and reference the proxy path in the EDL. Build b-roll EDL → \`${runDir}/broll_edl.json\`. Every clip in the EDL must be ≤1080p so the final composite needs no scale filter.` : `
(No B-Roll folder — skip.)`}

STEP 5 — ENHANCEMENTS${slots.music || slots.sfx || slots.ctas ? '' : `
(No enhancement folders found — skip.)`}${slots.music ? `
• Music bed from ${slots.music}. Mix at -20 to -24 dBFS under dialogue — listener must clearly hear VO. Duck music further under any high-energy speech. Do NOT go above -20 dBFS.` : ''}${slots.sfx ? `
• SFX from ${slots.sfx} → place at hook cut point and major section transitions only. Keep SFX brief and purposeful.` : ''}${slots.ctas ? `
• CTA end-card from ${slots.ctas} → append as final 3–5 s.` : ''}

STEP 6 — CAPTIONS
Build SRT from the VO/avatar transcript using these exact settings:
  Preset      : ${dicePreset.name} (bg ${dicePreset.bg}, text ${dicePreset.fg})
  Font family : ${diceFontDef.id} — set FontName="${diceFontDef.id}" in the ASS style; fall back to Arial Bold if not installed
  Font size   : ${dicePs.fontSize}pt (Fontsize=${dicePs.fontSize} in ASS style block)
  Max chars/line : ${dicePs.maxChars}
  Min duration   : ${dicePs.minDuration}s
  Gap            : ${dicePs.gapFrames} frames
  Case           : ${dicePs.caseStyle}
  Placement (HARD RULE — do NOT deviate): CENTER FRAME
    ASS style MUST use Alignment=5, MarginV=0, PlayResY=1920.
    This is non-negotiable for Meta ads — captions live dead-center.
Save to \`${runDir}/captions.srt\`. Captions burn LAST.

STEP 7 — COMPOSITE & FINAL ENCODE
1. Composite: trimmed hook + b-roll + VO/avatar + music/SFX (if present).
2. Burn captions (center-frame, from STEP 6) onto the composite.
3. Burn the visual hook text overlay from \`${runDir}/hook_text.txt\`:
   META SAFE ZONE RULE (9:16, 1080×1920): Instagram/Facebook UI covers the top
   270px (14%) — profile bar, timestamp, icons. The bottom 670px (35%) is
   covered by captions, handle, and CTAs. Keep ALL overlays out of these zones.
   The visual hook text lives in the UPPER safe zone: y=300px from the top.
   Use ffmpeg drawtext:
   \`\`\`
   drawtext=fontfile=/Windows/Fonts/arialbd.ttf:
     textfile='${runDir}/hook_text.txt':
     fontsize=72:fontcolor=white:
     box=1:boxcolor=black@0.65:boxborderw=20:
     x=(w-text_w)/2:y=300:
     enable='between(t\\,0\\,3)'
   \`\`\`
   This can be chained into the same ffmpeg invocation as the caption burn via
   \`-vf "subtitles=...,drawtext=..."\` — one encode, no extra pass.
4. **Final deliverable path (HARD RULE):** \`${outFile}\` — \`--encoder auto\`, no preview gate. This folder name contains a space, so ALWAYS quote it in shell commands: \`"${outFile}"\`. All working files (proxy, EDLs, trimmed hook, captions) stay under \`${runDir}/\`; ONLY the finished MP4 goes to the \`Final Output/\` folder. Create the parent dir first: \`mkdir -p "Final Output/dice_${ts}"\`.

━━━ REPORT ━━━
Markdown table: | Asset | Path | Why chosen |
Confirm the final output path (\`${outFile}\`), duration, and file size.`;

    activateTab('chat');
    promptInput.value = prompt;
    skipPipelineWrap  = true;
    nextSubmitRouted = true;   // autonomous batch → server routes the model (Sonnet)
    // Force a FRESH session: the prompt is fully self-contained, and a
    // continued session that already finished a prior run can convince Kino
    // the new request is "already complete" → it reports success with zero
    // tool calls and no output (bit us 2026-07-07). The submit handler
    // re-checks the toggle once the new session starts.
    continueToggle.checked = false;
    promptForm.requestSubmit();
  });
})();

// ============================================================
// VO + B-ROLL VARIANTS — batch-N script-rewrite + composite
// ============================================================
//
// Click flow:
//   1. User clicks "🎬 Variants" → modal opens, b-roll folder auto-fills
//      from <asset_root>/<team>/B-Roll, voice list populates from /11l.
//   2. User types/pastes the seed script, sets count + duration + aspect.
//   3. User clicks "▶ Generate Variants" → JS wraps a prompt that:
//        a) tells Kino to read brand guidelines (if present) and rewrite
//           the seed script into N variants with different angles,
//        b) for each variant, POST to /api/variant_factory with the
//           finalized script + style settings,
//        c) collect the N output paths and surface them.
//   4. Prompt is submitted via promptForm.requestSubmit(); the running
//      Claude orchestrator does the rewriting and dispatches the helper.
(function initVariantsModal() {
  const VAR_STATE_KEY = 'veditor.variants.v1';
  const btn       = document.getElementById('variants-btn');
  const modal     = document.getElementById('variants-modal');
  const closeBtn  = document.getElementById('variants-modal-close');
  const cancelBtn = document.getElementById('variants-modal-cancel');
  const genBtn    = document.getElementById('variants-modal-generate');
  const scriptEl  = document.getElementById('variants-script');
  const brollEl   = document.getElementById('variants-broll');
  const voiceEl   = document.getElementById('variants-voice');
  const voiceRef  = document.getElementById('variants-voice-refresh');
  const countEl   = document.getElementById('variants-count');
  const durEl     = document.getElementById('variants-duration');
  const aspectEl  = document.getElementById('variants-aspect');
  const subEl     = document.getElementById('variants-sub');
  const heygenEl  = document.getElementById('variants-heygen-toggle');
  const matchEl   = document.getElementById('variants-match');
  const fallbackEl = document.getElementById('variants-fallback');

  if (!btn || !modal) return;  // defensive — guards against partial DOM updates

  // ── Persistence ────────────────────────────────────────────
  const state = (() => {
    const def = { script: '', brollFolder: '', voiceId: '', count: 5, duration: 35, aspect: '9x16', heygenHook: false, match: 'random', fallback: 'none' };
    try {
      const saved = JSON.parse(localStorage.getItem(VAR_STATE_KEY) || 'null');
      return Object.assign({}, def, saved || {});
    } catch (e) { return def; }
  })();
  function save() {
    try { localStorage.setItem(VAR_STATE_KEY, JSON.stringify(state)); } catch (e) { /* */ }
  }
  function applyUI() {
    scriptEl.value = state.script || '';
    brollEl.value  = state.brollFolder || '';
    countEl.value  = state.count;
    durEl.value    = state.duration;
    aspectEl.value = state.aspect;
    if (heygenEl) heygenEl.checked = !!state.heygenHook;
    if (matchEl) matchEl.value = state.match || 'random';
    if (fallbackEl) fallbackEl.value = state.fallback || 'none';
    if (subEl) subEl.textContent = `VO + B-roll · ${state.count}×`;
  }

  // ── Voice list (same static file the VO modal uses) ────────
  async function loadVoices() {
    voiceEl.innerHTML = '<option value="">loading…</option>';
    try {
      const r = await fetch('/voices.json?_=' + Date.now());
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const voices = await r.json();
      voiceEl.innerHTML = '<option value="">— pick a voice —</option>';
      for (const v of (Array.isArray(voices) ? voices : [])) {
        const o = document.createElement('option');
        o.value = v.voice_id || v.id || '';
        o.textContent = v.name || v.voice_id || '?';
        voiceEl.appendChild(o);
      }
      if (state.voiceId) voiceEl.value = state.voiceId;
    } catch (e) {
      voiceEl.innerHTML = `<option value="">error: ${(e && e.message) || e}</option>`;
    }
  }

  // ── B-roll folder auto-fill from asset root + team ─────────
  async function autoFillBroll() {
    if (state.brollFolder) return;  // user already set one
    if (typeof diceRootSlot !== 'function') return;
    try {
      const p = await diceRootSlot('broll');
      if (p) { state.brollFolder = p; brollEl.value = p; save(); }
    } catch (e) { /* */ }
  }
  // Reuse `broll` alias chain; if diceRootSlot doesn't know it, fall back
  // to deriving from the dice root + team manually.
  // The existing DICE_SLOTS lookup only knows music/sfx — extend it inline
  // via a direct fs/list scan if needed.
  async function deriveBrollFromRoot() {
    let root = '';
    try { root = localStorage.getItem('veditor.diceRoot.v1') || ''; } catch (e) { /* */ }
    if (!root) return '';
    const teamName = (typeof loadTeam === 'function') ? (loadTeam() || '') : '';
    if (!teamName) return '';
    const teamFolder = (typeof resolveTeamFolder === 'function')
      ? await resolveTeamFolder(root, teamName) : `${root}/${teamName}`;
    try {
      const res = await fetch('/api/fs/list?path=' + encodeURIComponent(teamFolder));
      if (!res.ok) return '';
      const data = await res.json();
      const match = (data.entries || []).find((e) =>
        e.is_dir && /^(b[\s_-]?roll|broll)$/i.test(e.name)
      );
      return match ? match.path : '';
    } catch (e) { return ''; }
  }

  // Compute the current team's absolute folder path (with drive-letter healing
  // via the server's fs/list) so we can tell whether the saved b-roll belongs
  // to the CURRENT team or a stale prior one.
  async function currentTeamFolder() {
    let root = '';
    try { root = localStorage.getItem('veditor.diceRoot.v1') || ''; } catch {}
    const tid = (typeof loadTeam === 'function') ? (loadTeam() || '') : '';
    if (!root || !tid) return '';
    if (typeof resolveTeamFolder === 'function') {
      return (await resolveTeamFolder(root, tid)) || '';
    }
    return `${root}/${tid}`;
  }

  // Re-derive the b-roll folder from the currently selected team when the
  // saved value is empty OR belongs to a different team (or a stale drive
  // letter — the path prefix will differ). Fixes: switching teams while a
  // b-roll from a previous team was still saved in localStorage.
  async function syncBrollToTeam() {
    const teamFolder = await currentTeamFolder();
    if (!teamFolder) return;
    const saved = (state.brollFolder || '').replace(/\\/g, '/').toLowerCase();
    const teamPrefix = teamFolder.replace(/\\/g, '/').toLowerCase();
    if (saved && saved.startsWith(teamPrefix)) return;   // b-roll already in this team's tree
    const p = await deriveBrollFromRoot();
    if (p) { state.brollFolder = p; brollEl.value = p; save(); }
  }

  // ── Modal show/hide ────────────────────────────────────────
  async function openModal() {
    modal.hidden = false;
    applyUI();
    await syncBrollToTeam();
    if (!voiceEl.options.length || voiceEl.options.length === 1) await loadVoices();
    scriptEl.focus();
  }
  function closeModal() { modal.hidden = true; }

  btn.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

  // If the user switches teams while the modal is open, keep the b-roll in
  // sync with the new selection (same event Roll the Dice listens for).
  window.addEventListener('kk:team-changed', () => {
    if (!modal.hidden) syncBrollToTeam();
  });

  // ── Field inputs ───────────────────────────────────────────
  scriptEl.addEventListener('input', () => { state.script = scriptEl.value; save(); });
  brollEl.addEventListener('input',  () => { state.brollFolder = brollEl.value.trim(); save(); });
  voiceEl.addEventListener('change', () => { state.voiceId = voiceEl.value; save(); });
  countEl.addEventListener('input',  () => {
    state.count = Math.max(1, Math.min(10, parseInt(countEl.value, 10) || 5));
    if (subEl) subEl.textContent = `VO + B-roll · ${state.count}×`;
    save();
  });
  durEl.addEventListener('input',    () => {
    state.duration = Math.max(10, Math.min(90, parseInt(durEl.value, 10) || 35)); save();
  });
  aspectEl.addEventListener('change',() => { state.aspect = aspectEl.value; save(); });
  heygenEl && heygenEl.addEventListener('change', () => { state.heygenHook = heygenEl.checked; save(); });
  matchEl && matchEl.addEventListener('change', () => { state.match = matchEl.value; save(); });
  fallbackEl && fallbackEl.addEventListener('change', () => { state.fallback = fallbackEl.value; save(); });
  voiceRef && voiceRef.addEventListener('click', loadVoices);

  // ── Generate ───────────────────────────────────────────────
  genBtn.addEventListener('click', async () => {
    const script = (scriptEl.value || '').trim();
    // Empty script → AUTO mode: Kino writes the script itself from the brand
    // guidelines. This is only allowed when a Brand Guidelines folder with at
    // least one readable doc exists (verified below, after we resolve its path).
    const autoScript = !script;
    if (!state.brollFolder) {
      brollEl.focus();
      return;
    }
    if (!state.voiceId) {
      voiceEl.focus();
      return;
    }
    // HeyGen-hook guard: toggle is meaningless without an avatar + voice
    // picked in the Avatar tab. Block early with a clear pointer instead
    // of letting Kino hit a 400 on the first variant.
    if (state.heygenHook) {
      let hgPick = null;
      try { hgPick = JSON.parse(localStorage.getItem('veditor.heygen.v1') || 'null'); } catch {}
      const missing = [];
      if (!hgPick || !hgPick.avatarId) missing.push('avatar');
      if (!hgPick || !hgPick.voiceId)  missing.push('voice');
      if (missing.length) {
        alert(`HeyGen avatar hook is ON, but no ${missing.join(' + ')} is selected in the Avatar tab. Open the Avatar tab, pick an ${missing.join(' and a ')}, then come back and click Generate Variants again. (Or untoggle the HeyGen hook to run VO+b-roll only.)`);
        return;
      }
    }

    // Pull caption settings from the Captions tab so all N variants match
    // the studio's currently-configured style.
    const ps = (typeof presetState !== 'undefined') ? presetState : {};
    const cap = (typeof CAPTION_PRESETS !== 'undefined' && ps.captionId)
      ? (CAPTION_PRESETS.find((p) => p.id === ps.captionId) || CAPTION_PRESETS[0])
      : { id: 'psyglow', name: 'Psyglow', bg: '#FFDE59', fg: '#1A0F40' };
    const font = (typeof FONT_FAMILIES !== 'undefined')
      ? ((FONT_FAMILIES.find((f) => f.id === ps.fontFamily) || FONT_FAMILIES[0]).id) : 'Arial';
    // Captions are FORCED to center frame for Meta ads (HARD RULE) — the
    // Captions-tab placement is intentionally ignored here, matching Roll
    // the Dice. ASS Alignment=5 (true center), MarginV=0.
    const ap = { alignment: 5, marginV: 0 };

    // Aspect → width/height
    const ASPECT = {
      '9x16': { w: 1080, h: 1920 },
      '16x9': { w: 1920, h: 1080 },
      '1x1':  { w: 1080, h: 1080 },
    };
    const dim = ASPECT[state.aspect] || ASPECT['9x16'];

    // Asset root + team for brand guidelines lookup. Use the same
    // case-insensitive team-folder resolver as the b-roll auto-fill so we
    // don't emit a lowercase `windows` path when the actual folder is `Windows`.
    let root = '';
    try { root = localStorage.getItem('veditor.diceRoot.v1') || ''; } catch (e) { /* */ }
    const teamId = (typeof loadTeam === 'function') ? (loadTeam() || '') : '';
    // Resolve the brand-guidelines folder. Folder naming isn't consistent
    // across teams (Windows uses "Brand Guidelines", Endurance uses
    // "Guidelines"), so scan the team folder for any dir matching common
    // aliases instead of hardcoding one name.
    // Resolve the brand-guidelines folder the intuitive way (no manual picking):
    // asset root (Dice config) + selected team  ->  <root>/<team>  ->  scan for a
    // "Brand Guidelines" (or alias) subfolder and use everything inside it.
    const BG_ALIASES = [
      'brand guidelines', 'guidelines', 'brand-guidelines', 'brand_guidelines',
      'brand', 'brand book', 'brand-book', 'brand kit', 'brand-kit',
    ];
    let teamFolder = '';
    if (root && teamId && typeof resolveTeamFolder === 'function') {
      teamFolder = (await resolveTeamFolder(root, teamId)) || '';
    } else if (root && teamId) {
      teamFolder = `${root}/${teamId}`;
    }
    let brandGuidelinesPath = '';
    if (teamFolder) {
      try {
        const r = await fetch('/api/fs/list?path=' + encodeURIComponent(teamFolder));
        if (r.ok) {
          const d = await r.json();
          const hit = (d.entries || []).find(
            (e) => e.is_dir && BG_ALIASES.includes((e.name || '').toLowerCase().trim())
          );
          if (hit) brandGuidelinesPath = hit.path;
        }
      } catch (e) { /* leave empty → diagnostic gate below */ }
    }

    // AUTO mode (no seed script) REQUIRES a Brand Guidelines folder with at
    // least one readable doc — that's the source Kino writes the script from.
    // Verify it now (the server self-heals a stale drive letter on this path).
    if (autoScript) {
      if (!brandGuidelinesPath) {
        const norm = (s) => (s || '').replace(/\\/g, '/').toLowerCase().replace(/\/+$/, '');
        let why;
        if (!root) {
          why = 'No asset root is set. Open the Roll the Dice ⚙ config and set your assets root (e.g. E:\\01. Home Solutions\\Veditor Studio Assets).';
        } else if (!teamId) {
          why = 'No team is selected. Click a team pill (Windows / Endurance / Bath) in the left sidebar so Kino knows which team folder to use.';
        } else if (!teamFolder || norm(teamFolder) === norm(root)) {
          why = `Couldn't find the "${teamId}" folder inside:\n${root}\n\nMake sure that team's subfolder exists there.`;
        } else {
          why = `Found the team folder:\n${teamFolder}\n\n...but no "Brand Guidelines" folder inside it. Add one (with a short .txt or .md brand summary), or type a seed script above.`;
        }
        alert('No-script mode needs a Brand Guidelines folder.\n\n' + why + '\n\n(Or just type a seed script above to skip auto mode.)');
        return;
      }
      let bgDocs = [];
      try {
        const r = await fetch('/api/fs/list?path=' + encodeURIComponent(brandGuidelinesPath));
        if (r.ok) {
          const d = await r.json();
          bgDocs = (d.entries || []).filter((e) => !e.is_dir && /\.(txt|md|pdf|docx?|rtf)$/i.test(e.name));
        }
      } catch (e) { /* treat as missing below */ }
      if (!bgDocs.length) {
        alert(`No-script mode requires a Brand Guidelines document, but none was found in:\n\n${brandGuidelinesPath}\n\nDrop a short .txt or .md brand summary in that folder (avoid PDF/DOCX — they overflow context and crash the run), or type a seed script above.`);
        return;
      }
    }

    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    // Final deliverables go to "Final Output/<run>/" in the project root (easy
    // to find); variant_factory keeps its intermediates in videos/edit/variant_tmp/.
    const outDir = `${OUTPUT_ROOT}/variants_${ts}`;

    // HeyGen avatar hook — reads the saved Avatar-tab picker state. When on,
    // each variant opens with a 3–5s talking-head avatar speaking ONLY the
    // hook line, then cuts to the VO + b-roll body.
    let hgVar = null;
    if (state.heygenHook) {
      try { hgVar = JSON.parse(localStorage.getItem('veditor.heygen.v1') || 'null') || {}; } catch {}
    }
    const heygenHookBlock = (hgVar && hgVar.avatarId) ? `

━━━ HEYGEN AVATAR HOOK (toggle is ON) ━━━
Each variant must OPEN with a HeyGen talking-head avatar speaking ONLY that variant's hook line (the first sentence — the 3–5s scroll-stopper). The body + CTA stay VO + b-roll as normal. Per variant:

1. **Avatar hook clip.** Render the hook line via HeyGen using the avatar + voice the user picked in the Avatar tab. Run the helper directly (submit → poll → download, no gates):
\`\`\`bash
PYTHONUTF8=1 $VU_PY \\
  video-use/helpers/heygen_video.py \\
  --avatar-id "${hgVar.avatarId || ''}" \\
  --voice-id "${hgVar.voiceId || ''}" \\
  --width ${dim.w} --height ${dim.h} \\
  --avatar-style ${hgVar.avatarStyle || 'normal'} \\
  --voice-speed ${hgVar.voiceSpeed || 1.0} \\
  --output "${outDir}/variant_<N>_hook.mp4" \\
  --json --text "<this variant's hook line, verbatim>"
\`\`\`
2. **Normalize the avatar clip** to EXACTLY ${dim.w}×${dim.h} @ the variant fps, levelled dialogue [-6,-3] dBFS, so it concats cleanly with the body. (Use ffmpeg scale+pad if HeyGen's grid differs.)
3. **Body via variant_factory.** Call \`/api/variant_factory\` with the BODY + CTA text ONLY (strip the hook line — the avatar already delivered it) so the VO doesn't repeat the hook. Keep \`hook_text\` set to the hook line so the on-screen top overlay still appears over the body's opening b-roll for continuity (or omit if you judge it redundant).
4. **Concat** \`variant_<N>_hook.mp4\` + the variant_factory body output → \`${outDir}/variant_<N>.mp4\` (concat demuxer if codecs match, else a single filter_complex concat). The avatar hook is the final clip's first 3–5s.

If the HeyGen helper reports a missing key or a non-2xx on the FIRST variant, abort and tell the user to check HEYGEN_API_KEY / their Creator-plan credits before retrying — do not burn credits on the rest.
` : (state.heygenHook ? `

━━━ HEYGEN AVATAR HOOK ━━━
The toggle is ON but no avatar is selected in the Avatar tab. Skip the avatar hook for this run and proceed VO + b-roll only; note this in the report so the user knows to pick an avatar first.
` : '');

    const prompt =
`VARIANT FACTORY — autonomous batch generation, NO approval gates, NO preview gates

━━━ HARD RULE — RUN STRAIGHT THROUGH ━━━
The user has pre-authorized this entire pipeline by clicking "Generate Variants". Do NOT ask "should I proceed?", do NOT render previews and wait, do NOT confirm style choices. The only legitimate stop condition is a hard failure (helper exits non-zero, brand-guidelines file missing IF present in path, ElevenLabs API error).

━━━ TASK ━━━
${autoScript
  ? `Invent and generate ${state.count} ORIGINAL short VO + B-roll ad variants for this brand — THERE IS NO SEED SCRIPT. You choose the topic, angle, and message yourself, grounded entirely in the brand guidelines below. Each variant gets a DIFFERENT angle (e.g. problem-first, social-proof, FOMO, benefit-first, contrarian-take) and a different hook line. Each must hit roughly ${state.duration}s at natural ElevenLabs pace (~2.9 words/second → ~${Math.round(state.duration * 2.9)} words target). Hook + body + CTA structure mandatory.`
  : `Generate ${state.count} variants of a short VO + B-roll video from the seed script below. Each variant gets a DIFFERENT angle (e.g. problem-first, social-proof, FOMO, benefit-first, contrarian-take) and a different hook line. Each variant must hit roughly ${state.duration}s when read at natural pace by an ElevenLabs voice (~2.9 words/second → ~${Math.round(state.duration * 2.9)} words target). Hook + body + CTA structure mandatory.`}

━━━ BRAND GUIDELINES ━━━
${autoScript
  ? `**MANDATORY — this run has NO seed script, so the brand folder IS your source of truth. Read it in TWO passes before writing anything:**

PASS 1 — BRAND RULES: Read ONLY plain-text brand notes (.txt/.md) in \`${brandGuidelinesPath}\`, and read at most ~12,000 characters total. Do NOT open .pdf, .docx, image, or video files — they overflow your context and crash the run. If the brand rules exist ONLY as a PDF/DOCX, STOP and tell the user to add a short .txt or .md brand summary to that folder. From the text you read, extract and internalize: product/service, key offers/promotions, value propositions, target audience, approved claims, required terminology, tone of voice, off-limits topics.

PASS 2 — WINNING SCRIPTS: The folder also contains WINNING SCRIPTS the user has added (proven high-performing ad copy — .txt/.md files). Read each of them (skip .pdf/.docx — if a winning script is only in those formats, ask the user to re-save it as .txt so it doesn't overflow context). STUDY them as your model: how they open (hook style), how they build (problem → solution → proof), sentence rhythm and length, word choice, how they land the CTA, what makes them feel human and native to the platform. These are exemplars of what works for THIS brand.

Then WRITE YOUR OWN original scripts that (a) obey every brand rule from Pass 1, and (b) follow the winning patterns you learned in Pass 2 — same voice, cadence, and structure, but fresh copy and a distinct angle per variant. Do NOT copy a winning script verbatim; learn from them and produce new work in the same style.

If you cannot read the folder or it has neither brand docs nor scripts, STOP and report it — do NOT invent a brand from nothing.`
  : (brandGuidelinesPath ? `Look for brand guidelines documents at \`${brandGuidelinesPath}\`. If the folder exists, read only the .txt/.md files in it (at most ~12,000 characters total; do NOT open .pdf/.docx/images — they overflow context and crash the run) before drafting (including any winning example scripts — study their voice and structure). The variants MUST respect every constraint stated there — claims, terminology, voice, off-limits topics. If the folder doesn't exist or is empty, proceed using common-sense brand-safe defaults (no medical/legal claims, no competitor mentions, no superlatives without evidence) and note this in the report.` : `No asset root + team is set in the studio, so no brand-guidelines folder is available. Use common-sense brand-safe defaults (no medical/legal claims, no competitor mentions, no superlatives without evidence).`)}

${autoScript
  ? `━━━ NO SEED SCRIPT (auto mode) ━━━
There is no seed script — you are writing all ${state.count} scripts from scratch, grounded in the brand guidelines above. You have full creative latitude within those brand constraints.`
  : `━━━ SEED SCRIPT ━━━
"""
${script}
"""`}
${heygenHookBlock}
━━━ CTA RULE (HARD — applies to EVERY variant) ━━━
The call-to-action must drive the viewer to CLICK/TAP THE AD ITSELF — never a website, URL, or phone number. Meta ads carry their own click-through link/button, so the CTA points there. Use phrasing like: "Tap the link below", "Click below to get your free quote", "I've left the link right below", "Hit the button below to get started". NEVER say "visit <something>.com", never read out a web address, never say "go to our website", never give a phone number. If the brand guidelines specify a website/phone CTA, TRANSLATE it into a click-the-ad CTA — do not reproduce the URL.

━━━ LEGAL DISCLAIMERS (HARD — compliance, print-only, per variant) ━━━
Some offers/claims legally REQUIRE a printed disclaimer. Disclaimers are PRINT ONLY — never spoken in the VO. You assemble the correct disclaimer string per variant and pass it as \`disclaimer_text\` in the JSON body; the helper burns it bottom-center in 18pt Arial black. If a variant triggers NONE of the rules below, omit \`disclaimer_text\`.

1. **Read the brand folder for disclaimers first.** \`${brandGuidelinesPath || '(the team Guidelines folder)'}\` contains this brand's disclaimer language. Read ONLY the .txt/.md files there (never open the raw .pdf/.docx brand book — it overflows context). These EXACT trigger→disclaimer rules are mandatory and override any paraphrase:
   - If the ad uses a **"Buy one get one 40% off"** offer → disclaimer MUST include: \`*minimum purchase of 4\`
   - If the ad uses financing, it MUST be worded EXACTLY: **"No money down, no monthly payments, no interest for 12 months"** (no exceptions, no rewording) → disclaimer MUST include: \`*interest accrues during promotional period but is waived if paid in full within 12 months\`
   - If the ad mentions **Fibrex**, it MUST be written **Fibrex®** (with the registered-trademark symbol) everywhere it appears (VO caption text AND on-screen), AND include the Fibrex disclaimer from the folder.
   - Any **Endurance** offer/claim disclaimers: read them from the folder and include the ones that apply.
2. **Synthetic-actor rule (HARD):** ${(function(){ try { return (teamId && teamId.toLowerCase()==='endurance') || state.heygenHook; } catch(e){ return false; } })() ? `THIS RUN uses AI-generated people (${(teamId&&teamId.toLowerCase()==='endurance')?'the Endurance b-roll is AI-generated':''}${state.heygenHook?' + a HeyGen AI avatar':''}). EVERY variant's \`disclaimer_text\` MUST include \`*features synthetic actor\`.` : `If any clip shows an AI-generated person (currently: the Endurance b-roll library, or a HeyGen AI avatar), that variant's \`disclaimer_text\` MUST include \`*features synthetic actor\`. (Not applicable to this run's b-roll unless you add a HeyGen avatar.)`}
3. **Combine** all applicable disclaimers for a variant into ONE \`disclaimer_text\` string, separated by \`  ·  \` (e.g. \`*minimum purchase of 4  ·  *features synthetic actor\`). The helper wraps it to stay fully in frame.

━━━ EXECUTION ━━━

**STEP 0 — BUILD B-ROLL PROXIES FIRST (mandatory, once per run, BEFORE any variant).**
The b-roll may be 4K, which overloads a VRAM-limited GPU and can hard-freeze the machine when several renders run at once. Pre-downscale to cached 1080p proxies with a SINGLE blocking call, and WAIT for it to finish before firing any variant:
\`\`\`
POST http://127.0.0.1:8765/api/broll_proxies
{ "folder": "${state.brollFolder}" }
\`\`\`
This runs sequentially (one clip at a time — GPU-gentle, safe to run while the user edits in another app) and is cached, so it's fast on repeat runs (only new/changed clips get proxied). It returns a summary like \`{built, reused, already_1080p, failed}\`. Only after it returns 200 do you proceed to STEP 1. The variant renders then automatically use the light proxies.

**B-roll is TRUSTED.** The user curates \`${state.brollFolder}\` for action-only clips — no talking-head classification pass is needed. Do NOT extract frames, do NOT run vision on the b-roll, do NOT build an EXCLUDE_PATHS list. The variant_factory helper picks clips from the folder for you. If a specific clip looks off in the final, the user will curate it out of the folder — do NOT add complexity here. Leave \`exclude_paths\` out of the JSON body (or send \`[]\`).

**STEP 1 — ${autoScript ? `Invent ${state.count} original variant scripts from the brand guidelines (no seed)` : `Draft ${state.count} variant scripts`}.** Each gets a DIFFERENT angle (hook + body + CTA, ~${Math.round(state.duration * 2.9)} words, different angle from siblings)${autoScript ? ', every line compliant with the brand guidelines' : ''}. Print all ${state.count} drafts as a numbered list${autoScript ? ' with a one-line note on which brand value-prop each leans on' : ''}.

**STEP 1.5 — VO NATURALNESS REVIEW (mandatory manager pass — do this before ANY TTS).**
Put on a second hat: you are now a strict script editor whose ONLY job is to make sure each script sounds like a real person talking, not an AI reading copy. For EACH draft, read it ALOUD in your head and check:
- **Say-it-out-loud test:** would a real person actually say this sentence in conversation? If it sounds like marketing copy or a brochure, rewrite it.
- **No tongue-twisters / clusters** that TTS will fumble; no run-on sentences — break them so there's a natural breath.
- **Contractions & natural rhythm:** use "you're / it's / here's / that's". Vary sentence length — a couple short punchy lines, then a longer one. Robotic even-length sentences are a tell.
- **Punctuate for the voice:** commas and periods where a human pauses; an em-dash for a beat. This directly controls TTS pacing.
- **No unspeakable tokens:** spell out or rephrase symbols, URLs, "%", "&", "#", raw numbers that TTS mangles (write "twenty percent", "three hundred dollars"). No emoji.
- **Smooth transitions:** each sentence should hand off to the next; no abrupt topic jumps.
Rewrite every line that fails until the whole script passes. Print the FINAL revised scripts (these are what you send to TTS) with a one-line note per variant on what you changed. Only these approved scripts proceed to STEP 2.

**STEP 2 — Fire ${state.count} parallel POST requests** to \`http://127.0.0.1:8765/api/variant_factory\`. JSON body per variant:

\`\`\`json
{
  "broll_folder": "${state.brollFolder}",
  "script_text": "<the variant's rewritten script>",
  "voice_id": "${state.voiceId}",
  "output": "${outDir}/variant_<N>.mp4",
  "width": ${dim.w}, "height": ${dim.h},
  "fps": "30/1",
  "tts_stability": 0.4,
  "tts_similarity": 0.8,
  "tts_style": 0.15,
  "seed": <a FRESH random integer per variant AND per run — e.g. current unix-time seconds + N*1000. Do NOT reuse fixed values like 1000+N: the seed drives b-roll clip selection AND the random in-point windows, so a repeated seed reproduces the exact same b-roll sequence as the previous run>,
  "exclude_paths": [],
  "match": "${state.match || 'random'}",
  "broll_fallback": "${state.fallback || 'none'}",
  "caption_font": "${font}",
  "caption_size": ${ps.fontSize || 42},
  "caption_bg": "${cap.bg}",
  "caption_fg": "${cap.fg}",
  "caption_max_chars": ${ps.maxChars || 20},
  "caption_min_duration": ${ps.minDuration || 1.5},
  "caption_tail_pad": 0.25,
  "caption_alignment": ${ap.alignment},
  "caption_margin_v": ${ap.marginV},
  "caption_case": "${ps.caseStyle || 'natural'}",
  "caption_gap_frames": ${typeof ps.gapFrames === 'number' ? ps.gapFrames : 0},
  "caption_shadow": ${ps.shadow ? 'true' : 'false'},
  "caption_max_lines": ${ps.layout === 'double' ? 2 : 1},
  "hook_text": "<this variant's hook line — the 5-8 word scroll-stopper, distinct per variant>",
  "hook_font": "<one preset font, MUST differ from the caption font '${font}'; may match cta_font>",
  "cta_text": "<on-screen CTA: a REAL OFFER from the brand guidelines, 4-8 words>",
  "cta_font": "<one preset font, MUST differ from the caption font '${font}'>",
  "cta_mode": "<'full' or 'last30' — pick per variant>",
  "disclaimer_text": "<REQUIRED legal fine-print for this variant per the LEGAL DISCLAIMERS rules — combine applicable ones with '  ·  '. OMIT this field entirely if no rule triggers.>"
}
\`\`\`

The \`hook_text\` is burned as a static text overlay at the TOP of the frame (first 3s), inside the Meta ad safe zone (top 14% / 270px is IG/FB UI, so it lands at y=300px). \`hook_font\` MUST be one of the studio's preset fonts (Arial, Arial Black, Impact, Montserrat, Bebas Neue, Oswald, DM Sans) and **MUST NOT be the caption font ("${font}")** — the hook has to look distinct from the captions. It MAY match the CTA font (they're allowed to share a style). Prefer Arial Black or Impact (guaranteed installed). Captions are forced to CENTER FRAME (alignment 5, margin 0). All three text layers — hook, captions, CTA — burn on a 100%-opaque background box. Use each variant's distinct hook line in \`hook_text\` (NOT the body copy).

**ON-SCREEN CTA (\`cta_text\` / \`cta_font\` / \`cta_mode\`) — rules:**
- \`cta_text\` must be an OFFER taken from the brand guidelines (e.g. a free quote, a discount, a promotion actually stated there) — phrased as a short action line, 4–8 words. If no brand-guidelines folder is available on this run, derive the offer from the seed script. Same click-the-ad rule as the VO CTA: no URLs, no phone numbers, no "visit our website".
- \`cta_font\` must be one of the studio's preset fonts: Arial, Arial Black, Impact, Montserrat, Bebas Neue, Oswald, DM Sans — and it MUST NOT be the caption font ("${font}"). Prefer Arial Black or Impact (guaranteed installed on this machine).
- \`cta_mode\`: "full" keeps the CTA on screen for the whole video; "last30" shows it only in the final 30%. Pick whichever fits the variant's pacing.
- Placement/fitting is handled by the helper: it renders ~200px below the center captions, auto-wraps and shrinks to stay fully in frame, and clamps above the Meta bottom-35% UI zone. You only supply the text, font, and mode.

Each call takes ~30-60s (TTS + transcribe + concat + caption burn). Run them **in WAVES OF AT MOST 5 concurrent requests** (bash \`&\` + \`wait\` per wave, or Python \`asyncio.gather\` on batches of 5). ElevenLabs caps concurrent TTS at 5 — firing all ${state.count} at once makes the 6th+ return HTTP 429 and that variant silently fails. So for ${state.count} variants, run ${Math.ceil(state.count/5)} wave(s) of ≤5, waiting for each wave to finish before starting the next.

**STEP 3 — Parse each response's \`output\` field, and VERIFY each file.** If any variant returns non-2xx, RETRY that ONE variant once with a fresh seed (a 429 from the concurrency cap or a transient WinError clears on retry). After all waves, \`ffprobe\` each output: confirm it exists, has a video+audio stream, and duration > 1s. Report any variant that is missing or invalid as FAILED with its error — do not silently drop it. Note: ElevenLabs may return 401 \`payment_issue\` if the subscription has a billing problem; if you see that on the FIRST variant, abort immediately and tell the user to resolve their ElevenLabs billing before retrying.

━━━ REPORT ━━━
When all ${state.count} variants land, print a markdown table:

| # | Angle | Hook line | CTA (offer · font · mode) | Output path |
|---|-------|-----------|---------------------------|-------------|
| 1 | …     | "…"       | "…" · … · full/last30     | \`${outDir}/variant_1.mp4\` |
| … | …     | "…"       | …                         | …           |

End with: total wall time, average ElevenLabs character spend, and any rejected/retried variants.`;

    closeModal();
    activateTab('chat');
    promptInput.value = prompt;
    skipPipelineWrap = true;
    nextSubmitRouted = true;   // autonomous batch → server routes the model (Sonnet)
    // Fresh session — see Roll the Dice note: a continued session with a
    // finished prior run makes Kino report "already complete" without working.
    continueToggle.checked = false;
    promptForm.requestSubmit();
  });

  // Initialise the sub-label "VO + B-roll · 5×" on first paint.
  if (subEl) subEl.textContent = `VO + B-roll · ${state.count}×`;
})();

// ============================================================
// HEYGEN AVATAR TAB
// ============================================================
(function initHeygenTab() {
  const DEFAULT_AVATAR_ID = 'a78e96535de64bd4bbf758c1ec0eb90a';
  const HG_STATE_KEY = 'veditor.heygen.v1';

  // Persisted picker state
  const defaultState = {
    avatarId:    DEFAULT_AVATAR_ID,
    voiceId:     '',
    mode:        'segment',           // 'segment' | 'pip'
    quality:     '1080',              // '720' | '1080' — cost tier
    aspect:      'landscape',         // 'landscape' | 'vertical' | 'square'
    avatarStyle: 'normal',
    bg:          '#0e1a26',
    voiceSpeed:  1.0,
    pipPosition: 'br',
    pipScale:    32,
    includePublic: true,
  };

  // Resolve quality + aspect to (width, height). HeyGen renders at the chosen
  // pixel grid; 720 vs 1080 hits different credit tiers (Creator plan: 1080
  // costs roughly 2× the credits of 720 per second of output).
  function resolveDimensions(quality, aspect) {
    const h = (quality === '720') ? 720 : 1080;
    if (aspect === 'vertical') return { width: h, height: Math.round(h * 16 / 9) };
    if (aspect === 'square')   return { width: h, height: h };
    return { width: Math.round(h * 16 / 9), height: h };  // landscape default
  }
  let hg = (() => {
    try { return Object.assign({}, defaultState, JSON.parse(localStorage.getItem(HG_STATE_KEY) || 'null') || {}); }
    catch { return { ...defaultState }; }
  })();
  function saveHg() {
    try { localStorage.setItem(HG_STATE_KEY, JSON.stringify(hg)); } catch {}
  }

  // ── DOM refs ──
  const tab        = document.querySelector('.tab[data-tab="avatar"]');
  if (!tab) return;
  const statusEl   = $('#hg-status');
  const grid       = $('#hg-avatar-grid');
  const search     = $('#hg-avatar-search');
  const publicTog  = $('#hg-public-toggle');
  const refreshAv  = $('#hg-refresh-avatars');
  const voiceSearch= $('#hg-voice-search');
  const voiceSel   = $('#hg-voice-select');
  const refreshVo  = $('#hg-refresh-voices');
  const voicePrev  = $('#hg-voice-preview');
  const modeChips  = document.querySelectorAll('[data-hg-mode]');
  const qualitySel = $('#hg-quality');
  const aspectSel  = $('#hg-aspect');
  const styleSel   = $('#hg-avatar-style');
  const bgInput    = $('#hg-bg-color');
  const bgRow      = $('#hg-bg-row');
  const speedInput = $('#hg-voice-speed');
  const pipPanel   = document.querySelector('.hg-pip-only');
  const pipChips   = document.querySelectorAll('[data-hg-pos]');
  const pipScale   = $('#hg-pip-scale');
  const pipScaleOut= $('#hg-pip-scale-out');
  const scriptEl   = $('#hg-script');
  const scriptMeta = $('#hg-script-meta');
  const genBtn     = $('#hg-generate-btn');
  const previewBtn = $('#hg-preview-prompt-btn');
  const previewEl  = $('#hg-prompt-preview');

  // ── Cache ──
  let avatars = null;          // { avatars: [...], talking_photos: [...] } from API
  let voices  = null;          // raw voices payload

  // ── Initial state ──
  if (qualitySel) qualitySel.value = hg.quality;
  if (aspectSel)  aspectSel.value  = hg.aspect;
  if (styleSel) styleSel.value = hg.avatarStyle;
  if (bgInput)  bgInput.value  = hg.bg;
  if (speedInput) speedInput.value = hg.voiceSpeed;
  if (pipScale) { pipScale.value = hg.pipScale; pipScaleOut.textContent = hg.pipScale + '%'; }
  if (publicTog) publicTog.checked = hg.includePublic;
  syncMode();

  // ── Status check ──
  async function checkStatus() {
    if (!statusEl) return;
    try {
      const r = await fetch('/api/heygen/status');
      const d = await r.json();
      if (!d.helper_exists) {
        statusEl.textContent = 'helper script missing — heygen_video.py not found';
        statusEl.className = 'hg-status err';
      } else if (!d.key_configured) {
        statusEl.textContent = 'HEYGEN_API_KEY not set in video-use/.env';
        statusEl.className = 'hg-status err';
      } else {
        statusEl.textContent = 'connected · Creator plan';
        statusEl.className = 'hg-status ok';
      }
    } catch (e) {
      statusEl.textContent = 'server unreachable — restart studio? ' + e.message;
      statusEl.className = 'hg-status err';
    }
  }

  // ── Avatar grid ──
  async function loadAvatars(refresh = false) {
    if (!grid) return;
    grid.innerHTML = '<div class="hint" style="padding:12px">loading…</div>';
    try {
      const url = '/api/heygen/avatars' + (refresh ? '?refresh=true' : '');
      const r = await fetch(url);
      if (!r.ok) throw new Error(await r.text());
      avatars = await r.json();
      renderAvatars();
    } catch (e) {
      grid.innerHTML = `<div class="hint" style="padding:12px;color:var(--err)">${e.message}</div>`;
    }
  }

  function renderAvatars() {
    if (!grid || !avatars) return;
    const q = (search && search.value || '').trim().toLowerCase();
    const showPublic = publicTog ? publicTog.checked : true;
    grid.innerHTML = '';

    // Combine arrays — HeyGen returns {avatars: [...], talking_photos: [...]}.
    // Some entries are user-private, others are public (premade) — the API
    // doesn't always split them, so we treat everything as one list and
    // surface "public" via the avatar's own metadata if present.
    const list = [];
    for (const a of (avatars.avatars || [])) list.push({ kind: 'avatar', ...a });
    for (const a of (avatars.talking_photos || [])) list.push({ kind: 'talking_photo', ...a });
    if (!list.length) {
      grid.innerHTML = '<div class="hint" style="padding:12px">no avatars returned · try refresh</div>';
      return;
    }

    let count = 0;
    for (const a of list) {
      const name = a.avatar_name || a.talking_photo_name || a.name || '(unnamed)';
      const id   = a.avatar_id || a.talking_photo_id || a.id;
      const isPub = a.is_public === true || a.access_level === 'public' || a.tags === 'public';
      if (!showPublic && isPub) continue;
      if (q && !name.toLowerCase().includes(q) && !(id || '').toLowerCase().includes(q)) continue;

      const card = document.createElement('div');
      card.className = 'hg-avatar-card';
      if (id === hg.avatarId) card.classList.add('selected');
      if (id === DEFAULT_AVATAR_ID) card.classList.add('is-default');
      card.dataset.avatarId = id;
      card.title = `${name} · ${id}`;

      const thumbUrl = a.preview_image_url || a.preview_url || a.image_url;
      if (thumbUrl) {
        const img = document.createElement('img');
        img.className = 'hg-avatar-thumb';
        img.src = thumbUrl;
        img.alt = name;
        img.loading = 'lazy';
        card.appendChild(img);
      } else {
        const ph = document.createElement('div');
        ph.className = 'hg-avatar-thumb placeholder';
        ph.textContent = '👤';
        card.appendChild(ph);
      }

      const nm = document.createElement('div');
      nm.className = 'hg-avatar-name';
      nm.textContent = name;
      card.appendChild(nm);

      if (a.kind === 'talking_photo') {
        const tg = document.createElement('div');
        tg.className = 'hg-avatar-tag';
        tg.textContent = 'photo';
        card.appendChild(tg);
      }

      card.addEventListener('click', () => {
        hg.avatarId = id;
        saveHg();
        renderAvatars();
      });
      grid.appendChild(card);
      count++;
    }
    if (!count) {
      grid.innerHTML = '<div class="hint" style="padding:12px">no matches</div>';
    }
  }

  // ── Voices ──
  async function loadVoices(refresh = false) {
    if (!voiceSel) return;
    voiceSel.innerHTML = '<option value="">loading…</option>';
    try {
      const url = '/api/heygen/voices' + (refresh ? '?refresh=true' : '');
      const r = await fetch(url);
      if (!r.ok) throw new Error(await r.text());
      voices = await r.json();
      renderVoices();
    } catch (e) {
      voiceSel.innerHTML = `<option value="">error: ${e.message.slice(0, 50)}</option>`;
    }
  }

  function renderVoices() {
    if (!voiceSel || !voices) return;
    const q = (voiceSearch && voiceSearch.value || '').trim().toLowerCase();
    voiceSel.innerHTML = '<option value="">— pick a voice —</option>';
    const list = voices.voices || [];
    for (const v of list) {
      const id   = v.voice_id;
      const name = v.name || '(unnamed)';
      const lang = v.language || '';
      const gender = v.gender || '';
      const label = `${name}${lang ? ' · ' + lang : ''}${gender ? ' · ' + gender : ''}`;
      if (q && !label.toLowerCase().includes(q)) continue;
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = label;
      if (id === hg.voiceId) opt.selected = true;
      voiceSel.appendChild(opt);
    }
  }

  // ── Mode toggle (segment vs pip) ──
  function syncMode() {
    if (modeChips) {
      modeChips.forEach((c) => c.classList.toggle('selected', c.dataset.hgMode === hg.mode));
    }
    if (pipPanel) pipPanel.hidden = (hg.mode !== 'pip');
    if (bgRow)    bgRow.style.opacity = (hg.mode === 'pip') ? '0.4' : '1';
  }

  // ── Wire up events ──
  if (refreshAv) refreshAv.addEventListener('click', () => loadAvatars(true));
  if (refreshVo) refreshVo.addEventListener('click', () => loadVoices(true));
  if (search)    search.addEventListener('input', renderAvatars);
  if (voiceSearch) voiceSearch.addEventListener('input', renderVoices);
  if (publicTog) publicTog.addEventListener('change', () => {
    hg.includePublic = publicTog.checked; saveHg(); renderAvatars();
  });
  if (voiceSel) voiceSel.addEventListener('change', () => {
    hg.voiceId = voiceSel.value; saveHg();
  });
  if (voicePrev) voicePrev.addEventListener('click', () => {
    if (!voices || !hg.voiceId) return;
    const v = (voices.voices || []).find((x) => x.voice_id === hg.voiceId);
    if (v && v.preview_audio) window.open(v.preview_audio, '_blank');
  });

  modeChips.forEach((c) => c.addEventListener('click', () => {
    hg.mode = c.dataset.hgMode; saveHg(); syncMode();
  }));

  if (qualitySel) qualitySel.addEventListener('change', () => { hg.quality = qualitySel.value; saveHg(); });
  if (aspectSel)  aspectSel.addEventListener('change',  () => { hg.aspect  = aspectSel.value;  saveHg(); });
  if (styleSel) styleSel.addEventListener('change', () => { hg.avatarStyle = styleSel.value; saveHg(); });
  if (bgInput)  bgInput.addEventListener('change', () => { hg.bg = bgInput.value; saveHg(); });
  if (speedInput) speedInput.addEventListener('change', () => {
    hg.voiceSpeed = Math.max(0.5, Math.min(1.5, parseFloat(speedInput.value) || 1.0));
    speedInput.value = hg.voiceSpeed; saveHg();
  });
  pipChips.forEach((c) => c.addEventListener('click', () => {
    hg.pipPosition = c.dataset.hgPos; saveHg();
    pipChips.forEach((x) => x.classList.toggle('selected', x === c));
  }));
  if (pipScale) pipScale.addEventListener('input', () => {
    hg.pipScale = parseInt(pipScale.value, 10);
    pipScaleOut.textContent = hg.pipScale + '%';
    saveHg();
  });

  if (scriptEl) scriptEl.addEventListener('input', updateScriptMeta);

  function updateScriptMeta() {
    if (!scriptEl || !scriptMeta) return;
    const text = scriptEl.value;
    const chars = text.length;
    // Rough WPM-based estimate: 150 WPM × ~5 chars/word ≈ 12.5 chars/sec.
    const seconds = chars / 12.5;
    scriptMeta.textContent = `${chars} chars · ~${seconds.toFixed(1)}s estimated`;
  }
  updateScriptMeta();

  // ── Build the prompt the agent runs ──
  function buildHeygenPrompt() {
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const { width: w, height: h } = resolveDimensions(hg.quality, hg.aspect);
    const isPip = hg.mode === 'pip';
    const transparentFlag = isPip ? ' --transparent' : '';
    const ext = isPip ? 'webm' : 'mp4';
    // Working files live under videos/edit/; the finished deliverable goes to
    // "Final Output/<run>/". Segment mode: the avatar IS the deliverable →
    // Final Output. PIP mode: the avatar is an intermediate (composited below)
    // → keep it in videos/edit; the composite is the deliverable.
    const runDir   = `videos/edit/heygen_${hg.mode}_${ts}`;
    const finalDir = `Final Output/heygen_${hg.mode}_${ts}`;
    const outFile  = isPip ? `${runDir}/avatar.${ext}` : `${finalDir}/avatar.${ext}`;
    const text = (scriptEl && scriptEl.value || '').trim();

    if (!hg.avatarId)  return { error: 'Pick an avatar first.' };
    if (!hg.voiceId)   return { error: 'Pick a voice first.' };
    if (!text)         return { error: 'Script is empty.' };

    const lines = [];
    lines.push('## HeyGen avatar generation — autonomous, no approval gates');
    lines.push('');
    lines.push(`**Mode:** ${isPip ? 'PIP overlay (transparent background)' : 'Full segment (color background)'}`);
    lines.push(`**Avatar:** ${hg.avatarId}`);
    lines.push(`**Voice:** ${hg.voiceId} @ ${hg.voiceSpeed}× speed`);
    lines.push(`**Quality tier:** ${hg.quality}p (${hg.aspect}) → ${w}×${h}`);
    if (!isPip) lines.push(`**Background:** ${hg.bg}`);
    lines.push(`**Output:** \`${outFile}\``);
    lines.push('');
    lines.push('### Step 1 — Submit & poll');
    lines.push('Run the helper directly. It submits the render to HeyGen, polls every 8s, and downloads the result on completion. No preview gate, no permission ask.');
    lines.push('');
    lines.push('```bash');
    lines.push('PYTHONUTF8=1 $VU_PY \\');
    lines.push('  video-use/helpers/heygen_video.py \\');
    lines.push(`  --avatar-id "${hg.avatarId}" \\`);
    lines.push(`  --voice-id "${hg.voiceId}" \\`);
    lines.push(`  --output "${outFile}" \\`);
    lines.push(`  --width ${w} --height ${h} \\`);
    lines.push(`  --avatar-style ${hg.avatarStyle} \\`);
    lines.push(`  --voice-speed ${hg.voiceSpeed}${transparentFlag} \\`);
    if (!isPip) lines.push(`  --background "${hg.bg}" \\`);
    lines.push('  --json \\');
    lines.push('  --text "$(cat <<\'__HG_TEXT__\'');
    lines.push(text);
    lines.push('__HG_TEXT__');
    lines.push('  )"');
    lines.push('```');
    lines.push('');

    if (isPip) {
      const composerVid = composerVideoSelect && composerVideoSelect.value;
      if (composerVid) {
        const pos = hg.pipPosition;
        const scale = hg.pipScale;
        const pipOut = `${finalDir}/composite.mp4`;
        // Map position to ffmpeg overlay X/Y with 24px margin
        const POS_MAP = {
          tl: 'overlay=24:24',
          tr: `overlay=W-w-24:24`,
          bl: `overlay=24:H-h-24`,
          br: `overlay=W-w-24:H-h-24`,
        };
        lines.push('### Step 2 — Composite over picked video');
        lines.push(`Base video: \`${composerVid}\``);
        lines.push(`Position: ${pos.toUpperCase()} corner · scale ${scale}% of base height · 24px margin.`);
        lines.push('');
        lines.push('```bash');
        lines.push(`PATH="$FFMPEG_DIR:$PATH" ffmpeg -y -hwaccel cuda \\`);
        lines.push(`  -i "${composerVid}" \\`);
        lines.push(`  -i "${outFile}" \\`);
        lines.push(`  -filter_complex "[1:v]scale=-1:ih*${scale}/100[ov];[0:v][ov]${POS_MAP[pos]}[v]" \\`);
        lines.push(`  -map "[v]" -map 0:a? \\`);
        lines.push(`  -c:v h264_nvenc -preset p4 -cq 19 -c:a aac -b:a 192k \\`);
        lines.push(`  "${pipOut}"`);
        lines.push('```');
        lines.push('');
        lines.push(`Final deliverable: \`${pipOut}\`. The HeyGen segment audio is mixed into the base video's audio track via aac re-encode (single pass, no re-encode of the base video stream beyond the overlay).`);
      } else {
        lines.push('### Step 2 — Composite');
        lines.push('No video selected in the pipeline composer. The HeyGen overlay is generated but not composited. Pick a base video in the composer and re-run, or composite manually with ffmpeg.');
      }
    } else {
      lines.push('### Done');
      lines.push(`Final deliverable: \`${outFile}\`. Drop into the pipeline composer as the video source if you want to add captions, b-roll, etc.`);
    }
    lines.push('');
    lines.push('Report the output path, file size, and HeyGen video_id.');
    return { prompt: lines.join('\n'), outFile };
  }

  if (previewBtn) previewBtn.addEventListener('click', () => {
    const r = buildHeygenPrompt();
    if (r.error) { previewEl.textContent = r.error; previewEl.hidden = false; return; }
    previewEl.textContent = r.prompt;
    previewEl.hidden = !previewEl.hidden;
  });

  if (genBtn) genBtn.addEventListener('click', () => {
    const r = buildHeygenPrompt();
    if (r.error) { alert(r.error); return; }
    activateTab('chat');
    promptInput.value = r.prompt;
    skipPipelineWrap = true;
    nextSubmitModelOverride = 'sonnet';   // mechanical helper run — sonnet is plenty
    // Fresh session — same "already complete" hazard as Dice/Variants.
    continueToggle.checked = false;
    promptForm.requestSubmit();
  });

  // Lazy-load: only fetch when the tab is actually opened, not at page load.
  let loadedOnce = false;
  tab.addEventListener('click', () => {
    if (loadedOnce) return;
    loadedOnce = true;
    checkStatus();
    loadAvatars(false);
    loadVoices(false);
  });
})();

// ============================================================
// TEAM / BRAND PICKER
// ============================================================
(function initTeamPicker() {
  const pills = document.querySelectorAll('.team-pill');
  const sub   = document.getElementById('dice-team-sub');
  if (!pills.length) return;

  function applyState() {
    const active = loadTeam();
    pills.forEach((p) => {
      const id = p.dataset.team;
      const on = (id === active);
      p.classList.toggle('active', on);
      p.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    // Update the dice button subtitle so the active team is visible at a
    // glance from the Roll button itself.
    if (sub) {
      if (active && TEAMS[active]) {
        sub.textContent = '▸ ' + TEAMS[active].name.toLowerCase();
        sub.style.color = 'rgba(0, 230, 255, 0.85)';
      } else {
        sub.textContent = 'auto-compose';
        sub.style.color = '';
      }
    }
  }

  pills.forEach((p) => {
    p.addEventListener('click', () => {
      const id = p.dataset.team;
      const cur = loadTeam();
      // Click the active pill again to deselect (return to "no team" mode).
      saveTeam(cur === id ? '' : id);
      applyState();
      // Broadcast so any other panel listening (dice modal, future tabs)
      // can re-render scoped views. Same-tab localStorage changes don't
      // trigger the native 'storage' event, hence the custom event.
      window.dispatchEvent(new CustomEvent('kk:team-changed', {
        detail: { teamId: cur === id ? '' : id },
      }));
    });
  });

  applyState();
})();

// ============================================================
// FILES TAB — Outputs / Resources sub-tabs (added 2026-05-07)
// ============================================================
(function initFilesTab() {
  const subtabs   = document.querySelectorAll('.files-subtab');
  const refreshBtn= $('#files-refresh-btn');
  const outputsEl = $('#files-outputs');
  const resourcesEl = $('#files-resources');
  const blurb     = $('#files-blurb');
  if (!outputsEl || !resourcesEl) return;

  const cntOutputs = $('#fst-count-outputs');
  const cntResources = $('#fst-count-resources');

  // Text preview overlay
  const overlay   = $('#text-preview-overlay');
  const overlayTitle = $('#text-preview-title');
  const overlayBody  = $('#text-preview-body');
  const overlayClose = $('#text-preview-close');

  const VIDEO_EXTS_RE = /\.(mp4|mov|webm|m4v|mkv)$/i;
  const AUDIO_EXTS_RE = /\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i;
  const TEXT_EXTS_RE  = /\.(txt|md|srt|vtt|json)$/i;

  function fmtRel(mtime) {
    const ms = Date.now() - mtime * 1000;
    if (ms < 60_000) return Math.round(ms / 1000) + 's ago';
    if (ms < 3600_000) return Math.round(ms / 60_000) + 'm ago';
    if (ms < 86400_000) return Math.round(ms / 3600_000) + 'h ago';
    return Math.round(ms / 86400_000) + 'd ago';
  }

  function iconFor(name) {
    if (VIDEO_EXTS_RE.test(name)) return '🎬';
    if (AUDIO_EXTS_RE.test(name)) return '🎵';
    if (/\.srt$/i.test(name))     return '💬';
    if (/\.json$/i.test(name))    return '⚙';
    if (/\.(txt|md)$/i.test(name)) return '📄';
    return '·';
  }

  // Heuristic: a "deliverable" is the visible output of a run — final.mp4,
  // composite.mp4, or any .mp4 at the run-folder root that's not a proxy /
  // intermediate. Highlight these so the team can find them quickly.
  function isDeliverable(name) {
    if (!VIDEO_EXTS_RE.test(name)) return false;
    if (/proxy|_1080p\.mp4$|_synced\.mp4$|hook_trimmed/i.test(name)) return false;
    return /\b(final|composite|deliverable|export|out)\b/i.test(name) || /^[^_]+\.(mp4|mov|webm)$/i.test(name);
  }

  // Make a row interactive: click → preview (video/audio in player, text in
  // overlay). Drag → dropzones (existing flow).
  function attachRowBehavior(row, file) {
    row.draggable = true;
    row.dataset.path = file.path;

    row.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData(FS_DRAG_MIME, file.path);
      e.dataTransfer.setData('text/plain', file.path);
      e.dataTransfer.effectAllowed = 'copy';
      document.querySelectorAll('.dropzone').forEach((dz) => dz.classList.add('in-app-drop-target'));
    });
    row.addEventListener('dragend', () => {
      document.querySelectorAll('.dropzone').forEach((dz) => dz.classList.remove('in-app-drop-target'));
    });

    row.addEventListener('click', () => {
      // Update active highlight across both views
      document.querySelectorAll('.file-row.active').forEach((r) => r.classList.remove('active'));
      row.classList.add('active');

      if (VIDEO_EXTS_RE.test(file.path)) {
        loadVideo(file.path);
      } else if (AUDIO_EXTS_RE.test(file.path)) {
        // Use the same player element — works for audio-only files.
        loadVideo(file.path);
      } else if (TEXT_EXTS_RE.test(file.path)) {
        showTextPreview(file);
      }
    });

    // Double-click on video → also import into composer (legacy quick-pick)
    if (VIDEO_EXTS_RE.test(file.path)) {
      row.addEventListener('dblclick', () => {
        if (typeof importFsFile === 'function') importFsFile(file.path);
      });
    }
  }

  async function showTextPreview(file) {
    if (!overlay) return;
    overlayTitle.textContent = file.path;
    overlayBody.textContent = 'loading…';
    overlay.hidden = false;
    try {
      const r = await fetch('/api/file/' + encodeURIComponent(file.path));
      const txt = await r.text();
      overlayBody.textContent = txt.length > 200_000
        ? txt.slice(0, 200_000) + '\n\n[truncated — file is ' + fmtSize(file.size) + ']'
        : txt;
    } catch (e) {
      overlayBody.textContent = 'error: ' + e.message;
    }
  }

  function fileRow(file, opts = {}) {
    const row = document.createElement('div');
    row.className = 'file-row';
    if (opts.deliverable) row.classList.add('is-deliverable');

    const icon = document.createElement('span');
    icon.className = 'fr-icon';
    icon.textContent = iconFor(file.name || file.path);

    const name = document.createElement('span');
    name.className = 'fr-name';
    // For run-folder files, show just the leaf name (not the full path).
    name.textContent = opts.shortName || file.name || file.path.split('/').pop();
    name.title = file.path;

    const size = document.createElement('span');
    size.className = 'fr-size';
    size.textContent = fmtSize(file.size);

    const mtime = document.createElement('span');
    mtime.className = 'fr-mtime';
    mtime.textContent = fmtRel(file.mtime);

    row.append(icon, name, size, mtime);
    attachRowBehavior(row, file);
    return row;
  }

  // ── Outputs view: group artifacts by their first path segment under edit/ ──
  function renderOutputs(artifacts) {
    outputsEl.innerHTML = '';
    if (!artifacts.length) {
      outputsEl.innerHTML = '<div class="files-empty">no outputs yet — kick off a render and they\'ll appear here</div>';
      return;
    }

    // Group by first folder under videos/edit/. Files at the edit root land in '' (loose).
    const groups = new Map();   // folder name → array of files
    const looseFiles = [];
    for (const f of artifacts) {
      // f.name is the path relative to videos/edit/, e.g. "dice_2026-05-07-15-30-22/final.mp4"
      const rel = f.name || f.path.replace(/^videos\/edit\//, '');
      const idx = rel.indexOf('/');
      if (idx < 0) {
        looseFiles.push({ ...f, _shortName: rel });
      } else {
        const folder = rel.slice(0, idx);
        const inside = rel.slice(idx + 1);
        if (!groups.has(folder)) groups.set(folder, { files: [], newest: 0 });
        const g = groups.get(folder);
        g.files.push({ ...f, _shortName: inside });
        if (f.mtime > g.newest) g.newest = f.mtime;
      }
    }

    // Per-run folders, newest first
    const sortedGroups = [...groups.entries()].sort((a, b) => b[1].newest - a[1].newest);
    for (const [folder, group] of sortedGroups) {
      outputsEl.appendChild(buildRunCard(folder, group));
    }

    // Loose top-level files (legacy from before per-run folders)
    if (looseFiles.length) {
      const loose = document.createElement('div');
      loose.className = 'run-card';
      loose.classList.add('expanded');

      const header = document.createElement('div');
      header.className = 'run-card-header';
      header.innerHTML = '<span class="rch-toggle">▸</span><span class="rch-name">_loose (pre-folder runs)</span><span class="rch-meta">' + looseFiles.length + ' files</span>';
      header.addEventListener('click', () => loose.classList.toggle('expanded'));
      loose.appendChild(header);

      const body = document.createElement('div');
      body.className = 'run-card-body';
      looseFiles.sort((a, b) => b.mtime - a.mtime).forEach((f) => {
        body.appendChild(fileRow(f, { shortName: f._shortName }));
      });
      loose.appendChild(body);
      outputsEl.appendChild(loose);
    }
  }

  function buildRunCard(folder, group) {
    const card = document.createElement('div');
    card.className = 'run-card';

    // Detect team from folder prefix or any file metadata if available.
    // Folder names follow patterns like:  dice_<ts>, heygen_segment_<ts>, <stem>_<ts>
    const teamMatch = folder.match(/^(endurance|windows|bath)_/i);
    const teamTag = teamMatch ? teamMatch[1].toUpperCase() : '';

    const header = document.createElement('div');
    header.className = 'run-card-header';
    const teamHtml = teamTag ? `<span class="rch-team">${teamTag}</span>` : '';
    const newestStr = fmtRel(group.newest);
    header.innerHTML =
      '<span class="rch-toggle">▸</span>' +
      `<span class="rch-name">${teamHtml}${escapeHtml(folder)}</span>` +
      `<span class="rch-meta">${group.files.length} files · ${newestStr}</span>`;
    header.addEventListener('click', () => card.classList.toggle('expanded'));
    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'run-card-body';

    // Sort: deliverables first, then by mtime desc
    const files = [...group.files].sort((a, b) => {
      const aD = isDeliverable(a._shortName) ? 1 : 0;
      const bD = isDeliverable(b._shortName) ? 1 : 0;
      if (aD !== bD) return bD - aD;
      return b.mtime - a.mtime;
    });
    for (const f of files) {
      body.appendChild(fileRow(f, {
        shortName: f._shortName,
        deliverable: isDeliverable(f._shortName),
      }));
    }
    card.appendChild(body);

    // Auto-expand the most recent (first) card so the user sees latest output immediately.
    if (outputsEl.children.length === 0) card.classList.add('expanded');

    return card;
  }

  // ── Resources view: flat list of sources, grouped by kind ──
  function renderResources(sources) {
    resourcesEl.innerHTML = '';
    if (!sources.length) {
      resourcesEl.innerHTML = '<div class="files-empty">no resources yet — drop raw video / audio / script files into the dropzones on the right</div>';
      return;
    }
    const byKind = { video: [], audio: [], script: [] };
    for (const f of sources) {
      const k = f.kind || (VIDEO_EXTS_RE.test(f.path) ? 'video' :
                           AUDIO_EXTS_RE.test(f.path) ? 'audio' : 'script');
      (byKind[k] || (byKind[k] = [])).push(f);
    }
    const ORDER = [['video', 'videos'], ['audio', 'audio'], ['script', 'scripts']];
    for (const [k, label] of ORDER) {
      const list = byKind[k] || [];
      if (!list.length) continue;
      const grp = document.createElement('div');
      grp.className = 'files-group';
      const lbl = document.createElement('div');
      lbl.className = 'files-group-label';
      lbl.textContent = `${label} (${list.length})`;
      grp.appendChild(lbl);
      list.sort((a, b) => b.mtime - a.mtime).forEach((f) => {
        grp.appendChild(fileRow(f));
      });
      resourcesEl.appendChild(grp);
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ── Sub-tab switching ──
  subtabs.forEach((s) => {
    s.addEventListener('click', () => {
      const view = s.dataset.filesView;
      subtabs.forEach((x) => {
        const on = (x === s);
        x.classList.toggle('active', on);
        x.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      outputsEl.hidden = (view !== 'outputs');
      resourcesEl.hidden = (view !== 'resources');
    });
  });

  // ── Refresh handler — fetches /api/files, renders both views, updates counts ──
  async function refreshFilesTab() {
    try {
      const r = await fetch('/api/files');
      const j = await r.json();
      renderOutputs(j.artifacts || []);
      renderResources(j.sources || []);
      if (cntOutputs) cntOutputs.textContent = (j.artifacts || []).length;
      if (cntResources) cntResources.textContent = (j.sources || []).length;
    } catch (e) {
      outputsEl.innerHTML = `<div class="files-empty" style="color:var(--err)">error: ${e.message}</div>`;
    }
  }

  if (refreshBtn) refreshBtn.addEventListener('click', refreshFilesTab);

  // Hook into the existing refreshFiles() so this tab updates on every poll.
  if (typeof window.__originalRefreshFiles === 'undefined') {
    window.__originalRefreshFiles = (typeof refreshFiles === 'function') ? refreshFiles : null;
    window.refreshFilesTab = refreshFilesTab;
  }

  // Text-preview overlay close
  if (overlayClose) overlayClose.addEventListener('click', () => { overlay.hidden = true; });
  if (overlay) overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.hidden = true; });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay && !overlay.hidden) overlay.hidden = true;
  });

  // Initial load + periodic refresh (cheap — just /api/files)
  refreshFilesTab();
  setInterval(refreshFilesTab, 8000);
})();

// ============================================================
// JOBS — workflow category averages (added 2026-05-07)
// ============================================================
(function initWorkflowSummary() {
  const summary = $('#jobs-summary');
  if (!summary) return;

  // Workflow definitions, in priority order. First match wins. Each `match`
  // function takes the ops Set for a job and returns true if this category
  // applies. The order matters: HeyGEN > AI B-Roll > Complex UGC > Simple
  // UGC > A/V Sync.
  // The op names below match what `detectOperations()` produces — see the
  // top of app.js. If you add a new helper, also add it there.
  const WORKFLOWS = [
    {
      id: 'heygen',
      name: 'HeyGEN',
      desc: 'avatar gen → b-roll → captions → music → export',
      // HeyGEN run = anything that touches heygen_video.py (with or without b-roll/captions on top).
      match: (ops) => ops.has('heygen_video'),
    },
    {
      id: 'ai-broll',
      name: 'AI B-Roll',
      desc: 'TTS VO → b-roll stitch → captions → music → export',
      // Generated VO (tts_voice) + b-roll stitching is the signature of an AI b-roll piece.
      match: (ops) => ops.has('tts_voice') && ops.has('broll_overlay'),
    },
    {
      id: 'complex-ugc',
      name: 'Complex UGC',
      desc: 'cut to script + captions + b-roll + music → export',
      // Talking-head cut + captions + b-roll/music layered on.
      match: (ops) => ops.has('best_take') && ops.has('captions')
                     && (ops.has('broll_overlay') || ops.has('tts_music')),
    },
    {
      id: 'simple-ugc',
      name: 'Simple UGC',
      desc: 'cut to script + captions → export',
      // Talking-head cut + captions, no b-roll, no music gen.
      match: (ops) => ops.has('best_take') && ops.has('captions'),
    },
    {
      id: 'av-sync',
      name: 'A/V Sync',
      desc: 'sync audio + level dialogue + rotate → export',
      // Sync (or match-pairs) + leveling, with optional rotate. NO captions, NO best_take.
      match: (ops) => (ops.has('audio_sync') || ops.has('auto_pair_sync'))
                     && !ops.has('captions') && !ops.has('best_take'),
    },
  ];

  function classify(job) {
    const ops = new Set((job.operations || []).map((o) => o.op).filter(Boolean));
    if (!ops.size) return null;
    for (const wf of WORKFLOWS) {
      if (wf.match(ops)) return wf.id;
    }
    return null;
  }

  function fmtMsShort(ms) {
    if (ms == null) return '—';
    const s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    return Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
  }

  function renderWorkflowSummary(jobs) {
    let card = $('#workflow-summary-card');
    if (!card) {
      card = document.createElement('section');
      card.className = 'workflow-summary';
      card.id = 'workflow-summary-card';
      card.innerHTML = '<h3>averages by workflow</h3><div class="workflow-grid" id="workflow-grid"></div>';
      // Insert before the existing per-operation table
      summary.parentNode.insertBefore(card, summary);
    }
    const grid = $('#workflow-grid');
    grid.innerHTML = '';

    // Aggregate
    const buckets = {};
    for (const wf of WORKFLOWS) buckets[wf.id] = { jobs: [], totalCost: 0, totalWall: 0, totalTurns: 0 };
    for (const job of (jobs || [])) {
      if (!job.completed_at) continue;
      const cat = classify(job);
      if (!cat) continue;
      const b = buckets[cat];
      b.jobs.push(job);
      b.totalCost += job.cost_usd || 0;
      b.totalWall += job.wall_clock_ms || 0;
      b.totalTurns += job.turns || 0;
    }

    for (const wf of WORKFLOWS) {
      const b = buckets[wf.id];
      const n = b.jobs.length;
      const isEmpty = n === 0;
      const c = document.createElement('div');
      c.className = `workflow-card ${wf.id}` + (isEmpty ? ' empty' : '');
      const avgCost = n ? '$' + (b.totalCost / n).toFixed(4) : '—';
      const avgWall = n ? fmtMsShort(b.totalWall / n) : '—';
      const avgTurns = n ? Math.round(b.totalTurns / n) : '—';
      const totalCost = n ? '$' + b.totalCost.toFixed(2) : '—';
      c.innerHTML = `
        <div class="wf-count">${n}</div>
        <div class="wf-name">${wf.name}</div>
        <div class="wf-desc">${wf.desc}</div>
        <div class="wf-stats">
          <span class="wf-stat-label">avg cost</span><span class="wf-stat-val">${avgCost}</span>
          <span class="wf-stat-label">avg wall</span><span class="wf-stat-val">${avgWall}</span>
          <span class="wf-stat-label">avg turns</span><span class="wf-stat-val">${avgTurns}</span>
          <span class="wf-stat-label">total spent</span><span class="wf-stat-val">${totalCost}</span>
        </div>
      `;
      grid.appendChild(c);
    }
  }

  // Expose the renderer on window so refreshJobs (defined earlier at module
  // scope) can call it without us having to reach into its closure.
  window.renderWorkflowSummary = renderWorkflowSummary;

  // If jobs data has already been fetched once before this IIFE runs, render
  // the summary immediately on next refresh poll. The Jobs tab also has its
  // own refresh button which will trigger a re-fetch + render.
})();

// ============================================================
// TIMELINE STRIP — removed per user request (2026-05-07).
// The init function below now bails out immediately because the timeline DOM
// no longer exists. Code retained as a stub for now in case we add a
// different timeline visualization later.
// ============================================================
(function initTimeline() {
  return;  // disabled — DOM removed
  // eslint-disable-next-line no-unreachable
  const tlZoomIn  = $('#tl-zoom-in');
  const tlZoomOut = $('#tl-zoom-out');
  const tlLoadBtn = $('#tl-load-btn');
  const tlInfo    = $('#tl-info');
  const tlBody    = $('#tl-body');
  const tcVideo   = $('#tc-video');
  const tcGfx     = $('#tc-gfx');
  const tcCaps    = $('#tc-caps');
  const tlRuler   = $('#tl-ruler');
  const tlPlayhead = $('#tl-playhead');
  const player    = $('#player');

  if (!tlBody || !player) return;

  let tlData = null;   // last loaded /api/timeline response
  let tlZoom = 1.0;    // pixels-per-second multiplier (relative; 1.0 = auto-fit)
  const ZOOM_STEP = 1.4;

  // ---- zoom controls ----
  if (tlZoomIn)  tlZoomIn.addEventListener('click',  () => { tlZoom *= ZOOM_STEP; renderTimeline(); });
  if (tlZoomOut) tlZoomOut.addEventListener('click', () => { tlZoom = Math.max(0.1, tlZoom / ZOOM_STEP); renderTimeline(); });

  // ---- load button ----
  if (tlLoadBtn) tlLoadBtn.addEventListener('click', loadTimeline);

  // ---- auto-load when player src changes ----
  player.addEventListener('loadedmetadata', () => {
    tlZoom = 1.0;
    loadTimeline();
  });

  // Track label column is 60px wide; content starts at x=60 in the tl-body.
  const LABEL_W = 60;

  // ---- playhead tracks video time ----
  player.addEventListener('timeupdate', () => {
    if (!tlData || !tlData.duration) return;
    const trackW = tcVideo ? tcVideo.getBoundingClientRect().width : 0;
    const pxPerSec = computePxPerSec(trackW, tlData.duration);
    const x = LABEL_W + player.currentTime * pxPerSec;
    if (tlPlayhead) {
      tlPlayhead.style.left  = x + 'px';
      tlPlayhead.style.display = 'block';
    }
  });

  // Clicking the tl-body (tracks area) seeks the player.
  // tl-ruler has pointer-events:none so clicks fall through to tl-body.
  if (tlBody) {
    tlBody.addEventListener('click', (e) => {
      if (!tlData || !tlData.duration || !player.src) return;
      const rect = tlBody.getBoundingClientRect();
      const xInBody = e.clientX - rect.left;
      if (xInBody < LABEL_W) return; // clicked label column
      const trackW = tcVideo ? tcVideo.getBoundingClientRect().width : 0;
      const pxPerSec = computePxPerSec(trackW, tlData.duration);
      const t = (xInBody - LABEL_W) / pxPerSec;
      player.currentTime = Math.max(0, Math.min(tlData.duration, t));
    });
  }

  function computePxPerSec(trackW, duration) {
    if (!duration) return 1;
    const base = (trackW || 600) / duration;
    return base * tlZoom;
  }

  async function loadTimeline() {
    if (tlInfo) tlInfo.textContent = 'loading…';
    try {
      const res = await fetch('/api/timeline');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      tlData = await res.json();
      renderTimeline();
    } catch (e) {
      if (tlInfo) tlInfo.textContent = 'no timeline data yet';
      console.warn('[timeline] load failed:', e);
    }
  }

  function renderTimeline() {
    if (!tlData) return;
    const { duration, tracks } = tlData;
    if (!duration) { if (tlInfo) tlInfo.textContent = 'duration unknown'; return; }

    const trackW = tcVideo ? tcVideo.getBoundingClientRect().width : 600;
    const pxPerSec = computePxPerSec(trackW, duration);
    const totalW = Math.round(duration * pxPerSec);

    // Set a min-width on the tl-body so horizontal scroll works
    if (tlBody) tlBody.style.minWidth = (totalW + 8) + 'px';

    if (tlInfo) {
      const fmt = (s) => new Date(s * 1000).toISOString().slice(11, 19);
      tlInfo.textContent = `${fmt(duration)} · zoom ${tlZoom.toFixed(1)}×`;
    }

    // Ruler tick marks
    if (tlRuler) {
      tlRuler.innerHTML = '';
      tlRuler.style.width = totalW + 'px';
      const step = chooseTick(pxPerSec);
      let tickIdx = 0;
      for (let t = 0; t <= duration; t += step) {
        const isMajor = (tickIdx % 2 === 0);
        const tick = document.createElement('div');
        tick.className = 'tl-ruler-tick ' + (isMajor ? 'major' : 'minor');
        tick.style.left = Math.round(t * pxPerSec) + 'px';
        if (isMajor) {
          const lbl = document.createElement('span');
          lbl.textContent = fmtTime(t);
          tick.appendChild(lbl);
        }
        tlRuler.appendChild(tick);
        tickIdx++;
      }
    }

    // Render each track
    renderTrack(tcVideo, totalW, pxPerSec, tracks.video  || [], 'tl-clip-video');
    renderTrack(tcGfx,   totalW, pxPerSec, tracks.gfx    || [], 'tl-clip-gfx');
    renderTrack(tcCaps,  totalW, pxPerSec, tracks.caps   || [], 'tl-clip-cap');
  }

  function renderTrack(container, totalW, pxPerSec, clips, cls) {
    if (!container) return;
    container.innerHTML = '';
    container.style.width = totalW + 'px';
    for (const c of clips) {
      const el = document.createElement('div');
      el.className = 'tl-clip ' + cls;
      el.style.left  = Math.round(c.start * pxPerSec) + 'px';
      el.style.width = Math.max(2, Math.round((c.end - c.start) * pxPerSec)) + 'px';
      if (c.label) {
        el.title = c.label;
        if ((c.end - c.start) * pxPerSec > 40) {
          el.textContent = c.label;
        }
      }
      container.appendChild(el);
    }
  }

  function chooseTick(pxPerSec) {
    // Try ticks at [2, 5, 10, 30, 60, 120, 300] seconds; pick smallest where gap ≥ 40px
    const candidates = [2, 5, 10, 30, 60, 120, 300];
    for (const s of candidates) {
      if (s * pxPerSec >= 40) return s;
    }
    return 300;
  }

  function fmtTime(s) {
    const m = Math.floor(s / 60);
    const ss = Math.floor(s % 60);
    return m + ':' + String(ss).padStart(2, '0');
  }

  // Auto-load on startup if the edit directory likely has data
  loadTimeline();
})();

// ============================================================
// SHARED PROGRESS TRACKER (Kino Flow)
// Polls GET /api/progress?job=<id> and drives a .kf-prog bar.
// Any flow generates a job_id, passes it in its render POST, and calls
// window.kfTrackProgress(jobId, {container, fill, label}) alongside the
// (still-pending) POST — FastAPI serves the poll + the render at once.
// Returns stop(); call it in your finally to hide the bar.
// ============================================================
window.kfNewJobId = function(prefix) {
  return (prefix || 'job') + '_' + Date.now() + '_' + Math.floor(Math.random() * 1e6);
};
window.kfTrackProgress = function(jobId, els) {
  let stopped = false;
  const fmtEta = (s) => s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
  if (els.container) els.container.hidden = false;
  if (els.fill) els.fill.style.width = '0%';
  async function tick() {
    if (stopped) return;
    try {
      const r = await fetch('/api/progress?job=' + encodeURIComponent(jobId));
      const d = await r.json();
      if (d.found) {
        if (els.fill) els.fill.style.width = d.percent + '%';
        if (els.label) els.label.textContent = d.done
          ? '✓ complete'
          : `${d.percent}% · ${d.phase} · ~${fmtEta(d.eta_s)} left`;
        if (d.done) { stopped = true; return; }
      }
    } catch { /* transient — keep polling */ }
    if (!stopped) setTimeout(tick, 700);
  }
  tick();
  return function stop() {
    stopped = true;
    if (els.container) els.container.hidden = true;
    if (els.fill) els.fill.style.width = '0%';
  };
};
