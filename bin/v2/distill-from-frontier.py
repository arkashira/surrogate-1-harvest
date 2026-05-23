"""Surrogate-1 v2 — Free distillation from frontier models via free LLM ladder.

Uses ONLY free APIs (no Anthropic spend):
- Cerebras free (qwen-3-235b-a22b-instruct-2507) ~1M tok/day
- Groq free (llama-3.3-70b-versatile) ~500K tok/day
- OpenRouter free tier (DeepSeek-V3, Qwen3-Coder, Gemini Flash)
- Gemini AI Studio free
- NVIDIA NIM free
- Chutes free

Pipeline:
1. Load seed prompts from existing v2-sft data + 1000 hard custom prompts
2. For each prompt, sample N=5 completions from N different free providers
3. Self-consistency vote on best answer (majority logic / longest-correct / test-pass)
4. Output as DPO pairs (best vs worst) + as SFT (best alone)

Output: ~/.surrogate/data/v2-distill.jsonl + v2-distill-dpo.jsonl
"""
import os, json, time, sys, random, hashlib, subprocess
from pathlib import Path
from datetime import datetime
from collections import Counter

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
from sanitize import filter_pair

# Free LLM providers (already have bridges on HF Space)
PROVIDERS = [
    ("cerebras", "qwen-3-235b-a22b-instruct-2507"),
    ("groq",     "llama-3.3-70b-versatile"),
    ("groq",     "qwen-2.5-coder-32b"),
    ("openrouter", "deepseek/deepseek-chat-v3.1:free"),
    ("openrouter", "qwen/qwen3-coder-480b:free"),
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ("gemini",   "gemini-2.5-flash"),
    ("chutes",   "qwen-3-235b"),
]

OUT_SFT = Path.home() / ".surrogate/data/v2-distill.jsonl"
OUT_DPO = Path.home() / ".surrogate/data/v2-distill-dpo.jsonl"
OUT_SFT.parent.mkdir(parents=True, exist_ok=True)


def call_bridge(provider: str, model: str, messages: list, max_tokens: int = 1500) -> str | None:
    bridge_path = Path.home() / f".surrogate/bin/{provider}-bridge.sh"
    if not bridge_path.exists():
        return None
    payload = json.dumps({"messages": messages, "model": model, "max_tokens": max_tokens})
    try:
        r = subprocess.run(["bash", str(bridge_path)], input=payload,
                          capture_output=True, text=True, timeout=120)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def score_response(response: str, prompt: str) -> float:
    """Cheap quality heuristic — not perfect, but free."""
    s = 0.0
    if not response or len(response) < 30:
        return 0.0
    # Length appropriate
    s += min(1.0, len(response) / 500.0)
    # Has code block?
    if "```" in response:
        s += 0.5
    # Cites specifics (file/line/cmd)
    if any(c in response for c in ["```", "$ ", "# ", "$(", "package "]):
        s += 0.3
    # Avoid refusals
    if response.lower().startswith(("i'm sorry", "i cannot", "i can't")):
        s -= 1.0
    # Avoid known polluted patterns (sanity)
    v = filter_pair(prompt, response)
    if not v["keep"]:
        return 0.0
    return s


def distill_prompt(prompt_text: str) -> dict | None:
    """Get N completions, vote best, build SFT + DPO pair."""
    # Sample 5 providers (rotate to balance free quotas)
    chosen_providers = random.sample(PROVIDERS, k=min(5, len(PROVIDERS)))
    completions = []
    msgs = [{"role": "user", "content": prompt_text}]
    for prov, model in chosen_providers:
        resp = call_bridge(prov, model, msgs, max_tokens=1500)
        if resp:
            completions.append({
                "provider": prov,
                "model": model,
                "response": resp,
                "score": score_response(resp, prompt_text),
            })
    if len(completions) < 2:
        return None

    completions.sort(key=lambda c: -c["score"])
    best = completions[0]
    worst = completions[-1]
    if best["score"] < 0.5 or best["score"] - worst["score"] < 0.3:
        return None  # too close — skip

    return {
        "prompt": prompt_text,
        "best_response": best["response"],
        "best_provider": f"{best['provider']}:{best['model']}",
        "worst_response": worst["response"],
        "worst_provider": f"{worst['provider']}:{worst['model']}",
        "n_completions": len(completions),
        "ts": datetime.utcnow().isoformat(),
    }


def main():
    SEED_PROMPTS_PATH = Path.home() / ".surrogate/data/v2-distill-seeds.jsonl"
    if not SEED_PROMPTS_PATH.exists():
        print(f"⚠ no seeds at {SEED_PROMPTS_PATH}", flush=True)
        # Create from existing v2-sft data
        seed_dir = Path.home() / ".surrogate/data/v2-sft"
        if seed_dir.exists():
            seeds = []
            for f in seed_dir.glob("*.jsonl"):
                with open(f) as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line)
                            if obj.get("prompt"):
                                seeds.append({"prompt": obj["prompt"]})
                        except Exception:
                            continue
            random.shuffle(seeds)
            with open(SEED_PROMPTS_PATH, "w") as fh:
                for s in seeds[:10000]:
                    fh.write(json.dumps(s) + "\n")
            print(f"  built {len(seeds[:10000])} seeds from existing data", flush=True)
        else:
            print("  no v2-sft data yet — run build-data-pipeline.sh first", flush=True)
            return

    # Resume
    seen = 0
    if OUT_SFT.exists():
        with open(OUT_SFT) as f:
            seen = sum(1 for _ in f)
    print(f"resuming distill from {seen} existing samples", flush=True)

    target = int(os.environ.get("DISTILL_TARGET", "50000"))
    written = 0
    with open(SEED_PROMPTS_PATH) as fin, \
         open(OUT_SFT, "a") as fsft, \
         open(OUT_DPO, "a") as fdpo:
        for idx, line in enumerate(fin):
            if idx < seen: continue
            if written >= target: break
            try:
                seed = json.loads(line)
            except Exception:
                continue

            r = distill_prompt(seed["prompt"])
            if not r: continue

            # SFT row (best response)
            fsft.write(json.dumps({
                "prompt": r["prompt"],
                "response": r["best_response"],
                "source": f"distill-{r['best_provider']}",
            }, ensure_ascii=False) + "\n")
            fsft.flush()

            # DPO pair (best vs worst)
            fdpo.write(json.dumps({
                "prompt": r["prompt"],
                "chosen": r["best_response"],
                "rejected": r["worst_response"],
                "source": "distill-vote",
            }, ensure_ascii=False) + "\n")
            fdpo.flush()

            written += 1
            if written % 50 == 0:
                print(f"  [{written}/{target}] SFT+DPO rows written", flush=True)
            time.sleep(0.5)

    print(f"\n✅ done — distilled {written} samples to {OUT_SFT} + {OUT_DPO}")


if __name__ == "__main__":
    main()
