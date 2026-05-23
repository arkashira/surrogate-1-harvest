"""Surrogate-1 v2 — Constitutional self-critique → DPO data generator.

Implements Bai et al. 2022 (Constitutional AI) but specialized for
DevSecOps/SRE/code agents. For each input prompt:

  1. Surrogate generates a response.
  2. Self-critique against project-specific principles.
  3. Revise if any principle flagged.
  4. Output (original = rejected, revised = chosen) → DPO pair.

Used as nightly batch. Output appended to:
  ~/.surrogate/data/v2/constitutional-dpo.jsonl

Run:
  python3 constitutional-loop.py --input prompts.jsonl --n 200
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
    def filter_pair(p, r):  # fallback
        return {"keep": True, "reason": "no-sanitizer"}


PRINCIPLES = [
    {
        "name": "no_phantom_imports",
        "check": ("Does the response import only real, installable packages? "
                  "Flag any phantom modules, hallucinated APIs, or fictional "
                  "library functions."),
        "domain": "code",
    },
    {
        "name": "no_hardcoded_secrets",
        "check": ("Does the response contain hardcoded credentials, API keys, "
                  "tokens, passwords, or connection strings? Flag any leaked "
                  "secrets or examples that look real."),
        "domain": "security",
    },
    {
        "name": "least_privilege",
        "check": ("If IAM/RBAC/permissions are involved, does the response "
                  "follow least-privilege? Flag wildcards (* on Resource or "
                  "Action), admin roles attached to functions, public S3 "
                  "buckets without justification."),
        "domain": "security",
    },
    {
        "name": "input_validation",
        "check": ("If the response handles user input or external data, does "
                  "it validate/sanitize? Flag SQL/command/HTML injection "
                  "vectors, missing parameterized queries, or trusting "
                  "untrusted input."),
        "domain": "security",
    },
    {
        "name": "honest_uncertainty",
        "check": ("If the question requires data the model can't have "
                  "(versioned APIs, internal systems, future events), does "
                  "the response say 'I don't know' or 'verify against docs', "
                  "OR does it confabulate a confident-sounding wrong answer?"),
        "domain": "general",
    },
    {
        "name": "no_internal_path_leak",
        "check": ("Does the response leak internal paths, training-data "
                  "artifacts, or filesystem structures from training? Flag "
                  "/home/hermes/, /data/state/, axentx/ repo IDs, daemon "
                  "names, or 'generated via cerebras:' style headers."),
        "domain": "general",
    },
    {
        "name": "production_ready",
        "check": ("Does the response include error handling, logging, and "
                  "graceful failure? Flag bare exceptions, missing retries on "
                  "external calls, missing timeouts, or 'TODO'/'FIXME' "
                  "placeholders left in shipped code."),
        "domain": "code",
    },
    {
        "name": "specific_to_stack",
        "check": ("Is the answer specific to the user's stack/tooling/version "
                  "or is it generic boilerplate? Flag answers that ignore "
                  "stated tools (e.g., user said Terraform, response uses "
                  "CloudFormation; user said Python 3.12, response uses 2.x)."),
        "domain": "general",
    },
]


def llm_ladder(prompt: str, sys_prompt: str = "",
                max_tokens: int = 1024) -> str:
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
                              "max_tokens": max_tokens, "temperature": 0.3})
            r = subprocess.run(["bash", sh_path], input=req,
                               capture_output=True, text=True, timeout=60)
            out = (r.stdout or "").strip()
            if out and len(out) > 30:
                return out
        except Exception:
            continue
    return ""


def critique(prompt: str, response: str) -> dict:
    """Run all principles. Returns {flags: [name], details: {name: text}}."""
    sys_p = ("You are a security and quality reviewer. For EACH principle, "
             "answer YES (satisfied) or NO (violated) and give a 1-sentence "
             "reason. Return ONLY JSON: {\"<name>\": {\"ok\": bool, "
             "\"why\": str}, ...}.")
    p_block = "\n".join(f"- {p['name']}: {p['check']}" for p in PRINCIPLES)
    user_p = (f"PROMPT:\n{prompt[:1500]}\n\nRESPONSE:\n{response[:3000]}\n\n"
              f"PRINCIPLES:\n{p_block}\n\nReturn JSON only.")
    raw = llm_ladder(user_p, sys_p, max_tokens=600)
    try:
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("```")[1].lstrip("json").strip()
        verdict = json.loads(s)
        flags = [k for k, v in verdict.items()
                 if isinstance(v, dict) and v.get("ok") is False]
        return {"flags": flags, "details": verdict}
    except Exception:
        return {"flags": [], "details": {"_parse_error": raw[:300]}}


def revise(prompt: str, response: str, flags: list[str],
           details: dict) -> str:
    if not flags:
        return response
    weaknesses = []
    for fl in flags:
        d = details.get(fl, {})
        weaknesses.append(f"- {fl}: {d.get('why', 'flagged')}")
    sys_p = ("You are Surrogate-1. Revise the response to fix all listed "
             "principle violations. Keep what was correct. Output only the "
             "revised response — no preamble.")
    user_p = (f"PROMPT:\n{prompt[:1500]}\n\nORIGINAL:\n{response[:3000]}\n\n"
              f"VIOLATIONS:\n" + "\n".join(weaknesses) +
              "\n\nFix all and output revised response.")
    return llm_ladder(user_p, sys_p, max_tokens=1500) or response


def process_prompt(prompt: str, response: str | None = None) -> dict | None:
    """Returns DPO triple if revision improved, else None."""
    if not response:
        response = llm_ladder(
            prompt, "You are Surrogate-1, an expert coding/devops agent.",
            max_tokens=1024)
    if not response:
        return None
    crit = critique(prompt, response)
    if not crit["flags"]:
        return None
    revised = revise(prompt, response, crit["flags"], crit["details"])
    if not revised or revised.strip() == response.strip():
        return None
    if not filter_pair(prompt, revised)["keep"]:
        return None
    return {
        "prompt": prompt,
        "chosen": revised,
        "rejected": response,
        "violated": crit["flags"],
        "details": crit["details"],
        "ts": int(time.time()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="JSONL with {prompt, response?} per line")
    ap.add_argument("--out", default=str(
        Path.home() / ".surrogate/data/v2/constitutional-dpo.jsonl"))
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    inp = Path(args.input)
    if not inp.exists():
        print(f"❌ input not found: {inp}", file=sys.stderr)
        sys.exit(1)

    n_in = 0
    n_kept = 0
    with open(inp) as fin, open(out_path, "a") as fout:
        for line in fin:
            if n_kept >= args.n:
                break
            try:
                d = json.loads(line)
            except Exception:
                continue
            n_in += 1
            triple = process_prompt(d.get("prompt", ""), d.get("response"))
            if triple:
                fout.write(json.dumps(triple, ensure_ascii=False) + "\n")
                fout.flush()
                n_kept += 1
                if n_kept % 10 == 0:
                    print(f"  kept {n_kept}/{args.n} (scanned {n_in})")
    print(f"[done] in={n_in} dpo_pairs={n_kept} out={out_path}")


if __name__ == "__main__":
    main()
