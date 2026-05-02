# vanguard / discovery

## Final Synthesized Implementation

**Chosen approach:** Merge Candidate 1’s concrete automation and Candidate 2’s schema/projection rigor into a single robust discovery script.  
**Location:** `/opt/axentx/vanguard/discovery/run_discovery.sh` (executable).  
**Scope:** one new script + optional config + one schema-projection helper. No changes to existing source.

---

### 1) Diagnosis (resolved)
- **Discovery mechanism missing** → Add explicit top-hub + related docs query via `knowledge-rag` (best-effort) and optional market scan.
- **No market-research → RAG flow** → Chain `granite-business-research.sh` (if present) then query RAG; emit consolidated insights.
- **Missing pre-flight for top-connected hub docs** → Preflight step that reads top-hub and validates presence/staleness of key docs; warns and exits non-zero if critical docs missing (configurable).
- **No guardrails against schema heterogeneity** → Add schema projection (`project_schema.py`) that enforces `{prompt,response}` and casts via pyarrow; fail fast on unprojectable files.
- **No CDN-bypass file-list for Lightning training** → Preflight list HF folder non-recursive, emit `file-list.json`, and provide CDN-only loader snippet; avoid HF API 429.

---

### 2) Implementation

#### `/opt/axentx/vanguard/discovery/run_discovery.sh`
```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/discovery/run_discovery.sh
# Purpose: Discovery automation for vanguard (business context + data strategy + compute reuse)
# Tags: #discovery #business-research #knowledge-rag #graph #huggingface #cdn #lightning-ai
set -euo pipefail

BASE_DIR="/opt/axentx/vanguard"
CONF="${BASE_DIR}/discover.conf"
OUT_DIR="${BASE_DIR}/out/discovery"
HF_REPO="${HF_REPO:-datasets/axentx/vanguard-mirror}"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
HF_CDN_BASE="https://huggingface.co/datasets/${HF_REPO}/resolve/main"
REQUIRE_TOP_HUB="${REQUIRE_TOP_HUB:-true}"
MAX_HF_LIST_RETRIES="${MAX_HF_LIST_RETRIES:-3}"

mkdir -p "${OUT_DIR}"

log() { echo "== [discovery] $*"; }
fail() { log "ERROR: $*"; exit 1; }

# ---- 0) Load config ----
if [[ -f "${CONF}" ]]; then
  # shellcheck disable=SC1090
  . "${CONF}"
fi

# ---- 1) Business research + knowledge-rag ----
log "Running business research + knowledge-rag"
if [[ -x "${BASE_DIR}/granite-business-research.sh" ]]; then
  "${BASE_DIR}/granite-business-research.sh" --out "${OUT_DIR}/market-insights.json" || true
fi

TOP_HUB_JSON="${OUT_DIR}/top-hub.json"
RELATED_JSON="${OUT_DIR}/related-hub.json"
if command -v knowledge-rag &>/dev/null; then
  knowledge-rag top-hub --format json > "${TOP_HUB_JSON}" || true
  # Default hub fallback: MOC
  HUB_NAME="$(jq -r '.hub // "MOC"' "${TOP_HUB_JSON}" 2>/dev/null || echo MOC)"
  knowledge-rag related --hub "${HUB_NAME}" --format json > "${RELATED_JSON}" || true
else
  log "knowledge-rag not found — skipping RAG insights"
  echo '{}' > "${TOP_HUB_JSON}"
  echo '[]' > "${RELATED_JSON}"
fi

# ---- 2) Preflight: top-connected hub docs ----
log "Preflight: validating top-connected hub docs"
MISSING_CRITICAL=false
if [[ "${REQUIRE_TOP_HUB}" == "true" ]]; then
  # Expect at least one top-hub entry with a doc_path/file_path field
  if ! jq -e '.hub // .name // .title // empty' "${TOP_HUB_JSON}" &>/dev/null; then
    log "WARNING: no clear top-hub entry found"
    MISSING_CRITICAL=true
  fi
  # Check related docs for presence of expected files under BASE_DIR/docs (if paths provided)
  if jq -e '.[]?.doc_path // .[]?.file_path // empty' "${RELATED_JSON}" &>/dev/null; then
    while IFS= read -r docp; do
      [[ -z "${docp}" ]] && continue
      if [[ ! -f "${BASE_DIR}/docs/${docp}" && ! -f "${docp}" ]]; then
        log "WARNING: referenced doc missing: ${docp}"
      fi
    done < <(jq -r '.[]?.doc_path // .[]?.file_path // empty' "${RELATED_JSON}")
  fi
fi
if [[ "${MISSING_CRITICAL}" == "true" ]]; then
  fail "Critical top-hub missing; aborting discovery (set REQUIRE_TOP_HUB=false to bypass)"
fi

# ---- 3) HF CDN bypass: pre-list files for date folder ----
log "Pre-listing HF dataset paths (CDN strategy)"
FILE_LIST="${OUT_DIR}/file-list.json"
if command -v huggingface-cli &>/dev/null; then
  attempt=0
  until (( attempt >= MAX_HF_LIST_RETRIES )); do
    if huggingface-cli repo tree "${HF_REPO}" --path "${DATE_FOLDER}" --recursive false --json \
      2>/dev/null | jq -r '.files[]?.path // empty' | grep -v '/$' > "${FILE_LIST}"; then
      break
    fi
    attempt=$((attempt+1))
    sleep $((attempt*2))
  done
  # Ensure valid JSON even on failure
  if [[ ! -s "${FILE_LIST}" ]]; then
    echo "[]" > "${FILE_LIST}"
  fi
else
  echo "[]" > "${FILE_LIST}"
  log "huggingface-cli not found — empty file list produced"
fi

# ---- 4) Schema projection guardrail ----
log "Applying schema projection guardrail"
cat > "${OUT_DIR}/project_schema.py" <<'PY'
import json, sys, os, pyarrow as pa, pyarrow.csv as csv, pyarrow.json as pj
from pathlib import Path

HF_REPO = os.getenv("HF_REPO", "datasets/axentx/vanguard-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
FILE_LIST_PATH = os.getenv("FILE_LIST_PATH", "out/discovery/file-list.json")
OUT_SCHEMA = os.getenv("OUT_SCHEMA", "out/discovery/projected.arrow")

REQUIRED_FIELDS = {"prompt", "response"}
ALLOWED_TYPES = {pa.string()}

def project_to_pair(obj):
    if isinstance(obj, dict):
        prompt = obj.get("prompt") or obj.get("text") or obj.get("input") or ""
        response = obj.get("response") or obj.get("completion") or obj.get("output") or ""
        return {"prompt": str(prompt), "response": str(response)}
    return {"prompt": str(obj), "response": ""}

def infer_and_project(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        if path.suffix == ".json" or path.suffix == ".jsonl":
            if path.suffix == ".json":
                data = json.loads(path.read_text())
                rows = data if isinstance(data, list) else [data]
            else:
                rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        elif path.suffix in {".csv", ".tsv"}:
            table = csv.read_csv(path)
            rows = table.to_pylist()
        else:
            return None
        projected = [project_to_pair(r) for r in rows]
        table = pa.Table.from_pylist(projected, schema=pa.schema([
            pa.field("prompt", pa.string()),
            pa.field("response", pa.string())
        ]))
        return table
    except Exception as e:
        print(f"Projection failed for {path}: {e}", file=sys.stderr)
        return None

def main():
    file_list = json.loads(Path(FILE_LIST_PATH).read_text())
    out_tables = []
    for rel in file_list:
        if not rel or not rel.startswith(DATE_FOLDER):
            continue
        # For local testing, allow FILE_ROOT override
        root = os.getenv("FILE_ROOT", "")
        local_path = Path(root) / rel if root else Path(rel)
        tbl
