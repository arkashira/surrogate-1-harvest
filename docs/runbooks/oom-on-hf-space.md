# Runbook: OOM on Hugging Face Space

Alert code: `space_oom`
Severity: warn (ZeroGPU spaces self-heal) → critical if 3 OOMs in 30min

## Symptoms

- `/health` on `ashirato-surrogate-1-zero-gpu.hf.space` returns 503 or
  hangs > 30s.
- HF Space logs show one of:
  - `RuntimeError: CUDA out of memory.`
  - `Worker timeout, killing` followed by Gunicorn restart.
  - `ImportError: libcuda.so.1: cannot open shared object` after a
    forced cold-start that lost GPU lease.
- Calls to `bin/axentx_pipeline._call_surrogate_v1` start raising
  `v1: SSE returned no usable data` and the LLM chain falls through to
  the next provider (Gemini → Cerebras → …).
- `space_health` daemon flips the space to `degraded` on /dash.

## Immediate action (in order)

1. **Confirm the alert is real**: `curl -sS https://ashirato-surrogate-1-zero-gpu.hf.space/health -m 10` — if you get any 2xx, it's a transient blip; reset alert and watch.
2. **Force a Space restart** via the HF UI: Settings → Factory Reboot. ZeroGPU spaces do not always recover the GPU lease on simple "Restart" so prefer Factory Reboot. Restart takes ~90s.
3. **Stop dependent traffic for 2 minutes**: `sudo systemctl stop axentx-research-daemon@*` on the GCP host. This prevents a thundering herd reloading the Space the moment it comes back.
4. **Wait for `/health` 200**. Then start daemons back up: `sudo systemctl start axentx-research-daemon@*`.
5. **Post status** to Discord `#alerts`: "Space rebooted at HH:MM, monitoring 30 min".

## Root cause — common patterns

- **Model loaded twice**: a recent code change called `model.to('cuda')` after `from_pretrained(device_map='auto')`. Fix: use one or the other; assert `next(model.parameters()).device.type == 'cuda'` once at startup.
- **Long-context blow-up**: prompt > 8k tokens passed in. Surrogate-1 v1 LoRA on Qwen2.5-7B fits 4k comfortably; > 6k causes OOM at attention quadratic step. Fix: clamp prompt length in `_call_surrogate_v1` (it already has `prompt[:4000]` — verify the call site is honoring it).
- **Memory leak in concurrency**: ZeroGPU now allows brief concurrent calls; if Gradio's queue holds onto past activations, peak memory creeps up. Fix: in `app.py`, wrap inference in `with torch.inference_mode(), torch.cuda.empty_cache():` and call `gc.collect()` in `finally`.

## Post-incident

- Append OOM event to `state/space-health-events.jsonl` with prompt length and any stack-trace excerpt (already done by the `space_health` watcher — verify).
- If 3+ OOMs in a week from the same cause: open issue at `arkashira/surrogate-1-harvest`, tag `oom`, link to runbook.
- If a new code path is the trigger: add a regression test — synthetic 4k prompt, 6k prompt, 8k prompt — that runs against a local Qwen2.5-1.5B before the space is updated.
- Update this runbook with new patterns observed.

## Escalation

- 3 OOMs within 30 min on the same space → escalate to `critical`, page Ashira.
- 5+ OOMs in 24h on multiple spaces → likely a transformers/peft version regression. Pin the version of `transformers` and `peft` in `requirements.txt`, redeploy, hold for 24h.

## Related

- `docs/runbooks/cf-worker-down.md` (downstream impact when Space is up but Worker is the routing layer).
- `bin/axentx_pipeline.py` `_call_surrogate_v1` (caller).
- `agents/space-health-daemon` (alerting).
