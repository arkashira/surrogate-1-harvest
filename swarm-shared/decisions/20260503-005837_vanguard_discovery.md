# vanguard / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged both candidates, kept only the correct/lightweight approaches, and removed contradictions.

Key decisions:
- **Python-only manifest builder** (not Bash wrapper) — simpler, portable, easier to maintain.
- **Non-recursive `list_repo_tree`** — one API call per dateFolder, minimal quota.
- **CDN-only URLs** — `https://huggingface.co/datasets/<repo>/resolve/main/<dateFolder>/<file>` during training (no auth, no API quota).
- **Lightning Studio reuse + idle-restart guard** — single helper that reuses a running studio and restarts it if stopped by idle timeout.
- **Schema-drift resilience** — read only required columns (`prompt`, `response`) with `pyarrow`; skip unreadable files.
- **No recursive enumeration and no `load_dataset(streaming=True)` on heterogeneous repo** — prevents `pyarrow.CastError` and rate-limit amplification.

---

### 1) Manifest builder (single Python file)

`/opt/axentx/vanguard/scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for one repo + dateFolder.
Usage:
    python3 build_manifest.py axentx/datasets 2026-05-03 manifest.json
"""
import json
import sys
from pathlib import Path

from huggingface_hub import list_repo_tree

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{file_path}"


def build_manifest(repo: str, date_folder: str, out_path: Path):
    # Non-recursive: one top-level API call per dateFolder
    items = list_repo_tree(repo, path=date_folder, recursive=False)

    manifest = []
    for item in items:
        if item.type != "file":
            continue
        manifest.append(
            {
                "repo": repo,
                "path": item.path,
                "cdn_url": CDN_TEMPLATE.format(
                    repo=repo, date_folder=date_folder, file_path=item.path
                ),
                "size": getattr(item, "size", None),
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(manifest)} entries to {out_path}")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: build_manifest.py <repo> <dateFolder> <out.json>")
        sys.exit(1)
    build_manifest(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

---

### 2) Training script patch (CDN-only + Studio reuse + idle-restart)

`/opt/axentx/vanguard/train.py` (minimal, focused changes)

```python
import argparse
import io
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import requests

# Optional Lightning helpers (lightweight; safe if not installed)
try:
    from lightning import Machine, Studio, Teamspace

    _LIGHTNING_AVAILABLE = True
except Exception:
    _LIGHTNING_AVAILABLE = False


def load_manifest(manifest_path: Path):
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


def stream_cdn_parquet(cdn_url: str, columns=None, timeout: int = 30):
    """CDN-only fetch; no Authorization header (bypasses HF API rate limits)."""
    resp = requests.get(cdn_url, timeout=timeout)
    resp.raise_for_status()
    table = pq.read_table(io.BytesIO(resp.content), columns=columns)
    return table.to_pandas()


def get_or_create_studio(name: str, machine_type=None):
    """
    Reuse a running Lightning Studio if present.
    If stopped (idle timeout), restart it.
    If absent, create it.
    """
    if not _LIGHTNING_AVAILABLE:
        print("Lightning SDK not available; skipping studio management.")
        return None

    teamspace = Teamspace()
    studio = next(
        (s for s in teamspace.studios if s.name == name),
        None,
    )

    if studio is None:
        print(f"Creating studio '{name}'...")
        studio = Studio(
            name=name,
            machine=machine_type or Machine.L40S,
            cloud="lightning-public-prod",
            create_ok=True,
        )
        return studio

    if studio.status == "Running":
        print(f"Reusing running studio: {name}")
        return studio

    if studio.status == "Stopped":
        print(f"Studio '{name}' is stopped (likely idle timeout). Restarting...")
        studio.start(machine=machine_type or Machine.L40S)
        return studio

    print(f"Studio '{name}' is {studio.status}; waiting or recreate as needed.")
    return studio


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="JSON manifest from build_manifest.py")
    parser.add_argument("--project", default="vanguard")
    parser.add_argument("--studio-machine", default=None, help="Lightning machine type (optional)")
    args = parser.parse_args()

    # Optional: reuse/create Lightning Studio
    studio = get_or_create_studio(args.project, args.studio_machine)

    manifest = load_manifest(Path(args.manifest))
    if not manifest:
        raise RuntimeError("Manifest is empty")

    # Lightweight CDN-only data loading (no HF API calls during training)
    rows = []
    required_cols = {"prompt", "response"}
    for entry in manifest:
        try:
            df = stream_cdn_parquet(entry["cdn_url"], columns=list(required_cols))
            # Keep only available required columns; ignore schema drift
            available = [c for c in required_cols if c in df.columns]
            if not available:
                print(f"Skip {entry['path']}: missing required columns")
                continue
            rows.append(df[available])
        except Exception as exc:
            print(f"Skip {entry['path']}: {exc}")
            continue

    if not rows:
        raise RuntimeError("No rows loaded from manifest")

    data = pd.concat(rows, ignore_index=True)
    print(f"Loaded {len(data)} rows via CDN")

    # Your training logic here
    # model = ...
    # trainer.fit(model, data)

    # Optional: ensure studio is running (restart if stopped by idle timeout)
    if studio and studio.status != "Running":
        print("Studio not running; attempting restart...")
        studio.start(machine=args.studio_machine or Machine.L40S)


if __name__ == "__main__":
    main()
```

---

### 3) Usage (concrete and reproducible)

1) Build manifest once per dateFolder (run after HF API window clears):

```bash
cd /opt/axentx/vanguard
python3 scripts/build_manifest.py axentx/datasets 2026-05-03 manifest-2026-05-03.json
```

Verify output:

```bash
head manifest-2026-05-03.json
```

2) Train with CDN-only manifest (no HF API quota during data load):

```bash
cd /opt/axentx/vanguard
python3 train.py --manifest manifest-2026-05-03.json --project vanguard
```

Expected logs:
- `Loaded N rows via CDN`
- No `429` or `pyarrow.CastError` from schema drift
- `Reusing running studio: vanguard` (or creation message)

3) Rate-limit verification:
- While training runs, outbound requests should only be to `resolve/main/...` (CDN).
- No authenticated `/api/` calls during data loading.

---

### 4) Why this resolves the contradictions and risks

- **Rate limits**: Manifest built once with a single `list_repo_tree` call; training uses CDN-only URLs (no auth, no API quota).
- **Schema drift**: Reads only required columns with `pyarrow`; skips unreadable files instead of failing.
- **Lightning
