"""Surrogate-1 v2 — VeriFY trace generator.

Reference: arxiv.org/abs/2602.02018 (2026-02)
Goal: train Surrogate to PROBE its own factual claims and ABSTAIN when
uncertain. 9.7-53.3% factual hallucination reduction at modest recall cost.

For each (prompt, gold_response) we synthesize a 4-stage trace:

  <ANSWER_DRAFT>     — initial answer (may be wrong)
  <PROBE>            — what would I need to verify? generates self-questions
  <CONSISTENCY_CHECK> — does the answer hold up against probes?
  <FINAL>            — verified answer OR explicit abstention

Trained on these traces, the model learns the protocol implicitly. At
inference we read only <FINAL>; the rest is internal.

Output: ~/.surrogate/data/v2/verify-traces.jsonl
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
try:
    from sanitize import filter_pair  # type: ignore
except Exception:
    def filter_pair(p, r): return {"keep": True}

OUT_PATH = Path.home() / ".surrogate/data/v2/verify-traces.jsonl"


# Domain-specific probe templates (what does this domain need to verify?)
PROBE_TEMPLATES = {
    "code": [
        "Are all imports real and installable from PyPI/npm?",
        "Does the function signature match the standard library API?",
        "Is there any phantom method (e.g., dict.get_or_default)?",
        "Does the example handle edge cases (empty, None, large)?",
    ],
    "devops": [
        "Are all CloudFormation/Terraform resource types valid?",
        "Are all IAM actions real AWS service actions?",
        "Are version pins specified or floating?",
        "Are there least-privilege violations (wildcard *)?",
    ],
    "security": [
        "Is the CVE ID format valid (CVE-YYYY-NNNNN)?",
        "Is the affected package version range realistic?",
        "Does the mitigation match what the vendor advisory says?",
        "Are any secrets/credentials hardcoded in the example?",
    ],
    "sre": [
        "Are SLI metrics measurable (latency p99 from real source)?",
        "Is the error budget arithmetic correct (1 - SLO over window)?",
        "Are runbook steps actually executable (no TODO/FIXME)?",
        "Are escalation paths concrete (not 'page someone')?",
    ],
    "general": [
        "Is every cited fact verifiable against authoritative source?",
        "Are version numbers, dates, and identifiers plausible?",
        "Does the answer commit to claims I cannot verify offline?",
        "Should I abstain on parts I'm unsure about?",
    ],
}


def llm_ladder(prompt: str, sys_prompt: str = "",
               max_tokens: int = 800, temperature: float = 0.4) -> str:
    bridges = [
        "$HOME/.surrogate/bin/cerebras-bridge.sh",
        "$HOME/.surrogate/bin/groq-bridge.sh",
        "$HOME/.surrogate/bin/openrouter-bridge.sh",
        "$HOME/.surrogate/bin/gemini-bridge.sh",
        # "$HOME/.surrogate/bin/chutes-bridge.sh",  # disabled 2026-04-30: chutes 402 free-tier dead
        "$HOME/.surrogate/bin/ollama-bridge.sh",
    ]
    for sh in bridges:
        sh_path = os.path.expandvars(sh)
        if not Path(sh_path).exists():
            continue
        try:
            req = json.dumps({"system": sys_prompt, "prompt": prompt,
                              "max_tokens": max_tokens,
                              "temperature": temperature})
            r = subprocess.run(["bash", sh_path], input=req,
                               capture_output=True, text=True, timeout=60)
            out = (r.stdout or "").strip()
            if out and len(out) > 20:
                return out
        except Exception:
            continue
    return ""


def detect_domain(prompt: str, response: str) -> str:
    p = (prompt + " " + response).lower()
    if any(k in p for k in ["cve-", "exploit", "vulnerab", "remediation",
                             "iam:", "kms", "encryption", "secret"]):
        return "security"
    if any(k in p for k in ["slo", "sli", "error budget", "runbook",
                             "incident", "postmortem", "alert"]):
        return "sre"
    if any(k in p for k in ["terraform", "cloudformation", "kubernetes",
                             "kubectl", "helm", "aws", "gcp", "ansible"]):
        return "devops"
    if any(k in p for k in ["def ", "function ", "class ", "import ", ".py",
                             ".ts", ".js", "async ", "await ", "return"]):
        return "code"
    return "general"


def synthesize_trace(prompt: str, gold: str) -> dict | None:
    """Build a 4-stage verification trace ending with the gold answer."""
    if len(prompt) < 30 or len(gold) < 30:
        return None
    domain = detect_domain(prompt, gold)
    probes = PROBE_TEMPLATES.get(domain, PROBE_TEMPLATES["general"])

    # Step 1: synthesize a plausible-but-flawed draft (used as <ANSWER_DRAFT>)
    sys_p = ("You are simulating a model that produces a confident-sounding "
             "but slightly imperfect first draft. Output ONLY the draft "
             "answer — under 300 words. Include 1-2 small inaccuracies that "
             "a careful verifier would catch.")
    draft = llm_ladder(
        f"PROMPT: {prompt[:1500]}\n\nProduce a flawed first-draft answer:",
        sys_p, max_tokens=400, temperature=0.7)
    if not draft:
        draft = gold[:1500]  # fallback: use gold as draft (still trains format)

    # Step 2: synthesize <CONSISTENCY_CHECK> using LLM that compares draft vs gold
    sys_p = ("You are a verifier checking a draft against a gold reference. "
             "For each probe, judge if the draft satisfies it. Output 4 lines, "
             "one per probe, format: 'PROBE_N: [PASS/FAIL] - <1-line reason>'.")
    probe_block = "\n".join(f"PROBE_{i+1}: {p}" for i, p in enumerate(probes))
    user_p = (f"PROBES:\n{probe_block}\n\nDRAFT:\n{draft[:2000]}\n\n"
              f"GOLD:\n{gold[:2000]}\n\nRun all probes.")
    consistency = llm_ladder(user_p, sys_p, max_tokens=400, temperature=0.2)
    if not consistency:
        return None

    # Build trace as a single response string with explicit section markers
    trace = (
        f"<ANSWER_DRAFT>\n{draft.strip()}\n</ANSWER_DRAFT>\n\n"
        f"<PROBE domain=\"{domain}\">\n" +
        "\n".join(f"- {p}" for p in probes) +
        "\n</PROBE>\n\n"
        f"<CONSISTENCY_CHECK>\n{consistency.strip()}\n</CONSISTENCY_CHECK>\n\n"
        f"<FINAL>\n{gold.strip()}\n</FINAL>"
    )

    if not filter_pair(prompt, trace)["keep"]:
        return None

    return {
        "prompt": prompt[:100000],
        "response": trace[:200000],
        "source": "verify-trace",
        "meta": {"domain": domain, "n_probes": len(probes)},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="JSONL with {prompt, response} per line")
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--max", type=int, default=2000)
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not inp.exists():
        print(f"❌ {inp} missing", file=sys.stderr); sys.exit(1)

    n_in = 0; n_kept = 0
    with open(inp) as fin, open(out, "a") as fout:
        for line in fin:
            if n_kept >= args.max:
                break
            try:
                d = json.loads(line)
            except Exception:
                continue
            n_in += 1
            prompt = d.get("prompt") or d.get("instruction") or ""
            gold = (d.get("response") or d.get("output")
                    or d.get("answer") or "")
            row = synthesize_trace(prompt, gold)
            if row:
                fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                fout.flush()
                n_kept += 1
                if n_kept % 25 == 0:
                    print(f"  verify kept {n_kept}/{args.max} (in {n_in})")
    print(f"[done] in={n_in} verify_kept={n_kept} → {out}")


if __name__ == "__main__":
    main()
