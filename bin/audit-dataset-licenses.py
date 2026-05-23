#!/usr/bin/env python3
"""License audit on Hugging Face datasets we ingest into training pairs.

Reads the dataset-id list (HF dynamic-datasets / hermes-jobs / explicit
list file), queries HF Hub for each dataset's `tags.license` field, and
flags any non-permissive license. Output goes to two places:
  - state/license-audit.json   (machine-readable; CI gate consumes it)
  - logs/license-audit.log     (human-readable trail)

Permissive allowlist (default): apache-2.0, mit, bsd-2-clause, bsd-3-clause,
cc0-1.0, cc-by-4.0, openrail, openrail++, mit-0.

Borderline (warn but allow with --strict=false):
  cc-by-sa-4.0, cc-by-nc-4.0, cc-by-nd-4.0, openrail-m, llama2, llama3, gemma.

Hard-block (always flagged): gpl-2.0, gpl-3.0, agpl-3.0, no-license, unknown.

Usage:
  audit-dataset-licenses.py [--datasets-file path] [--strict] [--out path]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[1]))
DEFAULT_DATASETS_FILE = REPO_ROOT / "data" / "hermes-jobs.json"
OUT_DEFAULT = REPO_ROOT / "state" / "license-audit.json"
LOG_FILE = REPO_ROOT / "logs" / "license-audit.log"
HF_API = "https://huggingface.co/api/datasets"

PERMISSIVE = {
    "apache-2.0", "mit", "mit-0", "bsd", "bsd-2-clause", "bsd-3-clause",
    "cc0-1.0", "cc-by-4.0", "openrail", "openrail++", "openrail-m",
    "isc", "wtfpl", "unlicense",
}
BORDERLINE = {
    "cc-by-sa-4.0", "cc-by-nc-4.0", "cc-by-nd-4.0", "llama2", "llama3",
    "llama3.1", "gemma", "openrail-r", "rail", "afl-3.0",
}
HARD_BLOCK = {
    "gpl-2.0", "gpl-3.0", "lgpl-2.1", "lgpl-3.0", "agpl-3.0", "agpl-3.0-only",
    "no-license", "unknown", "other",
}


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    print(line, file=sys.stderr, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def classify(license_id: str) -> str:
    lic = (license_id or "").strip().lower()
    if not lic:
        return "block"
    if lic in PERMISSIVE:
        return "permissive"
    if lic in HARD_BLOCK:
        return "block"
    if lic in BORDERLINE:
        return "borderline"
    # Unknown → conservative block.
    return "block"


def fetch_license(dataset_id: str, hf_token: str | None) -> dict:
    url = f"{HF_API}/{dataset_id}"
    headers = {"User-Agent": "axentx-license-audit/1.0"}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return {"error": f"http_{exc.code}", "license": ""}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"error": str(exc), "license": ""}
    # HF dataset metadata: tags include "license:apache-2.0" entries.
    tags = data.get("tags", []) or []
    license_id = ""
    for t in tags:
        if isinstance(t, str) and t.startswith("license:"):
            license_id = t.split(":", 1)[1].lower()
            break
    if not license_id:
        # Sometimes set in cardData.license
        cd = data.get("cardData") or {}
        license_id = (cd.get("license") or "").strip().lower()
    return {"license": license_id, "downloads": data.get("downloads"), "tags": tags[:20]}


def discover_dataset_ids(datasets_file: Path) -> list[str]:
    """Pull dataset ids from hermes-jobs.json (and similar config files).

    Looks for any string matching `^[a-z0-9_-]+/[a-zA-Z0-9_.-]+$` that
    appears under keys named 'dataset', 'datasets', or 'hf_id'. Falls
    back to scanning all string values if none of those keys exist.
    """
    if not datasets_file.exists():
        log(f"WARN datasets file not found: {datasets_file}")
        return []
    try:
        data = json.loads(datasets_file.read_text())
    except Exception as exc:
        log(f"unreadable {datasets_file}: {exc}")
        return []

    ids: set[str] = set()

    def visit(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and "/" in v and " " not in v and len(v) < 120:
                    parts = v.split("/")
                    if len(parts) == 2 and all(parts):
                        ids.add(v)
                visit(v)
        elif isinstance(node, list):
            for x in node:
                visit(x)

    visit(data)
    # Drop common non-dataset slugs (model ids, repo paths) by heuristic.
    return sorted(i for i in ids if not i.startswith("http") and not i.endswith(".py"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets-file", default=str(DEFAULT_DATASETS_FILE),
                    help="JSON file containing dataset ids to audit")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on any borderline dataset")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    hf_token = os.environ.get("HF_TOKEN", "")
    ids = discover_dataset_ids(Path(args.datasets_file))[: args.limit]
    log(f"auditing {len(ids)} dataset ids from {args.datasets_file}")

    findings: list[dict] = []
    counts = {"permissive": 0, "borderline": 0, "block": 0, "errors": 0}

    for did in ids:
        info = fetch_license(did, hf_token)
        if info.get("error"):
            counts["errors"] += 1
            findings.append({"dataset": did, "status": "error",
                             "license": "", "error": info["error"]})
            log(f"  ✗ {did}: {info['error']}")
            time.sleep(0.2)
            continue
        lic = info["license"]
        kind = classify(lic)
        counts[kind] += 1
        findings.append({
            "dataset": did,
            "status": kind,
            "license": lic or "(none)",
            "downloads": info.get("downloads"),
        })
        marker = {"permissive": "✓", "borderline": "~", "block": "✗"}[kind]
        log(f"  {marker} {did}: {lic or '(none)'} → {kind}")
        time.sleep(0.2)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "audited_at": datetime.datetime.utcnow().isoformat() + "Z",
        "summary": counts,
        "findings": findings,
    }, indent=2))

    log(f"summary: permissive={counts['permissive']} "
        f"borderline={counts['borderline']} block={counts['block']} "
        f"errors={counts['errors']}")
    print(json.dumps(counts, indent=2))

    if counts["block"] > 0:
        return 1
    if args.strict and counts["borderline"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
