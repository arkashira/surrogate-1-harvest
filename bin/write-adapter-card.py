#!/usr/bin/env python3
"""Generate `README.md` model card for a Hugging Face adapter repo.

Usage:
    write-adapter-card.py <adapter-id> [--out <path>]

The card includes:
  * Base model + revision SHA (from training-config.json or HF API)
  * Training params (epochs, batch size, learning rate, LoRA rank, dtype)
  * Eval scores from state/eval-gate.last.json (if present)
  * Training data license summary from state/license-audit.json
  * Training timestamp + adapter version (semver derived from git tag)
  * Reproducibility hash of training config

Card is YAML-frontmatter + markdown so HF Hub renders it natively.
Writes to logs/adapter-cards/<adapter-id>.md by default; pass --out
to write directly into the adapter repo before push-training-to-hf.sh
uploads it.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[1]))
TRAINING_CONFIG = REPO_ROOT / "state" / "training-config.json"
EVAL_RESULT = REPO_ROOT / "state" / "eval-gate.last.json"
LICENSE_AUDIT = REPO_ROOT / "state" / "license-audit.json"
DEFAULT_OUT_DIR = REPO_ROOT / "logs" / "adapter-cards"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def fingerprint(data: dict) -> str:
    body = json.dumps(data, sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()[:12]


def format_eval(eval_data: dict) -> str:
    if not eval_data:
        return "_No eval gate result available._"
    return (
        f"| metric | value |\n"
        f"|---|---|\n"
        f"| candidate | `{eval_data.get('candidate','?')}` |\n"
        f"| baseline | `{eval_data.get('baseline','?')}` |\n"
        f"| n evaluated | {eval_data.get('n_evaluated','?')} |\n"
        f"| win rate | {eval_data.get('win_rate','?')} |\n"
        f"| win-rate Δ vs base | {eval_data.get('win_rate_delta','?')} |\n"
        f"| perplexity Δ vs base | {eval_data.get('perplexity_delta','?')} |\n"
        f"| gate passed | {eval_data.get('gate_passed','?')} |\n"
    )


def format_licenses(audit: dict) -> str:
    if not audit:
        return "_No license audit run._"
    summary = audit.get("summary", {})
    findings = audit.get("findings", [])
    blocks = [f for f in findings if f.get("status") == "block"]
    borders = [f for f in findings if f.get("status") == "borderline"]
    parts = [
        f"- permissive: **{summary.get('permissive', 0)}**",
        f"- borderline: **{summary.get('borderline', 0)}**",
        f"- blocked:    **{summary.get('block', 0)}**",
    ]
    if blocks:
        parts.append("\nBlocked datasets:")
        for f in blocks[:10]:
            parts.append(f"- `{f['dataset']}` → {f.get('license', 'unknown')}")
    if borders:
        parts.append("\nBorderline datasets:")
        for f in borders[:10]:
            parts.append(f"- `{f['dataset']}` → {f.get('license', 'unknown')}")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("adapter_id", help="HF model id, e.g. axentx/surrogate-1-v20")
    ap.add_argument("--out", default=None, help="output path (default logs/adapter-cards/<id>.md)")
    ap.add_argument("--base-model", default=None, help="override base model id")
    args = ap.parse_args()

    cfg = load_json(TRAINING_CONFIG)
    eval_data = load_json(EVAL_RESULT)
    audit = load_json(LICENSE_AUDIT)

    base_model = args.base_model or cfg.get("base_model") or "Qwen/Qwen2.5-Coder-7B-Instruct"
    base_revision = cfg.get("base_revision", "main")
    sha = git_sha()
    fp = fingerprint(cfg)
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"

    body = f"""---
license: apache-2.0
language: en
base_model: {base_model}
tags:
  - lora
  - adapter
  - axentx
  - surrogate-1
library_name: peft
---

# {args.adapter_id}

LoRA adapter trained on the axentx self-improvement training corpus.
Generated automatically by `bin/write-adapter-card.py`.

## Base model

- **id**: `{base_model}`
- **revision**: `{base_revision}`

## Training configuration

- **epochs**: {cfg.get('epochs', '?')}
- **batch size**: {cfg.get('batch_size', '?')}
- **learning rate**: {cfg.get('learning_rate', '?')}
- **LoRA rank**: {cfg.get('lora_rank', '?')}
- **LoRA alpha**: {cfg.get('lora_alpha', '?')}
- **dtype**: {cfg.get('dtype', '?')}
- **dataset hash**: `{cfg.get('dataset_hash', '?')}`
- **config fingerprint**: `{fp}`

## Eval

{format_eval(eval_data)}

## Training data licenses

{format_licenses(audit)}

## Reproducibility

- **harvest commit**: `{sha}`
- **trained at**: `{timestamp}`

To regenerate this adapter from scratch:

```bash
cd surrogate-1-harvest
git checkout {sha}
bin/kaggle-trainer.sh --config state/training-config.json
```

## License + safety

- Adapter: Apache-2.0 (matches harvest repo).
- Training data: see license summary above. Borderline-licensed sources
  are filtered before training; blocked sources never reach the trainer.
- PII scrubbed via `bin/lib/pii_scrub.py` at row-emit time.

---

_This card was auto-generated. Edit the underlying state files
(state/training-config.json, state/eval-gate.last.json,
state/license-audit.json) and re-run `bin/write-adapter-card.py` to
refresh._
"""

    if args.out:
        out_path = Path(args.out)
    else:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        slug = args.adapter_id.replace("/", "__")
        out_path = DEFAULT_OUT_DIR / f"{slug}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
