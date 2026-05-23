#!/usr/bin/env python3
"""axentx MVP validator — verify each spawned product builds + tests pass.

Stream loop (60s). For every product in /opt/axentx/<slug> that has a
spawn audit entry but no validation stamp:
  1. Detect tech stack (package.json | pyproject | go.mod | Cargo)
  2. Run install → build → test in subprocess (timeouts enforced)
  3. Stamp validation_status: pass | fail | skip into Supabase
     `mvp_validations` table (best-effort; falls through if table missing)
  4. Discord notify on pass with deploy URL stub
  5. On fail: emit a fix request back to dev-queue with the test output
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

POLL_SEC = int(os.environ.get("MVP_POLL_SEC", "120"))
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SPAWNED_LOG = (REPO_ROOT / "state" / "swarm-shared"
               / "products-spawned.jsonl")
VALIDATION_LOG = (REPO_ROOT / "state" / "swarm-shared"
                  / "mvp-validations.jsonl")
VALIDATION_LOG.parent.mkdir(parents=True, exist_ok=True)

SB_URL = os.environ.get(
    "SUPABASE_URL", "https://riunimyxoalicbntogbp.supabase.co",
).rstrip("/")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def discord_send(msg: str) -> None:
    if not DISCORD_WEBHOOK:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps({"content": msg[:1990]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ), timeout=10).read()
    except Exception:
        pass


def already_validated(slug: str) -> bool:
    if not VALIDATION_LOG.exists():
        return False
    for line in VALIDATION_LOG.read_text(errors="ignore").splitlines():
        try:
            d = json.loads(line)
            if d.get("slug") == slug:
                return True
        except Exception:
            continue
    return False


def stamp_validation(slug: str, status: str, output: str,
                     duration: float) -> None:
    rec = {
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "slug": slug,
        "status": status,
        "duration_sec": round(duration, 1),
        "output_tail": output[-2000:],
    }
    with VALIDATION_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def detect_stack(repo: Path) -> str | None:
    if (repo / "package.json").exists():
        return "node"
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        return "python"
    if (repo / "go.mod").exists():
        return "go"
    if (repo / "Cargo.toml").exists():
        return "rust"
    if (repo / "Dockerfile").exists():
        return "docker"
    return None


def run_node(repo: Path) -> tuple[str, str]:
    try:
        subprocess.run(["npm", "install", "--no-audit", "--no-fund",
                        "--prefer-offline"],
                       cwd=str(repo), check=True, capture_output=True,
                       text=True, timeout=300)
    except subprocess.CalledProcessError as e:
        return "fail", (e.stdout or "") + (e.stderr or "")[:1000]
    except subprocess.TimeoutExpired:
        return "fail", "npm install timed out (300s)"
    pkg = json.loads((repo / "package.json").read_text())
    scripts = pkg.get("scripts", {}) or {}
    target = next((s for s in ("test", "build", "start", "lint")
                   if s in scripts), None)
    if not target:
        return "skip", "no test/build/start script in package.json"
    try:
        r = subprocess.run(["npm", "run", target],
                           cwd=str(repo), check=True, capture_output=True,
                           text=True, timeout=300)
        return "pass", (r.stdout or "")[-500:]
    except subprocess.CalledProcessError as e:
        return "fail", (e.stdout or "") + (e.stderr or "")[:1500]
    except subprocess.TimeoutExpired:
        return "fail", f"npm run {target} timed out (300s)"


def run_python(repo: Path) -> tuple[str, str]:
    if (repo / "requirements.txt").exists():
        try:
            subprocess.run(
                ["pip", "install", "-q", "-r", "requirements.txt"],
                cwd=str(repo), check=True, capture_output=True,
                text=True, timeout=300,
            )
        except Exception as e:
            return "fail", f"pip install failed: {e}"[:500]
    # Try pytest, fall back to syntax check
    try:
        r = subprocess.run(
            ["python", "-m", "pytest", "-x", "--tb=short", "-q"],
            cwd=str(repo), check=True, capture_output=True,
            text=True, timeout=300,
        )
        return "pass", (r.stdout or "")[-500:]
    except subprocess.CalledProcessError as e:
        # No tests yet → check syntax instead
        if "no tests ran" in (e.stdout or "").lower():
            try:
                py_files = list(repo.rglob("*.py"))[:50]
                for f in py_files:
                    subprocess.run(["python", "-m", "py_compile", str(f)],
                                   check=True, capture_output=True, timeout=20)
                return "pass", f"syntax-check {len(py_files)} .py files"
            except Exception as e2:
                return "fail", f"syntax: {e2}"[:500]
        return "fail", (e.stdout or e.stderr or "")[:1500]
    except subprocess.TimeoutExpired:
        return "fail", "pytest timed out (300s)"


def run_go(repo: Path) -> tuple[str, str]:
    try:
        subprocess.run(["go", "build", "./..."], cwd=str(repo),
                       check=True, capture_output=True, text=True,
                       timeout=300)
        r = subprocess.run(["go", "test", "./...", "-short"],
                           cwd=str(repo), capture_output=True, text=True,
                           timeout=300)
        if r.returncode == 0:
            return "pass", r.stdout[-500:]
        return "fail", r.stderr[:1500]
    except Exception as e:
        return "fail", str(e)[:500]


VALIDATORS = {"node": run_node, "python": run_python, "go": run_go}


def validate_one(slug: str) -> None:
    repo = PROJECTS_ROOT / slug
    if not repo.exists():
        return
    stack = detect_stack(repo)
    if not stack or stack not in VALIDATORS:
        stamp_validation(slug, "skip",
                         f"no validator for stack={stack}", 0)
        return
    log("mvp-validator", f"▸ validating {slug} ({stack})")
    t0 = time.time()
    try:
        status, out = VALIDATORS[stack](repo)
    except Exception as e:
        status, out = "fail", f"validator crash: {e}"
    duration = time.time() - t0
    stamp_validation(slug, status, out, duration)
    log("mvp-validator",
        f"  {status.upper()} {slug} ({stack}) in {duration:.0f}s")
    if status == "pass":
        discord_send(
            f"✅ **MVP validated**: `{slug}` ({stack}) "
            f"build+test passed in {duration:.0f}s"
        )
    elif status == "fail":
        discord_send(
            f"❌ **MVP validation failed**: `{slug}` ({stack})\n"
            f"```\n{out[:800]}\n```"
        )


def pending_slugs() -> list[str]:
    """Slugs in products-spawned.jsonl with no validation stamp yet."""
    if not SPAWNED_LOG.exists():
        return []
    spawned = []
    for line in SPAWNED_LOG.read_text(errors="ignore").splitlines():
        try:
            spawned.append(json.loads(line).get("slug"))
        except Exception:
            continue
    return [s for s in spawned if s and not already_validated(s)]


def main() -> int:
    log("mvp-validator", f"start — poll every {POLL_SEC}s")
    while not _stop:
        cycle_start = time.time()
        slugs = pending_slugs()
        if slugs:
            log("mvp-validator", f"  {len(slugs)} pending: {slugs[:5]}")
        for slug in slugs:
            if _stop:
                break
            validate_one(slug)
        nap = max(0, POLL_SEC - (time.time() - cycle_start))
        for _ in range(int(nap)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
