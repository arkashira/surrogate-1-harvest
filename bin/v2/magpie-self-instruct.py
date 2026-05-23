"""Magpie self-instruct (ICLR 2025) — generate 1M training instructions for FREE.

Method: prompt aligned LLM with ONLY chat template (no actual user prompt).
Auto-regressive nature → model fills in user query first, then assistant response.
Zero API cost beyond compute. Used to create 4M Llama-3 instructions in paper.

For Surrogate-1 v2 we run on Qwen2.5-Coder-32B-Instruct (or 14B) via:
- Local inference if we have GPU
- HF Inference API (free tier rate-limited)
- Cerebras / Groq / OpenRouter free if available

Output: ~/.surrogate/data/v2-magpie-synth.jsonl (target 1M after dedup)

Reference: https://github.com/magpie-align/magpie
"""
import os, json, time, sys, random, re
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
from sanitize import filter_pair

# Choose target generator model — must be ALIGNED (instruct/chat-tuned)
MODEL = os.environ.get("MAGPIE_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")
TARGET_N = int(os.environ.get("MAGPIE_TARGET", "100000"))   # start with 100K, scale to 1M
OUT_PATH = Path.home() / ".surrogate/data/v2-magpie-synth.jsonl"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# Domain-conditioned templates — bias toward what Surrogate-1 v2 needs
# By varying the system prompt we steer Magpie toward different domains.
DOMAIN_SYSTEM_PROMPTS = [
    # Code
    "You are a senior Python engineer who writes production-grade, well-tested code.",
    "You are a senior TypeScript developer building React + Next.js apps.",
    "You are a senior Go engineer building cloud-native microservices.",
    "You are a Rust expert focused on performance + memory safety.",
    "You are a senior C++ developer working on high-performance systems.",
    # DevOps / Cloud
    "You are a senior DevOps engineer who writes Terraform, Helm, and Kubernetes manifests.",
    "You are an AWS Solutions Architect designing multi-region production workloads.",
    "You are an SRE who writes Prometheus alerting rules and runbooks.",
    "You are a Kubernetes platform engineer building GitOps with ArgoCD + Karpenter.",
    "You are a FinOps practitioner optimizing cloud costs.",
    # Security
    "You are a senior DevSecOps engineer writing Sigma detection rules + IaC security audits.",
    "You are a SOC analyst tier-2 investigating security alerts.",
    "You are a compliance engineer mapping controls between SOC2/ISO27001/HIPAA/GDPR.",
    "You are a penetration tester (defensive security focus).",
    "You are a threat hunter identifying advanced persistent threats.",
    # AI / ML
    "You are an AI engineer building production RAG pipelines.",
    "You are an MLOps engineer setting up training/serving infrastructure.",
    "You are a senior LLM engineer fine-tuning and deploying open models.",
    # Product / Business
    "You are a senior product manager writing PRDs and prioritizing roadmaps.",
    "You are a startup founder validating market and writing pitch decks.",
    "You are a growth marketer designing user acquisition funnels.",
    "You are a customer success engineer handling tier-2 support tickets.",
]


def call_local_vllm(model: str, system: str, max_tokens: int = 600) -> str | None:
    """Call locally-hosted vLLM with ONLY system + assistant template prefix.

    Magpie trick: don't include user message. Model auto-completes user → assistant.
    """
    import requests
    # Construct chat template with empty user slot — Qwen format:
    # <|im_start|>system\n{sys}<|im_end|>\n<|im_start|>user\n
    # The model will complete the user message + transition to assistant.
    prompt = (f"<|im_start|>system\n{system}<|im_end|>\n"
              f"<|im_start|>user\n")
    try:
        r = requests.post("http://localhost:8000/v1/completions",
            json={"model": model, "prompt": prompt, "max_tokens": max_tokens,
                  "temperature": 1.0, "top_p": 0.95,
                  "stop": ["<|im_end|>"]},
            timeout=60)
        return r.json().get("choices", [{}])[0].get("text", "").strip()
    except Exception as e:
        print(f"  vllm err: {e}", flush=True)
        return None


def call_via_bridge(provider: str, model: str, system: str, max_tokens: int = 600) -> str | None:
    """Fallback: use existing free LLM bridges. Less true-Magpie but still works."""
    import subprocess
    bridge = {
        "cerebras": str(Path.home() / ".surrogate/bin/cerebras-bridge.sh"),
        "groq": str(Path.home() / ".surrogate/bin/groq-bridge.sh"),
        "openrouter": str(Path.home() / ".surrogate/bin/openrouter-bridge.sh"),
        "gemini": str(Path.home() / ".surrogate/bin/gemini-bridge.sh"),
    }.get(provider)
    if not bridge or not Path(bridge).exists():
        return None
    # Pseudo-Magpie: ask the model to GENERATE a user query in the domain, then answer it
    prompt = (f"Generate a realistic user question that fits this persona, "
              f"then answer it as that persona.\n\nPersona: {system}\n\n"
              f"Format strictly:\nUSER: <one realistic question>\nASSISTANT: <thorough answer>")
    payload = json.dumps({"messages": [{"role": "user", "content": prompt}],
                          "model": model, "max_tokens": max_tokens})
    try:
        r = subprocess.run(["bash", bridge], input=payload, capture_output=True, text=True, timeout=60)
        return r.stdout.strip()
    except Exception as e:
        print(f"  bridge err: {e}", flush=True)
        return None


def parse_magpie_output(text: str) -> tuple[str | None, str | None]:
    """Extract user instruction + assistant response from Magpie output."""
    # Try Qwen-format completion: starts with user message text, then <|im_end|>, then assistant
    m = re.match(r"(.*?)<\|im_end\|>\s*<\|im_start\|>assistant\s*\n(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Try bridge format USER: ... ASSISTANT: ...
    m = re.match(r"USER:\s*(.*?)\s*\nASSISTANT:\s*(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def main():
    # Resume if file exists
    seen = 0
    if OUT_PATH.exists():
        with open(OUT_PATH) as f:
            seen = sum(1 for _ in f)
    print(f"resume from {seen} existing samples; target={TARGET_N}", flush=True)

    # Try local vLLM first (preferred — true Magpie)
    USE_LOCAL = bool(os.environ.get("USE_LOCAL_VLLM"))
    use_provider = "cerebras"  # for bridge fallback
    use_model = "qwen-3-235b-a22b-instruct-2507"

    written = 0
    with open(OUT_PATH, "a") as fout:
        for idx in range(seen, TARGET_N):
            sys_prompt = random.choice(DOMAIN_SYSTEM_PROMPTS)
            if USE_LOCAL:
                raw = call_local_vllm(MODEL, sys_prompt, max_tokens=800)
            else:
                raw = call_via_bridge(use_provider, use_model, sys_prompt, max_tokens=800)
            if not raw:
                time.sleep(3); continue

            user_q, asst_r = parse_magpie_output(raw)
            if not user_q or not asst_r:
                continue

            # Sanitize via existing filter
            v = filter_pair(user_q, asst_r)
            if not v["keep"]:
                continue

            fout.write(json.dumps({
                "prompt": user_q[:100000],
                "response": asst_r[:200000],
                "source": f"magpie-{use_model}",
                "domain_persona": sys_prompt,
                "ts": datetime.utcnow().isoformat(),
            }, ensure_ascii=False) + "\n")
            fout.flush()
            written += 1
            if written % 50 == 0:
                print(f"  [{written}/{TARGET_N - seen}] kept", flush=True)
            time.sleep(0.5)   # stay under free-tier RPM
    print(f"\n✅ done — wrote {written} new Magpie samples to {OUT_PATH}")


if __name__ == "__main__":
    main()
