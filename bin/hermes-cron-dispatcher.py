#!/usr/bin/env python3
"""hermes-cron dispatcher — runs as GitHub Actions cron tick.

Replaces the dead Mac hermes-gateway daemon (stopped 2026-04-27).
Reads jobs.json, picks jobs whose schedule matches current minute,
dispatches LLM-prompt jobs to a free-tier API rotation.

Triggered by .github/workflows/hermes-cron-dispatcher.yml (every 5 min).

Cron parsing follows POSIX 5-field format: minute hour dom month dow
"""
from __future__ import annotations

import json
import os
import sys
import time
import datetime
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JOBS_FILE = REPO_ROOT / "data" / "hermes-jobs.json"
LAST_RUN_FILE = REPO_ROOT / "data" / "hermes-last-run.json"
LOG_DIR = Path(os.environ.get("HOME", "/tmp")) / ".surrogate" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "hermes-cron-dispatcher.log"


def log(msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def cron_match(expr: str, now: datetime.datetime) -> bool:
    """Match a 5-field cron expression against datetime. Supports *, lists, ranges, */N."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    return (
        _cron_field_match(minute, now.minute, 0, 59) and
        _cron_field_match(hour, now.hour, 0, 23) and
        _cron_field_match(dom, now.day, 1, 31) and
        _cron_field_match(month, now.month, 1, 12) and
        _cron_field_match(dow, now.isoweekday() % 7, 0, 6)
    )


def _cron_field_match(field: str, value: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            if base == "*":
                if value % int(step) == 0:
                    return True
            else:
                start = int(base.split("-")[0])
                if (value - start) % int(step) == 0:
                    return True
        elif "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b):
                return True
        else:
            if int(part) == value:
                return True
    return False


def load_last_run() -> dict:
    if LAST_RUN_FILE.exists():
        try:
            return json.loads(LAST_RUN_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_last_run(state: dict) -> None:
    LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_FILE.write_text(json.dumps(state, indent=2, default=str))


UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def call_llm(prompt: str, max_tokens: int = 1024, timeout: int = 20) -> str:
    """11-provider fallback chain (mirrors axentx_pipeline.call_llm). Model
    names probed live 2026-05-01 — only these work for our free-tier keys."""
    chains = [
        ("Groq", "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        ("Cerebras", "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "llama3.1-8b"),
        ("SambaNova", "https://api.sambanova.ai/v1/chat/completions",
         os.environ.get("SAMBANOVA_API_KEY"), "Meta-Llama-3.3-70B-Instruct"),
        ("NVIDIA-NIM", "https://integrate.api.nvidia.com/v1/chat/completions",
         os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY"),
         "meta/llama-3.3-70b-instruct"),
        ("Kimi", "https://api.moonshot.ai/v1/chat/completions",
         os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY"),
         "moonshot-v1-8k"),
        ("xAI", "https://api.x.ai/v1/chat/completions",
         os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"),
         "grok-2-1212"),
        ("Chutes", "https://llm.chutes.ai/v1/chat/completions",
         os.environ.get("CHUTES_API_KEY"), "deepseek-ai/DeepSeek-V3"),
        ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "meta-llama/llama-3.3-70b-instruct:free"),
        ("GitHub-Models", "https://models.inference.ai.azure.com/chat/completions",
         os.environ.get("GITHUB_MODELS_TOKEN"), "gpt-4o-mini"),
    ]
    payload = {
        "messages": [{"role": "user", "content": prompt[:8000]}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    last_err = None
    for name, url, key, model in chains:
        if not key:
            continue
        body = dict(payload, model=model)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": UA_BROWSER,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                content = data["choices"][0]["message"]["content"]
                log(f"  → {name}/{model}: {len(content)} chars returned")
                return content
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, TimeoutError) as e:
            last_err = f"{name}: {e}"
            continue
    raise RuntimeError(f"all LLM providers failed; last={last_err}")


# Cloud-side path remapping — Mac jobs.json has script refs like:
#   /Users/Ashira/.claude/bin/foo.sh
#   bash ~/.claude/bin/foo.sh
#   foo.sh                  (no path, expects PATH lookup)
# On the OCI coordinator we want all of these to resolve to ./bin/foo.sh.
_REPO_BIN = REPO_ROOT / "bin"


def _rewrite_for_cloud(cmd: str) -> str:
    """Rewrite Mac-side absolute paths so the same command runs on the OCI box."""
    rewrites = (
        ("/Users/Ashira/.claude/bin/", str(_REPO_BIN) + "/"),
        ("/Users/Ashira/.hermes/scripts/", str(_REPO_BIN) + "/"),
        ("/Users/Ashira/.hermes/bin/", str(_REPO_BIN) + "/"),
        ("/Users/Ashira/.surrogate/bin/", str(_REPO_BIN) + "/"),
        ("/Users/Ashira/.surrogate/hf-space/bin/v2/", str(_REPO_BIN) + "/"),
        ("/Users/Ashira/develope/hermes-toolbelt/claude-bin/", str(_REPO_BIN) + "/"),
        ("/Users/Ashira/develope/hermes-toolbelt/hermes-scripts/", str(_REPO_BIN) + "/"),
        ("$HOME/.claude/bin/", str(_REPO_BIN) + "/"),
        ("$HOME/.hermes/scripts/", str(_REPO_BIN) + "/"),
        ("~/.claude/bin/", str(_REPO_BIN) + "/"),
        ("~/.hermes/scripts/", str(_REPO_BIN) + "/"),
    )
    for old, new in rewrites:
        cmd = cmd.replace(old, new)
    return cmd


def execute_job(job: dict) -> tuple[bool, str]:
    """Run a single hermes-cron job. Returns (success, output)."""
    job_id = job.get("id", "?")
    name = job.get("name", "unnamed")
    log(f"  ▸ executing job {job_id} '{name}'")

    # Job has a script field (shell-driven)
    script = job.get("script")
    if script:
        cmd = _rewrite_for_cloud(script)
        env = dict(os.environ)
        env["PATH"] = f"{_REPO_BIN}:{env.get('PATH', '')}"
        try:
            r = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=30, env=env,
                cwd=str(REPO_ROOT),
            )
            return r.returncode == 0, (r.stdout + r.stderr)[:4000]
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT after 30s"
        except Exception as e:
            return False, f"EXEC ERROR: {e}"

    # Job has a prompt field (LLM-driven)
    prompt = job.get("prompt", "")
    if not prompt:
        return False, "no script or prompt field"
    try:
        out = call_llm(prompt, max_tokens=1024)
        return True, out[:4000]
    except Exception as e:
        return False, f"LLM call failed: {e}"


def main() -> int:
    if not JOBS_FILE.exists():
        log(f"✗ jobs file not found: {JOBS_FILE}")
        return 1
    try:
        data = json.loads(JOBS_FILE.read_text())
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
    except Exception as e:
        log(f"✗ failed to parse jobs.json: {e}")
        return 1

    # GitHub Actions schedules can drift up to 15 min — we tolerate jobs whose
    # schedule fired within the last 5 minutes (matches our workflow tick).
    now = datetime.datetime.utcnow()
    last_run = load_last_run()
    fired = []
    skipped_recent = 0
    for job in jobs:
        if not job.get("enabled", True):
            continue
        sched = job.get("schedule", {})
        expr = sched.get("expr") if isinstance(sched, dict) else sched
        if not expr:
            continue

        # Match against current minute or any of the past 5 minutes
        match = False
        for delta in range(0, 6):
            t = now - datetime.timedelta(minutes=delta)
            if cron_match(expr, t):
                match = True
                break
        if not match:
            continue

        # Dedupe — don't run the same job twice within 4 minutes
        job_id = job.get("id", job.get("name", "?"))
        last = last_run.get(job_id)
        if last:
            last_dt = datetime.datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() < 240:
                skipped_recent += 1
                continue
        fired.append(job)

    log(f"=== tick @ {now.isoformat()}Z — {len(jobs)} jobs total, "
        f"{len(fired)} due, {skipped_recent} dedup-skipped ===")

    # Parallel execution. With 25–31 jobs/tick × 30s job-cap × 8 workers =
    # ~120s pessimal wall-clock. Budget = TICK_INTERVAL_SEC - 5 (loop kill).
    # Use TICK-30 as as_completed deadline so we have time to log unfinished.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout
    tick_budget = int(os.environ.get("TICK_INTERVAL_SEC", "300"))
    deadline = max(60, tick_budget - 30)
    successes = failures = unfinished = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(execute_job, j): j for j in fired}
        try:
            for fut in as_completed(futures, timeout=deadline):
                job = futures[fut]
                job_id = job.get("id", job.get("name", "?"))
                try:
                    ok, output = fut.result()
                except Exception as e:
                    ok, output = False, f"exec exception: {e}"
                last_run[job_id] = now.isoformat()
                if ok:
                    successes += 1
                    log(f"    ✓ {job_id}: ok ({len(output)} chars)")
                else:
                    failures += 1
                    log(f"    ✗ {job_id}: {output[:200]}")
        except FuturesTimeout:
            for fut, job in futures.items():
                if not fut.done():
                    unfinished += 1
                    job_id = job.get("id", job.get("name", "?"))
                    log(f"    ⏱ {job_id}: still running at {deadline}s deadline — abandoning")
                    fut.cancel()

    save_last_run(last_run)
    log(f"=== tick done — {successes} ok, {failures} fail, {unfinished} unfinished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
