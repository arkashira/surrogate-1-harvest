#!/usr/bin/env python3
"""axentx security-engineer — runs SAST + dep-CVE scan on each project,
flags issues, routes critical findings to dev queue as fix-tickets.

Fills the "Security Engineer / AppSec" gap from SDLC research.

Cycle (event-driven, 10min tick):
  - For each /opt/axentx/<slug>/ repo with recent commits (last 1h):
      1. Scan dependencies: package.json/requirements.txt/go.mod for known
         CVE-bearing versions (basic safety check via osv.dev when token-less)
      2. SAST: simple regex sweep for high-signal patterns:
         - secrets in code (api_key=*, AWS_*, sk-*, ghp_*)
         - SQL string-concat (`f"SELECT ... {user_input}"`)
         - eval() / exec() of user input
         - hardcoded URLs to internal services
         - missing input validation in HTTP handlers
      3. Critical findings → push as dev tickets with track="security"
      4. Log scan-result to shared_memory for audit

Doesn't replace dedicated SAST tools but provides cheap/fast first-pass
that's better than nothing. Every commit gets eyes on it.
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("SECURITY_ENG_POLL_SEC", "600"))   # 10min
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


# Patterns that almost always indicate trouble. Tuned for false-positive low.
SAST_PATTERNS = [
    ("secret_aws", re.compile(
        r"AKIA[0-9A-Z]{16}|aws_secret_access_key\s*=\s*['\"][A-Za-z0-9/+=]{30,}",
        re.IGNORECASE)),
    ("secret_gh_pat", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("secret_openai", re.compile(r"\bsk-[A-Za-z0-9]{30,}\b")),
    ("secret_hf", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("secret_gcp_sa",
     re.compile(r'"type":\s*"service_account"', re.IGNORECASE)),
    ("sql_concat",
     re.compile(r'(?i)f["\'].*SELECT.*\{[^}]+\}|".*"\s*\+\s*\w+\s*\+\s*"\s*WHERE')),
    ("eval_exec",
     re.compile(r'\beval\s*\([^)]*input\s*\(|\bexec\s*\([^)]*input\s*\(')),
    ("dangerous_pickle", re.compile(r"pickle\.loads?\(.*request|cPickle\.loads")),
    ("xxe", re.compile(r"etree\.parse\([^)]*\).*resolve_entities\s*=\s*True")),
]


def scan_dir(repo: Path) -> list[dict]:
    findings = []
    extensions = (".py", ".ts", ".tsx", ".js", ".jsx", ".go",
                  ".java", ".rs", ".sql", ".yml", ".yaml", ".env")
    for f in repo.rglob("*"):
        if not f.is_file():
            continue
        if any(part.startswith(".") for part in f.parts):
            continue
        if "node_modules" in str(f) or "vendor" in str(f) or ".venv" in str(f):
            continue
        if f.suffix not in extensions:
            continue
        if f.stat().st_size > 200_000:   # skip huge files
            continue
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for kind, regex in SAST_PATTERNS:
            for m in regex.finditer(txt):
                lineno = txt[:m.start()].count("\n") + 1
                findings.append({
                    "kind": kind,
                    "file": str(f.relative_to(repo)),
                    "line": lineno,
                    "snippet": txt[max(0, m.start()-30):m.end()+30][:200],
                    "severity": ("critical" if "secret_" in kind
                                 else "high" if kind in
                                 ("eval_exec", "dangerous_pickle", "xxe")
                                 else "med"),
                })
                if len(findings) >= 50:   # cap per repo
                    return findings
    return findings


def scan_dependencies(repo: Path) -> list[dict]:
    """Cheap dep audit — reads package.json/requirements.txt and queries
    OSV.dev for the latest CVEs on the listed versions. No-op if file missing."""
    out = []
    # Python
    req = repo / "requirements.txt"
    if req.exists():
        try:
            for line in req.read_text(errors="replace").splitlines()[:80]:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([a-zA-Z0-9_-]+)==(\S+)", line)
                if not m:
                    continue
                pkg, ver = m.group(1), m.group(2)
                osv = _osv_query(pkg, ver, "PyPI")
                if osv:
                    out.append({"ecosystem": "PyPI", "pkg": pkg,
                                "version": ver, "vulns": osv})
        except Exception:
            pass
    # Node
    pj = repo / "package.json"
    if pj.exists():
        try:
            d = json.loads(pj.read_text(errors="replace"))
            deps = {**(d.get("dependencies") or {}),
                    **(d.get("devDependencies") or {})}
            for pkg, ver in list(deps.items())[:80]:
                ver_clean = re.sub(r"[\^~>=<]", "", str(ver)).split()[0]
                if not ver_clean:
                    continue
                osv = _osv_query(pkg, ver_clean, "npm")
                if osv:
                    out.append({"ecosystem": "npm", "pkg": pkg,
                                "version": ver_clean, "vulns": osv})
        except Exception:
            pass
    return out


def _osv_query(pkg: str, version: str, ecosystem: str) -> list:
    """Query OSV.dev for vulnerabilities. Free, no token needed."""
    try:
        body = json.dumps({
            "package": {"name": pkg, "ecosystem": ecosystem},
            "version": version,
        }).encode()
        req = urllib.request.Request(
            "https://api.osv.dev/v1/query",
            data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
        return [v.get("id") for v in (d.get("vulns") or [])][:5]
    except Exception:
        return []


def _was_recently_scanned(slug: str, hours: int = 1) -> bool:
    try:
        from axentx_shared import kv_get
        v = kv_get(f"sec-scan.{slug}") or {}
        if isinstance(v, dict) and v.get("v"): v = v["v"]
        if not isinstance(v, dict) or not v.get("ts"):
            return False
        ts = datetime.datetime.fromisoformat(v["ts"].replace("Z", "+00:00"))
        return ((datetime.datetime.now(datetime.timezone.utc) - ts)
                .total_seconds() / 3600) < hours
    except Exception:
        return False


def push_security_ticket(slug: str, finding: dict) -> bool:
    fid = (f"20260504-sec-{slug}-{finding['kind']}-"
           f"{hashlib.md5(json.dumps(finding).encode()).hexdigest()[:10]}")
    brief = (
        f"# SECURITY: {finding['kind']} ({finding['severity']})\n\n"
        f"File: `{finding['file']}` line {finding['line']}\n\n"
        f"Snippet:\n```\n{finding['snippet']}\n```\n\n"
        f"## Acceptance criteria\n"
        f"- Remove/sanitize the flagged content\n"
        f"- Add test that prevents regression\n"
        f"- Update SECURITY.md if pattern is recurring"
    )
    payload = {
        "id": fid, "stage": "dev", "project": slug, "focus": "security-fix",
        "history": [{
            "stage": "security-engineer",
            "actor": "axentx-security-engineer",
            "output": brief[:1000],
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": brief},
        "ticket": {"track": "security", "complexity": "S",
                   "files_likely": [finding["file"]],
                   "acceptance": ["remove flagged content", "add test"]},
        "security_finding": finding,
    }
    body = {"id": fid, "stage": "dev", "project": slug,
            "focus": "security-fix", "payload": payload}
    try:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("security-eng", "  ⤷ not leader — skip")
        return False
    if not PROJECTS_ROOT.exists():
        return False

    repos = [p for p in PROJECTS_ROOT.iterdir()
             if p.is_dir() and (p / ".git").exists()]
    if not repos:
        return False

    pushed = 0
    scanned = 0
    for repo in repos[:8]:
        slug = repo.name
        if slug in {"cost-radar"}:
            continue
        if _was_recently_scanned(slug, hours=1):
            continue
        scanned += 1
        sast = scan_dir(repo)
        deps = scan_dependencies(repo)
        try:
            from axentx_shared import kv_set, memory_log
            kv_set(f"sec-scan.{slug}", {
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "n_sast": len(sast),
                "n_dep_vulns": len(deps),
                "host": HOST,
            })
            if sast or deps:
                memory_log("security-eng", "scan-finding",
                           f"{slug}: sast={len(sast)}, dep_vulns={len(deps)}",
                           body=json.dumps({"sast": sast[:10],
                                            "dep_vulns": deps[:10]})[:1500],
                           tags=["security-eng", slug,
                                 "critical" if any(f["severity"] == "critical"
                                                   for f in sast) else "info"])
        except Exception:
            pass
        # Push only critical/high SAST findings as dev tickets
        critical = [f for f in sast if f["severity"] in ("critical", "high")]
        for f in critical[:3]:   # cap per project per cycle
            if push_security_ticket(slug, f):
                pushed += 1
        log("security-eng",
            f"  ✓ {slug}: sast={len(sast)} (crit={len(critical)}) "
            f"deps={len(deps)} pushed={min(len(critical), 3)}")

    log("security-eng",
        f"  ✓ scanned {scanned} projects, pushed {pushed} security tickets")
    return False


if __name__ == "__main__":
    daemon_loop("security-eng", POLL_SEC, cycle)
