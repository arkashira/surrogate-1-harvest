# vanguard / discovery

## Final synthesized solution (correct + actionable)

**Core problem**: training/frontend call `load_dataset` at runtime, causing HF API 429s, non-reproducible epochs, and schema mismatches; no content-addressed manifest prevents CDN-only fetching; no deterministic repo mapping wastes HF commit quota; no Lightning Studio guard causes lost training on idle-stop.

**Single artifact to create**  
`/opt/axentx/vanguard/discovery/manifest.py` (one file, <150 LOC) plus a small wrapper script and cron entry.

---

### What the manifest does (unified behavior)

1. **Build phase (run once per date, e.g. on Mac after rate-limit window)**
   - Scans `enriched/{date}/` (local or HF repo) for parquet files.
   - For each file:
     - Record `path`, `sha256`, `rows`, `size`.
     - Validate schema contains `prompt` and `response` columns; fail fast otherwise.
     - Deterministic sibling assignment: `sibling = SIBLINGS[hash_slug(path) % len(SIBLINGS)]` to spread HF commit load (~640/hr across 5 siblings) instead of hitting one repo’s 128/hr cap.
   - Writes `manifest-{date}.json`.

2. **Training/frontend phase (reproducible, zero API calls)**
   - Loads manifest; never calls `load_dataset` on the full dataset.
   - For each entry:
     - Uses **CDN-only fetch** via `https://huggingface.co/datasets/{sibling}/resolve/main/{path}` (no Authorization header; avoids 429s).
     - Streams to a temp file and reads only `prompt`/`response` columns (drops `source`, `ts`, etc.) → fixes surrogate-1 schema expectations.
     - Optionally verifies `sha256` of downloaded file for content-addressed integrity.
   - Yields rows deterministically and in manifest order → reproducible epochs.

3. **Lightning Studio guard**
   - `ensure_running(teamspace, studio_name)`:
     - If studio is running → use it.
     - If stopped → start it.
     - If missing → create it.
   - Requires `LIGHTNING_API_KEY` in env; no-op with clear log if `lightning` not installed.
   - Prevents recreation waste and silent training loss on idle-stop.

---

### Implementation (final, corrected)

```python
#!/usr/bin/env python3
"""
Content-addressed manifest + CDN-only fetcher for vanguard.
Usage:
  python manifest.py build --date=2026-05-03 --repo=datasets/axentx/vanguard --local-root=.
  python manifest.py train --manifest=manifest-2026-05-03.json
"""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List

import pyarrow.parquet as pq
import requests

HF_CDN = "https://huggingface.co/datasets"
SIBLINGS = [
    "datasets/axentx/vanguard",
    "datasets/axentx/vanguard-sib1",
    "datasets/axentx/vanguard-sib2",
    "datasets/axentx/vanguard-sib3",
    "datasets/axentx/vanguard-sib4",
]


def hash_slug(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def pick_sibling(path: str) -> str:
    idx = int(hash_slug(path), 16) % len(SIBLINGS)
    return SIBLINGS[idx]


def project_to_prompt_response(parquet_path: Path) -> Iterator[Dict[str, str]]:
    tbl = pq.read_table(parquet_path, columns=["prompt", "response"])
    for i in range(tbl.num_rows):
        row = tbl.slice(i, 1).to_pydict()
        yield {"prompt": row["prompt"][0], "response": row["response"][0]}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(16384), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(date: str, repo: str, local_root: Path, out_path: Path) -> None:
    enriched_dir = local_root / "enriched" / date
    entries: List[Dict] = []

    if enriched_dir.exists():
        for p in sorted(enriched_dir.glob("*.parquet")):
            meta = pq.read_metadata(p)
            # Validate schema
            schema_names = {field.name for field in meta.schema}
            if not {"prompt", "response"}.issubset(schema_names):
                raise ValueError(f"{p} missing prompt/response columns: {schema_names}")
            entries.append({
                "path": str(p.relative_to(local_root)),
                "sha256": sha256_file(p),
                "rows": meta.num_rows,
                "size": p.stat().st_size,
                "sibling": pick_sibling(str(p.relative_to(local_root))),
            })
    else:
        # Remote build not implemented here to avoid 429 during rate-limit windows.
        # Run this locally after window.
        raise RuntimeError("HF remote build not implemented; run locally after rate-limit window.")

    manifest = {"date": date, "repo": repo, "entries": entries}
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(entries)} entries to {out_path}")


def iter_rows_cdn_only(repo: str, file_path: str, verify_sha256: str = None, project: bool = True) -> Iterator[Dict]:
    url = f"{HF_CDN}/{repo}/resolve/main/{file_path}"
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=16384):
                    f.write(chunk)
        if verify_sha256:
            actual = sha256_file(tmp_path)
            if actual != verify_sha256:
                raise ValueError(f"SHA256 mismatch for {file_path}: expected {verify_sha256}, got {actual}")
        if project:
            yield from project_to_prompt_response(tmp_path)
        else:
            tbl = pq.read_table(tmp_path)
            for i in range(tbl.num_rows):
                yield tbl.slice(i, 1).to_pydict()
    finally:
        tmp_path.unlink(missing_ok=True)


def ensure_running_studio(teamspace: str, studio_name: str):
    try:
        from lightning import Studio, Teamspace
    except ImportError:
        print("lightning not installed; skipping studio guard")
        return None
    ts = Teamspace(teamspace)
    running = [s for s in ts.studios if s.name == studio_name and s.status == "running"]
    if running:
        print(f"Using running studio: {studio_name}")
        return running[0]
    stopped = [s for s in ts.studios if s.name == studio_name and s.status == "stopped"]
    if stopped:
        print(f"Restarting stopped studio: {studio_name}")
        stopped[0].start()
        return stopped[0]
    print(f"Creating studio: {studio_name}")
    return Studio(name=studio_name, teamspace=teamspace, create_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard manifest builder/fetcher")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    build_p = subparsers.add_parser("build", help="Build manifest for a date")
    build_p.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-03")
    build_p.add_argument("--repo", default="datasets/axentx/vanguard", help="HF repo identifier")
    build_p.add_argument("--local-root", type=Path, default=Path("."), help="Local repo root")
    build_p.add_argument("--out", type=Path, help="Output manifest path")


