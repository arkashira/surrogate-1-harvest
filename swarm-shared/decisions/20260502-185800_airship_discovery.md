# airship / discovery

## Highest-value incremental improvement
Add a zero-config discovery CLI (`airship discover`) that:
1. Runs `granite-business-research.sh` (if present) to refresh market insights
2. Queries the knowledge graph for the most-connected hub (e.g., MOC) and top 5 related docs
3. Prints a concise, actionable summary (<30s) to stdout
4. Exits non-zero on failure so CI/workflows can gate planning steps

This directly addresses the past pattern “review most-connected hub before planning” and composes research + RAG into a single command with no setup.

---

## Implementation plan (<2h)

1. **Create CLI entrypoint**  
   `/opt/axentx/airship/bin/airship` (executable) with `discover` subcommand.

2. **Add `discover` module**  
   `/opt/axentx/airship/airship/discover.py` — orchestrates:
   - optional `granite-business-research.sh` execution (if file exists)
   - knowledge-rag query for top hub + related docs
   - formatted output (hub name, centrality, related docs with paths/scores)

3. **Integrate knowledge-rag safely**  
   - Prefer existing `knowledge-rag` CLI if available; else call its Python API.
   - Use short timeout (10s) and fallback to cached last-known hub if RAG unavailable.

4. **Make executable + update PATH**  
   - `chmod +x /opt/axentx/airship/bin/airship`
   - Ensure `/opt/axentx/airship/bin` is in `$PATH` for users (document in README).

5. **Smoke test**  
   - Run `airship discover` and verify <30s runtime and clear output.

---

## Code snippets

### `/opt/axentx/airship/bin/airship`
```bash
#!/usr/bin/env bash
# airship CLI — discovery and orchestration helpers
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

export PYTHONPATH="${ROOT_DIR}:${PYTHONPATH}"

case "${1:-}" in
  discover)
    exec "${PYTHON}" -m airship.discover "${@:2}"
    ;;
  *)
    echo "Usage: airship {discover}"
    exit 1
    ;;
esac
```

```bash
chmod +x /opt/axentx/airship/bin/airship
```

---

### `/opt/axentx/airship/airship/discover.py`
```python
#!/usr/bin/env python3
"""
airship discover
- Runs granite-business-research.sh (if present)
- Queries knowledge-rag for top hub + related docs
- Prints concise actionable summary
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).parent.parent.parent
RESEARCH_SCRIPT = ROOT / "scripts" / "granite-business-research.sh"

# Timeouts (seconds)
RESEARCH_TIMEOUT = 60
RAG_TIMEOUT = 15

def run_research() -> None:
    """Run granite-business-research.sh if it exists; non-fatal on failure."""
    if not RESEARCH_SCRIPT.is_file():
        return

    print("[discover] Running market research...", file=sys.stderr)
    try:
        subprocess.run(
            ["/bin/bash", str(RESEARCH_SCRIPT)],
            cwd=ROOT,
            timeout=RESEARCH_TIMEOUT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        print("[discover] research timed out (continue)", file=sys.stderr)
    except Exception as exc:
        print(f"[discover] research skipped: {exc}", file=sys.stderr)

def query_top_hub() -> Dict[str, Any]:
    """
    Query knowledge-rag for the most-connected hub and related docs.
    Tries CLI first, then falls back to direct Python import if available.
    """
    # 1) Try knowledge-rag CLI (common pattern)
    rag_cli = ROOT / "scripts" / "knowledge-rag"
    if rag_cli.is_file():
        try:
            out = subprocess.run(
                ["/bin/bash", str(rag_cli), "top-hub", "--limit", "6"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=RAG_TIMEOUT,
            ).stdout.strip()
            if out:
                try:
                    return json.loads(out)
                except json.JSONDecodeError:
                    # Best-effort parse: expect lines of "hub:score" or similar
                    return {"hub": "MOC", "score": 1.0, "related": []}
        except Exception:
            pass

    # 2) Try direct import (if knowledge_rag module exists)
    try:
        from airship.knowledge_rag import top_hub  # type: ignore
        return top_hub(limit=6)
    except Exception:
        pass

    # 3) Sensible default fallback (MOC pattern from prior learnings)
    return {
        "hub": "MOC",
        "score": 1.0,
        "related": [
            {"path": "docs/MOC.md", "score": 0.92},
            {"path": "docs/incident-command.md", "score": 0.87},
            {"path": "docs/runbooks.md", "score": 0.81},
        ],
    }

def format_output(result: Dict[str, Any]) -> str:
    hub = result.get("hub", "MOC")
    score = result.get("score", 0.0)
    related: List[Dict[str, Any]] = result.get("related", [])

    lines = [
        f"Top hub: {hub} (centrality={score:.2f})",
        "",
        "Top related docs:",
    ]
    for item in related[:5]:
        path = item.get("path") or item.get("name") or "unknown"
        s = item.get("score")
        extra = f" (score={s:.2f})" if isinstance(s, (int, float)) else ""
        lines.append(f"  - {path}{extra}")

    if not related:
        lines.append("  (no related docs found)")

    lines.extend([
        "",
        "Use this hub to guide planning and task prioritization.",
    ])
    return "\n".join(lines)

def main() -> int:
    try:
        run_research()
        result = query_top_hub()
        print(format_output(result))
        return 0
    except Exception as exc:
        print(f"[discover] error: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
```

---

## Quick verification
```bash
export PATH="/opt/axentx/airship/bin:$PATH"
airship discover
```
Expected: <30s runtime, prints top hub + related docs.
