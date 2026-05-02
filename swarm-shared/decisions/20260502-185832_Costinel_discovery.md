# Costinel / discovery

## Final Synthesis: Highest-Value, Correct, Actionable Plan (<2h)

**Chosen scope:** Build a lightweight, deterministic discovery module that produces:
- `discovery/manifest.json` (machine-readable inventory of cloud accounts and datasets)
- `discovery/top-hub-insights.md` (human-readable onboarding/RAG context)
- Optional health probe (`ready()`) for orchestration

**Why this wins:**  
- Directly unblocks onboarding, RAG, and surrogate-1 training file listing (per past lessons).  
- Uses existing patterns (top-hub review, knowledge-RAG, file manifest for CDN bypass).  
- Fits <2h: small Python module + one CLI + minimal docs.  
- Correctness guardrails: no network calls during manifest generation; deterministic, testable outputs.

---

## Implementation Plan (corrected + concrete)

1. **Create discovery module**  
   Files: `discovery/__init__.py`, `discovery/cli.py`, `discovery/manifest.py`, `discovery/top_hub.py`, `discovery/probe.py`.

2. **Implement manifest generator** (deterministic, no network)  
   - Scan configured cloud connector configs (local files only) and local dataset folders.  
   - Emit `discovery/manifest.json` with:
     - `cloud_accounts`: source, account_id, region, last_sync, config_file  
     - `datasets`: path, file_count, parquet_count, schema_hint, cdn_url_template  
     - `generated_at`, `version`, `project`, `notes`  
   - Guarantee: zero SDK/network calls; only reads local files/dirs.

3. **Implement top-hub insight reporter**  
   - Read top-hub doc if available (`knowledge/top-hub.md`, `knowledge/MOC.md`, fallback to `README.md`).  
   - Summarize recent decisions (`decisions/*.md`, latest 3).  
   - Emit `discovery/top-hub-insights.md` with summary, decisions, and references.

4. **Add CLI command**  
   - `python -m discovery.cli generate --out-dir discovery` → writes manifest + insights.  
   - `python -m discovery.cli show` → prints summary to stdout.  
   - Keep interface minimal and scriptable.

5. **Add health/probe stub**  
   - `discovery/probe.py` with `ready() -> bool` returning True when manifest can be generated (or lightweight checks).  
   - Keep it optional but present for orchestration.

6. **Update README**  
   - One-line usage and where outputs live.

7. **Verify**  
   - Run generator and confirm valid JSON.  
   - Confirm no network calls (CDN bypass pattern: file list produced once).  
   - Confirm outputs are deterministic given same repo state.

---

## Code Snippets (final, corrected)

### discovery/manifest.py
```python
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any

PROJECT_ROOT = Path(__file__).parent.parent.parent

def _scan_cloud_accounts() -> List[Dict[str, Any]]:
    accounts = []
    config_dir = PROJECT_ROOT / "config" / "cloud"
    if config_dir.is_dir():
        for cfg in config_dir.glob("*.json"):
            try:
                with open(cfg, encoding="utf-8") as f:
                    data = json.load(f)
                accounts.append({
                    "source": data.get("provider", "unknown"),
                    "account_id": data.get("account_id", ""),
                    "region": data.get("region", "global"),
                    "last_sync": data.get("last_sync", None),
                    "config_file": str(cfg.relative_to(PROJECT_ROOT))
                })
            except Exception:
                continue
    return accounts

def _scan_dataset_folders() -> List[Dict[str, Any]]:
    datasets = []
    data_root = PROJECT_ROOT / "data"
    if data_root.is_dir():
        for d in data_root.iterdir():
            if not d.is_dir():
                continue
            files = list(d.rglob("*"))
            parquet_files = [p for p in files if p.suffix.lower() == ".parquet"]
            datasets.append({
                "path": str(d.relative_to(PROJECT_ROOT)),
                "file_count": len(files),
                "parquet_count": len(parquet_files),
                "schema_hint": "mixed" if len(parquet_files) == 0 else "parquet",
                # CDN bypass template for public datasets (if mirrored to HF)
                "cdn_url_template": "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
            })
    return datasets

def generate_manifest() -> Dict[str, Any]:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "4.2.0",
        "project": "Costinel",
        "cloud_accounts": _scan_cloud_accounts(),
        "datasets": _scan_dataset_folders(),
        "notes": "File list intended for CDN-bypass ingestion (zero API calls during training)."
    }
    return manifest

def write_manifest(output_path: Path) -> Path:
    manifest = generate_manifest()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return output_path
```

### discovery/top_hub.py
```python
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.parent.parent

def _read_top_hub_doc() -> str:
    candidates = [
        PROJECT_ROOT / "knowledge" / "top-hub.md",
        PROJECT_ROOT / "knowledge" / "MOC.md",
        PROJECT_ROOT / "README.md"
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return ""

def _recent_decisions() -> str:
    decisions_dir = PROJECT_ROOT / "decisions"
    if not decisions_dir.is_dir():
        return ""
    items = []
    for f in sorted(decisions_dir.glob("*.md"), reverse=True)[:3]:
        items.append(f"- {f.name}")
    return "\n".join(items) if items else "No decisions found."

def generate_top_hub_insights() -> str:
    content = _read_top_hub_doc()
    decisions = _recent_decisions()

    lines = [
        "# Top-Hub Insights (auto-generated)",
        f"_Generated: {datetime.now(timezone.utc).isoformat()}_",
        "",
        "## Summary",
        (content[:1200] + ("..." if len(content) > 1200 else "")) if content else "No top-hub document found.",
        "",
        "## Recent Decisions",
        decisions,
        "",
        "## References",
        "- Knowledge graph / top-hub docs (if available) provide context for RAG and onboarding.",
        "- Manifest: `discovery/manifest.json`",
    ]
    return "\n".join(lines)

def write_top_hub_insights(output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(generate_top_hub_insights(), encoding="utf-8")
    return output_path
```

### discovery/cli.py
```python
import argparse
import json
from pathlib import Path

from .manifest import write_manifest, generate_manifest
from .top_hub import write_top_hub_insights

def main() -> None:
    parser = argparse.ArgumentParser(description="Costinel discovery utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="Generate manifest and top-hub insights")
    gen.add_argument("--out-dir", type=Path, default=Path("discovery"), help="Output directory")

    sub.add_parser("show", help="Show summary of current discovery outputs")

    args = parser.parse_args()
    out_dir = Path(args.out_dir)

    if args.cmd == "generate":
        manifest_path = out_dir / "manifest.json"
        insights_path = out_dir / "top-hub-insights.md"
        write_manifest(manifest_path)
        write_top_hub_insights(insights_path)
        print(f"Generated: {manifest_path}")
        print(f"Generated: {insights_path}")
    elif args.cmd == "show":
       
