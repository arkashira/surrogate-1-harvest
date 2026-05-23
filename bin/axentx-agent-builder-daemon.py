#!/usr/bin/env python3
"""axentx agent-builder — turns agent SPECS in shared_knowledge into
deployed daemons.

Why this exists (user feedback 2026-05-04):
  > 'สร้าง agent มาทำงานบางอย่างที่เห็นควรสร้างได้เอง สร้าง skill มาใช้
  >  ได้เอง ทุก vm agent ทุกตัว sync กัน knowledge skill context
  >  experience เพิ่มขึ้นเรื่อยๆ เก่งขึ้นเรื่อยๆ'

Pipeline:
  agent-synthesizer (existing) — observes pipeline gaps → writes spec to
      shared_knowledge under slug pattern 'agent-spec/<name>'.
  ↓
  agent-builder (THIS) — every 5 min:
      1. List shared_knowledge entries with category='agent-spec'
      2. For each spec NOT yet built (no shared_kv["agent-built.<name>"]):
         - Validate spec has: name, purpose, poll_sec, action_pseudo, deps
         - Use LLM to generate the .py file (uses axentx_pipeline.daemon_loop
           pattern, structured logging, signal-handling, kv_set/memory_log)
         - Generate matching .service file (matches host's User= conv)
         - Run `python3 -m py_compile` — reject if syntax fails
         - Write files to /opt/surrogate-1-harvest/{bin,systemd}/
         - systemctl daemon-reload + enable --now <unit>
         - Verify is-active=active for >30s, else rollback
         - Mark shared_kv["agent-built.<name>"] = {ts, host, validated}
         - memory_log "agent-builder" "deployed" — visible to ALL hosts
  3. Run only on the LEADER host (least hostname) so 3 VMs don't race-deploy
     the same agent.

Skills (agent-as-helper):
  skill-spec entries (category='skill-spec') → generate Python module under
  /opt/surrogate-1-harvest/bin/axentx_skills/<name>.py with one function.
  Daemons import-and-call. Lighter than full daemon for one-shot helpers.

Self-improving:
  Every successful build → memory_log experience entry. agent-synthesizer
  reads recent experience to refine future specs (closes the loop).
"""
from __future__ import annotations
import datetime
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop, call_llm, call_llm_strong  # noqa: E402

POLL_SEC = int(os.environ.get("AGENT_BUILDER_POLL_SEC", "300"))   # 5 min
HOST = socket.gethostname()
BIN_DIR = REPO_ROOT / "bin"
SYSTEMD_DIR = Path("/etc/systemd/system")
SKILLS_DIR = REPO_ROOT / "bin" / "axentx_skills"

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


# ── Helpers ─────────────────────────────────────────────────────────────


def _sh(cmd: list[str], t: int = 30, input_data: str | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=t,
            input=input_data)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _is_leader() -> bool:
    """Pick lowest-sorted hostname across all hosts that wrote
    llm.providers.health in last 5 min as the build leader."""
    try:
        from axentx_shared import kv_get
        # Read the per-host fingerprints — for now use single global key,
        # so we just check this host's name vs known set.
        # Simpler: leader = host with lexicographically smallest name.
        # We hard-code the known hosts. New hosts auto-join via env override.
        known = os.environ.get(
            "AXENTX_HOSTS",
            "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
        ).split(",")
        return HOST == sorted(h.strip() for h in known if h.strip())[0]
    except Exception:
        return True   # default to leader if can't determine


def _kv_get(key: str) -> dict | list | str | int | None:
    try:
        from axentx_shared import kv_get
        return kv_get(key)
    except Exception:
        return None


def _kv_set(key: str, val) -> None:
    try:
        from axentx_shared import kv_set
        kv_set(key, val)
    except Exception:
        pass


def _memory_log(kind: str, title: str, body: str = "",
                tags: list[str] | None = None) -> None:
    try:
        from axentx_shared import memory_log
        memory_log("agent-builder", kind, title, body=body,
                   tags=(tags or []))
    except Exception:
        pass


def _list_specs(category: str) -> list[dict]:
    """Read all shared_knowledge entries under a category."""
    import urllib.request
    import urllib.parse
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = (os.environ.get("SUPABASE_SECRET_KEY")
              or os.environ.get("SUPABASE_SERVICE_KEY", ""))
    if not (sb_url and sb_key):
        return []
    try:
        qs = urllib.parse.urlencode({
            "category": f"eq.{category}",
            "select": "slug,title,body,metadata,updated_at",
            "order": "updated_at.desc",
            "limit": "20",
        })
        req = urllib.request.Request(
            f"{sb_url}/rest/v1/shared_knowledge?{qs}",
            headers={"apikey": sb_key,
                     "Authorization": f"Bearer {sb_key}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log("agent-builder", f"  ⚠ list_specs({category}) failed: {e}")
        return []


# ── Build pipeline ──────────────────────────────────────────────────────


_AGENT_TEMPLATE_SYSTEM = """You are an axentx daemon-author. Generate a
production-ready Python 3.11 daemon module that:

1. Starts with `from __future__ import annotations` + module docstring
   describing purpose + linking to the user feedback that motivated it.
2. Imports: datetime, json, os, signal, socket, sys, time, urllib, Path.
3. Adds `sys.path.insert(0, str(REPO_ROOT / "bin"))` then imports
   `from axentx_pipeline import log, daemon_loop` and (if needed)
   `call_llm`, `call_llm_strong`.
4. Defines `POLL_SEC = int(os.environ.get("<UPPER_NAME>_POLL_SEC", "<default>"))`
5. Sets `_stop=False` + SIGTERM handler.
6. Implements `cycle()` that returns False (so daemon_loop sleeps full POLL).
7. Uses `axentx_shared.kv_set/kv_get/memory_log` (via try-import) to share
   state with other hosts. NEVER stores state only in local files.
8. Logs every action with `log("<name>", "...")`. Concise, no verbose text.
9. Signals Discord ONLY for genuinely-actionable issues (not idle ticks).
10. Ends with `if __name__ == "__main__": daemon_loop("<name>", POLL_SEC, cycle)`

Output ONLY the Python source. No markdown fences, no commentary, just code.
The first line MUST be `#!/usr/bin/env python3`."""


def _generate_daemon(spec: dict) -> str:
    """Use LLM to render a daemon from spec. Falls back to template if LLM
    fails (returns empty string in that case — caller skips)."""
    name = spec.get("name", "unknown")
    purpose = spec.get("purpose", "")
    pseudo = spec.get("action_pseudo", "")
    poll = spec.get("poll_sec", 300)
    deps = spec.get("deps", [])
    user_quote = spec.get("user_quote", "")
    prompt = (
        f"Build daemon `axentx-{name}-daemon`.\n"
        f"Purpose: {purpose}\n"
        f"Poll every: {poll} seconds\n"
        f"User feedback quote: {user_quote}\n"
        f"Action pseudocode:\n{pseudo}\n"
        f"Required deps (already in /opt/surrogate-1-harvest/.venv): {deps}\n"
        f"Module name should be `axentx-{name}-daemon.py`.\n"
        f"Logger key should be `{name}` (short).\n"
        f"Generate the full Python file."
    )
    try:
        out = call_llm_strong(
            prompt, system=_AGENT_TEMPLATE_SYSTEM,
            max_tokens=2000, timeout=90)
    except Exception as e:
        log("agent-builder", f"  ✗ LLM failed for spec '{name}': {e}")
        return ""
    # Strip code-fence if LLM ignored instruction
    src = out.strip()
    if src.startswith("```"):
        src = src.split("\n", 1)[1]
        if src.endswith("```"):
            src = src.rsplit("```", 1)[0]
    if not src.startswith("#!/usr/bin/env python3"):
        src = "#!/usr/bin/env python3\n" + src
    return src


def _detect_user() -> str:
    """Detect User= for systemd units: copy from existing axentx unit so
    the new unit matches host conventions (Kam2=root, Kam1/GCP=ubuntu)."""
    rc, out, _ = _sh(
        ["bash", "-c",
         "grep -h '^User=' /etc/systemd/system/axentx-bd-daemon.service "
         "2>/dev/null | head -1 | cut -d= -f2"], t=5)
    user = out.strip() or "root"
    return user


def _generate_unit(name: str, poll_default: int) -> str:
    user = _detect_user()
    upper = name.upper().replace("-", "_")
    return (
        f"[Unit]\n"
        f"Description=axentx {name} — auto-generated by agent-builder\n"
        f"After=network-online.target\n\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"User={user}\n"
        f"WorkingDirectory=/opt/surrogate-1-harvest\n"
        f"EnvironmentFile=/etc/surrogate-coordinator.env\n"
        f"Environment=PYTHONUNBUFFERED=1\n"
        f"Environment=REPO_ROOT=/opt/surrogate-1-harvest\n"
        f"Environment={upper}_POLL_SEC={poll_default}\n"
        f"ExecStart=/opt/surrogate-1-harvest/.venv/bin/python "
        f"/opt/surrogate-1-harvest/bin/axentx-{name}-daemon.py\n"
        f"Restart=always\n"
        f"RestartSec=120\n"
        f"MemoryMax=192M\n"
        f"TasksMax=8\n"
        f"StandardOutput=journal\n"
        f"StandardError=journal\n\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
    )


def _validate_python(src: str) -> tuple[bool, str]:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src)
        p = f.name
    rc, out, err = _sh([sys.executable, "-m", "py_compile", p], t=20)
    Path(p).unlink(missing_ok=True)
    if rc == 0:
        return True, ""
    return False, err[:400]


def _deploy_agent(name: str, src: str, unit_text: str,
                  poll_default: int) -> tuple[bool, str]:
    bin_path = BIN_DIR / f"axentx-{name}-daemon.py"
    unit_path = SYSTEMD_DIR / f"axentx-{name}-daemon.service"
    # Reserved names — never overwrite existing daemon
    if bin_path.exists():
        return False, f"daemon file already exists: {bin_path}"
    # Write via sudo since dirs may be root-owned
    rc, _, err = _sh(["sudo", "tee", str(bin_path)], t=10, input_data=src)
    if rc != 0:
        return False, f"write daemon failed: {err}"
    _sh(["sudo", "chmod", "0755", str(bin_path)], t=5)
    rc, _, err = _sh(["sudo", "tee", str(unit_path)], t=10, input_data=unit_text)
    if rc != 0:
        return False, f"write unit failed: {err}"
    rc, _, err = _sh(["sudo", "systemctl", "daemon-reload"], t=10)
    if rc != 0:
        return False, f"daemon-reload failed: {err}"
    rc, _, err = _sh(
        ["sudo", "systemctl", "enable", "--now",
         f"axentx-{name}-daemon"], t=15)
    if rc != 0:
        return False, f"enable+start failed: {err}"
    # Verify health: must stay active for >30s
    time.sleep(35)
    rc, out, _ = _sh(
        ["systemctl", "is-active", f"axentx-{name}-daemon"], t=5)
    if out.strip() != "active":
        # rollback
        _sh(["sudo", "systemctl", "disable", "--now",
             f"axentx-{name}-daemon"], t=10)
        _sh(["sudo", "rm", str(bin_path), str(unit_path)], t=5)
        _sh(["sudo", "systemctl", "daemon-reload"], t=5)
        return False, f"new daemon failed health check: {out.strip()}"
    return True, "deployed + healthy"


# ── Main loop ───────────────────────────────────────────────────────────


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("agent-builder", "  ⤷ not leader — skipping (other host owns deploys)")
        return False

    specs = _list_specs("agent-spec")
    if not specs:
        log("agent-builder", "  ✓ no agent-specs pending")
        return False

    built = 0
    for spec_row in specs:
        slug = spec_row.get("slug", "")
        body = spec_row.get("body", "")
        meta = spec_row.get("metadata") or {}
        # Spec format: body is JSON or YAML-like; trust metadata if present
        if isinstance(meta, dict) and meta.get("name"):
            spec = meta
        else:
            try:
                spec = json.loads(body)
            except Exception:
                log("agent-builder",
                    f"  ⚠ skip {slug}: spec is not valid JSON")
                continue

        name = (spec.get("name") or "").strip()
        if not re.match(r"^[a-z][a-z0-9-]{2,30}$", name):
            log("agent-builder",
                f"  ⚠ skip {slug}: invalid agent name '{name}'")
            continue

        # Already built? (kv key serves as the global build ledger)
        already = _kv_get(f"agent-built.{name}")
        if already:
            continue

        log("agent-builder", f"▸ building agent '{name}' from {slug}")
        src = _generate_daemon(spec)
        if not src or len(src) < 200:
            log("agent-builder", f"  ✗ generation produced empty/tiny src for {name}")
            continue

        ok, err = _validate_python(src)
        if not ok:
            log("agent-builder",
                f"  ✗ syntax check failed for {name}: {err[:120]}")
            _memory_log("build-fail", f"agent {name} syntax error",
                        body=err, tags=["agent-builder", HOST, name])
            continue

        poll_default = int(spec.get("poll_sec", 300))
        unit_text = _generate_unit(name, poll_default)
        ok, msg = _deploy_agent(name, src, unit_text, poll_default)
        if ok:
            built += 1
            _kv_set(f"agent-built.{name}", {
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "host": HOST,
                "spec_slug": slug,
                "lines": src.count("\n"),
                "user": _detect_user(),
            })
            _memory_log("deployed",
                        f"agent {name} deployed",
                        body=(f"From spec: {slug}\n"
                              f"Lines: {src.count(chr(10))}\n"
                              f"Purpose: {spec.get('purpose','')[:200]}\n"
                              f"Built on host: {HOST}"),
                        tags=["agent-builder", HOST, name])
            log("agent-builder", f"  ✓ {name} deployed + healthy")
        else:
            log("agent-builder", f"  ✗ {name} deploy failed: {msg}")
            _memory_log("build-fail", f"agent {name} deploy error",
                        body=msg, tags=["agent-builder", HOST, name])

    if built:
        log("agent-builder", f"  ✓ deployed {built} new agent(s) this cycle")
    return False   # full sleep after each cycle


if __name__ == "__main__":
    daemon_loop("agent-builder", POLL_SEC, cycle)
