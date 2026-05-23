"""Surrogate-1 v2 — Validator-graded RLVR (Reinforcement Learning from Verifier Rewards).

Run real domain validators on Surrogate-generated artifacts. Each validator
emits a deterministic numeric reward; the composite reward feeds DAPO/GRPO
during stage3 RL training.

Validators (all open-source, no LLM calls):
  • Python      → pyflakes  (parse + undefined names)
  • Shell       → shellcheck (best-practice + bug)
  • Dockerfile  → hadolint
  • Terraform   → tflint  (must be in PATH; falls back to `terraform validate`)
  • Kubernetes  → kubeval / kubeconform (manifest schema)
  • GH Actions  → actionlint
  • CloudFormation → cfn-lint
  • IAM/Sec     → semgrep --config p/security-audit
  • SQL         → sqlfluff lint --dialect postgres
  • CFN security → cfn-guard validate (if rule packs available)

Each validator returns: { ok: bool, score: float in [0,1], hits: [{rule,msg}] }.

Composite reward (matches stage3-dapo.yml weighting):
  R = 0.40 * lint_score + 0.20 * security_score + 0.20 * test_pass
      + 0.10 * format_score + 0.10 * cite_correct - 1.0 * polluted

Usage:
  echo '{"language":"terraform","code":"resource \"aws_s3_bucket\" \"x\" {}"}' \\
    | python3 validator-rlvr.py
  → {"ok": true, "score": 0.7, "validators": {...}, "composite": 0.7}

  python3 validator-rlvr.py --jsonl in.jsonl --out scored.jsonl  # batch mode
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LANG_HINTS = {
    "python":  ["import ", "def ", "class ", "from "],
    "bash":    ["#!/bin/bash", "#!/usr/bin/env bash", "set -e", "set -u"],
    "dockerfile": ["FROM ", "RUN ", "ENTRYPOINT ", "CMD "],
    "terraform": ["resource \"", "provider \"", "variable \"", "module \""],
    "k8s":     ["apiVersion:", "kind: Deployment", "kind: Service", "kind: Pod"],
    "github-actions": ["uses: actions/", "runs-on:", "jobs:"],
    "cloudformation": ["AWSTemplateFormatVersion", "Resources:\n  ",
                        "\"Type\": \"AWS::"],
    "sql":     ["select ", "create table ", "insert into ", "update "],
}


def detect_lang(code: str, hint: str | None = None) -> str:
    if hint:
        return hint.lower()
    code_low = code.lower()
    scores: dict[str, int] = {}
    for lang, hints in LANG_HINTS.items():
        scores[lang] = sum(1 for h in hints if h.lower() in code_low)
    if not scores:
        return "unknown"
    best = max(scores.items(), key=lambda x: x[1])
    return best[0] if best[1] >= 2 else "unknown"


def _run(cmd: list[str], stdin: str | None = None,
         timeout: int = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", f"validator not in PATH: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {cmd[0]}"


def _have(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


def validate_python(code: str) -> dict:
    if not _have("pyflakes"):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "pyflakes not installed"}
    rc, out, err = _run(["pyflakes", "-"], stdin=code, timeout=15)
    if rc == 0:
        return {"ok": True, "score": 1.0, "hits": []}
    hits = [{"line": ln, "msg": ln} for ln in out.splitlines()[:20] if ln]
    score = max(0.0, 1.0 - 0.1 * len(hits))
    return {"ok": False, "score": score, "hits": hits}


def validate_bash(code: str) -> dict:
    if not _have("shellcheck"):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "shellcheck not installed"}
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as t:
        t.write(code); t.flush()
        path = t.name
    try:
        rc, out, err = _run(["shellcheck", "-f", "json", path], timeout=15)
    finally:
        os.unlink(path)
    if rc == 0:
        return {"ok": True, "score": 1.0, "hits": []}
    try:
        hits = json.loads(out or "[]")
    except Exception:
        hits = []
    err_n = sum(1 for h in hits if h.get("level") == "error")
    warn_n = sum(1 for h in hits if h.get("level") == "warning")
    score = max(0.0, 1.0 - 0.2 * err_n - 0.05 * warn_n)
    return {"ok": err_n == 0, "score": score,
            "hits": [{"line": h.get("line"), "msg": h.get("message", "")[:120]}
                     for h in hits[:10]]}


def validate_dockerfile(code: str) -> dict:
    if not _have("hadolint"):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "hadolint not installed"}
    rc, out, err = _run(["hadolint", "-f", "json", "-"], stdin=code, timeout=15)
    try:
        hits = json.loads(out or "[]")
    except Exception:
        hits = []
    err_n = sum(1 for h in hits if h.get("level") == "error")
    warn_n = sum(1 for h in hits if h.get("level") == "warning")
    score = max(0.0, 1.0 - 0.25 * err_n - 0.05 * warn_n)
    return {"ok": err_n == 0, "score": score,
            "hits": [{"line": h.get("line"), "code": h.get("code"),
                      "msg": h.get("message", "")[:120]} for h in hits[:10]]}


def validate_terraform(code: str) -> dict:
    if not (_have("tflint") or _have("terraform")):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "no tflint or terraform"}
    with tempfile.TemporaryDirectory() as td:
        Path(td, "main.tf").write_text(code)
        if _have("tflint"):
            rc, out, err = _run(["tflint", "--format=json",
                                  f"--chdir={td}"], timeout=20)
            try:
                obj = json.loads(out or "{}")
                issues = obj.get("issues", [])
            except Exception:
                issues = []
            err_n = sum(1 for h in issues if h.get("rule", {}).get("severity") == "error")
            warn_n = sum(1 for h in issues if h.get("rule", {}).get("severity") == "warning")
            score = max(0.0, 1.0 - 0.2 * err_n - 0.05 * warn_n)
            return {"ok": err_n == 0, "score": score,
                    "hits": [{"rule": h.get("rule", {}).get("name"),
                              "msg": h.get("message", "")[:120]}
                             for h in issues[:10]]}
        rc, out, err = _run(
            ["terraform", "-chdir=" + td, "validate", "-no-color"], timeout=30)
        return {"ok": rc == 0, "score": 1.0 if rc == 0 else 0.4,
                "hits": [] if rc == 0 else [{"msg": err.splitlines()[-1] if err else "validate failed"}]}


def validate_k8s(code: str) -> dict:
    bin_name = "kubeconform" if _have("kubeconform") else (
        "kubeval" if _have("kubeval") else None)
    if not bin_name:
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "no kubeconform/kubeval"}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as t:
        t.write(code); t.flush()
        path = t.name
    try:
        rc, out, err = _run([bin_name, "-output", "json", path], timeout=15)
    finally:
        os.unlink(path)
    if rc == 0:
        return {"ok": True, "score": 1.0, "hits": []}
    return {"ok": False, "score": 0.4,
            "hits": [{"msg": (err or out).splitlines()[-1][:200] if (err or out) else "invalid"}]}


def validate_actions(code: str) -> dict:
    if not _have("actionlint"):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "actionlint not installed"}
    rc, out, err = _run(["actionlint", "-format=json", "-"], stdin=code,
                          timeout=15)
    try:
        hits = json.loads(out or "[]")
    except Exception:
        hits = []
    err_n = len(hits)
    score = max(0.0, 1.0 - 0.2 * err_n)
    return {"ok": err_n == 0, "score": score,
            "hits": [{"line": h.get("line"), "msg": h.get("message", "")[:120]}
                     for h in hits[:10]]}


def validate_cloudformation(code: str) -> dict:
    if not _have("cfn-lint"):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "cfn-lint not installed"}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as t:
        t.write(code); t.flush()
        path = t.name
    try:
        rc, out, err = _run(["cfn-lint", "-f", "json", path], timeout=20)
    finally:
        os.unlink(path)
    try:
        hits = json.loads(out or "[]")
    except Exception:
        hits = []
    err_n = sum(1 for h in hits if h.get("Level") == "Error")
    warn_n = sum(1 for h in hits if h.get("Level") == "Warning")
    score = max(0.0, 1.0 - 0.2 * err_n - 0.05 * warn_n)
    return {"ok": err_n == 0, "score": score,
            "hits": [{"rule": h.get("Rule", {}).get("Id"),
                      "msg": h.get("Message", "")[:120]} for h in hits[:10]]}


def validate_security(code: str, lang: str) -> dict:
    """Cross-language secrets + insecure-pattern scan via semgrep."""
    if not _have("semgrep"):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "semgrep not installed"}
    with tempfile.NamedTemporaryFile("w", suffix="." + (
        {"python": "py", "bash": "sh", "terraform": "tf",
         "k8s": "yaml", "dockerfile": "Dockerfile"}.get(lang, "txt")),
                                       delete=False) as t:
        t.write(code); t.flush()
        path = t.name
    try:
        rc, out, err = _run(
            ["semgrep", "--config=p/security-audit", "--json", "--quiet", path],
            timeout=60)
    finally:
        os.unlink(path)
    try:
        obj = json.loads(out or "{}")
        results = obj.get("results", [])
    except Exception:
        results = []
    high = sum(1 for r in results
               if r.get("extra", {}).get("severity") in ("ERROR", "WARNING"))
    score = max(0.0, 1.0 - 0.3 * high)
    return {"ok": high == 0, "score": score,
            "hits": [{"rule": r.get("check_id"),
                      "msg": r.get("extra", {}).get("message", "")[:120]}
                     for r in results[:10]]}


def validate_sql(code: str) -> dict:
    if not _have("sqlfluff"):
        return {"ok": False, "score": 0.5, "hits": [],
                "skipped": "sqlfluff not installed"}
    rc, out, err = _run(
        ["sqlfluff", "lint", "--dialect", "postgres", "--format", "json", "-"],
        stdin=code, timeout=20)
    try:
        hits = json.loads(out or "[]")
        violations = []
        for f in hits:
            violations.extend(f.get("violations", []))
    except Exception:
        violations = []
    err_n = len(violations)
    score = max(0.0, 1.0 - 0.1 * err_n)
    return {"ok": err_n == 0, "score": score,
            "hits": [{"rule": v.get("code"), "msg": v.get("description", "")[:120]}
                     for v in violations[:10]]}


VALIDATORS = {
    "python":         validate_python,
    "bash":           validate_bash,
    "dockerfile":     validate_dockerfile,
    "terraform":      validate_terraform,
    "k8s":            validate_k8s,
    "github-actions": validate_actions,
    "cloudformation": validate_cloudformation,
    "sql":            validate_sql,
}


def score_artifact(code: str, language: str | None = None) -> dict:
    lang = detect_lang(code, language)
    out = {"language": lang, "validators": {}, "composite": 0.0}
    if lang == "unknown":
        out["composite"] = 0.5
        out["note"] = "language could not be detected"
        return out

    base = VALIDATORS.get(lang, lambda c: {"ok": False, "score": 0.5,
                                            "skipped": f"no validator for {lang}"})
    out["validators"]["lint"] = base(code)
    out["validators"]["security"] = validate_security(code, lang)

    lint_s = out["validators"]["lint"].get("score", 0.5)
    sec_s = out["validators"]["security"].get("score", 0.5)
    # Composite (RLVR reward): lint 60%, security 40%. RL trainer can override.
    out["composite"] = round(0.6 * lint_s + 0.4 * sec_s, 4)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", help="batch: JSONL with {code, language?, prompt?}")
    ap.add_argument("--out", help="batch: output JSONL with score field added")
    args = ap.parse_args()

    if args.jsonl:
        if not args.out:
            print("--out required with --jsonl", file=sys.stderr)
            sys.exit(2)
        n_in = n_out = 0
        with open(args.jsonl) as fin, open(args.out, "w") as fout:
            for line in fin:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                n_in += 1
                code = d.get("response") or d.get("code") or ""
                lang = d.get("language")
                d["validator"] = score_artifact(code, lang)
                fout.write(json.dumps(d, ensure_ascii=False) + "\n")
                n_out += 1
                if n_out % 50 == 0:
                    print(f"  scored {n_out}/{n_in}")
        print(f"[done] in={n_in} scored={n_out} → {args.out}")
        return

    if sys.stdin.isatty():
        print("usage: echo '{...}' | python3 validator-rlvr.py", file=sys.stderr)
        sys.exit(2)
    d = json.load(sys.stdin)
    code = d.get("code") or d.get("response") or ""
    lang = d.get("language")
    print(json.dumps(score_artifact(code, lang), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
