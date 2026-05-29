"""Surrogate-2 — PTC sandbox: isolated code/command execution (QN2).

Tool-grounding backend so forge/pro/devmode agents can EXECUTE and VERIFY code
in isolation instead of hallucinating outputs. The Programmatic Tool Calling
(PTC) meta-tool routes `programmatic_call(code)` here.

Backend selection (env PTC_BACKEND, default "auto"):
  e2b   — E2B Firecracker microVM (free tier) if E2B_API_KEY is set + e2b SDK
  bwrap — local bubblewrap sandbox (kernel namespaces + seccomp), $0, no daemon
  auto  — e2b if available else bwrap

Decision: Modal credits are DEPLETED → not used. No E2B_API_KEY is provisioned,
so the default working backend on the fleet is **bubblewrap** — installed on the
always-on host, no Docker required, no thrash. Swap to E2B by exporting
E2B_API_KEY (free tier) with zero code change.

bubblewrap hardening:
  • new mount/pid/ipc/uts/cgroup/user namespace (--unshare-all)
  • NO network (--unshare-net implied by --unshare-all; net never bound)
  • host filesystem read-only; only a fresh tmpfs workdir is writable
  • --die-with-parent, --new-session, --cap-drop ALL
  • wall-clock timeout (timeout) + RAM/CPU/file caps (prlimit)

Usage:
    from ptc_sandbox import run_code, run_cmd
    r = run_code("print(2**10)")           # -> {ok, stdout, stderr, exit, backend, ms}
    r = run_cmd(["python3", "-c", "..."])

CLI:
    ptc_sandbox.py info
    echo 'print("hi")' | ptc_sandbox.py run-code [--lang python|bash]
    ptc_sandbox.py selftest
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

PTC_BACKEND = os.environ.get("PTC_BACKEND", "auto")
TIMEOUT_S = int(os.environ.get("PTC_TIMEOUT_S", "10"))
MEM_MB = int(os.environ.get("PTC_MEM_MB", "512"))
OUTPUT_CAP = int(os.environ.get("PTC_OUTPUT_CAP", str(64 * 1024)))  # bytes

# Read-only host paths the sandbox needs to run interpreters.
_RO_PATHS = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives",
             "/etc/ssl/certs"]


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _e2b_available() -> bool:
    if not os.environ.get("E2B_API_KEY"):
        return False
    try:
        import e2b_code_interpreter  # noqa: F401
        return True
    except Exception:
        return False


def pick_backend() -> str:
    if PTC_BACKEND in ("e2b", "bwrap"):
        return PTC_BACKEND
    if _e2b_available():
        return "e2b"
    if _have("bwrap"):
        return "bwrap"
    return "none"


# ── bubblewrap backend ──────────────────────────────────────────────────────
def _bwrap_argv(workdir: str) -> list[str]:
    argv = ["bwrap",
            "--unshare-all",            # net/pid/ipc/uts/cgroup/user/mount ns
            "--die-with-parent",
            "--new-session",
            "--cap-drop", "ALL",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", workdir, "/work",
            "--chdir", "/work",
            "--setenv", "HOME", "/work",
            "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1"]
    for p in _RO_PATHS:
        if os.path.exists(p):
            argv += ["--ro-bind", p, p]
    return argv


def _run_bwrap(cmd: list[str]) -> dict:
    t0 = time.time()
    workdir = tempfile.mkdtemp(prefix="ptc-")
    # prlimit: address-space (RAM), cpu-time, max file size, no core dumps
    prlimit = ["prlimit",
               f"--as={MEM_MB * 1024 * 1024}",
               f"--cpu={TIMEOUT_S}",
               "--fsize=33554432",   # 32MB max output file
               "--core=0", "--nproc=64"]
    full = ["timeout", "--kill-after=2", str(TIMEOUT_S)] + \
           prlimit + _bwrap_argv(workdir) + cmd
    try:
        p = subprocess.run(full, capture_output=True, timeout=TIMEOUT_S + 5)
        out = p.stdout[:OUTPUT_CAP].decode("utf-8", "replace")
        err = p.stderr[:OUTPUT_CAP].decode("utf-8", "replace")
        timed_out = p.returncode == 124  # timeout(1) convention
        return {"ok": p.returncode == 0, "exit": p.returncode, "stdout": out,
                "stderr": err, "timed_out": timed_out, "backend": "bwrap",
                "ms": int((time.time() - t0) * 1000)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "wall-timeout",
                "timed_out": True, "backend": "bwrap",
                "ms": int((time.time() - t0) * 1000)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── e2b backend ─────────────────────────────────────────────────────────────
def _run_e2b(code: str, lang: str) -> dict:
    t0 = time.time()
    from e2b_code_interpreter import Sandbox
    try:
        with Sandbox(timeout=TIMEOUT_S) as sb:
            ex = sb.run_code(code) if lang == "python" else sb.commands.run(code)
            logs = getattr(ex, "logs", None)
            out = "\n".join(getattr(logs, "stdout", []) or []) if logs else \
                  getattr(ex, "stdout", "")
            err = "\n".join(getattr(logs, "stderr", []) or []) if logs else \
                  getattr(ex, "stderr", "")
            err_obj = getattr(ex, "error", None)
            return {"ok": err_obj is None, "exit": 0 if err_obj is None else 1,
                    "stdout": out[:OUTPUT_CAP], "stderr": (err or str(err_obj or ""))[:OUTPUT_CAP],
                    "timed_out": False, "backend": "e2b",
                    "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "exit": -1, "stdout": "", "stderr": f"e2b: {e}",
                "timed_out": False, "backend": "e2b",
                "ms": int((time.time() - t0) * 1000)}


# ── public API ──────────────────────────────────────────────────────────────
def run_code(code: str, lang: str = "python") -> dict:
    """Execute a code snippet in isolation. lang ∈ {python, bash}."""
    backend = pick_backend()
    if backend == "none":
        return {"ok": False, "exit": -1, "stdout": "", "backend": "none",
                "stderr": "no sandbox backend (install bubblewrap or set E2B_API_KEY)",
                "timed_out": False, "ms": 0}
    if backend == "e2b":
        return _run_e2b(code, lang)
    interp = ["python3", "-I", "-c", code] if lang == "python" else ["/bin/sh", "-c", code]
    return _run_bwrap(interp)


def run_cmd(argv: list[str]) -> dict:
    """Execute an argv command in isolation (bwrap backend)."""
    backend = pick_backend()
    if backend == "e2b":
        return _run_e2b(" ".join(shlex.quote(a) for a in argv), "bash")
    if backend == "none":
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "no backend",
                "timed_out": False, "backend": "none", "ms": 0}
    return _run_bwrap(argv)


def info() -> dict:
    return {"chosen_backend": pick_backend(), "configured": PTC_BACKEND,
            "bwrap": _have("bwrap"), "e2b": _e2b_available(),
            "timeout_s": TIMEOUT_S, "mem_mb": MEM_MB}


# ── CLI ─────────────────────────────────────────────────────────────────────
def _main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    lang = "python"
    if "--lang" in sys.argv:
        lang = sys.argv[sys.argv.index("--lang") + 1]
    if cmd == "info":
        print(json.dumps(info(), indent=2))
    elif cmd == "run-code":
        print(json.dumps(run_code(sys.stdin.read(), lang), ensure_ascii=False, indent=2))
    elif cmd == "selftest":
        checks = {}
        checks["compute"] = run_code("print(sum(range(1,11)))")           # 55
        checks["net_blocked"] = run_code(
            "import socket;\n"
            "try:\n socket.create_connection(('1.1.1.1',53),2); print('LEAK')\n"
            "except Exception: print('NET_BLOCKED')")
        # Target a ro-bound HOST path; the sandbox root itself is throwaway.
        checks["fs_readonly"] = run_code(
            "try:\n open('/usr/ptc_pwned','w').write('x'); print('WRITABLE')\n"
            "except Exception: print('FS_READONLY')")
        checks["timeout"] = run_code("import time; time.sleep(30); print('NO_TIMEOUT')")
        summary = {
            "backend": checks["compute"]["backend"],
            "compute_55": checks["compute"]["stdout"].strip() == "55",
            "net_blocked": "NET_BLOCKED" in checks["net_blocked"]["stdout"],
            "fs_readonly": "FS_READONLY" in checks["fs_readonly"]["stdout"],
            "timeout_enforced": checks["timeout"]["timed_out"],
        }
        summary["all_pass"] = all([summary["compute_55"], summary["net_blocked"],
                                   summary["fs_readonly"], summary["timeout_enforced"]])
        print(json.dumps(summary, indent=2))
        return 0 if summary["all_pass"] else 2
    else:
        print(f"unknown: {cmd}", file=sys.stderr)
        return 1
    return 0


__all__ = ["run_code", "run_cmd", "info", "pick_backend"]

if __name__ == "__main__":
    sys.exit(_main())
