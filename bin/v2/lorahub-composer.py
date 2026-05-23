"""Surrogate-1 v2 — LoraHub / Arrow runtime LoRA composition.

Reference: LoraHub (Huang et al. 2023) + Arrow (2024) — at inference time,
compose multiple specialist LoRAs with task-aware weights instead of using
a single statically-merged super-LoRA.

Why: at inference, the user's prompt rarely needs ALL 9 cluster LoRAs at
equal strength. A devops question ⇒ 0.55 eng-ops + 0.30 eng-sec + 0.15
meta. A code question ⇒ 0.60 eng-build + 0.25 eng-ai + 0.15 meta.

This module:
  1. Classifies the prompt domain via a small Qwen-Coder-1.5B prompt
     (fast, free) OR keyword heuristics (instant fallback).
  2. Returns per-LoRA weights via a learned table OR sane defaults.
  3. Emits a vLLM `--lora-modules` compatible weight string OR
     PEFT `add_weighted_adapter()` call args.

Routing table is bootstrapped from heuristics + improved over time using
self-improve-loop's winner data — same closed loop as the rest of v2.

CLI:
  echo '{"prompt":"Write a Terraform module..."}' | python3 lorahub-composer.py
  → {"weights": {"eng-build":0.10, "eng-ops":0.55, ...}, "domain":"devops-tf"}

  python3 lorahub-composer.py --learn winners.jsonl   # update routing weights
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / ".surrogate/state/lorahub.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 9 cluster LoRAs (must match merge-9-loras.sh + serve-vllm.sh USE_MULTI_LORA)
LORAS = [
    "eng-build", "eng-ops", "eng-sec", "eng-ai",
    "product-ux", "gtm", "finance-legal", "compliance",
    "meta-orchestrator",
]

# Heuristic routing — domain → adapter weights summing to ~1.0
# meta-orchestrator always gets a small slice (it's the planner)
ROUTING_HEURISTIC: dict[str, dict[str, float]] = {
    "code-python": {
        "eng-build": 0.55, "eng-ai": 0.20, "eng-sec": 0.10,
        "meta-orchestrator": 0.15},
    "code-typescript": {
        "eng-build": 0.55, "eng-ai": 0.15, "product-ux": 0.15,
        "meta-orchestrator": 0.15},
    "devops-tf": {
        "eng-ops": 0.50, "eng-sec": 0.25, "eng-build": 0.10,
        "meta-orchestrator": 0.15},
    "devops-k8s": {
        "eng-ops": 0.55, "eng-sec": 0.20, "eng-build": 0.10,
        "meta-orchestrator": 0.15},
    "devops-cdk": {
        "eng-ops": 0.45, "eng-build": 0.20, "eng-sec": 0.20,
        "meta-orchestrator": 0.15},
    "sec-iam": {
        "eng-sec": 0.55, "eng-ops": 0.20, "compliance": 0.10,
        "meta-orchestrator": 0.15},
    "sec-secrets": {
        "eng-sec": 0.55, "eng-ops": 0.15, "compliance": 0.15,
        "meta-orchestrator": 0.15},
    "sec-cve": {
        "eng-sec": 0.50, "compliance": 0.20, "eng-ops": 0.15,
        "meta-orchestrator": 0.15},
    "sre-runbook": {
        "eng-ops": 0.55, "eng-sec": 0.15, "meta-orchestrator": 0.30},
    "sre-slo": {
        "eng-ops": 0.50, "eng-ai": 0.15, "meta-orchestrator": 0.35},
    "data-sql": {
        "eng-build": 0.55, "eng-ai": 0.15, "compliance": 0.10,
        "meta-orchestrator": 0.20},
    "ai-eng": {
        "eng-ai": 0.60, "eng-build": 0.20, "meta-orchestrator": 0.20},
    "ai-prompt": {
        "eng-ai": 0.55, "product-ux": 0.20, "meta-orchestrator": 0.25},
    "api-rest": {
        "eng-build": 0.45, "product-ux": 0.20, "eng-ai": 0.15,
        "meta-orchestrator": 0.20},
    "api-graphql": {
        "eng-build": 0.50, "product-ux": 0.15, "eng-ai": 0.15,
        "meta-orchestrator": 0.20},
    "ci-github": {
        "eng-ops": 0.55, "eng-build": 0.20, "eng-sec": 0.10,
        "meta-orchestrator": 0.15},
    "debug-traceback": {
        "eng-build": 0.55, "eng-ai": 0.15, "meta-orchestrator": 0.30},
    "perf-profile": {
        "eng-build": 0.45, "eng-ops": 0.20, "eng-ai": 0.15,
        "meta-orchestrator": 0.20},
    "test-pytest": {
        "eng-build": 0.55, "eng-ai": 0.15, "meta-orchestrator": 0.30},
    "docs-api": {
        "eng-build": 0.30, "product-ux": 0.30, "meta-orchestrator": 0.40},
    "arch-adr": {
        "meta-orchestrator": 0.55, "eng-build": 0.15, "eng-ai": 0.15,
        "product-ux": 0.15},
    "cloud-cost": {
        "eng-ops": 0.40, "finance-legal": 0.30, "meta-orchestrator": 0.30},
    "business": {
        "gtm": 0.45, "finance-legal": 0.30, "meta-orchestrator": 0.25},
    "compliance": {
        "compliance": 0.55, "eng-sec": 0.20, "finance-legal": 0.10,
        "meta-orchestrator": 0.15},
    "_default": {
        "meta-orchestrator": 0.40, "eng-build": 0.20, "eng-ops": 0.15,
        "eng-sec": 0.10, "eng-ai": 0.15},
}

# Domain heuristic copied from inference-augment.py
DOMAIN_HINTS = {
    "code-python":      ["def ", "import ", "python", ".py", "pytest", "asyncio"],
    "code-typescript":  ["typescript", ".ts", "interface ", "tsconfig"],
    "devops-tf":        ["terraform", "resource \"", "provider \"", ".tf"],
    "devops-k8s":       ["kubernetes", "kubectl", "kind: deployment", "helm"],
    "devops-cdk":       ["aws-cdk", "cdk synth", "Stack", "CfnOutput"],
    "sec-iam":          ["iam:", "policy", "principal", "least privilege"],
    "sec-secrets":      ["secret", "api key", "token", "credentials"],
    "sec-cve":          ["cve-", "vulnerability", "exploit", "remediation"],
    "sre-runbook":      ["runbook", "incident", "on-call", "page"],
    "sre-slo":          ["sli", "slo", "error budget", "latency p99"],
    "data-sql":         ["select ", "from ", "join ", "create table"],
    "ai-eng":           ["embedding", "rag", "vector", "lora", "vllm"],
    "ai-prompt":        ["system prompt", "few-shot", "in-context"],
    "api-rest":         ["rest api", "openapi", "endpoint", "GET /", "POST /"],
    "api-graphql":      ["graphql", "resolver", "type Query", "schema"],
    "ci-github":        ["github actions", ".github/workflows", "uses: actions/"],
    "debug-traceback":  ["traceback", "stack trace", "valueerror", "typeerror"],
    "perf-profile":     ["profile", "bottleneck", "latency", "throughput"],
    "test-pytest":      ["pytest", "@pytest.fixture", "assert ", "unittest"],
    "docs-api":         ["api documentation", "endpoint reference", "sdk"],
    "arch-adr":         ["adr", "trade-off", "decision record", "architecture"],
    "cloud-cost":       ["cost", "spend", "savings plan", "reserved instance"],
    "business":         ["pricing", "go-to-market", "positioning", "icp"],
    "compliance":       ["soc 2", "iso 27001", "hipaa", "pci-dss", "gdpr"],
}


def detect_domain(prompt: str) -> str:
    p = prompt.lower()
    best, best_n = "_default", 0
    for dom, kws in DOMAIN_HINTS.items():
        n = sum(1 for k in kws if k in p)
        if n > best_n:
            best, best_n = dom, n
    return best if best_n >= 2 else "_default"


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10,
                        check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS routing (
        domain TEXT,
        adapter TEXT,
        weight REAL,
        n_observations INTEGER DEFAULT 0,
        PRIMARY KEY (domain, adapter)
    )""")
    return c


def get_weights(domain: str) -> dict[str, float]:
    """Lookup learned weights, fall back to heuristic."""
    c = _db()
    rows = c.execute("""SELECT adapter, weight FROM routing
                        WHERE domain=? AND n_observations >= 5""",
                     (domain,)).fetchall()
    c.close()
    if rows:
        w = {a: weight for a, weight in rows}
    else:
        w = dict(ROUTING_HEURISTIC.get(domain, ROUTING_HEURISTIC["_default"]))
    # Normalize to sum 1.0
    s = sum(w.values()) or 1.0
    return {a: round(v / s, 4) for a, v in w.items()}


def compose(prompt: str, override_domain: str | None = None) -> dict:
    domain = override_domain or detect_domain(prompt)
    weights = get_weights(domain)
    # vLLM compatible serialization (passes via --lora-modules with weights)
    vllm_arg = ",".join(f"{a}={w}" for a, w in weights.items())
    return {
        "prompt": prompt[:200] + ("…" if len(prompt) > 200 else ""),
        "domain": domain,
        "weights": weights,
        "vllm_lora_modules": vllm_arg,
        "peft_args": [{"adapter_name": a, "weight": w}
                       for a, w in weights.items()],
    }


def learn_from_winners(jsonl_path: str, lr: float = 0.1) -> int:
    """Update routing table from self-improve winners.
    Each winner is treated as evidence that its detected domain → ADAPTER
    weights worked. We bump observed adapters' weights toward what the
    winning examples used (or, lacking adapter signal, just count domain
    occurrences to confirm the heuristic).
    """
    inp = Path(jsonl_path)
    if not inp.exists():
        return 0
    c = _db()
    n = 0
    for line in inp.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        prompt = d.get("prompt", "")
        if not prompt:
            continue
        # If logger captured which adapter served best, use that.
        used = d.get("meta", {}).get("adapter") or d.get("adapter")
        domain = d.get("meta", {}).get("domain") or detect_domain(prompt)
        if used:
            cur = c.execute("SELECT weight, n_observations FROM routing "
                            "WHERE domain=? AND adapter=?",
                            (domain, used)).fetchone()
            if cur:
                w, obs = cur
                w_new = w * (1 - lr) + 1.0 * lr
                c.execute("""UPDATE routing SET weight=?, n_observations=?
                             WHERE domain=? AND adapter=?""",
                          (w_new, obs + 1, domain, used))
            else:
                c.execute("""INSERT INTO routing
                             (domain, adapter, weight, n_observations)
                             VALUES (?, ?, ?, 1)""",
                          (domain, used, lr))
        else:
            # Bump heuristic adapters' observation counts (confidence signal)
            for adapter, w in ROUTING_HEURISTIC.get(domain, {}).items():
                cur = c.execute("SELECT 1 FROM routing WHERE domain=? "
                                "AND adapter=?", (domain, adapter)).fetchone()
                if cur:
                    c.execute("""UPDATE routing SET n_observations=
                                 n_observations + 1
                                 WHERE domain=? AND adapter=?""",
                              (domain, adapter))
                else:
                    c.execute("""INSERT INTO routing
                                 (domain, adapter, weight, n_observations)
                                 VALUES (?, ?, ?, 1)""",
                              (domain, adapter, w))
        n += 1
    c.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--learn", help="JSONL of winners to learn routing from")
    ap.add_argument("--domain", help="override detected domain")
    args = ap.parse_args()

    if args.learn:
        n = learn_from_winners(args.learn)
        print(json.dumps({"learned_from": n, "db": str(DB_PATH)}))
        return

    if sys.stdin.isatty():
        sample = "Write a Terraform module that provisions an S3 bucket with versioning and KMS encryption."
        print(json.dumps(compose(sample, args.domain), indent=2,
                         ensure_ascii=False))
        return
    d = json.load(sys.stdin)
    print(json.dumps(compose(d.get("prompt", ""), args.domain),
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
