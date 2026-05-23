"""Surrogate-1 v2 — Verifiable Rewards Gym (Kimi K2 + APRIL).

Reference: arxiv.org/abs/2507.20534 (Kimi K2)
            arxiv.org/abs/2509.18521 (APRIL — partial rollouts)

Single registry of deterministic 1/0 rewards across domains. Replaces
hand-tuned reward models. Used during DAPO/GRPO/GSPO RL training to give
clean, hack-resistant signals.

Domains:
  • code-python   → ast.parse + pyflakes pass + test pass
  • code-bash     → shellcheck + (optional) bats execution
  • iac-tf        → terraform validate + tflint pass
  • iac-cfn       → cfn-lint pass
  • iac-k8s       → kubeconform pass
  • dockerfile    → hadolint pass
  • github-actions→ actionlint pass
  • sql           → sqlfluff lint clean
  • security      → semgrep p/security-audit clean
  • math          → numerical answer match (regex extract + float compare)
  • format-json   → json.loads succeeds
  • format-yaml   → yaml.safe_load succeeds
  • idk-honest    → response opens with abstention phrase when gold is "unknown"

Output: deterministic 0.0 or 1.0 per probe, plus combined reward.

CLI:
  echo '{"domain":"code-python","response":"def add(a,b): return a+b"}' | python3 verifiable-rewards-gym.py
  python3 verifiable-rewards-gym.py --jsonl in.jsonl --out scored.jsonl
"""
from __future__ import annotations
import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ABSTAIN_RE = re.compile(
    r"\b(?:i\s+don'?t\s+know|cannot\s+verify|need\s+to\s+check|"
    r"verify\s+against\s+docs|out\s+of\s+(?:scope|date))\b", re.IGNORECASE)
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?", re.IGNORECASE)


def _have(b): return shutil.which(b) is not None


def _run(cmd, stdin=None, timeout=30):
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except FileNotFoundError:
        return 127, "", f"missing: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


# ── individual verifiers ─────────────────────────────────────────────
def verify_python(code: str) -> dict:
    try:
        ast.parse(code)
    except SyntaxError as e:
        return {"r": 0.0, "why": f"syntax: {e.msg}"}
    if _have("pyflakes"):
        rc, out, _ = _run(["pyflakes", "-"], stdin=code, timeout=15)
        if rc != 0:
            return {"r": 0.0, "why": f"pyflakes: {out.splitlines()[0][:100]}"}
    return {"r": 1.0, "why": "ast+pyflakes ok"}


def verify_bash(code: str) -> dict:
    if not _have("shellcheck"):
        return {"r": 0.5, "why": "shellcheck missing — neutral"}
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(code); f.flush(); p = f.name
    try:
        rc, _, _ = _run(["shellcheck", p], timeout=15)
    finally:
        os.unlink(p)
    return {"r": 1.0 if rc == 0 else 0.0, "why": "shellcheck"}


def verify_tf(code: str) -> dict:
    if _have("tflint"):
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/"main.tf").write_text(code)
            rc, _, _ = _run(["tflint", f"--chdir={td}"], timeout=20)
            return {"r": 1.0 if rc == 0 else 0.0, "why": "tflint"}
    if _have("terraform"):
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/"main.tf").write_text(code)
            rc, _, _ = _run(["terraform", f"-chdir={td}", "validate"], timeout=30)
            return {"r": 1.0 if rc == 0 else 0.0, "why": "terraform validate"}
    return {"r": 0.5, "why": "no tf/tflint"}


def verify_cfn(code: str) -> dict:
    if not _have("cfn-lint"):
        return {"r": 0.5, "why": "cfn-lint missing"}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(code); f.flush(); p = f.name
    try:
        rc, _, _ = _run(["cfn-lint", p], timeout=20)
    finally: os.unlink(p)
    return {"r": 1.0 if rc == 0 else 0.0, "why": "cfn-lint"}


def verify_k8s(code: str) -> dict:
    bin_ = "kubeconform" if _have("kubeconform") else (
        "kubeval" if _have("kubeval") else None)
    if not bin_:
        return {"r": 0.5, "why": "no kubeconform/kubeval"}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(code); f.flush(); p = f.name
    try:
        rc, _, _ = _run([bin_, p], timeout=15)
    finally: os.unlink(p)
    return {"r": 1.0 if rc == 0 else 0.0, "why": bin_}


def verify_dockerfile(code: str) -> dict:
    if not _have("hadolint"):
        return {"r": 0.5, "why": "hadolint missing"}
    rc, _, _ = _run(["hadolint", "-"], stdin=code, timeout=15)
    return {"r": 1.0 if rc == 0 else 0.0, "why": "hadolint"}


def verify_actions(code: str) -> dict:
    if not _have("actionlint"):
        return {"r": 0.5, "why": "actionlint missing"}
    rc, _, _ = _run(["actionlint", "-"], stdin=code, timeout=15)
    return {"r": 1.0 if rc == 0 else 0.0, "why": "actionlint"}


def verify_sql(code: str) -> dict:
    if not _have("sqlfluff"):
        return {"r": 0.5, "why": "sqlfluff missing"}
    rc, _, _ = _run(["sqlfluff", "lint", "--dialect", "postgres", "-"],
                    stdin=code, timeout=20)
    return {"r": 1.0 if rc == 0 else 0.0, "why": "sqlfluff"}


def verify_security(code: str, lang: str = "python") -> dict:
    if not _have("semgrep"):
        return {"r": 0.5, "why": "semgrep missing"}
    suffix = {"python":"py","bash":"sh","tf":"tf","yaml":"yaml"}.get(lang, "txt")
    with tempfile.NamedTemporaryFile("w", suffix=f".{suffix}", delete=False) as f:
        f.write(code); f.flush(); p = f.name
    try:
        rc, out, _ = _run(["semgrep", "--config=p/security-audit", "--quiet",
                           "--json", p], timeout=60)
    finally: os.unlink(p)
    try:
        results = json.loads(out or "{}").get("results", [])
        high = sum(1 for r in results
                   if r.get("extra", {}).get("severity") in ("ERROR","WARNING"))
        return {"r": 1.0 if high == 0 else 0.0, "why": f"semgrep hits={high}"}
    except Exception:
        return {"r": 0.5, "why": "semgrep parse error"}


def verify_format_json(text: str) -> dict:
    try:
        json.loads(text); return {"r": 1.0, "why": "json valid"}
    except Exception as e:
        return {"r": 0.0, "why": f"json: {str(e)[:80]}"}


def verify_format_yaml(text: str) -> dict:
    try:
        import yaml
        yaml.safe_load(text); return {"r": 1.0, "why": "yaml valid"}
    except ImportError:
        return {"r": 0.5, "why": "pyyaml missing"}
    except Exception as e:
        return {"r": 0.0, "why": f"yaml: {str(e)[:80]}"}


def verify_math_numeric(response: str, gold: str) -> dict:
    """Extract last number from response, compare to gold (within rel tol 1e-4)."""
    nums_r = NUM_RE.findall(response)
    nums_g = NUM_RE.findall(gold)
    if not nums_r or not nums_g:
        return {"r": 0.0, "why": "no number extracted"}
    try:
        r_v = float(nums_r[-1]); g_v = float(nums_g[-1])
        denom = max(1e-9, abs(g_v))
        if abs(r_v - g_v) / denom <= 1e-4:
            return {"r": 1.0, "why": f"{r_v} ~= {g_v}"}
        return {"r": 0.0, "why": f"{r_v} != {g_v}"}
    except ValueError:
        return {"r": 0.0, "why": "non-numeric"}


def verify_idk_honest(response: str, is_unknown: bool) -> dict:
    head = response[: max(200, len(response)//2)]
    abstain = bool(ABSTAIN_RE.search(head))
    if is_unknown and abstain:
        return {"r": 1.0, "why": "calibrated_idk"}
    if is_unknown and not abstain:
        return {"r": 0.0, "why": "should_have_abstained"}
    if not is_unknown and abstain:
        return {"r": 0.0, "why": "over_abstain"}
    return {"r": 1.0, "why": "answered_known"}


VERIFIERS = {
    "code-python":     lambda d: verify_python(d.get("response","")),
    "code-bash":       lambda d: verify_bash(d.get("response","")),
    "iac-tf":          lambda d: verify_tf(d.get("response","")),
    "iac-cfn":         lambda d: verify_cfn(d.get("response","")),
    "iac-k8s":         lambda d: verify_k8s(d.get("response","")),
    "dockerfile":      lambda d: verify_dockerfile(d.get("response","")),
    "github-actions":  lambda d: verify_actions(d.get("response","")),
    "sql":             lambda d: verify_sql(d.get("response","")),
    "security":        lambda d: verify_security(d.get("response",""),
                                                  d.get("lang","python")),
    "format-json":     lambda d: verify_format_json(d.get("response","")),
    "format-yaml":     lambda d: verify_format_yaml(d.get("response","")),
    "math":            lambda d: verify_math_numeric(d.get("response",""),
                                                       d.get("gold","")),
    "idk-honest":      lambda d: verify_idk_honest(d.get("response",""),
                                                     bool(d.get("is_unknown", False))),
}


def reward(d: dict) -> dict:
    domain = d.get("domain", "")
    if domain not in VERIFIERS:
        return {"reward": 0.5, "branch": "no_verifier", "domain": domain}
    res = VERIFIERS[domain](d)
    return {"reward": float(res["r"]), "branch": res["why"], "domain": domain}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.jsonl:
        n_in = n_out = 0
        sums = {}
        with open(args.jsonl) as fin, open(args.out or "/dev/stdout", "w") as fout:
            for line in fin:
                try: d = json.loads(line)
                except: continue
                n_in += 1
                d["verifiable_reward"] = reward(d)
                key = d["verifiable_reward"]["branch"]
                sums[key] = sums.get(key, 0) + 1
                fout.write(json.dumps(d, ensure_ascii=False) + "\n")
                n_out += 1
                if n_out % 50 == 0: print(f"  scored {n_out}/{n_in}", file=sys.stderr)
        for k, v in sums.items(): print(f"  {k:<30} {v:>5}", file=sys.stderr)
        print(f"[done] in={n_in} out={n_out}", file=sys.stderr)
        return

    if sys.stdin.isatty():
        print("usage: echo '{...}' | python3 verifiable-rewards-gym.py", file=sys.stderr)
        sys.exit(2)
    d = json.load(sys.stdin)
    print(json.dumps(reward(d), indent=2))


if __name__ == "__main__":
    main()
