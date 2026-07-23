/* ═══════════════════════════════════════════════════════════════
   KINOKORE — Manager tab (manager.js)

   On-demand quality/cost oversight. GET /api/manager/review is a
   deterministic pass over the studio's logs (sync_log.jsonl,
   level_log.jsonl, jobs.jsonl, usage.log) — no LLM. The "deep
   review" button wraps that JSON into a Kino prompt for actual
   recommendations and sends it through the normal chat path.

   Loaded AFTER app.js; the generic tab system (.tab[data-tab] ↔
   .tab-panel[data-panel]) picks up the Manager tab with no app.js
   changes.
═══════════════════════════════════════════════════════════════ */

(() => {
  'use strict';

  const $id = (s) => document.getElementById(s);
  const refreshBtn = $id('manager-refresh-btn');
  const deepBtn = $id('manager-deep-btn');
  const metaEl = $id('manager-meta');
  const cardsEl = $id('manager-cards');
  const findingsEl = $id('manager-findings');
  const opsBody = document.querySelector('#manager-ops-table tbody');
  const badge = $id('manager-badge');
  if (!refreshBtn || !cardsEl) return;

  let lastReview = null;

  const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function card(label, value, sub) {
    return `<div class="manager-card">
      <div class="mc-value">${esc(value)}</div>
      <div class="mc-label">${esc(label)}</div>
      ${sub ? `<div class="mc-sub">${esc(sub)}</div>` : ''}
    </div>`;
  }

  function render(review) {
    lastReview = review;
    metaEl.textContent = `review generated ${review.generated_at} · ${review.findings.length} finding(s), ${review.alert_count} need attention`;

    const s = review.stats || {};
    const cards = [];
    if (s.sync) cards.push(card('syncs logged', s.sync.total,
      `${s.sync.out_of_audio_window} out-of-window · ${s.sync.ambiguous_pearson} ambiguous`));
    if (s.leveling) cards.push(card('leveling passes', s.leveling.total,
      `${s.leveling.out_of_window} missed window`));
    if (s.cost) cards.push(card('project spend', '$' + s.cost.total_usd,
      `${s.cost.jobs} completed jobs`));
    if (s.turns) cards.push(card('turns', s.turns.total,
      `${s.turns.errors} error(s)`));
    cardsEl.innerHTML = cards.join('') || '<span class="hint">no log data found</span>';

    findingsEl.innerHTML = (review.findings || []).map((f) => `
      <li class="manager-finding sev-${esc(f.severity)}">
        <span class="mf-sev">${esc(f.severity)}</span>
        <span class="mf-area">${esc(f.area)}</span>
        <span class="mf-msg">${esc(f.msg)}${f.detail ? ` <span class="mf-detail">· ${esc(f.detail)}</span>` : ''}</span>
      </li>`).join('')
      || '<li class="hint">all clear — nothing to flag</li>';

    if (opsBody) {
      const ops = (s.cost && s.cost.by_operation) || {};
      opsBody.innerHTML = Object.entries(ops)
        .sort((a, b) => b[1].cost - a[1].cost)
        .map(([op, b]) => `<tr>
          <td>${esc(op)}</td><td>${b.jobs}</td>
          <td>$${b.avg_cost}</td><td>${b.avg_wall_s}s</td><td>$${b.cost}</td>
        </tr>`).join('');
    }

    if (badge) {
      badge.textContent = review.alert_count;
      badge.hidden = !review.alert_count;
    }
  }

  async function runReview(silent) {
    if (!silent) metaEl.textContent = 'reviewing logs…';
    try {
      const res = await fetch('/api/manager/review');
      if (!res.ok) throw new Error(res.statusText);
      render(await res.json());
    } catch (e) {
      if (!silent) metaEl.textContent = 'review failed: ' + e.message;
    }
  }

  refreshBtn.addEventListener('click', () => runReview(false));

  // Deep review: hand the deterministic data to Kino and ask for judgment.
  if (deepBtn) deepBtn.addEventListener('click', async () => {
    if (!lastReview) await runReview(false);
    if (!lastReview) return;
    const promptEl = $id('prompt');
    const form = $id('prompt-form');
    if (!promptEl || !form) return;
    promptEl.value =
`MANAGER DEEP REVIEW — you are the studio's production manager. Below is the deterministic review JSON compiled from videos/edit/{sync_log.jsonl, level_log.jsonl, jobs.jsonl, usage.log}.

Analyze it and report back in this shape:
1. **Health verdict** — one line, green/yellow/red.
2. **Top 3 issues** ranked by impact, each with the concrete evidence from the data and the exact fix (which helper/flag/setting to change).
3. **Cost efficiency** — where money is being wasted, and the single highest-leverage change.
4. **Trends to watch** — anything drifting in the wrong direction.

Do NOT re-read the raw logs; the JSON below is the source of truth. Do not run any renders. This is a read-only review.

━━━ REVIEW DATA ━━━
${JSON.stringify(lastReview, null, 2)}`;
    const tabBtn = document.querySelector('.tab[data-tab="chat"]');
    if (tabBtn) tabBtn.click();
    form.requestSubmit();
  });

  // First paint: run once quietly so the sidebar badge reflects reality,
  // and again whenever the tab is opened.
  document.querySelectorAll('.tab[data-tab="manager"]').forEach((b) =>
    b.addEventListener('click', () => runReview(true)));
  runReview(true);
})();
