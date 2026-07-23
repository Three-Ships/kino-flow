# server.py patch — Tasks 1 & 3 (drop-in code)

Concrete code for the routing + `--continue` changes. Line anchors reference the
current `studio/server.py`. Claude Code: verify anchors before applying (the file
moves as it's edited).

---

## Step A — add the routing helper (near the top, after `CLAUDE_BIN`, ~line 46)

```python
import functools

# Where the routing map lives once it's moved server-side.
MODEL_ROUTING_FILE = Path(__file__).resolve().parent / "model_routing.json"

@functools.lru_cache(maxsize=1)
def _routing_config() -> dict:
    try:
        return json.loads(MODEL_ROUTING_FILE.read_text(encoding="utf-8"))
    except Exception:
        # Fail safe: never block a job because the config is missing/bad.
        return {"default": "sonnet", "routes": {}, "prompt_markers": {}}

def route_model(prompt: str, requested: str = "", force: bool = False) -> str:
    """Pick a Claude model for a job.

    - If `force` is set and `requested` is non-empty, the caller's choice wins
      (the interactive model picker in the UI).
    - Otherwise classify from the prompt head and look up the routing map.
    """
    cfg = _routing_config()
    if force and requested:
        return requested
    head = (prompt or "")[:200].upper()
    for marker, job_type in (cfg.get("prompt_markers") or {}).items():
        if marker in head:
            model = (cfg.get("routes") or {}).get(job_type)
            if model:
                return model
    return cfg.get("default", "sonnet")
```

> Move `cost-optimization/model_routing.json` to `studio/model_routing.json` so the
> path above resolves. `lru_cache` means edits need a server reload to take effect —
> fine, it's config.

---

## Step B — apply routing in `/api/chat` (current lines ~161–180)

**Change the signature** to accept an explicit-override flag:

```python
@app.post("/api/chat")
async def chat(
    prompt: str = Form(...),
    continue_session: bool = Form(True),
    resume_session_id: str = Form(""),
    model: str = Form(""),
    force_model: bool = Form(False),   # NEW: UI model-picker sets this True
):
```

**Replace** the current model + continue block:

```python
    args = [CLAUDE_BIN, "--print", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits"]
    if model:
        args.extend(["--model", model])
    # Precedence: explicit --resume <id> > --continue > fresh session.
    if resume_session_id:
        args.extend(["--resume", resume_session_id])
    elif continue_session:
        args.append("--continue")
```

**with:**

```python
    args = [CLAUDE_BIN, "--print", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits"]

    # ── Model routing (Task 1) ──
    resolved_model = route_model(prompt, requested=model, force=force_model)
    args.extend(["--model", resolved_model])

    # ── Session hygiene (Task 3) ──
    # Autonomous, pre-authorized batch runs are self-contained — don't inherit
    # (and re-cache) the whole prior conversation. Detect them by prompt marker.
    head = prompt[:200].upper()
    is_autonomous = any(m in head for m in (
        "VARIANT FACTORY", "STREAMLINED AD", "ROLL THE DICE"))

    # Precedence: explicit --resume <id> > --continue > fresh session.
    if resume_session_id:
        args.extend(["--resume", resume_session_id])
    elif continue_session and not is_autonomous:
        args.append("--continue")
    # else: fresh session (autonomous batch, or caller asked not to continue)
```

Expose `resolved_model` to the response/logging if you want the job record to show
the routed model (see Step C).

---

## Step C — make `jobs.jsonl` record the routed model (telemetry accuracy)

The frontend calls `/api/jobs/start` with its own `model` (currently hardcoded
`"opus"` for batch runs), so the log won't reflect routing unless you fix one side.

**Cleanest:** have `/api/jobs/start` route it too, so the log always matches what
was actually spawned. In `jobs_start` (~line 454), change:

```python
        "model": payload.get("model") or "",
```

to:

```python
        "model": route_model(payload.get("prompt") or "", payload.get("model") or ""),
```

Then in the frontend, stop hardcoding `model: "opus"` on the batch-run POSTs — either
omit `model` (server routes) or send `force_model` only from the interactive picker.
Search `static/*.js` for `"opus"` and `model:` to find the ~2 call sites (variant
factory + streamlined ad).

---

## Verification (before/after)

1. Apply Steps A–C, restart the studio (respect the running-job check in CLAUDE.md).
2. Run one STREAMLINED AD job. Confirm the new `jobs.jsonl` row shows
   `"model": "sonnet"` and a **much** smaller `tokens_cache`.
3. Run a plain interactive chat — confirm it still uses Opus (default `chat` route).
4. Spot-check quality of the generated copy is unchanged.

Rollback is trivial: `route_model` returning `default` and removing the
`is_autonomous` branch restores prior behaviour.
```
