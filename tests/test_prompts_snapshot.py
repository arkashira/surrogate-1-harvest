"""Snapshot tests for daemon system prompts.

Why this exists: prompts drift silently. A maintainer tweaks a daemon
"just to clarify the JSON schema", and downstream agent behavior subtly
changes — sometimes invisibly until DPO pairs start mining bad signal.

This test computes a SHA-256 hash of each daemon's `*_SYSTEM` constant
and compares it to a checked-in snapshot. Any change forces an
intentional snapshot bump (`pytest --snapshot-update`), which makes the
diff visible in code review.

How it works:
  1. We import each daemon module via importlib (file path → module),
     not regular import, because the files have hyphens in their names.
  2. We grab the named constant (`ARCH_SYSTEM`, `DEV_SYSTEM`, …)
  3. We render it with a fixed sample input by string-formatting if it
     has placeholders (most are static system strings — formatting is a
     no-op when there are no `{}`).
  4. We hash and compare against `tests/snapshots/prompts.json`.

If the snapshot file is missing, this test seeds it on first run and
passes (so a fresh checkout is not blocked); subsequent edits will then
fail the test until the maintainer reviews + updates the snapshot.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "prompts.json"

# (module-file-stem, constant-name) pairs to snapshot.
PROMPT_TARGETS = [
    ("axentx-architect-daemon", "ARCH_SYSTEM"),
    ("axentx-business-daemon", "BUSINESS_SYSTEM"),
    ("axentx-content-daemon", "CONTENT_SYSTEM"),
    ("axentx-design-thinking-daemon", "DESIGN_SYSTEM"),
    ("axentx-dev-daemon", "DEV_SYSTEM"),
    ("axentx-docs-daemon", "DOCS_SYSTEM"),
    ("axentx-marketing-daemon", "MARKETING_SYSTEM"),
    ("axentx-perf-daemon", "PERF_SYSTEM"),
    ("axentx-pm-daemon", "PM_SYSTEM"),
    ("axentx-prd-daemon", "PRD_SYSTEM"),
    ("axentx-qa-daemon", "QA_SYSTEM"),
    ("axentx-release-daemon", "REL_SYSTEM"),
    ("axentx-research-daemon", "RESEARCH_SYSTEM"),
    ("axentx-reviewer-daemon", "REVIEWER_SYSTEM"),
    ("axentx-security-daemon", "SEC_SYSTEM"),
    ("axentx-trends-daemon", "TRENDS_SYSTEM"),
    ("axentx-ux-daemon", "UX_SYSTEM"),
    ("axentx-bd-daemon", "BD_SYSTEM"),
]

# A minimal sample input — daemons that string-format their system prompt
# can render against this without raising.
SAMPLE_INPUT = {
    "project": "Costinel",
    "focus": "discovery",
    "audience": "indie founders",
    "task": "snapshot test",
    "id": "snapshot-fixture",
}


def _load_module(stem: str):
    """Load a hyphenated-name module by file path. Returns None on any
    failure so a single broken daemon file doesn't disable the whole
    snapshot test — we just skip that one prompt."""
    src = BIN_DIR / f"{stem}.py"
    if not src.exists():
        return None
    spec = importlib.util.spec_from_file_location(stem.replace("-", "_"), src)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _render(template: str) -> str:
    """If the prompt has `{name}` placeholders, fill them with SAMPLE_INPUT;
    otherwise return as-is. Failures fall back to raw template."""
    if "{" not in template:
        return template
    try:
        return template.format(**SAMPLE_INPUT)
    except (KeyError, IndexError, ValueError):
        return template


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _compute_current_snapshot() -> dict[str, str]:
    out: dict[str, str] = {}
    for stem, const_name in PROMPT_TARGETS:
        module = _load_module(stem)
        if module is None:
            continue
        prompt = getattr(module, const_name, None)
        if not isinstance(prompt, str):
            continue
        rendered = _render(prompt)
        out[f"{stem}.{const_name}"] = _digest(rendered)
    return out


def _load_snapshot() -> dict[str, str]:
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        return json.loads(SNAPSHOT_FILE.read_text())
    except Exception:
        return {}


def _save_snapshot(data: dict[str, str]) -> None:
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def test_prompts_unchanged():
    """Hash every daemon's system prompt; compare to checked-in snapshot.

    Failure = a prompt drifted. To accept the change:
        pytest tests/test_prompts_snapshot.py --update-snapshot
    """
    current = _compute_current_snapshot()
    saved = _load_snapshot()

    if not saved:
        # First run / fresh checkout — seed and pass.
        _save_snapshot(current)
        pytest.skip("snapshot file seeded — re-run to compare")

    drift = {
        k: (saved.get(k), v)
        for k, v in current.items()
        if saved.get(k) != v
    }
    missing = [k for k in saved if k not in current]

    if drift or missing:
        msg_lines = ["prompt snapshot drift detected:"]
        for k, (old, new) in drift.items():
            msg_lines.append(f"  ~ {k}: {old} → {new}")
        for k in missing:
            msg_lines.append(f"  - {k}: removed (was {saved[k]})")
        msg_lines.append("")
        msg_lines.append("If this change is intentional, regenerate:")
        msg_lines.append("  pytest tests/test_prompts_snapshot.py --update-snapshot")
        pytest.fail("\n".join(msg_lines))


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshot",
        action="store_true",
        default=False,
        help="rewrite the prompt snapshot file from current values",
    )


def pytest_configure(config):
    if config.getoption("--update-snapshot"):
        snap = _compute_current_snapshot()
        _save_snapshot(snap)
        print(f"\nupdated snapshot: {len(snap)} entries → {SNAPSHOT_FILE}")
