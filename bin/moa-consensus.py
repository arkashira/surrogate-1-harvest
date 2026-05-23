#!/usr/bin/env python3
"""Mixture-of-Agents (MoA) consensus — 3 LLMs propose, 1 LLM judges + synthesizes.

Used by orchestrate's `--consensus` mode (ENABLE_MOA=1) for critical stages
(DEV implementation, REVIEWER verdict). Trades 4× cost for higher quality.

Usage from bash:
    python3 ~/.surrogate/bin/moa-consensus.py <prompt_file> [stage]
Reads prompt from file, returns synthesized response on stdout.
"""
from __future__ import annotations
import sys, os, json, urllib.request, urllib.error
from pathlib import Path

PROPOSERS = [
    ("cerebras-llama-70b", "https://api.cerebras.ai/v1/chat/completions", "llama-3.3-70b", "CEREBRAS_API_KEY"),
    ("groq-llama-70b", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    ("hf-router-deepseek", "https://router.huggingface.co/v1/chat/completions", "deepseek-ai/DeepSeek-V3.1-Terminus", "HF_TOKEN"),
]
JUDGE = ("hf-router-qwen3-coder-480b", "https://router.huggingface.co/v1/chat/completions",
         "Qwen/Qwen3-Coder-480B-A35B-Instruct", "HF_TOKEN")


def call_oai(url: str, model: str, key: str, prompt: str, temperature: float = 0.4, max_tokens: int = 6000) -> str:
    body = {"model": model, "messages": [{"role":"user","content":prompt}],
            "temperature": temperature, "max_tokens": max_tokens}
    headers = {"Content-Type":"application/json", "Authorization": f"Bearer {key}"}
    if "openrouter" in url or "router.huggingface" in url:
        headers["HTTP-Referer"] = "https://axentx.ai"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: moa-consensus.py <prompt_file> [stage]", file=sys.stderr); return 2
    prompt = Path(sys.argv[1]).read_text()
    stage = sys.argv[2] if len(sys.argv) > 2 else "general"

    # Round 1: 3 proposers in parallel via threading
    import concurrent.futures as cf
    proposals: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {}
        for name, url, model, key_env in PROPOSERS:
            key = os.environ.get(key_env)
            if not key: continue
            futures[ex.submit(call_oai, url, model, key, prompt, 0.5)] = name
        for fut in cf.as_completed(futures, timeout=180):
            name = futures[fut]
            try:
                proposals[name] = fut.result()
                print(f"# {name}: {len(proposals[name])} chars", file=sys.stderr)
            except Exception as e:
                print(f"# {name}: FAIL {type(e).__name__}: {e}", file=sys.stderr)

    if not proposals:
        print("ERR: all proposers failed", file=sys.stderr); return 3
    if len(proposals) == 1:
        # Only one succeeded → just return it
        sys.stdout.write(next(iter(proposals.values())))
        return 0

    # Round 2: judge synthesizes best answer from all proposals
    judge_prompt = f"""You are the SYNTHESIS JUDGE. {len(proposals)} expert agents proposed answers to this task.
Evaluate each, then output a SINGLE final answer that combines the best ideas.
Do NOT just pick one — synthesize across them. Output the answer directly, no preamble.

=== TASK ===
{prompt[:6000]}

"""
    for i, (name, text) in enumerate(proposals.items(), 1):
        judge_prompt += f"\n=== PROPOSAL {i} (from {name}) ===\n{text[:6000]}\n"
    judge_prompt += "\n=== YOUR SYNTHESIZED ANSWER ===\n"

    judge_key = os.environ.get(JUDGE[3])
    if not judge_key:
        # No judge key → return best-effort: longest proposal
        sys.stdout.write(max(proposals.values(), key=len))
        return 0
    try:
        synthesized = call_oai(JUDGE[1], JUDGE[2], judge_key, judge_prompt, 0.3, 8000)
        sys.stdout.write(synthesized)
        print(f"# judge ({JUDGE[0]}): synthesized {len(synthesized)} chars from {len(proposals)} proposals", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"# judge FAIL {type(e).__name__}: {e}", file=sys.stderr)
        # Fallback: longest
        sys.stdout.write(max(proposals.values(), key=len))
        return 0


if __name__ == "__main__":
    sys.exit(main())
