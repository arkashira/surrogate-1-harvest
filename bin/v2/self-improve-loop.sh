#!/usr/bin/env bash
# Surrogate-1 v2 — Self-Improvement Loop (the sustainability cron).
#
# Daily: generate problems → Surrogate v2 attempts → LLM judge scores →
# winners append to training set, losers stored in reflexion-store with
# a critique-derived lesson. Closes the loop without humans.
#
# Built around the existing free LLM ladder (cerebras > groq > openrouter
# > gemini > chutes > ollama) — no Anthropic API.
#
# Schedule: every 6h via start.sh cron. Output: ~/.surrogate/data/v2/self-improve/{date}.jsonl
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

DATE="${SELF_IMPROVE_DATE:-$(date +%Y%m%d-%H)}"
N_PROBLEMS="${SELF_IMPROVE_N:-50}"
KEEP_TOP_PCT="${SELF_IMPROVE_KEEP_PCT:-40}"   # keep top 40% as winners
LOG="$HOME/.surrogate/logs/self-improve-${DATE}.log"
OUT_DIR="$HOME/.surrogate/data/v2/self-improve"
WIN_FILE="$OUT_DIR/winners-${DATE}.jsonl"
LOSE_FILE="$OUT_DIR/losers-${DATE}.jsonl"
mkdir -p "$OUT_DIR" "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] self-improve-loop start n=$N_PROBLEMS" | tee -a "$LOG"

# Use existing serve endpoint if up; else fall back to LLM ladder for inference.
SURROGATE_URL="${SURROGATE_URL:-http://127.0.0.1:8000}"
SURROGATE_UP=0
curl -fsS --max-time 3 "$SURROGATE_URL/v1/models" >/dev/null 2>&1 && SURROGATE_UP=1
echo "[$(date +%H:%M:%S)] surrogate vLLM up=$SURROGATE_UP" | tee -a "$LOG"

# Kick the python driver. All work in Python — bash is just the launcher.
N_PROBLEMS="$N_PROBLEMS" KEEP_TOP_PCT="$KEEP_TOP_PCT" \
  SURROGATE_URL="$SURROGATE_URL" SURROGATE_UP="$SURROGATE_UP" \
  WIN_FILE="$WIN_FILE" LOSE_FILE="$LOSE_FILE" \
python3 - <<'PYEOF' 2>&1 | tee -a "$LOG"
"""Driver: problem-gen → surrogate-attempt → judge → split."""
import json, os, random, sys, time, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
sys.path.insert(0, str(Path.home() / ".surrogate/bin/v2"))

N = int(os.environ.get("N_PROBLEMS", 50))
KEEP_PCT = int(os.environ.get("KEEP_TOP_PCT", 40))
SURROGATE_URL = os.environ.get("SURROGATE_URL", "http://127.0.0.1:8000")
SURROGATE_UP = os.environ.get("SURROGATE_UP", "0") == "1"
WIN_FILE = Path(os.environ["WIN_FILE"])
LOSE_FILE = Path(os.environ["LOSE_FILE"])

# 22 domain prompts (mirrors magpie-self-instruct.py categories)
DOMAINS = [
    ("code-python", "Write a non-trivial Python function"),
    ("code-typescript", "Write a TypeScript function with proper types"),
    ("devops-tf", "Write a Terraform module"),
    ("devops-k8s", "Write a Kubernetes manifest"),
    ("devops-cdk", "Write an AWS CDK construct"),
    ("sec-iam", "Write a least-privilege IAM policy"),
    ("sec-secrets", "Detect and remediate hardcoded secrets in this snippet"),
    ("sec-cve", "Explain how to mitigate this CVE in production"),
    ("sre-runbook", "Write an incident response runbook for"),
    ("sre-slo", "Define SLI/SLO + error budget for"),
    ("data-sql", "Write a parameterized SQL query for"),
    ("ai-eng", "Implement a RAG pipeline component"),
    ("ai-prompt", "Design a system prompt for"),
    ("api-rest", "Design a REST API endpoint contract"),
    ("api-graphql", "Write a GraphQL resolver"),
    ("ci-github", "Write a GitHub Actions workflow"),
    ("debug-traceback", "Diagnose and fix this Python traceback"),
    ("perf-profile", "Identify the bottleneck in this code"),
    ("test-pytest", "Write pytest tests for"),
    ("docs-api", "Write API documentation for"),
    ("arch-adr", "Write an ADR for"),
    ("cloud-cost", "Optimize cloud cost for"),
]


def llm_ladder(prompt: str, sys_prompt: str = "", max_tokens: int = 1024) -> str:
    """Free LLM ladder via existing bridges. Returns first non-empty."""
    bridges = [
        ("$HOME/.surrogate/bin/cerebras-bridge.sh", "cerebras"),
        ("$HOME/.surrogate/bin/groq-bridge.sh", "groq"),
        ("$HOME/.surrogate/bin/openrouter-bridge.sh", "openrouter"),
        ("$HOME/.surrogate/bin/gemini-bridge.sh", "gemini"),
        # ("$HOME/.surrogate/bin/chutes-bridge.sh", "chutes")),  # disabled 2026-04-30: chutes 402 free-tier dead
        ("$HOME/.surrogate/bin/ollama-bridge.sh", "ollama"),
    ]
    import subprocess
    for sh, name in bridges:
        sh_path = os.path.expandvars(sh)
        if not Path(sh_path).exists():
            continue
        try:
            req = json.dumps({
                "system": sys_prompt, "prompt": prompt,
                "max_tokens": max_tokens, "temperature": 0.7,
            })
            r = subprocess.run(["bash", sh_path], input=req, capture_output=True,
                               text=True, timeout=60)
            out = r.stdout.strip()
            if out and len(out) > 20:
                return out
        except Exception:
            continue
    return ""


def gen_problem(domain: str, hint: str) -> str:
    sys_p = ("You are a senior interviewer at a top tech company. Generate ONE "
             "specific, concrete coding/devops/security problem. Output the "
             "problem statement only — no preamble, no solution, no markdown "
             "fences. 2-5 sentences. Specify expected I/O, constraints, "
             "real tools/libs only.")
    p = f"Domain: {domain}. Generate one problem. Format: '{hint} ___'."
    return llm_ladder(p, sys_p, max_tokens=200).strip()


def surrogate_attempt(prob: str) -> str:
    if SURROGATE_UP:
        try:
            req = json.dumps({
                "model": "surrogate-1-coder-7b-v2",
                "messages": [{"role": "user", "content": prob}],
                "max_tokens": 1024, "temperature": 0.4,
            }).encode()
            r = urllib.request.Request(
                f"{SURROGATE_URL}/v1/chat/completions",
                data=req,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(r, timeout=90) as resp:
                d = json.loads(resp.read())
                return d["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  surrogate err: {e}", file=sys.stderr)
    # fallback: ladder (uses qwen-coder via openrouter free)
    return llm_ladder(prob, "You are Surrogate-1, an expert coding agent.",
                      max_tokens=1024)


def judge(prob: str, attempt: str) -> dict:
    sys_p = ("You are a strict code reviewer. Score the attempt on a SOLUTION "
             "from 0-10 across: correctness, security, completeness, idiomatic. "
             "Return ONLY JSON: "
             "{\"score\": float, \"strengths\": [str], \"weaknesses\": [str], "
             "\"would_ship\": bool}. No markdown, no preamble.")
    p = f"PROBLEM:\n{prob[:1500]}\n\nATTEMPT:\n{attempt[:3000]}\n\nReturn JSON."
    raw = llm_ladder(p, sys_p, max_tokens=400)
    try:
        # strip code fences if any
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("```")[1].lstrip("json").strip()
        return json.loads(s)
    except Exception:
        return {"score": 5.0, "strengths": [], "weaknesses": ["judge-parse-fail"],
                "would_ship": False, "raw": raw[:500]}


def main() -> None:
    samples = []
    print(f"[gen] generating {N} problems")
    for i in range(N):
        dom, hint = random.choice(DOMAINS)
        prob = gen_problem(dom, hint)
        if not prob or len(prob) < 30:
            continue
        attempt = surrogate_attempt(prob)
        if not attempt or len(attempt) < 50:
            continue
        verdict = judge(prob, attempt)
        samples.append({
            "domain": dom, "prompt": prob, "response": attempt,
            "score": float(verdict.get("score", 0)),
            "would_ship": bool(verdict.get("would_ship", False)),
            "weaknesses": verdict.get("weaknesses", []),
            "strengths": verdict.get("strengths", []),
            "ts": int(time.time()),
        })
        if (i + 1) % 10 == 0:
            print(f"  done {i+1}/{N}")

    if not samples:
        print("[done] no samples produced")
        return

    samples.sort(key=lambda x: -x["score"])
    cut = max(1, len(samples) * KEEP_PCT // 100)
    winners, losers = samples[:cut], samples[cut:]

    with open(WIN_FILE, "w") as f:
        for s in winners:
            f.write(json.dumps({"prompt": s["prompt"], "response": s["response"],
                                 "source": "self-improve", "meta": s},
                                ensure_ascii=False) + "\n")
    with open(LOSE_FILE, "w") as f:
        for s in losers:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # Push losers + critiques into reflexion-store for inference-time retrieval
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "reflexion_store",
            str(Path.home() / ".surrogate/bin/v2/reflexion-store.py"))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)  # type: ignore
        for s in losers:
            mod.store(
                task=s["prompt"], attempt=s["response"],
                error="; ".join(s["weaknesses"])[:1000],
                reflection=("Improvement directions: " +
                            "; ".join(s["weaknesses"])[:800]),
                fix="(pending — flagged for next training batch)",
                domain=s["domain"],
            )
    except Exception as e:
        print(f"  reflexion-store err: {e}")

    print(f"[done] winners={len(winners)} losers={len(losers)}  "
          f"win_avg={sum(s['score'] for s in winners)/max(1,len(winners)):.2f} "
          f"lose_avg={sum(s['score'] for s in losers)/max(1,len(losers)):.2f}")


if __name__ == "__main__":
    main()
PYEOF

echo "[$(date +%H:%M:%S)] self-improve-loop end" | tee -a "$LOG"
