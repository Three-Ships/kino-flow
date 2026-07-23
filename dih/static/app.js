const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};
const post = (url, body) =>
  api(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

let CFG = {};

async function loadConfig() {
  CFG = await api("/api/config");
  $("model").value = CFG.gemini_model || "";
  $("target").value = CFG.video?.target_seconds ?? 22;
  $("captions").checked = !!CFG.captions?.enabled;
  $("music").checked = !!CFG.music?.enabled;
  loadVoices();
}

async function loadVoices() {
  const sel = $("voice");
  try {
    const vs = await api("/api/voices");
    sel.innerHTML = '<option value="">— pick a voice —</option>';
    for (const v of vs) {
      const o = document.createElement("option");
      o.value = v.voice_id;
      o.textContent = `${v.name} (${v.category || "voice"})`;
      if (v.voice_id === CFG.voice_id) o.selected = true;
      sel.appendChild(o);
    }
  } catch (e) {
    sel.innerHTML = `<option value="">ElevenLabs key missing — ${e.message}</option>`;
  }
}

$("saveCfg").onclick = async () => {
  const patch = {
    voice_id: $("voice").value,
    gemini_model: $("model").value.trim(),
    video: { target_seconds: Number($("target").value) },
    captions: { enabled: $("captions").checked },
    music: { enabled: $("music").checked },
  };
  await post("/api/config", { patch });
  $("cfgMsg").textContent = "Saved ✓";
  setTimeout(() => ($("cfgMsg").textContent = ""), 1500);
  CFG = await api("/api/config");
};

$("settingsBtn").onclick = () => $("settings").classList.toggle("hidden");

$("addNote").onclick = async () => {
  const title = $("noteTitle").value.trim() || "brief";
  const text = $("noteText").value.trim();
  if (!text) return;
  await post("/api/notes", { title, text });
  $("noteTitle").value = "";
  $("noteText").value = "";
  loadNotes();
};

$("runAll").onclick = async () => {
  await post("/api/run", {});
  pollJobs();
};

async function loadNotes() {
  const notes = await api("/api/notes");
  $("noteCount").textContent = notes.length;
  const ul = $("notes");
  ul.innerHTML = "";
  for (const n of notes) {
    const li = document.createElement("li");
    li.innerHTML = `<div class="title">${n.name}</div>
      <div class="body">${escapeHtml(n.text)}</div>
      <div class="acts">
        <button class="primary" data-run="${n.name}">Run this</button>
        <button data-del="${n.name}">Delete</button>
      </div>`;
    ul.appendChild(li);
  }
  ul.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
    await post("/api/run", { name: b.dataset.run }); pollJobs();
  }));
  ul.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    await api(`/api/notes/${b.dataset.del}`, { method: "DELETE" }); loadNotes();
  }));
}

async function loadLabels() {
  const labels = await api("/api/labels");
  const box = $("labels");
  box.innerHTML = labels.length ? "" : '<span class="muted">No labels yet — add folders under broll/</span>';
  for (const l of labels) {
    const s = document.createElement("span");
    s.className = "chip" + (l.clips ? "" : " empty");
    s.innerHTML = `${l.label} <b>${l.clips}</b>`;
    box.appendChild(s);
  }
}

async function loadKnowledge() {
  const docs = await api("/api/knowledge");
  const ul = $("knowledge");
  ul.innerHTML = docs.length ? "" : '<li class="stg">Drop docs in knowledge/</li>';
  for (const d of docs) {
    const li = document.createElement("li");
    li.textContent = d;
    ul.appendChild(li);
  }
}

async function pollJobs() {
  const jobs = await api("/api/jobs");
  const ul = $("jobs");
  ul.innerHTML = jobs.length ? "" : '<li class="stg">No jobs yet.</li>';
  for (const j of jobs.slice().reverse()) {
    const li = document.createElement("li");
    li.innerHTML = `<b>${j.note_name}</b><span class="badge ${j.status}">${j.status}</span>
      <div class="stg">${j.stage || ""} ${j.message ? "· " + escapeHtml(j.message) : ""}</div>`;
    ul.appendChild(li);
  }
}

async function loadOutputs() {
  const outs = await api("/api/outputs");
  const box = $("outputs");
  box.innerHTML = outs.length ? "" : '<span class="muted">Finished videos show up here.</span>';
  for (const o of outs) {
    const div = document.createElement("div");
    div.className = "vid";
    const finalPath = o.meta?.final;
    const src = o.has_final && finalPath ? `/file?path=${encodeURIComponent(finalPath)}` : "";
    const status = o.meta?.status || (o.has_final ? "done" : "");
    div.innerHTML = `
      ${src ? `<video controls preload="metadata" src="${src}"></video>` : '<div style="aspect-ratio:9/16;display:grid;place-items:center;color:#93a3b0">no render</div>'}
      <div class="meta">
        <div><b>${o.meta?.title || o.folder}</b> <span class="badge ${status}">${status}</span></div>
        <div class="stg">${o.meta?.platform || ""} · ${o.meta?.vo_duration_s ? o.meta.vo_duration_s + "s" : ""}</div>
        ${src ? `<a href="${src}" download>Download</a>` : (o.meta?.error ? `<span class="stg">${escapeHtml(o.meta.error)}</span>` : "")}
      </div>`;
    box.appendChild(div);
  }
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function tick() { pollJobs(); loadOutputs(); }

loadConfig(); loadNotes(); loadLabels(); loadKnowledge(); tick();
setInterval(tick, 2500);
