"""Surrogate-1 v2 — Inference-time prompt augmentation.

Glues reflexion-store + voyager-skills into the serving prompt so the
model gets free in-context lessons + validated snippets without retraining.

Used as a sidecar by serve-vllm.sh: every incoming prompt is passed
through `augment(prompt, domain)` before being sent to vLLM.

Adds (under explicit headers, easy to strip):
  ## Past lessons (top-3 similar)
  ## Validated skills (top-3 by tag)

If neither store has hits, returns prompt unchanged.
"""
from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path

V2_DIR = Path.home() / ".surrogate/bin/v2"


def _load(name: str):
    p = V2_DIR / f"{name}.py"
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  str(p))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore
        return mod
    except Exception:
        return None


_REFLEX = _load("reflexion-store")
_VOYAGER = _load("voyager-skills")


# Hermes-3 reserved tokens (2026 spec, github.com/NousResearch/Hermes-Function-Calling)
# Bake into training-time templates AND inference-time prompts so the model
# learns to use them implicitly.
HERMES3_TOKENS = {
    "tools_open":     "<tools>",
    "tools_close":    "</tools>",
    "tool_call_open": "<tool_call>",
    "tool_call_close": "</tool_call>",
    "tool_resp_open": "<tool_response>",
    "tool_resp_close": "</tool_response>",
    "scratchpad":     "<SCRATCHPAD>",
    "scratchpad_end": "</SCRATCHPAD>",
    "plan":           "<PLAN>",
    "plan_end":       "</PLAN>",
    "reflection":     "<REFLECTION>",
    "reflection_end": "</REFLECTION>",
}


def build_hermes3_system_prompt(tool_schemas: list[dict] | None = None) -> str:
    """Render a Hermes-3 system prompt block (compatible with vLLM tool parser)."""
    parts = [
        "You are Surrogate-1, an expert DevSecOps + SRE + coding agent.",
        "When you need to think before acting, use <SCRATCHPAD>...</SCRATCHPAD>.",
        "When you draft a multi-step plan, use <PLAN>...</PLAN>.",
        "When you reflect on what worked or failed, use <REFLECTION>...</REFLECTION>.",
    ]
    if tool_schemas:
        parts.append("\nYou have access to the following tools:")
        parts.append("<tools>")
        for s in tool_schemas:
            parts.append(json.dumps(s, ensure_ascii=False))
        parts.append("</tools>")
        parts.append(
            "Invoke a tool with: "
            "<tool_call>{\"name\": \"<tool>\", \"arguments\": {...}}</tool_call>")
    return "\n".join(parts)


# Domain heuristic — keyword-only, fast, no LLM call.
DOMAIN_HINTS = {
    "code-python":   ["def ", "import ", "python", ".py", "pytest", "asyncio"],
    "code-typescript": ["typescript", ".ts", "interface ", "tsconfig", "node_modules"],
    "devops-tf":     ["terraform", "resource \"", "provider \"", "tf state", ".tf"],
    "devops-k8s":    ["kubernetes", "kubectl", "kind: deployment", "kind: service",
                      "namespace", "helm"],
    "devops-cdk":    ["aws-cdk", "cdk synth", "Stack", "CfnOutput"],
    "sec-iam":       ["iam:", "policy", "principal", "assume role", "least privilege"],
    "sec-secrets":   ["secret", "api key", "token", "password", "credentials"],
    "sec-cve":       ["cve-", "vulnerability", "exploit", "patch", "remediation"],
    "sre-runbook":   ["runbook", "incident", "on-call", "page", "escalation"],
    "sre-slo":       ["sli", "slo", "error budget", "latency p99", "availability"],
    "data-sql":      ["select ", "from ", "join ", "where ", "create table"],
    "ai-eng":        ["embedding", "rag", "vector", "lora", "fine-tune", "vllm"],
    "ci-github":     ["github actions", ".github/workflows", "uses: actions/", "runs-on:"],
}


def detect_domain(prompt: str) -> str | None:
    p = prompt.lower()
    best, best_n = None, 0
    for dom, kws in DOMAIN_HINTS.items():
        n = sum(1 for k in kws if k in p)
        if n > best_n:
            best, best_n = dom, n
    return best if best_n >= 2 else None


def augment(prompt: str, domain: str | None = None,
            k_lessons: int = 3, k_skills: int = 3,
            max_each_chars: int = 600) -> str:
    """Return prompt with prepended lesson/skill context. Idempotent if no hits."""
    domain = domain or detect_domain(prompt)
    parts: list[str] = []

    if _REFLEX is not None:
        try:
            lessons = _REFLEX.retrieve_similar(prompt, domain, k=k_lessons)
        except Exception:
            lessons = []
        if lessons:
            block = ["## Past lessons (do NOT repeat these mistakes)"]
            for i, l in enumerate(lessons, 1):
                err = (l.get("error") or "")[:max_each_chars]
                ref = (l.get("reflection") or "")[:max_each_chars]
                fix = (l.get("fix") or "")[:max_each_chars]
                block.append(
                    f"{i}. error_signal: {err}\n"
                    f"   lesson: {ref}\n"
                    f"   correct_pattern: {fix}")
            parts.append("\n".join(block))

    if _VOYAGER is not None:
        try:
            tags = [domain.split("-")[0]] if domain else []
            skills = _VOYAGER.search(prompt, tags=tags, limit=k_skills,
                                     only_promoted=True)
        except Exception:
            skills = []
        if skills:
            block = ["## Validated snippets (proven in production)"]
            for s in skills:
                code = (s.get("code") or "")[:max_each_chars]
                desc = (s.get("description") or s.get("name", ""))[:200]
                block.append(f"- {desc}\n```\n{code}\n```")
            parts.append("\n".join(block))

    if not parts:
        return prompt
    return "\n\n".join(parts) + "\n\n## User request\n" + prompt


# CLI: read JSON {prompt, domain?} from stdin, print {prompt: augmented} JSON.
if __name__ == "__main__":
    if sys.stdin.isatty():
        # Demo mode
        demo = ("Write a Terraform module that provisions an S3 bucket "
                "with versioning and KMS encryption.")
        print(augment(demo))
    else:
        try:
            d = json.load(sys.stdin)
        except Exception as e:
            print(json.dumps({"error": f"bad json: {e}"}))
            sys.exit(1)
        out = augment(d.get("prompt", ""), d.get("domain"))
        print(json.dumps({"prompt": out}, ensure_ascii=False))
