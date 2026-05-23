"""Ground-truth check — objective verification beyond reviewer opinion.

When task produces code, run external validators:
  - Python: ast.parse (syntax) + optional ruff / mypy / pytest
  - TypeScript/JS: tsc / eslint (if available)
  - Terraform: terraform validate + tfsec (if available)
  - CloudFormation: cfn-lint (if available)
  - Shell: bash -n (syntax) + shellcheck (if available)
  - JSON/YAML: parse check

Reviewer opinion + ground-truth = double check. Review says pass BUT compile
fails → overrides to fail.

Output: {"verdict": "pass|fail", "checks": [...], "blocking_failure": bool}
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

CODE_BLOCK_RE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return list of (language, content) pairs from markdown fenced blocks."""
    blocks = []
    for m in CODE_BLOCK_RE.finditer(text):
        lang = (m.group(1) or "").lower().strip()
        content = m.group(2).strip()
        if content:
            blocks.append((lang, content))
    return blocks


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(cmd: list[str], stdin: Optional[str] = None, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout + r.stderr)[:2000]
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as e:
        return -1, str(e)


# ----------------------------------------------------------------------
# Per-language checkers
# ----------------------------------------------------------------------
def check_python(code: str) -> list[dict]:
    out = []
    # 1. syntax
    try:
        ast.parse(code)
        out.append({"tool": "python-syntax", "pass": True, "msg": "syntactically valid"})
    except SyntaxError as e:
        out.append({"tool": "python-syntax", "pass": False,
                   "msg": f"SyntaxError: {e}", "blocking": True})
        return out  # no point in running linters
    # 2. ruff (if installed)
    if _have("ruff"):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
            path = f.name
        try:
            rc, output = _run(["ruff", "check", "--select=E,F", "--output-format=concise", path])
            passed = rc == 0
            out.append({"tool": "ruff", "pass": passed,
                       "msg": output[:500] if output else "clean"})
        finally:
            Path(path).unlink(missing_ok=True)
    # 3. mypy (if installed, non-blocking)
    if _have("mypy"):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
            path = f.name
        try:
            rc, output = _run(["mypy", "--no-error-summary", "--ignore-missing-imports", path])
            out.append({"tool": "mypy", "pass": rc == 0, "msg": output[:500]})
        finally:
            Path(path).unlink(missing_ok=True)
    return out


def check_typescript(code: str) -> list[dict]:
    out = []
    if not _have("npx") and not _have("tsc"):
        return [{"tool": "typescript", "pass": True, "msg": "tsc/npx not installed — skipped"}]
    with tempfile.NamedTemporaryFile("w", suffix=".ts", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        cmd = (["tsc", "--noEmit", "--allowJs", "--target", "ES2022",
                "--moduleResolution", "node", path] if _have("tsc")
               else ["npx", "-y", "--package=typescript", "--",
                     "tsc", "--noEmit", "--target", "ES2022", path])
        rc, output = _run(cmd, timeout=60)
        out.append({"tool": "tsc", "pass": rc == 0,
                    "msg": output[:600] if output else "clean",
                    "blocking": rc != 0})
    finally:
        Path(path).unlink(missing_ok=True)
    return out


def check_shell(code: str) -> list[dict]:
    out = []
    # bash -n (syntax only — no execution). Use file path; stdin parser is lenient.
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        rc, output = _run(["bash", "-n", path])
    finally:
        Path(path).unlink(missing_ok=True)
    out.append({"tool": "bash-syntax", "pass": rc == 0, "msg": output or "valid",
                "blocking": rc != 0})
    if _have("shellcheck"):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(code)
            path = f.name
        try:
            rc, output = _run(["shellcheck", "-f", "gcc", path])
            # shellcheck returns nonzero for warnings — non-blocking
            out.append({"tool": "shellcheck", "pass": rc == 0, "msg": output[:500]})
        finally:
            Path(path).unlink(missing_ok=True)
    return out


def check_terraform(code: str) -> list[dict]:
    out = []
    if not _have("terraform"):
        return [{"tool": "terraform", "pass": True, "msg": "terraform not installed — skipped"}]
    with tempfile.TemporaryDirectory() as d:
        Path(d, "main.tf").write_text(code)
        rc, output = _run(["terraform", "-chdir=" + d, "init", "-backend=false", "-input=false"], timeout=60)
        if rc != 0:
            out.append({"tool": "terraform-init", "pass": False, "msg": output[:500],
                        "blocking": True})
            return out
        rc, output = _run(["terraform", "-chdir=" + d, "validate"])
        out.append({"tool": "terraform-validate", "pass": rc == 0,
                    "msg": output[:500] if output else "clean",
                    "blocking": rc != 0})
        if _have("tfsec"):
            rc, output = _run(["tfsec", d, "--no-color"])
            out.append({"tool": "tfsec", "pass": rc == 0, "msg": output[:500]})
    return out


def check_cloudformation(code: str) -> list[dict]:
    if not _have("cfn-lint"):
        return [{"tool": "cfn-lint", "pass": True, "msg": "cfn-lint not installed — skipped"}]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        rc, output = _run(["cfn-lint", path])
        return [{"tool": "cfn-lint", "pass": rc == 0, "msg": output[:500],
                 "blocking": rc != 0}]
    finally:
        Path(path).unlink(missing_ok=True)


def check_json(code: str) -> list[dict]:
    try:
        json.loads(code)
        return [{"tool": "json-parse", "pass": True, "msg": "valid JSON"}]
    except json.JSONDecodeError as e:
        return [{"tool": "json-parse", "pass": False, "msg": str(e), "blocking": True}]


def check_yaml(code: str) -> list[dict]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return [{"tool": "yaml-parse", "pass": True, "msg": "pyyaml not installed — skipped"}]
    try:
        yaml.safe_load(code)
        return [{"tool": "yaml-parse", "pass": True, "msg": "valid YAML"}]
    except yaml.YAMLError as e:
        return [{"tool": "yaml-parse", "pass": False, "msg": str(e)[:300], "blocking": True}]


LANG_CHECKERS = {
    "python": check_python, "py": check_python,
    "typescript": check_typescript, "ts": check_typescript,
    "javascript": check_typescript, "js": check_typescript,
    "bash": check_shell, "sh": check_shell, "shell": check_shell,
    "terraform": check_terraform, "hcl": check_terraform, "tf": check_terraform,
    "cloudformation": check_cloudformation, "yaml": check_yaml, "yml": check_yaml,
    "json": check_json,
}


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------
def check(work_product: str) -> dict:
    """Extract code blocks + run checkers. Returns aggregate verdict.

    Returns:
      {
        "has_code": bool,
        "verdict": "pass" | "fail",
        "blocking_failure": bool,
        "checks": [{tool, pass, msg, blocking?}, ...],
        "blocks_checked": int,
      }
    """
    blocks = extract_code_blocks(work_product)
    all_checks: list[dict] = []
    has_code = False

    for lang, content in blocks:
        checker = LANG_CHECKERS.get(lang)
        if not checker:
            continue
        has_code = True
        results = checker(content)
        for r in results:
            r["language"] = lang
        all_checks.extend(results)

    blocking_failure = any(c.get("blocking") and not c.get("pass") for c in all_checks)
    # Only blocking checks determine pass/fail. Non-blocking (warn) tools like
    # mypy or shellcheck can fail without sinking the verdict.
    blocking_passed = all(c.get("pass") for c in all_checks if c.get("blocking"))
    any_blocking = any(c.get("blocking") for c in all_checks)

    if not has_code:
        return {
            "has_code": False,
            "verdict": "pass",  # nothing to check → don't block review
            "blocking_failure": False,
            "checks": [],
            "blocks_checked": 0,
        }

    if blocking_failure:
        verdict = "fail"
    elif not any_blocking:
        # no blocking checks ran (e.g. tools missing) — warn
        verdict = "warn"
    else:
        # all blocking checks passed — non-blocking may still complain, but ship it
        any_non_blocking_failed = any(
            not c.get("pass") and not c.get("blocking") for c in all_checks
        )
        verdict = "warn" if any_non_blocking_failed else "pass"

    return {
        "has_code": True,
        "verdict": verdict,
        "blocking_failure": blocking_failure,
        "checks": all_checks,
        "blocks_checked": len(blocks),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        text = Path(sys.argv[1]).read_text()
    else:
        text = sys.stdin.read()
    result = check(text)
    print(json.dumps(result, indent=2))
